"""Integration tests for the critical execution pipeline (Phase 2 — A1).

Tests the FULL flow through real code with a mocked broker — zero
real network calls.  Covers:

  1. test_order_creation_flow          — correct params reach the broker
  2. test_order_fill_handling          — filled order triggers ledger + stop
  3. test_position_tracking_consistency — entry+exit cycle, P&L correct
  4. test_daily_pnl_calculation        — P&L recorded accurately on exits
  5. test_circuit_breaker_integration  — loss threshold blocks new entries

Patterns follow tests/test_circuit_breaker.py:
  * _live_config() / _mock_broker() / _make_executor() helpers
  * @patch decorators for external dependencies
  * Never touches a real broker, Telegram, or filesystem journal

Run:
    cd /root/atlas && python3 -m pytest tests/test_execution_integration.py -v
"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch, call

import pytest

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

from brokers.live_executor import LiveExecutor, preflight_check_order
from brokers.base import (
    AccountInfo, OrderResult, OrderSide, OrderStatus, OrderType, PositionInfo,
)


# ═══════════════════════════════════════════════════════════════
# Shared helpers (mirror test_circuit_breaker.py conventions)
# ═══════════════════════════════════════════════════════════════

def _live_config(max_daily_loss_pct: float = 0.02) -> dict:
    """Return a minimal live-trading config."""
    return {
        "version": "test-integration-v1.0",
        "market": "sp500",
        "market_id": "sp500",
        "trading": {
            "mode": "live",
            "broker": "alpaca",
            "live_enabled": True,
            "live_safety": {
                "max_order_value": 10_000,
                "max_daily_orders": 20,
                "dry_run_first": False,
                "max_daily_loss_pct": max_daily_loss_pct,
            },
        },
        "risk": {
            "starting_equity": 10_000.0,
            "max_risk_per_trade_pct": 0.01,
            "max_open_positions": 10,
        },
        "data": {"source": "alpaca", "history_years": 3},
    }


def _mock_broker(equity: float = 10_000.0) -> MagicMock:
    """Return a mock broker that reports *equity* as portfolio value."""
    broker = MagicMock()
    broker.get_account_info.return_value = AccountInfo(
        equity=equity, cash=5_000.0
    )
    broker.get_positions.return_value = []
    broker.get_open_orders.return_value = []
    return broker


def _make_executor(config: dict) -> LiveExecutor:
    """Build a pre-connected LiveExecutor (skips real connect() call)."""
    ex = LiveExecutor(config)
    ex._connected = True
    ex._halted = False
    ex._daily_date = "2026-03-24"
    return ex


def _filled_order(ticker: str, qty: int, price: float) -> OrderResult:
    """Convenience: an order that's instantly FILLED at *price*."""
    return OrderResult(
        success=True,
        order_id=f"ORD-{ticker}-001",
        ticker=ticker,
        side=OrderSide.BUY,
        status=OrderStatus.FILLED,
        requested_qty=qty,
        filled_qty=qty,
        fill_price=price,
        raw={},
    )


def _submitted_order(ticker: str, qty: int) -> OrderResult:
    """Convenience: a LIMIT order accepted but not yet filled."""
    return OrderResult(
        success=True,
        order_id=f"ORD-{ticker}-002",
        ticker=ticker,
        side=OrderSide.BUY,
        status=OrderStatus.SUBMITTED,
        requested_qty=qty,
        filled_qty=0,
        fill_price=0.0,
        raw={},
    )


def _filled_exit(ticker: str, qty: int, price: float) -> OrderResult:
    """Convenience: a SELL order that's instantly FILLED at *price*."""
    return OrderResult(
        success=True,
        order_id=f"EXIT-{ticker}-001",
        ticker=ticker,
        side=OrderSide.SELL,
        status=OrderStatus.FILLED,
        requested_qty=qty,
        filled_qty=qty,
        fill_price=price,
        raw={},
    )


# Common patch targets
# filter_tradable is imported inside execute_plan via:
#   from brokers.alpaca.tradable_assets import filter_tradable
# We must patch the function at its SOURCE module so the local import picks it up.
_FILTER_TRADABLE   = "brokers.alpaca.tradable_assets.filter_tradable"
_JOURNAL_ENTRY     = "brokers.live_executor._journal_entry"
_TELEGRAM          = "utils.telegram.send_message"
_TRADE_LEDGER      = "journal.logger.TradeLedger"
_LIVE_PORTFOLIO    = "brokers.live_portfolio.LivePortfolio"
# journal.round_trip may not exist in all environments — always use create=True
_ROUND_TRIP        = "journal.round_trip.RoundTripStore"


def _patch_execution_dependencies(ex: LiveExecutor, tradable: list[str]):
    """Context-manager stack that patches all external deps for execute_plan.

    Returns a *list* of patchers; callers should use contextlib.ExitStack
    or chain with-blocks.  We expose it as a helper to avoid repetition.
    """
    # Not used as a context manager directly — callers chain patches manually.
    # Kept as a doc reference.
    pass


# ═══════════════════════════════════════════════════════════════
# 1. test_order_creation_flow
# ═══════════════════════════════════════════════════════════════

class TestOrderCreationFlow:
    """Full entry flow: verify the broker receives the correct order parameters."""

    def _run_plan(self, ex: LiveExecutor, plan: dict, broker: MagicMock):
        """Execute *plan* through *ex* with all external deps mocked."""
        tickers = [e["ticker"] for e in plan.get("proposed_entries", [])]
        with patch(_FILTER_TRADABLE, return_value=(tickers, [])):
            with patch.object(ex, "check_market_state",
                              return_value={"is_tradeable": True, "message": "ok"}):
                with patch.object(ex, "_run_volatility_gate",
                                  return_value={"action": "allow",
                                                "message": "ok",
                                                "gate_enabled": False}):
                    with patch.object(ex, "place_stops_for_plan", return_value={}):
                        with patch(_JOURNAL_ENTRY):
                            return ex.execute_plan(plan, "2026-03-24")

    def test_order_creation_flow(self):
        """Broker receives BUY orders with correct ticker, qty, price, side."""
        cfg = _live_config()
        ex = _make_executor(cfg)

        broker = _mock_broker()
        broker.place_order.side_effect = [
            _submitted_order("AAPL", 10),
            _submitted_order("MSFT", 5),
        ]
        ex._broker = broker
        ex._daily_start_equity = 10_000.0

        plan = {
            "status": "APPROVED",
            "proposed_entries": [
                {
                    "ticker": "AAPL",
                    "entry_price": 150.00,
                    "position_size": 10,
                    "strategy": "momentum_breakout",
                    "confidence": 0.75,
                    "stop_price": 145.00,
                },
                {
                    "ticker": "MSFT",
                    "entry_price": 300.00,
                    "position_size": 5,
                    "strategy": "trend_following",
                    "confidence": 0.80,
                    "stop_price": 290.00,
                },
            ],
            "proposed_exits": [],
        }

        report = self._run_plan(ex, plan, broker)

        # Two orders must have been placed
        assert broker.place_order.call_count == 2, (
            f"Expected 2 place_order calls, got {broker.place_order.call_count}"
        )

        # Verify first order: AAPL
        first_call = broker.place_order.call_args_list[0]
        kw1 = first_call.kwargs
        assert kw1["ticker"] == "AAPL"
        assert kw1["side"] == OrderSide.BUY
        assert kw1["qty"] == 10
        assert kw1["price"] == pytest.approx(150.00)
        assert kw1["order_type"] == OrderType.LIMIT

        # Verify second order: MSFT
        second_call = broker.place_order.call_args_list[1]
        kw2 = second_call.kwargs
        assert kw2["ticker"] == "MSFT"
        assert kw2["side"] == OrderSide.BUY
        assert kw2["qty"] == 5
        assert kw2["price"] == pytest.approx(300.00)
        assert kw2["order_type"] == OrderType.LIMIT

        # Report structure
        assert report["total_entries"] == 2
        assert report["successful_entries"] == 2
        assert report["dry_run"] is False

    def test_order_creation_rejects_non_approved_plan(self):
        """Plans that are not APPROVED status are rejected before any orders."""
        cfg = _live_config()
        ex = _make_executor(cfg)
        broker = _mock_broker()
        ex._broker = broker

        for bad_status in ("PENDING", "DRAFT", "REJECTED", ""):
            plan = {
                "status": bad_status,
                "proposed_entries": [
                    {"ticker": "AAPL", "entry_price": 150.0,
                     "position_size": 10, "strategy": "momentum_breakout",
                     "confidence": 0.75, "stop_price": 145.0}
                ],
                "proposed_exits": [],
            }
            with patch(_JOURNAL_ENTRY):
                report = ex.execute_plan(plan, "2026-03-24")

            # No orders placed
            broker.place_order.assert_not_called()
            # Report signals an error
            assert report.get("error") or report.get("errors"), (
                f"Expected 'error' or 'errors' key for status={bad_status!r}, got: {report}"
            )

    def test_order_creation_flow_remark_contains_strategy(self):
        """Order remark includes the strategy name (for Alpaca audit trail)."""
        cfg = _live_config()
        ex = _make_executor(cfg)
        broker = _mock_broker()
        broker.place_order.return_value = _submitted_order("TSLA", 3)
        ex._broker = broker
        ex._daily_start_equity = 10_000.0

        plan = {
            "status": "APPROVED",
            "proposed_entries": [
                {"ticker": "TSLA", "entry_price": 200.0,
                 "position_size": 3, "strategy": "opening_gap",
                 "confidence": 0.70, "stop_price": 192.0},
            ],
            "proposed_exits": [],
        }

        with patch(_FILTER_TRADABLE, return_value=(["TSLA"], [])):
            with patch.object(ex, "check_market_state",
                              return_value={"is_tradeable": True, "message": "ok"}):
                with patch.object(ex, "_run_volatility_gate",
                                  return_value={"action": "allow", "message": "ok",
                                                "gate_enabled": False}):
                    with patch.object(ex, "place_stops_for_plan", return_value={}):
                        with patch(_JOURNAL_ENTRY):
                            ex.execute_plan(plan, "2026-03-24")

        kw = broker.place_order.call_args.kwargs
        assert "opening_gap" in kw.get("remark", ""), (
            f"remark={kw.get('remark')!r} should contain strategy name"
        )

    def test_order_preflight_blocks_oversized_order(self):
        """Pre-flight check rejects an order whose value exceeds max_order_value."""
        cfg = _live_config()
        cfg["trading"]["live_safety"]["max_order_value"] = 500  # $500 limit
        ex = _make_executor(cfg)
        broker = _mock_broker()
        ex._broker = broker
        ex._daily_start_equity = 10_000.0

        # $200/share × 10 shares = $2000 — exceeds $500 limit
        plan = {
            "status": "APPROVED",
            "proposed_entries": [
                {"ticker": "GOOG", "entry_price": 200.0,
                 "position_size": 10, "strategy": "momentum_breakout",
                 "confidence": 0.75, "stop_price": 190.0},
            ],
            "proposed_exits": [],
        }

        with patch(_FILTER_TRADABLE, return_value=(["GOOG"], [])):
            with patch.object(ex, "check_market_state",
                              return_value={"is_tradeable": True, "message": "ok"}):
                with patch.object(ex, "_run_volatility_gate",
                                  return_value={"action": "allow", "message": "ok",
                                                "gate_enabled": False}):
                    with patch.object(ex, "place_stops_for_plan", return_value={}):
                        with patch(_JOURNAL_ENTRY):
                            report = ex.execute_plan(plan, "2026-03-24")

        # Order must be blocked — broker never called
        broker.place_order.assert_not_called()
        assert report["successful_entries"] == 0
        goog_result = report["entries"][0]
        assert goog_result["success"] is False
        assert any("max" in e.lower() or "value" in e.lower()
                   for e in goog_result.get("errors", []))


# ═══════════════════════════════════════════════════════════════
# 2. test_order_fill_handling
# ═══════════════════════════════════════════════════════════════

class TestOrderFillHandling:
    """Filled order → position tracked, stop placed, ledger updated."""

    def test_order_fill_handling(self):
        """Filled order: TradeLedger records entry with correct fields.

        Verifies:
        - TradeLedger.record_entry called
        - Correct ticker, shares, fill_price, stop_price, strategy recorded
        - place_stops_for_plan called after entries
        """
        cfg = _live_config()
        ex = _make_executor(cfg)
        broker = _mock_broker()

        # Order fills immediately
        broker.place_order.return_value = _filled_order("NVDA", 8, 450.00)
        ex._broker = broker
        ex._daily_start_equity = 10_000.0

        plan = {
            "status": "APPROVED",
            "proposed_entries": [
                {
                    "ticker": "NVDA",
                    "entry_price": 450.00,
                    "position_size": 8,
                    "strategy": "momentum_breakout",
                    "confidence": 0.82,
                    "stop_price": 432.00,
                },
            ],
            "proposed_exits": [],
        }

        ledger_entries = []

        class _MockLedger:
            def record_entry(self, rec):
                ledger_entries.append(rec)
            def record_exit(self, rec):
                pass

        with patch(_FILTER_TRADABLE, return_value=(["NVDA"], [])):
            with patch.object(ex, "check_market_state",
                              return_value={"is_tradeable": True, "message": "ok"}):
                with patch.object(ex, "_run_volatility_gate",
                                  return_value={"action": "allow", "message": "ok",
                                                "gate_enabled": False}):
                    with patch.object(ex, "place_stops_for_plan",
                                      return_value={"NVDA": "STOP-001"}) as mock_stops:
                        with patch(_JOURNAL_ENTRY):
                            with patch(_TRADE_LEDGER, return_value=_MockLedger()):
                                report = ex.execute_plan(plan, "2026-03-24")

        # Entry succeeded
        assert report["successful_entries"] == 1
        nvda_result = report["entries"][0]
        assert nvda_result["success"] is True
        assert nvda_result["status"] == "FILLED"
        assert nvda_result["fill_price"] == pytest.approx(450.00)

        # Ledger received the entry record
        assert len(ledger_entries) == 1, (
            f"Expected 1 ledger entry, got {len(ledger_entries)}"
        )
        rec = ledger_entries[0]
        assert rec["ticker"] == "NVDA"
        assert rec["shares"] == 8
        assert rec["fill_price"] == pytest.approx(450.00)
        assert rec["stop_price"] == pytest.approx(432.00)
        assert rec["strategy"] == "momentum_breakout"
        assert rec["order_id"] == "ORD-NVDA-001"

        # Protective stop placement was called
        mock_stops.assert_called_once()
        # plan_entries and entry_results passed through
        stop_args = mock_stops.call_args
        assert stop_args is not None

    def test_submitted_order_defers_ledger_entry(self):
        """SUBMITTED (not FILLED) orders do NOT record to TradeLedger immediately."""
        cfg = _live_config()
        ex = _make_executor(cfg)
        broker = _mock_broker()

        # Order accepted but NOT yet filled
        broker.place_order.return_value = _submitted_order("AMD", 15)
        # Polling: return same status (never fills during test)
        broker.get_order_status.return_value = _submitted_order("AMD", 15)
        ex._broker = broker
        ex._daily_start_equity = 10_000.0

        plan = {
            "status": "APPROVED",
            "proposed_entries": [
                {"ticker": "AMD", "entry_price": 120.0,
                 "position_size": 15, "strategy": "trend_following",
                 "confidence": 0.70, "stop_price": 114.0},
            ],
            "proposed_exits": [],
        }

        ledger_entries = []

        class _MockLedger:
            def record_entry(self, rec):
                ledger_entries.append(rec)

        with patch(_FILTER_TRADABLE, return_value=(["AMD"], [])):
            with patch.object(ex, "check_market_state",
                              return_value={"is_tradeable": True, "message": "ok"}):
                with patch.object(ex, "_run_volatility_gate",
                                  return_value={"action": "allow", "message": "ok",
                                                "gate_enabled": False}):
                    with patch.object(ex, "place_stops_for_plan", return_value={}):
                        with patch(_JOURNAL_ENTRY):
                            # Patch time.sleep to skip polling delay
                            with patch("time.sleep"):
                                with patch(_TRADE_LEDGER,
                                           return_value=_MockLedger()):
                                    report = ex.execute_plan(plan, "2026-03-24")

        # Order placed successfully but no fill
        assert report["entries"][0]["success"] is True
        assert report["entries"][0]["status"] == "SUBMITTED"

        # Ledger should NOT be called for unfilled orders
        assert len(ledger_entries) == 0, (
            "TradeLedger.record_entry should be deferred until fill confirmation"
        )


# ═══════════════════════════════════════════════════════════════
# 3. test_position_tracking_consistency
# ═══════════════════════════════════════════════════════════════

class TestPositionTrackingConsistency:
    """Entry+exit cycle: position count and P&L calculations are consistent."""

    def test_position_tracking_consistency(self):
        """Full entry→exit cycle.

        Verifies:
        - Entry order count increases after successful entry
        - Successful exits record closed trades
        - Closed trade P&L matches (exit_price - entry_price) × shares
        """
        cfg = _live_config()
        ex = _make_executor(cfg)
        broker = _mock_broker()

        # Entry: AAPL fills at $150
        broker.place_order.return_value = _filled_order("AAPL", 10, 150.0)
        ex._broker = broker
        ex._daily_start_equity = 10_000.0

        entry_plan = {
            "status": "APPROVED",
            "proposed_entries": [
                {"ticker": "AAPL", "entry_price": 150.0,
                 "position_size": 10, "strategy": "momentum_breakout",
                 "confidence": 0.75, "stop_price": 145.0},
            ],
            "proposed_exits": [],
        }

        ledger_entries = []
        ledger_exits = []
        closed_trades = []

        class _MockLedger:
            def record_entry(self, rec):
                ledger_entries.append(dict(rec))
            def record_exit(self, rec):
                ledger_exits.append(dict(rec))

        class _MockPortfolio:
            def __init__(self, *a, **kw): pass
            def load_state(self): pass
            def record_closed_trade(self, rec): closed_trades.append(dict(rec))

        with patch(_FILTER_TRADABLE, return_value=(["AAPL"], [])):
            with patch.object(ex, "check_market_state",
                              return_value={"is_tradeable": True, "message": "ok"}):
                with patch.object(ex, "_run_volatility_gate",
                                  return_value={"action": "allow", "message": "ok",
                                                "gate_enabled": False}):
                    with patch.object(ex, "place_stops_for_plan", return_value={}):
                        with patch(_JOURNAL_ENTRY):
                            with patch(_TRADE_LEDGER, return_value=_MockLedger()):
                                entry_report = ex.execute_plan(entry_plan, "2026-03-24")

        assert entry_report["successful_entries"] == 1
        assert ex._daily_order_count == 1

        # ── Now execute the exit ──────────────────────────────
        # Broker reports the position we just entered
        aapl_position = PositionInfo(
            ticker="AAPL",
            entry_price=150.0,
            shares=10,
            current_price=165.0,     # 10% gain
            market_value=1650.0,
            strategy="momentum_breakout",
            entry_date="2026-03-24",
        )
        broker.get_positions.return_value = [aapl_position]
        broker.get_open_orders.return_value = []
        # Exit fills at $165
        broker.place_order.return_value = _filled_exit("AAPL", 10, 165.0)

        exit_plan = {
            "status": "APPROVED",
            "proposed_entries": [],
            "proposed_exits": [
                {"ticker": "AAPL", "reason": "signal_exit", "direction": "long"},
            ],
        }

        with patch(_FILTER_TRADABLE, return_value=([], [])):
            with patch.object(ex, "check_market_state",
                              return_value={"is_tradeable": True, "message": "ok"}):
                with patch.object(ex, "_run_volatility_gate",
                                  return_value={"action": "allow", "message": "ok",
                                                "gate_enabled": False}):
                    with patch.object(ex, "place_stops_for_plan", return_value={}):
                        with patch(_JOURNAL_ENTRY):
                            with patch(_TRADE_LEDGER, return_value=_MockLedger()):
                                with patch(_LIVE_PORTFOLIO,
                                           return_value=_MockPortfolio()):
                                    # journal.round_trip may not exist; the executor
                                    # wraps that import in try/except so it's non-fatal.
                                    exit_report = ex.execute_plan(exit_plan, "2026-03-25")

        # Exit succeeded
        assert exit_report["successful_exits"] == 1
        assert exit_report["exits"][0]["success"] is True

        # Ledger recorded exit
        assert len(ledger_exits) == 1
        exit_rec = ledger_exits[0]
        assert exit_rec["ticker"] == "AAPL"
        assert exit_rec["shares"] == 10

        # P&L: (165 - 150) × 10 = $150
        expected_pnl = (165.0 - 150.0) * 10
        assert exit_rec["pnl"] == pytest.approx(expected_pnl), (
            f"Expected P&L {expected_pnl}, got {exit_rec['pnl']}"
        )

        # Closed trade recorded in portfolio
        assert len(closed_trades) == 1
        ct = closed_trades[0]
        assert ct["ticker"] == "AAPL"
        assert ct["pnl"] == pytest.approx(expected_pnl)

    def test_exit_no_position_handled_gracefully(self):
        """Exit for a ticker with no open position returns failure (non-crash)."""
        cfg = _live_config()
        ex = _make_executor(cfg)
        broker = _mock_broker()
        broker.get_positions.return_value = []   # No positions
        broker.get_open_orders.return_value = []
        ex._broker = broker
        ex._daily_start_equity = 10_000.0

        plan = {
            "status": "APPROVED",
            "proposed_entries": [],
            "proposed_exits": [
                {"ticker": "NFLX", "reason": "signal_exit", "direction": "long"},
            ],
        }

        with patch(_FILTER_TRADABLE, return_value=([], [])):
            with patch.object(ex, "check_market_state",
                              return_value={"is_tradeable": True, "message": "ok"}):
                with patch.object(ex, "_run_volatility_gate",
                                  return_value={"action": "allow", "message": "ok",
                                                "gate_enabled": False}):
                    with patch.object(ex, "place_stops_for_plan", return_value={}):
                        with patch(_JOURNAL_ENTRY):
                            report = ex.execute_plan(plan, "2026-03-24")

        # Exit recorded as failed — no broker order placed
        assert report["exits"][0]["success"] is False
        # "message" key should mention NFLX
        exit_msg = report["exits"][0].get("message", "")
        assert "NFLX" in exit_msg, f"Expected NFLX in exit message, got: {exit_msg!r}"
        broker.place_order.assert_not_called()

    def test_multiple_entries_order_count_increments(self):
        """Daily order counter increments for each successful order."""
        cfg = _live_config()
        ex = _make_executor(cfg)
        broker = _mock_broker()
        broker.place_order.side_effect = [
            _submitted_order("SPY", 5),
            _submitted_order("QQQ", 3),
            _submitted_order("IWM", 7),
        ]
        ex._broker = broker
        ex._daily_start_equity = 10_000.0
        assert ex._daily_order_count == 0

        plan = {
            "status": "APPROVED",
            "proposed_entries": [
                {"ticker": "SPY", "entry_price": 450.0, "position_size": 5,
                 "strategy": "momentum_breakout", "confidence": 0.7, "stop_price": 440.0},
                {"ticker": "QQQ", "entry_price": 350.0, "position_size": 3,
                 "strategy": "trend_following", "confidence": 0.7, "stop_price": 340.0},
                {"ticker": "IWM", "entry_price": 200.0, "position_size": 7,
                 "strategy": "mean_reversion", "confidence": 0.6, "stop_price": 193.0},
            ],
            "proposed_exits": [],
        }

        with patch(_FILTER_TRADABLE,
                   return_value=(["SPY", "QQQ", "IWM"], [])):
            with patch.object(ex, "check_market_state",
                              return_value={"is_tradeable": True, "message": "ok"}):
                with patch.object(ex, "_run_volatility_gate",
                                  return_value={"action": "allow", "message": "ok",
                                                "gate_enabled": False}):
                    with patch.object(ex, "place_stops_for_plan", return_value={}):
                        with patch(_JOURNAL_ENTRY):
                            report = ex.execute_plan(plan, "2026-03-24")

        assert report["successful_entries"] == 3
        assert ex._daily_order_count == 3


# ═══════════════════════════════════════════════════════════════
# 4. test_daily_pnl_calculation
# ═══════════════════════════════════════════════════════════════

class TestDailyPnLCalculation:
    """P&L is calculated correctly through the execution flow."""

    def test_daily_pnl_calculation(self):
        """P&L recorded in ledger matches (exit_price - entry_price) × shares.

        Three positions are closed with known prices:
          AAPL:  +$150  (10 shares: 150→165)
          MSFT:  -$100  (5 shares:  300→280)
          TSLA:  +$600  (3 shares:  200→400)
        Expected total P&L = $650.
        """
        cfg = _live_config()
        ex = _make_executor(cfg)
        broker = _mock_broker()
        broker.get_open_orders.return_value = []
        ex._broker = broker
        ex._daily_start_equity = 10_000.0

        positions = [
            PositionInfo(ticker="AAPL", entry_price=150.0, shares=10,
                         current_price=165.0, market_value=1650.0,
                         strategy="momentum_breakout", entry_date="2026-03-20"),
            PositionInfo(ticker="MSFT", entry_price=300.0, shares=5,
                         current_price=280.0, market_value=1400.0,
                         strategy="trend_following", entry_date="2026-03-21"),
            PositionInfo(ticker="TSLA", entry_price=200.0, shares=3,
                         current_price=400.0, market_value=1200.0,
                         strategy="opening_gap", entry_date="2026-03-22"),
        ]

        fill_prices = {
            "AAPL": 165.0,
            "MSFT": 280.0,
            "TSLA": 400.0,
        }

        expected_pnls = {
            "AAPL": (165.0 - 150.0) * 10,   # +150
            "MSFT": (280.0 - 300.0) * 5,    # -100
            "TSLA": (400.0 - 200.0) * 3,    # +600
        }
        expected_total = sum(expected_pnls.values())  # 650

        ledger_exits = []

        class _MockLedger:
            def record_exit(self, rec):
                ledger_exits.append(dict(rec))
            def record_entry(self, rec): pass

        class _MockPortfolio:
            def __init__(self, *a, **kw): pass
            def load_state(self): pass
            def record_closed_trade(self, rec): pass

        def _side_effect_positions():
            # Return different position for each call (simulate exits draining list)
            return positions

        def _place_order_side_effect(**kwargs):
            ticker = kwargs.get("ticker", "")
            fp = fill_prices.get(ticker, kwargs.get("price", 0.0))
            return OrderResult(
                success=True,
                order_id=f"EXIT-{ticker}",
                ticker=ticker,
                side=OrderSide.SELL,
                status=OrderStatus.FILLED,
                requested_qty=kwargs.get("qty", 0),
                filled_qty=kwargs.get("qty", 0),
                fill_price=fp,
                raw={},
            )

        broker.get_positions.side_effect = _side_effect_positions
        broker.place_order.side_effect = _place_order_side_effect

        plan = {
            "status": "APPROVED",
            "proposed_entries": [],
            "proposed_exits": [
                {"ticker": "AAPL", "reason": "signal_exit", "direction": "long"},
                {"ticker": "MSFT", "reason": "signal_exit", "direction": "long"},
                {"ticker": "TSLA", "reason": "signal_exit", "direction": "long"},
            ],
        }

        with patch(_FILTER_TRADABLE, return_value=([], [])):
            with patch.object(ex, "check_market_state",
                              return_value={"is_tradeable": True, "message": "ok"}):
                with patch.object(ex, "_run_volatility_gate",
                                  return_value={"action": "allow", "message": "ok",
                                                "gate_enabled": False}):
                    with patch.object(ex, "place_stops_for_plan", return_value={}):
                        with patch(_JOURNAL_ENTRY):
                            with patch(_TRADE_LEDGER, return_value=_MockLedger()):
                                with patch(_LIVE_PORTFOLIO,
                                           return_value=_MockPortfolio()):
                                    # journal.round_trip wrapped in try/except — non-fatal
                                    report = ex.execute_plan(plan, "2026-03-25")

        # All exits succeeded
        assert report["successful_exits"] == 3

        # P&L for each ticker
        assert len(ledger_exits) == 3
        actual_pnls = {rec["ticker"]: rec["pnl"] for rec in ledger_exits}

        for ticker, expected in expected_pnls.items():
            assert actual_pnls[ticker] == pytest.approx(expected, abs=0.01), (
                f"{ticker}: expected P&L {expected:.2f}, got {actual_pnls[ticker]:.2f}"
            )

        actual_total = sum(actual_pnls.values())
        assert actual_total == pytest.approx(expected_total, abs=0.01), (
            f"Total P&L: expected {expected_total:.2f}, got {actual_total:.2f}"
        )

    def test_pnl_percentage_recorded_correctly(self):
        """P&L percentage is recorded as (exit - entry) / entry × 100."""
        cfg = _live_config()
        ex = _make_executor(cfg)
        broker = _mock_broker()
        broker.get_open_orders.return_value = []

        pos = PositionInfo(ticker="META", entry_price=500.0, shares=4,
                           current_price=550.0, market_value=2200.0,
                           strategy="sector_rotation", entry_date="2026-03-20")
        broker.get_positions.return_value = [pos]
        broker.place_order.return_value = _filled_exit("META", 4, 550.0)
        ex._broker = broker
        ex._daily_start_equity = 10_000.0

        ledger_exits = []

        class _MockLedger:
            def record_exit(self, rec): ledger_exits.append(dict(rec))
            def record_entry(self, rec): pass

        class _MockPortfolio:
            def __init__(self, *a, **kw): pass
            def load_state(self): pass
            def record_closed_trade(self, rec): pass

        plan = {
            "status": "APPROVED",
            "proposed_entries": [],
            "proposed_exits": [
                {"ticker": "META", "reason": "signal_exit", "direction": "long"},
            ],
        }

        with patch(_FILTER_TRADABLE, return_value=([], [])):
            with patch.object(ex, "check_market_state",
                              return_value={"is_tradeable": True, "message": "ok"}):
                with patch.object(ex, "_run_volatility_gate",
                                  return_value={"action": "allow", "message": "ok",
                                                "gate_enabled": False}):
                    with patch.object(ex, "place_stops_for_plan", return_value={}):
                        with patch(_JOURNAL_ENTRY):
                            with patch(_TRADE_LEDGER, return_value=_MockLedger()):
                                with patch(_LIVE_PORTFOLIO,
                                           return_value=_MockPortfolio()):
                                    # journal.round_trip wrapped in try/except — non-fatal
                                    ex.execute_plan(plan, "2026-03-25")

        assert len(ledger_exits) == 1
        rec = ledger_exits[0]

        # P&L = (550 - 500) × 4 = $200
        assert rec["pnl"] == pytest.approx(200.0)

        # P&L% = (550 - 500) / 500 × 100 = 10.0%
        expected_pct = (550.0 - 500.0) / 500.0 * 100
        assert rec["pnl_pct"] == pytest.approx(expected_pct, abs=0.01)


# ═══════════════════════════════════════════════════════════════
# 5. test_circuit_breaker_integration
# ═══════════════════════════════════════════════════════════════

class TestCircuitBreakerIntegration:
    """Circuit breaker: loss threshold blocks new entries, Telegram alert fires."""

    def _plan_with_entries(self, n: int = 2) -> dict:
        return {
            "status": "APPROVED",
            "proposed_entries": [
                {
                    "ticker": f"TICK{i}",
                    "entry_price": 100.0,
                    "position_size": 10,
                    "strategy": "momentum_breakout",
                    "confidence": 0.75,
                    "stop_price": 95.0,
                }
                for i in range(n)
            ],
            "proposed_exits": [],
        }

    def test_circuit_breaker_integration(self):
        """Daily loss > 2% → all new entries are BLOCKED.

        Verifies:
        - circuit_breaker_tripped flag in report
        - Every entry result has success=False, blocked=True, reason='circuit_breaker'
        - Broker.place_order is never called for blocked entries
        - Telegram alert is sent
        """
        cfg = _live_config(max_daily_loss_pct=0.02)
        ex = _make_executor(cfg)
        ex._daily_start_equity = 10_000.0

        # Current equity shows 3% loss ($300) — exceeds 2% threshold ($200)
        broker = _mock_broker(equity=9_700.0)
        broker.get_positions.return_value = []
        ex._broker = broker

        plan = self._plan_with_entries(n=2)
        tickers = [e["ticker"] for e in plan["proposed_entries"]]

        telegram_calls = []

        with patch(_FILTER_TRADABLE, return_value=(tickers, [])):
            with patch.object(ex, "check_market_state",
                              return_value={"is_tradeable": True, "message": "ok"}):
                with patch.object(ex, "_run_volatility_gate",
                                  return_value={"action": "allow", "message": "ok",
                                                "gate_enabled": False}):
                    with patch.object(ex, "place_stops_for_plan", return_value={}):
                        with patch(_JOURNAL_ENTRY):
                            with patch(_TELEGRAM,
                                       side_effect=lambda msg: telegram_calls.append(msg)) as mock_tg:
                                report = ex.execute_plan(plan, "2026-03-24")

        # Circuit breaker must have tripped
        assert report.get("circuit_breaker_tripped") is True, (
            "Expected circuit_breaker_tripped=True in report"
        )

        # Every entry is blocked
        assert len(report["entries"]) == 2
        for entry_result in report["entries"]:
            assert entry_result["success"] is False, (
                f"Entry {entry_result['ticker']} should be blocked, got success=True"
            )
            assert entry_result.get("blocked") is True
            assert entry_result.get("reason") == "circuit_breaker"

        # No real orders were placed
        broker.place_order.assert_not_called()

        # Telegram was called (alert fired)
        assert len(telegram_calls) >= 1, "Expected Telegram alert to fire on circuit breaker trip"

    def test_circuit_breaker_resets_next_day(self):
        """Circuit breaker tripped today → resets on the next trading day."""
        cfg = _live_config(max_daily_loss_pct=0.02)
        ex = _make_executor(cfg)
        ex._daily_start_equity = 10_000.0
        ex._daily_date = "2026-03-24"
        ex._circuit_breaker_tripped = True   # Already tripped

        # Simulate a new day: reset should clear the breaker
        ex._reset_circuit_breaker_if_new_day("2026-03-25")

        assert ex._circuit_breaker_tripped is False, (
            "Circuit breaker should reset on a new trading day"
        )
        assert ex._daily_start_equity == pytest.approx(0.0), (
            "Daily start equity should reset to 0 on a new trading day"
        )

    def test_circuit_breaker_same_day_stays_tripped(self):
        """A tripped circuit breaker stays tripped on the same day."""
        cfg = _live_config(max_daily_loss_pct=0.02)
        ex = _make_executor(cfg)
        ex._daily_date = "2026-03-24"
        ex._circuit_breaker_tripped = True
        ex._daily_start_equity = 10_000.0

        ex._reset_circuit_breaker_if_new_day("2026-03-24")   # Same day

        assert ex._circuit_breaker_tripped is True
        assert ex._daily_start_equity == pytest.approx(10_000.0)

    def test_circuit_breaker_below_threshold_allows_entries(self):
        """Loss below threshold → entries proceed normally."""
        cfg = _live_config(max_daily_loss_pct=0.02)
        ex = _make_executor(cfg)
        ex._daily_start_equity = 10_000.0

        # Only 0.5% loss — well within 2% limit
        broker = _mock_broker(equity=9_950.0)
        broker.get_positions.return_value = []
        broker.place_order.return_value = _submitted_order("TICK0", 10)
        ex._broker = broker

        plan = self._plan_with_entries(n=1)
        tickers = [e["ticker"] for e in plan["proposed_entries"]]

        with patch(_FILTER_TRADABLE, return_value=(tickers, [])):
            with patch.object(ex, "check_market_state",
                              return_value={"is_tradeable": True, "message": "ok"}):
                with patch.object(ex, "_run_volatility_gate",
                                  return_value={"action": "allow", "message": "ok",
                                                "gate_enabled": False}):
                    with patch.object(ex, "place_stops_for_plan", return_value={}):
                        with patch(_JOURNAL_ENTRY):
                            report = ex.execute_plan(plan, "2026-03-24")

        assert report.get("circuit_breaker_tripped") is not True
        assert report["successful_entries"] == 1
        broker.place_order.assert_called_once()

    def test_circuit_breaker_already_tripped_blocks_without_broker_call(self):
        """Fast-path: if breaker already tripped, broker equity is NOT queried."""
        cfg = _live_config(max_daily_loss_pct=0.02)
        ex = _make_executor(cfg)
        ex._daily_start_equity = 10_000.0
        ex._circuit_breaker_tripped = True   # pre-tripped

        broker = _mock_broker(equity=9_500.0)
        broker.get_positions.return_value = []
        ex._broker = broker

        plan = self._plan_with_entries(n=1)
        tickers = [e["ticker"] for e in plan["proposed_entries"]]

        with patch(_FILTER_TRADABLE, return_value=(tickers, [])):
            with patch.object(ex, "check_market_state",
                              return_value={"is_tradeable": True, "message": "ok"}):
                with patch.object(ex, "_run_volatility_gate",
                                  return_value={"action": "allow", "message": "ok",
                                                "gate_enabled": False}):
                    with patch.object(ex, "place_stops_for_plan", return_value={}):
                        with patch(_JOURNAL_ENTRY):
                            report = ex.execute_plan(plan, "2026-03-24")

        # Already tripped — fast path should not call broker equity check
        # get_account_info should not be called again (it was NOT used to trip)
        broker.get_account_info.assert_not_called()

        assert report.get("circuit_breaker_tripped") is True
        assert report["entries"][0]["reason"] == "circuit_breaker"

    def test_circuit_breaker_journal_entry_on_trip(self):
        """Circuit breaker trip writes a 'circuit_breaker_tripped' journal entry."""
        cfg = _live_config(max_daily_loss_pct=0.02)
        ex = _make_executor(cfg)
        ex._daily_start_equity = 10_000.0

        broker = _mock_broker(equity=9_700.0)
        broker.get_positions.return_value = []
        ex._broker = broker

        plan = self._plan_with_entries(n=1)
        tickers = [e["ticker"] for e in plan["proposed_entries"]]

        journal_events = []

        def _capture_journal(event, data):
            journal_events.append(event)

        with patch(_FILTER_TRADABLE, return_value=(tickers, [])):
            with patch.object(ex, "check_market_state",
                              return_value={"is_tradeable": True, "message": "ok"}):
                with patch.object(ex, "_run_volatility_gate",
                                  return_value={"action": "allow", "message": "ok",
                                                "gate_enabled": False}):
                    with patch.object(ex, "place_stops_for_plan", return_value={}):
                        with patch(_JOURNAL_ENTRY, side_effect=_capture_journal):
                            with patch(_TELEGRAM, return_value=True):
                                ex.execute_plan(plan, "2026-03-24")

        assert "circuit_breaker_tripped" in journal_events, (
            f"Expected 'circuit_breaker_tripped' journal event, got: {journal_events}"
        )

    def test_circuit_breaker_dry_run_bypasses_check(self):
        """Dry-run mode does NOT trip the circuit breaker (no real money at risk)."""
        cfg = _live_config(max_daily_loss_pct=0.02)
        cfg["trading"]["live_safety"]["dry_run_first"] = True  # dry-run ON
        ex = _make_executor(cfg)
        assert ex.is_dry_run is True

        ex._daily_start_equity = 10_000.0

        # Massive 10% loss — would normally trip the breaker
        broker = _mock_broker(equity=9_000.0)
        broker.get_positions.return_value = []
        ex._broker = broker

        plan = self._plan_with_entries(n=1)
        tickers = [e["ticker"] for e in plan["proposed_entries"]]

        with patch(_FILTER_TRADABLE, return_value=(tickers, [])):
            with patch.object(ex, "check_market_state",
                              return_value={"is_tradeable": True, "message": "ok"}):
                with patch.object(ex, "_run_volatility_gate",
                                  return_value={"action": "allow", "message": "ok",
                                                "gate_enabled": False}):
                    with patch.object(ex, "place_stops_for_plan", return_value={}):
                        with patch(_JOURNAL_ENTRY):
                            report = ex.execute_plan(plan, "2026-03-24")

        # Dry-run bypasses circuit breaker — entries proceed
        assert report.get("circuit_breaker_tripped") is not True
        # Dry-run entries succeed
        assert report["entries"][0].get("dry_run") is True


# ═══════════════════════════════════════════════════════════════
# Edge cases and safety constraints
# ═══════════════════════════════════════════════════════════════

class TestSafetyConstraints:
    """Miscellaneous safety and edge-case tests."""

    def test_daily_order_limit_enforced(self):
        """Subsequent orders fail when daily order limit is hit."""
        cfg = _live_config()
        cfg["trading"]["live_safety"]["max_daily_orders"] = 1  # Only 1 per day
        ex = _make_executor(cfg)
        broker = _mock_broker()
        broker.place_order.return_value = _submitted_order("SPY", 5)
        ex._broker = broker
        ex._daily_start_equity = 10_000.0

        # Already used the limit
        ex._daily_order_count = 1

        plan = {
            "status": "APPROVED",
            "proposed_entries": [
                {"ticker": "SPY", "entry_price": 450.0, "position_size": 5,
                 "strategy": "momentum_breakout", "confidence": 0.7, "stop_price": 440.0},
            ],
            "proposed_exits": [],
        }

        with patch(_FILTER_TRADABLE, return_value=(["SPY"], [])):
            with patch.object(ex, "check_market_state",
                              return_value={"is_tradeable": True, "message": "ok"}):
                with patch.object(ex, "_run_volatility_gate",
                                  return_value={"action": "allow", "message": "ok",
                                                "gate_enabled": False}):
                    with patch.object(ex, "place_stops_for_plan", return_value={}):
                        with patch(_JOURNAL_ENTRY):
                            report = ex.execute_plan(plan, "2026-03-24")

        assert report["successful_entries"] == 0
        broker.place_order.assert_not_called()
        assert "limit" in report["entries"][0]["errors"][0].lower()

    def test_not_connected_returns_error_report(self):
        """Executor returns an error report if not connected to broker."""
        cfg = _live_config()
        ex = LiveExecutor(cfg)
        # DO NOT set _connected = True

        plan = {
            "status": "APPROVED",
            "proposed_entries": [
                {"ticker": "AAPL", "entry_price": 150.0, "position_size": 10,
                 "strategy": "momentum_breakout", "confidence": 0.75,
                 "stop_price": 145.0},
            ],
            "proposed_exits": [],
        }

        with patch(_JOURNAL_ENTRY):
            report = ex.execute_plan(plan, "2026-03-24")

        assert "not connected" in report.get("error", "").lower(), (
            f"Expected 'not connected' in report['error'], got: {report}"
        )

    def test_halted_executor_returns_error(self):
        """A halted executor rejects all execution immediately."""
        cfg = _live_config()
        ex = _make_executor(cfg)
        ex._halted = True
        ex._halt_reason = "Manual emergency halt"

        plan = {
            "status": "APPROVED",
            "proposed_entries": [
                {"ticker": "AAPL", "entry_price": 150.0, "position_size": 10,
                 "strategy": "momentum_breakout", "confidence": 0.75,
                 "stop_price": 145.0},
            ],
            "proposed_exits": [],
        }

        broker = _mock_broker()
        ex._broker = broker

        with patch(_JOURNAL_ENTRY):
            report = ex.execute_plan(plan, "2026-03-24")

        broker.place_order.assert_not_called()
        assert "halt" in report.get("error", "").lower(), (
            f"Expected 'halt' in report['error'], got: {report}"
        )
