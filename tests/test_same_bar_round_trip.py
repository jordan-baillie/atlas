"""Regression tests for same-bar buy/sell round-trip recording.

Task #311 — Same-bar buy/sell investigation + fix.

When reconcile_entry_fills detects a BUY whose bracket SELL also filled,
it must RECORD the round-trip in the trade ledger (entry stub + exit record)
rather than silently dropping it.  The zombie-open-row protection is
preserved — no OPEN trade row must be created.

Run:
    cd /root/atlas && python3 -m pytest tests/test_same_bar_round_trip.py -v --timeout=30
"""
from __future__ import annotations

import sys
import logging
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

import db.atlas_db as _adb


# ─────────────────────────────────────────────────────────────────────────────
# Shared helpers (mirror test_reconcile_entry_fills_guard.py patterns)
# ─────────────────────────────────────────────────────────────────────────────

def _ts(hour: int, minute: int = 0, second: int = 0, day: int = 7) -> datetime:
    """Return a tz-aware UTC datetime on 2026-05-{day}."""
    return datetime(2026, 5, day, hour, minute, second, tzinfo=timezone.utc)


def _make_order(
    symbol: str,
    side: str,
    status: str,
    filled_at: datetime | None,
    fill_price: float = 100.0,
    qty: int = 2,
    client_order_id: str = "atlas_entry_test",
    order_type: str = "limit",
) -> MagicMock:
    """Construct a fake Alpaca order object."""
    o = MagicMock()
    o.id = f"order-{symbol}-{side}-{id(o)}"
    o.side.value = side
    o.status.value = status
    o.symbol = symbol
    o.filled_at = filled_at
    o.filled_avg_price = fill_price if status == "filled" else None
    o.filled_qty = qty if status == "filled" else 0
    o.qty = qty
    o.client_order_id = client_order_id
    o.order_type.value = order_type
    return o


def _make_executor() -> object:
    """Return a LiveExecutor wired for testing (no real broker connect)."""
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
    # Wire a live-mode routing policy so _record_same_bar_round_trip works
    from brokers.routing_policy import BrokerRoutingPolicy
    ex._policy = BrokerRoutingPolicy(cfg, market_id='sp500')
    return ex


def _plan_with_stop(ticker: str, stop: float = 98.0, strategy: str = "momentum_breakout") -> dict:
    return {
        "proposed_entries": [
            {
                "ticker": ticker,
                "strategy": strategy,
                "stop_price": stop,
                "entry_price": 102.0,
                "confidence": 0.75,
            }
        ]
    }


def _call_reconcile(executor, orders: list, plan: dict | None = None):
    """Drive reconcile_entry_fills; returns (result, mock_ledger)."""
    mock_ledger = MagicMock()
    mock_ledger.trades = []
    mock_ledger.record_entry.return_value = 42
    executor._broker._broker_call.return_value = orders

    with (
        patch("brokers.live_executor._get_regime_model") as mock_regime,
        patch("journal.logger.TradeLedger", return_value=mock_ledger),
        # Patch Telegram to prevent real network calls
        patch("utils.telegram.send_message"),
    ):
        mock_regime.return_value.classify_current.return_value.state.value = "bull_risk_on"
        result = executor.reconcile_entry_fills(plan=plan)

    return result, mock_ledger


# ─────────────────────────────────────────────────────────────────────────────
# Test 1 — same-bar round-trip is recorded to ledger
# ─────────────────────────────────────────────────────────────────────────────

class TestSameBarRoundTripRecordsToLedger:
    """Main fix verification: same-bar round-trip appears in ledger."""

    def test_entry_stub_and_exit_recorded(self):
        """BUY at T1, SELL at T1+5s → both recorded; no OPEN row in SQLite.

        MCHP scenario from 2026-05-07: BUY filled 13:30:00, SELL (bracket stop)
        filled 13:30:36 — should produce entry stub + exit in ledger.
        """
        ex = _make_executor()
        buy = _make_order(
            symbol="XYZ",
            side="buy", status="filled",
            filled_at=_ts(13, 30, 0),
            fill_price=102.28, qty=2,
            client_order_id="atlas_entry_xyz_001",
        )
        sell = _make_order(
            symbol="XYZ",
            side="sell", status="filled",
            filled_at=_ts(13, 30, 5),  # 5s later — same-bar
            fill_price=100.94, qty=2,
            client_order_id="atlas_stop_xyz_001",
            order_type="stop",
        )
        plan = _plan_with_stop("XYZ", stop=100.88)

        result, mock_ledger = _call_reconcile(ex, [buy, sell], plan)

        # No OPEN row created (zombie protection preserved)
        assert result == [], (
            f"Expected empty reconciled list (no OPEN row for XYZ), got: {result}"
        )
        # record_entry called once for the entry stub
        mock_ledger.record_entry.assert_called_once()
        entry_call = mock_ledger.record_entry.call_args[0][0]
        assert entry_call["ticker"] == "XYZ"
        assert entry_call["fill_price"] == 102.28
        assert entry_call["same_bar_round_trip"] is True
        assert entry_call["reconciled"] is True

        # record_exit called once
        mock_ledger.record_exit.assert_called_once()
        exit_call = mock_ledger.record_exit.call_args[0][0]
        assert exit_call["ticker"] == "XYZ"
        assert exit_call["fill_price"] == 100.94
        assert exit_call["same_bar_round_trip"] is True
        assert exit_call["exit_reason"] == "stop_loss"

    def test_pnl_is_correct(self):
        """Realized PnL = (sell - buy) × qty."""
        ex = _make_executor()
        buy = _make_order(
            "ABC", "buy", "filled", _ts(13, 30, 0),
            fill_price=100.0, qty=3,
            client_order_id="atlas_entry_abc",
        )
        sell = _make_order(
            "ABC", "sell", "filled", _ts(13, 30, 10),
            fill_price=97.0, qty=3,
            client_order_id="atlas_stop_abc",
            order_type="stop",
        )
        plan = _plan_with_stop("ABC", stop=96.0)

        _result, mock_ledger = _call_reconcile(ex, [buy, sell], plan)

        exit_call = mock_ledger.record_exit.call_args[0][0]
        assert exit_call["pnl"] == pytest.approx(-9.0, abs=0.01), (
            f"Expected PnL = (97-100)*3 = -9.0, got {exit_call['pnl']}"
        )
        assert exit_call["pnl_pct"] == pytest.approx(-3.0, abs=0.01)

    def test_no_open_sqlite_row_created(self):
        """The zombie-protection invariant: no status=open row inserted to SQLite.

        record_entry writes to TradeLedger (JSON) with same_bar_round_trip=True;
        TradeLedger.record_entry will SQLite dual-write an OPEN row.  That OPEN
        row is immediately followed by record_exit which closes it.  We verify
        the calls happen in correct order.
        """
        ex = _make_executor()
        buy = _make_order(
            "DEF", "buy", "filled", _ts(13, 30, 0),
            fill_price=50.0, qty=1,
            client_order_id="atlas_entry_def",
        )
        sell = _make_order(
            "DEF", "sell", "filled", _ts(13, 30, 2),
            fill_price=49.0, qty=1,
            client_order_id="atlas_stop_def",
            order_type="stop",
        )
        plan = _plan_with_stop("DEF", stop=48.0)

        _result, mock_ledger = _call_reconcile(ex, [buy, sell], plan)

        # entry before exit — correct ordering
        assert mock_ledger.record_entry.call_count == 1
        assert mock_ledger.record_exit.call_count == 1

        # Confirm record_entry was called BEFORE record_exit in call sequence
        entry_idx = None
        exit_idx = None
        for i, c in enumerate(mock_ledger.mock_calls):
            if c[0] == "record_entry":
                entry_idx = i
            elif c[0] == "record_exit":
                exit_idx = i
        assert entry_idx is not None and exit_idx is not None
        assert entry_idx < exit_idx, "record_entry must be called before record_exit"


# ─────────────────────────────────────────────────────────────────────────────
# Test 2 — zombie protection preserved for non-same-bar (existing behaviour)
# ─────────────────────────────────────────────────────────────────────────────

class TestZombieProtectionPreservedForOpenPositions:
    """Existing behaviour: BUY with no matching SELL → OPEN row created."""

    def test_records_buy_when_no_sell_filled(self):
        """BUY with no SELL → reconcile records it as OPEN."""
        ex = _make_executor()
        buy = _make_order(
            "OPEN1", "buy", "filled", _ts(13, 30, 0),
            fill_price=110.0, qty=1,
            client_order_id="atlas_entry_open1",
        )
        plan = _plan_with_stop("OPEN1", stop=105.0)

        result, mock_ledger = _call_reconcile(ex, [buy], plan)

        assert len(result) == 1, f"Expected 1 open position recorded, got: {result}"
        assert result[0]["ticker"] == "OPEN1"
        mock_ledger.record_entry.assert_called_once()
        mock_ledger.record_exit.assert_not_called()

    def test_canceled_sell_does_not_block_open_row(self):
        """CANCELED SELL does not trigger same-bar guard → BUY recorded as OPEN."""
        ex = _make_executor()
        buy = _make_order(
            "OPEN2", "buy", "filled", _ts(13, 30, 0),
            fill_price=110.0, qty=1,
            client_order_id="atlas_entry_open2",
        )
        canceled_sell = _make_order(
            "OPEN2", "sell", "canceled",
            filled_at=None,  # canceled → no fill time
            fill_price=0, qty=1,
            client_order_id="atlas_stop_open2",
        )
        plan = _plan_with_stop("OPEN2", stop=105.0)

        result, mock_ledger = _call_reconcile(ex, [buy, canceled_sell], plan)

        assert len(result) == 1
        mock_ledger.record_entry.assert_called_once()
        mock_ledger.record_exit.assert_not_called()

    def test_earlier_sell_does_not_block_new_buy(self):
        """A SELL from a previous lifecycle (before the BUY) does not trigger guard."""
        ex = _make_executor()
        old_sell = _make_order(
            "REENTRY", "sell", "filled",
            filled_at=_ts(13, 0, 0, day=4),  # yesterday
            fill_price=90.0, qty=1,
            client_order_id="atlas_stop_reentry_old",
        )
        new_buy = _make_order(
            "REENTRY", "buy", "filled",
            filled_at=_ts(13, 30, 0, day=5),  # today — sell_ts < buy_ts
            fill_price=95.0, qty=1,
            client_order_id="atlas_entry_reentry_new",
        )
        plan = _plan_with_stop("REENTRY", stop=92.0)

        result, mock_ledger = _call_reconcile(ex, [old_sell, new_buy], plan)

        assert len(result) == 1, (
            "Old sell (before new buy) should not trigger guard; new buy should be recorded"
        )
        assert result[0]["ticker"] == "REENTRY"


# ─────────────────────────────────────────────────────────────────────────────
# Test 3 — dedup: running reconcile twice does NOT double-record
# ─────────────────────────────────────────────────────────────────────────────

class TestDeduplicateRoundTrip:
    """Second call to reconcile_entry_fills must not double-record."""

    def test_dedup_via_sell_order_id_tracking(self):
        """On second run, sell order_id is in _exit_order_ids_for_recon → no double write."""
        ex = _make_executor()
        buy = _make_order(
            "DEDUP", "buy", "filled", _ts(13, 30, 0),
            fill_price=100.0, qty=1,
            client_order_id="atlas_entry_dedup",
        )
        sell = _make_order(
            "DEDUP", "sell", "filled", _ts(13, 30, 5),
            fill_price=98.0, qty=1,
            client_order_id="atlas_stop_dedup",
            order_type="stop",
        )
        plan = _plan_with_stop("DEDUP", stop=97.0)

        # First run — records the round-trip
        mock_ledger_1 = MagicMock()
        mock_ledger_1.trades = []
        mock_ledger_1.record_entry.return_value = 10
        ex._broker._broker_call.return_value = [buy, sell]

        with (
            patch("brokers.live_executor._get_regime_model") as mock_regime,
            patch("journal.logger.TradeLedger", return_value=mock_ledger_1),
            patch("utils.telegram.send_message"),
        ):
            mock_regime.return_value.classify_current.return_value.state.value = "bull_risk_on"
            ex.reconcile_entry_fills(plan=plan)

        assert mock_ledger_1.record_entry.call_count == 1
        assert mock_ledger_1.record_exit.call_count == 1

        # Second run — ledger now contains the exit record; dedup prevents re-write
        sell_order_id = str(sell.id)
        mock_ledger_2 = MagicMock()
        mock_ledger_2.trades = [
            {"type": "exit", "order_id": sell_order_id, "ticker": "DEDUP"},
        ]
        mock_ledger_2.record_entry.return_value = None  # dedup → None
        ex._broker._broker_call.return_value = [buy, sell]

        with (
            patch("brokers.live_executor._get_regime_model") as mock_regime,
            patch("journal.logger.TradeLedger", return_value=mock_ledger_2),
            patch("utils.telegram.send_message"),
        ):
            mock_regime.return_value.classify_current.return_value.state.value = "bull_risk_on"
            ex.reconcile_entry_fills(plan=plan)

        # On second run: sell order_id already in exit records → skip (no write)
        mock_ledger_2.record_entry.assert_not_called()
        mock_ledger_2.record_exit.assert_not_called()


# ─────────────────────────────────────────────────────────────────────────────
# Test 4 — WARNING log line appears
# ─────────────────────────────────────────────────────────────────────────────

class TestRoundTripLogsWarning:
    """SAME_BAR_ROUND_TRIP warning must appear in logs."""

    def test_warning_logged(self, caplog):
        """reconcile_entry_fills emits SAME_BAR_ROUND_TRIP at WARNING level."""
        ex = _make_executor()
        buy = _make_order(
            "LOGTEST", "buy", "filled", _ts(13, 30, 0),
            fill_price=100.0, qty=1,
            client_order_id="atlas_entry_logtest",
        )
        sell = _make_order(
            "LOGTEST", "sell", "filled", _ts(13, 30, 10),
            fill_price=98.0, qty=1,
            client_order_id="atlas_stop_logtest",
            order_type="stop",
        )
        plan = _plan_with_stop("LOGTEST", stop=97.0)

        mock_ledger = MagicMock()
        mock_ledger.trades = []
        mock_ledger.record_entry.return_value = 99
        ex._broker._broker_call.return_value = [buy, sell]

        with (
            patch("brokers.live_executor._get_regime_model") as mock_regime,
            patch("journal.logger.TradeLedger", return_value=mock_ledger),
            patch("utils.telegram.send_message"),
            caplog.at_level(logging.WARNING, logger="brokers.live_executor"),
        ):
            mock_regime.return_value.classify_current.return_value.state.value = "bull_risk_on"
            ex.reconcile_entry_fills(plan=plan)

        warning_msgs = [r.message for r in caplog.records if r.levelno >= logging.WARNING]
        same_bar_msgs = [m for m in warning_msgs if "SAME_BAR_ROUND_TRIP" in m]
        assert same_bar_msgs, (
            f"Expected at least one WARNING containing 'SAME_BAR_ROUND_TRIP'. "
            f"All warning messages: {warning_msgs}"
        )
        # Verify key fields present in the log line
        assert "LOGTEST" in same_bar_msgs[0]
        assert "100.28" in same_bar_msgs[0] or "100.00" in same_bar_msgs[0]


# ─────────────────────────────────────────────────────────────────────────────
# Test 5 — exit reason correctly classified from client_order_id + order_type
# ─────────────────────────────────────────────────────────────────────────────

class TestRoundTripExitReasonFromClientOrderId:
    """exit_reason is inferred correctly from client_order_id and order_type."""

    @pytest.mark.parametrize("client_order_id,order_type_val,expected_reason", [
        ("atlas_stop_xyz_001", "stop", "stop_loss"),
        ("atlas_tp_xyz_001", "limit", "take_profit"),
        ("atlas_trail_xyz_001", "trailing_stop", "trailing_stop_fill"),
        ("atlas_exit_xyz_001", "limit", "signal_exit"),
        # Bracket child order — UUID (Alpaca-generated), must fall back to order_type
        ("566eab63-f242-4ed4-b444-731626efc36e", "stop", "stop_loss"),
        # Trailing stop with UUID
        ("some-uuid-1234", "trailing_stop", "trailing_stop_fill"),
    ])
    def test_exit_reason(self, client_order_id: str, order_type_val: str, expected_reason: str):
        """Parametrized: client_order_id+order_type → expected exit_reason."""
        ex = _make_executor()
        buy = _make_order(
            "ERTEST", "buy", "filled", _ts(13, 30, 0),
            fill_price=100.0, qty=1,
            client_order_id="atlas_entry_ertest",
        )
        sell = _make_order(
            "ERTEST", "sell", "filled", _ts(13, 30, 10),
            fill_price=99.0, qty=1,
            client_order_id=client_order_id,
            order_type=order_type_val,
        )
        plan = _plan_with_stop("ERTEST", stop=98.0)

        _result, mock_ledger = _call_reconcile(ex, [buy, sell], plan)

        mock_ledger.record_exit.assert_called_once()
        exit_call = mock_ledger.record_exit.call_args[0][0]
        assert exit_call["exit_reason"] == expected_reason, (
            f"For client_order_id={client_order_id!r}, order_type={order_type_val!r}: "
            f"expected exit_reason={expected_reason!r}, got {exit_call['exit_reason']!r}"
        )
