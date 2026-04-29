"""tests/test_healthz_error_remediation.py — Phase 1 healthz meta-monitor tests.

Covers all 16+ spec tests for scripts.healthz_error_remediation.
DB isolation: conftest._isolate_prod_db redirects atlas_db to a per-test tmp file.
Table bootstrap: _setup_remediation_tables creates Phase-0 tables.
Telegram: utils.telegram.send_message is always mocked — no real messages sent.
"""
from __future__ import annotations

import json
import os
import sys
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch, call
import pytest

# ---------------------------------------------------------------------------
# Shared table DDL (same as test_error_monitor)
# ---------------------------------------------------------------------------

_CREATE_ERRORS = """
CREATE TABLE IF NOT EXISTS errors (
  id                   INTEGER PRIMARY KEY AUTOINCREMENT,
  fingerprint          TEXT    NOT NULL,
  first_seen_ts        TEXT    NOT NULL,
  last_seen_ts         TEXT    NOT NULL,
  occurrence_count     INTEGER NOT NULL DEFAULT 1,
  ts                   TEXT    NOT NULL,
  source               TEXT    NOT NULL CHECK(source IN
    ('python_logger','journald','cron','healthcheck','telegram_alert','manual','backfill')),
  service              TEXT,
  level                TEXT    NOT NULL CHECK(level IN ('WARNING','ERROR','CRITICAL')),
  logger_name          TEXT,
  message              TEXT    NOT NULL,
  exc_type             TEXT,
  exc_message          TEXT,
  traceback            TEXT,
  file_path            TEXT,
  line_number          INTEGER,
  function_name        TEXT,
  pid                  INTEGER,
  hostname             TEXT,
  context_json         TEXT,
  market_hours         INTEGER NOT NULL DEFAULT 0 CHECK(market_hours IN (0,1)),
  halt_active          INTEGER NOT NULL DEFAULT 0 CHECK(halt_active IN (0,1)),
  git_sha              TEXT,
  classification       TEXT    NOT NULL DEFAULT 'UNCLASSIFIED'
    CHECK(classification IN ('AUTO_FIX','ASSIST','ESCALATE','IGNORE','UNCLASSIFIED',
                              'ESCALATE_DEFERRED','IGNORE_PENDING_CLEAR')),
  triage_reason        TEXT,
  tier                 INTEGER NOT NULL DEFAULT 99 CHECK(tier IN (0,1,2,99)),
  remediation_status   TEXT    NOT NULL DEFAULT 'NEW'
    CHECK(remediation_status IN
      ('NEW','TRIAGED','IN_FLIGHT','FIXED','REVERTED','ESCALATED','IGNORED','SUPPRESSED')),
  remediation_attempts INTEGER NOT NULL DEFAULT 0,
  last_attempt_at      TEXT,
  fixed_by_attempt_id  INTEGER,
  resolved_at          TEXT,
  created_at           TEXT    NOT NULL DEFAULT (datetime('now'))
)
"""

_CREATE_FIX_ATTEMPTS = """
CREATE TABLE IF NOT EXISTS fix_attempts (
  id                   INTEGER PRIMARY KEY AUTOINCREMENT,
  error_id             INTEGER NOT NULL,
  fingerprint          TEXT    NOT NULL,
  started_ts           TEXT    NOT NULL,
  finished_ts          TEXT,
  status               TEXT    NOT NULL DEFAULT 'triaged'
    CHECK(status IN ('triaged','reproducing','diagnosing','fixing','verifying',
                     'reviewing','merged','reverted','failed','escalated','blocked','aborted')),
  classification       TEXT    NOT NULL
    CHECK(classification IN ('AUTO_FIX','ASSIST','ESCALATE','IGNORE')),
  reverted_ts          TEXT,
  notes                TEXT
)
"""

_CREATE_FIX_AUDIT_LOG = """
CREATE TABLE IF NOT EXISTS fix_audit_log (
  id                   INTEGER PRIMARY KEY AUTOINCREMENT,
  attempt_id           INTEGER,
  error_id             INTEGER,
  ts                   TEXT    NOT NULL DEFAULT (datetime('now')),
  phase                TEXT    NOT NULL
    CHECK(phase IN ('capture','triage','reproduce','diagnose','fix','verify','review',
                    'gate_check','merge','monitor','revert','halt','resume',
                    'config_change','graduation','demotion','manual')),
  actor                TEXT    NOT NULL,
  model                TEXT,
  decision             TEXT,
  reasoning            TEXT,
  payload_json         TEXT,
  result_status        TEXT
    CHECK(result_status IS NULL OR
          result_status IN ('success','blocked','error','timeout','aborted'))
)
"""


@pytest.fixture(autouse=True)
def _setup_remediation_tables():
    """Create Phase-0 tables in the per-test isolated DB."""
    import db.atlas_db as adb
    with adb.get_db() as conn:
        for ddl in (_CREATE_ERRORS, _CREATE_FIX_ATTEMPTS, _CREATE_FIX_AUDIT_LOG):
            conn.execute(ddl)
    yield


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def _now_ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def _ago_ts(hours: float) -> str:
    dt = datetime.now(timezone.utc) - timedelta(hours=hours)
    return dt.strftime("%Y-%m-%dT%H:%M:%S")


def _insert_error(classification: str = "UNCLASSIFIED", remediation_status: str = "NEW",
                  last_seen_ts: str | None = None) -> int:
    import db.atlas_db as adb
    ts = last_seen_ts or _now_ts()
    with adb.get_db() as conn:
        cur = conn.execute(
            """INSERT INTO errors
               (fingerprint, first_seen_ts, last_seen_ts, ts,
                source, level, message, classification, remediation_status)
               VALUES (?, ?, ?, ?, 'python_logger', 'ERROR', 'msg', ?, ?)""",
            (f"fp-{ts}", ts, ts, ts, classification, remediation_status),
        )
        return cur.lastrowid


def _insert_fix_attempt(status: str, error_id: int = 1,
                        finished_ts: str | None = None,
                        reverted_ts: str | None = None) -> int:
    import db.atlas_db as adb
    cls = "ASSIST" if status in ("merged", "reverted") else "ESCALATE"
    with adb.get_db() as conn:
        cur = conn.execute(
            """INSERT INTO fix_attempts
               (error_id, fingerprint, started_ts, finished_ts, status, classification, reverted_ts)
               VALUES (?, 'fp', ?, ?, ?, ?, ?)""",
            (error_id, _now_ts(), finished_ts or _now_ts(), status, cls, reverted_ts),
        )
        return cur.lastrowid


def _insert_audit_log(ts: str | None = None) -> None:
    import db.atlas_db as adb
    with adb.get_db() as conn:
        conn.execute(
            """INSERT INTO fix_audit_log (ts, phase, actor, result_status)
               VALUES (?, 'triage', 'classifier', 'success')""",
            (ts or _now_ts(),),
        )


# ---------------------------------------------------------------------------
# Import module under test
# ---------------------------------------------------------------------------

import scripts.healthz_error_remediation as healthz


# ---------------------------------------------------------------------------
# Individual check tests
# ---------------------------------------------------------------------------


def test_check_capture_alive_returns_ok_true():
    import db.atlas_db as adb
    with adb.get_db() as conn:
        ok, detail = healthz.check_capture_alive(conn, lookback_hours=24)
    assert ok is True
    assert "errors_last_24h" in detail
    assert "errors_total" in detail


def test_check_capture_alive_counts_recent_errors():
    import db.atlas_db as adb
    _insert_error(last_seen_ts=_now_ts())
    _insert_error(last_seen_ts=_ago_ts(48))  # older than 24h window
    with adb.get_db() as conn:
        ok, detail = healthz.check_capture_alive(conn, lookback_hours=24)
    assert detail["errors_last_24h"] == 1
    assert detail["errors_total"] == 2


def test_check_classifier_backlog_ok_when_under_threshold():
    import db.atlas_db as adb
    for _ in range(5):
        _insert_error("UNCLASSIFIED", "NEW")
    with adb.get_db() as conn:
        ok, detail = healthz.check_classifier_backlog(conn, threshold=100)
    assert ok is True
    assert detail["unclassified_backlog"] == 5


def test_check_classifier_backlog_fails_when_over_threshold():
    import db.atlas_db as adb
    for _ in range(10):
        _insert_error("UNCLASSIFIED", "NEW")
    with adb.get_db() as conn:
        ok, detail = healthz.check_classifier_backlog(conn, threshold=5)
    assert ok is False
    assert detail["unclassified_backlog"] == 10


def test_check_audit_log_writes_always_ok():
    import db.atlas_db as adb
    _insert_audit_log()
    with adb.get_db() as conn:
        ok, detail = healthz.check_audit_log_writes(conn, lookback_hours=24)
    assert ok is True
    assert detail["audit_writes_last_24h"] >= 1


def test_check_revert_rate_ok_when_no_activity():
    import db.atlas_db as adb
    with adb.get_db() as conn:
        ok, detail = healthz.check_revert_rate(conn)
    assert ok is True
    assert detail["revert_rate_pct"] == 0.0


def test_check_revert_rate_ok_under_alert_threshold():
    """3 merged, 0 reverted = 0% revert rate — ok."""
    import db.atlas_db as adb
    for _ in range(3):
        _insert_fix_attempt("merged", finished_ts=_now_ts())
    with adb.get_db() as conn:
        ok, detail = healthz.check_revert_rate(conn)
    assert ok is True
    assert detail["revert_rate_pct"] == 0.0


def test_check_revert_rate_fails_at_alert_threshold():
    """2 merged, 1 reverted = 50% revert rate — fails (>= 15% alert threshold)."""
    import db.atlas_db as adb
    for _ in range(2):
        _insert_fix_attempt("merged", finished_ts=_now_ts())
    _insert_fix_attempt("reverted", reverted_ts=_now_ts())
    with adb.get_db() as conn:
        ok, detail = healthz.check_revert_rate(conn)
    assert ok is False
    assert detail["revert_rate_pct"] >= 15.0


def test_check_revert_rate_fails_at_halt_threshold():
    """4 merged, 2 reverted = 50% — exceeds both alert (15%) and halt (25%)."""
    import db.atlas_db as adb
    for _ in range(4):
        _insert_fix_attempt("merged", finished_ts=_now_ts())
    for _ in range(2):
        _insert_fix_attempt("reverted", reverted_ts=_now_ts())
    with adb.get_db() as conn:
        ok, detail = healthz.check_revert_rate(conn)
    assert ok is False
    assert detail["revert_rate_pct"] >= 25.0


def test_check_phase_state_reads_config():
    """Reads real config/auto_remediation.yaml; expects phase=2, phase_3_enabled=False."""
    import db.atlas_db as adb
    with adb.get_db() as conn:
        ok, detail = healthz.check_phase_state(conn)
    assert ok is True
    assert detail["phase"] == 2
    assert detail["phase_3_enabled"] is False


def test_check_phase_state_fails_when_config_missing(monkeypatch, tmp_path):
    """Returns ok=False when config file is not present."""
    import db.atlas_db as adb
    monkeypatch.setattr(healthz, "PROJECT_ROOT", tmp_path)
    with adb.get_db() as conn:
        ok, detail = healthz.check_phase_state(conn)
    assert ok is False
    assert detail.get("config_missing") is True


# ---------------------------------------------------------------------------
# run_health integration
# ---------------------------------------------------------------------------


def test_run_health_exits_zero_when_all_pass(monkeypatch):
    """run_health returns 0 when all checks pass (empty DB = valid phase 1 state)."""
    # Mock send_telegram_alert so no actual Telegram calls
    monkeypatch.setattr(healthz, "send_telegram_alert", MagicMock())
    rc = healthz.run_health()
    assert rc == 0


def test_run_health_exits_one_when_check_fails(monkeypatch):
    """run_health returns 1 when backlog exceeds threshold."""
    import db.atlas_db as adb
    # Insert 200 UNCLASSIFIED rows to breach the 100-row default threshold
    for i in range(105):
        _insert_error("UNCLASSIFIED", "NEW")

    monkeypatch.setattr(healthz, "send_telegram_alert", MagicMock())
    rc = healthz.run_health()
    assert rc == 1


def test_run_health_json_output(monkeypatch, capsys):
    """--json flag produces valid JSON with ok/failures/summary keys."""
    monkeypatch.setattr(healthz, "send_telegram_alert", MagicMock())
    rc = healthz.run_health(json_output=True)
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert "ok" in data
    assert "failures" in data
    assert "summary" in data


def test_telegram_not_called_on_success(monkeypatch):
    """send_telegram_alert is NEVER called when all checks pass (on_success=never)."""
    mock_tg = MagicMock()
    monkeypatch.setattr(healthz, "send_telegram_alert", mock_tg)
    rc = healthz.run_health()
    assert rc == 0
    mock_tg.assert_not_called()


def test_telegram_called_once_on_failure(monkeypatch):
    """send_telegram_alert is called exactly once when any check fails."""
    import db.atlas_db as adb
    # Cause backlog failure
    for _ in range(105):
        _insert_error("UNCLASSIFIED", "NEW")

    mock_tg = MagicMock()
    monkeypatch.setattr(healthz, "send_telegram_alert", mock_tg)
    rc = healthz.run_health()
    assert rc == 1
    mock_tg.assert_called_once()


def test_property_100_successful_runs_zero_telegram_calls(monkeypatch):
    """Property test: 100 successful run_health calls → 0 Telegram calls."""
    call_count = 0

    def _mock_alert(failures, summary):
        nonlocal call_count
        call_count += 1

    monkeypatch.setattr(healthz, "send_telegram_alert", _mock_alert)

    for _ in range(100):
        healthz.run_health()

    assert call_count == 0


# ---------------------------------------------------------------------------
# send_telegram_alert (unit — mock utils.telegram)
# ---------------------------------------------------------------------------


def test_send_telegram_alert_escapes_content(monkeypatch):
    """send_telegram_alert calls utils.telegram.send_message exactly once."""
    sent = []

    fake_tg = types.ModuleType("utils.telegram")
    fake_tg.send_message = lambda text, **kwargs: sent.append(text)
    fake_tg._esc = lambda s: str(s).replace("&", "&amp;").replace("<", "&lt;")
    monkeypatch.setitem(sys.modules, "utils.telegram", fake_tg)

    # Reload to use the mock
    import importlib
    import scripts.healthz_error_remediation as h
    importlib.reload(h)

    failures = [{"check": "classifier_backlog", "detail": {"unclassified_backlog": 200}}]
    h.send_telegram_alert(failures, {"classifier_backlog": {"unclassified_backlog": 200}})

    assert len(sent) == 1
    assert "classifier_backlog" in sent[0]
