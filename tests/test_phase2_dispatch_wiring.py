"""Phase 2 dispatch wiring tests — verify error_monitor.run_once() dispatches
fix_worker → reviewer → merger when dry_run=False AND classification=ASSIST.

Phase 1 dry_run=True path is already tested in test_error_monitor.py. This
file adds the missing Phase 2 path coverage.

Implementation notes:
- tests that rely on ASSIST classification (services/foo.py) also mock
  TriageClassifier.is_market_hours_now and is_halt_active so the tests are
  deterministic regardless of wall-clock time and halt-file state.
- tests that rely on ESCALATE/IGNORE use brokers/** (NEVER-list, layer 1,
  unaffected by halt/market-hours) and "Circuit breaker tripped" (IGNORE
  pattern, still fires during market hours per triage.py layer 3 logic).
"""
import sqlite3
import sys
import types
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch, call

import pytest
import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


# ---------------------------------------------------------------------------
# DB bootstrap (same schema as test_error_monitor.py)
# ---------------------------------------------------------------------------

def _bootstrap(path: Path) -> None:
    conn = sqlite3.connect(str(path))
    conn.executescript("""
    CREATE TABLE errors (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      fingerprint TEXT UNIQUE NOT NULL,
      first_seen_ts TEXT, last_seen_ts TEXT, occurrence_count INTEGER DEFAULT 1,
      ts TEXT NOT NULL, source TEXT NOT NULL, service TEXT,
      level TEXT NOT NULL, logger_name TEXT, message TEXT NOT NULL,
      exc_type TEXT, exc_message TEXT, traceback TEXT,
      file_path TEXT, line_number INTEGER, function_name TEXT,
      pid INTEGER, hostname TEXT, context_json TEXT,
      market_hours INTEGER DEFAULT 0, halt_active INTEGER DEFAULT 0, git_sha TEXT,
      classification TEXT DEFAULT 'UNCLASSIFIED', triage_reason TEXT,
      tier INTEGER DEFAULT 99,
      remediation_status TEXT DEFAULT 'NEW', remediation_attempts INTEGER DEFAULT 0,
      last_attempt_at TEXT, fixed_by_attempt_id INTEGER, resolved_at TEXT,
      created_at TEXT
    );
    CREATE TABLE fix_attempts (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      error_id INTEGER NOT NULL, fingerprint TEXT NOT NULL,
      started_ts TEXT NOT NULL, finished_ts TEXT, reverted_ts TEXT,
      status TEXT NOT NULL, classification TEXT NOT NULL,
      review_verdict TEXT, gates_passed_json TEXT, gates_failed_json TEXT,
      blocked_by_gate TEXT, fix_branch TEXT, fix_commit_sha TEXT, fix_diff_lines INTEGER,
      monitor_outcome TEXT, total_wall_seconds REAL, notes TEXT,
      review_confidence REAL, review_reason TEXT
    );
    CREATE TABLE fix_audit_log (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      attempt_id INTEGER, error_id INTEGER,
      ts TEXT NOT NULL, phase TEXT NOT NULL, actor TEXT NOT NULL,
      model TEXT, decision TEXT, reasoning TEXT, diff TEXT,
      payload_json TEXT, duration_sec REAL, tokens_in INTEGER, tokens_out INTEGER,
      cost_usd REAL DEFAULT 0,
      result_status TEXT, blocked_by_gate TEXT, notes TEXT
    );
    """)
    conn.commit()
    conn.close()


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    db = tmp_path / "test.db"
    _bootstrap(db)
    return db


def _insert_error(
    db: Path,
    fingerprint: str,
    file_path: str,
    message: str = "test error",
) -> int:
    conn = sqlite3.connect(str(db))
    conn.execute(
        """INSERT INTO errors (fingerprint, first_seen_ts, last_seen_ts, ts,
            source, service, level, message, file_path)
            VALUES (?, '2026-04-29T10:00', '2026-04-29T10:00', '2026-04-29T10:00',
                    'python_logger', 'test', 'ERROR', ?, ?)""",
        (fingerprint, message, file_path),
    )
    conn.commit()
    eid = conn.execute(
        "SELECT id FROM errors WHERE fingerprint=?", (fingerprint,)
    ).fetchone()[0]
    conn.close()
    return eid


# ---------------------------------------------------------------------------
# Shared helpers for ASSIST-path tests: patch out market-hours + halt-active
# so classification is deterministic regardless of wall clock and halt-file state.
# ---------------------------------------------------------------------------

def _patch_assist_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Prevent TriageClassifier.is_market_hours_now / is_halt_active from
    blocking ASSIST classification due to test-environment state."""
    monkeypatch.setattr(
        "core.triage.TriageClassifier.is_market_hours_now",
        staticmethod(lambda *a, **kw: False),
    )
    monkeypatch.setattr(
        "core.triage.TriageClassifier.is_halt_active",
        staticmethod(lambda *a, **kw: False),
    )


# ---------------------------------------------------------------------------
# The 15 spec tests
# ---------------------------------------------------------------------------

def test_dry_run_true_does_not_dispatch_fix_worker(db_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Phase 1 behavior preserved: dry_run=True never calls fix_worker."""
    _insert_error(db_path, "fp1", "tests/foo.py")  # classification irrelevant for dry_run
    from core import error_monitor

    monkeypatch.setattr(error_monitor, "find_halt_reason", lambda: None)
    fix_worker_called = MagicMock()
    monkeypatch.setattr("core.fix_worker.run_fix", fix_worker_called)

    metrics = error_monitor.run_once(db_path=str(db_path), dry_run=True)
    assert metrics["fixes_attempted"] == 0
    assert fix_worker_called.call_count == 0


def test_dry_run_false_assist_dispatches_fix_worker(db_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Phase 2 ASSIST: dry_run=False with ASSIST classification → fix_worker invoked."""
    eid = _insert_error(db_path, "fp2", "services/foo.py")
    from core import error_monitor

    _patch_assist_env(monkeypatch)
    monkeypatch.setattr(error_monitor, "find_halt_reason", lambda: None)
    monkeypatch.setattr(
        "core.budget.enforce_budget",
        lambda **kw: SimpleNamespace(action="PROCEED", reason="ok", metric={}),
    )
    monkeypatch.setattr("core.remediation_kill_switch.check_all_layers", lambda **kw: None)

    fx_outcome = SimpleNamespace(
        success=True, error_id=eid, fingerprint="fp2",
        branch="auto-fix/err-1-fp2", worktree_path="/tmp/wt",
        diff="--- a\n+++ b\n+ok\n", diff_lines=2, diagnosis="d", fix_reasoning="r",
        error=None, duration_seconds=0.5, classification="ASSIST",
    )
    fx_run = MagicMock(return_value=fx_outcome)
    monkeypatch.setattr("core.fix_worker.run_fix", fx_run)

    rv = SimpleNamespace(
        success=True, verdict="REJECT", confidence=0.0,
        reject_reasons=["adversarial reject"],
    )
    monkeypatch.setattr("core.reviewer.review_fix", lambda *a, **kw: rv)

    mg = SimpleNamespace(
        success=False, error_id=eid, fingerprint="fp2", branch="x",
        staging_commit_sha=None, classification="ASSIST",
        gates_passed=[], gates_failed=[], blocking_failures=["reviewer_approved"],
        error="reviewer rejected",
    )
    monkeypatch.setattr("core.merger.merge_fix", lambda *a, **kw: mg)

    metrics = error_monitor.run_once(db_path=str(db_path), dry_run=False)
    assert metrics["fixes_attempted"] == 1
    assert fx_run.call_count == 1


def test_escalate_does_not_dispatch_fix_worker(db_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """ESCALATE classifications never trigger fix_worker."""
    # brokers/** is in the NEVER list (layer 1) → always ESCALATE
    _insert_error(db_path, "fp3", "brokers/live_executor.py", "broker timeout")
    from core import error_monitor

    monkeypatch.setattr(error_monitor, "find_halt_reason", lambda: None)
    fx_run = MagicMock()
    monkeypatch.setattr("core.fix_worker.run_fix", fx_run)

    metrics = error_monitor.run_once(db_path=str(db_path), dry_run=False)
    assert fx_run.call_count == 0
    assert metrics["fixes_attempted"] == 0


def test_ignore_does_not_dispatch_fix_worker(db_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """IGNORE classifications never trigger fix_worker."""
    # "Circuit breaker tripped" → IGNORE (ignore_patterns, checked even during RTH)
    _insert_error(db_path, "fp4", "x.py", "Circuit breaker tripped")
    from core import error_monitor

    monkeypatch.setattr(error_monitor, "find_halt_reason", lambda: None)
    fx_run = MagicMock()
    monkeypatch.setattr("core.fix_worker.run_fix", fx_run)

    metrics = error_monitor.run_once(db_path=str(db_path), dry_run=False)
    assert fx_run.call_count == 0
    assert metrics["fixes_attempted"] == 0


def test_budget_halt_skips_dispatch(db_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """If budget returns HALT, fix_worker is NOT invoked."""
    _insert_error(db_path, "fp5", "services/foo.py")
    from core import error_monitor

    _patch_assist_env(monkeypatch)
    monkeypatch.setattr(error_monitor, "find_halt_reason", lambda: None)
    monkeypatch.setattr(
        "core.budget.enforce_budget",
        lambda **kw: SimpleNamespace(action="HALT", reason="cap", metric={}),
    )
    monkeypatch.setattr("core.remediation_kill_switch.check_all_layers", lambda **kw: None)
    fx_run = MagicMock()
    monkeypatch.setattr("core.fix_worker.run_fix", fx_run)

    metrics = error_monitor.run_once(db_path=str(db_path), dry_run=False)
    assert metrics["fixes_skipped_budget"] == 1
    assert fx_run.call_count == 0


def test_kill_switch_mid_cycle_skips_dispatch(db_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Kill switch tripped after triage but before dispatch → skip."""
    _insert_error(db_path, "fp6", "services/foo.py")
    from core import error_monitor

    _patch_assist_env(monkeypatch)
    monkeypatch.setattr(error_monitor, "find_halt_reason", lambda: None)  # initial check passes
    monkeypatch.setattr(
        "core.budget.enforce_budget",
        lambda **kw: SimpleNamespace(action="PROCEED", reason="ok", metric={}),
    )
    # Kill-switch trips inside the loop
    block = SimpleNamespace(layer="L4", reason="dd", detail={})
    monkeypatch.setattr("core.remediation_kill_switch.check_all_layers", lambda **kw: block)
    fx_run = MagicMock()
    monkeypatch.setattr("core.fix_worker.run_fix", fx_run)

    metrics = error_monitor.run_once(db_path=str(db_path), dry_run=False)
    assert metrics["fixes_blocked_kill_switch"] == 1
    assert fx_run.call_count == 0


def test_fix_worker_failure_increments_failed_counter(db_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """fix_worker.success=False → fixes_failed incremented; audit row written."""
    eid = _insert_error(db_path, "fp7", "services/foo.py")
    from core import error_monitor

    _patch_assist_env(monkeypatch)
    monkeypatch.setattr(error_monitor, "find_halt_reason", lambda: None)
    monkeypatch.setattr(
        "core.budget.enforce_budget",
        lambda **kw: SimpleNamespace(action="PROCEED", reason="ok", metric={}),
    )
    monkeypatch.setattr("core.remediation_kill_switch.check_all_layers", lambda **kw: None)

    fx_outcome = SimpleNamespace(
        success=False, error_id=eid, fingerprint="fp7",
        branch=None, worktree_path=None, diff=None, diff_lines=0,
        diagnosis=None, error="pi exit 17", duration_seconds=0.1,
        classification="ASSIST",
    )
    monkeypatch.setattr("core.fix_worker.run_fix", lambda *a, **kw: fx_outcome)

    metrics = error_monitor.run_once(db_path=str(db_path), dry_run=False)
    assert metrics["fixes_failed"] == 1

    # Audit row should record the failure
    conn = sqlite3.connect(str(db_path))
    audit = conn.execute(
        "SELECT decision FROM fix_audit_log WHERE phase='fix'"
    ).fetchall()
    assert len(audit) >= 1
    assert audit[0][0] == "fix_worker_failed"
    conn.close()


def test_reviewer_reject_blocks_merge(db_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Reviewer REJECTs → merger.merge_fix returns success=False → fixes_failed++"""
    eid = _insert_error(db_path, "fp8", "services/foo.py")
    from core import error_monitor

    _patch_assist_env(monkeypatch)
    monkeypatch.setattr(error_monitor, "find_halt_reason", lambda: None)
    monkeypatch.setattr(
        "core.budget.enforce_budget",
        lambda **kw: SimpleNamespace(action="PROCEED", reason="ok", metric={}),
    )
    monkeypatch.setattr("core.remediation_kill_switch.check_all_layers", lambda **kw: None)

    fx_outcome = SimpleNamespace(
        success=True, error_id=eid, fingerprint="fp8",
        branch="auto-fix/err-1-fp8", worktree_path="/tmp/wt",
        diff="x", diff_lines=2, diagnosis="d", fix_reasoning="r",
        error=None, duration_seconds=0.1, classification="ASSIST",
    )
    monkeypatch.setattr("core.fix_worker.run_fix", lambda *a, **kw: fx_outcome)

    rv = SimpleNamespace(
        success=True, verdict="REJECT", confidence=0.3, reject_reasons=["bad"],
    )
    monkeypatch.setattr("core.reviewer.review_fix", lambda *a, **kw: rv)

    mg = SimpleNamespace(
        success=False, error_id=eid, fingerprint="fp8", branch="x",
        staging_commit_sha=None, classification="ASSIST",
        gates_passed=[], gates_failed=["reviewer_approved"],
        blocking_failures=["reviewer_approved"], error="rejected",
    )
    monkeypatch.setattr("core.merger.merge_fix", lambda *a, **kw: mg)

    metrics = error_monitor.run_once(db_path=str(db_path), dry_run=False)
    assert metrics["fixes_attempted"] == 1
    assert metrics["fixes_failed"] == 1
    assert metrics["fixes_succeeded"] == 0


def test_fix_succeeds_increments_succeeded(db_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Full happy path: fix_worker succeeds → reviewer approves → merger merges → fixes_succeeded."""
    eid = _insert_error(db_path, "fp9", "services/foo.py")
    from core import error_monitor

    _patch_assist_env(monkeypatch)
    monkeypatch.setattr(error_monitor, "find_halt_reason", lambda: None)
    monkeypatch.setattr(
        "core.budget.enforce_budget",
        lambda **kw: SimpleNamespace(action="PROCEED", reason="ok", metric={}),
    )
    monkeypatch.setattr("core.remediation_kill_switch.check_all_layers", lambda **kw: None)

    fx = SimpleNamespace(
        success=True, error_id=eid, fingerprint="fp9",
        branch="auto-fix/err-1-fp9", worktree_path="/tmp/wt",
        diff="x", diff_lines=2, diagnosis="d", fix_reasoning="r",
        error=None, duration_seconds=0.1, classification="ASSIST",
    )
    monkeypatch.setattr("core.fix_worker.run_fix", lambda *a, **kw: fx)

    rv = SimpleNamespace(
        success=True, verdict="APPROVE", confidence=0.85, reject_reasons=[],
    )
    monkeypatch.setattr("core.reviewer.review_fix", lambda *a, **kw: rv)

    mg = SimpleNamespace(
        success=True, error_id=eid, fingerprint="fp9", branch="x",
        staging_commit_sha="abc123", classification="ASSIST",
        gates_passed=["all"], gates_failed=[], blocking_failures=[], error=None,
    )
    monkeypatch.setattr("core.merger.merge_fix", lambda *a, **kw: mg)

    metrics = error_monitor.run_once(db_path=str(db_path), dry_run=False)
    assert metrics["fixes_succeeded"] == 1
    assert metrics["fixes_failed"] == 0


def test_auto_fix_classification_downgrades_to_assist_in_phase_2(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Phase 3 disabled — AUTO_FIX downgrade logic exists in source."""
    src = Path(PROJECT_ROOT / "core" / "error_monitor.py").read_text()
    assert "Phase 3 disabled" in src or "phase_3_enabled" in src.lower()
    assert "AUTO_FIX" in src


def test_phase_2_dispatch_does_not_run_during_halt(
    db_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Hard halt at run_once start → no triage, no dispatch."""
    _insert_error(db_path, "fp10", "services/foo.py")
    from core import error_monitor

    monkeypatch.setattr(error_monitor, "find_halt_reason", lambda: "test halt")
    fx_run = MagicMock()
    monkeypatch.setattr("core.fix_worker.run_fix", fx_run)

    metrics = error_monitor.run_once(db_path=str(db_path), dry_run=False)
    assert metrics["halted"] is True
    assert metrics["processed"] == 0
    assert fx_run.call_count == 0


def test_config_phase_current_is_2() -> None:
    """auto_remediation.yaml phase.current must be 2 after this commit."""
    cfg = yaml.safe_load(
        (PROJECT_ROOT / "config" / "auto_remediation.yaml").read_text()
    )
    assert cfg["phase"]["current"] == 2


def test_config_phase_3_enabled_remains_false() -> None:
    """Phase 3 still gated until 14d Phase 2 data accrues."""
    cfg = yaml.safe_load(
        (PROJECT_ROOT / "config" / "auto_remediation.yaml").read_text()
    )
    assert cfg["phase"]["phase_3_enabled"] is False


def test_config_dry_run_remains_true_default() -> None:
    """Operator must explicitly flip dry_run to false after cron install.

    Phase 2 dispatch is written but the default remains dry_run=True so the
    operator can review the cron setup before enabling live dispatch. To
    activate Phase 2 live dispatch, set monitor.dry_run: false in
    config/auto_remediation.yaml (or pass --no-dry-run on the CLI).
    """
    cfg = yaml.safe_load(
        (PROJECT_ROOT / "config" / "auto_remediation.yaml").read_text()
    )
    assert cfg["monitor"]["dry_run"] is True


def test_metrics_includes_phase_2_counters(db_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Metrics dict shape includes the Phase 2 fields even when dry_run=True."""
    from core import error_monitor

    monkeypatch.setattr(error_monitor, "find_halt_reason", lambda: None)
    metrics = error_monitor.run_once(db_path=str(db_path), dry_run=True)
    for key in ("fixes_attempted", "fixes_succeeded", "fixes_failed"):
        assert key in metrics, f"Missing key: {key}"


# ---------------------------------------------------------------------------
# 5+ additional edge-case tests (beyond the 15 spec tests)
# ---------------------------------------------------------------------------

def test_multi_error_batch_only_assist_dispatched(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Batch with ASSIST + ESCALATE + IGNORE → fix_worker called exactly once (ASSIST only)."""
    _insert_error(db_path, "batch_assist",   "services/dashboard.py")  # → ASSIST
    _insert_error(db_path, "batch_escalate", "brokers/alpaca/broker.py")  # → ESCALATE (NEVER list)
    _insert_error(db_path, "batch_ignore",   "util.py", "Circuit breaker tripped")  # → IGNORE

    from core import error_monitor

    _patch_assist_env(monkeypatch)
    monkeypatch.setattr(error_monitor, "find_halt_reason", lambda: None)
    monkeypatch.setattr(
        "core.budget.enforce_budget",
        lambda **kw: SimpleNamespace(action="PROCEED", reason="ok", metric={}),
    )
    monkeypatch.setattr("core.remediation_kill_switch.check_all_layers", lambda **kw: None)

    fx = SimpleNamespace(
        success=True, error_id=1, fingerprint="batch_assist",
        branch="auto-fix/b", worktree_path="/tmp/wt",
        diff="+x", diff_lines=1, diagnosis="d", fix_reasoning="r",
        error=None, duration_seconds=0.1, classification="ASSIST",
    )
    fx_run = MagicMock(return_value=fx)
    monkeypatch.setattr("core.fix_worker.run_fix", fx_run)

    rv = SimpleNamespace(success=True, verdict="APPROVE", confidence=0.9, reject_reasons=[])
    monkeypatch.setattr("core.reviewer.review_fix", lambda *a, **kw: rv)

    mg = SimpleNamespace(
        success=True, error_id=1, fingerprint="batch_assist", branch="b",
        staging_commit_sha="sha1", classification="ASSIST",
        gates_passed=["all"], gates_failed=[], blocking_failures=[], error=None,
    )
    monkeypatch.setattr("core.merger.merge_fix", lambda *a, **kw: mg)

    metrics = error_monitor.run_once(db_path=str(db_path), dry_run=False)
    # 3 errors processed, only 1 dispatched
    assert metrics["processed"] == 3
    assert metrics["fixes_attempted"] == 1
    assert metrics["fixes_succeeded"] == 1
    assert fx_run.call_count == 1
    # Classification breakdown
    assert metrics["by_class"].get("ASSIST", 0) >= 1
    assert metrics["by_class"].get("ESCALATE", 0) >= 1


def test_auto_fix_dispatched_as_assist_when_phase3_disabled(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Even if classify() returns AUTO_FIX, run_once treats it as ASSIST in Phase 2."""
    eid = _insert_error(db_path, "fp_autofix", "services/foo.py")
    from core import error_monitor

    # Inject a fake triage module that returns AUTO_FIX
    fake_result = SimpleNamespace(
        classification="AUTO_FIX",
        reason="whitelisted",
        rule_id="auto_fix.whitelist:test_import_error",
        tier=2,
    )
    fake_classifier = MagicMock()
    fake_classifier.classify.return_value = fake_result
    fake_triage_mod = types.ModuleType("core.triage")
    fake_triage_mod.TriageClassifier = MagicMock(return_value=fake_classifier)

    monkeypatch.setitem(sys.modules, "core.triage", fake_triage_mod)
    monkeypatch.setattr(error_monitor, "find_halt_reason", lambda: None)
    monkeypatch.setattr(
        "core.budget.enforce_budget",
        lambda **kw: SimpleNamespace(action="PROCEED", reason="ok", metric={}),
    )
    monkeypatch.setattr("core.remediation_kill_switch.check_all_layers", lambda **kw: None)

    captured_classification = {}

    def fake_run_fix(error, *, classification="ASSIST", **kw):
        captured_classification["val"] = classification
        return SimpleNamespace(
            success=False, error_id=eid, fingerprint="fp_autofix",
            branch=None, worktree_path=None, diff=None, diff_lines=0,
            diagnosis=None, error="test stop", duration_seconds=0.1,
            classification=classification,
        )

    monkeypatch.setattr("core.fix_worker.run_fix", fake_run_fix)

    metrics = error_monitor.run_once(db_path=str(db_path), dry_run=False)
    # fix_worker should have been called with classification="ASSIST" not "AUTO_FIX"
    assert captured_classification.get("val") == "ASSIST"
    assert metrics["fixes_attempted"] == 1


def test_budget_alert_does_not_skip_dispatch(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Budget ALERT (not HALT) still allows dispatch."""
    eid = _insert_error(db_path, "fp_alert", "services/foo.py")
    from core import error_monitor

    _patch_assist_env(monkeypatch)
    monkeypatch.setattr(error_monitor, "find_halt_reason", lambda: None)
    monkeypatch.setattr(
        "core.budget.enforce_budget",
        lambda **kw: SimpleNamespace(action="ALERT", reason="rate warning", metric={}),
    )
    monkeypatch.setattr("core.remediation_kill_switch.check_all_layers", lambda **kw: None)

    fx = SimpleNamespace(
        success=True, error_id=eid, fingerprint="fp_alert",
        branch="auto-fix/b", worktree_path="/tmp/wt",
        diff="+x", diff_lines=1, diagnosis="d", fix_reasoning="r",
        error=None, duration_seconds=0.1, classification="ASSIST",
    )
    fx_run = MagicMock(return_value=fx)
    monkeypatch.setattr("core.fix_worker.run_fix", fx_run)

    rv = SimpleNamespace(success=True, verdict="REJECT", confidence=0.2, reject_reasons=["r"])
    monkeypatch.setattr("core.reviewer.review_fix", lambda *a, **kw: rv)

    mg = SimpleNamespace(
        success=False, error_id=eid, fingerprint="fp_alert", branch="b",
        staging_commit_sha=None, classification="ASSIST",
        gates_passed=[], gates_failed=["reviewer_approved"],
        blocking_failures=["reviewer_approved"], error="rejected",
    )
    monkeypatch.setattr("core.merger.merge_fix", lambda *a, **kw: mg)

    metrics = error_monitor.run_once(db_path=str(db_path), dry_run=False)
    # ALERT → dispatch proceeds
    assert metrics["fixes_attempted"] == 1
    assert metrics["fixes_skipped_budget"] == 0
    assert fx_run.call_count == 1


def test_merger_receives_correct_fix_outcome_attrs(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """merger.merge_fix receives fix_outcome_for_merger with all required attributes."""
    eid = _insert_error(db_path, "fp_attrs", "services/auth.py")
    from core import error_monitor

    _patch_assist_env(monkeypatch)
    monkeypatch.setattr(error_monitor, "find_halt_reason", lambda: None)
    monkeypatch.setattr(
        "core.budget.enforce_budget",
        lambda **kw: SimpleNamespace(action="PROCEED", reason="ok", metric={}),
    )
    monkeypatch.setattr("core.remediation_kill_switch.check_all_layers", lambda **kw: None)

    fx = SimpleNamespace(
        success=True, error_id=eid, fingerprint="fp_attrs",
        branch="auto-fix/err-X-fp_attrs", worktree_path="/tmp/wt2",
        diff="--- a\n+++ b\n+fix\n", diff_lines=3,
        diagnosis="root cause: null check", fix_reasoning="added guard",
        error=None, duration_seconds=0.2, classification="ASSIST",
    )
    monkeypatch.setattr("core.fix_worker.run_fix", lambda *a, **kw: fx)

    rv = SimpleNamespace(success=True, verdict="APPROVE", confidence=0.9, reject_reasons=[])
    monkeypatch.setattr("core.reviewer.review_fix", lambda *a, **kw: rv)

    captured_merge_args: dict = {}

    def fake_merge_fix(fix_outcome, reviewer_outcome, **kw):
        captured_merge_args["fix_outcome"] = fix_outcome
        captured_merge_args["reviewer_outcome"] = reviewer_outcome
        captured_merge_args["kw"] = kw
        return SimpleNamespace(
            success=True, error_id=eid, fingerprint="fp_attrs", branch="b",
            staging_commit_sha="sha_ok", classification="ASSIST",
            gates_passed=["all"], gates_failed=[], blocking_failures=[], error=None,
        )

    monkeypatch.setattr("core.merger.merge_fix", fake_merge_fix)

    metrics = error_monitor.run_once(db_path=str(db_path), dry_run=False)
    assert metrics["fixes_succeeded"] == 1

    fo = captured_merge_args["fix_outcome"]
    # Verify all required attributes on the duck-typed FXO object
    assert fo.attempt_id == -1
    assert fo.error_id == eid
    assert fo.fingerprint == "fp_attrs"
    assert fo.branch == "auto-fix/err-X-fp_attrs"
    assert fo.worktree == "/tmp/wt2"
    assert fo.success is True
    assert fo.diff_lines == 3
    assert fo.classification == "ASSIST"

    # reviewer_outcome is the rv SimpleNamespace
    assert captured_merge_args["reviewer_outcome"] is rv

    # db_path kwarg is passed through
    assert "db_path" in captured_merge_args["kw"]


def test_dispatch_exception_increments_fixes_failed(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If fix_worker raises an unexpected exception, fixes_failed is incremented."""
    _insert_error(db_path, "fp_exc", "services/foo.py")
    from core import error_monitor

    _patch_assist_env(monkeypatch)
    monkeypatch.setattr(error_monitor, "find_halt_reason", lambda: None)
    monkeypatch.setattr(
        "core.budget.enforce_budget",
        lambda **kw: SimpleNamespace(action="PROCEED", reason="ok", metric={}),
    )
    monkeypatch.setattr("core.remediation_kill_switch.check_all_layers", lambda **kw: None)

    def exploding_run_fix(*a, **kw):
        raise RuntimeError("unexpected pi crash")

    monkeypatch.setattr("core.fix_worker.run_fix", exploding_run_fix)

    metrics = error_monitor.run_once(db_path=str(db_path), dry_run=False)
    assert metrics["fixes_attempted"] == 1
    assert metrics["fixes_failed"] == 1
    assert metrics["fixes_succeeded"] == 0


def test_halt_return_dict_has_phase2_keys(monkeypatch: pytest.MonkeyPatch, db_path: Path) -> None:
    """When halted early, return dict must still carry Phase 2 counter keys."""
    from core import error_monitor

    monkeypatch.setattr(error_monitor, "find_halt_reason", lambda: "test_halt_file")
    metrics = error_monitor.run_once(db_path=str(db_path), dry_run=False)
    assert metrics["halted"] is True
    for key in ("fixes_attempted", "fixes_succeeded", "fixes_failed"):
        assert key in metrics, f"Missing key {key!r} in halted metrics"


def test_budget_crash_treated_as_halt(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If enforce_budget raises, dispatch is skipped (fail-closed)."""
    _insert_error(db_path, "fp_budgetcrash", "services/foo.py")
    from core import error_monitor

    _patch_assist_env(monkeypatch)
    monkeypatch.setattr(error_monitor, "find_halt_reason", lambda: None)
    monkeypatch.setattr(
        "core.budget.enforce_budget",
        MagicMock(side_effect=RuntimeError("DB locked")),
    )
    fx_run = MagicMock()
    monkeypatch.setattr("core.fix_worker.run_fix", fx_run)

    metrics = error_monitor.run_once(db_path=str(db_path), dry_run=False)
    # Budget crash → fail-closed → no dispatch
    assert fx_run.call_count == 0
    # fixes_attempted stays 0 (skipped before increment)
    assert metrics["fixes_attempted"] == 0
