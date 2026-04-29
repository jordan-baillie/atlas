"""Tests for RCA latent #7: place_stops_for_plan double-placement guard.

Verifies that _is_already_protected detects live SELL stop orders at the
broker and causes place_stops_for_plan to skip placement for protected
tickers.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch
import pytest

from brokers.base import OrderResult, OrderStatus, OrderSide


# ─────────────────────────────────────────────────────────────────────────────
# Tests for _is_already_protected standalone function
# ─────────────────────────────────────────────────────────────────────────────

class TestIsAlreadyProtected:
    """Unit tests for brokers.live_executor._is_already_protected."""

    def _make_open_order(self, ticker: str, side: str, order_type: str) -> OrderResult:
        side_enum = OrderSide.SELL if side.lower() == "sell" else OrderSide.BUY
        return OrderResult(
            success=True,
            order_id="oid-111",
            ticker=ticker,
            side=side_enum,
            status=OrderStatus.SUBMITTED,
            raw={
                "order_type": order_type,
                "side": side.lower(),
            },
        )

    def test_returns_true_when_sell_stop_exists(self):
        """Returns True when broker has an open SELL stop for the ticker."""
        from brokers.live_executor import _is_already_protected
        broker = MagicMock()
        broker.get_open_orders.return_value = [
            self._make_open_order("AAPL", "sell", "stop"),
        ]
        assert _is_already_protected(broker, "AAPL") is True

    def test_returns_true_when_sell_stop_limit_exists(self):
        """Returns True for stop_limit orders too."""
        from brokers.live_executor import _is_already_protected
        broker = MagicMock()
        broker.get_open_orders.return_value = [
            self._make_open_order("AAPL", "sell", "stop_limit"),
        ]
        assert _is_already_protected(broker, "AAPL") is True

    def test_returns_true_when_trailing_stop_exists(self):
        """Returns True for trailing_stop orders."""
        from brokers.live_executor import _is_already_protected
        broker = MagicMock()
        broker.get_open_orders.return_value = [
            self._make_open_order("AAPL", "sell", "trailing_stop"),
        ]
        assert _is_already_protected(broker, "AAPL") is True

    def test_returns_false_when_no_sell_stop(self):
        """Returns False when broker has NO open SELL stop for the ticker."""
        from brokers.live_executor import _is_already_protected
        broker = MagicMock()
        broker.get_open_orders.return_value = []
        assert _is_already_protected(broker, "AAPL") is False

    def test_returns_false_when_different_ticker(self):
        """Returns False when the existing stop is for a DIFFERENT ticker."""
        from brokers.live_executor import _is_already_protected
        broker = MagicMock()
        broker.get_open_orders.return_value = [
            self._make_open_order("MSFT", "sell", "stop"),
        ]
        assert _is_already_protected(broker, "AAPL") is False

    def test_returns_false_when_sell_limit_not_stop(self):
        """Returns False for limit sell (TP-only order, no stop protection)."""
        from brokers.live_executor import _is_already_protected
        broker = MagicMock()
        broker.get_open_orders.return_value = [
            self._make_open_order("AAPL", "sell", "limit"),
        ]
        assert _is_already_protected(broker, "AAPL") is False

    def test_returns_false_on_broker_exception(self):
        """Returns False (conservative) when get_open_orders raises."""
        from brokers.live_executor import _is_already_protected
        broker = MagicMock()
        broker.get_open_orders.side_effect = RuntimeError("API timeout")
        # Should not raise, should return False (let placement attempt)
        assert _is_already_protected(broker, "AAPL") is False

    def test_returns_false_when_buy_stop_exists(self):
        """Returns False for BUY stop (irrelevant for sell-side protection)."""
        from brokers.live_executor import _is_already_protected
        broker = MagicMock()
        broker.get_open_orders.return_value = [
            self._make_open_order("AAPL", "buy", "stop"),
        ]
        assert _is_already_protected(broker, "AAPL") is False


# ─────────────────────────────────────────────────────────────────────────────
# Tests for place_stops_for_plan integration
# ─────────────────────────────────────────────────────────────────────────────

class TestPlaceStopsForPlanGuard:
    """Integration tests for the double-placement guard in place_stops_for_plan."""

    def _make_filled_entry_result(self, ticker: str) -> dict:
        return {
            "ticker": ticker,
            "success": True,
            "status": "FILLED",  # must be FILLED to pass the pending-status check
        }

    def _make_plan_entry(self, ticker: str, stop_price: float = 95.0,
                         qty: int = 10, take_profit: float = 110.0) -> dict:
        return {
            "ticker": ticker,
            "position_size": qty,
            "stop_price": stop_price,
            "take_profit": take_profit,
            "strategy": "test_strategy",
            "direction": "long",
            "entry_price": 100.0,
        }

    def _make_executor(self, open_orders: list) -> "LiveExecutor":
        from brokers.live_executor import LiveExecutor
        executor = LiveExecutor.__new__(LiveExecutor)
        executor.config = {
            "market_id": "sp500",
            "strategies": {},
            "trading": {"live_safety": {"dry_run_first": False}},
        }

        mock_broker = MagicMock()
        mock_broker.get_open_orders.return_value = open_orders
        mock_broker.place_order = MagicMock(
            return_value=OrderResult(
                success=True, order_id="new-stop-id", ticker="AAPL",
                side=OrderSide.SELL, status=OrderStatus.SUBMITTED,
            )
        )

        # place_protective_stop and place_take_profit call through to place_order
        mock_broker.place_order_stop = MagicMock(return_value="new-stop-id")
        executor._broker = mock_broker
        executor._connected = True

        # Patch place_protective_stop and place_take_profit on executor
        executor.place_protective_stop = MagicMock(return_value="stop-id-placed")
        executor.place_take_profit = MagicMock(return_value="tp-id-placed")

        return executor

    def _stop_order(self, ticker: str) -> OrderResult:
        return OrderResult(
            success=True,
            order_id="existing-stop-id",
            ticker=ticker,
            side=OrderSide.SELL,
            status=OrderStatus.SUBMITTED,
            raw={"order_type": "stop", "side": "sell"},
        )

    def test_no_placement_when_already_protected(self):
        """place_stops_for_plan skips ticker if SELL stop already exists at broker."""
        ticker = "AAPL"
        executor = self._make_executor(open_orders=[self._stop_order(ticker)])

        plan = {"proposed_entries": [self._make_plan_entry(ticker)]}
        results = [self._make_filled_entry_result(ticker)]
        config = {"market_id": "sp500", "strategies": {}}

        stop_orders = executor.place_stops_for_plan(plan, results, config, "2026-04-29")

        # Neither protective_stop nor take_profit should have been placed
        executor.place_protective_stop.assert_not_called()
        executor.place_take_profit.assert_not_called()
        # The ticker should NOT appear in stop_orders (nothing placed)
        assert ticker not in stop_orders

    def test_placement_proceeds_when_not_protected(self):
        """place_stops_for_plan places stop when broker has NO existing stop."""
        ticker = "AAPL"
        executor = self._make_executor(open_orders=[])  # no existing orders

        plan = {"proposed_entries": [self._make_plan_entry(ticker)]}
        results = [self._make_filled_entry_result(ticker)]
        config = {"market_id": "sp500", "strategies": {}}

        stop_orders = executor.place_stops_for_plan(plan, results, config, "2026-04-29")

        # At least the stop placement should have been attempted
        executor.place_protective_stop.assert_called_once()

    def test_only_protected_ticker_skipped_other_proceeds(self):
        """When AAPL has existing stop but MSFT does not, only MSFT gets placement."""
        executor = self._make_executor(open_orders=[self._stop_order("AAPL")])

        plan = {
            "proposed_entries": [
                self._make_plan_entry("AAPL"),
                self._make_plan_entry("MSFT"),
            ]
        }
        results = [
            self._make_filled_entry_result("AAPL"),
            self._make_filled_entry_result("MSFT"),
        ]
        config = {"market_id": "sp500", "strategies": {}}

        stop_orders = executor.place_stops_for_plan(plan, results, config, "2026-04-29")

        # AAPL was skipped, MSFT was placed
        assert "AAPL" not in stop_orders, "AAPL should be skipped (already protected)"
        # place_protective_stop was called exactly once (for MSFT)
        assert executor.place_protective_stop.call_count == 1
        call_kwargs = executor.place_protective_stop.call_args
        assert call_kwargs.kwargs.get("ticker") == "MSFT" or (
            call_kwargs.args and call_kwargs.args[0] == "MSFT"
        )

    def test_broker_none_guard_skips_is_already_protected(self):
        """When _broker is None, _is_already_protected guard is skipped (placement proceeds)."""
        from brokers.live_executor import LiveExecutor
        executor = LiveExecutor.__new__(LiveExecutor)
        executor.config = {
            "market_id": "sp500",
            "strategies": {},
            "trading": {"live_safety": {"dry_run_first": False}},
        }
        executor._broker = None
        executor._connected = False
        executor.place_protective_stop = MagicMock(return_value="stop-id")
        executor.place_take_profit = MagicMock(return_value="tp-id")

        ticker = "AAPL"
        plan = {"proposed_entries": [self._make_plan_entry(ticker)]}
        results = [self._make_filled_entry_result(ticker)]
        config = {"market_id": "sp500", "strategies": {}}

        # Should not raise — just proceeds as if not protected
        stop_orders = executor.place_stops_for_plan(plan, results, config, "2026-04-29")
        # With _broker=None, the guard is skipped and placement proceeds
        executor.place_protective_stop.assert_called_once()
