"""tests/test_error_monitor.py — Phase 1 error monitor tests.

Covers all 22+ spec tests for core.error_monitor.
DB isolation: conftest._isolate_prod_db redirects atlas_db to a per-test tmp file.
Table bootstrap: _setup_remediation_tables fixture creates the Phase 0 tables.
Triage stub: _fake_triage_module patches sys.modules['core.triage'] with a
             deterministic FakeTriageClassifier so tests never need the real impl.
"""
from __future__ import annotations

import json
import os
import sys
import types
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# Fake triage module — injected before any import of core.error_monitor
# so the lazy "from core.triage import TriageClassifier" inside run_once sees it.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class FakeTriageResult:
    classification: str
    reason: str
    rule_id: str
    tier: int


class FakeTriageClassifier:
    """Deterministic stub — returns ASSIST by default."""

    _default_classification = "ASSIST"

    def __init__(self, config_path=None, deny_path=None, funcs_path=None):
        pass

    def classify(self, error_dict: dict) -> FakeTriageResult:
        return FakeTriageResult(
            classification=self._default_classification,
            reason="test reason",
            rule_id="test_rule_001",
            tier=1,
        )


def _make_fake_triage_module(cls_name: str = "ASSIST"):
    """Build a fake core.triage module with the given default classification."""
    m = types.ModuleType("core.triage")

    class _Cls(FakeTriageClassifier):
        _default_classification = cls_name

    m.TriageClassifier = _Cls
    m.TriageResult = FakeTriageResult
    # Use a lambda closure to capture cls_name without dataclass field-name shadowing.
    m.classify_error = lambda d: FakeTriageResult(
        classification=cls_name,
        reason="test reason",
        rule_id="test_rule_001",
        tier=1,
    )
    return m


# ---------------------------------------------------------------------------
# Table bootstrap — creates Phase-0 tables that schema.sql doesn't include yet.
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
  triage_model         TEXT,
  triage_reason        TEXT,
  revert_commit_sha    TEXT,
  revert_reason        TEXT,
  reverted_ts          TEXT,
  monitor_outcome      TEXT,
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
  diff                 TEXT,
  payload_json         TEXT,
  duration_sec         REAL,
  tokens_in            INTEGER,
  tokens_out           INTEGER,
  cost_usd             REAL DEFAULT 0,
  result_status        TEXT
    CHECK(result_status IS NULL OR
          result_status IN ('success','blocked','error','timeout','aborted')),
  blocked_by_gate      TEXT,
  notes                TEXT
)
"""


@pytest.fixture(autouse=True)
def _setup_remediation_tables():
    """Create Phase-0 tables in the test-isolated DB (created by conftest)."""
    import db.atlas_db as adb
    with adb.get_db() as conn:
        for ddl in (_CREATE_ERRORS, _CREATE_FIX_ATTEMPTS, _CREATE_FIX_AUDIT_LOG):
            conn.execute(ddl)
    yield


@pytest.fixture()
def fake_triage(monkeypatch):
    """Inject a fake core.triage module returning ASSIST by default."""
    m = _make_fake_triage_module("ASSIST")
    monkeypatch.setitem(sys.modules, "core.triage", m)
    yield m


@pytest.fixture()
def fake_triage_for(monkeypatch):
    """Factory fixture — call it with a classification to inject."""
    def _factory(classification: str):
        m = _make_fake_triage_module(classification)
        monkeypatch.setitem(sys.modules, "core.triage", m)
        return m
    return _factory


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def _insert_error(classification: str = "UNCLASSIFIED",
                  remediation_status: str = "NEW",
                  fingerprint: str = "fp-test") -> int:
    import db.atlas_db as adb
    with adb.get_db() as conn:
        cur = conn.execute(
            """INSERT INTO errors
               (fingerprint, first_seen_ts, last_seen_ts, ts,
                source, level, message, classification, remediation_status)
               VALUES (?, datetime('now'), datetime('now'), datetime('now'),
                       'python_logger', 'ERROR', 'test error', ?, ?)""",
            (fingerprint, classification, remediation_status),
        )
        return cur.lastrowid


def _get_error(error_id: int) -> dict:
    import db.atlas_db as adb
    with adb.get_db() as conn:
        r = conn.execute("SELECT * FROM errors WHERE id=?", (error_id,)).fetchone()
        return dict(r) if r else {}


def _audit_log_count(error_id: int) -> int:
    import db.atlas_db as adb
    with adb.get_db() as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM fix_audit_log WHERE error_id=?", (error_id,)
        ).fetchone()[0]


# ---------------------------------------------------------------------------
# is_disabled_via_env / find_halt_reason
# ---------------------------------------------------------------------------


def test_find_halt_reason_none_when_clear(tmp_path, monkeypatch):
    """Returns None when no halt files present and env var not set."""
    from core.error_monitor import find_halt_reason
    monkeypatch.delenv("ATLAS_AUTO_REMEDIATION_DISABLED", raising=False)
    # Patch HALT_FILES to non-existent tmp paths so production halt files
    # don't interfere.
    import core.error_monitor as em
    orig = em.HALT_FILES
    monkeypatch.setattr(em, "HALT_FILES", (
        tmp_path / "HALT",
        tmp_path / ".live_halt",
        tmp_path / "AUTO_REMEDIATION_HALT",
    ))
    assert find_halt_reason() is None
    monkeypatch.setattr(em, "HALT_FILES", orig)


def test_find_halt_reason_auto_remediation_halt(tmp_path, monkeypatch):
    """Returns the path string when AUTO_REMEDIATION_HALT file is present."""
    import core.error_monitor as em
    halt_file = tmp_path / "AUTO_REMEDIATION_HALT"
    halt_file.touch()
    monkeypatch.delenv("ATLAS_AUTO_REMEDIATION_DISABLED", raising=False)
    monkeypatch.setattr(em, "HALT_FILES", (
        tmp_path / "HALT",
        tmp_path / ".live_halt",
        halt_file,
    ))
    reason = em.find_halt_reason()
    assert reason == str(halt_file)


def test_find_halt_reason_env_var(monkeypatch):
    """Returns env string when ATLAS_AUTO_REMEDIATION_DISABLED=1."""
    from core.error_monitor import find_halt_reason
    monkeypatch.setenv("ATLAS_AUTO_REMEDIATION_DISABLED", "1")
    assert find_halt_reason() == "env:ATLAS_AUTO_REMEDIATION_DISABLED=1"


def test_is_disabled_via_env_true(monkeypatch):
    from core.error_monitor import is_disabled_via_env
    monkeypatch.setenv("ATLAS_AUTO_REMEDIATION_DISABLED", "1")
    assert is_disabled_via_env() is True


def test_is_disabled_via_env_false(monkeypatch):
    from core.error_monitor import is_disabled_via_env
    monkeypatch.delenv("ATLAS_AUTO_REMEDIATION_DISABLED", raising=False)
    assert is_disabled_via_env() is False


# ---------------------------------------------------------------------------
# run_once — halt path
# ---------------------------------------------------------------------------


def test_run_once_halted_returns_halted(tmp_path, monkeypatch):
    """run_once skips all DB work when halted; returns halted=True, processed=0."""
    import core.error_monitor as em
    halt_file = tmp_path / "AUTO_REMEDIATION_HALT"
    halt_file.touch()
    monkeypatch.setattr(em, "HALT_FILES", (halt_file,))
    monkeypatch.delenv("ATLAS_AUTO_REMEDIATION_DISABLED", raising=False)

    result = em.run_once(dry_run=True)
    assert result["halted"] is True
    assert result["processed"] == 0
    assert "halt_reason" in result


# ---------------------------------------------------------------------------
# run_once — processing path
# ---------------------------------------------------------------------------


def test_run_once_processes_batch(monkeypatch, fake_triage):
    """Processes UNCLASSIFIED rows and returns processed > 0."""
    import core.error_monitor as em
    import db.atlas_db as adb
    # Ensure no halt files in effect
    monkeypatch.delenv("ATLAS_AUTO_REMEDIATION_DISABLED", raising=False)
    monkeypatch.setattr(em, "HALT_FILES", ())

    _insert_error("UNCLASSIFIED", "NEW", "fp-001")
    _insert_error("UNCLASSIFIED", "NEW", "fp-002")

    m = em.run_once(dry_run=True)
    assert m["processed"] == 2
    assert m["halted"] is False


def test_run_once_updates_classification(monkeypatch, fake_triage):
    """The errors row classification is updated from UNCLASSIFIED → ASSIST."""
    import core.error_monitor as em
    monkeypatch.delenv("ATLAS_AUTO_REMEDIATION_DISABLED", raising=False)
    monkeypatch.setattr(em, "HALT_FILES", ())

    eid = _insert_error("UNCLASSIFIED", "NEW", "fp-classify")
    em.run_once(dry_run=True)

    row = _get_error(eid)
    assert row["classification"] == "ASSIST"
    assert row["remediation_status"] == "TRIAGED"


def test_run_once_sets_ignored_for_ignore(monkeypatch, fake_triage_for):
    """IGNORE classification → remediation_status='IGNORED'."""
    import core.error_monitor as em
    monkeypatch.delenv("ATLAS_AUTO_REMEDIATION_DISABLED", raising=False)
    monkeypatch.setattr(em, "HALT_FILES", ())
    fake_triage_for("IGNORE")

    eid = _insert_error("UNCLASSIFIED", "NEW", "fp-ignore")
    em.run_once(dry_run=True)

    assert _get_error(eid)["remediation_status"] == "IGNORED"


def test_run_once_sets_escalated_for_escalate(monkeypatch, fake_triage_for):
    """ESCALATE classification → remediation_status='ESCALATED'."""
    import core.error_monitor as em
    monkeypatch.delenv("ATLAS_AUTO_REMEDIATION_DISABLED", raising=False)
    monkeypatch.setattr(em, "HALT_FILES", ())
    fake_triage_for("ESCALATE")

    eid = _insert_error("UNCLASSIFIED", "NEW", "fp-escalate")
    em.run_once(dry_run=True)

    assert _get_error(eid)["remediation_status"] == "ESCALATED"


def test_run_once_keeps_new_for_escalate_deferred(monkeypatch, fake_triage_for):
    """ESCALATE_DEFERRED → remediation_status stays 'NEW' (re-evaluate later)."""
    import core.error_monitor as em
    monkeypatch.delenv("ATLAS_AUTO_REMEDIATION_DISABLED", raising=False)
    monkeypatch.setattr(em, "HALT_FILES", ())
    fake_triage_for("ESCALATE_DEFERRED")

    eid = _insert_error("UNCLASSIFIED", "NEW", "fp-deferred")
    em.run_once(dry_run=True)

    row = _get_error(eid)
    assert row["remediation_status"] == "NEW"
    assert row["classification"] == "ESCALATE_DEFERRED"


def test_run_once_keeps_new_for_ignore_pending_clear(monkeypatch, fake_triage_for):
    """IGNORE_PENDING_CLEAR → remediation_status stays 'NEW'."""
    import core.error_monitor as em
    monkeypatch.delenv("ATLAS_AUTO_REMEDIATION_DISABLED", raising=False)
    monkeypatch.setattr(em, "HALT_FILES", ())
    fake_triage_for("IGNORE_PENDING_CLEAR")

    eid = _insert_error("UNCLASSIFIED", "NEW", "fp-pending-clear")
    em.run_once(dry_run=True)

    row = _get_error(eid)
    assert row["remediation_status"] == "NEW"
    assert row["classification"] == "IGNORE_PENDING_CLEAR"


def test_run_once_writes_audit_log_per_error(monkeypatch, fake_triage):
    """One fix_audit_log row is written for each processed error."""
    import core.error_monitor as em
    monkeypatch.delenv("ATLAS_AUTO_REMEDIATION_DISABLED", raising=False)
    monkeypatch.setattr(em, "HALT_FILES", ())

    eid1 = _insert_error("UNCLASSIFIED", "NEW", "fp-audit-1")
    eid2 = _insert_error("UNCLASSIFIED", "NEW", "fp-audit-2")
    em.run_once(dry_run=True)

    assert _audit_log_count(eid1) == 1
    assert _audit_log_count(eid2) == 1


def test_audit_log_has_correct_phase_and_actor(monkeypatch, fake_triage):
    """fix_audit_log row has phase='triage' and actor='classifier'."""
    import core.error_monitor as em
    import db.atlas_db as adb
    monkeypatch.delenv("ATLAS_AUTO_REMEDIATION_DISABLED", raising=False)
    monkeypatch.setattr(em, "HALT_FILES", ())

    eid = _insert_error("UNCLASSIFIED", "NEW", "fp-phase-actor")
    em.run_once(dry_run=True)

    with adb.get_db() as conn:
        row = conn.execute(
            "SELECT phase, actor FROM fix_audit_log WHERE error_id=?", (eid,)
        ).fetchone()
    assert row["phase"] == "triage"
    assert row["actor"] == "classifier"


def test_run_once_skips_already_classified(monkeypatch, fake_triage):
    """Rows that are already classified (not UNCLASSIFIED) are not processed."""
    import core.error_monitor as em
    monkeypatch.delenv("ATLAS_AUTO_REMEDIATION_DISABLED", raising=False)
    monkeypatch.setattr(em, "HALT_FILES", ())

    _insert_error("ASSIST", "TRIAGED", "fp-already-classified")
    _insert_error("UNCLASSIFIED", "NEW", "fp-new-only")

    m = em.run_once(dry_run=True)
    assert m["processed"] == 1  # only the UNCLASSIFIED one


def test_run_once_respects_batch_size(monkeypatch, fake_triage):
    """Only up to batch_size rows are processed per cycle."""
    import core.error_monitor as em
    monkeypatch.delenv("ATLAS_AUTO_REMEDIATION_DISABLED", raising=False)
    monkeypatch.setattr(em, "HALT_FILES", ())

    for i in range(10):
        _insert_error("UNCLASSIFIED", "NEW", f"fp-batch-{i}")

    m = em.run_once(batch_size=3, dry_run=True)
    assert m["processed"] == 3


def test_run_once_continues_on_classifier_exception(monkeypatch):
    """If classifier.classify() raises, the error is logged and processing continues."""
    import core.error_monitor as em
    monkeypatch.delenv("ATLAS_AUTO_REMEDIATION_DISABLED", raising=False)
    monkeypatch.setattr(em, "HALT_FILES", ())

    class _BoomClassifier:
        def __init__(self, **_): pass
        def classify(self, d):
            raise RuntimeError("boom")

    bad_module = types.ModuleType("core.triage")
    bad_module.TriageClassifier = _BoomClassifier
    bad_module.TriageResult = FakeTriageResult
    monkeypatch.setitem(sys.modules, "core.triage", bad_module)

    _insert_error("UNCLASSIFIED", "NEW", "fp-boom-1")
    _insert_error("UNCLASSIFIED", "NEW", "fp-boom-2")

    m = em.run_once(dry_run=True)
    # Both rows attempted; both failed; errors metric = 2, processed = 0
    assert m["errors"] == 2
    assert m["processed"] == 0


def test_run_once_returns_import_error_when_triage_missing(monkeypatch):
    """Returns import_error metric when core.triage cannot be imported."""
    import core.error_monitor as em
    monkeypatch.delenv("ATLAS_AUTO_REMEDIATION_DISABLED", raising=False)
    monkeypatch.setattr(em, "HALT_FILES", ())
    # Remove any cached triage module so the import fails
    monkeypatch.delitem(sys.modules, "core.triage", raising=False)

    # Patch builtins.__import__ to raise for core.triage
    real_import = __builtins__.__import__ if hasattr(__builtins__, "__import__") else __import__

    import builtins
    orig_import = builtins.__import__

    def _failing_import(name, *args, **kwargs):
        if name == "core.triage":
            raise ImportError("core.triage not available (parallel worker pending)")
        return orig_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _failing_import)

    m = em.run_once(dry_run=True)
    assert "import_error" in m
    assert m["errors"] == 1
    assert m["processed"] == 0


def test_run_once_by_class_accumulates(monkeypatch, fake_triage):
    """by_class counter increments correctly for each classification."""
    import core.error_monitor as em
    monkeypatch.delenv("ATLAS_AUTO_REMEDIATION_DISABLED", raising=False)
    monkeypatch.setattr(em, "HALT_FILES", ())

    # Insert 3 rows — all classified as ASSIST by fake_triage
    for i in range(3):
        _insert_error("UNCLASSIFIED", "NEW", f"fp-byclass-{i}")

    m = em.run_once(dry_run=True)
    assert m["by_class"].get("ASSIST", 0) == 3


def test_main_once_exits_zero(monkeypatch, fake_triage, tmp_path):
    """main(['--once', '--db', path]) exits 0 when no errors."""
    import core.error_monitor as em
    import db.atlas_db as adb
    monkeypatch.delenv("ATLAS_AUTO_REMEDIATION_DISABLED", raising=False)
    monkeypatch.setattr(em, "HALT_FILES", ())

    # Use the current test-isolated DB path
    db_path = adb._db_path_override or str(adb.DB_PATH)
    rc = em.main(["--once", "--db", db_path])
    assert rc == 0


def test_main_without_once_returns_2():
    """main([]) (no --once flag) returns exit code 2."""
    import core.error_monitor as em
    rc = em.main([])
    assert rc == 2


def test_dry_run_is_default(monkeypatch, fake_triage):
    """The default dry_run=True means the metrics dict contains 'dry_run': True."""
    import core.error_monitor as em
    monkeypatch.delenv("ATLAS_AUTO_REMEDIATION_DISABLED", raising=False)
    monkeypatch.setattr(em, "HALT_FILES", ())

    _insert_error("UNCLASSIFIED", "NEW", "fp-dryrun-default")
    m = em.run_once()  # no dry_run kwarg — default True
    assert m.get("dry_run") is True


def test_same_row_not_processed_twice(monkeypatch, fake_triage):
    """Idempotency: the same UNCLASSIFIED row is only processed once across two sweeps."""
    import core.error_monitor as em
    import db.atlas_db as adb
    monkeypatch.delenv("ATLAS_AUTO_REMEDIATION_DISABLED", raising=False)
    monkeypatch.setattr(em, "HALT_FILES", ())

    eid = _insert_error("UNCLASSIFIED", "NEW", "fp-idempotent")

    m1 = em.run_once(dry_run=True)
    m2 = em.run_once(dry_run=True)

    assert m1["processed"] == 1
    assert m2["processed"] == 0  # already classified, not UNCLASSIFIED/NEW

    # Exactly one audit log entry
    assert _audit_log_count(eid) == 1

    # The row's classification was written exactly once
    row = _get_error(eid)
    assert row["classification"] != "UNCLASSIFIED"
