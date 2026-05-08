"""Regression tests: SAME_BAR_ROUND_TRIP idempotency precheck (Fix #312).

Root cause: `reconcile_entry_fills` runs every 15 min from cron and
re-detects the same buy/sell order pairs (MCHP, FSLR, EBAY) at Alpaca.
The in-memory dedup set (`_exit_order_ids_for_recon`) is built from
`_ledger.trades`, but `TradeLedger._save()` became a no-op in Wave D1
(2026-04-28) so `_ledger.trades` is always empty between cron runs.

Fix: idempotent SQLite precheck at the top of `_record_same_bar_round_trip`.
Queries `trades` for an existing non-superseded closed row today with
matching (ticker, ROUND(pnl,2)). Skips all side-effects on hit. Fail-open
on DB error.

Run:
    cd /root/atlas && python3 -m pytest tests/test_same_bar_round_trip_idempotency.py -v --timeout=30
"""
from __future__ import annotations

import sys
import logging
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

import db.atlas_db as _adb


# ---------------------------------------------------------------------------
# Helpers — mirrors test_same_bar_round_trip.py patterns
# ---------------------------------------------------------------------------

def _make_order(
    symbol: str,
    side: str,
    fill_price: float = 100.0,
    qty: int = 3,
    client_order_id: str = "atlas_stop_test",
    order_type: str = "stop",
) -> MagicMock:
    """Construct a fake filled Alpaca order object."""
    o = MagicMock()
    o.id = f"order-{symbol}-{side}-{id(o)}"
    o.symbol = symbol
    o.side = MagicMock()
    o.side.value = side
    o.status = MagicMock()
    o.status.value = "filled"
    o.filled_avg_price = fill_price
    o.filled_qty = qty
    o.qty = qty
    o.filled_at = datetime(2026, 5, 8, 13, 30, 0, tzinfo=timezone.utc)
    o.client_order_id = client_order_id
    o.order_type = MagicMock()
    o.order_type.value = order_type
    return o


def _make_executor() -> object:
    """Return a LiveExecutor stub wired for live mode (no real broker connect)."""
    from brokers.live_executor import LiveExecutor

    cfg = {
        "market_id": "sp500",
        "version": "test-v1",
        "trading": {
            "mode": "live",
            "live_enabled": True,
            "broker": "alpaca",
            "live_safety": {
                "dry_run_first": False,
                "max_order_value": 50_000,
                "max_daily_orders": 50,
                "max_daily_loss_pct": 0.05,
            },
        },
        "risk": {
            "starting_equity": 10_000.0,
            "max_risk_per_trade_pct": 0.02,
            "max_open_positions": 10,
            "leverage": 1.0,
        },
        "fees": {"commission_per_trade": 0, "commission_pct": 0},
    }
    ex = LiveExecutor.__new__(LiveExecutor)
    ex.config = cfg
    ex._connected = True
    ex._broker = MagicMock()
    ex._halted = False
    # _policy = None => skips paper-mode branch, falls through to live path
    ex._policy = None
    return ex


def _insert_closed_trade(
    ticker: str,
    pnl: float,
    *,
    exit_date: str | None = None,
    superseded: int = 0,
    strategy: str = "reconciled",
) -> int:
    """Insert a closed trade row into the isolated test DB. Returns row id."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    _exit_date = exit_date if exit_date is not None else today
    # entry_date must be <= exit_date (CHECK constraint); use exit_date as entry_date
    _entry_date = _exit_date
    with _adb.get_db() as conn:
        cur = conn.execute(
            "INSERT INTO trades "
            "(ticker, strategy, universe, direction, entry_date, entry_price, shares, "
            "exit_date, pnl, status, superseded) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (ticker, strategy, "sp500", "long", _entry_date, 100.0, 3,
             _exit_date, pnl, "closed", superseded),
        )
        conn.commit()
        return cur.lastrowid


def _call_sbrt(
    executor,
    ticker: str,
    buy_price: float,
    sell_price: float,
    qty: int = 3,
    mock_ledger: MagicMock | None = None,
) -> MagicMock:
    """Call _record_same_bar_round_trip directly. Returns (ledger, mock_tg) tuple."""
    if mock_ledger is None:
        mock_ledger = MagicMock()
        mock_ledger.record_entry.return_value = None
        mock_ledger.record_exit.return_value = None

    buy_order = _make_order(ticker, "buy", fill_price=buy_price, qty=qty,
                            client_order_id="atlas_entry_test", order_type="limit")
    sell_order = _make_order(ticker, "sell", fill_price=sell_price, qty=qty,
                             client_order_id="atlas_stop_test", order_type="stop")
    plan_by_ticker = {
        ticker: {
            "strategy": "momentum_breakout",
            "stop_price": sell_price - 1.0,
            "entry_price": buy_price,
            "confidence": 0.75,
        }
    }
    with patch("utils.telegram.send_message") as mock_tg:
        executor._record_same_bar_round_trip(
            buy_order=buy_order,
            sell_order=sell_order,
            ledger=mock_ledger,
            plan_by_ticker=plan_by_ticker,
            regime="bull_risk_on",
        )
    # stash Telegram mock on the ledger mock for caller assertions
    mock_ledger._mock_tg = mock_tg
    return mock_ledger


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestSameBarRoundTripIdempotency:
    """Idempotency precheck: SQLite-backed dedup in _record_same_bar_round_trip."""

    def test_skips_when_close_already_recorded_today(self):
        """Existing non-superseded closed trade today => all side-effects skipped.

        Scenario: MCHP buy=100.00, sell=99.11, qty=3 => pnl=-2.67.
        A matching closed row already in SQLite for today causes the function
        to return early without calling record_entry, record_exit, or Telegram.
        """
        ticker = "MCHP"
        buy_price = 100.0
        sell_price = 99.11   # (99.11 - 100.0) * 3 = -2.67
        expected_pnl = round((sell_price - buy_price) * 3, 2)  # -2.67

        _insert_closed_trade(ticker, expected_pnl, superseded=0)

        ex = _make_executor()
        ledger = _call_sbrt(ex, ticker, buy_price, sell_price, qty=3)

        assert not ledger.record_entry.called, (
            "record_entry must NOT be called when a closed trade already exists today"
        )
        assert not ledger.record_exit.called, (
            "record_exit must NOT be called when a closed trade already exists today"
        )
        assert not ledger._mock_tg.called, (
            "Telegram send_message must NOT be called when a closed trade already exists today"
        )

    def test_records_when_no_existing_close(self):
        """Empty trades table => function proceeds and records normally.

        No existing row in SQLite => precheck finds nothing => falls through
        => record_entry and Telegram are both called.
        """
        ticker = "FSLR"
        buy_price = 200.0
        sell_price = 196.50   # pnl = -10.50

        ex = _make_executor()
        ledger = _call_sbrt(ex, ticker, buy_price, sell_price, qty=3)

        assert ledger.record_entry.call_count == 1, (
            "record_entry must be called once when no existing close exists today"
        )
        assert ledger._mock_tg.call_count == 1, (
            "Telegram send_message must be called once when no existing close exists today"
        )

    def test_falls_through_on_db_error(self, monkeypatch):
        """DB exception in precheck => fail-open: record_entry still called.

        If atlas_db.get_db raises (DB locked, schema missing, etc.), the
        function must fall through and record rather than silently dropping
        a potentially legitimate alert.
        """
        ticker = "EBAY"
        buy_price = 55.0
        sell_price = 53.50   # pnl = -4.50

        import db.atlas_db as real_adb

        @contextmanager
        def _failing_get_db(*args, **kwargs):
            raise RuntimeError("simulated DB lock for test")
            yield  # unreachable — keeps it a generator so @contextmanager works

        monkeypatch.setattr(real_adb, "get_db", _failing_get_db)

        ex = _make_executor()
        ledger = _call_sbrt(ex, ticker, buy_price, sell_price, qty=3)

        assert ledger.record_entry.call_count == 1, (
            "record_entry must still be called (fail-open) when DB raises an exception"
        )
        assert ledger._mock_tg.call_count == 1, (
            "Telegram must still fire (fail-open) when DB raises an exception"
        )

    def test_does_not_match_superseded_rows(self):
        """superseded=1 closed row must NOT block a fresh recording.

        Superseded duplicates have superseded=1. The precheck query filters
        `superseded = 0`, so a superseded row must not prevent new alerts.
        """
        ticker = "MCHP_SUP"
        buy_price = 100.0
        sell_price = 99.11   # pnl = -2.67
        expected_pnl = round((sell_price - buy_price) * 3, 2)

        # Pre-insert a SUPERSEDED row
        _insert_closed_trade(ticker, expected_pnl, superseded=1)

        ex = _make_executor()
        ledger = _call_sbrt(ex, ticker, buy_price, sell_price, qty=3)

        assert ledger.record_entry.call_count == 1, (
            "record_entry must be called — superseded rows must not block new records"
        )
        assert ledger._mock_tg.call_count == 1, (
            "Telegram must fire — superseded rows must not block new alerts"
        )

    def test_does_not_match_yesterday(self):
        """Yesterday's closed row must NOT block today's recording.

        The precheck uses `DATE(exit_date) = DATE('now')`, so a closed trade
        from the prior session day must not suppress a fresh event today.
        """
        ticker = "MCHP_YEST"
        buy_price = 100.0
        sell_price = 99.11   # pnl = -2.67
        expected_pnl = round((sell_price - buy_price) * 3, 2)
        yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")

        # Pre-insert a row for YESTERDAY
        _insert_closed_trade(ticker, expected_pnl, exit_date=yesterday, superseded=0)

        ex = _make_executor()
        ledger = _call_sbrt(ex, ticker, buy_price, sell_price, qty=3)

        assert ledger.record_entry.call_count == 1, (
            "record_entry must be called — yesterday's rows must not block today's records"
        )
        assert ledger._mock_tg.call_count == 1, (
            "Telegram must fire — yesterday's rows must not block today's alerts"
        )
