"""Task #315 — reconcile_exit_fills idempotency + natural-key UNIQUE INDEX tests.

Tests:
1. reconcile_exit_fills skips a fill already recorded in SQLite (idempotency guard).
2. reconcile_exit_fills is stable on a second consecutive call.
3. A new fill (no prior DB row) passes through to record_exit normally.
4. The uq_trades_natural_key UNIQUE INDEX blocks raw duplicate inserts.
5. Different DATE(exit_date) with same price/shares/ticker is allowed.
6. Open trades (exit_date=NULL) are not covered by the natural-key index.
7. Index SQL fragment contains DATE(exit_date), exit_price, shares.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _db_path() -> str:
    """Return the isolated DB path set by the autouse _isolate_prod_db fixture."""
    import db.atlas_db as _adb
    return _adb._db_path_override  # type: ignore[return-value]


def _seed_closed_trade(
    db_path: str,
    ticker: str,
    exit_date: str,
    exit_price: float,
    shares: int,
    strategy: str = "momentum_breakout",
) -> int:
    """Insert a minimal open trade then close it; return the row id."""
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO trades(ticker, strategy, universe, entry_date, entry_price,
                               shares, status)
            VALUES (?, ?, 'sp500', ?, ?, ?, 'open')
            """,
            (ticker, strategy, exit_date[:10], round(exit_price * 0.99, 4), shares),
        )
        tid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute(
            """
            UPDATE trades
               SET status     = 'closed',
                   exit_date  = ?,
                   exit_price = ?,
                   pnl        = ROUND((? - entry_price) * shares, 2),
                   superseded = 0
             WHERE id = ?
            """,
            (exit_date, exit_price, exit_price, tid),
        )
        conn.commit()
        return tid


def _count_closed(db_path: str, ticker: str) -> int:
    with sqlite3.connect(db_path) as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM trades WHERE ticker=? AND status='closed'",
            (ticker,),
        ).fetchone()[0]


def _make_filled_sell_order(
    ticker: str,
    price: float,
    qty: int,
    filled_at: datetime,
    order_id: str | None = None,
) -> MagicMock:
    """Build a minimal Alpaca-order-like mock for a filled SELL."""
    order = MagicMock()
    order.id = order_id or f"ord-{ticker}-abc123"
    order.symbol = ticker
    order.side = MagicMock()
    order.side.value = "sell"
    order.status = MagicMock()
    order.status.value = "filled"
    order.filled_avg_price = price
    order.filled_qty = str(qty)
    order.qty = str(qty)
    order.client_order_id = f"atlas_sl_{ticker}_001"
    order.filled_at = filled_at
    return order


def _make_executor() -> object:
    """Build a LiveExecutor without calling __init__ (mirrors existing test pattern)."""
    from brokers.live_executor import LiveExecutor

    executor = object.__new__(LiveExecutor)
    executor.config = {"market_id": "sp500", "live_enabled": False, "risk": {}}
    executor._connected = True
    executor._broker = MagicMock()
    executor._broker._trade_client = MagicMock()

    from brokers.routing_policy import BrokerRoutingPolicy
    executor._policy = BrokerRoutingPolicy(
        {"market_id": "sp500", "live_enabled": False, "risk": {}},
        market_id="sp500",
    )
    return executor


# ---------------------------------------------------------------------------
# 1+2+3: Idempotency — reconcile_exit_fills must skip already-recorded fills
# ---------------------------------------------------------------------------

class TestReconcilerIdempotent:
    """reconcile_exit_fills must skip a fill already in SQLite (guard #315)."""

    def test_reconciler_skips_already_recorded_fill(self):
        """Seed SYK as closed in SQLite; reconciler must not call record_exit."""
        db_path = _db_path()
        ticker = "SYK"
        exit_price = 286.68
        shares = 1
        filled_at = datetime(2026, 5, 9, 8, 1, 4, tzinfo=timezone.utc)

        # Seed the already-recorded closed trade
        _seed_closed_trade(db_path, ticker, "2026-05-09T08:01:04", exit_price, shares)
        before_count = _count_closed(db_path, ticker)
        assert before_count == 1

        executor = _make_executor()
        mock_order = _make_filled_sell_order(ticker, exit_price, shares, filled_at)

        mock_ledger = MagicMock()
        mock_ledger.trades = []  # no exit order IDs known to the ledger
        mock_regime = MagicMock()
        mock_regime.classify_current.return_value.state.value = "bull_risk_on"

        # TradeLedger is imported locally inside reconcile_exit_fills via
        # "from journal.logger import TradeLedger" — must patch at source module.
        with (
            patch("journal.logger.TradeLedger", return_value=mock_ledger),
            patch("brokers.live_executor._get_regime_model", return_value=mock_regime),
            patch.object(
                executor._broker, "_broker_call", return_value=[mock_order]
            ),
            patch("brokers.live_portfolio.LivePortfolio") as mock_lp_cls,
        ):
            mock_lp = MagicMock()
            mock_lp.broker_data_valid = False
            mock_lp_cls.return_value = mock_lp

            result = executor.reconcile_exit_fills()

        # Guard fired — nothing new recorded
        assert result == [], f"Expected empty reconciled list, got: {result}"
        assert _count_closed(db_path, ticker) == before_count, (
            "Closed trade count changed — idempotency guard failed"
        )
        mock_ledger.record_exit.assert_not_called()

    def test_reconciler_idempotent_on_second_call(self):
        """Call reconcile_exit_fills twice after seeding; count unchanged both times."""
        db_path = _db_path()
        ticker = "IDEMPTEST"
        exit_price = 100.0
        shares = 2
        filled_at = datetime(2026, 5, 10, 14, 30, 0, tzinfo=timezone.utc)

        # Simulate what a successful first run would have written
        _seed_closed_trade(db_path, ticker, "2026-05-10T14:30:00", exit_price, shares)
        count_after_seed = _count_closed(db_path, ticker)
        assert count_after_seed == 1

        executor = _make_executor()
        mock_order = _make_filled_sell_order(ticker, exit_price, shares, filled_at)
        mock_ledger = MagicMock()
        mock_ledger.trades = []
        mock_regime = MagicMock()
        mock_regime.classify_current.return_value.state.value = "transition_uncertain"

        with (
            patch("journal.logger.TradeLedger", return_value=mock_ledger),
            patch("brokers.live_executor._get_regime_model", return_value=mock_regime),
            patch.object(
                executor._broker, "_broker_call", return_value=[mock_order]
            ),
            patch("brokers.live_portfolio.LivePortfolio") as mock_lp_cls,
        ):
            mock_lp = MagicMock()
            mock_lp.broker_data_valid = False
            mock_lp_cls.return_value = mock_lp

            result1 = executor.reconcile_exit_fills()
            result2 = executor.reconcile_exit_fills()

        assert result1 == [], "1st call with already-seeded row: expected empty"
        assert result2 == [], "2nd call: expected empty (idempotent)"
        assert _count_closed(db_path, ticker) == 1, (
            "Count changed — double-record on consecutive calls"
        )

    def test_new_fill_not_in_db_passes_through(self):
        """A fill with no prior SQLite row must pass through to record_exit."""
        db_path = _db_path()
        ticker = "NEWFILL"
        exit_price = 55.0
        shares = 3
        filled_at = datetime(2026, 5, 11, 9, 30, 0, tzinfo=timezone.utc)

        # Nothing seeded in SQLite for NEWFILL
        assert _count_closed(db_path, ticker) == 0

        executor = _make_executor()
        mock_order = _make_filled_sell_order(ticker, exit_price, shares, filled_at)
        mock_ledger = MagicMock()
        mock_ledger.trades = []
        mock_regime = MagicMock()
        mock_regime.classify_current.return_value.state.value = "bull_risk_on"

        with (
            patch("journal.logger.TradeLedger", return_value=mock_ledger),
            patch("brokers.live_executor._get_regime_model", return_value=mock_regime),
            patch.object(
                executor._broker, "_broker_call", return_value=[mock_order]
            ),
            patch("brokers.live_portfolio.LivePortfolio") as mock_lp_cls,
        ):
            mock_lp = MagicMock()
            mock_lp.broker_data_valid = False
            mock_lp_cls.return_value = mock_lp

            result = executor.reconcile_exit_fills()

        # Guard passed (no existing row) — record_exit was called
        mock_ledger.record_exit.assert_called_once()
        assert len(result) == 1, f"Expected 1 reconciled exit, got: {result}"


# ---------------------------------------------------------------------------
# 4+5+6+7: UNIQUE INDEX — raw SQL constraints
# ---------------------------------------------------------------------------

class TestNaturalKeyIndex:
    """uq_trades_natural_key must block duplicate inserts at the DB level."""

    def test_unique_index_blocks_dup_insert(self):
        """Two rows with the same (ticker, DATE(exit_date), exit_price, shares)
        and status='closed' must trigger IntegrityError on the second insert."""
        db_path = _db_path()

        with sqlite3.connect(db_path) as conn:
            # First insert — must succeed
            conn.execute(
                """
                INSERT INTO trades(ticker, strategy, universe, entry_date, entry_price,
                                   shares, status, exit_date, exit_price, pnl, superseded)
                VALUES ('DUPTEST', 'test_strat', 'sp500', '2026-05-11', 99.0,
                        1, 'closed', '2026-05-11T00:01:00', 100.0, 1.0, 0)
                """
            )
            conn.commit()

        # Second insert with same DATE(exit_date)='2026-05-11' — must fail
        with pytest.raises(sqlite3.IntegrityError):
            with sqlite3.connect(db_path) as conn:
                conn.execute(
                    """
                    INSERT INTO trades(ticker, strategy, universe, entry_date, entry_price,
                                       shares, status, exit_date, exit_price, pnl, superseded)
                    VALUES ('DUPTEST', 'test_strat', 'sp500', '2026-05-11', 99.0,
                            1, 'closed', '2026-05-11T12:00:00', 100.0, 1.0, 0)
                    """
                )
                conn.commit()

    def test_different_date_allowed(self):
        """Same (ticker, exit_price, shares) with DIFFERENT DATE(exit_date) is allowed."""
        db_path = _db_path()

        with sqlite3.connect(db_path) as conn:
            conn.executemany(
                """
                INSERT INTO trades(ticker, strategy, universe, entry_date, entry_price,
                                   shares, status, exit_date, exit_price, pnl, superseded)
                VALUES (?, 'test_strat', 'sp500', '2026-05-09', 99.0,
                        2, 'closed', ?, 200.0, 2.0, 0)
                """,
                [
                    ("DIFFDATE", "2026-05-10T08:00:00"),
                    ("DIFFDATE", "2026-05-11T08:00:00"),  # different date → must be allowed
                ],
            )
            conn.commit()

        with sqlite3.connect(db_path) as conn:
            cnt = conn.execute(
                "SELECT COUNT(*) FROM trades WHERE ticker='DIFFDATE' AND status='closed'"
            ).fetchone()[0]
        assert cnt == 2, f"Expected 2 rows for different exit dates, got {cnt}"

    def test_open_trades_not_covered_by_index(self):
        """Rows with exit_date=NULL (open trades) must not be covered by
        uq_trades_natural_key — the index is partial (WHERE exit_date IS NOT NULL)."""
        db_path = _db_path()

        with sqlite3.connect(db_path) as conn:
            # A single open trade for OPENTRD (idx_trades_unique_open limits one per
            # ticker+universe; this just confirms uq_trades_natural_key doesn't fire).
            conn.execute(
                """
                INSERT INTO trades(ticker, strategy, universe, entry_date, entry_price,
                                   shares, status)
                VALUES ('OPENTRD', 'test_strat', 'sp500', '2026-05-11', 99.0, 1, 'open')
                """
            )
            conn.commit()

        with sqlite3.connect(db_path) as conn:
            cnt = conn.execute(
                "SELECT COUNT(*) FROM trades WHERE ticker='OPENTRD' AND status='open'"
            ).fetchone()[0]
        assert cnt == 1

    def test_natural_key_index_exists(self):
        """uq_trades_natural_key must appear in sqlite_master with the correct SQL."""
        db_path = _db_path()

        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "SELECT name, sql FROM sqlite_master "
                "WHERE type='index' AND name='uq_trades_natural_key'"
            ).fetchone()

        assert row is not None, (
            "uq_trades_natural_key index not found in sqlite_master. "
            "Was schema.sql updated and init_db() run?"
        )
        name, sql = row
        assert name == "uq_trades_natural_key"
        assert "DATE(exit_date)" in sql, (
            f"Expected DATE(exit_date) in index SQL, got: {sql}"
        )
        assert "exit_price" in sql, f"Expected exit_price in index SQL, got: {sql}"
        assert "shares" in sql, f"Expected shares in index SQL, got: {sql}"
        assert "exit_date IS NOT NULL" in sql, (
            f"Expected 'exit_date IS NOT NULL' partial-filter in index SQL, got: {sql}"
        )
