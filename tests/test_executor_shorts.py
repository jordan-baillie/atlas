"""Tests for short selling support in LiveExecutor.

Verifies:
  - Short entry uses OrderSide.SELL
  - Long entry still uses OrderSide.BUY (regression)
  - Short exit uses OrderSide.BUY (buy to cover)
  - Long exit uses OrderSide.SELL (regression)
  - Preflight rejects short entries when short_enabled=false
  - Preflight allows short entries when short_enabled=true
  - Protective stop order side is inverted for shorts (BUY stop)
  - Ledger entry includes direction field
  - PnL is calculated correctly for shorts (price fall = profit)
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Ensure project root is importable
sys.path.insert(0, str(Path(__file__).parent.parent))

from brokers.base import (
    OrderResult, OrderSide, OrderStatus, OrderType, PositionInfo,
)
from brokers.live_executor import LiveExecutor


# ═══════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════

def _make_config(short_enabled: bool = True) -> dict:
    """Minimal config for LiveExecutor tests."""
    return {
        "trading": {
            "live_enabled": True,
            "broker": "alpaca",
            "live_safety": {
                "max_order_value": 100_000,
                "max_daily_orders": 50,
                "dry_run_first": False,  # live mode for most tests
            },
        },
        "strategies": {
            "mean_reversion": {
                "short_enabled": short_enabled,
            },
        },
        "fees": {
            "commission_per_trade": 0.0,
            "commission_pct": 0.0,
        },
    }


def _make_executor(short_enabled: bool = True) -> LiveExecutor:
    """Create a LiveExecutor that is connected with a mock broker."""
    executor = LiveExecutor(_make_config(short_enabled=short_enabled))
    executor._connected = True
    executor._broker = MagicMock()
    executor._daily_date = "2026-03-15"
    executor._daily_order_count = 0
    return executor


def _make_order_result(success: bool = True, order_id: str = "ORD-001",
                       fill_price: float = 100.0) -> OrderResult:
    return OrderResult(
        success=success,
        order_id=order_id,
        status=OrderStatus.FILLED if success else OrderStatus.FAILED,
        fill_price=fill_price,
        message="OK" if success else "FAILED",
        raw={"filled_at": "2026-03-15T10:00:00"},
    )


def _make_long_entry(ticker: str = "AAPL", price: float = 100.0, qty: int = 10) -> dict:
    return {
        "ticker": ticker,
        "direction": "long",
        "entry_price": price,
        "position_size": qty,
        "stop_price": 95.0,
        "take_profit": 110.0,
        "strategy": "mean_reversion",
        "confidence": 0.8,
        "order_type": "",
    }


def _make_short_entry(ticker: str = "AAPL", price: float = 100.0, qty: int = 10) -> dict:
    return {
        "ticker": ticker,
        "direction": "short",
        "entry_price": price,
        "position_size": qty,
        "stop_price": 105.0,   # stop ABOVE entry for shorts
        "take_profit": 90.0,   # TP BELOW entry for shorts
        "strategy": "mean_reversion",
        "confidence": 0.8,
        "order_type": "",
    }


def _make_long_exit(ticker: str = "AAPL") -> dict:
    return {"ticker": ticker, "direction": "long", "reason": "signal_exit"}


def _make_short_exit(ticker: str = "AAPL") -> dict:
    return {"ticker": ticker, "direction": "short", "reason": "signal_exit"}


def _make_position(ticker: str = "AAPL", direction: str = "long",
                   shares: int = 10, entry_price: float = 100.0,
                   current_price: float = 105.0) -> PositionInfo:
    return PositionInfo(
        ticker=ticker,
        strategy="mean_reversion",
        entry_price=entry_price,
        shares=shares,
        current_price=current_price,
    )


# ═══════════════════════════════════════════════════════════════
# Entry order side
# ═══════════════════════════════════════════════════════════════

class TestEntryOrderSide:
    """Verify _execute_entry() uses correct OrderSide based on direction."""

    def test_long_entry_uses_buy(self):
        """Long direction must place OrderSide.BUY."""
        executor = _make_executor()
        executor._broker.place_order.return_value = _make_order_result()

        with patch("brokers.live_executor._journal_entry"):
            result = executor._execute_entry(_make_long_entry(), "2026-03-15")

        call_kwargs = executor._broker.place_order.call_args
        assert call_kwargs is not None, "place_order was not called"
        assert call_kwargs.kwargs["side"] == OrderSide.BUY
        assert result["side"] == "BUY"
        assert result["direction"] == "long"

    def test_short_entry_uses_sell(self):
        """Short direction must place OrderSide.SELL (short sell to open)."""
        executor = _make_executor()
        executor._broker.place_order.return_value = _make_order_result()

        with patch("brokers.live_executor._journal_entry"):
            result = executor._execute_entry(_make_short_entry(), "2026-03-15")

        call_kwargs = executor._broker.place_order.call_args
        assert call_kwargs is not None, "place_order was not called"
        assert call_kwargs.kwargs["side"] == OrderSide.SELL
        assert result["side"] == "SELL"
        assert result["direction"] == "short"

    def test_no_direction_defaults_to_long_buy(self):
        """Entry with no direction key defaults to BUY (backward-compatible)."""
        executor = _make_executor()
        executor._broker.place_order.return_value = _make_order_result()
        entry = _make_long_entry()
        del entry["direction"]

        with patch("brokers.live_executor._journal_entry"):
            result = executor._execute_entry(entry, "2026-03-15")

        call_kwargs = executor._broker.place_order.call_args
        assert call_kwargs.kwargs["side"] == OrderSide.BUY
        assert result["direction"] == "long"

    def test_short_entry_dry_run_uses_sell(self):
        """Dry-run short entry reports SELL side without calling broker."""
        config = _make_config()
        config["trading"]["live_safety"]["dry_run_first"] = True
        executor = LiveExecutor(config)
        executor._connected = True
        executor._broker = MagicMock()
        executor._daily_date = "2026-03-15"

        with patch("brokers.live_executor._journal_entry"):
            result = executor._execute_entry(_make_short_entry(), "2026-03-15")

        executor._broker.place_order.assert_not_called()
        assert result["side"] == "SELL"
        assert result["direction"] == "short"
        assert result["dry_run"] is True

    def test_long_entry_dry_run_uses_buy(self):
        """Dry-run long entry reports BUY side (regression)."""
        config = _make_config()
        config["trading"]["live_safety"]["dry_run_first"] = True
        executor = LiveExecutor(config)
        executor._connected = True
        executor._broker = MagicMock()
        executor._daily_date = "2026-03-15"

        with patch("brokers.live_executor._journal_entry"):
            result = executor._execute_entry(_make_long_entry(), "2026-03-15")

        executor._broker.place_order.assert_not_called()
        assert result["side"] == "BUY"
        assert result["direction"] == "long"


# ═══════════════════════════════════════════════════════════════
# Exit order side
# ═══════════════════════════════════════════════════════════════

class TestExitOrderSide:
    """Verify _execute_exit() uses correct OrderSide based on direction."""

    def test_long_exit_uses_sell(self):
        """Long position exit must place OrderSide.SELL."""
        executor = _make_executor()
        executor._broker.get_positions.return_value = [_make_position("AAPL")]
        executor._broker.place_order.return_value = _make_order_result()

        with patch("brokers.live_executor._journal_entry"):
            result = executor._execute_exit(_make_long_exit(), "2026-03-15")

        call_kwargs = executor._broker.place_order.call_args
        assert call_kwargs is not None, "place_order was not called"
        assert call_kwargs.kwargs["side"] == OrderSide.SELL
        assert result["side"] == "SELL"

    def test_short_exit_uses_buy(self):
        """Short position exit must place OrderSide.BUY (buy to cover)."""
        executor = _make_executor()
        executor._broker.get_positions.return_value = [
            _make_position("AAPL", current_price=90.0)
        ]
        executor._broker.place_order.return_value = _make_order_result(fill_price=90.0)

        with patch("brokers.live_executor._journal_entry"):
            result = executor._execute_exit(_make_short_exit(), "2026-03-15")

        call_kwargs = executor._broker.place_order.call_args
        assert call_kwargs is not None, "place_order was not called"
        assert call_kwargs.kwargs["side"] == OrderSide.BUY
        assert result["side"] == "BUY"

    def test_no_direction_exit_defaults_to_sell(self):
        """Exit with no direction defaults to SELL (backward-compatible)."""
        executor = _make_executor()
        executor._broker.get_positions.return_value = [_make_position("AAPL")]
        executor._broker.place_order.return_value = _make_order_result()
        exit_rec = {"ticker": "AAPL", "reason": "signal_exit"}  # no direction

        with patch("brokers.live_executor._journal_entry"):
            result = executor._execute_exit(exit_rec, "2026-03-15")

        call_kwargs = executor._broker.place_order.call_args
        assert call_kwargs.kwargs["side"] == OrderSide.SELL
        assert result["side"] == "SELL"

    def test_short_exit_dry_run_uses_buy(self):
        """Dry-run short exit reports BUY side."""
        config = _make_config()
        config["trading"]["live_safety"]["dry_run_first"] = True
        executor = LiveExecutor(config)
        executor._connected = True
        executor._broker = MagicMock()
        executor._broker.get_positions.return_value = [_make_position("AAPL")]
        executor._daily_date = "2026-03-15"

        with patch("brokers.live_executor._journal_entry"):
            result = executor._execute_exit(_make_short_exit(), "2026-03-15")

        executor._broker.place_order.assert_not_called()
        assert result["side"] == "BUY"
        assert result["dry_run"] is True

    def test_long_exit_dry_run_uses_sell(self):
        """Dry-run long exit reports SELL side (regression)."""
        config = _make_config()
        config["trading"]["live_safety"]["dry_run_first"] = True
        executor = LiveExecutor(config)
        executor._connected = True
        executor._broker = MagicMock()
        executor._broker.get_positions.return_value = [_make_position("AAPL")]
        executor._daily_date = "2026-03-15"

        with patch("brokers.live_executor._journal_entry"):
            result = executor._execute_exit(_make_long_exit(), "2026-03-15")

        executor._broker.place_order.assert_not_called()
        assert result["side"] == "SELL"
        assert result["dry_run"] is True


# ═══════════════════════════════════════════════════════════════
# Preflight: short_enabled gate
# ═══════════════════════════════════════════════════════════════

class TestPreflightShortEnabled:
    """Verify execute_plan() filters short entries based on short_enabled flag."""

    def _make_approved_plan(self, direction: str = "short") -> dict:
        return {
            "status": "APPROVED",
            "proposed_entries": [{
                "ticker": "AAPL",
                "direction": direction,
                "entry_price": 100.0,
                "position_size": 10,
                "stop_price": 105.0 if direction == "short" else 95.0,
                "take_profit": 90.0 if direction == "short" else 110.0,
                "strategy": "mean_reversion",
                "confidence": 0.8,
                "order_type": "",
            }],
            "proposed_exits": [],
        }

    def _run_plan(self, executor: LiveExecutor, plan: dict) -> dict:
        """Run execute_plan() with all external calls mocked."""
        with patch("brokers.live_executor._journal_entry"), \
             patch.object(executor, "_run_volatility_gate",
                          return_value={"action": "none", "size_multiplier": 1.0,
                                        "message": "ok", "gate_enabled": False,
                                        "triggered_count": 0, "flags": []}), \
             patch.object(executor, "place_stops_for_plan", return_value={}), \
             patch("brokers.alpaca.tradable_assets.filter_tradable",
                   return_value=(["AAPL"], [])), \
             patch.object(executor, "check_market_state",
                          return_value={"is_tradeable": True, "message": "ok",
                                        "states": []}):
            return executor.execute_plan(plan, "2026-03-15")

    def test_short_entry_rejected_when_short_disabled(self):
        """Short entries must be filtered when short_enabled=False."""
        executor = _make_executor(short_enabled=False)
        executor._broker.place_order.return_value = _make_order_result()

        report = self._run_plan(executor, self._make_approved_plan(direction="short"))

        # place_order must NOT have been called for the short entry
        executor._broker.place_order.assert_not_called()
        assert report["total_entries"] == 0, (
            f"Expected 0 entries after short filter, got {report['total_entries']}"
        )

    def test_short_entry_allowed_when_short_enabled(self):
        """Short entries must proceed when short_enabled=True."""
        executor = _make_executor(short_enabled=True)
        executor._broker.place_order.return_value = _make_order_result()

        report = self._run_plan(executor, self._make_approved_plan(direction="short"))

        executor._broker.place_order.assert_called_once()
        assert report["total_entries"] == 1

    def test_long_entry_always_allowed(self):
        """Long entries are never filtered by short_enabled gate."""
        executor = _make_executor(short_enabled=False)
        executor._broker.place_order.return_value = _make_order_result()

        report = self._run_plan(executor, self._make_approved_plan(direction="long"))

        executor._broker.place_order.assert_called_once()
        assert report["total_entries"] == 1


# ═══════════════════════════════════════════════════════════════
# Protective order side
# ═══════════════════════════════════════════════════════════════

class TestProtectiveOrderSide:
    """Verify place_protective_stop() uses correct side based on direction."""

    def test_long_protective_stop_uses_sell(self):
        """Long position protective stop must be SELL."""
        executor = _make_executor()
        executor._broker.place_order.return_value = _make_order_result(order_id="STOP-001")

        with patch("brokers.live_executor._journal_entry"):
            order_id = executor.place_protective_stop(
                ticker="AAPL", qty=10, stop_price=95.0,
                strategy="mean_reversion", trade_date="2026-03-15",
                direction="long",
            )

        call_kwargs = executor._broker.place_order.call_args
        assert call_kwargs.kwargs["side"] == OrderSide.SELL
        assert order_id == "STOP-001"

    def test_short_protective_stop_uses_buy(self):
        """Short position protective stop must be BUY (buy-to-cover)."""
        executor = _make_executor()
        executor._broker.place_order.return_value = _make_order_result(order_id="STOP-002")

        with patch("brokers.live_executor._journal_entry"):
            order_id = executor.place_protective_stop(
                ticker="AAPL", qty=10, stop_price=105.0,
                strategy="mean_reversion", trade_date="2026-03-15",
                direction="short",
            )

        call_kwargs = executor._broker.place_order.call_args
        assert call_kwargs.kwargs["side"] == OrderSide.BUY
        assert order_id == "STOP-002"

    def test_default_direction_uses_sell_stop(self):
        """place_protective_stop() without direction defaults to SELL (backward-compat)."""
        executor = _make_executor()
        executor._broker.place_order.return_value = _make_order_result(order_id="STOP-003")

        with patch("brokers.live_executor._journal_entry"):
            executor.place_protective_stop(
                ticker="AAPL", qty=10, stop_price=95.0,
                strategy="mean_reversion", trade_date="2026-03-15",
            )

        call_kwargs = executor._broker.place_order.call_args
        assert call_kwargs.kwargs["side"] == OrderSide.SELL

    def test_trailing_stop_long_uses_sell(self):
        """Trailing stop for long position uses SELL."""
        executor = _make_executor()
        executor._broker.place_order.return_value = _make_order_result()

        with patch("brokers.live_executor._journal_entry"):
            executor.place_protective_stop(
                ticker="AAPL", qty=10, stop_price=95.0,
                trailing_atr=2.0, strategy="mean_reversion",
                trade_date="2026-03-15", direction="long",
            )

        call_kwargs = executor._broker.place_order.call_args
        assert call_kwargs.kwargs["side"] == OrderSide.SELL
        assert call_kwargs.kwargs["order_type"] == OrderType.TRAILING_STOP

    def test_trailing_stop_short_uses_buy(self):
        """Trailing stop for short position uses BUY."""
        executor = _make_executor()
        executor._broker.place_order.return_value = _make_order_result()

        with patch("brokers.live_executor._journal_entry"):
            executor.place_protective_stop(
                ticker="AAPL", qty=10, stop_price=105.0,
                trailing_atr=2.0, strategy="mean_reversion",
                trade_date="2026-03-15", direction="short",
            )

        call_kwargs = executor._broker.place_order.call_args
        assert call_kwargs.kwargs["side"] == OrderSide.BUY
        assert call_kwargs.kwargs["order_type"] == OrderType.TRAILING_STOP

    def test_place_stops_for_plan_passes_direction(self):
        """place_stops_for_plan must pass direction from entry_rec to place_protective_stop."""
        executor = _make_executor()

        plan = {
            "proposed_entries": [
                {"ticker": "AAPL", "direction": "short", "position_size": 10,
                 "stop_price": 105.0, "strategy": "mean_reversion", "features": {}},
            ],
        }
        entry_results = [{"ticker": "AAPL", "success": True, "status": "FILLED"}]

        with patch.object(executor, "place_protective_stop", return_value="STOP-XYZ") as mock_stop:
            executor.place_stops_for_plan(plan, entry_results, _make_config(), "2026-03-15")

        mock_stop.assert_called_once()
        assert mock_stop.call_args.kwargs.get("direction") == "short", (
            f"Expected direction='short', got {mock_stop.call_args.kwargs.get('direction')}"
        )


# ═══════════════════════════════════════════════════════════════
# Ledger direction field
# ═══════════════════════════════════════════════════════════════

class TestLedgerDirection:
    """Verify TradeLedger records include the direction field."""

    def test_entry_ledger_includes_direction_long(self):
        """TradeLedger.record_entry must receive direction='long' for long entries."""
        executor = _make_executor()
        executor._broker.place_order.return_value = _make_order_result()

        mock_ledger = MagicMock()
        with patch("brokers.live_executor._journal_entry"), \
             patch("journal.logger.TradeLedger", return_value=mock_ledger):
            executor._execute_entry(_make_long_entry(), "2026-03-15")

        mock_ledger.record_entry.assert_called_once()
        ledger_kwargs = mock_ledger.record_entry.call_args[0][0]
        assert ledger_kwargs.get("direction") == "long"

    def test_entry_ledger_includes_direction_short(self):
        """TradeLedger.record_entry must receive direction='short' for short entries."""
        executor = _make_executor()
        executor._broker.place_order.return_value = _make_order_result()

        mock_ledger = MagicMock()
        with patch("brokers.live_executor._journal_entry"), \
             patch("journal.logger.TradeLedger", return_value=mock_ledger):
            executor._execute_entry(_make_short_entry(), "2026-03-15")

        mock_ledger.record_entry.assert_called_once()
        ledger_kwargs = mock_ledger.record_entry.call_args[0][0]
        assert ledger_kwargs.get("direction") == "short"

    def test_exit_ledger_includes_direction_long(self):
        """TradeLedger.record_exit must receive direction='long' for long exits."""
        executor = _make_executor()
        executor._broker.get_positions.return_value = [
            _make_position("AAPL", entry_price=90.0, current_price=100.0)
        ]
        executor._broker.place_order.return_value = _make_order_result(fill_price=100.0)

        mock_ledger = MagicMock()
        with patch("brokers.live_executor._journal_entry"), \
             patch("journal.logger.TradeLedger", return_value=mock_ledger):
            executor._execute_exit(_make_long_exit(), "2026-03-15")

        mock_ledger.record_exit.assert_called_once()
        ledger_kwargs = mock_ledger.record_exit.call_args[0][0]
        assert ledger_kwargs.get("direction") == "long"

    def test_exit_ledger_includes_direction_short(self):
        """TradeLedger.record_exit must receive direction='short' for short exits."""
        executor = _make_executor()
        executor._broker.get_positions.return_value = [
            _make_position("AAPL", entry_price=100.0, current_price=90.0)
        ]
        executor._broker.place_order.return_value = _make_order_result(fill_price=90.0)

        mock_ledger = MagicMock()
        with patch("brokers.live_executor._journal_entry"), \
             patch("journal.logger.TradeLedger", return_value=mock_ledger):
            executor._execute_exit(_make_short_exit(), "2026-03-15")

        mock_ledger.record_exit.assert_called_once()
        ledger_kwargs = mock_ledger.record_exit.call_args[0][0]
        assert ledger_kwargs.get("direction") == "short"


# ═══════════════════════════════════════════════════════════════
# Short PnL calculation
# ═══════════════════════════════════════════════════════════════

class TestShortPnL:
    """Verify PnL is sign-correct for short positions."""

    def test_short_exit_pnl_positive_when_price_falls(self):
        """Short position earns profit when exit price < entry price."""
        executor = _make_executor()
        executor._broker.get_positions.return_value = [
            _make_position("AAPL", entry_price=100.0, current_price=90.0)
        ]
        # Exit price will be 90 * 0.99 ≈ 89.1 for LIMIT, fill confirmed at 90
        executor._broker.place_order.return_value = _make_order_result(fill_price=90.0)

        mock_ledger = MagicMock()
        with patch("brokers.live_executor._journal_entry"), \
             patch("journal.logger.TradeLedger", return_value=mock_ledger):
            executor._execute_exit(_make_short_exit(), "2026-03-15")

        mock_ledger.record_exit.assert_called_once()
        ledger_kwargs = mock_ledger.record_exit.call_args[0][0]
        # Short PnL: (entry - fill) * shares = (100 - 90) * 10 = +100
        assert ledger_kwargs["pnl"] == pytest.approx(100.0, abs=1.0)
        assert ledger_kwargs["pnl_pct"] > 0, "Short profit should be positive"

    def test_short_exit_pnl_negative_when_price_rises(self):
        """Short position loses when exit price > entry price."""
        executor = _make_executor()
        executor._broker.get_positions.return_value = [
            _make_position("AAPL", entry_price=100.0, current_price=110.0)
        ]
        executor._broker.place_order.return_value = _make_order_result(fill_price=110.0)

        mock_ledger = MagicMock()
        with patch("brokers.live_executor._journal_entry"), \
             patch("journal.logger.TradeLedger", return_value=mock_ledger):
            executor._execute_exit(_make_short_exit(), "2026-03-15")

        mock_ledger.record_exit.assert_called_once()
        ledger_kwargs = mock_ledger.record_exit.call_args[0][0]
        # Short PnL: (entry - fill) * shares = (100 - 110) * 10 = -100
        assert ledger_kwargs["pnl"] == pytest.approx(-100.0, abs=1.0)
        assert ledger_kwargs["pnl_pct"] < 0, "Short loss should be negative"

    def test_long_pnl_positive_when_price_rises(self):
        """Long position PnL calculation must be unchanged (regression)."""
        executor = _make_executor()
        executor._broker.get_positions.return_value = [
            _make_position("AAPL", entry_price=90.0, current_price=100.0)
        ]
        executor._broker.place_order.return_value = _make_order_result(fill_price=100.0)

        mock_ledger = MagicMock()
        with patch("brokers.live_executor._journal_entry"), \
             patch("journal.logger.TradeLedger", return_value=mock_ledger):
            executor._execute_exit(_make_long_exit(), "2026-03-15")

        mock_ledger.record_exit.assert_called_once()
        ledger_kwargs = mock_ledger.record_exit.call_args[0][0]
        # Long PnL: (fill - entry) * shares = (100 - 90) * 10 = +100
        assert ledger_kwargs["pnl"] == pytest.approx(100.0, abs=1.0)
        assert ledger_kwargs["pnl_pct"] > 0, "Long profit should be positive"
