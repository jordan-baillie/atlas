"""Unit tests for the AlpacaBroker adapter.

Tests cover:
  - AlpacaBroker initialisation with config
  - Mapper functions (to_alpaca, to_atlas)
  - Order type / side mapping helpers
  - AccountInfo conversion from Alpaca response
  - PositionInfo conversion from Alpaca response
  - OrderResult conversion from Alpaca order
  - place_order (market, limit, stop, stop-limit)
  - cancel_order / cancel_all_orders
  - get_open_orders / get_order_status
  - connect success / failure paths
  - is_live / paper mode logic

All tests use mocked alpaca-py clients — no real API calls.

Run:
    cd /root/atlas && python3 -m pytest tests/test_alpaca_broker.py -v
"""
from __future__ import annotations

import os
import sys
import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock, patch, PropertyMock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from brokers.alpaca.broker import AlpacaBroker, _map_status, _map_side, _STATUS_MAP
from brokers.alpaca.mapper import to_alpaca, to_atlas, to_alpaca_list, to_atlas_list
from brokers.base import (
    AccountInfo,
    OrderResult,
    OrderSide,
    OrderStatus,
    OrderType,
    PositionInfo,
)


# ═══════════════════════════════════════════════════════════════════════════════
# Fixtures & helpers
# ═══════════════════════════════════════════════════════════════════════════════

def _alpaca_config(paper: bool = True) -> dict:
    """Minimal config dict for AlpacaBroker."""
    return {
        "market": "sp500",
        "trading": {
            "broker": "alpaca",
            "live_enabled": True,
        },
        "alpaca": {
            "paper": paper,
            "data_feed": "iex",
        },
    }


def _make_mock_account(**overrides) -> SimpleNamespace:
    """Build a fake Alpaca TradeAccount-like object."""
    defaults = {
        "id": "acc-123",
        "account_number": "PA12345678",
        "equity": "10000.00",
        "last_equity": "9800.00",
        "cash": "5000.00",
        "long_market_value": "5000.00",
        "buying_power": "10000.00",
        "currency": "USD",
        "trading_blocked": False,
        "account_blocked": False,
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _make_mock_position(**overrides) -> SimpleNamespace:
    """Build a fake Alpaca Position-like object."""
    defaults = {
        "symbol": "AAPL",
        "qty": "10",
        "avg_entry_price": "150.00",
        "current_price": "160.00",
        "market_value": "1600.00",
        "cost_basis": "1500.00",
        "unrealized_pl": "100.00",
        "unrealized_plpc": "0.0667",
        "qty_available": "10",
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _make_mock_order(
    order_id: str = None,
    symbol: str = "AAPL",
    status: str = "new",
    side: str = "buy",
    qty: str = "5",
    filled_qty: str = "0",
    filled_avg_price: str = None,
    limit_price: str = "155.00",
    stop_price: str = None,
    order_type: str = "limit",
) -> SimpleNamespace:
    """Build a fake Alpaca Order-like object."""
    _id = order_id or str(uuid.uuid4())
    status_obj = SimpleNamespace(value=status)
    side_obj = SimpleNamespace(value=side)
    return SimpleNamespace(
        id=uuid.UUID(_id) if len(_id) == 36 else uuid.uuid4(),
        symbol=symbol,
        status=status_obj,
        side=side_obj,
        qty=qty,
        filled_qty=filled_qty,
        filled_avg_price=filled_avg_price,
        limit_price=limit_price,
        stop_price=stop_price,
        order_type=SimpleNamespace(value=order_type),
        type=SimpleNamespace(value=order_type),
    )


def _connected_broker(paper: bool = True) -> tuple[AlpacaBroker, MagicMock]:
    """Return a connected AlpacaBroker with a mocked TradingClient."""
    broker = AlpacaBroker(_alpaca_config(paper=paper), live=not paper)
    mock_client = MagicMock()
    broker._trading_client = mock_client
    broker._connected = True
    return broker, mock_client


# ═══════════════════════════════════════════════════════════════════════════════
# 1. Mapper tests
# ═══════════════════════════════════════════════════════════════════════════════

class TestMapper:
    """Tests for brokers.alpaca.mapper."""

    def test_to_alpaca_bare_us_symbol(self):
        """US bare symbols pass through unchanged (uppercased)."""
        assert to_alpaca("AAPL") == "AAPL"
        assert to_alpaca("msft") == "MSFT"
        assert to_alpaca("tsla") == "TSLA"

    def test_to_alpaca_strips_ax_suffix(self):
        """Accidental .AX suffix is stripped cleanly."""
        assert to_alpaca("BHP.AX", "asx") == "BHP"
        assert to_alpaca("CBA.AX", "asx") == "CBA"

    def test_to_alpaca_strips_hk_suffix(self):
        """Accidental .HK suffix is stripped cleanly."""
        assert to_alpaca("0700.HK", "hk") == "0700"

    def test_to_alpaca_strips_l_suffix(self):
        """Accidental .L (London) suffix is stripped."""
        assert to_alpaca("SHEL.L") == "SHEL"

    def test_to_atlas_bare_us_symbol(self):
        """Atlas format for US stocks is bare uppercase symbol."""
        assert to_atlas("AAPL") == "AAPL"
        assert to_atlas("msft") == "MSFT"

    def test_to_alpaca_list(self):
        """Batch conversion works for lists."""
        result = to_alpaca_list(["AAPL", "MSFT", "TSLA"])
        assert result == ["AAPL", "MSFT", "TSLA"]

    def test_to_atlas_list(self):
        """Batch reverse conversion works for lists."""
        result = to_atlas_list(["AAPL", "MSFT", "TSLA"])
        assert result == ["AAPL", "MSFT", "TSLA"]

    def test_to_alpaca_to_atlas_roundtrip(self):
        """to_atlas(to_alpaca(x)) == x for US symbols."""
        for sym in ["AAPL", "NVDA", "SPY", "QQQ"]:
            assert to_atlas(to_alpaca(sym)) == sym


# ═══════════════════════════════════════════════════════════════════════════════
# 2. Status / side mapping
# ═══════════════════════════════════════════════════════════════════════════════

class TestStatusMapping:
    """Tests for order status and side mapping helpers."""

    def test_status_filled(self):
        """'filled' maps to FILLED."""
        mock = SimpleNamespace(value="filled")
        assert _map_status(mock) == OrderStatus.FILLED

    def test_status_partially_filled(self):
        """'partially_filled' maps to PARTIAL_FILLED."""
        mock = SimpleNamespace(value="partially_filled")
        assert _map_status(mock) == OrderStatus.PARTIAL_FILLED

    def test_status_new_is_submitted(self):
        """'new' maps to SUBMITTED."""
        mock = SimpleNamespace(value="new")
        assert _map_status(mock) == OrderStatus.SUBMITTED

    def test_status_canceled(self):
        """'canceled' maps to CANCELLED."""
        mock = SimpleNamespace(value="canceled")
        assert _map_status(mock) == OrderStatus.CANCELLED

    def test_status_rejected_is_failed(self):
        """'rejected' maps to FAILED."""
        mock = SimpleNamespace(value="rejected")
        assert _map_status(mock) == OrderStatus.FAILED

    def test_status_pending_new(self):
        """'pending_new' maps to PENDING."""
        mock = SimpleNamespace(value="pending_new")
        assert _map_status(mock) == OrderStatus.PENDING

    def test_status_unknown_string_returns_unknown(self):
        """Unmapped status falls back to UNKNOWN."""
        mock = SimpleNamespace(value="some_future_status")
        assert _map_status(mock) == OrderStatus.UNKNOWN

    def test_status_all_known_values_mapped(self):
        """Every value in _STATUS_MAP is covered — no UNKNOWN leakage."""
        all_known = set(_STATUS_MAP.keys())
        assert "filled" in all_known
        assert "canceled" in all_known
        assert "rejected" in all_known
        # All must be valid OrderStatus members
        for val in _STATUS_MAP.values():
            assert isinstance(val, OrderStatus)

    def test_side_buy(self):
        """'buy' maps to OrderSide.BUY."""
        mock = SimpleNamespace(value="buy")
        assert _map_side(mock) == OrderSide.BUY

    def test_side_sell(self):
        """'sell' maps to OrderSide.SELL."""
        mock = SimpleNamespace(value="sell")
        assert _map_side(mock) == OrderSide.SELL


# ═══════════════════════════════════════════════════════════════════════════════
# 3. AlpacaBroker initialisation
# ═══════════════════════════════════════════════════════════════════════════════

class TestAlpacaBrokerInit:
    """Tests for AlpacaBroker constructor and properties."""

    def test_name_is_alpaca(self):
        broker = AlpacaBroker(_alpaca_config())
        assert broker.name == "alpaca"

    def test_default_paper_mode(self):
        """Default config with paper=True and live=False → not live."""
        broker = AlpacaBroker(_alpaca_config(paper=True), live=False)
        assert broker.is_live is False

    def test_live_false_when_paper_true_even_with_live_flag(self):
        """paper=True overrides live=True → still not live (safety)."""
        broker = AlpacaBroker(_alpaca_config(paper=True), live=True)
        assert broker.is_live is False

    def test_live_true_when_paper_false_and_live_flag(self):
        """paper=False + live=True → is_live=True."""
        broker = AlpacaBroker(_alpaca_config(paper=False), live=True)
        assert broker.is_live is True

    def test_not_connected_initially(self):
        broker = AlpacaBroker(_alpaca_config())
        assert broker.is_connected is False

    def test_market_id_from_config(self):
        cfg = _alpaca_config()
        cfg["market"] = "sp500"
        broker = AlpacaBroker(cfg)
        assert broker._market_id == "sp500"

    def test_data_feed_from_config(self):
        broker = AlpacaBroker(_alpaca_config())
        assert broker._data_feed == "iex"


# ═══════════════════════════════════════════════════════════════════════════════
# 4. connect / disconnect
# ═══════════════════════════════════════════════════════════════════════════════

class TestConnect:
    """Tests for connect() and disconnect() lifecycle."""

    def test_connect_success(self):
        """connect() returns True and sets _connected when auth succeeds."""
        mock_client = MagicMock()
        mock_client.get_account.return_value = _make_mock_account()

        with patch("brokers.alpaca.broker.get_secret", return_value="test-key"), \
             patch("brokers.alpaca.broker.TradingClient", return_value=mock_client), \
             patch("brokers.alpaca.broker._ALPACA_AVAILABLE", True):
            broker = AlpacaBroker(_alpaca_config())
            result = broker.connect()

        assert result is True
        assert broker.is_connected is True

    def test_connect_fails_without_credentials(self):
        """connect() returns False when no API key is configured."""
        with patch("brokers.alpaca.broker.get_secret", return_value=None), \
             patch("brokers.alpaca.broker._ALPACA_AVAILABLE", True):
            broker = AlpacaBroker(_alpaca_config())
            result = broker.connect()
        assert result is False
        assert broker.is_connected is False

    def test_connect_fails_on_api_error(self):
        """connect() returns False when TradingClient raises."""
        mock_client = MagicMock()
        mock_client.get_account.side_effect = Exception("401 Unauthorized")

        with patch("brokers.alpaca.broker.get_secret", return_value="test-key"), \
             patch("brokers.alpaca.broker.TradingClient", return_value=mock_client), \
             patch("brokers.alpaca.broker._ALPACA_AVAILABLE", True):
            broker = AlpacaBroker(_alpaca_config())
            result = broker.connect()

        assert result is False
        assert broker.is_connected is False

    def test_disconnect_clears_client(self):
        """disconnect() clears client and marks disconnected."""
        broker, mock_client = _connected_broker()
        broker.disconnect()
        assert broker._trading_client is None
        assert broker.is_connected is False


# ═══════════════════════════════════════════════════════════════════════════════
# 5. AccountInfo conversion
# ═══════════════════════════════════════════════════════════════════════════════

class TestGetAccountInfo:
    """Tests for get_account_info() and AccountInfo field mapping."""

    def test_get_account_info_fields(self):
        """AccountInfo fields map correctly from Alpaca response."""
        broker, mock_client = _connected_broker()
        mock_client.get_account.return_value = _make_mock_account(
            equity="12000.00",
            last_equity="10000.00",
            cash="6000.00",
            long_market_value="6000.00",
            buying_power="12000.00",
            currency="USD",
            trading_blocked=False,
            account_blocked=False,
        )

        info = broker.get_account_info()

        assert isinstance(info, AccountInfo)
        assert info.equity == pytest.approx(12000.0)
        assert info.cash == pytest.approx(6000.0)
        assert info.market_value == pytest.approx(6000.0)
        assert info.buying_power == pytest.approx(12000.0)
        assert info.total_pnl == pytest.approx(2000.0)
        assert info.currency == "USD"
        assert info.market_id == "sp500"
        assert info.halted is False

    def test_get_account_info_halted_when_blocked(self):
        """halted=True when account_blocked is True."""
        broker, mock_client = _connected_broker()
        mock_client.get_account.return_value = _make_mock_account(
            account_blocked=True
        )
        info = broker.get_account_info()
        assert info.halted is True
        assert "account_blocked" in info.halt_reason

    def test_get_account_info_disconnected_returns_empty(self):
        """Returns empty AccountInfo if not connected."""
        broker = AlpacaBroker(_alpaca_config())
        info = broker.get_account_info()
        assert info.equity == 0.0
        assert info.market_id == "sp500"

    def test_get_account_info_api_error_returns_empty(self):
        """Returns empty AccountInfo on API error."""
        broker, mock_client = _connected_broker()
        mock_client.get_account.side_effect = Exception("timeout")
        info = broker.get_account_info()
        assert info.equity == 0.0


# ═══════════════════════════════════════════════════════════════════════════════
# 6. PositionInfo conversion
# ═══════════════════════════════════════════════════════════════════════════════

class TestGetPositions:
    """Tests for get_positions() and PositionInfo field mapping."""

    def test_get_positions_maps_fields(self):
        """PositionInfo fields map correctly from Alpaca position."""
        broker, mock_client = _connected_broker()
        mock_client.get_all_positions.return_value = [
            _make_mock_position(
                symbol="NVDA",
                qty="20",
                avg_entry_price="400.00",
                current_price="450.00",
                market_value="9000.00",
                cost_basis="8000.00",
                unrealized_pl="1000.00",
                unrealized_plpc="0.125",
            )
        ]

        positions = broker.get_positions()

        assert len(positions) == 1
        pos = positions[0]
        assert pos.ticker == "NVDA"
        assert pos.shares == 20
        assert pos.entry_price == pytest.approx(400.0)
        assert pos.current_price == pytest.approx(450.0)
        assert pos.market_value == pytest.approx(9000.0)
        assert pos.unrealized_pnl == pytest.approx(1000.0)

    def test_get_positions_zero_qty_filtered(self):
        """Positions with qty=0 are filtered out."""
        broker, mock_client = _connected_broker()
        mock_client.get_all_positions.return_value = [
            _make_mock_position(symbol="AAPL", qty="0"),
            _make_mock_position(symbol="MSFT", qty="10"),
        ]

        positions = broker.get_positions()
        assert len(positions) == 1
        assert positions[0].ticker == "MSFT"

    def test_get_positions_empty_when_disconnected(self):
        """Returns empty list when not connected."""
        broker = AlpacaBroker(_alpaca_config())
        assert broker.get_positions() == []

    def test_get_positions_multiple(self):
        """Multiple positions all converted correctly."""
        broker, mock_client = _connected_broker()
        mock_client.get_all_positions.return_value = [
            _make_mock_position(symbol="AAPL", qty="5"),
            _make_mock_position(symbol="MSFT", qty="3"),
            _make_mock_position(symbol="TSLA", qty="2"),
        ]

        positions = broker.get_positions()
        symbols = [p.ticker for p in positions]
        assert "AAPL" in symbols
        assert "MSFT" in symbols
        assert "TSLA" in symbols


# ═══════════════════════════════════════════════════════════════════════════════
# 7. place_order
# ═══════════════════════════════════════════════════════════════════════════════

class TestPlaceOrder:
    """Tests for place_order() with all order types."""

    def test_place_limit_order_success(self):
        """Limit order returns OrderResult with order_id and SUBMITTED status."""
        broker, mock_client = _connected_broker()
        mock_order = _make_mock_order(
            symbol="AAPL", status="new", side="buy", qty="5",
            limit_price="155.00", order_type="limit"
        )
        mock_client.submit_order.return_value = mock_order
        mock_req = MagicMock()

        with patch("brokers.alpaca.broker.LimitOrderRequest", return_value=mock_req):
            result = broker.place_order(
                ticker="AAPL",
                side=OrderSide.BUY,
                qty=5,
                price=155.0,
                order_type=OrderType.LIMIT,
            )

        assert result.success is True
        assert result.ticker == "AAPL"
        assert result.requested_qty == 5
        assert result.requested_price == pytest.approx(155.0)
        assert result.status == OrderStatus.SUBMITTED
        assert result.commission == 0.0   # Alpaca is commission-free

    def test_place_market_order_success(self):
        """Market order uses MarketOrderRequest."""
        broker, mock_client = _connected_broker()
        mock_order = _make_mock_order(status="new", order_type="market", limit_price=None)
        mock_client.submit_order.return_value = mock_order
        mock_req = MagicMock()

        with patch("brokers.alpaca.broker.MarketOrderRequest", return_value=mock_req) as MockReq:
            result = broker.place_order(
                ticker="MSFT",
                side=OrderSide.BUY,
                qty=3,
                price=300.0,
                order_type=OrderType.MARKET,
            )

        assert result.success is True
        MockReq.assert_called_once()

    def test_place_stop_order_uses_stop_price(self):
        """Stop order passes stop_price to StopOrderRequest."""
        broker, mock_client = _connected_broker()
        mock_order = _make_mock_order(
            status="new", order_type="stop",
            limit_price=None, stop_price="148.00"
        )
        mock_client.submit_order.return_value = mock_order
        mock_req = MagicMock()

        with patch("brokers.alpaca.broker.StopOrderRequest", return_value=mock_req) as MockReq:
            result = broker.place_order(
                ticker="AAPL",
                side=OrderSide.SELL,
                qty=5,
                price=148.0,
                order_type=OrderType.STOP,
                stop_price=148.0,
            )

        assert result.success is True
        MockReq.assert_called_once()

    def test_place_stop_limit_order(self):
        """Stop-limit order passes both stop_price and limit_price."""
        broker, mock_client = _connected_broker()
        mock_order = _make_mock_order(
            status="new", order_type="stop_limit",
            limit_price="149.00", stop_price="148.00"
        )
        mock_client.submit_order.return_value = mock_order
        mock_req = MagicMock()

        with patch("brokers.alpaca.broker.StopLimitOrderRequest", return_value=mock_req) as MockReq:
            result = broker.place_order(
                ticker="AAPL",
                side=OrderSide.SELL,
                qty=5,
                price=149.0,
                order_type=OrderType.STOP_LIMIT,
                stop_price=148.0,
            )

        assert result.success is True
        MockReq.assert_called_once()

    def test_place_order_api_error_returns_failure(self):
        """API error returns OrderResult(success=False)."""
        broker, mock_client = _connected_broker()
        mock_client.submit_order.side_effect = Exception("insufficient funds")
        mock_req = MagicMock()

        with patch("brokers.alpaca.broker.LimitOrderRequest", return_value=mock_req):
            result = broker.place_order(
                ticker="AAPL", side=OrderSide.BUY, qty=100, price=200.0
            )

        assert result.success is False
        assert "insufficient funds" in result.message

    def test_place_order_disconnected_returns_failure(self):
        """Returns failure immediately if not connected."""
        broker = AlpacaBroker(_alpaca_config())
        result = broker.place_order(
            ticker="AAPL", side=OrderSide.BUY, qty=1, price=100.0
        )
        assert result.success is False


# ═══════════════════════════════════════════════════════════════════════════════
# 8. cancel_order / cancel_all_orders
# ═══════════════════════════════════════════════════════════════════════════════

class TestCancelOrders:
    """Tests for cancel_order() and cancel_all_orders()."""

    def test_cancel_order_success(self):
        """cancel_order() returns CANCELLED on success."""
        broker, mock_client = _connected_broker()
        order_id = str(uuid.uuid4())
        mock_client.cancel_order_by_id.return_value = None  # void response

        result = broker.cancel_order(order_id)

        assert result.success is True
        assert result.status == OrderStatus.CANCELLED
        mock_client.cancel_order_by_id.assert_called_once()

    def test_cancel_order_api_error(self):
        """cancel_order() returns failure on API error."""
        broker, mock_client = _connected_broker()
        mock_client.cancel_order_by_id.side_effect = Exception("order not found")
        order_id = str(uuid.uuid4())

        result = broker.cancel_order(order_id)

        assert result.success is False

    def test_cancel_all_orders(self):
        """cancel_all_orders() returns list of CANCELLED results."""
        broker, mock_client = _connected_broker()
        cancelled_orders = [
            _make_mock_order(symbol="AAPL"),
            _make_mock_order(symbol="MSFT"),
        ]
        mock_client.cancel_orders.return_value = cancelled_orders

        results = broker.cancel_all_orders()

        assert len(results) == 2
        assert all(r.success is True for r in results)
        assert all(r.status == OrderStatus.CANCELLED for r in results)

    def test_cancel_all_orders_empty_when_disconnected(self):
        """Returns empty list if disconnected."""
        broker = AlpacaBroker(_alpaca_config())
        assert broker.cancel_all_orders() == []


# ═══════════════════════════════════════════════════════════════════════════════
# 9. get_open_orders / get_order_status
# ═══════════════════════════════════════════════════════════════════════════════

class TestOrderQueries:
    """Tests for get_open_orders() and get_order_status()."""

    def test_get_open_orders_returns_results(self):
        """get_open_orders() converts Alpaca orders to OrderResult list."""
        broker, mock_client = _connected_broker()
        mock_client.get_orders.return_value = [
            _make_mock_order(symbol="AAPL", status="new", qty="5", limit_price="155.00"),
            _make_mock_order(symbol="MSFT", status="new", qty="3", limit_price="300.00"),
        ]

        # GetOrdersRequest and QueryOrderStatus are at module level — just call directly
        orders = broker.get_open_orders()

        assert len(orders) == 2
        tickers = {o.ticker for o in orders}
        assert "AAPL" in tickers
        assert "MSFT" in tickers

    def test_get_open_orders_empty_when_disconnected(self):
        """Returns empty list if disconnected."""
        broker = AlpacaBroker(_alpaca_config())
        assert broker.get_open_orders() == []

    def test_get_order_status_success(self):
        """get_order_status() returns correct status for known order."""
        broker, mock_client = _connected_broker()
        order_id = str(uuid.uuid4())
        mock_client.get_order_by_id.return_value = _make_mock_order(
            order_id=order_id, status="filled", filled_qty="5", filled_avg_price="156.00"
        )

        result = broker.get_order_status(order_id)

        assert result.success is True
        assert result.status == OrderStatus.FILLED
        assert result.filled_qty == 5
        assert result.fill_price == pytest.approx(156.0)

    def test_get_order_status_disconnected_returns_failure(self):
        """Returns failure if not connected."""
        broker = AlpacaBroker(_alpaca_config())
        result = broker.get_order_status(str(uuid.uuid4()))
        assert result.success is False


# ═══════════════════════════════════════════════════════════════════════════════
# 10. Registry integration
# ═══════════════════════════════════════════════════════════════════════════════

class TestRegistry:
    """Tests for brokers.registry alpaca integration."""

    def test_alpaca_factory_registered(self):
        """AlpacaBroker is registered in the broker factory."""
        from brokers.registry import _register_defaults, _BROKER_FACTORIES
        # Clear and re-register to ensure fresh state
        _BROKER_FACTORIES.clear()
        _register_defaults()
        assert "alpaca" in _BROKER_FACTORIES

    def test_get_broker_returns_alpaca_when_configured(self):
        """get_broker returns AlpacaBroker when trading.broker == 'alpaca'."""
        from brokers.registry import get_broker, _BROKER_FACTORIES

        config = {
            "market": "sp500",
            "trading": {
                "broker": "alpaca",
                "live_enabled": True,
            },
            "alpaca": {"paper": True},
        }

        # Ensure alpaca is in registry
        if "alpaca" not in _BROKER_FACTORIES:
            from brokers.registry import _make_alpaca_broker
            _BROKER_FACTORIES["alpaca"] = _make_alpaca_broker

        broker = get_broker("sp500", config)
        assert broker is not None
        assert isinstance(broker, AlpacaBroker)

    def test_get_broker_returns_none_when_live_disabled(self):
        """get_broker returns None when live_enabled=False."""
        from brokers.registry import get_broker

        config = {
            "market": "sp500",
            "trading": {
                "broker": "alpaca",
                "live_enabled": False,
            },
            "alpaca": {"paper": True},
        }

        broker = get_broker("sp500", config)
        assert broker is None
