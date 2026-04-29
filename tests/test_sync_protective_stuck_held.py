"""Tests for sync_protective_orders._handle_held_stops.

Verifies that:
  - First held observation: recorded in state file, NO cancel/resubmit.
  - Second consecutive held observation: cancel_order called, telegram alert sent.
  - Non-held stops: state entries cleaned up automatically.
  - dry_run=True: state written (to mark resolved) but cancel_order NOT called.
  - Multiple tickers: each handled independently.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.sync_protective_orders import _handle_held_stops


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_order(ticker: str, order_id: str, status: str = "held",
                order_type: str = "stop", side: str = "sell") -> MagicMock:
    """Build a mock OrderResult for a stop SELL order."""
    o = MagicMock()
    o.ticker = ticker
    o.order_id = order_id
    o.raw = {
        "status": status,
        "order_type": order_type,
        "side": side,
    }
    return o


def _make_broker(orders: list) -> MagicMock:
    """Build a mock broker that returns *orders* from get_open_orders."""
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from brokers.base import OrderResult, OrderStatus

    b = MagicMock()
    b.get_open_orders.return_value = orders
    cancel_ok = MagicMock()
    cancel_ok.success = True
    cancel_ok.message = "cancelled"
    b.cancel_order.return_value = cancel_ok
    # Phase 2B: mock get_order_status to return CANCELLED immediately so
    # _wait_for_cancel_confirm confirms on the first poll (no real timeout).
    b.get_order_status.return_value = OrderResult(
        success=True, order_id="", status=OrderStatus.CANCELLED
    )
    return b


# ── Tests ─────────────────────────────────────────────────────────────────────

class TestFirstCycleNoCancelResubmit:
    """On the first held observation no cancellation should occur."""

    def test_no_cancel_first_cycle(self, tmp_path: Path) -> None:
        state_file = tmp_path / "held.json"
        broker = _make_broker([_make_order("AMD", "order-amd-1")])

        result = _handle_held_stops(
            broker, "sp500",
            dry_run=False, send_telegram=False,
            state_file=state_file,
            now_iso="2026-04-21T10:00:00",
        )

        assert result["resubmitted"] == []
        assert result["newly_held"] == ["AMD"]
        assert result["errors"] == []
        broker.cancel_order.assert_not_called()

    def test_state_written_on_first_cycle(self, tmp_path: Path) -> None:
        state_file = tmp_path / "held.json"
        broker = _make_broker([_make_order("GLD", "order-gld-1")])

        _handle_held_stops(
            broker, "sp500",
            dry_run=False, send_telegram=False,
            state_file=state_file,
            now_iso="2026-04-21T10:00:00",
        )

        state = json.loads(state_file.read_text())
        assert "GLD::sp500" in state
        assert state["GLD::sp500"]["order_id"] == "order-gld-1"
        assert state["GLD::sp500"]["first_seen"] == "2026-04-21T10:00:00"


class TestSecondCycleCancelsAndResubmits:
    """On second consecutive held observation, cancel_order must be called."""

    def test_cancel_called_second_cycle(self, tmp_path: Path) -> None:
        state_file = tmp_path / "held.json"
        # Pre-populate state to simulate first cycle having already run
        state_file.write_text(json.dumps({
            "AMD::sp500": {"first_seen": "2026-04-21T09:45:00", "order_id": "order-amd-1"},
        }))
        broker = _make_broker([_make_order("AMD", "order-amd-1")])

        result = _handle_held_stops(
            broker, "sp500",
            dry_run=False, send_telegram=False,
            state_file=state_file,
            now_iso="2026-04-21T10:00:00",
        )

        assert result["resubmitted"] == ["AMD"]
        assert result["newly_held"] == []
        assert result["errors"] == []
        broker.cancel_order.assert_called_once_with("order-amd-1")

    def test_state_cleared_after_resubmit(self, tmp_path: Path) -> None:
        state_file = tmp_path / "held.json"
        state_file.write_text(json.dumps({
            "AMD::sp500": {"first_seen": "2026-04-21T09:45:00", "order_id": "order-amd-1"},
        }))
        broker = _make_broker([_make_order("AMD", "order-amd-1")])

        _handle_held_stops(
            broker, "sp500",
            dry_run=False, send_telegram=False,
            state_file=state_file,
        )

        state = json.loads(state_file.read_text())
        # State entry must be removed after successful resubmit
        assert "AMD::sp500" not in state

    def test_telegram_alert_sent_on_resubmit(self, tmp_path: Path) -> None:
        state_file = tmp_path / "held.json"
        state_file.write_text(json.dumps({
            "AMD::sp500": {"first_seen": "2026-04-21T09:45:00", "order_id": "ord-1"},
        }))
        broker = _make_broker([_make_order("AMD", "ord-1")])

        with patch("utils.telegram.send_message") as mock_tg:
            result = _handle_held_stops(
                broker, "sp500",
                dry_run=False, send_telegram=True,
                state_file=state_file,
            )

        assert result["resubmitted"] == ["AMD"]
        mock_tg.assert_called_once()
        msg = mock_tg.call_args[0][0]
        assert "AMD" in msg
        assert "held" in msg.lower()

    def test_no_telegram_when_suppressed(self, tmp_path: Path) -> None:
        state_file = tmp_path / "held.json"
        state_file.write_text(json.dumps({
            "GLD::sp500": {"first_seen": "2026-04-20T22:00:00", "order_id": "ord-gld"},
        }))
        broker = _make_broker([_make_order("GLD", "ord-gld")])

        with patch("utils.telegram.send_message") as mock_tg:
            _handle_held_stops(
                broker, "sp500",
                dry_run=False, send_telegram=False,
                state_file=state_file,
            )

        mock_tg.assert_not_called()


class TestDryRunBehavior:
    """dry_run=True must NOT call cancel_order but DOES update state file."""

    def test_dry_run_no_cancel_first_cycle(self, tmp_path: Path) -> None:
        state_file = tmp_path / "held.json"
        broker = _make_broker([_make_order("SLV", "ord-slv")])

        result = _handle_held_stops(
            broker, "sp500",
            dry_run=True, send_telegram=False,
            state_file=state_file,
        )

        assert result["resubmitted"] == []
        broker.cancel_order.assert_not_called()

    def test_dry_run_cancel_NOT_called_second_cycle(self, tmp_path: Path) -> None:
        state_file = tmp_path / "held.json"
        state_file.write_text(json.dumps({
            "SLV::sp500": {"first_seen": "2026-04-21T09:00:00", "order_id": "ord-slv"},
        }))
        broker = _make_broker([_make_order("SLV", "ord-slv")])

        result = _handle_held_stops(
            broker, "sp500",
            dry_run=True, send_telegram=False,
            state_file=state_file,
        )

        # In dry-run, we report resubmitted but do NOT actually call cancel
        assert result["resubmitted"] == ["SLV"]
        broker.cancel_order.assert_not_called()


class TestStaleStateCleanup:
    """State entries for tickers no longer held are removed automatically."""

    def test_stale_entry_removed(self, tmp_path: Path) -> None:
        state_file = tmp_path / "held.json"
        # NFLX was previously held but is no longer
        state_file.write_text(json.dumps({
            "NFLX::sp500": {"first_seen": "2026-04-20T10:00:00", "order_id": "ord-old"},
        }))
        # No held orders returned now
        broker = _make_broker([])

        _handle_held_stops(
            broker, "sp500",
            dry_run=False, send_telegram=False,
            state_file=state_file,
        )

        state = json.loads(state_file.read_text())
        assert "NFLX::sp500" not in state


class TestMultiTickerIndependence:
    """Multiple tickers are handled independently — first+second cycles co-exist."""

    def test_mix_first_and_second_cycle(self, tmp_path: Path) -> None:
        state_file = tmp_path / "held.json"
        # AMD is on first cycle, GLD is on second
        state_file.write_text(json.dumps({
            "GLD::sp500": {"first_seen": "2026-04-21T09:00:00", "order_id": "ord-gld"},
        }))
        broker = _make_broker([
            _make_order("AMD", "ord-amd"),   # first cycle
            _make_order("GLD", "ord-gld"),   # second cycle
        ])

        result = _handle_held_stops(
            broker, "sp500",
            dry_run=False, send_telegram=False,
            state_file=state_file,
        )

        assert "AMD" in result["newly_held"]
        assert "GLD" in result["resubmitted"]
        # Only GLD's cancel should be called
        broker.cancel_order.assert_called_once_with("ord-gld")

    def test_cancel_failure_does_not_affect_other_tickers(self, tmp_path: Path) -> None:
        state_file = tmp_path / "held.json"
        state_file.write_text(json.dumps({
            "AMD::sp500": {"first_seen": "2026-04-21T09:00:00", "order_id": "ord-amd"},
            "GLD::sp500": {"first_seen": "2026-04-21T09:00:00", "order_id": "ord-gld"},
        }))

        cancel_fail = MagicMock()
        cancel_fail.success = False
        cancel_fail.message = "broker error"
        cancel_ok = MagicMock()
        cancel_ok.success = True
        cancel_ok.message = "ok"

        broker = _make_broker([
            _make_order("AMD", "ord-amd"),
            _make_order("GLD", "ord-gld"),
        ])
        broker.cancel_order.side_effect = [cancel_fail, cancel_ok]

        result = _handle_held_stops(
            broker, "sp500",
            dry_run=False, send_telegram=False,
            state_file=state_file,
        )

        # One error, one success
        assert len(result["errors"]) == 1
        assert len(result["resubmitted"]) == 1


class TestNonStopOrdersIgnored:
    """Only stop SELL orders in held status are targeted."""

    def test_limit_buy_held_ignored(self, tmp_path: Path) -> None:
        state_file = tmp_path / "held.json"
        broker = _make_broker([
            _make_order("AMD", "ord-limit", status="held", order_type="limit", side="buy"),
        ])

        result = _handle_held_stops(
            broker, "sp500",
            dry_run=False, send_telegram=False,
            state_file=state_file,
        )

        assert result["newly_held"] == []
        assert result["resubmitted"] == []
        broker.cancel_order.assert_not_called()

    def test_non_held_stop_ignored(self, tmp_path: Path) -> None:
        state_file = tmp_path / "held.json"
        broker = _make_broker([
            _make_order("AMD", "ord-active", status="new", order_type="stop", side="sell"),
        ])

        result = _handle_held_stops(
            broker, "sp500",
            dry_run=False, send_telegram=False,
            state_file=state_file,
        )

        assert result["newly_held"] == []
        broker.cancel_order.assert_not_called()
