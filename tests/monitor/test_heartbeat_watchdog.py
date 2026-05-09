"""Tests for the schedule-aware heartbeat watchdog.

Each test freezes time via the injectable `now_utc` parameter and
provides fake heartbeat rows via `load_heartbeats_fn`.  The DB and
Telegram are never touched in these tests.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

# Ensure project root is on sys.path
_PROJECT = Path(__file__).resolve().parent.parent.parent
if str(_PROJECT) not in sys.path:
    sys.path.insert(0, str(_PROJECT))

from scripts.heartbeat_watchdog import (  # noqa: E402
    _is_service_stale,
    _load_config,
    _should_alert,
    run_watchdog,
)

# ─── Shared config / helpers ──────────────────────────────────────────────────

_CFG = _load_config()          # real config/heartbeat.json
_SVC = _CFG["services"]


def _ts(dt: datetime) -> str:
    """Format a UTC-aware datetime into the DB string format."""
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _make_row(
    service: str,
    ts: datetime,
    status: str = "completed",
    now: datetime | None = None,
) -> dict[str, Any]:
    """Build a fake heartbeat row (matches DB query output)."""
    age_hours = 0.0
    if now is not None:
        age_hours = round((now - ts).total_seconds() / 3600, 1)
    return {
        "service": service,
        "timestamp": _ts(ts),
        "status": status,
        "age_hours": age_hours,
    }


def _run(
    rows: list[dict[str, Any]],
    now_utc: datetime,
    state_file: Path,
    dry_run: bool = False,
) -> None:
    """Helper: run_watchdog with injected fake data and a no-op flip function."""
    run_watchdog(
        dry_run=dry_run,
        load_heartbeats_fn=lambda _: rows,
        flip_fn=lambda _: [],       # never flip in unit tests
        now_utc=now_utc,
        state_file=state_file,
    )


# ─── Test 1: Saturday — postclose not stale ───────────────────────────────────

def test_saturday_postclose_not_stale(tmp_path):
    """Sat 2026-04-18 14:31 UTC, postclose ran Fri 22:02 UTC → NOT stale.

    prev_expected = Fri Apr 17 22:00 UTC
    last_run (Fri 22:02) >= prev_expected − 5 min → condition 1 false → not stale.
    """
    now = datetime(2026, 4, 18, 14, 31, tzinfo=timezone.utc)
    last_run = datetime(2026, 4, 17, 22, 2, tzinfo=timezone.utc)
    row = _make_row("postclose", last_run, now=now)

    with patch("utils.telegram.send_message") as mock_tg:
        _run([row], now, tmp_path / "state.json")

    mock_tg.assert_not_called()


# ─── Test 2: Tuesday — postclose missed Monday run → stale ───────────────────

def test_monday_postclose_missed_is_stale(tmp_path):
    """Tue 2026-04-21 05:00 UTC, postclose last ran Fri 22:02 UTC → STALE.

    prev_expected = Mon Apr 20 22:00 UTC
    last_run (Fri 22:02) < Mon 21:55  AND  Tue 05:00 >= Mon 22:00 + 6h (Tue 04:00).
    """
    now = datetime(2026, 4, 21, 5, 0, tzinfo=timezone.utc)
    last_run = datetime(2026, 4, 17, 22, 2, tzinfo=timezone.utc)
    row = _make_row("postclose", last_run, now=now)

    with patch("utils.telegram.send_message", return_value=True) as mock_tg:
        _run([row], now, tmp_path / "state.json")

    mock_tg.assert_called_once()
    msg = mock_tg.call_args[0][0]
    assert "postclose" in msg
    assert "Stale services" in msg


# ─── Test 3: Monday morning — postclose not stale yet ─────────────────────────

def test_monday_morning_postclose_not_stale_yet(tmp_path):
    """Mon 2026-04-20 18:00 UTC, postclose last ran Fri 22:02 UTC → NOT stale.

    prev_expected from Mon 18:00 = Fri Apr 17 22:00 (Mon 22:00 hasn't fired).
    last_run (Fri 22:02) >= Fri 21:55 → missed=False → not stale.
    """
    now = datetime(2026, 4, 20, 18, 0, tzinfo=timezone.utc)
    last_run = datetime(2026, 4, 17, 22, 2, tzinfo=timezone.utc)
    row = _make_row("postclose", last_run, now=now)

    with patch("utils.telegram.send_message") as mock_tg:
        _run([row], now, tmp_path / "state.json")

    mock_tg.assert_not_called()


# ─── Test 4: Saturday — health-check not stale ────────────────────────────────

def test_health_check_weekly_not_stale_on_saturday(tmp_path):
    """Sat 2026-04-18 14:31 UTC, health-check ran Fri 23:00 UTC → NOT stale.

    prev_expected = Fri Apr 17 23:00 UTC (most recent Friday 23:00).
    last_run (Fri 23:00:03) >= Fri 22:55 → missed=False → not stale.
    """
    now = datetime(2026, 4, 18, 14, 31, tzinfo=timezone.utc)
    last_run = datetime(2026, 4, 17, 23, 0, 3, tzinfo=timezone.utc)
    row = _make_row("health-check", last_run, now=now)

    with patch("utils.telegram.send_message") as mock_tg:
        _run([row], now, tmp_path / "state.json")

    mock_tg.assert_not_called()


# ─── Test 5: Following Sunday — health-check missed weekly run ────────────────

def test_health_check_weekly_stale_after_missed_run(tmp_path):
    """Sun 2026-04-26 14:00 UTC, health-check last ran Apr 17 23:00 → STALE.

    prev_expected = Fri Apr 24 23:00 UTC (the Apr 24 run was missed).
    last_run (Apr 17) < Apr 24 22:55  AND  Apr 26 14:00 >= Apr 24 23:00 + 24h (Apr 25 23:00).
    """
    now = datetime(2026, 4, 26, 14, 0, tzinfo=timezone.utc)
    last_run = datetime(2026, 4, 17, 23, 0, tzinfo=timezone.utc)
    row = _make_row("health-check", last_run, now=now)

    with patch("utils.telegram.send_message", return_value=True) as mock_tg:
        _run([row], now, tmp_path / "state.json")

    mock_tg.assert_called_once()
    msg = mock_tg.call_args[0][0]
    assert "health-check" in msg


# ─── Test 6: Monday — premarket ran on time ───────────────────────────────────

def test_premarket_weekday_ok(tmp_path):
    """Mon 2026-04-20 10:00 UTC, premarket ran Mon 09:05 UTC → NOT stale.

    prev_expected = Mon Apr 20 09:00 UTC.
    last_run (09:05) >= 08:55 → missed=False → not stale.
    """
    now = datetime(2026, 4, 20, 10, 0, tzinfo=timezone.utc)
    last_run = datetime(2026, 4, 20, 9, 5, tzinfo=timezone.utc)
    row = _make_row("premarket", last_run, now=now)

    with patch("utils.telegram.send_message") as mock_tg:
        _run([row], now, tmp_path / "state.json")

    mock_tg.assert_not_called()


# ─── Test 7: Unknown service falls back to global threshold ───────────────────

def test_unknown_service_uses_global_threshold(tmp_path):
    """Unknown service, 7h since last run, off-hours → STALE (threshold=6h)."""
    now = datetime(2026, 4, 19, 4, 0, tzinfo=timezone.utc)   # Sat 04:00 UTC — off-hours
    last_run = now - timedelta(hours=7)
    row = _make_row("custom_unknown_svc", last_run, now=now)

    with patch("utils.telegram.send_message", return_value=True) as mock_tg:
        with patch("utils.market_hours.is_rth", return_value=False):
            _run([row], now, tmp_path / "state.json")

    mock_tg.assert_called_once()
    msg = mock_tg.call_args[0][0]
    assert "custom_unknown_svc" in msg


def test_unknown_service_rth_short_threshold_not_stale(tmp_path):
    """Unknown service, 1.5h since last run, during RTH → NOT stale (threshold=2h)."""
    now = datetime(2026, 4, 18, 16, 0, tzinfo=timezone.utc)  # Friday 16:00 UTC ≈ RTH
    last_run = now - timedelta(hours=1, minutes=30)
    row = _make_row("custom_unknown_svc", last_run, now=now)

    with patch("utils.telegram.send_message") as mock_tg:
        with patch("utils.market_hours.is_rth", return_value=True):
            _run([row], now, tmp_path / "state.json")

    mock_tg.assert_not_called()


# ─── Test 8: Ignored prefixes are skipped entirely ────────────────────────────

def test_ignored_prefixes_skipped(tmp_path):
    """Service 'test_foo' with 100h old timestamp → NOT in alert list (ignored)."""
    now = datetime(2026, 4, 18, 14, 0, tzinfo=timezone.utc)
    old_ts = now - timedelta(hours=100)
    rows = [
        _make_row("test_foo", old_ts, now=now),
        _make_row("verify_bar", old_ts, now=now),
        _make_row("TEST_UPPER", old_ts, now=now),   # case-insensitive
    ]

    with patch("utils.telegram.send_message") as mock_tg:
        _run(rows, now, tmp_path / "state.json")

    mock_tg.assert_not_called()


# ─── Test 9: Alert throttling suppresses repeated alerts ─────────────────────

def test_alert_throttling_suppresses_repeat(tmp_path):
    """Stale service, last alerted 2h ago (min_gap=4h) → NO alert sent."""
    now = datetime(2026, 4, 21, 5, 0, tzinfo=timezone.utc)
    last_run = datetime(2026, 4, 17, 22, 2, tzinfo=timezone.utc)   # Fri → missed Mon
    row = _make_row("postclose", last_run, now=now)

    # State: alerted 2h ago for the Mon 22:00 expected run
    prev_expected = datetime(2026, 4, 20, 22, 0, tzinfo=timezone.utc)
    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps({
        "postclose": {
            "last_alert_utc": (now - timedelta(hours=2)).isoformat(),
            "prev_expected_utc": prev_expected.isoformat(),
        }
    }))

    with patch("utils.telegram.send_message") as mock_tg:
        _run([row], now, state_file)

    mock_tg.assert_not_called()


def test_alert_throttling_fires_after_gap_expires(tmp_path):
    """Same scenario but last alerted 5h ago (min_gap=4h) → alert IS sent."""
    now = datetime(2026, 4, 21, 9, 0, tzinfo=timezone.utc)
    last_run = datetime(2026, 4, 17, 22, 2, tzinfo=timezone.utc)
    row = _make_row("postclose", last_run, now=now)

    prev_expected = datetime(2026, 4, 20, 22, 0, tzinfo=timezone.utc)
    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps({
        "postclose": {
            "last_alert_utc": (now - timedelta(hours=5)).isoformat(),
            "prev_expected_utc": prev_expected.isoformat(),
        }
    }))

    with patch("utils.telegram.send_message", return_value=True) as mock_tg:
        _run([row], now, state_file)

    mock_tg.assert_called_once()


# ─── Test 10: Escalation when prev_expected advances ─────────────────────────

def test_alert_throttling_escalates_on_new_missed_run(tmp_path):
    """Alerted 2h ago for Mon's missed run; now Tue's run is also missed → escalates."""
    # Scenario: alerted Monday 22+6=04:00 for missing Mon run.
    # Now it's Wed 05:00 — Tue 22:00 also passed without a run.
    now = datetime(2026, 4, 22, 5, 0, tzinfo=timezone.utc)    # Wednesday
    last_run = datetime(2026, 4, 17, 22, 2, tzinfo=timezone.utc)  # last ran Friday
    row = _make_row("postclose", last_run, now=now)

    # State records alert was sent when prev_expected = Mon Apr 20 22:00
    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps({
        "postclose": {
            "last_alert_utc": (now - timedelta(hours=2)).isoformat(),
            "prev_expected_utc": "2026-04-20T22:00:00+00:00",   # Mon — now stale
        }
    }))

    # Now prev_expected from Wed 05:00 = Tue Apr 21 22:00 — advanced!
    with patch("utils.telegram.send_message", return_value=True) as mock_tg:
        _run([row], now, state_file)

    mock_tg.assert_called_once()
    msg = mock_tg.call_args[0][0]
    assert "postclose" in msg


def test_alert_state_updated_after_send(tmp_path):
    """After sending alert, state file is written with new last_alert_utc."""
    now = datetime(2026, 4, 21, 5, 0, tzinfo=timezone.utc)
    last_run = datetime(2026, 4, 17, 22, 2, tzinfo=timezone.utc)
    row = _make_row("postclose", last_run, now=now)
    state_file = tmp_path / "state.json"

    with patch("utils.telegram.send_message", return_value=True):
        _run([row], now, state_file)

    state = json.loads(state_file.read_text())
    assert "postclose" in state
    assert "last_alert_utc" in state["postclose"]
    assert "prev_expected_utc" in state["postclose"]


# ─── Test 11: Holiday-aware fallback via utils.market_hours.is_rth ────────────

def test_holiday_awareness_via_market_hours_fallback(tmp_path):
    """Fallback path (unknown service) calls utils.market_hours.is_rth, not a
    deleted local _is_market_hours function."""
    now = datetime(2026, 4, 18, 15, 0, tzinfo=timezone.utc)
    last_run = now - timedelta(hours=3)   # 3h ago — only stale if RTH (2h threshold)
    row = _make_row("mystery_service", last_run, now=now)

    call_count: list[int] = [0]
    original_is_rth = None
    try:
        from utils import market_hours
        original_is_rth = market_hours.is_rth
    except ImportError:
        pytest.skip("utils.market_hours not available")

    def _mock_is_rth(dt=None):
        call_count[0] += 1
        return True  # RTH → 2h threshold → 3h is stale

    with patch("utils.market_hours.is_rth", side_effect=_mock_is_rth):
        with patch("utils.telegram.send_message", return_value=True) as mock_tg:
            _run([row], now, tmp_path / "state.json")

    # is_rth was called (used for both threshold and market_label)
    assert call_count[0] >= 1
    # Service was stale (3h > 2h RTH threshold) → alert fired
    mock_tg.assert_called_once()


# ─── Direct unit tests for _is_service_stale ─────────────────────────────────

def test_is_service_stale_direct_not_stale():
    """Direct function test: ran on time → not stale."""
    now = datetime(2026, 4, 18, 14, 31, tzinfo=timezone.utc)
    last_run = datetime(2026, 4, 17, 22, 2, tzinfo=timezone.utc)
    cfg = {"expected_cron": "0 22 * * 1-5", "threshold_hours": 6}
    stale, prev = _is_service_stale(last_run, now, cfg)
    assert not stale
    assert prev == datetime(2026, 4, 17, 22, 0, tzinfo=timezone.utc)


def test_is_service_stale_direct_is_stale():
    """Direct function test: missed Monday run, now Tue 05:00 → stale."""
    now = datetime(2026, 4, 21, 5, 0, tzinfo=timezone.utc)
    last_run = datetime(2026, 4, 17, 22, 2, tzinfo=timezone.utc)
    cfg = {"expected_cron": "0 22 * * 1-5", "threshold_hours": 6}
    stale, prev = _is_service_stale(last_run, now, cfg)
    assert stale
    assert prev == datetime(2026, 4, 20, 22, 0, tzinfo=timezone.utc)


# ─── Direct unit tests for _should_alert ─────────────────────────────────────

def test_should_alert_no_prior_state():
    """No prior alert → always alert."""
    now = datetime(2026, 4, 21, 5, 0, tzinfo=timezone.utc)
    prev = datetime(2026, 4, 20, 22, 0, tzinfo=timezone.utc)
    assert _should_alert("postclose", prev, now, {}, min_gap_hours=4) is True


def test_should_alert_within_gap_same_prev():
    """Within gap window + same prev_expected → suppress."""
    now = datetime(2026, 4, 21, 5, 0, tzinfo=timezone.utc)
    prev = datetime(2026, 4, 20, 22, 0, tzinfo=timezone.utc)
    state = {
        "postclose": {
            "last_alert_utc": (now - timedelta(hours=2)).isoformat(),
            "prev_expected_utc": prev.isoformat(),
        }
    }
    assert _should_alert("postclose", prev, now, state, min_gap_hours=4) is False


def test_should_alert_prev_advanced_escalates():
    """prev_expected advanced → escalate regardless of gap."""
    now = datetime(2026, 4, 22, 5, 0, tzinfo=timezone.utc)
    new_prev = datetime(2026, 4, 21, 22, 0, tzinfo=timezone.utc)   # Tuesday
    state = {
        "postclose": {
            "last_alert_utc": (now - timedelta(hours=2)).isoformat(),
            "prev_expected_utc": "2026-04-20T22:00:00+00:00",   # Monday (older)
        }
    }
    assert _should_alert("postclose", new_prev, now, state, min_gap_hours=4) is True


# ─── Dry-run flag ─────────────────────────────────────────────────────────────

def test_dry_run_prints_and_no_telegram(tmp_path, capsys):
    """--dry-run prints but does NOT send Telegram."""
    now = datetime(2026, 4, 21, 5, 0, tzinfo=timezone.utc)
    last_run = datetime(2026, 4, 17, 22, 2, tzinfo=timezone.utc)
    row = _make_row("postclose", last_run, now=now)

    with patch("utils.telegram.send_message") as mock_tg:
        _run([row], now, tmp_path / "state.json", dry_run=True)

    mock_tg.assert_not_called()
    captured = capsys.readouterr()
    assert "DRY RUN" in captured.out
    assert "postclose" in captured.out


# ─── Config loading ───────────────────────────────────────────────────────────

def test_config_loads_expected_keys():
    """config/heartbeat.json loads all required keys."""
    cfg = _load_config()
    assert "services" in cfg
    assert "postclose" in cfg["services"]
    assert "premarket" in cfg["services"]
    assert "health-check" in cfg["services"]
    assert "ignored_prefixes" in cfg
    assert "default_threshold_hours" in cfg
    assert "rth_threshold_hours" in cfg
    assert "min_alert_gap_hours" in cfg
    for svc, scfg in cfg["services"].items():
        assert "expected_cron" in scfg, f"{svc} missing expected_cron"
        assert "threshold_hours" in scfg, f"{svc} missing threshold_hours"


# ─── Test A: Source regression guard for sync_protective typo ─────────────────

def test_sync_protective_orders_typo_fix_source_inspection():
    """Regression guard: policy.should_skip() heartbeat must use 'sync_protective_orders',
    NOT the old typo 'sync_protective'.

    If this test breaks, the typo introduced in commit 39023372 has been re-introduced.
    """
    import ast
    source_path = Path(__file__).resolve().parent.parent.parent / "scripts" / "sync_protective_orders.py"
    source = source_path.read_text()

    # The fixed name MUST appear
    assert '_hb("sync_protective_orders", "skipped"' in source, (
        "Expected '_hb(\"sync_protective_orders\", \"skipped\"' in sync_protective_orders.py "
        "— the policy.should_skip() heartbeat call uses the wrong service name"
    )

    # The typo MUST NOT appear
    assert '_hb("sync_protective", "skipped"' not in source, (
        "Found typo '_hb(\"sync_protective\", \"skipped\"' in sync_protective_orders.py "
        "— this creates an orphaned heartbeat row when market is disabled"
    )


# ─── Test B: Unconfigured service does NOT escalate every cycle ───────────────

def test_unconfigured_service_does_not_escalate_every_cycle(tmp_path):
    """Regression guard for heartbeat_watchdog escalation spam.

    For services NOT in heartbeat.json config (fallback path), the escalation
    override must NOT fire on every 15-min watchdog cycle.  It should fire once,
    then be throttled by min_alert_gap_hours.

    Setup: service 'some_legacy_thing' has a very stale heartbeat (never updated)
    and is not in heartbeat.json config.  Two watchdog cycles 15 min apart.
    Expected: Telegram called exactly ONCE (second cycle is throttled).

    Note: off-hours threshold is 6h (default_threshold_hours from heartbeat.json).
    stale_ts must be >6h before T0 so the row is stale at both cycles.
    """
    T0 = datetime(2026, 5, 1, 3, 0, tzinfo=timezone.utc)
    T1 = T0 + timedelta(minutes=15)

    # Heartbeat row: 7h stale at T0 — well past the 6h off-hours threshold
    stale_ts = T0 - timedelta(hours=7)
    row = _make_row("some_legacy_thing", stale_ts, status="ok", now=T0)
    rows = [row]

    state_file = tmp_path / "state.json"
    call_count: list[int] = [0]

    def _mock_send(msg: str) -> bool:
        call_count[0] += 1
        return True

    with patch("utils.telegram.send_message", side_effect=_mock_send):
        with patch("utils.market_hours.is_rth", return_value=False):
            # Cycle 1 — T0 — should alert (first time)
            run_watchdog(
                dry_run=False,
                load_heartbeats_fn=lambda _: rows,
                flip_fn=lambda _: [],
                now_utc=T0,
                state_file=state_file,
            )
            # Cycle 2 — T0+15min — must be THROTTLED (same stale row, gap<4h)
            run_watchdog(
                dry_run=False,
                load_heartbeats_fn=lambda _: rows,
                flip_fn=lambda _: [],
                now_utc=T1,
                state_file=state_file,
            )

    assert call_count[0] == 1, (
        f"Expected Telegram called exactly 1 time across 2 watchdog cycles for "
        f"unconfigured service, but got {call_count[0]}. "
        "The escalation override is firing every cycle (Bug #3 regression)."
    )


# ─── Test C: Unconfigured service alerts again after min_gap expires ──────────

def test_unconfigured_service_alerts_again_after_min_gap(tmp_path):
    """After min_alert_gap_hours (4h) the watchdog SHOULD re-alert.

    Setup: same as Test B but third call at T0+5h (past gap window).
    Expected: Telegram called TWICE total (initial alert + after-gap alert).
    """
    T0 = datetime(2026, 5, 1, 3, 0, tzinfo=timezone.utc)
    T1 = T0 + timedelta(minutes=15)
    T5 = T0 + timedelta(hours=5)   # past min_alert_gap_hours=4

    # 7h stale at T0 — exceeds the 6h off-hours threshold at all three cycles
    stale_ts = T0 - timedelta(hours=7)
    # Row timestamp stays constant — heartbeat was never refreshed
    row = _make_row("some_legacy_thing", stale_ts, status="ok", now=T0)
    rows = [row]

    state_file = tmp_path / "state.json"
    call_count: list[int] = [0]

    def _mock_send(msg: str) -> bool:
        call_count[0] += 1
        return True

    with patch("utils.telegram.send_message", side_effect=_mock_send):
        with patch("utils.market_hours.is_rth", return_value=False):
            # Cycle 1 — T0 — alert fires
            run_watchdog(
                dry_run=False,
                load_heartbeats_fn=lambda _: rows,
                flip_fn=lambda _: [],
                now_utc=T0,
                state_file=state_file,
            )
            # Cycle 2 — T0+15min — throttled
            run_watchdog(
                dry_run=False,
                load_heartbeats_fn=lambda _: rows,
                flip_fn=lambda _: [],
                now_utc=T1,
                state_file=state_file,
            )
            # Cycle 3 — T0+5h — gap expired → alert fires again
            run_watchdog(
                dry_run=False,
                load_heartbeats_fn=lambda _: rows,
                flip_fn=lambda _: [],
                now_utc=T5,
                state_file=state_file,
            )

    assert call_count[0] == 2, (
        f"Expected Telegram called exactly 2 times (initial + after-gap), "
        f"but got {call_count[0]}. "
        "Either the first alert was suppressed or the second was not fired after gap expiry."
    )
