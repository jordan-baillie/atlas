"""Tests for the bracket-exit guard in reconcile_entry_fills.

Guard location: brokers/live_executor.py :: reconcile_entry_fills()
Root-cause scenario: EBAY id=206 zombie (2026-05-06) — BUY filled 13:30 UTC,
bracket STOP SELL filled 13:30:37 UTC; both appear in the 7-day CLOSED scan;
without the guard, the BUY creates a zombie 'open' trade row.

Cases:
  1. test_skips_buy_when_sell_also_filled
     FILLED BUY at T1, FILLED SELL at T2 > T1 → skip BUY (no DB row, empty result).
  2. test_records_buy_when_no_sell_filled
     FILLED BUY, NO sell order → record BUY (record_entry called once).
  3. test_records_buy_when_sell_filled_BEFORE_buy
     FILLED SELL at T0 (yesterday), FILLED BUY at T1 > T0 → record BUY
     (sell belongs to a PREVIOUS lifecycle, not this one).
  4. test_records_buy_when_sell_canceled_not_filled
     FILLED BUY + CANCELED (not FILLED) SELL → record BUY normally.

Run:
    cd /root/atlas && python3 -m pytest tests/test_reconcile_entry_fills_guard.py -v --timeout=30
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

import db.atlas_db as _adb


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _ts(hour: int, minute: int = 0, second: int = 0, day: int = 5) -> datetime:
    """Return a tz-aware UTC datetime on 2026-05-{day}."""
    return datetime(2026, 5, day, hour, minute, second, tzinfo=timezone.utc)


def _make_order(
    symbol: str,
    side: str,          # "buy" | "sell"
    status: str,        # "filled" | "canceled" | "expired"
    filled_at: datetime | None,
    fill_price: float = 100.0,
    qty: int = 1,
    client_order_id: str = "atlas_entry_test",
) -> MagicMock:
    """Construct a fake Alpaca order object that reconcile_entry_fills can consume."""
    o = MagicMock()
    o.id = f"order-{symbol}-{side}-{status}"
    # Alpaca SDK enums: .value gives the lowercase string
    o.side.value = side
    o.status.value = status
    o.symbol = symbol
    o.filled_at = filled_at
    o.filled_avg_price = fill_price if status == "filled" else None
    o.filled_qty = qty if status == "filled" else 0
    o.qty = qty
    o.client_order_id = client_order_id
    return o


def _make_executor() -> object:
    """Return a LiveExecutor instance wired for testing (no broker connect)."""
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
    from brokers.routing_policy import BrokerRoutingPolicy
    ex._policy = BrokerRoutingPolicy(cfg, market_id="sp500")
    return ex


def _plan_with_stop(ticker: str, stop: float = 105.0, strategy: str = "momentum_breakout") -> dict:
    """Minimal plan that satisfies the stop_price>0 guard inside the loop."""
    return {
        "proposed_entries": [
            {
                "ticker": ticker,
                "strategy": strategy,
                "stop_price": stop,
                "entry_price": 107.5,
                "confidence": 0.7,
            }
        ]
    }


def _call_reconcile(
    executor,
    orders: list,
    plan: dict | None = None,
) -> tuple[list, MagicMock]:
    """Drive reconcile_entry_fills with mock orders and patched dependencies.

    Returns (result, mock_ledger) so tests can inspect both return value and
    whether TradeLedger.record_entry was called.
    """
    mock_ledger = MagicMock()
    mock_ledger.trades = []  # no pre-existing order_ids → nothing skipped as duplicate
    mock_ledger.record_entry.return_value = 999  # fake trade_id

    # Mock broker call: return our fake order list
    executor._broker._broker_call.return_value = orders

    with (
        patch("brokers.live_executor._get_regime_model") as mock_regime,
        patch("journal.logger.TradeLedger", return_value=mock_ledger),
        patch("utils.telegram.send_message"),
    ):
        # _get_regime_model().classify_current().state.value
        mock_regime.return_value.classify_current.return_value.state.value = "bull_risk_on"

        result = executor.reconcile_entry_fills(plan=plan)

    return result, mock_ledger


# ─────────────────────────────────────────────────────────────────────────────
# Tests
# ─────────────────────────────────────────────────────────────────────────────

class TestReconcileEntryFillsGuard:
    """Bracket-exit guard in reconcile_entry_fills — 4 cases."""

    # ── Case 1 ────────────────────────────────────────────────────────────────

    def test_skips_buy_when_sell_also_filled(self):
        """FILLED BUY + FILLED SELL (sell after buy) → round-trip recorded; no OPEN row.

        Fix #311: the guard now records the round-trip in the ledger (entry stub
        + exit) instead of silently dropping it.  Zombie protection is preserved:
        no OPEN trade row is created (result == []).

        Mirrors the EBAY id=206 zombie scenario:
          BUY  filled 2026-05-05 13:30:00 UTC
          SELL filled 2026-05-05 13:30:37 UTC  (bracket stop fill)
        Expected: result == [] (no open row), but record_entry AND record_exit called.
        """
        ex = _make_executor()
        buy_order = _make_order(
            symbol="EBAY",
            side="buy",
            status="filled",
            filled_at=_ts(13, 30, 0),
            fill_price=107.5,
            qty=1,
            client_order_id="atlas_entry_ebay_test",
        )
        sell_order = _make_order(
            symbol="EBAY",
            side="sell",
            status="filled",
            filled_at=_ts(13, 30, 37),  # AFTER the buy → round-trip recorded
            fill_price=107.0969,
            qty=1,
            client_order_id="atlas_stop_ebay_test",
        )
        plan = _plan_with_stop("EBAY", stop=105.0)

        result, mock_ledger = _call_reconcile(ex, [buy_order, sell_order], plan)

        # No OPEN row created (zombie protection preserved)
        assert result == [], (
            f"Expected empty result (no OPEN row for EBAY), got: {result}"
        )
        # Fix #311: round-trip IS now recorded in ledger (entry stub + exit)
        mock_ledger.record_entry.assert_called_once()
        entry_call = mock_ledger.record_entry.call_args[0][0]
        assert entry_call["ticker"] == "EBAY"
        assert entry_call.get("same_bar_round_trip") is True
        mock_ledger.record_exit.assert_called_once()
        exit_call = mock_ledger.record_exit.call_args[0][0]
        assert exit_call["exit_reason"] == "stop_loss"

    # ── Case 2 ────────────────────────────────────────────────────────────────

    def test_records_buy_when_no_sell_filled(self):
        """FILLED BUY with no corresponding SELL → BUY is recorded normally.

        Mirrors SYK entering a position and staying open.
        Expected: reconcile_entry_fills returns 1 record AND record_entry called once.
        """
        ex = _make_executor()
        buy_order = _make_order(
            symbol="SYK",
            side="buy",
            status="filled",
            filled_at=_ts(13, 30, 0),
            fill_price=294.65,
            qty=1,
            client_order_id="atlas_entry_syk_test",
        )
        plan = _plan_with_stop("SYK", stop=285.0, strategy="momentum_breakout")

        result, mock_ledger = _call_reconcile(ex, [buy_order], plan)

        assert len(result) == 1, f"Expected 1 reconciled fill for SYK, got: {result}"
        assert result[0]["ticker"] == "SYK"
        # record_entry must have been called exactly once — actual ledger write happened
        mock_ledger.record_entry.assert_called_once()
        # Verify the call was for SYK
        call_args = mock_ledger.record_entry.call_args[0][0]
        assert call_args["ticker"] == "SYK", f"record_entry called with wrong ticker: {call_args}"

    # ── Case 3 ────────────────────────────────────────────────────────────────

    def test_records_buy_when_sell_filled_before_buy(self):
        """SELL from a PREVIOUS lifecycle (T0 < T1) does NOT block a new BUY at T1.

        Scenario: yesterday's stop exit for XYZ at 13:00 day-4, then a new BUY
        at 13:30 day-5. The guard must not fire because sell_filled_at < buy_filled_at.
        Expected: reconcile_entry_fills returns 1 record AND record_entry called once.
        """
        ex = _make_executor()
        old_sell = _make_order(
            symbol="XYZ",
            side="sell",
            status="filled",
            filled_at=_ts(13, 0, 0, day=4),   # DAY 4 — previous lifecycle exit
            fill_price=50.0,
            qty=1,
            client_order_id="atlas_stop_xyz_old",
        )
        new_buy = _make_order(
            symbol="XYZ",
            side="buy",
            status="filled",
            filled_at=_ts(13, 30, 0, day=5),   # DAY 5 — new entry; sell_filled_at < buy
            fill_price=52.0,
            qty=1,
            client_order_id="atlas_entry_xyz_new",
        )
        plan = _plan_with_stop("XYZ", stop=49.0, strategy="momentum_breakout")

        result, mock_ledger = _call_reconcile(ex, [old_sell, new_buy], plan)

        assert len(result) == 1, (
            f"Expected 1 reconciled fill for XYZ new entry; sell was from prior "
            f"lifecycle (sell_filled_at < buy_filled_at should NOT trigger guard). "
            f"Got: {result}"
        )
        assert result[0]["ticker"] == "XYZ"
        mock_ledger.record_entry.assert_called_once()

    # ── Case 4 ────────────────────────────────────────────────────────────────

    def test_records_buy_when_sell_canceled_not_filled(self):
        """CANCELED (not FILLED) SELL does NOT trigger the guard.

        Scenario: AAPL BUY fills, bracket SELL gets canceled (maybe replaced
        with a tighter stop). The BUY should still be recorded.
        Expected: reconcile_entry_fills returns 1 record AND record_entry called once.
        """
        ex = _make_executor()
        buy_order = _make_order(
            symbol="AAPL",
            side="buy",
            status="filled",
            filled_at=_ts(14, 0, 0),
            fill_price=175.0,
            qty=1,
            client_order_id="atlas_entry_aapl_test",
        )
        canceled_sell = _make_order(
            symbol="AAPL",
            side="sell",
            status="canceled",             # ← CANCELED, not FILLED
            filled_at=None,                # canceled orders have no filled_at
            fill_price=0.0,
            qty=1,
            client_order_id="atlas_stop_aapl_old",
        )
        plan = _plan_with_stop("AAPL", stop=168.0, strategy="momentum_breakout")

        result, mock_ledger = _call_reconcile(ex, [buy_order, canceled_sell], plan)

        assert len(result) == 1, (
            f"Expected 1 reconciled fill for AAPL (canceled SELL should not block "
            f"BUY recording). Got: {result}"
        )
        assert result[0]["ticker"] == "AAPL"
        mock_ledger.record_entry.assert_called_once()
