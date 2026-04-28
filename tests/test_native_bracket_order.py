"""Tests for native OCO BRACKET order support (#273).

Covers:
1. _build_order_request shape — BRACKET, OTO, plain LIMIT
2. place_order passes take_profit_price through
3. _execute_entry submits exactly ONE call for a bracket entry
4. child leg IDs persisted after fill
5. update_trade_protective_orders DB helper
6. partial-fill warning emission

All DB-touching tests use the autouse _isolate_prod_db fixture.
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from alpaca.trading.enums import OrderClass, TimeInForce
from alpaca.trading.enums import OrderSide as AlpacaSide
from alpaca.trading.requests import (
    LimitOrderRequest,
    StopLossRequest,
    TakeProfitRequest,
)

import db.atlas_db as _adb
from db.atlas_db import init_db, update_trade_protective_orders, get_db
from brokers.alpaca.broker import _build_order_request, OrderType
from brokers.base import OrderResult, OrderSide, OrderStatus


# ─── shared config/broker helpers ────────────────────────────────────────────

def _live_config() -> dict:
    """Minimal live-mode config (dry_run_first=False)."""
    return {
        "version": "test-bracket-v1.0",
        "market_id": "sp500",
        "trading": {
            "mode": "live",
            "live_enabled": True,
            "live_safety": {
                "max_order_value": 50_000,
                "max_daily_orders": 50,
                "dry_run_first": False,
                "max_daily_loss_pct": 0.05,
            },
        },
        "risk": {
            "starting_equity": 10_000.0,
            "max_risk_per_trade_pct": 0.02,
            "max_open_positions": 10,
        },
    }


def _make_order_result(
    ticker: str = "MU",
    status: OrderStatus = OrderStatus.SUBMITTED,
    fill_price: float = 0.0,
    filled_qty: int = 0,
    legs: list | None = None,
    order_id: str = "parent-order-uuid",
) -> OrderResult:
    return OrderResult(
        success=True,
        order_id=order_id,
        ticker=ticker,
        side=OrderSide.BUY,
        status=status,
        requested_qty=10,
        filled_qty=filled_qty,
        fill_price=fill_price,
        raw={
            "legs": legs or [],
            "filled_qty": str(filled_qty),
            "submitted_at": "2026-04-28T10:00:00Z",
            "filled_at": "2026-04-28T10:00:01Z" if fill_price > 0 else "",
        },
    )


def _make_executor(mock_broker: MagicMock):
    """Build a pre-connected LiveExecutor using a proper config."""
    from brokers.live_executor import LiveExecutor
    ex = LiveExecutor(_live_config())
    ex._broker = mock_broker
    ex._connected = True
    ex._halted = False
    ex._daily_date = "2026-04-28"
    ex._daily_order_count = 0
    return ex


def _insert_open_trade(ticker: str = "MU", universe: str = "sp500") -> int:
    with get_db() as conn:
        cur = conn.execute(
            """
            INSERT INTO trades
              (ticker, strategy, universe, direction, entry_date, entry_price,
               shares, stop_price, take_profit, confidence, status)
            VALUES (?, 'momentum', ?, 'long', '2026-04-28', 90.0, 10, 80.0, 110.0, 0.8, 'open')
            """,
            (ticker, universe),
        )
        return cur.lastrowid


# ─── 1. _build_order_request shape ───────────────────────────────────────────

class TestBuildOrderRequestShape:
    """Test 1-3 as specified."""

    def _req(self, stop_loss_price=None, take_profit_price=None, price=100.0):
        return _build_order_request(
            symbol="MU",
            side=AlpacaSide.BUY,
            qty=10,
            price=price,
            order_type=OrderType.LIMIT,
            stop_price=None,
            tif=TimeInForce.DAY,
            client_id="test-client-id",
            stop_loss_price=stop_loss_price,
            take_profit_price=take_profit_price,
        )

    def test_build_order_request_bracket_with_stop_and_tp(self):
        """Test 1: stop+tp → OrderClass.BRACKET with both child legs."""
        req = self._req(stop_loss_price=99.0, take_profit_price=110.0)
        assert isinstance(req, LimitOrderRequest)
        assert req.order_class == OrderClass.BRACKET
        assert isinstance(req.stop_loss, StopLossRequest)
        assert float(req.stop_loss.stop_price) == 99.0
        assert isinstance(req.take_profit, TakeProfitRequest)
        assert float(req.take_profit.limit_price) == 110.0

    def test_build_order_request_oto_with_stop_only(self):
        """Test 2: stop only → OrderClass.OTO, no take_profit (backward compat)."""
        req = self._req(stop_loss_price=99.0, take_profit_price=None)
        assert isinstance(req, LimitOrderRequest)
        assert req.order_class == OrderClass.OTO
        assert isinstance(req.stop_loss, StopLossRequest)
        assert float(req.stop_loss.stop_price) == 99.0
        assert getattr(req, "take_profit", None) is None

    def test_build_order_request_plain_limit_no_protective(self):
        """Test 3: neither stop nor tp → no order_class, no child legs."""
        req = self._req(stop_loss_price=None, take_profit_price=None)
        assert isinstance(req, LimitOrderRequest)
        assert getattr(req, "order_class", None) is None
        assert getattr(req, "stop_loss", None) is None
        assert getattr(req, "take_profit", None) is None


# ─── 2. place_order passes take_profit_price through ─────────────────────────

class TestPlaceOrderPassThrough:
    def test_place_order_passes_take_profit_through(self):
        """Test 4: take_profit_price kwarg flows through to _build_order_request."""
        captured: dict = {}

        def fake_build(**kwargs):
            captured.update(kwargs)
            return LimitOrderRequest(
                symbol="MU",
                qty=10,
                side=AlpacaSide.BUY,
                time_in_force=TimeInForce.DAY,
                limit_price=100.0,
                order_class=OrderClass.BRACKET,
                stop_loss=StopLossRequest(stop_price=99.0),
                take_profit=TakeProfitRequest(limit_price=110.0),
            )

        # Minimal fake order from SDK
        fake_sdk_order = MagicMock()
        fake_sdk_order.id = "order-123"
        fake_sdk_order.status = "accepted"
        fake_sdk_order.filled_avg_price = None
        fake_sdk_order.filled_qty = None
        fake_sdk_order.legs = []
        fake_sdk_order.model_dump = lambda: {
            "id": "order-123", "status": "accepted", "legs": [],
            "filled_avg_price": None, "filled_qty": None,
            "order_type": "limit", "side": "buy",
        }

        mock_trade_client = MagicMock()
        mock_trade_client.submit_order.return_value = fake_sdk_order

        from brokers.alpaca.broker import AlpacaBroker

        broker = MagicMock(spec=AlpacaBroker)
        broker._trade_client = mock_trade_client
        broker.is_live = True

        with patch("brokers.alpaca.broker._build_order_request", side_effect=fake_build):
            # Directly call the real place_order on a real broker instance
            real_broker = AlpacaBroker.__new__(AlpacaBroker)
            real_broker._connected = True
            real_broker._trade_client = mock_trade_client
            real_broker._paper = False  # is_live property returns not self._paper
            real_broker._account_id = "test"
            real_broker._daily_order_count = 0
            real_broker._tif = "day"  # required by place_order TIF resolution

            real_broker.place_order(
                ticker="MU",
                side=OrderSide.BUY,
                qty=10,
                price=100.0,
                order_type=OrderType.LIMIT,
                stop_loss_price=99.0,
                take_profit_price=110.0,
            )

        assert captured.get("stop_loss_price") == 99.0
        assert captured.get("take_profit_price") == 110.0


# ─── 3. _execute_entry submits exactly ONE call ───────────────────────────────

class TestExecuteEntryBracketSubmission:
    def test_execute_entry_submits_single_bracket_call(self):
        """Test 5: one submit_order call; kwargs include stop_loss_price+take_profit_price."""
        submitted_result = _make_order_result(
            status=OrderStatus.SUBMITTED,  # not filled — deferred
            fill_price=0.0,
            filled_qty=0,
        )

        mock_broker = MagicMock()
        mock_broker.place_order.return_value = submitted_result
        mock_broker.is_live = True

        executor = _make_executor(mock_broker)

        entry = {
            "ticker": "MU",
            "entry_price": 100.0,
            "position_size": 10,
            "strategy": "momentum",
            "confidence": 0.8,
            "stop_price": 90.0,
            "take_profit": 115.0,
        }

        with (
            patch("brokers.live_executor.preflight_check_order", return_value=[]),
            patch("brokers.kill_switch.is_halted", return_value=False),
            patch("brokers.price_arbiter.is_ticker_halted", return_value=False),
        ):
            result = executor._execute_entry(entry, "2026-04-28")

        # Exactly ONE broker.place_order call (not separate SL + TP calls)
        assert mock_broker.place_order.call_count == 1
        call_kwargs = mock_broker.place_order.call_args[1]
        assert call_kwargs.get("stop_loss_price") == 90.0
        assert call_kwargs.get("take_profit_price") == 115.0
        assert call_kwargs.get("order_type") == OrderType.LIMIT


# ─── 4. child leg IDs persisted after fill ────────────────────────────────────

class TestChildLegPersistence:
    def test_execute_entry_records_child_leg_ids_after_fill(self, tmp_path, monkeypatch):
        """Test 6: FILLED bracket → child leg IDs written via update_trade_protective_orders."""
        db_path = str(tmp_path / "bracket_test.db")
        monkeypatch.setattr(_adb, "_db_path_override", db_path)
        init_db()

        filled_result = _make_order_result(
            status=OrderStatus.FILLED,
            fill_price=100.5,
            filled_qty=10,
            legs=[
                {"id": "stop-uuid", "side": "sell", "order_type": "stop"},
                {"id": "tp-uuid",   "side": "sell", "order_type": "limit"},
            ],
        )

        mock_broker = MagicMock()
        mock_broker.place_order.return_value = filled_result
        mock_broker.is_live = True

        executor = _make_executor(mock_broker)

        entry = {
            "ticker": "MU",
            "entry_price": 100.0,
            "position_size": 10,
            "strategy": "momentum",
            "confidence": 0.8,
            "stop_price": 90.0,
            "take_profit": 115.0,
        }

        captured_update: dict = {}

        def fake_update(*, ticker, universe, stop_order_id, tp_order_id):
            captured_update["ticker"] = ticker
            captured_update["universe"] = universe
            captured_update["stop_order_id"] = stop_order_id
            captured_update["tp_order_id"] = tp_order_id
            return 1

        with (
            patch("brokers.live_executor.preflight_check_order", return_value=[]),
            patch("brokers.kill_switch.is_halted", return_value=False),
            patch("brokers.price_arbiter.is_ticker_halted", return_value=False),
            patch("db.atlas_db.update_trade_protective_orders", fake_update),
        ):
            executor._execute_entry(entry, "2026-04-28")

        assert captured_update.get("stop_order_id") == "stop-uuid"
        assert captured_update.get("tp_order_id") == "tp-uuid"
        assert captured_update.get("ticker") == "MU"
        assert captured_update.get("universe") == "sp500"


# ─── 5. update_trade_protective_orders DB helper ──────────────────────────────

class TestUpdateTradeProtectiveOrdersHelper:
    def test_update_trade_protective_orders_helper(self, tmp_path, monkeypatch):
        """Test 7: basic update, idempotency, no-match returns 0 + WARN."""
        db_path = str(tmp_path / "helper_test.db")
        monkeypatch.setattr(_adb, "_db_path_override", db_path)
        init_db()

        trade_id = _insert_open_trade("MU", "sp500")

        # First write
        n = update_trade_protective_orders(
            ticker="MU",
            universe="sp500",
            stop_order_id="abc-stop",
            tp_order_id="def-tp",
        )
        assert n == 1

        with get_db() as conn:
            row = conn.execute(
                "SELECT stop_order_id, tp_order_id FROM trades WHERE id=?",
                (trade_id,),
            ).fetchone()
        assert row["stop_order_id"] == "abc-stop"
        assert row["tp_order_id"] == "def-tp"

        # Idempotency — second call, same params, same values
        n2 = update_trade_protective_orders(
            ticker="MU",
            universe="sp500",
            stop_order_id="abc-stop",
            tp_order_id="def-tp",
        )
        assert n2 == 1

        with get_db() as conn:
            row2 = conn.execute(
                "SELECT stop_order_id, tp_order_id FROM trades WHERE id=?",
                (trade_id,),
            ).fetchone()
        assert row2["stop_order_id"] == "abc-stop"
        assert row2["tp_order_id"] == "def-tp"

    def test_update_no_match_returns_zero_and_warns(self, tmp_path, monkeypatch, caplog):
        """No open trade for ticker → 0 + WARNING."""
        db_path = str(tmp_path / "nomatch_test.db")
        monkeypatch.setattr(_adb, "_db_path_override", db_path)
        init_db()

        with caplog.at_level(logging.WARNING, logger="db.atlas_db"):
            n = update_trade_protective_orders(
                ticker="NOMATCH",
                universe="sp500",
                stop_order_id="x",
                tp_order_id="y",
            )
        assert n == 0
        assert any("no open trade" in r.message.lower() for r in caplog.records)

    def test_update_empty_args_returns_zero(self, tmp_path, monkeypatch):
        """Both args None → returns 0 without touching DB."""
        db_path = str(tmp_path / "empty_test.db")
        monkeypatch.setattr(_adb, "_db_path_override", db_path)
        init_db()

        n = update_trade_protective_orders(ticker="AAPL", universe="sp500")
        assert n == 0

    def test_partial_update_stop_only(self, tmp_path, monkeypatch):
        """Only stop_order_id passed → only that field updated."""
        db_path = str(tmp_path / "partial_test.db")
        monkeypatch.setattr(_adb, "_db_path_override", db_path)
        init_db()

        trade_id = _insert_open_trade("AMZN", "sp500")

        n = update_trade_protective_orders(
            ticker="AMZN",
            universe="sp500",
            stop_order_id="stop-only",
        )
        assert n == 1

        with get_db() as conn:
            row = conn.execute(
                "SELECT stop_order_id, tp_order_id FROM trades WHERE id=?",
                (trade_id,),
            ).fetchone()
        assert row["stop_order_id"] == "stop-only"
        # tp_order_id was not passed — should remain at its default ('' or NULL)
        assert row["tp_order_id"] in (None, "")


# ─── 6. partial-fill warning ─────────────────────────────────────────────────

class TestPartialFillWarning:
    def test_partial_fill_logged_warning(self, caplog):
        """Test 8: filled_qty < qty on FILLED status → WARNING emitted."""
        partial_result = _make_order_result(
            status=OrderStatus.FILLED,
            fill_price=100.5,
            filled_qty=5,   # ordered 10, only 5 filled
            order_id="partial-order",
        )
        # Override filled_qty in raw to simulate partial fill
        partial_result.raw["filled_qty"] = "5"

        mock_broker = MagicMock()
        mock_broker.place_order.return_value = partial_result
        mock_broker.is_live = True

        executor = _make_executor(mock_broker)

        entry = {
            "ticker": "MU",
            "entry_price": 100.0,
            "position_size": 10,    # ordered 10
            "strategy": "momentum",
            "confidence": 0.8,
            "stop_price": 90.0,
            "take_profit": 115.0,
        }

        with (
            caplog.at_level(logging.WARNING, logger="brokers.live_executor"),
            patch("brokers.live_executor.preflight_check_order", return_value=[]),
            patch("brokers.kill_switch.is_halted", return_value=False),
            patch("brokers.price_arbiter.is_ticker_halted", return_value=False),
        ):
            executor._execute_entry(entry, "2026-04-28")

        partial_warnings = [
            r for r in caplog.records
            if "partial fill" in r.message.lower() or "partial_fill" in r.message.lower()
        ]
        assert partial_warnings, (
            f"Expected PARTIAL FILL warning. Got: {[r.message for r in caplog.records]}"
        )
