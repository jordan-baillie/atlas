"""End-to-end tests for the Phase 2 auto-remediation pipeline.

Synthetic error -> triage -> fix -> review -> gates -> merge -> audit.
LLM calls are mocked. Real subprocess calls (git, pytest) are mocked
when they touch the test environment.

Test groups:
  A. Full pipeline -- IGNORE
  B. Full pipeline -- ESCALATE
  C. Full pipeline -- ASSIST (the main path)
  D. Full pipeline -- AUTO_FIX (Phase 3 disabled -- should never reach here)
  E. Multi-error batch processing
  F. Pipeline halt scenarios (every kill-switch layer)
  G. Reviewer reject path (gate 10 fails -> no merge)
  H. Gate failure cascade (e.g. diff > 30 lines -> blocked)
  I. Telegram silence on success / alert on failure
  J. Audit log immutability
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


# ---------------------------------------------------------------------------
# Schema bootstrap
# ---------------------------------------------------------------------------

def _bootstrap_db(path: Path) -> None:
    conn = sqlite3.connect(str(path))
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS errors (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      fingerprint TEXT UNIQUE NOT NULL,
      first_seen_ts TEXT NOT NULL,
      last_seen_ts TEXT NOT NULL,
      occurrence_count INTEGER NOT NULL DEFAULT 1,
      ts TEXT NOT NULL,
      source TEXT NOT NULL,
      service TEXT,
      level TEXT NOT NULL,
      logger_name TEXT,
      message TEXT NOT NULL,
      exc_type TEXT, exc_message TEXT, traceback TEXT,
      file_path TEXT, line_number INTEGER, function_name TEXT,
      pid INTEGER, hostname TEXT, context_json TEXT,
      market_hours INTEGER NOT NULL DEFAULT 0,
      halt_active INTEGER NOT NULL DEFAULT 0,
      git_sha TEXT,
      classification TEXT NOT NULL DEFAULT 'UNCLASSIFIED',
      triage_reason TEXT,
      tier INTEGER NOT NULL DEFAULT 99,
      remediation_status TEXT NOT NULL DEFAULT 'NEW',
      remediation_attempts INTEGER NOT NULL DEFAULT 0,
      last_attempt_at TEXT,
      fixed_by_attempt_id INTEGER,
      resolved_at TEXT,
      created_at TEXT NOT NULL DEFAULT (datetime('now'))
    );
    CREATE TABLE IF NOT EXISTS fix_attempts (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      error_id INTEGER NOT NULL,
      fingerprint TEXT NOT NULL,
      started_ts TEXT NOT NULL,
      finished_ts TEXT,
      status TEXT NOT NULL DEFAULT 'triaged',
      classification TEXT NOT NULL,
      triage_model TEXT, triage_reason TEXT, triage_tokens INTEGER,
      diagnosis_model TEXT, diagnosis_summary TEXT, diagnosis_tokens INTEGER,
      fix_model TEXT, fix_branch TEXT, fix_commit_sha TEXT, fix_diff_lines INTEGER,
      fix_tokens INTEGER,
      review_model TEXT, review_verdict TEXT, review_confidence REAL,
      review_reason TEXT, review_tokens INTEGER,
      test_results_json TEXT, gates_passed_json TEXT, gates_failed_json TEXT,
      blocked_by_gate TEXT,
      revert_commit_sha TEXT, revert_reason TEXT, reverted_ts TEXT,
      monitor_outcome TEXT, total_wall_seconds REAL, notes TEXT
    );
    CREATE TABLE IF NOT EXISTS fix_audit_log (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      attempt_id INTEGER, error_id INTEGER,
      ts TEXT NOT NULL DEFAULT (datetime('now')),
      phase TEXT NOT NULL, actor TEXT NOT NULL,
      model TEXT, decision TEXT, reasoning TEXT, diff TEXT,
      payload_json TEXT, duration_sec REAL, tokens_in INTEGER, tokens_out INTEGER,
      cost_usd REAL DEFAULT 0,
      result_status TEXT, blocked_by_gate TEXT, notes TEXT
    );
    CREATE TABLE IF NOT EXISTS portfolio_snapshots (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      timestamp TEXT NOT NULL,
      equity REAL,
      daily_pnl_pct REAL,
      market_id TEXT
    );
    """)
    conn.commit()
    conn.close()


def _insert_error(
    db_path: Path,
    *,
    fingerprint: str = "fp_test",
    message: str = "Generic test error",
    exc_type: str = "TypeError",
    file_path: str | None = "tests/test_foo.py",
    function_name: str | None = "test_foo",
    traceback: str | None = None,
    classification: str = "UNCLASSIFIED",
    remediation_status: str = "NEW",
) -> dict:
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """INSERT OR IGNORE INTO errors
            (fingerprint, first_seen_ts, last_seen_ts, ts, source, service, level,
             logger_name, message, exc_type, file_path, line_number, function_name,
             classification, tier, remediation_status)
           VALUES (?, '2026-04-29T10:00:00', '2026-04-29T10:00:00', '2026-04-29T10:00:00',
                   'python_logger', 'test_service', 'ERROR', 'atlas.test',
                   ?, ?, ?, 42, ?, ?, 99, ?)""",
        (fingerprint, message, exc_type, file_path, function_name,
         classification, remediation_status),
    )
    conn.commit()
    row_id = conn.execute(
        "SELECT id FROM errors WHERE fingerprint=?", (fingerprint,)
    ).fetchone()[0]
    if traceback:
        conn.execute("UPDATE errors SET traceback=? WHERE id=?", (traceback, row_id))
        conn.commit()
    conn.close()
    return {
        "id": row_id, "fingerprint": fingerprint, "message": message,
        "exc_type": exc_type, "file_path": file_path, "function_name": function_name,
        "traceback": traceback, "service": "test_service", "level": "ERROR",
        "logger_name": "atlas.test", "line_number": 42, "occurrence_count": 1,
    }


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    db = tmp_path / "atlas.db"
    _bootstrap_db(db)
    return db


@pytest.fixture
def synthetic_error(db_path: Path) -> dict:
    return _insert_error(
        db_path,
        fingerprint="fp_synth_001",
        message="TypeError: 'NoneType' has no attribute 'foo'",
        exc_type="TypeError",
        file_path="tests/test_foo.py",
        function_name="test_foo",
    )


# ---------------------------------------------------------------------------
# Group A -- IGNORE pipeline
# ---------------------------------------------------------------------------

class TestPipelineIgnore:
    def test_circuit_breaker_message_classified_ignore(self):
        from core.triage import TriageClassifier
        c = TriageClassifier()
        e = {
            "message": "Circuit breaker tripped: daily loss exceeded",
            "exc_type": None, "file_path": None, "line_number": None,
            "function_name": None, "traceback": None,
        }
        with patch.object(TriageClassifier, "is_market_hours_now", return_value=False), \
             patch.object(TriageClassifier, "is_halt_active", return_value=False):
            r = c.classify(e)
        assert r.classification == "IGNORE"
        assert "Circuit breaker tripped" in r.reason

    def test_execution_blocked_plan_status_ignore(self):
        """'Execution blocked: Plan status is' is pure noise, not a trading failure."""
        from core.triage import TriageClassifier
        c = TriageClassifier()
        e = {
            "message": "Execution blocked: Plan status is REJECTED",
            "exc_type": None, "file_path": None, "line_number": None,
            "function_name": None, "traceback": None,
        }
        with patch.object(TriageClassifier, "is_market_hours_now", return_value=False), \
             patch.object(TriageClassifier, "is_halt_active", return_value=False):
            r = c.classify(e)
        assert r.classification == "IGNORE"

    def test_execution_blocked_plan_status_classified_ignore(self):
        from core.triage import TriageClassifier
        c = TriageClassifier()
        e = {
            "message": "Execution blocked: Plan status is REJECTED",
            "exc_type": None, "file_path": None, "line_number": None,
            "function_name": None, "traceback": None,
        }
        with patch.object(TriageClassifier, "is_market_hours_now", return_value=False), \
             patch.object(TriageClassifier, "is_halt_active", return_value=False):
            r = c.classify(e)
        assert r.classification == "IGNORE"

    def test_ignore_does_not_appear_in_fetch_unclassified(self, db_path, synthetic_error):
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        conn.execute(
            "UPDATE errors SET classification='IGNORE', remediation_status='IGNORED' WHERE id=?",
            (synthetic_error["id"],),
        )
        conn.commit()
        from core import error_monitor
        rows = error_monitor.fetch_unclassified(conn, limit=50)
        conn.close()
        assert len(rows) == 0

    def test_run_once_marks_circuit_breaker_ignored(self, db_path):
        _insert_error(
            db_path,
            fingerprint="fp_cb_run",
            message="Circuit breaker tripped: session loss limit",
            file_path=None,
            function_name=None,
        )
        from core import error_monitor
        from core.triage import TriageClassifier
        with patch.object(TriageClassifier, "is_market_hours_now", return_value=False), \
             patch.object(TriageClassifier, "is_halt_active", return_value=False):
            result = error_monitor.run_once(db_path=str(db_path), dry_run=True)
        assert result["processed"] >= 1
        conn = sqlite3.connect(str(db_path))
        row = conn.execute(
            "SELECT remediation_status FROM errors WHERE fingerprint='fp_cb_run'"
        ).fetchone()
        conn.close()
        assert row[0] == "IGNORED"


# ---------------------------------------------------------------------------
# Group B -- ESCALATE pipeline
# ---------------------------------------------------------------------------

class TestPipelineEscalate:
    def test_brokers_path_escalate(self):
        from core.triage import TriageClassifier
        c = TriageClassifier()
        e = {
            "message": "broker timeout connecting",
            "exc_type": None,
            "file_path": "brokers/alpaca/broker.py",
            "line_number": 100,
            "function_name": "connect",
            "traceback": None,
        }
        r = c.classify(e)
        assert r.classification == "ESCALATE"
        assert r.tier == 0

    def test_kill_switch_path_escalate(self):
        from core.triage import TriageClassifier
        c = TriageClassifier()
        e = {
            "message": "unexpected error",
            "exc_type": None,
            "file_path": "brokers/kill_switch.py",
            "line_number": 1,
            "function_name": None,
            "traceback": None,
        }
        r = c.classify(e)
        assert r.classification == "ESCALATE"

    def test_risk_path_escalate(self):
        from core.triage import TriageClassifier
        c = TriageClassifier()
        e = {
            "message": "value error",
            "exc_type": "ValueError",
            "file_path": "risk/portfolio_var.py",
            "line_number": 42,
            "function_name": "compute",
            "traceback": None,
        }
        r = c.classify(e)
        assert r.classification == "ESCALATE"
        assert r.tier == 0

    def test_message_pattern_broker_escalate(self):
        from core.triage import TriageClassifier
        c = TriageClassifier()
        e = {
            "message": "Failed to connect to broker API",
            "exc_type": None, "file_path": None, "line_number": None,
            "function_name": None, "traceback": None,
        }
        r = c.classify(e)
        assert r.classification == "ESCALATE"

    def test_default_deny_escalates_unknown_errors(self):
        from core.triage import TriageClassifier
        c = TriageClassifier()
        e = {
            "message": "Something weird happened with the graph computation",
            "exc_type": "RuntimeError",
            "file_path": "utils/graph_helper.py",
            "line_number": 99,
            "function_name": "compute_graph",
            "traceback": None,
        }
        with patch.object(TriageClassifier, "is_market_hours_now", return_value=False), \
             patch.object(TriageClassifier, "is_halt_active", return_value=False):
            r = c.classify(e)
        assert r.classification == "ESCALATE"
        assert r.rule_id == "default_deny"


# ---------------------------------------------------------------------------
# Group C -- ASSIST pipeline (the main path)
# ---------------------------------------------------------------------------

class TestPipelineAssist:
    def test_services_path_permanent_assist(self):
        from core.triage import TriageClassifier
        c = TriageClassifier()
        e = {
            "message": "AttributeError in chat handler",
            "exc_type": "AttributeError",
            "file_path": "services/chat_server.py",
            "line_number": 200,
            "function_name": "handle_chat",
            "traceback": None,
        }
        with patch.object(TriageClassifier, "is_market_hours_now", return_value=False), \
             patch.object(TriageClassifier, "is_halt_active", return_value=False):
            r = c.classify(e)
        assert r.classification == "ASSIST"
        assert "services" in r.rule_id

    def test_full_assist_pipeline_with_approval(self, db_path, synthetic_error, tmp_path, monkeypatch):
        from core import fix_worker, remediation_kill_switch as ks
        monkeypatch.setattr(ks, "check_all_layers", lambda **kw: None)
        monkeypatch.setattr(fix_worker, "preflight_oauth", lambda: True)

        worktree = tmp_path / "wt"
        worktree.mkdir()
        (worktree / "tests").mkdir()
        (worktree / "tests" / "test_fix.py").write_text("def test_new(): assert True\n")
        monkeypatch.setattr(
            fix_worker, "create_worktree",
            lambda eid, fp: (worktree, f"auto-fix/err-{eid}-{fp[:8]}"),
        )

        def _mock_pi_invoke(prompt, wt, timeout_sec=600):
            return (0, json.dumps({
                "status": "PROPOSED",
                "branch": "auto-fix/err-1",
                "diff_lines": 8,
                "diagnosis": "fixed null pointer",
                "fix_reasoning": "added None guard",
            }), "")
        monkeypatch.setattr(fix_worker, "invoke_fix_worker_via_pi_team", _mock_pi_invoke)
        monkeypatch.setattr(
            fix_worker, "capture_diff",
            lambda wt, br: ("--- a\n+++ b\n+def test_new(): assert True\n", 8),
        )

        outcome = fix_worker.run_fix(synthetic_error, classification="ASSIST")
        assert outcome.success
        assert outcome.diff_lines == 8
        assert outcome.diagnosis == "fixed null pointer"

    def test_run_fix_dry_run_success(self, db_path, synthetic_error, tmp_path, monkeypatch):
        from core import fix_worker, remediation_kill_switch as ks
        monkeypatch.setattr(ks, "check_all_layers", lambda **kw: None)
        worktree = tmp_path / "wt_dry"
        worktree.mkdir()
        monkeypatch.setattr(
            fix_worker, "create_worktree",
            lambda eid, fp: (worktree, f"auto-fix/err-{eid}"),
        )
        outcome = fix_worker.run_fix(synthetic_error, classification="ASSIST", dry_run=True)
        assert outcome.success
        assert outcome.diagnosis == "DRY_RUN: prompt constructed, worker not invoked"
        assert outcome.fix_reasoning == "DRY_RUN"

    def test_run_fix_returns_fix_outcome_type(self, db_path, synthetic_error, tmp_path, monkeypatch):
        from core import fix_worker, remediation_kill_switch as ks
        from core.fix_worker import FixOutcome
        monkeypatch.setattr(ks, "check_all_layers", lambda **kw: None)
        monkeypatch.setattr(fix_worker, "preflight_oauth", lambda: True)
        worktree = tmp_path / "wt_type"
        worktree.mkdir()
        monkeypatch.setattr(
            fix_worker, "create_worktree",
            lambda eid, fp: (worktree, "auto-fix/err-test"),
        )
        monkeypatch.setattr(
            fix_worker, "invoke_fix_worker_via_pi_team",
            lambda p, w, timeout_sec=600: (1, "", "pi error"),
        )
        outcome = fix_worker.run_fix(synthetic_error, classification="ASSIST")
        assert isinstance(outcome, FixOutcome)
        assert not outcome.success
        assert outcome.error is not None


# ---------------------------------------------------------------------------
# Group D -- Phase 3 disabled gate
# ---------------------------------------------------------------------------

class TestPhase3DisabledNoAutoFix:
    def test_classifier_returns_no_auto_fix_in_phase_1_2(self):
        from core.triage import TriageClassifier
        c = TriageClassifier()
        e = {
            "message": "Test fixture stale data",
            "exc_type": "AssertionError",
            "file_path": "tests/fixtures/data.json",
            "line_number": 1,
            "function_name": None,
            "traceback": None,
        }
        with patch.object(TriageClassifier, "is_market_hours_now", return_value=False), \
             patch.object(TriageClassifier, "is_halt_active", return_value=False):
            r = c.classify(e)
        assert r.classification != "AUTO_FIX", f"got {r.classification}"

    def test_phase_3_enabled_false_in_config(self):
        import yaml
        with open(PROJECT_ROOT / "config" / "auto_remediation.yaml") as f:
            cfg = yaml.safe_load(f)
        assert cfg["phase"]["phase_3_enabled"] is False

    def test_whitelist_check_returns_none_when_phase3_disabled(self):
        from core.triage import TriageClassifier
        c = TriageClassifier()
        c._phase_3_enabled = False
        e = {
            "message": "import error in test",
            "exc_type": "ImportError",
            "file_path": "tests/test_utils.py",
            "line_number": 1,
            "function_name": None,
            "traceback": None,
        }
        result = c._check_auto_fix_whitelist(e)
        assert result is None


# ---------------------------------------------------------------------------
# Group E -- Multi-error batch processing
# ---------------------------------------------------------------------------

class TestMultiErrorBatch:
    def test_batch_of_five_errors_all_processed(self, db_path):
        for i in range(5):
            _insert_error(
                db_path,
                fingerprint=f"fp_batch_{i}",
                message=f"Batch test error {i}: computation went wrong",
            )
        from core import error_monitor
        result = error_monitor.run_once(db_path=str(db_path), dry_run=True)
        assert result["processed"] == 5
        assert result["errors"] == 0
        assert result["halted"] is False

    def test_batch_size_limit_respected(self, db_path):
        for i in range(20):
            _insert_error(
                db_path,
                fingerprint=f"fp_limit_{i}",
                message=f"Limit test error {i}: something happened",
            )
        from core import error_monitor
        result = error_monitor.run_once(db_path=str(db_path), batch_size=5, dry_run=True)
        assert result["processed"] == 5

    def test_already_classified_errors_not_reprocessed(self, db_path):
        _insert_error(
            db_path, fingerprint="fp_already_done",
            message="Previous run error",
            classification="ESCALATE",
            remediation_status="ESCALATED",
        )
        _insert_error(
            db_path, fingerprint="fp_new_unclassified",
            message="Fresh test error for new sweep",
        )
        from core import error_monitor
        result = error_monitor.run_once(db_path=str(db_path), dry_run=True)
        assert result["processed"] == 1

    def test_batch_by_class_counts_sum_equals_processed(self, db_path):
        for i in range(4):
            _insert_error(
                db_path,
                fingerprint=f"fp_sum_{i}",
                message=f"Sum test {i}: non-trading message",
            )
        from core import error_monitor
        result = error_monitor.run_once(db_path=str(db_path), dry_run=True)
        assert result["processed"] == 4
        assert sum(result["by_class"].values()) == 4

    def test_run_once_returns_metrics_dict(self, db_path):
        _insert_error(db_path, fingerprint="fp_metrics", message="test error for metrics")
        from core import error_monitor
        result = error_monitor.run_once(db_path=str(db_path), dry_run=True)
        for key in ("halted", "halt_reason", "processed", "by_class", "errors", "dry_run"):
            assert key in result, f"Missing key: {key}"


# ---------------------------------------------------------------------------
# Group F -- Pipeline halt scenarios
# ---------------------------------------------------------------------------

class TestKillSwitchLayersInPipeline:
    def test_l1_env_halts_pipeline(self, db_path, synthetic_error, monkeypatch):
        monkeypatch.setenv("ATLAS_AUTO_REMEDIATION_DISABLED", "1")
        from core import fix_worker
        outcome = fix_worker.run_fix(synthetic_error, classification="ASSIST", dry_run=False)
        assert not outcome.success
        assert "kill-switch" in outcome.error or "L1" in outcome.error

    def test_l2_halt_file_halts_pipeline(self, tmp_path, monkeypatch):
        from core import remediation_kill_switch as ks, fix_worker
        monkeypatch.setattr(ks, "PROJECT_ROOT", tmp_path)
        (tmp_path / "data").mkdir()
        (tmp_path / "data" / "AUTO_REMEDIATION_HALT").write_text("manual halt")
        outcome = fix_worker.run_fix(
            {"id": 1, "fingerprint": "x", "message": "test", "file_path": None,
             "exc_type": None, "traceback": None, "function_name": None},
            classification="ASSIST", dry_run=False,
        )
        assert not outcome.success
        assert outcome.error is not None

    def test_l3_trading_halt_halts_pipeline(self, tmp_path, monkeypatch):
        from core import remediation_kill_switch as ks, fix_worker
        monkeypatch.setattr(ks, "PROJECT_ROOT", tmp_path)
        (tmp_path / "data").mkdir()
        (tmp_path / "data" / "HALT").write_text("trading halt active")
        outcome = fix_worker.run_fix(
            {"id": 2, "fingerprint": "y", "message": "test", "file_path": None,
             "exc_type": None, "traceback": None, "function_name": None},
            classification="ASSIST", dry_run=False,
        )
        assert not outcome.success
        assert outcome.error is not None

    def test_run_once_halted_returns_halted_true(self, db_path, monkeypatch):
        monkeypatch.setenv("ATLAS_AUTO_REMEDIATION_DISABLED", "1")
        _insert_error(db_path, fingerprint="fp_halt_check", message="test error")
        from core import error_monitor
        result = error_monitor.run_once(db_path=str(db_path), dry_run=True)
        assert result["halted"] is True
        assert result["processed"] == 0
        assert result["halt_reason"] is not None

    def test_run_once_halted_does_not_write_audit_log(self, db_path, monkeypatch):
        monkeypatch.setenv("ATLAS_AUTO_REMEDIATION_DISABLED", "1")
        _insert_error(db_path, fingerprint="fp_no_audit", message="test error")
        from core import error_monitor
        error_monitor.run_once(db_path=str(db_path), dry_run=True)
        conn = sqlite3.connect(str(db_path))
        count = conn.execute("SELECT COUNT(*) FROM fix_audit_log").fetchone()[0]
        conn.close()
        assert count == 0

    def test_kill_switch_check_happens_before_worktree_creation(
        self, tmp_path, monkeypatch
    ):
        """L1 fires before create_worktree is ever called."""
        monkeypatch.setenv("ATLAS_AUTO_REMEDIATION_DISABLED", "1")
        create_worktree_called = []
        from core import fix_worker
        original_cw = fix_worker.create_worktree

        def _spy_create_worktree(eid, fp):
            create_worktree_called.append((eid, fp))
            return original_cw(eid, fp)

        monkeypatch.setattr(fix_worker, "create_worktree", _spy_create_worktree)
        fix_worker.run_fix(
            {"id": 99, "fingerprint": "ks_test", "message": "test",
             "file_path": None, "exc_type": None, "traceback": None, "function_name": None},
            classification="ASSIST", dry_run=False,
        )
        assert len(create_worktree_called) == 0

    def test_l2_halt_resume_clears_block(self, tmp_path, monkeypatch):
        from core import remediation_kill_switch as ks
        monkeypatch.setattr(ks, "PROJECT_ROOT", tmp_path)
        (tmp_path / "data").mkdir()
        halt_path = tmp_path / "data" / "AUTO_REMEDIATION_HALT"
        halt_path.write_text("manual halt")
        assert ks.check_l2_remediation_halt() is not None
        ks.resume()
        assert ks.check_l2_remediation_halt() is None


# ---------------------------------------------------------------------------
# Group G -- Reviewer reject path
# ---------------------------------------------------------------------------

class TestReviewerRejectBlocksMerge:
    def test_reviewer_reject_blocks_merge(self):
        from core import merge_gates
        review_outcome = SimpleNamespace(
            success=True, verdict="REJECT", confidence=0.3,
            addresses_root_cause=False, could_lose_money=True,
            reject_reasons=["fix doesn't address root cause"],
        )
        gate = merge_gates.gate_reviewer_approved(review_outcome)
        assert not gate.passed
        assert gate.detail["verdict"] == "REJECT"

    def test_reviewer_low_confidence_gate_only_checks_verdict(self):
        # gate_reviewer_approved checks verdict (reviewer already applied confidence threshold)
        from core import merge_gates
        review_outcome = SimpleNamespace(
            success=True, verdict="APPROVE", confidence=0.5,
            addresses_root_cause=True, could_lose_money=False,
            reject_reasons=[],
        )
        gate = merge_gates.gate_reviewer_approved(review_outcome)
        assert gate.passed  # gate trusts reviewer's verdict

    def test_reviewer_none_blocks_merge(self):
        from core import merge_gates
        gate = merge_gates.gate_reviewer_approved(None)
        assert not gate.passed
        assert "no reviewer run" in gate.detail.get("error", "")

    def test_reviewer_approve_high_confidence_passes_gate(self):
        from core import merge_gates
        review_outcome = SimpleNamespace(
            success=True, verdict="APPROVE", confidence=0.95,
            addresses_root_cause=True, could_lose_money=False,
            reject_reasons=[],
        )
        gate = merge_gates.gate_reviewer_approved(review_outcome)
        assert gate.passed
        assert gate.detail["verdict"] == "APPROVE"


# ---------------------------------------------------------------------------
# Group H -- Gate failure cascade
# ---------------------------------------------------------------------------

class TestGateFailureCascade:
    def test_oversized_diff_blocks(self, tmp_path):
        from core import merge_gates
        big_diff = "\n".join(f"+line_{i}: changed content" for i in range(50))
        with patch.object(merge_gates, "_git_diff", return_value=big_diff):
            gate = merge_gates.gate_diff_size_cap(tmp_path, "branch", max_lines=30)
        assert not gate.passed
        assert gate.detail["total"] >= 50

    def test_exact_boundary_30_lines_passes(self, tmp_path):
        from core import merge_gates
        diff_30 = "\n".join(f"+line_{i}: changed" for i in range(30))
        with patch.object(merge_gates, "_git_diff", return_value=diff_30):
            gate = merge_gates.gate_diff_size_cap(tmp_path, "branch", max_lines=30)
        assert gate.passed
        assert gate.detail["total"] == 30

    def test_missing_regression_test_blocks(self, tmp_path):
        from core import merge_gates
        with patch.object(merge_gates, "_git_files_in_branch", return_value=["src/foo.py"]), \
             patch.object(merge_gates, "_git_diff", return_value="+def foo(): pass\n"):
            gate = merge_gates.gate_regression_test_present(tmp_path, "branch")
        assert not gate.passed

    def test_new_test_function_in_diff_passes(self, tmp_path):
        from core import merge_gates
        diff_with_test = "+def test_foo_handles_none():\n+    assert foo(None) == 0\n"
        with patch.object(merge_gates, "_git_files_in_branch", return_value=["src/foo.py"]), \
             patch.object(merge_gates, "_git_diff", return_value=diff_with_test):
            gate = merge_gates.gate_regression_test_present(tmp_path, "branch")
        assert gate.passed
        assert gate.detail["new_test_functions_added"] == 1

    def test_never_list_path_blocks(self, tmp_path):
        from core import merge_gates
        with patch.object(
            merge_gates, "_git_files_in_branch",
            return_value=["brokers/live_executor.py"],
        ):
            gate = merge_gates.gate_no_never_list_touched(tmp_path, "branch")
        assert not gate.passed
        assert any("brokers" in v[1] for v in gate.detail["violations"])

    def test_risk_path_triggers_never_list_gate(self, tmp_path):
        from core import merge_gates
        with patch.object(
            merge_gates, "_git_files_in_branch",
            return_value=["risk/portfolio_var.py"],
        ):
            gate = merge_gates.gate_no_never_list_touched(tmp_path, "branch")
        assert not gate.passed

    def test_safe_path_passes_never_list_gate(self, tmp_path):
        from core import merge_gates
        with patch.object(
            merge_gates, "_git_files_in_branch",
            return_value=["tests/test_utils.py"],
        ):
            gate = merge_gates.gate_no_never_list_touched(tmp_path, "branch")
        assert gate.passed


# ---------------------------------------------------------------------------
# Group I -- Telegram silence on success / alert on failure
# ---------------------------------------------------------------------------

class TestTelegramBehavior:
    def test_no_telegram_on_successful_merge(self, tmp_path):
        from core import merger
        from core.merge_gates import GateRunOutcome

        passing_gates = GateRunOutcome(
            all_passed=True,
            results=[],
            summary={"passed": ["g1", "g2"], "failed": [], "blocking_failures": []},
        )
        fix_out = SimpleNamespace(
            success=True, attempt_id=-1, error_id=1,
            fingerprint="fp_success_tg", branch="auto-fix/err-1",
            worktree=str(tmp_path),
        )
        rev_out = SimpleNamespace(
            success=True, verdict="APPROVE", confidence=0.95,
            addresses_root_cause=True, could_lose_money=False,
            reject_reasons=[],
        )
        with patch("utils.telegram.send_message") as mock_tg, \
             patch("core.merger.run_all_gates", return_value=passing_gates), \
             patch("core.merger.push_to_staging", return_value=(True, "abc123sha")), \
             patch("core.merger._persist_merge_result"):
            outcome = merger.merge_fix(fix_out, rev_out)

        assert mock_tg.call_count == 0
        assert outcome.success

    def test_telegram_called_on_gate_failure(self, tmp_path):
        from core import merger
        from core.merge_gates import GateRunOutcome, GateResult

        failing_gates = GateRunOutcome(
            all_passed=False,
            results=[GateResult("diff_size_cap", False, {"total": 50})],
            summary={
                "passed": [],
                "failed": ["diff_size_cap"],
                "blocking_failures": ["diff_size_cap"],
            },
        )
        fix_out = SimpleNamespace(
            success=True, attempt_id=-1, error_id=2,
            fingerprint="fp_fail_tg", branch="auto-fix/err-2",
            worktree=str(tmp_path),
        )
        with patch("utils.telegram.send_message") as mock_tg, \
             patch("core.merger.run_all_gates", return_value=failing_gates), \
             patch("core.merger._persist_merge_result"):
            merger.merge_fix(fix_out, None)

        assert mock_tg.call_count >= 1

    def test_failure_alert_message_contains_fingerprint(self):
        from core import merger
        outcome = merger.MergeOutcome(
            success=False, error_id=3,
            fingerprint="fp_alert_check",
            branch="auto-fix/err-3",
            blocking_failures=["reviewer_approved"],
        )
        with patch("utils.telegram.send_message") as mock_tg:
            merger._send_failure_alert(outcome)

        assert mock_tg.called
        msg = mock_tg.call_args[0][0]
        assert "fp_alert_check" in msg

    def test_failure_alert_always_sends_on_call(self):
        """_send_failure_alert always sends (the success guard lives in merge_fix)."""
        from core import merger
        calls = []
        with patch("utils.telegram.send_message", side_effect=lambda m: calls.append(m)):
            for i in range(5):
                merger._send_failure_alert(
                    merger.MergeOutcome(
                        success=False, error_id=i, fingerprint=f"fp_{i}",
                        branch="b", blocking_failures=["g"],
                    )
                )
        assert len(calls) == 5


# ---------------------------------------------------------------------------
# Group J -- Audit log immutability
# ---------------------------------------------------------------------------

class TestAuditLogImmutability:
    def test_pk_constraint_prevents_duplicate_id_insert(self, db_path):
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "INSERT INTO fix_audit_log (id, ts, phase, actor, decision) "
            "VALUES (1, '2026-04-29T10:00:00', 'triage', 'classifier', 'ESCALATE')"
        )
        conn.commit()
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO fix_audit_log (id, ts, phase, actor, decision) "
                "VALUES (1, '2026-04-29T11:00:00', 'triage', 'classifier', 'IGNORE')"
            )
            conn.commit()
        conn.close()

    def test_run_once_appends_one_audit_row_per_error(self, db_path):
        _insert_error(db_path, fingerprint="fp_audit_mono",
                      message="Audit monotone test error")
        from core import error_monitor
        error_monitor.run_once(db_path=str(db_path), dry_run=True)

        conn = sqlite3.connect(str(db_path))
        count = conn.execute("SELECT COUNT(*) FROM fix_audit_log").fetchone()[0]
        conn.close()
        assert count == 1

        # Second run: already classified -> no new audit rows
        error_monitor.run_once(db_path=str(db_path), dry_run=True)
        conn = sqlite3.connect(str(db_path))
        count2 = conn.execute("SELECT COUNT(*) FROM fix_audit_log").fetchone()[0]
        conn.close()
        assert count2 == 1

    def test_audit_log_rows_have_correct_phase_and_actor(self, db_path):
        _insert_error(db_path, fingerprint="fp_audit_meta",
                      message="Audit metadata test error")
        from core import error_monitor
        error_monitor.run_once(db_path=str(db_path), dry_run=True)

        conn = sqlite3.connect(str(db_path))
        row = conn.execute(
            "SELECT phase, actor, result_status FROM fix_audit_log ORDER BY id DESC LIMIT 1"
        ).fetchone()
        conn.close()
        assert row is not None
        assert row[0] == "triage"
        assert row[1] == "classifier"
        assert row[2] == "success"

    def test_audit_log_dry_run_flag_in_payload(self, db_path):
        _insert_error(db_path, fingerprint="fp_dry_audit",
                      message="Dry run audit test error")
        from core import error_monitor
        error_monitor.run_once(db_path=str(db_path), dry_run=True)

        conn = sqlite3.connect(str(db_path))
        row = conn.execute(
            "SELECT payload_json FROM fix_audit_log ORDER BY id DESC LIMIT 1"
        ).fetchone()
        conn.close()
        assert row is not None
        payload = json.loads(row[0])
        assert payload["dry_run"] is True
