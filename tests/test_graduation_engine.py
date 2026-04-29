"""Tests for core/graduation.py — Phase 3 graduation engine.

Schema bootstrap uses inline CREATE TABLE (no migration dependency) for unit
tests of individual functions; file-based DBs with full schema for run() tests.

Run:
    cd /root/atlas && python3 -m pytest tests/test_graduation_engine.py -x -v --timeout=30
"""
from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import pytest

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

from core.graduation import (
    ClassMetric,
    GraduationDecision,
    _classify_attempts_by_class,
    _load_thresholds,
    evaluate_graduation,
    run,
    write_graduation_decisions,
)


# ── Schema helpers ─────────────────────────────────────────────────────────────

_DDL_FIX_ATTEMPTS = """
CREATE TABLE fix_attempts (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    error_id          INTEGER NOT NULL,
    fingerprint       TEXT    NOT NULL,
    started_ts        TEXT    NOT NULL,
    finished_ts       TEXT,
    status            TEXT    NOT NULL DEFAULT 'triaged',
    classification    TEXT    NOT NULL,
    blocked_by_gate   TEXT,
    gates_failed_json TEXT,
    gates_passed_json TEXT,
    monitor_outcome   TEXT,
    notes             TEXT
)
"""

_DDL_FIX_AUDIT_LOG = """
CREATE TABLE fix_audit_log (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    attempt_id     INTEGER,
    error_id       INTEGER,
    ts             TEXT    NOT NULL DEFAULT (datetime('now')),
    phase          TEXT    NOT NULL,
    actor          TEXT    NOT NULL,
    decision       TEXT,
    reasoning      TEXT,
    payload_json   TEXT,
    result_status  TEXT,
    notes          TEXT
)
"""

_DDL_TRIGGER_NO_UPDATE = """
CREATE TRIGGER fix_audit_log_no_update
BEFORE UPDATE ON fix_audit_log
BEGIN
    SELECT RAISE(ABORT, 'fix_audit_log is immutable (append-only)');
END
"""

_DDL_TRIGGER_NO_DELETE = """
CREATE TRIGGER fix_audit_log_no_delete
BEFORE DELETE ON fix_audit_log
BEGIN
    SELECT RAISE(ABORT, 'fix_audit_log is immutable (append-only)');
END
"""


def _minimal_conn(with_triggers: bool = False) -> sqlite3.Connection:
    """In-memory SQLite with fix_attempts + fix_audit_log (FK enforcement OFF)."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(_DDL_FIX_ATTEMPTS)
    conn.execute(_DDL_FIX_AUDIT_LOG)
    if with_triggers:
        conn.execute(_DDL_TRIGGER_NO_UPDATE)
        conn.execute(_DDL_TRIGGER_NO_DELETE)
    conn.commit()
    return conn


def _make_db_file(tmp_path: Path) -> str:
    """Create a file-based SQLite with full remediation schema (for run() tests)."""
    path = tmp_path / "test_graduation.db"
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE errors (id INTEGER PRIMARY KEY AUTOINCREMENT, fingerprint TEXT NOT NULL)")
    conn.execute(_DDL_FIX_ATTEMPTS)
    conn.execute(_DDL_FIX_AUDIT_LOG)
    conn.execute(_DDL_TRIGGER_NO_UPDATE)
    conn.execute(_DDL_TRIGGER_NO_DELETE)
    conn.commit()
    conn.close()
    return str(path)


def _now_minus(days: int = 0, hours: int = 0) -> str:
    """ISO timestamp N days (+ h hours) in the past (UTC)."""
    return (datetime.now(timezone.utc) - timedelta(days=days, hours=hours)).isoformat()


def _insert_assist(conn: sqlite3.Connection, fp: str = "fp1",
                   started_ts: Optional[str] = None, status: str = "merged") -> None:
    ts = started_ts or _now_minus(days=20)
    conn.execute(
        "INSERT INTO fix_attempts (error_id, fingerprint, started_ts, status, classification)"
        " VALUES (1, ?, ?, ?, 'ASSIST')",
        (fp, ts, status),
    )
    conn.commit()


def _insert_auto_fix(conn: sqlite3.Connection, cls_name: str = "test_cls",
                     fp: str = "fp_auto",
                     started_ts: Optional[str] = None,
                     blocked_by_gate: Optional[str] = None,
                     status: str = "merged") -> None:
    ts = started_ts or _now_minus()
    notes = f"matched_class={cls_name}" if cls_name else ""
    conn.execute(
        "INSERT INTO fix_attempts"
        " (error_id, fingerprint, started_ts, status, classification, blocked_by_gate, notes)"
        " VALUES (1, ?, ?, ?, 'AUTO_FIX', ?, ?)",
        (fp, ts, status, blocked_by_gate, notes),
    )
    conn.commit()


def _seed_file_db(db_path: str, error_id: int = 1) -> None:
    """Insert a parent errors row into a file-based DB (FK must be satisfied)."""
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute(
        "INSERT INTO errors (id, fingerprint) VALUES (?, 'seed_fp')",
        (error_id,),
    )
    conn.commit()
    conn.close()


def _seed_assist_file(db_path: str, fp: str = "graduate_fp",
                      count: int = 5, days_back: int = 15) -> None:
    """Seed `count` merged ASSIST rows into a file-based DB for promotion testing."""
    _seed_file_db(db_path)
    conn = sqlite3.connect(db_path)
    started_ts = _now_minus(days=days_back)
    for _ in range(count):
        conn.execute(
            "INSERT INTO fix_attempts"
            " (error_id, fingerprint, started_ts, status, classification)"
            " VALUES (1, ?, ?, 'merged', 'ASSIST')",
            (fp, started_ts),
        )
    conn.commit()
    conn.close()


def _seed_auto_fix_file(db_path: str, cls_name: str = "danger_class",
                        violation_count: int = 6) -> None:
    """Seed `violation_count` scope-violated AUTO_FIX rows into a file-based DB."""
    _seed_file_db(db_path)
    conn = sqlite3.connect(db_path)
    for i in range(violation_count):
        conn.execute(
            "INSERT INTO fix_attempts"
            " (error_id, fingerprint, started_ts, status, classification, blocked_by_gate, notes)"
            " VALUES (1, ?, ?, 'blocked', 'AUTO_FIX', 'no_never_list_touched', ?)",
            (f"fp_auto_{i}", _now_minus(), f"matched_class={cls_name}"),
        )
    conn.commit()
    conn.close()


_FIXED_THRESHOLDS = {
    "days_of_clean_assist": 14,
    "min_merged_assist_fixes": 5,
    "scope_violations_threshold": 5,
    "scope_violations_window_days": 60,
}


# ── 1-2: _load_thresholds ──────────────────────────────────────────────────────

class TestLoadThresholds:
    def test_user_locked_values(self):
        """Config YAML returns the exact user-locked values from auto_remediation.yaml."""
        cfg_path = PROJECT / "config" / "auto_remediation.yaml"
        if not cfg_path.exists():
            pytest.skip("auto_remediation.yaml not present")
        th = _load_thresholds(cfg_path)
        assert th["days_of_clean_assist"] == 14
        assert th["min_merged_assist_fixes"] == 5
        assert th["scope_violations_threshold"] == 5
        assert th["scope_violations_window_days"] == 60

    def test_missing_config_falls_back_to_defaults(self, tmp_path: Path):
        """Non-existent config path returns hard-coded defaults."""
        th = _load_thresholds(tmp_path / "does_not_exist.yaml")
        assert th["days_of_clean_assist"] == 14
        assert th["min_merged_assist_fixes"] == 5
        assert th["scope_violations_threshold"] == 5
        assert th["scope_violations_window_days"] == 60


# ── 3-6: _classify_attempts_by_class ──────────────────────────────────────────

class TestClassifyAttemptsByClass:
    def test_empty_db_returns_empty_dict(self):
        """No fix_attempts rows → empty metrics dict."""
        conn = _minimal_conn()
        result = _classify_attempts_by_class(conn)
        assert result == {}

    def test_one_assist_merged_attempt(self):
        """Single merged ASSIST attempt produces 1 metric with merged_assist_count=1."""
        conn = _minimal_conn()
        _insert_assist(conn, fp="abc", status="merged")
        metrics = _classify_attempts_by_class(conn)
        assert "fp:abc" in metrics
        m = metrics["fp:abc"]
        assert m.merged_assist_count == 1
        assert m.current_state == "ASSIST"

    def test_five_assist_merged_same_fp(self):
        """5 merged ASSIST rows for same fp → 1 metric with merged=5."""
        conn = _minimal_conn()
        for _ in range(5):
            _insert_assist(conn, fp="shared_fp", status="merged")
        metrics = _classify_attempts_by_class(conn)
        assert len(metrics) == 1
        assert metrics["fp:shared_fp"].merged_assist_count == 5

    def test_days_in_assist_computed_correctly(self):
        """days_in_assist reflects calendar days since first merged attempt."""
        conn = _minimal_conn()
        # 15 days + 2 hours ago → days = 15 (not 14 or 16)
        started_ts = _now_minus(days=15, hours=2)
        _insert_assist(conn, fp="timed_fp", started_ts=started_ts, status="merged")
        metrics = _classify_attempts_by_class(conn)
        assert metrics["fp:timed_fp"].days_in_assist == 15


# ── 7-13: evaluate_graduation ─────────────────────────────────────────────────

class TestEvaluateGraduation:
    def _assist_conn(self, merged: int = 5, days_back: int = 15) -> sqlite3.Connection:
        """Helper: conn seeded with N merged ASSIST rows, days_back days ago."""
        conn = _minimal_conn()
        started_ts = _now_minus(days=days_back, hours=1)
        for _ in range(merged):
            _insert_assist(conn, fp="test_fp", started_ts=started_ts, status="merged")
        return conn

    def _auto_fix_conn(self, violations: int = 0) -> sqlite3.Connection:
        conn = _minimal_conn()
        gate = "no_never_list_touched" if violations > 0 else None
        for i in range(violations):
            _insert_auto_fix(conn, cls_name="hot_class", fp=f"fp_{i}",
                             blocked_by_gate=gate, status="blocked")
        return conn

    def test_promotes_when_all_thresholds_met(self):
        """14+ days, 5+ merged, 0 violations → PROMOTE_TO_AUTO_FIX."""
        conn = self._assist_conn(merged=5, days_back=15)
        decisions = evaluate_graduation(conn, thresholds=_FIXED_THRESHOLDS)
        promotions = [d for d in decisions if d.decision == "PROMOTE_TO_AUTO_FIX"]
        assert len(promotions) == 1
        assert promotions[0].class_name == "fp:test_fp"

    def test_no_change_insufficient_days(self):
        """13 days (< 14 threshold) → NO_CHANGE even with enough merges."""
        conn = self._assist_conn(merged=5, days_back=13)
        decisions = evaluate_graduation(conn, thresholds=_FIXED_THRESHOLDS)
        assert all(d.decision == "NO_CHANGE" for d in decisions)

    def test_no_change_insufficient_merged(self):
        """Only 4 merged (< 5 threshold) → NO_CHANGE even with enough days."""
        conn = self._assist_conn(merged=4, days_back=15)
        decisions = evaluate_graduation(conn, thresholds=_FIXED_THRESHOLDS)
        assert all(d.decision == "NO_CHANGE" for d in decisions)

    def test_no_change_with_any_violations(self):
        """1 scope-guard violation on an ASSIST-state fp → NO_CHANGE (violations must be 0)."""
        # Insert a merged ASSIST row + one AUTO_FIX row with scope violation for same fp
        conn = _minimal_conn()
        started_ts = _now_minus(days=15, hours=1)
        for _ in range(5):
            _insert_assist(conn, fp="mixed_fp", started_ts=started_ts, status="merged")
        # The fingerprint overlap doesn't apply — scope violations only tracked for AUTO_FIX
        # Instead, set violations via the ASSIST metric directly by patching the conn
        # We achieve this by overriding thresholds: make scope_violations_threshold=0 which
        # means even scope_violations=1 would block promotion.
        # But actually ASSIST metrics never have scope_violations (AUTO_FIX only).
        # The spec says "violations > 0 → NO_CHANGE" for ASSIST. Let's test via thresholds.
        # The real test is: if any ASSIST fp somehow has violations > 0 → NO_CHANGE.
        # We inject that by patching the metric directly after classification.
        from core.graduation import _classify_attempts_by_class as _cap
        metrics = _cap(conn)
        # Manually inject a violation count to test the guard
        for m in metrics.values():
            m.scope_violations = 1
        # Evaluate with injected metric
        import core.graduation as gm
        from unittest.mock import patch
        with patch.object(gm, "_classify_attempts_by_class", return_value=metrics):
            decisions = evaluate_graduation(conn, thresholds=_FIXED_THRESHOLDS)
        assert all(d.decision == "NO_CHANGE" for d in decisions)

    def test_demotes_auto_fix_too_many_violations(self):
        """AUTO_FIX class with 6 violations (> 5 threshold) → DEMOTE_TO_PERMANENT_ASSIST."""
        conn = self._auto_fix_conn(violations=6)
        decisions = evaluate_graduation(conn, thresholds=_FIXED_THRESHOLDS)
        demotions = [d for d in decisions if d.decision == "DEMOTE_TO_PERMANENT_ASSIST"]
        assert len(demotions) == 1
        assert "hot_class" in demotions[0].class_name

    def test_no_change_auto_fix_at_boundary(self):
        """Exactly 5 violations (NOT > 5) → NO_CHANGE (boundary: threshold is strict >)."""
        conn = self._auto_fix_conn(violations=5)
        decisions = evaluate_graduation(conn, thresholds=_FIXED_THRESHOLDS)
        assert all(d.decision == "NO_CHANGE" for d in decisions)

    def test_no_change_auto_fix_zero_violations(self):
        """AUTO_FIX class with 0 violations → NO_CHANGE (not enough violations to demote)."""
        conn = self._auto_fix_conn(violations=0)
        # Insert at least one AUTO_FIX row so the class exists
        _insert_auto_fix(conn, cls_name="stable_class", fp="stable_fp", status="merged")
        decisions = evaluate_graduation(conn, thresholds=_FIXED_THRESHOLDS)
        assert all(d.decision == "NO_CHANGE" for d in decisions)


# ── 14-18: write_graduation_decisions ─────────────────────────────────────────

class TestWriteGraduationDecisions:
    def _promote_decision(self) -> GraduationDecision:
        m = ClassMetric(name="fp:testfp", days_in_assist=15, merged_assist_count=5,
                        scope_violations=0, current_state="ASSIST")
        return GraduationDecision(
            class_name="fp:testfp", decision="PROMOTE_TO_AUTO_FIX",
            reason="days=15>=14 AND merged=5>=5 AND scope_violations=0",
            metric=m, decided_ts="2026-04-30T00:00:00",
        )

    def _demote_decision(self) -> GraduationDecision:
        m = ClassMetric(name="class:danger", scope_violations=6, current_state="AUTO_FIX")
        return GraduationDecision(
            class_name="class:danger", decision="DEMOTE_TO_PERMANENT_ASSIST",
            reason="scope_violations=6>5 in 60d",
            metric=m, decided_ts="2026-04-30T00:00:00",
        )

    def _no_change_decision(self) -> GraduationDecision:
        m = ClassMetric(name="fp:boring", days_in_assist=3, merged_assist_count=1,
                        current_state="ASSIST")
        return GraduationDecision(
            class_name="fp:boring", decision="NO_CHANGE",
            reason="days=3, merged=1, violations=0",
            metric=m, decided_ts="2026-04-30T00:00:00",
        )

    def test_promote_writes_graduation_phase(self):
        """PROMOTE_TO_AUTO_FIX decision → fix_audit_log row with phase='graduation'."""
        conn = _minimal_conn()
        write_graduation_decisions(conn, [self._promote_decision()])
        conn.commit()
        rows = conn.execute("SELECT phase, actor, decision FROM fix_audit_log").fetchall()
        assert len(rows) == 1
        assert rows[0]["phase"] == "graduation"
        assert rows[0]["actor"] == "graduation_engine"
        assert rows[0]["decision"] == "PROMOTE_TO_AUTO_FIX"

    def test_demote_writes_demotion_phase(self):
        """DEMOTE_TO_PERMANENT_ASSIST decision → fix_audit_log row with phase='demotion'."""
        conn = _minimal_conn()
        write_graduation_decisions(conn, [self._demote_decision()])
        conn.commit()
        rows = conn.execute("SELECT phase, decision FROM fix_audit_log").fetchall()
        assert len(rows) == 1
        assert rows[0]["phase"] == "demotion"
        assert rows[0]["decision"] == "DEMOTE_TO_PERMANENT_ASSIST"

    def test_no_change_writes_zero_rows(self):
        """NO_CHANGE decision → 0 audit-log rows written."""
        conn = _minimal_conn()
        n = write_graduation_decisions(conn, [self._no_change_decision()])
        conn.commit()
        assert n == 0
        count = conn.execute("SELECT COUNT(*) FROM fix_audit_log").fetchone()[0]
        assert count == 0

    def test_payload_json_contains_expected_fields(self):
        """payload_json includes class_name, days_in_assist, merged_assist_count, scope_violations."""
        conn = _minimal_conn()
        write_graduation_decisions(conn, [self._promote_decision()])
        conn.commit()
        row = conn.execute("SELECT payload_json FROM fix_audit_log").fetchone()
        payload = json.loads(row["payload_json"])
        assert payload["class_name"] == "fp:testfp"
        metric = payload["metric"]
        assert metric["days_in_assist"] == 15
        assert metric["merged_assist_count"] == 5
        assert metric["scope_violations"] == 0
        assert metric["current_state"] == "ASSIST"

    def test_multiple_decisions_persist_correctly(self):
        """Multiple non-NO_CHANGE decisions all persist; NO_CHANGE is skipped."""
        conn = _minimal_conn()
        decisions = [
            self._promote_decision(),
            self._demote_decision(),
            self._no_change_decision(),
        ]
        n = write_graduation_decisions(conn, decisions)
        conn.commit()
        assert n == 2  # NO_CHANGE skipped
        count = conn.execute("SELECT COUNT(*) FROM fix_audit_log").fetchone()[0]
        assert count == 2
        phases = {r["phase"] for r in conn.execute("SELECT phase FROM fix_audit_log").fetchall()}
        assert phases == {"graduation", "demotion"}


# ── 19-23: run() ──────────────────────────────────────────────────────────────

class TestRun:
    def test_empty_db_returns_evaluated_zero(self, tmp_path: Path):
        """run() on DB with no fix_attempts → evaluated=0."""
        db_path = _make_db_file(tmp_path)
        result = run(db_path=db_path)
        assert result["evaluated"] == 0
        assert result["promotions"] == []
        assert result["demotions"] == []

    def test_with_promotion_eligible_class(self, tmp_path: Path):
        """run() with 5+ merged ASSIST rows 15+ days back → promotions=[name]."""
        db_path = _make_db_file(tmp_path)
        _seed_assist_file(db_path, fp="grad_fp", count=5, days_back=15)
        result = run(db_path=db_path)
        assert "fp:grad_fp" in result["promotions"]

    def test_with_demotion_eligible_class(self, tmp_path: Path):
        """run() with 6 scope-violation AUTO_FIX rows → demotions=[name]."""
        db_path = _make_db_file(tmp_path)
        _seed_auto_fix_file(db_path, cls_name="bad_class", violation_count=6)
        result = run(db_path=db_path)
        assert "class:bad_class" in result["demotions"]

    def test_dry_run_does_not_write_audit_rows(self, tmp_path: Path):
        """run(dry_run=True) with promotion-eligible class → 0 rows in fix_audit_log."""
        db_path = _make_db_file(tmp_path)
        _seed_assist_file(db_path, fp="dry_fp", count=5, days_back=15)
        result = run(db_path=db_path, dry_run=True)
        assert result["dry_run"] is True
        assert result["promotions"]  # eligible
        # Verify no audit rows written
        conn = sqlite3.connect(db_path)
        count = conn.execute("SELECT COUNT(*) FROM fix_audit_log").fetchone()[0]
        conn.close()
        assert count == 0

    def test_non_dry_run_writes_audit_rows(self, tmp_path: Path):
        """run(dry_run=False) with promotion-eligible class → ≥1 row in fix_audit_log."""
        db_path = _make_db_file(tmp_path)
        _seed_assist_file(db_path, fp="live_fp", count=5, days_back=15)
        result = run(db_path=db_path, dry_run=False)
        assert not result["dry_run"]
        assert result["promotions"]
        conn = sqlite3.connect(db_path)
        count = conn.execute("SELECT COUNT(*) FROM fix_audit_log").fetchone()[0]
        conn.close()
        assert count >= 1


# ── 24-27: AUTO_FIX classification / scope violations ─────────────────────────

class TestAutoFixClassification:
    def test_class_name_extracted_from_notes(self):
        """notes='matched_class=test_import_error' → metric named 'class:test_import_error'."""
        conn = _minimal_conn()
        _insert_auto_fix(conn, cls_name="test_import_error", fp="fp_auto1", status="merged")
        metrics = _classify_attempts_by_class(conn)
        assert "class:test_import_error" in metrics

    def test_never_list_gate_increments_violations(self):
        """blocked_by_gate='no_never_list_touched' → scope_violations incremented by 1."""
        conn = _minimal_conn()
        _insert_auto_fix(conn, cls_name="cls_a", fp="fp1",
                         blocked_by_gate="no_never_list_touched", status="blocked")
        metrics = _classify_attempts_by_class(conn)
        assert metrics["class:cls_a"].scope_violations == 1

    def test_safety_critical_gate_increments_violations(self):
        """blocked_by_gate='no_safety_critical_function_modified' → scope_violations++."""
        conn = _minimal_conn()
        _insert_auto_fix(conn, cls_name="cls_b", fp="fp2",
                         blocked_by_gate="no_safety_critical_function_modified",
                         status="blocked")
        metrics = _classify_attempts_by_class(conn)
        assert metrics["class:cls_b"].scope_violations == 1

    def test_diff_size_gate_does_not_increment_violations(self):
        """blocked_by_gate='diff_size_cap' → scope_violations remains 0."""
        conn = _minimal_conn()
        _insert_auto_fix(conn, cls_name="cls_c", fp="fp3",
                         blocked_by_gate="diff_size_cap", status="blocked")
        metrics = _classify_attempts_by_class(conn)
        assert metrics["class:cls_c"].scope_violations == 0


# ── 28: Property test ─────────────────────────────────────────────────────────

class TestPropertyTests:
    def test_100_assist_attempts_same_fp_one_metric(self):
        """100 merged ASSIST rows with same fingerprint → 1 metric, merged_count=100."""
        conn = _minimal_conn()
        started_ts = _now_minus(days=20)
        for i in range(100):
            conn.execute(
                "INSERT INTO fix_attempts"
                " (error_id, fingerprint, started_ts, status, classification)"
                " VALUES (1, 'bigfp', ?, 'merged', 'ASSIST')",
                (started_ts,),
            )
        conn.commit()
        metrics = _classify_attempts_by_class(conn)
        assert len(metrics) == 1
        assert list(metrics.values())[0].merged_assist_count == 100


# ── 29-30: fix_audit_log immutability ─────────────────────────────────────────

class TestAuditLogImmutability:
    def test_insert_ok_update_blocked_by_trigger(self):
        """INSERT to fix_audit_log succeeds; subsequent UPDATE raises IntegrityError."""
        conn = _minimal_conn(with_triggers=True)
        # INSERT should succeed
        conn.execute(
            "INSERT INTO fix_audit_log (ts, phase, actor, decision)"
            " VALUES ('2026-04-30T00:00:00', 'graduation', 'graduation_engine', 'TEST')"
        )
        conn.commit()
        count = conn.execute("SELECT COUNT(*) FROM fix_audit_log").fetchone()[0]
        assert count == 1
        # UPDATE should raise IntegrityError (immutability trigger)
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute("UPDATE fix_audit_log SET decision='MUTATED' WHERE id=1")

    def test_graduation_engine_write_then_update_blocked(self):
        """After write_graduation_decisions() writes a row, UPDATE on that row → IntegrityError."""
        conn = _minimal_conn(with_triggers=True)
        m = ClassMetric(name="fp:testme", days_in_assist=15, merged_assist_count=5,
                        current_state="ASSIST")
        decision = GraduationDecision(
            class_name="fp:testme", decision="PROMOTE_TO_AUTO_FIX",
            reason="days=15>=14 AND merged=5>=5 AND scope_violations=0",
            metric=m, decided_ts="2026-04-30T00:00:00",
        )
        # Engine writes the row
        n = write_graduation_decisions(conn, [decision])
        conn.commit()
        assert n == 1
        # Engine row is visible
        count = conn.execute("SELECT COUNT(*) FROM fix_audit_log").fetchone()[0]
        assert count == 1
        # Attempting to UPDATE that row must fail with immutability trigger
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute("UPDATE fix_audit_log SET actor='hacked' WHERE id=1")
