"""Tests for AlpacaBroker.cancel_order() idempotency on Alpaca 42210000 race.

Errors-table ids resolved: 12, 13, 14, 15, 16, 17, 23 — all
  cancel_order failed for ...: {"code":42210000,"message":"order pending cancel"}

Run:
    cd /root/atlas && python3 -m pytest tests/test_cancel_order_idempotency.py -v --timeout=30
"""
from __future__ import annotations

import logging
from unittest.mock import MagicMock, patch

import pytest

from atlas.brokers.alpaca.broker import AlpacaBroker
from atlas.brokers.base import OrderStatus


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _make_broker() -> AlpacaBroker:
    """Return a minimal AlpacaBroker stub with _connected=True (no real init)."""
    broker = AlpacaBroker.__new__(AlpacaBroker)
    broker._connected = True
    broker._market_data = None
    # _trade_client must exist so _require_connected() doesn't blow up, but
    # we'll always patch _broker_call directly in these tests.
    broker._trade_client = MagicMock()
    return broker


def _api_error(message: str) -> Exception:
    """Return a plain Exception whose str() looks like an Alpaca error payload."""
    return Exception(message)


# ─────────────────────────────────────────────────────────────────────────────
# Tests
# ─────────────────────────────────────────────────────────────────────────────

class TestCancelOrderIdempotency:

    def test_cancel_order_success_returns_cancelled(self):
        """Normal cancel succeeds: success=True, status=CANCELLED."""
        broker = _make_broker()
        with patch.object(broker, "_broker_call", return_value=None) as mock_call:
            result = broker.cancel_order("order-abc-123")

        assert result.success is True
        assert result.status == OrderStatus.CANCELLED
        assert result.order_id == "order-abc-123"
        mock_call.assert_called_once()

    def test_cancel_order_pending_cancel_race_treated_as_success(self, caplog):
        """42210000 'order pending cancel' race returns success=True and NO ERROR log."""
        broker = _make_broker()
        error_payload = '{"code":42210000,"message":"order pending cancel"}'

        with patch.object(broker, "_broker_call", side_effect=_api_error(error_payload)):
            with caplog.at_level(logging.ERROR, logger="atlas.brokers.alpaca.broker"):
                result = broker.cancel_order("order-race-42210000")

        # Must succeed (idempotent)
        assert result.success is True
        assert result.status == OrderStatus.CANCELLED
        assert result.order_id == "order-race-42210000"
        assert "idempotent" in result.message.lower() or "pending cancel" in result.message.lower()

        # No ERROR log must have been emitted for this benign race
        error_records = [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert error_records == [], (
            f"Expected no ERROR log for benign 42210000 race, got: {error_records}"
        )

    def test_cancel_order_other_error_still_fails(self, caplog):
        """A different error (e.g. 40410000 not-found) returns success=False + ERROR log."""
        broker = _make_broker()
        error_payload = '{"code":40410000,"message":"order not found"}'

        with patch.object(broker, "_broker_call", side_effect=_api_error(error_payload)):
            with caplog.at_level(logging.ERROR, logger="atlas.brokers.alpaca.broker"):
                result = broker.cancel_order("order-gone-40410000")

        assert result.success is False
        assert result.status == OrderStatus.FAILED
        assert result.order_id == "order-gone-40410000"
        assert "40410000" in result.message or "not found" in result.message.lower()

        # ERROR log must appear
        error_records = [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert len(error_records) >= 1, "Expected at least one ERROR log for non-idempotent error"

    def test_cancel_order_message_contains_pending_cancel_text(self, caplog):
        """Human-readable variant triggers idempotency (case-insensitive match)."""
        broker = _make_broker()
        # Message with human-readable text only (no numeric code) — still benign
        error_payload = "Order Pending Cancel — cannot cancel twice"

        with patch.object(broker, "_broker_call", side_effect=_api_error(error_payload)):
            with caplog.at_level(logging.ERROR, logger="atlas.brokers.alpaca.broker"):
                result = broker.cancel_order("order-text-pending")

        assert result.success is True
        assert result.status == OrderStatus.CANCELLED

        error_records = [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert error_records == [], (
            f"Expected no ERROR log for human-readable pending-cancel message, got: {error_records}"
        )
