"""Tests for scripts/healthcheck_pipelines.py.

All DB and Telegram interactions are mocked — no live connections.
Weekend-aware and cooldown logic validated with injected timestamps.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ── path bootstrap ─────────────────────────────────────────────────────────────
_ATLAS_ROOT = Path(__file__).resolve().parent.parent
if str(_ATLAS_ROOT) not in sys.path:
    sys.path.insert(0, str(_ATLAS_ROOT))

from scripts.healthcheck_pipelines import (
    ALERT_COOLDOWN_HOURS,
    PIPELINES,
    _build_alert_message,
    _check_pipeline,
    _get_last_fresh_from_db,
    _get_last_fresh_from_logfile,
    _is_weekend_skip,
    _load_state,
    _parse_timestamp,
    _save_state,
    run_once,
)


# ── Helpers ────────────────────────────────────────────────────────────────────

# Wednesday 2026-04-29 04:00:00 UTC (weekday=2)
_WEDNESDAY = datetime(2026, 4, 29, 4, 0, 0, tzinfo=timezone.utc)

# Sunday 2026-04-26 04:00:00 UTC (weekday=6)
_SUNDAY = datetime(2026, 4, 26, 4, 0, 0, tzinfo=timezone.utc)

# Friday 2026-04-24 23:00:00 UTC (weekday=4)
_FRIDAY = datetime(2026, 4, 24, 23, 0, 0, tzinfo=timezone.utc)


def _make_pipeline(
    name: str = "test_pipeline",
    source: str = "sqlite",
    query: str = "SELECT MAX(ts) FROM test_tbl",
    threshold_days: int = 2,
    weekday_only: bool = False,
) -> dict:
    return {
        "name": name,
        "source": source,
        "query": query,
        "threshold_days": threshold_days,
        "weekday_only": weekday_only,
    }


def _db_returning(value: str | None):
    """Context manager that mocks get_db() to return a DB with a single value."""
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE test_tbl (ts TEXT)")
    if value:
        conn.execute("INSERT INTO test_tbl VALUES (?)", (value,))
        conn.commit()

    @contextmanager
    def _fake_get_db(*args, **kwargs):
        yield conn

    return patch("scripts.healthcheck_pipelines.get_db" if False else
                 "db.atlas_db.get_db",  # the real import path used inside the module
                 _fake_get_db)


def _patch_get_db_returning(value: str | None):
    """Patch _get_last_fresh_from_db to return a parsed timestamp directly."""
    from scripts import healthcheck_pipelines as hp
    ts = hp._parse_timestamp(value)
    return patch.object(hp, "_get_last_fresh_from_db", return_value=ts)


def _patch_send_message():
    """Patch Telegram send_message; return the mock."""
    return patch("utils.telegram.send_message", return_value=True)


# ── Tests ──────────────────────────────────────────────────────────────────────

class TestAllFreshNoAlert:
    """All pipelines report fresh timestamps — no alert fires."""

    def test_all_fresh_no_alert(self, tmp_path: Path):
        """When every pipeline is within threshold, exit 0 and no Telegram call."""
        # 1 hour ago is well within 2-day threshold
        fresh_ts = (_WEDNESDAY - timedelta(hours=1)).isoformat()

        from scripts import healthcheck_pipelines as hp

        # Patch data retrieval for ALL pipelines
        def _always_fresh(pipeline, atlas_root, now):
            return False, _WEDNESDAY - timedelta(hours=1), 1 / 24

        state_file = tmp_path / "state.json"

        with patch.object(hp, "_check_pipeline", side_effect=_always_fresh), \
             patch("utils.telegram.send_message") as mock_tg:
            rc = hp.run_once(
                quiet=True,
                no_alert=False,
                state_path=state_file,
                _now=_WEDNESDAY,
            )

        assert rc == 0
        mock_tg.assert_not_called()


class TestSignalsStale:
    """Stale signals pipeline triggers an alert."""

    def test_signals_stale_3_days_alerts(self, tmp_path: Path):
        """signals_written_today stale 3d → fires Telegram alert, exit 1."""
        from scripts import healthcheck_pipelines as hp

        # 3 days ago (> 2-day threshold)
        stale_dt = _WEDNESDAY - timedelta(days=3)
        stale_pipeline = _make_pipeline(
            name="signals_written_today",
            threshold_days=2,
            weekday_only=True,
        )

        def _check(pipeline, atlas_root, now):
            if pipeline["name"] == "signals_written_today":
                return True, stale_dt, 3.0
            return False, now - timedelta(hours=1), 1 / 24

        state_file = tmp_path / "state.json"

        with patch.object(hp, "_check_pipeline", side_effect=_check), \
             patch("utils.telegram.send_message", return_value=True) as mock_tg:
            rc = hp.run_once(
                quiet=True,
                no_alert=False,
                state_path=state_file,
                _now=_WEDNESDAY,
                pipelines=[stale_pipeline],
            )

        assert rc == 1
        mock_tg.assert_called_once()
        call_text = mock_tg.call_args[0][0]
        assert "signals_written_today" in call_text
        assert "PIPELINE STALENESS" in call_text


class TestWeekendSkip:
    """weekday_only pipelines are skipped on weekends."""

    def test_weekend_skips_weekday_only_pipelines(self, tmp_path: Path):
        """Sunday: signals_written_today stale since Friday — no alert because weekend."""
        from scripts import healthcheck_pipelines as hp

        # Pipeline is weekday_only=True
        pipeline = _make_pipeline(
            name="signals_written_today",
            source="sqlite",
            threshold_days=2,
            weekday_only=True,
        )

        # _is_weekend_skip should return True for Sunday
        assert _is_weekend_skip(pipeline, _SUNDAY) is True

        # _check_pipeline should return (False, None, 0.0) on weekend
        # We mock _get_last_fresh to return Friday (2 days ago)
        with patch.object(hp, "_get_last_fresh", return_value=_FRIDAY):
            is_stale, _, days_ago = _check_pipeline(pipeline, _ATLAS_ROOT, _SUNDAY)

        assert is_stale is False
        assert days_ago == 0.0

    def test_non_weekday_only_pipeline_still_checked_on_weekend(self, tmp_path: Path):
        """weekday_only=False pipelines are checked even on Sunday."""
        pipeline = _make_pipeline(
            name="regime_observed_today",
            threshold_days=2,
            weekday_only=False,
        )
        assert _is_weekend_skip(pipeline, _SUNDAY) is False


class TestAlertCooldown:
    """6h cooldown prevents repeated alerts."""

    def test_alert_cooldown_skips_repeat_within_6h(self, tmp_path: Path):
        """Second run within 6h does not re-alert (cooldown active)."""
        from scripts import healthcheck_pipelines as hp

        pipeline = _make_pipeline(
            name="signals_written_today", threshold_days=2, weekday_only=True
        )
        stale_dt = _WEDNESDAY - timedelta(days=3)

        # Simulate a previous alert 2h ago
        first_alert_time = _WEDNESDAY - timedelta(hours=2)
        state_file = tmp_path / "state.json"
        state = {"last_alerted_at": {"signals_written_today": first_alert_time.isoformat()}}
        state_file.write_text(json.dumps(state))

        def _check(pipeline, atlas_root, now):
            return True, stale_dt, 3.0

        with patch.object(hp, "_check_pipeline", side_effect=_check), \
             patch("utils.telegram.send_message", return_value=True) as mock_tg:
            rc = hp.run_once(
                quiet=True,
                no_alert=False,
                state_path=state_file,
                _now=_WEDNESDAY,
                pipelines=[pipeline],
            )

        # Exit 1 (still stale) but no Telegram call (within cooldown)
        assert rc == 1
        mock_tg.assert_not_called()

    def test_alert_after_cooldown_fires_again(self, tmp_path: Path):
        """Alert 7h after previous alert — cooldown expired, fires again."""
        from scripts import healthcheck_pipelines as hp

        pipeline = _make_pipeline(
            name="signals_written_today", threshold_days=2, weekday_only=True
        )
        stale_dt = _WEDNESDAY - timedelta(days=3)

        # Previous alert was 7h ago (> 6h cooldown)
        first_alert_time = _WEDNESDAY - timedelta(hours=7)
        state_file = tmp_path / "state.json"
        state = {"last_alerted_at": {"signals_written_today": first_alert_time.isoformat()}}
        state_file.write_text(json.dumps(state))

        def _check(pipeline, atlas_root, now):
            return True, stale_dt, 3.0

        with patch.object(hp, "_check_pipeline", side_effect=_check), \
             patch("utils.telegram.send_message", return_value=True) as mock_tg:
            rc = hp.run_once(
                quiet=True,
                no_alert=False,
                state_path=state_file,
                _now=_WEDNESDAY,
                pipelines=[pipeline],
            )

        assert rc == 1
        mock_tg.assert_called_once()


class TestMissingTable:
    """Empty or missing DB state is treated as stale."""

    def test_missing_table_treated_as_stale(self, tmp_path: Path):
        """DB query that returns NULL (empty table) → is_stale=True."""
        from scripts import healthcheck_pipelines as hp

        pipeline = _make_pipeline(
            name="test_no_data",
            source="sqlite",
            query="SELECT MAX(ts) FROM empty_tbl",
            threshold_days=2,
        )

        # Mock _get_last_fresh_from_db to return None (empty/missing)
        with patch.object(hp, "_get_last_fresh_from_db", return_value=None):
            is_stale, last_fresh, days_ago = _check_pipeline(
                pipeline, _ATLAS_ROOT, _WEDNESDAY
            )

        assert is_stale is True
        assert last_fresh is None
        assert days_ago == float("inf")

    def test_db_operational_error_treated_as_stale(self):
        """OperationalError in DB query → returns None (treated as stale)."""
        # _get_last_fresh_from_db should catch OperationalError and return None
        with patch("db.atlas_db.get_db") as mock_get_db:
            mock_conn = MagicMock()
            mock_conn.__enter__ = lambda s: mock_conn
            mock_conn.__exit__ = MagicMock(return_value=False)
            mock_conn.execute.side_effect = sqlite3.OperationalError("no such column: synced_at")
            mock_get_db.return_value = mock_conn

            result = _get_last_fresh_from_db("SELECT MAX(synced_at) FROM market_state")

        assert result is None


class TestStateFileCorrupted:
    """Corrupt state file resets cleanly without error."""

    def test_state_file_corrupted_resets_cleanly(self, tmp_path: Path):
        """Corrupt JSON in state file → treated as empty state, no crash."""
        state_file = tmp_path / "state.json"
        state_file.write_text("{ this is not valid JSON !!!")

        state = _load_state(state_file)
        assert state == {"last_alerted_at": {}}

    def test_state_file_missing_resets_cleanly(self, tmp_path: Path):
        """Missing state file → treated as empty state."""
        state_file = tmp_path / "nonexistent_state.json"
        state = _load_state(state_file)
        assert state == {"last_alerted_at": {}}

    def test_state_file_wrong_type_resets_cleanly(self, tmp_path: Path):
        """State file containing a JSON array → treated as empty state."""
        state_file = tmp_path / "state.json"
        state_file.write_text("[1, 2, 3]")
        state = _load_state(state_file)
        assert state == {"last_alerted_at": {}}


class TestNoAlertFlag:
    """--no-alert suppresses Telegram but still exits 1 on stale."""

    def test_no_alert_flag(self, tmp_path: Path):
        """--no-alert: no Telegram call but exit code 1 when stale."""
        from scripts import healthcheck_pipelines as hp

        pipeline = _make_pipeline(name="signals_written_today", threshold_days=2)

        def _check(pipeline, atlas_root, now):
            return True, now - timedelta(days=5), 5.0

        state_file = tmp_path / "state.json"

        with patch.object(hp, "_check_pipeline", side_effect=_check), \
             patch("utils.telegram.send_message", return_value=True) as mock_tg:
            rc = hp.run_once(
                quiet=True,
                no_alert=True,  # <-- no-alert mode
                state_path=state_file,
                _now=_WEDNESDAY,
                pipelines=[pipeline],
            )

        assert rc == 1
        mock_tg.assert_not_called()


class TestConsolidatedAlert:
    """Multiple stale pipelines produce exactly one Telegram message."""

    def test_multiple_stale_consolidated_in_single_alert(self, tmp_path: Path):
        """3 stale pipelines → exactly 1 Telegram call (not 3)."""
        from scripts import healthcheck_pipelines as hp

        pipelines = [
            _make_pipeline(name="signals_written_today", threshold_days=2),
            _make_pipeline(name="experiment_generated_today", threshold_days=3),
            _make_pipeline(name="regime_observed_today", threshold_days=2),
        ]

        def _check(pipeline, atlas_root, now):
            return True, now - timedelta(days=5), 5.0

        state_file = tmp_path / "state.json"

        with patch.object(hp, "_check_pipeline", side_effect=_check), \
             patch("utils.telegram.send_message", return_value=True) as mock_tg:
            rc = hp.run_once(
                quiet=True,
                no_alert=False,
                state_path=state_file,
                _now=_WEDNESDAY,
                pipelines=pipelines,
            )

        assert rc == 1
        # EXACTLY one call, not three
        assert mock_tg.call_count == 1
        call_text = mock_tg.call_args[0][0]
        # All 3 pipelines should appear in the single message
        assert "signals_written_today" in call_text
        assert "experiment_generated_today" in call_text
        assert "regime_observed_today" in call_text
        assert "3 pipelines" in call_text


class TestExitCode:
    """Exit code semantics."""

    def test_exit_code_1_when_any_stale(self, tmp_path: Path):
        """Even a single stale pipeline → exit 1."""
        from scripts import healthcheck_pipelines as hp

        pipelines = [
            _make_pipeline(name="signals_written_today", threshold_days=2),
            _make_pipeline(name="experiment_generated_today", threshold_days=3),
        ]

        call_count = [0]

        def _check(pipeline, atlas_root, now):
            call_count[0] += 1
            # Only the first pipeline is stale
            if pipeline["name"] == "signals_written_today":
                return True, now - timedelta(days=3), 3.0
            return False, now - timedelta(hours=1), 1 / 24

        state_file = tmp_path / "state.json"

        with patch.object(hp, "_check_pipeline", side_effect=_check), \
             patch("utils.telegram.send_message", return_value=True):
            rc = hp.run_once(
                quiet=True,
                no_alert=True,
                state_path=state_file,
                _now=_WEDNESDAY,
                pipelines=pipelines,
            )

        assert rc == 1
        assert call_count[0] == 2  # both pipelines checked

    def test_exit_code_0_when_all_fresh(self, tmp_path: Path):
        """All pipelines fresh → exit 0."""
        from scripts import healthcheck_pipelines as hp

        pipeline = _make_pipeline(name="signals_written_today", threshold_days=2)

        def _check(p, atlas_root, now):
            return False, now - timedelta(hours=12), 0.5

        state_file = tmp_path / "state.json"

        with patch.object(hp, "_check_pipeline", side_effect=_check), \
             patch("utils.telegram.send_message") as mock_tg:
            rc = hp.run_once(
                quiet=True,
                state_path=state_file,
                _now=_WEDNESDAY,
                pipelines=[pipeline],
            )

        assert rc == 0
        mock_tg.assert_not_called()


class TestHelperFunctions:
    """Unit tests for low-level helper functions."""

    def test_parse_timestamp_iso_with_tz(self):
        ts = "2026-04-29T04:00:05.913972+00:00"
        dt = _parse_timestamp(ts)
        assert dt is not None
        assert dt.tzinfo is not None
        assert dt.year == 2026

    def test_parse_timestamp_datetime_no_tz(self):
        ts = "2026-04-28 16:33:17"
        dt = _parse_timestamp(ts)
        assert dt is not None
        assert dt.tzinfo == timezone.utc

    def test_parse_timestamp_date_only(self):
        ts = "2026-04-28"
        dt = _parse_timestamp(ts)
        assert dt is not None
        assert dt.year == 2026 and dt.month == 4 and dt.day == 28
        assert dt.tzinfo == timezone.utc

    def test_parse_timestamp_none_returns_none(self):
        assert _parse_timestamp(None) is None

    def test_parse_timestamp_empty_returns_none(self):
        assert _parse_timestamp("") is None

    def test_get_last_fresh_from_logfile_missing(self, tmp_path: Path):
        """Missing log file → None (no crash)."""
        result = _get_last_fresh_from_logfile("logs/nonexistent.log", tmp_path)
        assert result is None

    def test_get_last_fresh_from_logfile_present(self, tmp_path: Path):
        """Existing log file → mtime parsed as UTC datetime."""
        log_dir = tmp_path / "logs"
        log_dir.mkdir()
        log_file = log_dir / "test.log"
        log_file.write_text("some log content\n")

        result = _get_last_fresh_from_logfile("logs/test.log", tmp_path)
        assert result is not None
        assert result.tzinfo is not None  # timezone-aware

    def test_build_alert_message_format(self):
        """Alert message contains expected elements."""
        now = _WEDNESDAY
        stale_dt = now - timedelta(days=3)
        pipeline = _make_pipeline(name="signals_written_today", threshold_days=2)
        msg = _build_alert_message([(pipeline, stale_dt, 3.0)])

        assert "🚨" in msg
        assert "PIPELINE STALENESS" in msg
        assert "signals_written_today" in msg
        assert "3.0 days ago" in msg
        assert "threshold 2d" in msg
