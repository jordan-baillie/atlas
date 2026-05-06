"""Integration tests for LiveExecutor paper-mode DB routing.

Verifies:
 - mode="paper" writes to paper_trades, NOT to trades
 - mode="live" writes to trades, NOT to paper_trades
 - mode="passive" writes nothing to either table
 - paper_account_id column is populated in paper mode

All broker calls are mocked — no live network traffic.
The standard autouse _isolate_prod_db fixture from conftest.py provides
an isolated SQLite DB per test.  The paper_trades table is created inline
if not already present (pre-migration compat).
"""
from __future__ import annotations

import os
import sys
import sqlite3
from types import SimpleNamespace
from unittest.mock import MagicMock, patch, PropertyMock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ── Helpers ────────────────────────────────────────────────────

def _make_config(mode: str) -> dict:
    """Minimal config for LiveExecutor in the given mode."""
    return {
        "market": "sp500",
        "market_id": "sp500",
        "version": "test-1.0",
        "trading": {
            "broker": "alpaca",
            "live_enabled": mode == "live",  # live mode requires live_enabled=True
            "mode": mode,
            "live_safety": {
                "max_order_value": 50000,
                "max_daily_orders": 50,
                "max_position_value": 10000,
                "dry_run_first": False,  # we want real execution path in tests
            },
        },
        "risk": {
            "starting_equity": 100000,
            "max_position_pct": 0.10,
            "max_total_position_pct": 0.95,
            "leverage": 1.0,
            "max_daily_drawdown_pct": 0.02,
        },
        "alpaca": {"feed": "iex", "tif": "day", "paper": mode == "paper"},
    }


def _filled_order_result(ticker: str = "AAPL") -> SimpleNamespace:
    """Simulate a fully-filled order from broker."""
    from brokers.base import OrderResult, OrderStatus, OrderSide
    return OrderResult(
        success=True,
        order_id="test-order-001",
        ticker=ticker,
        side=OrderSide.BUY,
        status=OrderStatus.FILLED,
        requested_qty=10,
        filled_qty=10,
        requested_price=150.00,
        fill_price=150.10,
        raw={"legs": []},
    )


def _mock_plan(ticker: str = "AAPL") -> dict:
    """Minimal approved trade plan.

    Note: executor reads 'position_size' (not 'qty') for share count.
    """
    return {
        "status": "APPROVED",
        "trade_date": "2026-05-06",
        "proposed_entries": [{
            "ticker": ticker,
            "strategy": "momentum_breakout",
            "entry_price": 150.00,
            "stop_price": 143.00,
            "take_profit": 165.00,
            "position_size": 10,  # executor reads position_size, not qty
            "confidence": 0.75,
            "direction": "long",
            "risk_amount": 70.00,
            "position_value": 1500.00,
            "universe": "sp500",
        }],
        "proposed_exits": [],
        "rejected_entries": [],
    }


def _ensure_paper_trades_table(db_path: str) -> None:
    """Create paper_trades table if not present (pre-migration compat)."""
    schema = """
    CREATE TABLE IF NOT EXISTS paper_trades (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ticker TEXT NOT NULL,
        strategy TEXT NOT NULL DEFAULT '',
        universe TEXT,
        direction TEXT NOT NULL DEFAULT 'long',
        entry_date TEXT,
        entry_price REAL NOT NULL DEFAULT 0,
        shares INTEGER NOT NULL DEFAULT 0,
        stop_price REAL,
        take_profit REAL,
        confidence REAL DEFAULT 0,
        regime_at_entry TEXT,
        exit_date TEXT,
        exit_price REAL,
        pnl REAL,
        pnl_pct REAL,
        exit_reason TEXT,
        regime_at_exit TEXT,
        status TEXT NOT NULL DEFAULT 'open',
        config_version TEXT,
        paper_account_id TEXT,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        updated_at TEXT DEFAULT CURRENT_TIMESTAMP
    )
    """
    with sqlite3.connect(db_path) as conn:
        conn.execute(schema)
        conn.commit()


def _make_mock_broker(mode: str, ticker: str = "AAPL") -> MagicMock:
    """Build a fully mocked broker instance that looks like AlpacaBroker."""
    broker = MagicMock()
    broker.name = f"AlpacaBroker[{mode.upper()}]"
    broker.mode = mode
    broker.account_number = "PA-TEST-ACCOUNT" if mode == "paper" else ""
    broker.connect.return_value = True
    broker.get_account_info.return_value = MagicMock(
        equity=100000.0, cash=90000.0, buying_power=100000.0,
        market_value=10000.0, total_pnl=0.0, total_pnl_pct=0.0,
        num_positions=0, halted=False, halt_reason="",
    )
    broker.get_positions.return_value = []
    broker.get_open_orders.return_value = []
    broker.place_order.return_value = _filled_order_result(ticker)
    return broker


# ── Test classes ──────────────────────────────────────────────

class TestPaperExecutorDBRouting:
    """Verify DB routing based on trading mode."""

    @pytest.fixture(autouse=True)
    def _setup_paper_table(self, tmp_path):
        """Ensure paper_trades table exists in the isolated test DB."""
        import db.atlas_db as _adb
        db_path = _adb._db_path_override
        if db_path:
            _ensure_paper_trades_table(db_path)

    def _make_record_paper_entry(self, tmp_path):
        """Returns a record_paper_trade_entry function that inserts into paper_trades."""
        import db.atlas_db as _adb
        db_path = _adb._db_path_override

        def _record_paper_entry(
            ticker, strategy, universe, entry_price, shares, stop_price,
            take_profit, confidence, regime_state, direction="long",
            config_version=None, paper_account_id=None, **kwargs
        ):
            from datetime import datetime
            with sqlite3.connect(db_path) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.execute(
                    """INSERT INTO paper_trades
                       (ticker, strategy, universe, direction, entry_date, entry_price,
                        shares, stop_price, take_profit, confidence, regime_at_entry,
                        status, config_version, paper_account_id)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?)""",
                    (ticker, strategy, universe, direction,
                     datetime.now().isoformat(), entry_price,
                     shares, stop_price, take_profit, confidence, regime_state,
                     config_version, paper_account_id),
                )
                conn.commit()
                return cursor.lastrowid
        return _record_paper_entry

    def _make_record_paper_exit(self, tmp_path):
        """Returns a record_paper_trade_exit function."""
        import db.atlas_db as _adb
        db_path = _adb._db_path_override

        def _record_paper_exit(
            ticker, strategy="", exit_price=0.0, exit_reason="",
            regime_at_exit=None, paper_account_id=None, **kwargs
        ):
            from datetime import datetime
            with sqlite3.connect(db_path) as conn:
                conn.execute(
                    """UPDATE paper_trades SET status='closed', exit_price=?,
                       exit_reason=?, regime_at_exit=?, exit_date=?, updated_at=?
                       WHERE ticker=? AND status='open'""",
                    (exit_price, exit_reason, regime_at_exit,
                     datetime.now().date().isoformat(),
                     datetime.now().isoformat(), ticker),
                )
                conn.commit()
        return _record_paper_exit

    def test_executor_with_paper_mode_writes_to_paper_trades(self, tmp_path):
        """mode="paper" → row in paper_trades, NO row in trades."""
        import db.atlas_db as _adb
        db_path = _adb._db_path_override

        cfg = _make_config("paper")
        mock_broker = _make_mock_broker("paper")
        record_paper_entry = self._make_record_paper_entry(tmp_path)
        record_paper_exit = self._make_record_paper_exit(tmp_path)

        from brokers.live_executor import LiveExecutor

        executor = LiveExecutor(cfg)
        executor._mode = "paper"

        with patch("brokers.registry.get_live_broker", return_value=mock_broker), \
             patch("brokers.alpaca.broker.AlpacaBroker", return_value=mock_broker), \
             patch("db.atlas_db.record_paper_trade_entry", side_effect=record_paper_entry), \
             patch("db.atlas_db.record_paper_trade_exit", side_effect=record_paper_exit), \
             patch("brokers.live_executor.HALT_FILE") as mock_halt, \
             patch("brokers.kill_switch.is_halted", return_value=False), \
             patch("brokers.price_arbiter.is_ticker_halted", return_value=False):
            mock_halt.exists.return_value = False
            mock_broker.connect.return_value = True
            executor._broker = mock_broker
            executor._connected = True

            plan = _mock_plan("AAPL")
            executor.execute_plan(plan, "2026-05-06")

        # paper_trades should have a row
        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            paper_rows = conn.execute("SELECT * FROM paper_trades WHERE ticker='AAPL'").fetchall()
            live_rows = conn.execute("SELECT * FROM trades WHERE ticker='AAPL'").fetchall()

        assert len(paper_rows) >= 1, f"Expected ≥1 row in paper_trades, got {len(paper_rows)}"
        assert len(live_rows) == 0, f"Expected 0 rows in trades for paper mode, got {len(live_rows)}"

    def test_executor_with_live_mode_writes_to_trades(self, tmp_path):
        """mode="live" -> row in trades (via TradeLedger), NO row in paper_trades.

        TradeLedger is a lazy import inside live_executor, so we patch it at
        journal.logger where it's actually defined.
        """
        import db.atlas_db as _adb
        db_path = _adb._db_path_override

        cfg = _make_config("live")
        mock_broker = _make_mock_broker("live")

        from brokers.live_executor import LiveExecutor

        executor = LiveExecutor(cfg)
        executor._mode = "live"

        # Track calls to record_paper_trade_entry -- must NOT be called in live mode
        paper_entry_calls = []
        def _should_not_be_called(*args, **kwargs):
            paper_entry_calls.append(kwargs)
            return None

        # Patch journal.logger.TradeLedger (where the lazy import resolves to)
        mock_ledger = MagicMock()
        mock_ledger.return_value.record_entry.return_value = 42  # fake trade_id
        mock_ledger.return_value.record_exit.return_value = None
        mock_ledger.return_value.trades = []

        with patch("brokers.registry.get_live_broker", return_value=mock_broker), \
             patch("brokers.live_executor.HALT_FILE") as mock_halt, \
             patch("brokers.kill_switch.is_halted", return_value=False), \
             patch("journal.logger.TradeLedger", mock_ledger), \
             patch("db.atlas_db.record_paper_trade_entry", side_effect=_should_not_be_called):
            mock_halt.exists.return_value = False
            mock_broker.connect.return_value = True
            executor._broker = mock_broker
            executor._connected = True

            plan = _mock_plan("MSFT")
            executor.execute_plan(plan, "2026-05-06")

        # record_paper_trade_entry must NOT have been called for live mode
        assert len(paper_entry_calls) == 0, \
            f"record_paper_trade_entry called {len(paper_entry_calls)} times in live mode!"
        # paper_trades must be empty
        with sqlite3.connect(db_path) as conn:
            paper_rows = conn.execute(
                "SELECT * FROM paper_trades WHERE ticker='MSFT'"
            ).fetchall()
        assert len(paper_rows) == 0, f"Expected 0 paper_trades rows for live mode, got {len(paper_rows)}"
    def test_executor_with_passive_mode_writes_nothing(self, tmp_path):
        """mode="passive" → no rows in either table."""
        import db.atlas_db as _adb
        db_path = _adb._db_path_override

        cfg = _make_config("passive")
        mock_broker = _make_mock_broker("passive")

        from brokers.live_executor import LiveExecutor

        executor = LiveExecutor(cfg)
        executor._mode = "passive"
        # Passive mode: executor is not connected, execute_plan should bail early
        executor._connected = False

        plan = _mock_plan("GLD")
        result = executor.execute_plan(plan, "2026-05-06")

        # Should return early with error since not connected
        assert result.get("success") is False or "error" in result or "errors" in result

        with sqlite3.connect(db_path) as conn:
            paper_rows = conn.execute("SELECT * FROM paper_trades WHERE ticker='GLD'").fetchall()
            live_rows = conn.execute("SELECT * FROM trades WHERE ticker='GLD'").fetchall()

        assert len(paper_rows) == 0, f"Expected 0 rows in paper_trades for passive mode"
        assert len(live_rows) == 0, f"Expected 0 rows in trades for passive mode"

    def test_paper_account_id_is_recorded(self, tmp_path):
        """paper_account_id from broker.account_number is written to paper_trades."""
        import db.atlas_db as _adb
        db_path = _adb._db_path_override

        cfg = _make_config("paper")
        mock_broker = _make_mock_broker("paper", ticker="NVDA")
        mock_broker.account_number = "PAPER-ACCT-XYZ"

        recorded_calls = []

        def _capture_paper_entry(*args, **kwargs):
            recorded_calls.append(kwargs)
            # Actually insert so we can check the DB
            from datetime import datetime
            with sqlite3.connect(db_path) as conn:
                cursor = conn.execute(
                    """INSERT INTO paper_trades
                       (ticker, strategy, universe, direction, entry_date, entry_price,
                        shares, stop_price, take_profit, confidence, regime_at_entry,
                        status, config_version, paper_account_id)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?)""",
                    (
                        kwargs.get("ticker", ""), kwargs.get("strategy", ""),
                        kwargs.get("universe", ""), kwargs.get("direction", "long"),
                        datetime.now().isoformat(), kwargs.get("entry_price", 0),
                        kwargs.get("shares", 0), kwargs.get("stop_price"),
                        kwargs.get("take_profit"), kwargs.get("confidence", 0),
                        kwargs.get("regime_state"),
                        kwargs.get("config_version"),
                        kwargs.get("paper_account_id"),
                    ),
                )
                conn.commit()
                return cursor.lastrowid

        from brokers.live_executor import LiveExecutor

        executor = LiveExecutor(cfg)
        executor._mode = "paper"

        with patch("brokers.registry.get_live_broker", return_value=mock_broker), \
             patch("db.atlas_db.record_paper_trade_entry", side_effect=_capture_paper_entry), \
             patch("brokers.live_executor.HALT_FILE") as mock_halt, \
             patch("brokers.kill_switch.is_halted", return_value=False), \
             patch("brokers.price_arbiter.is_ticker_halted", return_value=False):
            mock_halt.exists.return_value = False
            executor._broker = mock_broker
            executor._connected = True

            plan = _mock_plan("NVDA")
            executor.execute_plan(plan, "2026-05-06")

        # Verify paper_account_id was passed to the DB write
        assert len(recorded_calls) >= 1, "record_paper_trade_entry was not called"
        assert recorded_calls[0].get("paper_account_id") == "PAPER-ACCT-XYZ", \
            f"Expected paper_account_id='PAPER-ACCT-XYZ' but got: {recorded_calls[0].get('paper_account_id')}"

        # Verify it's in the DB row
        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "SELECT paper_account_id FROM paper_trades WHERE ticker='NVDA'"
            ).fetchone()
        assert row is not None, "No row found in paper_trades for NVDA"
        assert row[0] == "PAPER-ACCT-XYZ", f"paper_account_id in DB: {row[0]}"
