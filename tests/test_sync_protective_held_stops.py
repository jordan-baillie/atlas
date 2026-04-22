"""Tests for sync_protective_orders._handle_held_stops — new retry-cap behaviour.

Contract under test (added by parallel worker):
  - _HELD_MAX_RETRIES = 4
  - _maybe_alert_stuck(ticker, market_id, *, reason, state, key, send_telegram, permanent, now_iso) -> bool
  - _handle_held_stops: retry-cap dispatch loop, account-level reject fast-path

State entry shape:
  {
    "first_seen": "<iso>",
    "order_id": "<id>",
    "retry_count": <int>,
    "last_alerted_date": "<YYYY-MM-DD>",
    "permanently_skipped": <bool>,
    "skip_reason": "<string>"
  }
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.sync_protective_orders import (
    _handle_held_stops,
    _HELD_MAX_RETRIES,
    _maybe_alert_stuck,
    _load_held_state,
)


# ── Minimal helpers ───────────────────────────────────────────────────────────


class _Order:
    """Minimal order-like object matching broker.get_open_orders() return shape."""

    def __init__(
        self,
        ticker: str,
        order_id: str,
        status: str = "held",
        order_type: str = "stop",
        side: str = "sell",
        reject_reason: str = "",
    ) -> None:
        self.ticker = ticker
        self.order_id = order_id
        self.raw: dict = {
            "status": status,
            "order_type": order_type,
            "side": side,
        }
        if reject_reason:
            self.raw["reject_reason"] = reject_reason


def _mk_broker(held_orders=None, cancel_ok: bool = True):
    b = MagicMock()
    b.get_open_orders.return_value = held_orders or []
    cancel = MagicMock()
    cancel.success = cancel_ok
    cancel.message = "" if cancel_ok else "reject"
    b.cancel_order.return_value = cancel
    return b


def _write_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2))


def _read_state(path: Path) -> dict:
    if not path.exists():
        return {}
    return json.loads(path.read_text())


# ── Sanity: constant is correct value ────────────────────────────────────────


def test_held_max_retries_constant() -> None:
    assert _HELD_MAX_RETRIES == 4


# ═══════════════════════════════════════════════════════════════
# 1. First observation → record state, no cancel, newly_held populated
# ═══════════════════════════════════════════════════════════════


class TestFirstObservation:
    def test_first_observation_records_state(self, tmp_path: Path) -> None:
        """First held order for CHTR → recorded in state, cancel NOT called."""
        state_file = tmp_path / "s.json"
        broker = _mk_broker([_Order("CHTR", "o1")])

        result = _handle_held_stops(
            broker, "sp500",
            state_file=state_file,
            send_telegram=False,
        )

        # Return values
        assert result["newly_held"] == ["CHTR"]
        assert result["resubmitted"] == []
        broker.cancel_order.assert_not_called()

        # State file shape
        state = _read_state(state_file)
        assert "CHTR::sp500" in state
        entry = state["CHTR::sp500"]
        assert entry["retry_count"] == 0
        assert entry["permanently_skipped"] is False
        assert entry["order_id"] == "o1"
        assert "first_seen" in entry


# ═══════════════════════════════════════════════════════════════
# 2. Second observation → cancel, retry_count incremented, entry KEPT
# ═══════════════════════════════════════════════════════════════


class TestSecondObservation:
    def test_second_observation_cancels_and_increments_retry(self, tmp_path: Path) -> None:
        """On second cycle: cancel_order called, retry_count → 1, entry retained."""
        state_file = tmp_path / "s.json"
        _write_state(state_file, {
            "CHTR::sp500": {
                "first_seen": "2026-04-22T00:00:00",
                "order_id": "o1",
                "retry_count": 0,
                "last_alerted_date": "",
                "permanently_skipped": False,
                "skip_reason": "",
            }
        })
        broker = _mk_broker([_Order("CHTR", "o1")])

        result = _handle_held_stops(
            broker, "sp500",
            state_file=state_file,
            send_telegram=False,
        )

        # cancel called with the order id
        broker.cancel_order.assert_called_once()
        call_args = broker.cancel_order.call_args
        # could be positional or keyword
        called_id = call_args[0][0] if call_args[0] else call_args[1].get("order_id", "")
        assert called_id == "o1", f"Expected cancel_order('o1'), got args={call_args}"

        assert result["resubmitted"] == ["CHTR"]

        # Entry KEPT (not popped), retry_count incremented
        state = _read_state(state_file)
        assert "CHTR::sp500" in state, "State entry must NOT be removed after retry"
        assert state["CHTR::sp500"]["retry_count"] == 1
        assert state["CHTR::sp500"]["permanently_skipped"] is False


# ═══════════════════════════════════════════════════════════════
# 3. retry_count >= 4 → permanently_skipped, no cancel, telegram
# ═══════════════════════════════════════════════════════════════


class TestRetryCap:
    def test_retry_cap_stops_resubmission_and_alerts(self, tmp_path: Path) -> None:
        """When retry_count == _HELD_MAX_RETRIES: no cancel, permanently_skipped=True, telegram."""
        state_file = tmp_path / "s.json"
        _write_state(state_file, {
            "CHTR::sp500": {
                "first_seen": "2026-04-18T00:00:00",
                "order_id": "o1",
                "retry_count": _HELD_MAX_RETRIES,  # == 4
                "last_alerted_date": "",
                "permanently_skipped": False,
                "skip_reason": "",
            }
        })
        broker = _mk_broker([_Order("CHTR", "o1")])

        with patch("utils.telegram.send_message") as mock_send:
            result = _handle_held_stops(
                broker, "sp500",
                state_file=state_file,
                send_telegram=True,
            )

        # cancel NOT called
        broker.cancel_order.assert_not_called()

        # State transitions to permanently skipped
        state = _read_state(state_file)
        assert "CHTR::sp500" in state
        entry = state["CHTR::sp500"]
        assert entry["permanently_skipped"] is True
        assert entry["skip_reason"] == "max_retries_4"

        # Telegram sent exactly once
        assert mock_send.called is True
        assert mock_send.call_count == 1


# ═══════════════════════════════════════════════════════════════
# 4. Permanently skipped: telegram once-per-day throttle
# ═══════════════════════════════════════════════════════════════


class TestPermanentlySkippedThrottle:
    def test_permanently_skipped_alerts_once_per_day(self, tmp_path: Path) -> None:
        """With last_alerted_date == today → no alert. With stale date → alert."""
        state_file = tmp_path / "s.json"

        # Case A: alerted today → no telegram
        today = date.today().isoformat()  # e.g. "2026-04-22"
        _write_state(state_file, {
            "CHTR::sp500": {
                "first_seen": "2026-04-18T00:00:00",
                "order_id": "o1",
                "retry_count": _HELD_MAX_RETRIES,
                "last_alerted_date": today,
                "permanently_skipped": True,
                "skip_reason": "max_retries_4",
            }
        })
        broker = _mk_broker([_Order("CHTR", "o1")])

        with patch("utils.telegram.send_message") as mock_send:
            _handle_held_stops(
                broker, "sp500",
                state_file=state_file,
                send_telegram=True,
                now_iso=f"{today}T09:00:00",
            )
        assert mock_send.called is False, "Should NOT alert when last_alerted_date == today"

        # Case B: stale date → telegram
        _write_state(state_file, {
            "CHTR::sp500": {
                "first_seen": "2026-04-18T00:00:00",
                "order_id": "o1",
                "retry_count": _HELD_MAX_RETRIES,
                "last_alerted_date": "2026-04-20",
                "permanently_skipped": True,
                "skip_reason": "max_retries_4",
            }
        })

        with patch("utils.telegram.send_message") as mock_send:
            _handle_held_stops(
                broker, "sp500",
                state_file=state_file,
                send_telegram=True,
                now_iso=f"{today}T09:00:00",
            )
        assert mock_send.called is True, "Should alert when last_alerted_date is stale"


# ═══════════════════════════════════════════════════════════════
# 5. Account-level reject (pdt/short_sale/htb/insufficient_bp) → fast-path skip
# ═══════════════════════════════════════════════════════════════


class TestAccountLevelReject:
    def test_account_level_reject_skipped_immediately(self, tmp_path: Path) -> None:
        """PDT reject → permanently_skipped immediately, cancel NOT called."""
        state_file = tmp_path / "s.json"
        broker = _mk_broker([_Order("CHTR", "o1", reject_reason="pdt_rule_violation")])

        with patch("utils.telegram.send_message") as mock_send:
            result = _handle_held_stops(
                broker, "sp500",
                state_file=state_file,
                send_telegram=True,
            )

        broker.cancel_order.assert_not_called()
        assert result["errors"] == ["CHTR"]

        state = _read_state(state_file)
        assert "CHTR::sp500" in state
        entry = state["CHTR::sp500"]
        assert entry["permanently_skipped"] is True
        assert "pdt" in entry["skip_reason"].lower()
        assert mock_send.called is True

    @pytest.mark.parametrize("reason,token", [
        ("short_sale_restriction", "short"),
        ("hard_to_borrow", "htb"),
        ("insufficient_buying_power", "insufficient_bp"),
        ("htb_order", "htb"),
    ])
    def test_various_account_rejects_trigger_fast_path(
        self, tmp_path: Path, reason: str, token: str
    ) -> None:
        """Various account-level reject reasons all trigger fast-path permanently_skipped."""
        state_file = tmp_path / "s.json"
        broker = _mk_broker([_Order("ON", "o2", reject_reason=reason)])

        with patch("utils.telegram.send_message"):
            result = _handle_held_stops(
                broker, "sp500",
                state_file=state_file,
                send_telegram=True,
            )

        broker.cancel_order.assert_not_called()
        # Either errors or the state is permanently skipped
        state = _read_state(state_file)
        entry = state.get("ON::sp500", {})
        assert entry.get("permanently_skipped") is True, (
            f"Expected permanently_skipped=True for reject_reason={reason!r}"
        )


# ═══════════════════════════════════════════════════════════════
# 6. Resolved ticker (no longer held) → state entry removed
# ═══════════════════════════════════════════════════════════════


class TestResolvedHeld:
    def test_resolved_held_clears_state(self, tmp_path: Path) -> None:
        """When a ticker is no longer in held orders → state entry removed."""
        state_file = tmp_path / "s.json"
        _write_state(state_file, {
            "CHTR::sp500": {
                "first_seen": "2026-04-21T10:00:00",
                "order_id": "o1",
                "retry_count": 1,
                "last_alerted_date": "",
                "permanently_skipped": False,
                "skip_reason": "",
            }
        })
        # Broker returns NO held orders (order resolved itself)
        broker = _mk_broker([])

        _handle_held_stops(
            broker, "sp500",
            state_file=state_file,
            send_telegram=False,
        )

        state = _read_state(state_file)
        assert "CHTR::sp500" not in state, (
            "Resolved ticker must be cleared from state"
        )


# ═══════════════════════════════════════════════════════════════
# 7. Dry-run: no cancel, no state write
# ═══════════════════════════════════════════════════════════════


class TestDryRun:
    def test_dry_run_no_cancel_no_state_write(self, tmp_path: Path) -> None:
        """dry_run=True: cancel NOT called, state file unchanged from pre-populated."""
        state_file = tmp_path / "s.json"
        initial_state = {
            "CHTR::sp500": {
                "first_seen": "2026-04-22T00:00:00",
                "order_id": "o1",
                "retry_count": 0,
                "last_alerted_date": "",
                "permanently_skipped": False,
                "skip_reason": "",
            }
        }
        _write_state(state_file, initial_state)

        broker = _mk_broker([_Order("CHTR", "o1")])

        _handle_held_stops(
            broker, "sp500",
            state_file=state_file,
            send_telegram=False,
            dry_run=True,
        )

        broker.cancel_order.assert_not_called()

        # State file should be unchanged from pre-populated
        state_after = _read_state(state_file)
        assert state_after == initial_state, (
            f"dry_run must not modify state file.\n"
            f"  Before: {initial_state}\n"
            f"  After:  {state_after}"
        )


# ═══════════════════════════════════════════════════════════════
# 8. _maybe_alert_stuck: direct unit tests
# ═══════════════════════════════════════════════════════════════


class TestMaybeAlertStuck:
    def test_maybe_alert_stuck_returns_bool(self, tmp_path: Path) -> None:
        """_maybe_alert_stuck must return a bool."""
        state = {}
        result = _maybe_alert_stuck(
            "CHTR", "sp500",
            reason="max_retries_4",
            state=state,
            key="CHTR::sp500",
            send_telegram=False,
            permanent=True,
            now_iso="2026-04-22T09:00:00",
        )
        assert isinstance(result, bool)

    def test_maybe_alert_stuck_no_telegram_when_false(self, tmp_path: Path) -> None:
        """send_telegram=False → no utils.telegram.send_message call."""
        state: dict = {}
        with patch("utils.telegram.send_message") as mock_send:
            _maybe_alert_stuck(
                "CHTR", "sp500",
                reason="max_retries_4",
                state=state,
                key="CHTR::sp500",
                send_telegram=False,
                permanent=True,
                now_iso="2026-04-22T09:00:00",
            )
        assert mock_send.called is False
