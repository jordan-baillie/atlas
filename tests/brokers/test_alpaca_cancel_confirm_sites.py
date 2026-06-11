"""Tests for RCA latent #4: Phase 2C cancel-confirm guards at 5 broker.py sites.

Tests:
  1. _wait_for_cancel_confirmed unit tests (direct method calls)
  2. Source inspection: verify Phase 2C calls are present at all 5 sites
  3. Behavioral tests via driving sync_all_protective_orders with raw order mocks
"""
from __future__ import annotations

import os
import time
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

from atlas.brokers.base import OrderResult, OrderStatus, OrderSide

# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _make_broker():
    """Create a minimal AlpacaBroker stub with _connected=True."""
    from atlas.brokers.alpaca.broker import AlpacaBroker
    broker = AlpacaBroker.__new__(AlpacaBroker)
    broker._connected = True
    broker._market_data = None
    return broker


def _status_result(order_id: str, status: OrderStatus) -> OrderResult:
    return OrderResult(
        success=True, order_id=order_id, status=status,
    )


def _status_sequence(*statuses: OrderStatus):
    """Mock that cycles through statuses on each call."""
    it = iter(statuses)

    def _side(order_id: str) -> OrderResult:
        try:
            st = next(it)
        except StopIteration:
            st = statuses[-1]
        return _status_result(order_id, st)

    return _side


# ─────────────────────────────────────────────────────────────────────────────
# Unit tests for _wait_for_cancel_confirmed
# ─────────────────────────────────────────────────────────────────────────────

class TestWaitForCancelConfirmedMethod:
    """Unit tests for AlpacaBroker._wait_for_cancel_confirmed."""

    def test_returns_true_when_immediately_cancelled(self, monkeypatch):
        """Returns True when status is CANCELLED on first poll."""
        broker = _make_broker()
        broker.get_order_status = _status_sequence(OrderStatus.CANCELLED)
        monkeypatch.setenv("ATLAS_BROKER_CANCEL_CONFIRM_TIMEOUT_SEC", "1.0")
        assert broker._wait_for_cancel_confirmed("oid-1", timeout_s=5.0) is True

    def test_returns_true_when_failed(self, monkeypatch):
        """Returns True for FAILED status (also terminal)."""
        broker = _make_broker()
        broker.get_order_status = _status_sequence(OrderStatus.FAILED)
        assert broker._wait_for_cancel_confirmed("oid-2", timeout_s=5.0) is True

    def test_returns_true_after_two_pending_polls(self):
        """Returns True after cycling through PENDING → PENDING → CANCELLED."""
        broker = _make_broker()
        broker.get_order_status = _status_sequence(
            OrderStatus.PENDING, OrderStatus.PENDING, OrderStatus.CANCELLED,
        )

        call_count = [0]
        original_sleep = time.sleep

        with patch("atlas.brokers.alpaca.broker.time") as mock_time:
            _t = [0.0]

            def _mono():
                v = _t[0]
                _t[0] += 0.1   # small increment — won't timeout at 5s
                return v

            mock_time.monotonic.side_effect = _mono
            mock_time.sleep = MagicMock()

            result = broker._wait_for_cancel_confirmed("oid-3", timeout_s=5.0)

        assert result is True

    def test_returns_false_on_timeout(self):
        """Returns False when polls always return PENDING and timeout expires."""
        broker = _make_broker()
        broker.get_order_status = MagicMock(
            return_value=_status_result("oid-4", OrderStatus.PENDING)
        )

        with patch("atlas.brokers.alpaca.broker.time") as mock_time:
            _t = [0.0]

            def _mono():
                v = _t[0]
                _t[0] += 20.0   # jump past any timeout
                return v

            mock_time.monotonic.side_effect = _mono
            mock_time.sleep = MagicMock()
            result = broker._wait_for_cancel_confirmed("oid-4", timeout_s=1.0)

        assert result is False

    def test_returns_false_on_filled(self):
        """Returns False when order is FILLED (race lost)."""
        broker = _make_broker()
        broker.get_order_status = _status_sequence(OrderStatus.FILLED)
        assert broker._wait_for_cancel_confirmed("oid-5", timeout_s=5.0) is False

    def test_env_var_sets_default_timeout(self, monkeypatch):
        """ATLAS_BROKER_CANCEL_CONFIRM_TIMEOUT_SEC env var controls default timeout."""
        monkeypatch.setenv("ATLAS_BROKER_CANCEL_CONFIRM_TIMEOUT_SEC", "0.001")
        broker = _make_broker()
        broker.get_order_status = MagicMock(
            return_value=_status_result("oid-6", OrderStatus.PENDING)
        )

        with patch("atlas.brokers.alpaca.broker.time") as mock_time:
            _t = [0.0]

            def _mono():
                v = _t[0]
                _t[0] += 100.0   # immediately past 0.001s timeout
                return v

            mock_time.monotonic.side_effect = _mono
            mock_time.sleep = MagicMock()
            result = broker._wait_for_cancel_confirmed("oid-6")   # uses env var

        assert result is False   # timed out

    def test_tolerates_poll_exception_and_retries(self):
        """Exception in get_order_status is handled; eventually confirms."""
        broker = _make_broker()
        call_count = [0]

        def _side(oid):
            call_count[0] += 1
            if call_count[0] < 3:
                raise RuntimeError("transient error")
            return _status_result(oid, OrderStatus.CANCELLED)

        broker.get_order_status = _side

        with patch("atlas.brokers.alpaca.broker.time") as mock_time:
            _t = [0.0]

            def _mono():
                v = _t[0]
                _t[0] += 0.05
                return v

            mock_time.monotonic.side_effect = _mono
            mock_time.sleep = MagicMock()
            result = broker._wait_for_cancel_confirmed("oid-7", timeout_s=5.0)

        assert result is True


# ─────────────────────────────────────────────────────────────────────────────
# Source inspection: verify Phase 2C pattern is present at all 5 sites
# ─────────────────────────────────────────────────────────────────────────────

class TestPhase2CSourceInspection:
    """Verify the Phase 2C cancel-confirm pattern appears at all 5 broker.py sites."""

    @pytest.fixture(autouse=True)
    def _load_source(self):
        source_path = Path(__file__).parent.parent.parent / "atlas" / "brokers" / "alpaca" / "broker.py"
        self.source = source_path.read_text(encoding="utf-8")
        self.lines = self.source.split("\n")

    def test_site_1_confirm_before_tightened_oco(self):
        """Site 1: stop cancel confirmed before tightened OCO is placed."""
        assert "Site 1 of 5" in self.source, "Site 1 marker missing"
        assert "existing_stop_order_id" in self.source
        # Confirm that _wait_for_cancel_confirmed is called for the stop
        # in the tightening block (look for the call after existing_stop_order_id)
        idx = self.source.index("Site 1 of 5")
        block = self.source[idx:idx + 1500]
        assert "_wait_for_cancel_confirmed" in block

    def test_site_2_confirm_before_tightened_oco(self):
        """Site 2: TP cancel confirmed before tightened OCO is placed."""
        assert "Site 2 of 5" in self.source
        idx = self.source.index("Site 2 of 5")
        block = self.source[idx:idx + 1500]
        assert "_wait_for_cancel_confirmed" in block
        assert "existing_tp_order_id" in block

    def test_site_3_confirm_in_stop_cancel_loop(self):
        """Site 3: stop cancel confirmed in the loop before OCO placement."""
        assert "Site 3 of 5" in self.source
        idx = self.source.index("Site 3 of 5")
        block = self.source[idx:idx + 1500]
        assert "_wait_for_cancel_confirmed" in block
        assert "cancel_confirmed_all" in block

    def test_site_4_confirm_in_tp_cancel_loop(self):
        """Site 4: TP cancel confirmed in the loop before OCO placement."""
        assert "Site 4 of 5" in self.source
        idx = self.source.index("Site 4 of 5")
        block = self.source[idx:idx + 1500]
        assert "_wait_for_cancel_confirmed" in block
        assert "cancel_confirmed_all" in block

    def test_site_5_confirm_before_trailing_upgrade(self):
        """Site 5: static stop cancel confirmed before trailing stop upgrade."""
        assert "Site 5 of 5" in self.source
        idx = self.source.index("Site 5 of 5")
        block = self.source[idx:idx + 500]
        assert "_wait_for_cancel_confirmed" in block

    def test_cancel_confirmed_all_gates_oco_placement(self):
        """cancel_confirmed_all flag gates the OCO try block."""
        assert "cancel_confirmed_all = True" in self.source
        assert "if not cancel_confirmed_all:" in self.source
        assert "if cancel_confirmed_all:" in self.source

    def test_method_is_defined_in_class(self):
        """_wait_for_cancel_confirmed is a class method of AlpacaBroker."""
        assert "def _wait_for_cancel_confirmed(" in self.source

    def test_imports_os_and_time(self):
        """broker.py has 'import os' and 'import time' for the method."""
        assert "import os\n" in self.source, "import os missing"
        assert "import time\n" in self.source, "import time missing"


# ─────────────────────────────────────────────────────────────────────────────
# Behavioral tests: confirm guards fire correctly
# ─────────────────────────────────────────────────────────────────────────────

def _make_broker_with_confirm(confirm_returns: bool):
    """Make a broker stub where _wait_for_cancel_confirmed returns given value."""
    broker = _make_broker()
    broker._wait_for_cancel_confirmed = MagicMock(return_value=confirm_returns)
    return broker


class TestCancelConfirmBehavior:
    """Functional tests for the cancel-confirm guard: simulate what happens
    when _wait_for_cancel_confirmed returns True vs False."""

    def test_site5_trailing_upgrade_placed_when_confirm_true(self):
        """When cancel confirms, the trailing stop IS placed (site 5 path)."""
        broker = _make_broker_with_confirm(True)

        place_calls = []

        def _cancel(oid):
            return OrderResult(success=True, order_id=oid, status=OrderStatus.CANCELLED)

        def _place(**kwargs):
            place_calls.append(kwargs)
            return OrderResult(
                success=True, order_id="trail-id", ticker="AAPL",
                side=OrderSide.SELL, status=OrderStatus.SUBMITTED,
            )

        broker.cancel_order = _cancel
        broker.place_order = _place

        # Drive the site 5 path by calling the method that embeds it
        _drive_site5_path(broker)

        trailing_calls = [c for c in place_calls if "TRAILING" in str(c.get("order_type", "")).upper() or
                          "trailing" in str(c.get("order_type", "")).lower()]
        assert len(trailing_calls) >= 1, f"Expected trailing stop placed; calls={place_calls}"
        # Confirm was checked
        broker._wait_for_cancel_confirmed.assert_called_once()

    def test_site5_trailing_upgrade_skipped_when_confirm_false(self):
        """When cancel confirm fails, trailing stop is NOT placed (site 5 path)."""
        broker = _make_broker_with_confirm(False)

        place_calls = []

        def _cancel(oid):
            return OrderResult(success=True, order_id=oid, status=OrderStatus.SUBMITTED)

        def _place(**kwargs):
            if "TRAILING" in str(kwargs.get("order_type", "")).upper() or \
               "trailing" in str(kwargs.get("order_type", "")).lower():
                place_calls.append(kwargs)
            return OrderResult(success=False, order_id="", ticker="AAPL",
                               side=OrderSide.SELL, status=OrderStatus.FAILED)

        broker.cancel_order = _cancel
        broker.place_order = _place

        _drive_site5_path(broker)

        assert len(place_calls) == 0, f"Trailing must not be placed after timeout; calls={place_calls}"


# ─────────────────────────────────────────────────────────────────────────────
# Infrastructure for behavioral tests
# ─────────────────────────────────────────────────────────────────────────────

def _fake_raw_order(order_id: str, symbol: str, order_type: str, side: str = "sell",
                    stop_price: float = 0.0, limit_price: float = 0.0):
    """Create a fake raw Alpaca order object (with attribute access)."""
    class _Enum:
        def __init__(self, v):
            self.value = v

    class _FakeOrder:
        id = order_id
        legs = None

    o = _FakeOrder()
    o.symbol = symbol
    o.order_type = _Enum(order_type)
    o.side = _Enum(side)
    o.stop_price = stop_price
    o.limit_price = limit_price
    o.status = _Enum("open")
    o.id = order_id
    return o


def _drive_site5_path(broker):
    """Drive sync_all_protective_orders into the trailing stop upgrade path (site 5).

    Requires a profitable position with existing static stop (no TP).
    """
    from atlas.brokers.base import PositionInfo
    from atlas.brokers.alpaca.broker import GetOrdersRequest, QueryOrderStatus

    ticker = "AAPL"
    entry_price = 80.0
    current_price = 100.0  # profitable
    stop_price = 70.0      # static stop, no TP

    pos = PositionInfo(
        ticker=ticker,
        entry_price=entry_price,
        current_price=current_price,
        shares=10,
        stop_price=stop_price,
    )
    plan = {ticker: {"stop_price": stop_price}}

    # Existing static stop order
    existing_stop = _fake_raw_order(
        "static-stop-001", ticker, "stop", "sell", stop_price
    )

    broker._broker_call = lambda fn, *args, **kw: fn(*args, **kw)
    broker._trade_client = MagicMock()
    broker._trade_client.get_orders = MagicMock(return_value=[existing_stop])
    broker._trade_client.submit_order = MagicMock()

    broker.sync_all_protective_orders(
        [pos], plan=plan, trade_date="2026-04-29", dry_run=False,
    )
