"""Tests for core.reconcile — canonical reconciliation module (Phase B.2).

12 tests covering reconcile_fills and reconcile_positions.
Uses mock broker (no real API calls) and real isolated SQLite DB via the
global _isolate_prod_db autouse fixture from conftest.py.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ── Project path ──────────────────────────────────────────────────────────────
ATLAS_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ATLAS_ROOT))


# ═══════════════════════════════════════════════════════════════════════════════
# Helpers — mock broker
# ═══════════════════════════════════════════════════════════════════════════════

def _make_order(
    order_id: str,
    ticker: str,
    side: str,          # "buy" or "sell"
    status: str,        # "filled", "accepted", "canceled" …
    fill_price: float = 0.0,
    filled_qty: int = 0,
    requested_qty: int = 0,
    order_type: str = "market",
) -> MagicMock:
    """Build a minimal OrderResult-like MagicMock."""
    order = MagicMock()
    order.order_id = order_id
    order.ticker = ticker

    side_obj = MagicMock()
    side_obj.value = side
    order.side = side_obj

    status_obj = MagicMock()
    status_obj.value = status
    order.status = status_obj

    order.fill_price = fill_price if fill_price else None
    order.filled_qty = filled_qty if filled_qty else None
    order.requested_qty = requested_qty
    order.raw = {
        "submitted_at": "2026-04-29T00:00:00+00:00",
        "order_type": order_type,
        "order_class": "simple",
    }
    return order


def _make_position(ticker: str, shares: int = 10, entry_price: float = 100.0) -> MagicMock:
    """Build a minimal PositionInfo-like MagicMock."""
    pos = MagicMock()
    pos.ticker = ticker
    pos.shares = shares
    pos.entry_price = entry_price
    return pos


class MockBroker:
    """Minimal mock broker for reconcile tests."""

    def __init__(self, positions=None, orders=None):
        self._positions = positions or []
        self._orders = orders or []

    def get_positions(self):
        return self._positions

    def get_history_orders(self, days=30):
        return self._orders


# ═══════════════════════════════════════════════════════════════════════════════
# Helpers — DB seeding
# ═══════════════════════════════════════════════════════════════════════════════

def _insert_open_trade(ticker: str, universe: str, strategy: str = "test_strat",
                       entry_price: float = 100.0, shares: int = 10) -> None:
    """Insert a minimal open trade into the isolated DB."""
    from db import atlas_db
    atlas_db.record_trade_entry(
        ticker=ticker,
        strategy=strategy,
        universe=universe,
        entry_price=entry_price,
        shares=shares,
        stop_price=None,
        take_profit=None,
        confidence=0.0,
        regime_state=None,
        direction="long",
    )


def _seed_broker_order_row(
    order_id: str,
    symbol: str,
    side: str,
    status: str,
    fill_price: float | None,
) -> None:
    """Insert a row directly into broker_orders for 'already known' scenarios."""
    from db import atlas_db
    with atlas_db.get_db() as conn:
        conn.execute(
            """INSERT OR IGNORE INTO broker_orders
               (order_id, symbol, side, qty, filled_qty, fill_price, status,
                submitted_at, raw_alpaca_json, last_synced_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                order_id, symbol, side, 10.0,
                10.0 if fill_price else None,
                fill_price,
                status,
                "2026-04-29T00:00:00+00:00",
                "{}",
                "2026-04-29T00:00:00+00:00",
            ),
        )


def _count_open_trades(ticker: str = None, universe: str = None) -> int:
    """Count open trades in the isolated DB."""
    from db import atlas_db
    with atlas_db.get_db() as conn:
        if ticker and universe:
            return conn.execute(
                "SELECT COUNT(*) FROM trades WHERE status='open' AND ticker=? AND universe=?",
                (ticker, universe),
            ).fetchone()[0]
        if ticker:
            return conn.execute(
                "SELECT COUNT(*) FROM trades WHERE status='open' AND ticker=?",
                (ticker,),
            ).fetchone()[0]
        return conn.execute(
            "SELECT COUNT(*) FROM trades WHERE status='open'"
        ).fetchone()[0]


def _count_broker_orders() -> int:
    """Count rows in broker_orders table."""
    from db import atlas_db
    with atlas_db.get_db() as conn:
        return conn.execute("SELECT COUNT(*) FROM broker_orders").fetchone()[0]


# ═══════════════════════════════════════════════════════════════════════════════
# reconcile_fills — 7 tests
# ═══════════════════════════════════════════════════════════════════════════════

class TestReconcileFills:

    def _module(self):
        """Import core.reconcile fresh."""
        import importlib
        import core.reconcile as m
        return m

    @patch("core.reconcile._get_market_tickers", return_value={"AAPL", "MSFT"})
    def test_no_broker_orders_no_changes(self, mock_tickers):
        """Empty broker → empty report."""
        from db import atlas_db
        broker = MockBroker(orders=[])
        m = self._module()
        report = m.reconcile_fills("sp500", broker, atlas_db, dry_run=True)

        assert report.fills_added == []
        assert report.fills_updated == []
        assert report.trades_opened == []
        assert report.trades_closed == []
        assert report.errors == []
        assert not report.changed

    @patch("core.reconcile._get_market_tickers", return_value={"AAPL"})
    def test_new_buy_fill_creates_trade(self, mock_tickers):
        """Broker has BUY fill, SQLite has no trade → report says trades_opened."""
        from db import atlas_db
        order = _make_order("ord-001", "AAPL", "buy", "filled",
                            fill_price=150.0, filled_qty=10, requested_qty=10)
        broker = MockBroker(orders=[order])

        m = self._module()
        report = m.reconcile_fills("sp500", broker, atlas_db, dry_run=True)

        assert len(report.fills_added) == 1
        assert report.fills_added[0]["ticker"] == "AAPL"
        assert len(report.trades_opened) == 1  # 0 = dry_run placeholder
        assert report.trades_opened[0] == 0
        assert report.errors == []

    @patch("core.reconcile._get_market_tickers", return_value={"AAPL"})
    def test_existing_trade_no_duplicate(self, mock_tickers):
        """BUY fill in broker_orders AND open trade in SQLite → no changes."""
        from db import atlas_db

        # Pre-seed: broker_orders row + open trade
        _seed_broker_order_row("ord-002", "AAPL", "buy", "filled", fill_price=150.0)
        _insert_open_trade("AAPL", "sp500", entry_price=150.0)

        order = _make_order("ord-002", "AAPL", "buy", "filled",
                            fill_price=150.0, filled_qty=10, requested_qty=10)
        broker = MockBroker(orders=[order])

        m = self._module()
        report = m.reconcile_fills("sp500", broker, atlas_db, dry_run=True)

        # Order was already in broker_orders AND no status change → no fills_added
        assert report.fills_added == []
        assert report.fills_updated == []
        assert report.trades_opened == []
        assert report.trades_closed == []

    @patch("core.reconcile._get_market_tickers", return_value={"AAPL"})
    def test_new_sell_fill_closes_trade(self, mock_tickers):
        """Broker has SELL stop fill, SQLite has open trade → report says trades_closed."""
        from db import atlas_db

        # Pre-seed: open trade for AAPL in sp500
        _insert_open_trade("AAPL", "sp500", entry_price=150.0)

        order = _make_order("ord-003", "AAPL", "sell", "filled",
                            fill_price=140.0, filled_qty=10, requested_qty=10,
                            order_type="stop")
        broker = MockBroker(orders=[order])

        m = self._module()
        report = m.reconcile_fills("sp500", broker, atlas_db, dry_run=True)

        assert len(report.fills_added) == 1  # new SELL order seen
        assert len(report.trades_closed) == 1
        assert report.trades_closed[0] == 0  # dry_run placeholder
        assert report.errors == []

    @patch("core.reconcile._get_market_tickers", return_value={"AAPL"})
    def test_dry_run_no_writes(self, mock_tickers):
        """dry_run=True must not write any DB rows."""
        from db import atlas_db

        order = _make_order("ord-004", "AAPL", "buy", "filled",
                            fill_price=200.0, filled_qty=5, requested_qty=5)
        broker = MockBroker(orders=[order])

        before_broker_orders = _count_broker_orders()
        before_open_trades = _count_open_trades("AAPL", "sp500")

        m = self._module()
        report = m.reconcile_fills("sp500", broker, atlas_db, dry_run=True)

        # Report says something would change
        assert len(report.fills_added) == 1
        assert len(report.trades_opened) == 1

        # But DB must be unchanged
        assert _count_broker_orders() == before_broker_orders
        assert _count_open_trades("AAPL", "sp500") == before_open_trades

    def test_market_id_filter_excludes_other_markets(self):
        """sp500 reconcile ignores commodity_etfs orders (GLD)."""
        from db import atlas_db

        # sp500 tickers: AAPL only; commodity_etfs tickers: GLD
        with patch(
            "core.reconcile._get_market_tickers",
            side_effect=lambda market: {"AAPL"} if market == "sp500" else {"GLD"},
        ):
            gld_order = _make_order("ord-005", "GLD", "buy", "filled",
                                    fill_price=300.0, filled_qty=2, requested_qty=2)
            aapl_order = _make_order("ord-006", "AAPL", "buy", "filled",
                                     fill_price=150.0, filled_qty=5, requested_qty=5)
            broker = MockBroker(orders=[aapl_order, gld_order])

            m = self._module()
            report = m.reconcile_fills("sp500", broker, atlas_db, dry_run=True)

        # Only AAPL should appear
        tickers_seen = {f["ticker"] for f in report.fills_added}
        assert "GLD" not in tickers_seen
        assert "AAPL" in tickers_seen

    @patch("core.reconcile._get_market_tickers", return_value={"AAPL"})
    def test_partial_fills_aggregated(self, mock_tickers):
        """Partial fill (status change accepted→filled) captured as fills_updated;
        trade opened exactly once — not double-counted on second BUY for same ticker."""
        from db import atlas_db

        # First call: AAPL accepted (partial, not yet filled)
        _seed_broker_order_row("ord-007", "AAPL", "buy", "accepted", fill_price=None)

        # Broker now reports same order as FILLED (full fill)
        order_filled = _make_order("ord-007", "AAPL", "buy", "filled",
                                   fill_price=155.0, filled_qty=10, requested_qty=10)
        broker = MockBroker(orders=[order_filled])

        m = self._module()
        report = m.reconcile_fills("sp500", broker, atlas_db, dry_run=True)

        # Existing row status changed accepted → filled: fills_updated, not fills_added
        assert len(report.fills_added) == 0
        assert len(report.fills_updated) == 1
        assert report.fills_updated[0]["ticker"] == "AAPL"
        # Trade should be reported as would-open (fill_price went from None → 155)
        assert len(report.trades_opened) == 1


# ═══════════════════════════════════════════════════════════════════════════════
# reconcile_positions — 5 tests
# ═══════════════════════════════════════════════════════════════════════════════

class TestReconcilePositions:

    def _module(self):
        import core.reconcile as m
        return m

    @patch("core.reconcile._get_market_tickers", return_value={"AAPL"})
    @patch("core.reconcile._get_other_market_tickers", return_value=set())
    def test_clean_state_no_drift(self, mock_other, mock_tickers):
        """Broker matches SQLite → empty drift_detected."""
        from db import atlas_db

        _insert_open_trade("AAPL", "sp500", shares=10)
        broker = MockBroker(positions=[_make_position("AAPL", shares=10)])

        m = self._module()
        report = m.reconcile_positions("sp500", broker, atlas_db, dry_run=True)

        assert report.drift_detected == []
        assert report.errors == []

    @patch("core.reconcile._get_market_tickers", return_value={"AAPL", "MSFT"})
    @patch("core.reconcile._get_other_market_tickers", return_value=set())
    def test_broker_orphan_detected(self, mock_other, mock_tickers):
        """Broker has MSFT, SQLite has no open trade → BROKER_ORPHAN."""
        from db import atlas_db

        broker = MockBroker(positions=[_make_position("MSFT", shares=5)])

        m = self._module()
        report = m.reconcile_positions("sp500", broker, atlas_db, dry_run=True)

        types = [d["type"] for d in report.drift_detected]
        assert "BROKER_ORPHAN" in types
        orphan = next(d for d in report.drift_detected if d["type"] == "BROKER_ORPHAN")
        assert orphan["ticker"] == "MSFT"

    @patch("core.reconcile._get_market_tickers", return_value={"AAPL"})
    @patch("core.reconcile._get_other_market_tickers", return_value=set())
    def test_sqlite_orphan_detected(self, mock_other, mock_tickers):
        """SQLite has open trade, broker has no position → SQLITE_ORPHAN (MU class)."""
        from db import atlas_db

        _insert_open_trade("AAPL", "sp500", shares=10)
        broker = MockBroker(positions=[])  # empty — position gone from broker

        m = self._module()
        report = m.reconcile_positions("sp500", broker, atlas_db, dry_run=True)

        types = [d["type"] for d in report.drift_detected]
        assert "SQLITE_ORPHAN" in types
        orphan = next(d for d in report.drift_detected if d["type"] == "SQLITE_ORPHAN")
        assert orphan["ticker"] == "AAPL"
        assert "MU" in orphan["details"] or "trade #" in orphan["details"]

    @patch("core.reconcile._get_market_tickers", return_value={"AAPL"})
    @patch("core.reconcile._get_other_market_tickers", return_value=set())
    def test_qty_drift_detected(self, mock_other, mock_tickers):
        """Broker has 5 shares, SQLite shows 10 → QTY_DRIFT."""
        from db import atlas_db

        _insert_open_trade("AAPL", "sp500", shares=10)
        broker = MockBroker(positions=[_make_position("AAPL", shares=5)])  # qty mismatch

        m = self._module()
        report = m.reconcile_positions("sp500", broker, atlas_db, dry_run=True)

        types = [d["type"] for d in report.drift_detected]
        assert "QTY_DRIFT" in types
        drift = next(d for d in report.drift_detected if d["type"] == "QTY_DRIFT")
        assert drift["ticker"] == "AAPL"
        assert "broker=5" in drift["details"]
        assert "SQLite=10" in drift["details"]

    @patch("core.reconcile._get_market_tickers", return_value={"AAPL"})
    @patch("core.reconcile._get_other_market_tickers", return_value=set())
    def test_dry_run_reports_but_no_action(self, mock_other, mock_tickers):
        """reconcile_positions is report-only; dry_run=True, no DB changes either way."""
        from db import atlas_db

        # Drift scenario: AAPL in broker but not SQLite
        broker = MockBroker(positions=[_make_position("AAPL", shares=5)])

        before_open = _count_open_trades()

        m = self._module()
        report = m.reconcile_positions("sp500", broker, atlas_db, dry_run=True)

        # Drift detected
        assert len(report.drift_detected) == 1
        assert report.drift_detected[0]["type"] == "BROKER_ORPHAN"

        # But nothing written (reconcile_positions never writes — even without dry_run)
        assert _count_open_trades() == before_open
