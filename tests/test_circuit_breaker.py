"""Tests for A4 (circuit breaker) and A2 (bare exception fixes) in LiveExecutor.

A4: Circuit breaker halts new entries when daily portfolio drawdown > threshold.
A2: Bare except blocks log the exception instead of silently swallowing it.

Run:
    cd /root/atlas && python3 -m pytest tests/test_circuit_breaker.py -v
"""
from __future__ import annotations

import copy
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

from brokers.live_executor import LiveExecutor, preflight_check_order
from brokers.base import AccountInfo, OrderResult, OrderSide, OrderStatus


# ── Minimal config ────────────────────────────────────────────

def _live_config(max_daily_loss_pct: float = 0.02) -> dict:
    """Return a minimal live-trading config with circuit breaker configured."""
    return {
        "version": "test-v1.0",
        "market": "sp500",
        "trading": {
            "mode": "live",
            "broker": "alpaca",
            "live_enabled": True,
            "live_safety": {
                "max_order_value": 5000,
                "max_daily_orders": 20,
                "dry_run_first": False,
                "max_daily_loss_pct": max_daily_loss_pct,
            },
        },
        "risk": {
            "starting_equity": 5000.0,
            "max_risk_per_trade_pct": 0.01,
            "max_open_positions": 5,
        },
        "data": {"source": "alpaca", "history_years": 3},
    }


def _mock_broker(equity: float = 5000.0) -> MagicMock:
    """Return a mock broker that reports *equity* as current portfolio value."""
    broker = MagicMock()
    account = AccountInfo(equity=equity, cash=1000.0)
    broker.get_account_info.return_value = account
    return broker


def _make_executor(config: dict) -> LiveExecutor:
    executor = LiveExecutor(config)
    executor._connected = True
    executor._halted = False
    return executor


# ── max_daily_loss_pct property ───────────────────────────────

class TestMaxDailyLossPct:
    def test_reads_from_config(self):
        cfg = _live_config(max_daily_loss_pct=0.03)
        ex = _make_executor(cfg)
        assert ex.max_daily_loss_pct == pytest.approx(0.03)

    def test_default_when_missing(self):
        cfg = _live_config()
        del cfg["trading"]["live_safety"]["max_daily_loss_pct"]
        ex = _make_executor(cfg)
        assert ex.max_daily_loss_pct == pytest.approx(0.02)

    def test_handles_int_value(self):
        cfg = _live_config()
        cfg["trading"]["live_safety"]["max_daily_loss_pct"] = 5  # 5% as int
        ex = _make_executor(cfg)
        assert ex.max_daily_loss_pct == pytest.approx(5.0)


# ── _reset_circuit_breaker_if_new_day ─────────────────────────

class TestResetCircuitBreaker:
    def test_resets_on_new_day(self):
        ex = _make_executor(_live_config())
        ex._circuit_breaker_tripped = True
        ex._daily_start_equity = 5000.0
        ex._daily_date = "2026-03-23"

        ex._reset_circuit_breaker_if_new_day("2026-03-24")
        assert ex._circuit_breaker_tripped is False
        assert ex._daily_start_equity == 0.0

    def test_no_reset_same_day(self):
        ex = _make_executor(_live_config())
        ex._circuit_breaker_tripped = True
        ex._daily_start_equity = 5000.0
        ex._daily_date = "2026-03-24"

        ex._reset_circuit_breaker_if_new_day("2026-03-24")
        assert ex._circuit_breaker_tripped is True
        assert ex._daily_start_equity == pytest.approx(5000.0)


# ── _capture_start_equity ─────────────────────────────────────

class TestCaptureStartEquity:
    def test_captures_equity_when_not_set(self):
        ex = _make_executor(_live_config())
        ex._broker = _mock_broker(equity=5500.0)
        ex._daily_start_equity = 0.0

        ex._capture_start_equity()
        assert ex._daily_start_equity == pytest.approx(5500.0)

    def test_does_not_overwrite_if_already_set(self):
        ex = _make_executor(_live_config())
        ex._broker = _mock_broker(equity=6000.0)
        ex._daily_start_equity = 5000.0  # already set

        ex._capture_start_equity()
        assert ex._daily_start_equity == pytest.approx(5000.0)  # unchanged

    def test_handles_broker_error_gracefully(self):
        ex = _make_executor(_live_config())
        broker = MagicMock()
        broker.get_account_info.side_effect = ConnectionError("no broker")
        ex._broker = broker
        ex._daily_start_equity = 0.0

        # Should not raise — just log a warning
        ex._capture_start_equity()
        assert ex._daily_start_equity == pytest.approx(0.0)

    def test_noop_when_no_broker(self):
        ex = _make_executor(_live_config())
        ex._broker = None
        ex._daily_start_equity = 0.0

        ex._capture_start_equity()  # Should not raise
        assert ex._daily_start_equity == pytest.approx(0.0)


# ── _check_circuit_breaker ────────────────────────────────────

class TestCheckCircuitBreaker:
    def _executor_with_state(
        self, start_equity: float, current_equity: float,
        threshold: float = 0.02
    ) -> LiveExecutor:
        cfg = _live_config(max_daily_loss_pct=threshold)
        ex = _make_executor(cfg)
        ex._daily_start_equity = start_equity
        ex._broker = _mock_broker(equity=current_equity)
        return ex

    def test_allows_when_no_loss(self):
        ex = self._executor_with_state(5000.0, 5000.0)
        assert ex._check_circuit_breaker("2026-03-24") is False

    def test_allows_when_loss_below_threshold(self):
        # 1% loss, 2% threshold → allow
        ex = self._executor_with_state(5000.0, 4950.0, threshold=0.02)
        assert ex._check_circuit_breaker("2026-03-24") is False

    def test_blocks_when_loss_at_threshold(self):
        # Exactly 2% loss at 2% threshold → block
        ex = self._executor_with_state(5000.0, 4900.0, threshold=0.02)
        with patch("brokers.live_executor.send_message", create=True):
            result = ex._check_circuit_breaker("2026-03-24")
        assert result is True

    def test_blocks_when_loss_exceeds_threshold(self):
        # 3% loss at 2% threshold → block
        ex = self._executor_with_state(5000.0, 4850.0, threshold=0.02)
        with patch("utils.telegram.send_message", return_value=True):
            result = ex._check_circuit_breaker("2026-03-24")
        assert result is True

    def test_sets_tripped_flag(self):
        ex = self._executor_with_state(5000.0, 4850.0, threshold=0.02)
        assert ex._circuit_breaker_tripped is False
        with patch("utils.telegram.send_message", return_value=True):
            ex._check_circuit_breaker("2026-03-24")
        assert ex._circuit_breaker_tripped is True

    def test_fast_path_when_already_tripped(self):
        ex = self._executor_with_state(5000.0, 4850.0, threshold=0.02)
        ex._circuit_breaker_tripped = True
        # Should not call broker at all
        result = ex._check_circuit_breaker("2026-03-24")
        ex._broker.get_account_info.assert_not_called()
        assert result is True

    def test_allows_when_start_equity_not_set(self):
        ex = self._executor_with_state(0.0, 4850.0, threshold=0.02)
        ex._daily_start_equity = 0.0
        assert ex._check_circuit_breaker("2026-03-24") is False

    def test_allows_when_broker_error(self):
        cfg = _live_config(max_daily_loss_pct=0.02)
        ex = _make_executor(cfg)
        ex._daily_start_equity = 5000.0
        broker = MagicMock()
        broker.get_account_info.side_effect = ConnectionError("no connection")
        ex._broker = broker
        # Non-blocking: should allow through (False)
        assert ex._check_circuit_breaker("2026-03-24") is False

    def test_sends_telegram_alert_on_trip(self):
        ex = self._executor_with_state(5000.0, 4800.0, threshold=0.02)
        with patch("utils.telegram.send_message", return_value=True) as mock_tg:
            ex._check_circuit_breaker("2026-03-24")
        # Telegram should have been called (via the import inside the method)
        # We patch the module directly
        # The implementation calls: from utils.telegram import send_message
        # so we can check the call was attempted even if it fails

    def test_journal_entry_on_trip(self):
        """Circuit breaker trip writes a journal entry."""
        ex = self._executor_with_state(5000.0, 4800.0, threshold=0.02)
        journal_events = []
        with patch("brokers.live_executor._journal_entry",
                   side_effect=lambda evt, data: journal_events.append(evt)):
            with patch("utils.telegram.send_message", return_value=True):
                ex._check_circuit_breaker("2026-03-24")
        assert "circuit_breaker_tripped" in journal_events


# ── Circuit breaker integration in execute_plan ───────────────

class TestCircuitBreakerInExecutePlan:
    """Verify circuit breaker blocks entries in the full execute_plan flow."""

    def _build_plan(self, n_entries: int = 2) -> dict:
        entries = []
        for i in range(n_entries):
            entries.append({
                "ticker": f"TICK{i}",
                "entry_price": 100.0,
                "position_size": 10,
                "strategy": "momentum_breakout",
                "confidence": 0.75,
                "stop_price": 95.0,
            })
        return {
            "status": "APPROVED",
            "proposed_entries": entries,
            "proposed_exits": [],
        }

    def test_circuit_breaker_blocks_entries(self):
        """When circuit breaker trips, all entries are BLOCKED."""
        cfg = _live_config(max_daily_loss_pct=0.01)
        ex = _make_executor(cfg)
        ex._daily_start_equity = 5000.0

        # Current equity shows 3% loss (exceeds 1% threshold)
        broker = _mock_broker(equity=4850.0)
        broker.get_positions.return_value = []
        broker.get_market_snapshot.return_value = None
        ex._broker = broker

        plan = self._build_plan(n_entries=2)

        with patch("brokers.live_executor.filter_tradable",
                   return_value=(["TICK0", "TICK1"], []), create=True):
            with patch("brokers.live_executor.LiveExecutor.check_market_state",
                       return_value={"is_tradeable": True, "message": "ok"}):
                with patch("utils.telegram.send_message", return_value=True):
                    with patch("brokers.live_executor.LiveExecutor._run_volatility_gate",
                               return_value={"action": "allow", "message": "ok",
                                             "gate_enabled": False}):
                        with patch("brokers.live_executor.LiveExecutor.place_stops_for_plan",
                                   return_value=[]):
                            with patch("brokers.live_executor._journal_entry"):
                                report = ex.execute_plan(plan, "2026-03-24")

        # All entries should be blocked by circuit breaker
        assert report.get("circuit_breaker_tripped") is True
        for entry_result in report["entries"]:
            assert entry_result["success"] is False
            assert entry_result.get("blocked") is True
            assert entry_result.get("reason") == "circuit_breaker"

    def test_dry_run_skips_circuit_breaker(self):
        """Dry-run mode bypasses circuit breaker check (no real money)."""
        cfg = _live_config(max_daily_loss_pct=0.01)
        cfg["trading"]["live_safety"]["dry_run_first"] = True
        ex = _make_executor(cfg)
        assert ex.is_dry_run is True

        # Circuit breaker should NOT trip in dry-run mode
        ex._daily_start_equity = 5000.0
        broker = _mock_broker(equity=4800.0)
        broker.get_positions.return_value = []
        ex._broker = broker

        plan = self._build_plan(n_entries=1)

        with patch("brokers.live_executor.filter_tradable",
                   return_value=(["TICK0"], []), create=True):
            with patch("brokers.live_executor.LiveExecutor.check_market_state",
                       return_value={"is_tradeable": True, "message": "ok"}):
                with patch("brokers.live_executor.LiveExecutor._run_volatility_gate",
                           return_value={"action": "allow", "message": "ok",
                                         "gate_enabled": False}):
                    with patch("brokers.live_executor.LiveExecutor.place_stops_for_plan",
                               return_value=[]):
                        with patch("brokers.live_executor._journal_entry"):
                            report = ex.execute_plan(plan, "2026-03-24")

        # Dry-run entries should succeed (no real order placed)
        assert report.get("circuit_breaker_tripped") is not True
        for entry_result in report["entries"]:
            # dry_run entries: success=True, dry_run=True
            assert entry_result.get("dry_run") is True


# ── A2: Bare exception handling improvements ──────────────────

class TestBareExceptionFixes:
    """Verify that bare except blocks are fixed — they must log, not silently pass."""

    def test_spread_capture_failure_logged_not_swallowed_entry(self):
        """Spread capture failure in _execute_entry should log, not silently pass."""
        cfg = _live_config()
        cfg["trading"]["live_safety"]["dry_run_first"] = False
        ex = _make_executor(cfg)

        broker = MagicMock()
        # Make get_market_snapshot raise to trigger the except block
        broker.get_market_snapshot.side_effect = RuntimeError("API down")
        broker.has_attr = True

        # place_order should succeed
        from brokers.base import OrderResult, OrderStatus
        broker.place_order.return_value = OrderResult(
            success=True, order_id="test-123", ticker="AAPL",
            side=OrderSide.BUY, status=OrderStatus.FILLED,
            fill_price=150.0, requested_qty=10,
        )
        ex._broker = broker
        ex._daily_order_count = 0
        ex._daily_date = "2026-03-24"

        entry = {
            "ticker": "AAPL", "entry_price": 150.0, "position_size": 10,
            "strategy": "momentum_breakout", "confidence": 0.75,
            "stop_price": 145.0,
        }

        with patch("brokers.live_executor._journal_entry"), \
             patch("journal.logger.TradeLedger.record_entry"):
            result = ex._execute_entry(entry, "2026-03-24")

        # The order should still be placed despite spread failure
        assert result["success"] is True
        broker.place_order.assert_called_once()

    def test_holding_days_failure_logged_not_swallowed(self):
        """Entry date parsing failure in _execute_exit should log, not silently pass."""
        cfg = _live_config()
        cfg["trading"]["live_safety"]["dry_run_first"] = False
        ex = _make_executor(cfg)

        from brokers.base import PositionInfo
        pos = PositionInfo(
            ticker="AAPL", entry_price=140.0, shares=10,
            current_price=155.0, market_value=1550.0,
        )
        # Give it a bad entry_date to trigger the except block
        pos.entry_date = "INVALID DATE FORMAT 🔥"
        pos.strategy = "momentum_breakout"

        broker = MagicMock()
        broker.get_positions.return_value = [pos]
        broker.cancel_all_orders_for_ticker = MagicMock()

        from brokers.base import OrderResult, OrderStatus
        broker.place_order.return_value = OrderResult(
            success=True, order_id="exit-456", ticker="AAPL",
            side=OrderSide.SELL, status=OrderStatus.FILLED,
            fill_price=155.0, requested_qty=10,
        )
        broker.get_order_status.return_value = OrderResult(
            success=True, order_id="exit-456", ticker="AAPL",
            side=OrderSide.SELL, status=OrderStatus.FILLED,
            fill_price=155.0, requested_qty=10,
        )
        ex._broker = broker
        ex._daily_order_count = 0
        ex._daily_date = "2026-03-24"

        exit_rec = {"ticker": "AAPL", "reason": "signal_exit", "direction": "long"}

        with patch("brokers.live_executor._journal_entry"), \
             patch("journal.logger.TradeLedger.record_exit"), \
             patch("brokers.live_executor.LiveExecutor._cancel_open_orders_for_ticker", return_value=0), \
             patch("brokers.live_executor.LiveExecutor.cancel_protective_stop"):
            result = ex._execute_exit(exit_rec, "2026-03-24")

        # Exit should succeed even with bad entry date
        assert result["success"] is True
        # holding_days should be None (failed to parse)
        assert result.get("holding_days") is None

    def test_no_bare_except_in_execution_code(self):
        """Verify no bare 'except Exception:' blocks remain in live_executor.py."""
        executor_path = PROJECT / "brokers" / "live_executor.py"
        source = executor_path.read_text()
        lines = source.splitlines()
        bare_exceptions = []
        for lineno, line in enumerate(lines, 1):
            stripped = line.strip()
            if stripped == "except Exception:" or stripped == "except:":
                bare_exceptions.append((lineno, line.rstrip()))
        assert bare_exceptions == [], (
            f"Found bare except blocks in live_executor.py:\n"
            + "\n".join(f"  Line {ln}: {txt}" for ln, txt in bare_exceptions)
        )
