"""Unit tests for the AlpacaBroker adapter.

All tests use mocked alpaca-py clients — no real API calls.

Run:
    cd /root/atlas && python3 -m pytest tests/test_alpaca_broker.py -v
"""
from __future__ import annotations

import os
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from atlas.brokers.alpaca.broker import (
    AlpacaBroker, _map_order_status, _map_side, _map_tif,
    _order_to_result, _STATUS_MAP,
)
from atlas.brokers.alpaca.mapper import to_alpaca, to_atlas, to_alpaca_list, to_atlas_list
from atlas.brokers.base import (
    AccountInfo, OrderResult, OrderSide, OrderStatus, OrderType, PositionInfo,
)


# ── Fixtures ──────────────────────────────────────────────────

def _cfg(paper=True):
    return {
        "market": "sp500",
        "trading": {"broker": "alpaca", "live_enabled": True},
        "risk": {"starting_equity": 4000},
        "alpaca": {"paper": paper, "feed": "iex", "tif": "day"},
    }


def _mock_account(**overrides):
    defaults = dict(
        equity="7500.00", cash="3500.00", buying_power="7000.00",
        portfolio_value="4000.00", status="ACTIVE",
        account_blocked=False, trading_blocked=False,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _mock_position(symbol="AAPL", qty="10", avg_entry_price="150.00",
                   current_price="155.00", unrealized_pl="50.00",
                   unrealized_plpc="0.0333", market_value="1550.00",
                   side="long", cost_basis="1500.00"):
    return SimpleNamespace(
        symbol=symbol, qty=qty, avg_entry_price=avg_entry_price,
        current_price=current_price, unrealized_pl=unrealized_pl,
        unrealized_plpc=unrealized_plpc, market_value=market_value,
        side=side, cost_basis=cost_basis,
    )


def _mock_order(id="abc-123", status="filled", symbol="AAPL",
                qty="10", filled_qty="10", filled_avg_price="150.00",
                limit_price=None, stop_price=None,
                client_order_id="atlas-1", filled_at="2026-03-04",
                submitted_at="2026-03-04"):
    s = SimpleNamespace(value=status) if isinstance(status, str) else status
    return SimpleNamespace(
        id=id, status=s, symbol=symbol, qty=qty,
        filled_qty=filled_qty, filled_avg_price=filled_avg_price,
        limit_price=limit_price, stop_price=stop_price,
        client_order_id=client_order_id,
        filled_at=filled_at, submitted_at=submitted_at,
    )


# ── Mapper Tests ──────────────────────────────────────────────

class TestMapper:
    def test_to_alpaca_passthrough(self):
        assert to_alpaca("AAPL") == "AAPL"

    def test_to_alpaca_strips_ax(self):
        assert to_alpaca("BHP.AX") == "BHP"

    def test_to_alpaca_strips_hk(self):
        assert to_alpaca("0700.HK") == "0700"

    def test_to_atlas_passthrough(self):
        assert to_atlas("MSFT") == "MSFT"

    def test_roundtrip(self):
        assert to_atlas(to_alpaca("TSLA")) == "TSLA"

    def test_to_alpaca_list(self):
        result = to_alpaca_list(["AAPL", "BHP.AX", "MSFT"])
        assert result == ["AAPL", "BHP", "MSFT"]

    def test_to_atlas_list(self):
        result = to_atlas_list(["AAPL", "MSFT"])
        assert result == ["AAPL", "MSFT"]


# ── Status Mapping Tests ─────────────────────────────────────

class TestStatusMapping:
    def test_filled(self):
        assert _map_order_status("filled") == OrderStatus.FILLED

    def test_new_is_submitted(self):
        assert _map_order_status("new") == OrderStatus.SUBMITTED

    def test_partially_filled(self):
        assert _map_order_status("partially_filled") == OrderStatus.PARTIAL_FILLED

    def test_canceled(self):
        assert _map_order_status("canceled") == OrderStatus.CANCELLED

    def test_rejected(self):
        assert _map_order_status("rejected") == OrderStatus.FAILED

    def test_pending_new(self):
        assert _map_order_status("pending_new") == OrderStatus.PENDING

    def test_unknown(self):
        assert _map_order_status("nonsense") == OrderStatus.UNKNOWN

    def test_all_map_entries_valid(self):
        for key, val in _STATUS_MAP.items():
            assert isinstance(val, OrderStatus)


# ── Side Mapping Tests ────────────────────────────────────────

class TestSideMapping:
    def test_buy(self):
        result = _map_side(OrderSide.BUY)
        assert result is not None  # Returns AlpacaSide.BUY

    def test_sell(self):
        result = _map_side(OrderSide.SELL)
        assert result is not None  # Returns AlpacaSide.SELL


# ── TIF Mapping Tests ─────────────────────────────────────────

class TestTifMapping:
    def test_day(self):
        result = _map_tif("day")
        assert result is not None

    def test_gtc(self):
        result = _map_tif("gtc")
        assert result is not None

    def test_unknown_defaults_day(self):
        result = _map_tif("xxx")
        assert result is not None  # defaults to DAY


# ── Order To Result Tests ─────────────────────────────────────

class TestOrderToResult:
    def test_filled_order(self):
        order = _mock_order(status="filled", filled_qty="10", filled_avg_price="155.00")
        result = _order_to_result(order, "AAPL", OrderSide.BUY)
        assert result.success is True
        assert result.order_id == "abc-123"
        assert result.ticker == "AAPL"
        assert result.side == OrderSide.BUY
        assert result.status == OrderStatus.FILLED
        assert result.filled_qty == 10
        assert result.fill_price == 155.00

    def test_pending_order(self):
        order = _mock_order(status="pending_new", filled_qty="0", filled_avg_price=None)
        result = _order_to_result(order, "MSFT", OrderSide.SELL)
        assert result.status == OrderStatus.PENDING
        assert result.filled_qty == 0

    def test_limit_price_as_requested(self):
        order = _mock_order(limit_price="200.00", stop_price=None)
        result = _order_to_result(order, "AAPL", OrderSide.BUY)
        assert result.requested_price == 200.00


# ── AlpacaBroker Init Tests ───────────────────────────────────

class TestAlpacaBrokerInit:
    def test_paper_mode_default(self):
        broker = AlpacaBroker(_cfg(paper=True))
        assert broker.is_live is False
        assert "PAPER" in broker.name

    def test_live_mode(self):
        broker = AlpacaBroker(_cfg(paper=False), live=True)
        assert broker.is_live is True
        assert "LIVE" in broker.name

    def test_market_id(self):
        broker = AlpacaBroker(_cfg())
        assert broker.market_id == "sp500"

    def test_not_connected_initially(self):
        broker = AlpacaBroker(_cfg())
        assert broker._connected is False


# ── Connect/Disconnect Tests ─────────────────────────────────

class TestConnect:
    @patch("atlas.brokers.alpaca.broker.get_secret")
    @patch("atlas.brokers.alpaca.broker.TradingClient")
    @patch("atlas.brokers.alpaca.broker.AlpacaMarketData")
    def test_connect_success(self, mock_md, mock_tc_cls, mock_secret):
        mock_secret.side_effect = lambda k, **kw: "test-key" if "API_KEY" in k else "test-secret"
        mock_client = MagicMock()
        mock_client.get_account.return_value = _mock_account()
        mock_tc_cls.return_value = mock_client
        mock_md.return_value = MagicMock()

        broker = AlpacaBroker(_cfg())
        assert broker.connect() is True
        assert broker._connected is True

    @patch("atlas.brokers.alpaca.broker.get_secret", return_value=None)
    def test_connect_fails_no_key(self, mock_secret):
        broker = AlpacaBroker(_cfg())
        assert broker.connect() is False

    @patch("atlas.brokers.alpaca.broker.get_secret")
    @patch("atlas.brokers.alpaca.broker.TradingClient")
    @patch("atlas.brokers.alpaca.broker.AlpacaMarketData")
    def test_disconnect(self, mock_md, mock_tc_cls, mock_secret):
        mock_secret.side_effect = lambda k, **kw: "key"
        mock_client = MagicMock()
        mock_client.get_account.return_value = _mock_account()
        mock_tc_cls.return_value = mock_client
        mock_md.return_value = MagicMock()

        broker = AlpacaBroker(_cfg())
        broker.connect()
        broker.disconnect()
        assert broker._connected is False
        assert broker._trade_client is None


# ── Account Info Tests ────────────────────────────────────────

class TestGetAccountInfo:
    def _connected_broker(self):
        broker = AlpacaBroker(_cfg())
        broker._connected = True
        broker._trade_client = MagicMock()
        broker._market_data = MagicMock()
        return broker

    def test_returns_account_info(self):
        broker = self._connected_broker()
        broker._trade_client.get_account.return_value = _mock_account(
            equity="8000.00", cash="4000.00", buying_power="6000.00",
        )
        info = broker.get_account_info()
        assert isinstance(info, AccountInfo)
        assert info.equity == 8000.00
        assert info.cash == 4000.00
        assert info.buying_power == 6000.00

    def test_blocked_account(self):
        broker = self._connected_broker()
        broker._trade_client.get_account.return_value = _mock_account(
            trading_blocked=True
        )
        info = broker.get_account_info()
        assert isinstance(info, AccountInfo)

    def test_api_error_returns_empty(self):
        broker = self._connected_broker()
        broker._trade_client.get_account.side_effect = Exception("API error")
        info = broker.get_account_info()
        assert isinstance(info, AccountInfo)
        assert info.equity == 0


# ── Positions Tests ───────────────────────────────────────────

class TestGetPositions:
    def _connected_broker(self):
        broker = AlpacaBroker(_cfg())
        broker._connected = True
        broker._trade_client = MagicMock()
        broker._market_data = MagicMock()
        return broker

    def test_returns_positions(self):
        broker = self._connected_broker()
        broker._trade_client.get_all_positions.return_value = [
            _mock_position("AAPL", "10", "150.00", "155.00", "50.00"),
            _mock_position("MSFT", "5", "300.00", "310.00", "50.00"),
        ]
        positions = broker.get_positions()
        assert len(positions) == 2
        assert positions[0].ticker == "AAPL"
        assert positions[0].shares == 10
        assert positions[1].ticker == "MSFT"

    def test_zero_qty_filtered(self):
        broker = self._connected_broker()
        broker._trade_client.get_all_positions.return_value = [
            _mock_position("AAPL", "0"),
        ]
        positions = broker.get_positions()
        assert len(positions) == 0

    def test_api_error_returns_empty(self):
        broker = self._connected_broker()
        broker._trade_client.get_all_positions.side_effect = Exception("fail")
        positions = broker.get_positions()
        assert positions == []


# ── Place Order Tests ─────────────────────────────────────────

class TestPlaceOrder:
    def _connected_broker(self):
        broker = AlpacaBroker(_cfg())
        broker._connected = True
        broker._trade_client = MagicMock()
        broker._market_data = MagicMock()
        return broker

    def test_market_order(self):
        broker = self._connected_broker()
        broker._trade_client.submit_order.return_value = _mock_order(
            status="new", filled_qty="0"
        )
        result = broker.place_order("AAPL", OrderSide.BUY, 10, 0.0, OrderType.MARKET)
        assert result.success is True
        broker._trade_client.submit_order.assert_called_once()

    def test_limit_order(self):
        broker = self._connected_broker()
        broker._trade_client.submit_order.return_value = _mock_order(
            status="new", limit_price="150.00"
        )
        result = broker.place_order("AAPL", OrderSide.BUY, 10, 150.00, OrderType.LIMIT)
        assert result.success is True

    def test_stop_order(self):
        broker = self._connected_broker()
        broker._trade_client.submit_order.return_value = _mock_order(
            status="new", stop_price="140.00"
        )
        result = broker.place_order("AAPL", OrderSide.SELL, 10, 0.0, OrderType.STOP, stop_price=140.00)
        assert result.success is True

    def test_api_error(self):
        broker = self._connected_broker()
        broker._trade_client.submit_order.side_effect = Exception("rejected")
        result = broker.place_order("AAPL", OrderSide.BUY, 10, 0.0, OrderType.MARKET)
        assert result.success is False


# ── Cancel Order Tests ────────────────────────────────────────

class TestCancelOrder:
    def _connected_broker(self):
        broker = AlpacaBroker(_cfg())
        broker._connected = True
        broker._trade_client = MagicMock()
        broker._market_data = MagicMock()
        return broker

    def test_cancel_success(self):
        broker = self._connected_broker()
        broker._trade_client.cancel_order_by_id.return_value = None
        result = broker.cancel_order("order-123")
        assert isinstance(result, OrderResult)
        assert result.success is True
        assert result.status == OrderStatus.CANCELLED

    def test_cancel_failure(self):
        broker = self._connected_broker()
        broker._trade_client.cancel_order_by_id.side_effect = Exception("not found")
        result = broker.cancel_order("order-123")
        assert isinstance(result, OrderResult)
        assert result.success is False


# ── Registry Tests ────────────────────────────────────────────

class TestRegistry:
    def test_alpaca_in_registry(self):
        from atlas.brokers.registry import get_broker
        cfg = _cfg()
        broker = get_broker("sp500", cfg)
        assert isinstance(broker, AlpacaBroker)

    def test_none_when_disabled(self):
        from atlas.brokers.registry import get_broker
        cfg = _cfg()
        cfg["trading"]["live_enabled"] = False
        broker = get_broker("sp500", cfg)
        assert broker is None
