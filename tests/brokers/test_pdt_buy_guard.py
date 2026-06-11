"""Regression tests for PDT pre-submit guard on BUY orders.

Covers the 2026-05-11 FTNT incident: a BUY order was rejected by Alpaca
(code 40310100) because the account had 3 day-trades in the rolling
5-business-day window. Local PDT guard at brokers/alpaca/broker.py:638
only fired for SELL orders — BUY bypassed pre-check.

This test suite verifies:
1. Ticker-level PDT deferral now blocks BOTH BUY and SELL submits
2. Account-level pre-check blocks BUY when equity<$25k AND daytrade_count>=3
3. Healthy account (equity>$25k OR daytrade_count<3) allows BUY normally
4. Failed account fetch fails-open (don't break trading on API hiccup)
5. After a BUY pre-empt, ticker is marked deferred so next cycle also skips
"""
from __future__ import annotations
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from atlas.brokers.base import OrderSide, OrderType


def _make_broker():
    """Construct AlpacaBroker bypassing __init__ (no API keys required)."""
    from atlas.brokers.alpaca.broker import AlpacaBroker
    broker = object.__new__(AlpacaBroker)
    broker._trade_client = MagicMock()
    broker._tif = "gtc"
    broker._live = False
    broker._paper = True
    broker._order_map = {}
    return broker


def _account(equity: float, daytrade_count: int, pdt: bool = False) -> SimpleNamespace:
    return SimpleNamespace(
        equity=equity,
        daytrade_count=daytrade_count,
        pattern_day_trader=pdt,
    )


class TestPDTBuyGuard:
    """Ticker-level deferral now applies to BOTH sides."""

    def test_buy_blocked_when_ticker_deferred(self):
        """BUY of a PDT-deferred ticker is blocked pre-submit (no submit_order call)."""
        broker = _make_broker()
        broker._trade_client.submit_order.side_effect = AssertionError(
            "submit_order must NOT be called for deferred ticker"
        )

        with patch("atlas.brokers.alpaca.broker._is_pdt_deferred_new", return_value=True), \
             patch.object(broker, "_require_connected"):
            result = broker.place_order(
                "FTNT", OrderSide.BUY, 4, 114.07,
                OrderType.LIMIT, tif="day",
            )

        assert result.success is False
        assert "pdt_deferred" in result.message.lower() or "40310100" in result.message

    def test_sell_blocked_when_ticker_deferred(self):
        """SELL of a deferred ticker still blocked (regression for original AVGO path)."""
        broker = _make_broker()
        broker._trade_client.submit_order.side_effect = AssertionError(
            "submit_order must NOT be called for deferred ticker"
        )

        with patch("atlas.brokers.alpaca.broker._is_pdt_deferred_new", return_value=True), \
             patch.object(broker, "_require_connected"):
            result = broker.place_order(
                "AVGO", OrderSide.SELL, 1, 0.0,
                OrderType.STOP, stop_price=400.0, tif="gtc",
            )

        assert result.success is False


class TestPDTAccountLevelPreempt:
    """Account-level pre-check blocks BUY when daytrade_count + equity would trigger PDT."""

    def test_pdt_status_blocks_when_sub25k_and_3_daytrades(self):
        """get_pdt_status returns blocked=True when equity<$25k and daytrade_count>=3."""
        broker = _make_broker()
        broker._trade_client.get_account.return_value = _account(equity=5237.0, daytrade_count=3)
        status = broker.get_pdt_status()
        assert status["blocked"] is True
        assert status["daytrade_count"] == 3
        assert status["equity"] == 5237.0
        assert "5237" in status["reason"]
        assert "3" in status["reason"]

    def test_pdt_status_blocks_when_4_daytrades(self):
        """Even higher daytrade_count is blocked (5+ also returns True)."""
        broker = _make_broker()
        broker._trade_client.get_account.return_value = _account(equity=10000.0, daytrade_count=5)
        status = broker.get_pdt_status()
        assert status["blocked"] is True

    def test_pdt_status_allows_above_25k(self):
        """Account with equity>=$25k is never PDT-blocked regardless of daytrade count."""
        broker = _make_broker()
        broker._trade_client.get_account.return_value = _account(equity=30000.0, daytrade_count=10)
        status = broker.get_pdt_status()
        assert status["blocked"] is False
        assert status["daytrade_count"] == 10

    def test_pdt_status_allows_low_daytrade_count(self):
        """Sub-$25k account with daytrade_count<3 still allowed."""
        broker = _make_broker()
        broker._trade_client.get_account.return_value = _account(equity=5237.0, daytrade_count=2)
        status = broker.get_pdt_status()
        assert status["blocked"] is False

    def test_pdt_status_fails_open_on_api_error(self):
        """Account fetch failure → blocked=False (don't break trading on hiccup)."""
        broker = _make_broker()
        broker._trade_client.get_account.side_effect = Exception("network timeout")
        status = broker.get_pdt_status()
        assert status["blocked"] is False
        assert "account_fetch_failed" in status["reason"]

    def test_place_order_buy_blocked_by_account_preempt(self):
        """BUY rejected pre-submit when get_pdt_status returns blocked=True."""
        broker = _make_broker()
        # First call: ticker not deferred. Second call (inside _set_pdt_deferred_new): N/A.
        broker._trade_client.get_account.return_value = _account(equity=5237.0, daytrade_count=3)
        broker._trade_client.submit_order.side_effect = AssertionError(
            "submit_order must NOT be called when pre-empt fires"
        )

        with patch("atlas.brokers.alpaca.broker._is_pdt_deferred_new", return_value=False), \
             patch("atlas.brokers.alpaca.broker._set_pdt_deferred_new") as mock_defer, \
             patch.object(broker, "_require_connected"):
            result = broker.place_order(
                "FTNT", OrderSide.BUY, 4, 114.07,
                OrderType.LIMIT, tif="day",
            )

        assert result.success is False
        assert "pdt_preempt" in result.message
        # After pre-empt, ticker should be marked deferred so next cycle skips
        mock_defer.assert_called_once()
        assert mock_defer.call_args[0][0] == "FTNT"

    def test_place_order_buy_succeeds_when_account_healthy(self):
        """BUY proceeds normally when account-level check is clean."""
        broker = _make_broker()
        broker._trade_client.get_account.return_value = _account(equity=30000.0, daytrade_count=1)

        fake_order = MagicMock()
        fake_order.id = "fake-order-id"
        fake_order.status = "accepted"
        fake_order.symbol = "FTNT"
        fake_order.filled_qty = "0"
        fake_order.filled_avg_price = None
        fake_order.qty = "4"
        broker._trade_client.submit_order.return_value = fake_order

        with patch("atlas.brokers.alpaca.broker._is_pdt_deferred_new", return_value=False), \
             patch.object(broker, "_require_connected"):
            result = broker.place_order(
                "FTNT", OrderSide.BUY, 4, 114.07,
                OrderType.LIMIT, tif="day",
            )

        # submit_order WAS called (no pre-empt block)
        broker._trade_client.submit_order.assert_called_once()

    def test_sell_skips_account_preempt(self):
        """SELL orders skip the account-level pre-check (only ticker-level applies)."""
        broker = _make_broker()
        # Even with blocking account state, SELL bypasses get_pdt_status check
        broker._trade_client.get_account.return_value = _account(equity=5237.0, daytrade_count=3)

        fake_order = MagicMock()
        fake_order.id = "fake-sell-id"
        fake_order.status = "accepted"
        fake_order.symbol = "FTNT"
        fake_order.filled_qty = "0"
        fake_order.filled_avg_price = None
        fake_order.qty = "1"
        broker._trade_client.submit_order.return_value = fake_order

        with patch("atlas.brokers.alpaca.broker._is_pdt_deferred_new", return_value=False), \
             patch.object(broker, "_require_connected"):
            result = broker.place_order(
                "FTNT", OrderSide.SELL, 1, 0.0,
                OrderType.STOP, stop_price=100.0, tif="gtc",
            )

        # submit_order WAS called (SELL is not subject to opening-BUY pre-empt)
        broker._trade_client.submit_order.assert_called_once()


class TestRegressionExistingPDTBackoff:
    """Verify existing test patterns in test_pdt_backoff_avgo_ccj.py still pass logically."""

    def test_pdt_denial_records_deferred_state(self):
        """PDT 40310100 error → set_pdt_deferred called (existing behavior preserved)."""
        broker = _make_broker()
        broker._trade_client.submit_order.side_effect = Exception(
            '{"code":40310100,"message":"trade denied due to pattern day trading protection"}'
        )

        with patch("atlas.brokers.alpaca.broker._is_pdt_deferred_new", return_value=False), \
             patch("atlas.brokers.alpaca.broker._set_pdt_deferred_new") as mock_set, \
             patch.object(broker, "_require_connected"):
            # Use SELL to skip the BUY account-level pre-check
            result = broker.place_order(
                "FTNT", OrderSide.SELL, 1, 0.0,
                OrderType.STOP, stop_price=100.0, tif="gtc",
            )

        assert result.success is False
        mock_set.assert_called_once()
        assert mock_set.call_args[0][0] == "FTNT"
