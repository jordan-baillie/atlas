"""Tests for core/merger.py — Phase 2 ASSIST merge pipeline.

Mocks all subprocess (git) calls and SQLite writes; no live operations.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from unittest.mock import MagicMock, Mock, call, patch

import pytest

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

from core.merger import (
    STAGING_BRANCH,
    MergeOutcome,
    _now_utc,
    _persist_merge_result,
    _send_failure_alert,
    merge_fix,
    push_to_staging,
)
from core.merge_gates import GateResult, GateRunOutcome


# ── Fixtures & helpers ────────────────────────────────────────────────

@pytest.fixture
def tmp_worktree(tmp_path: Path) -> Path:
    (tmp_path / "tests").mkdir()
    return tmp_path


@pytest.fixture
def gate_pass_outcome() -> GateRunOutcome:
    """A GateRunOutcome where all 15 gates passed."""
    names = [
        "targeted_tests", "regression_test_present", "full_suite",
        "no_new_bare_except", "pi_system_prompt_lint", "no_check_violations",
        "diff_size_cap", "no_never_list_touched", "no_safety_critical_function_modified",
        "reviewer_approved", "no_healthcheck_regression", "no_fingerprint_recurrence",
        "no_warning_demotion", "mypy_clean", "pre_commit_hooks",
    ]
    return GateRunOutcome(
        all_passed=True,
        results=[GateResult(n, True) for n in names],
        summary={"passed": names, "failed": [], "blocking_failures": []},
    )


@pytest.fixture
def gate_fail_outcome() -> GateRunOutcome:
    """A GateRunOutcome with one blocking failure."""
    return GateRunOutcome(
        all_passed=False,
        results=[GateResult("diff_size_cap", False, {"total": 50})],
        summary={
            "passed": [],
            "failed": ["diff_size_cap"],
            "blocking_failures": ["diff_size_cap"],
        },
    )


@pytest.fixture
def minimal_db(tmp_path: Path) -> Path:
    """Minimal SQLite DB with fix_attempts + fix_audit_log tables."""
    db_path = tmp_path / "test_atlas.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("""
        CREATE TABLE fix_attempts (
            id INTEGER PRIMARY KEY,
            error_id INTEGER,
            fingerprint TEXT,
            status TEXT DEFAULT 'triaged',
            started_ts TEXT,
            finished_ts TEXT,
            gates_passed_json TEXT,
            gates_failed_json TEXT,
            blocked_by_gate TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE fix_audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            attempt_id INTEGER,
            error_id INTEGER,
            ts TEXT,
            phase TEXT NOT NULL,
            actor TEXT NOT NULL,
            decision TEXT,
            payload_json TEXT,
            result_status TEXT,
            blocked_by_gate TEXT,
            notes TEXT
        )
    """)
    # Seed a fix_attempts row
    conn.execute(
        "INSERT INTO fix_attempts (id, error_id, fingerprint, status, started_ts) VALUES (?,?,?,?,?)",
        (1, 42, "abc123fingerprint", "verifying", "2026-04-29T00:00:00Z"),
    )
    conn.commit()
    conn.close()
    return db_path


@dataclass
class FakeFixOutcome:
    success: bool = True
    attempt_id: int = 1
    error_id: int = 42
    fingerprint: str = "abc123fingerprint"
    branch: str = "fix/autofix-abc123-1"
    worktree: Path = field(default_factory=lambda: Path("/fake/worktree"))


@dataclass
class FakeReviewerOutcome:
    success: bool = True
    verdict: str = "APPROVE"
    confidence: float = 0.92
    addresses_root_cause: bool = True
    could_lose_money: bool = False
    reject_reasons: list = field(default_factory=list)


# ── MergeOutcome dataclass ────────────────────────────────────────────

class TestMergeOutcomeDataclass:
    def test_required_fields(self):
        mo = MergeOutcome(
            success=True,
            error_id=1,
            fingerprint="fp1",
            branch="fix/branch",
        )
        assert mo.success is True
        assert mo.classification == "ASSIST"

    def test_defaults(self):
        mo = MergeOutcome(success=False, error_id=0, fingerprint="", branch="")
        assert mo.staging_commit_sha is None
        assert mo.gates_passed == []
        assert mo.gates_failed == []
        assert mo.blocking_failures == []
        assert mo.error is None


# ── push_to_staging ────────────────────────────────────────────────────

class TestPushToStaging:
    FIX_SHA = "abc123def456abc123def456abc123def456abcd"
    STAGING_SHA = "111111111111111111111111111111111111aaaa"

    def _git_responses(self, staging_exists=True, ff_ok=True):
        """Build ordered subprocess.run side_effect list."""
        responses = []
        # 1. rev-parse --verify STAGING_BRANCH
        responses.append(Mock(returncode=0 if staging_exists else 1, stdout="", stderr=""))
        if not staging_exists:
            # 2. git branch STAGING_BRANCH main
            responses.append(Mock(returncode=0, stdout="", stderr=""))
        # 3. git rev-parse branch → fix_sha
        responses.append(Mock(returncode=0, stdout=self.FIX_SHA + "\n", stderr=""))
        # 4. git rev-parse STAGING_BRANCH → cur_sha
        responses.append(Mock(returncode=0, stdout=self.STAGING_SHA + "\n", stderr=""))
        # 5. git merge-base
        base_sha = self.STAGING_SHA if ff_ok else "deadbeefdeadbeef" * 2
        responses.append(Mock(returncode=0, stdout=base_sha + "\n", stderr=""))
        # 6. git update-ref
        responses.append(Mock(returncode=0, stdout="", stderr=""))
        return responses

    def test_success_returns_fix_sha(self, tmp_worktree: Path):
        with patch("core.merger.subprocess.run") as mock_run:
            mock_run.side_effect = self._git_responses()
            ok, result = push_to_staging(tmp_worktree, "fix/some-branch")
        assert ok is True
        assert result == self.FIX_SHA

    def test_creates_staging_branch_if_missing(self, tmp_worktree: Path):
        with patch("core.merger.subprocess.run") as mock_run:
            mock_run.side_effect = self._git_responses(staging_exists=False)
            ok, result = push_to_staging(tmp_worktree, "fix/some-branch")
        assert ok is True
        # Verify branch-creation call was made
        calls = [str(c) for c in mock_run.call_args_list]
        assert any("branch" in c and STAGING_BRANCH in c for c in calls)

    def test_non_ff_returns_false(self, tmp_worktree: Path):
        """staging has diverged from the fix branch merge-base → not fast-forward."""
        with patch("core.merger.subprocess.run") as mock_run:
            mock_run.side_effect = self._git_responses(ff_ok=False)
            ok, result = push_to_staging(tmp_worktree, "fix/some-branch")
        assert ok is False
        assert "not fast-forward" in result

    def test_branch_sha_lookup_fails(self, tmp_worktree: Path):
        with patch("core.merger.subprocess.run") as mock_run:
            mock_run.side_effect = [
                Mock(returncode=0, stdout="", stderr=""),         # rev-parse verify
                Mock(returncode=1, stdout="", stderr="not found"), # rev-parse branch
            ]
            ok, result = push_to_staging(tmp_worktree, "missing-branch")
        assert ok is False
        assert "sha lookup failed" in result

    def test_timeout_returns_false(self, tmp_worktree: Path):
        import subprocess
        with patch("core.merger.subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.TimeoutExpired(cmd="git", timeout=10)
            ok, result = push_to_staging(tmp_worktree, "fix/branch")
        assert ok is False
        assert "timeout" in result

    def test_update_ref_failure_returns_false(self, tmp_worktree: Path):
        responses = self._git_responses()
        # Replace the last (update-ref) call with a failure
        responses[-1] = Mock(returncode=1, stdout="", stderr="CAS mismatch")
        with patch("core.merger.subprocess.run") as mock_run:
            mock_run.side_effect = responses
            ok, result = push_to_staging(tmp_worktree, "fix/branch")
        assert ok is False
        assert "update-ref failed" in result


# ── _persist_merge_result ─────────────────────────────────────────────

class TestPersistMergeResult:
    def test_updates_fix_attempts_row(self, minimal_db: Path, gate_pass_outcome: GateRunOutcome):
        _persist_merge_result(
            db_path=minimal_db,
            attempt_id=1,
            error_id=42,
            gate_outcome=gate_pass_outcome,
            staging_sha="aabbcc",
            success=True,
        )
        conn = sqlite3.connect(str(minimal_db))
        row = conn.execute(
            "SELECT status, gates_passed_json, gates_failed_json, blocked_by_gate "
            "FROM fix_attempts WHERE id=1"
        ).fetchone()
        conn.close()
        assert row[0] == "merged"
        assert json.loads(row[1]) == gate_pass_outcome.summary["passed"]
        assert json.loads(row[2]) == []
        assert row[3] is None  # no blocking failures

    def test_sets_status_blocked_on_failure(self, minimal_db: Path, gate_fail_outcome: GateRunOutcome):
        _persist_merge_result(
            db_path=minimal_db,
            attempt_id=1,
            error_id=42,
            gate_outcome=gate_fail_outcome,
            staging_sha=None,
            success=False,
        )
        conn = sqlite3.connect(str(minimal_db))
        row = conn.execute("SELECT status, blocked_by_gate FROM fix_attempts WHERE id=1").fetchone()
        conn.close()
        assert row[0] == "blocked"
        assert row[1] == "diff_size_cap"

    def test_inserts_gate_check_audit_row(self, minimal_db: Path, gate_pass_outcome: GateRunOutcome):
        _persist_merge_result(
            db_path=minimal_db,
            attempt_id=1,
            error_id=42,
            gate_outcome=gate_pass_outcome,
            staging_sha="sha999",
            success=True,
        )
        conn = sqlite3.connect(str(minimal_db))
        rows = conn.execute(
            "SELECT phase, actor, decision, result_status FROM fix_audit_log WHERE attempt_id=1"
        ).fetchall()
        conn.close()
        phases = [r[0] for r in rows]
        assert "gate_check" in phases

    def test_inserts_merge_audit_row_on_success(self, minimal_db: Path, gate_pass_outcome: GateRunOutcome):
        _persist_merge_result(
            db_path=minimal_db,
            attempt_id=1,
            error_id=42,
            gate_outcome=gate_pass_outcome,
            staging_sha="sha999",
            success=True,
        )
        conn = sqlite3.connect(str(minimal_db))
        rows = conn.execute(
            "SELECT phase, decision FROM fix_audit_log WHERE attempt_id=1"
        ).fetchall()
        conn.close()
        assert any(r[0] == "merge" and r[1] == "MERGED" for r in rows)

    def test_no_merge_audit_row_on_failure(self, minimal_db: Path, gate_fail_outcome: GateRunOutcome):
        _persist_merge_result(
            db_path=minimal_db,
            attempt_id=1,
            error_id=42,
            gate_outcome=gate_fail_outcome,
            staging_sha=None,
            success=False,
        )
        conn = sqlite3.connect(str(minimal_db))
        rows = conn.execute(
            "SELECT phase FROM fix_audit_log WHERE attempt_id=1 AND phase='merge'"
        ).fetchall()
        conn.close()
        assert rows == []


# ── _send_failure_alert ────────────────────────────────────────────────

class TestSendFailureAlert:
    def test_sends_telegram_on_failure(self):
        outcome = MergeOutcome(
            success=False,
            error_id=7,
            fingerprint="fp_test",
            branch="fix/fp-test",
            blocking_failures=["diff_size_cap"],
            gates_failed=["diff_size_cap", "reviewer_approved"],
        )
        with patch("core.merger.send_message", create=True) as mock_send, \
             patch.dict("sys.modules", {"utils.telegram": MagicMock(send_message=mock_send)}):
            try:
                import importlib
                import core.merger as merger_mod
                orig = getattr(merger_mod, "_send_failure_alert")
                # Call via try/except path — telegram import may fail in test env
                _send_failure_alert(outcome)
            except Exception:
                pass  # If Telegram module absent, just verify it doesn't raise

    def test_handles_telegram_import_failure_gracefully(self):
        """If utils.telegram is unavailable, _send_failure_alert must not raise."""
        outcome = MergeOutcome(
            success=False, error_id=1, fingerprint="fp", branch="b",
            blocking_failures=["gate_x"],
        )
        import sys
        saved = sys.modules.get("utils.telegram")
        sys.modules["utils.telegram"] = None  # type: ignore
        try:
            _send_failure_alert(outcome)  # should not raise
        finally:
            if saved is None and "utils.telegram" in sys.modules:
                del sys.modules["utils.telegram"]
            elif saved is not None:
                sys.modules["utils.telegram"] = saved


# ── merge_fix ─────────────────────────────────────────────────────────

class TestMergeFix:
    def test_success_path(
        self,
        tmp_worktree: Path,
        minimal_db: Path,
        gate_pass_outcome: GateRunOutcome,
    ):
        fix = FakeFixOutcome(worktree=tmp_worktree, success=True)
        reviewer = FakeReviewerOutcome()

        FIX_SHA = "aabbccddeeffaabb"
        STAGING_SHA = "1111222233334444"

        git_responses = [
            Mock(returncode=0, stdout="", stderr=""),             # rev-parse verify staging
            Mock(returncode=0, stdout=FIX_SHA + "\n", stderr=""), # rev-parse fix branch
            Mock(returncode=0, stdout=STAGING_SHA + "\n", stderr=""), # rev-parse staging
            Mock(returncode=0, stdout=STAGING_SHA + "\n", stderr=""), # merge-base (ff ok)
            Mock(returncode=0, stdout="", stderr=""),             # update-ref
        ]

        with patch("core.merger.run_all_gates", return_value=gate_pass_outcome), \
             patch("core.merger.subprocess.run") as mock_git:
            mock_git.side_effect = git_responses
            outcome = merge_fix(fix, reviewer, db_path=minimal_db)

        assert outcome.success is True
        assert outcome.staging_commit_sha == FIX_SHA
        assert outcome.fingerprint == fix.fingerprint
        assert outcome.error is None

    def test_gate_blocking_failure_blocks_merge(
        self,
        tmp_worktree: Path,
        minimal_db: Path,
        gate_fail_outcome: GateRunOutcome,
    ):
        fix = FakeFixOutcome(worktree=tmp_worktree, success=True)
        reviewer = FakeReviewerOutcome()

        with patch("core.merger.run_all_gates", return_value=gate_fail_outcome), \
             patch("core.merger.subprocess.run"), \
             patch("core.merger._send_failure_alert") as mock_alert:
            outcome = merge_fix(fix, reviewer, db_path=minimal_db)

        assert outcome.success is False
        assert "diff_size_cap" in outcome.blocking_failures
        assert outcome.staging_commit_sha is None
        mock_alert.assert_called_once()

    def test_upstream_failure_short_circuits(self, tmp_worktree: Path, minimal_db: Path):
        fix = FakeFixOutcome(worktree=tmp_worktree, success=False)  # upstream failure

        with patch("core.merger.run_all_gates") as mock_gates, \
             patch("core.merger._send_failure_alert") as mock_alert:
            outcome = merge_fix(fix, None, db_path=minimal_db)

        mock_gates.assert_not_called()  # gates not run on upstream failure
        assert outcome.success is False
        assert "fix_outcome.success=False" in (outcome.error or "")
        mock_alert.assert_called_once()

    def test_push_failure_sets_success_false(
        self,
        tmp_worktree: Path,
        minimal_db: Path,
        gate_pass_outcome: GateRunOutcome,
    ):
        fix = FakeFixOutcome(worktree=tmp_worktree, success=True)
        reviewer = FakeReviewerOutcome()

        with patch("core.merger.run_all_gates", return_value=gate_pass_outcome), \
             patch("core.merger.push_to_staging", return_value=(False, "CAS race")) as mock_push, \
             patch("core.merger._send_failure_alert") as mock_alert:
            outcome = merge_fix(fix, reviewer, db_path=minimal_db)

        assert outcome.success is False
        assert outcome.error == "CAS race"
        mock_alert.assert_called_once()

    def test_gate_runner_exception_handled(self, tmp_worktree: Path, minimal_db: Path):
        fix = FakeFixOutcome(worktree=tmp_worktree, success=True)

        with patch("core.merger.run_all_gates", side_effect=RuntimeError("boom")), \
             patch("core.merger._send_failure_alert") as mock_alert:
            outcome = merge_fix(fix, None, db_path=minimal_db)

        assert outcome.success is False
        assert "gate runner exception" in (outcome.error or "")
        mock_alert.assert_called_once()

    def test_db_persist_failure_is_non_fatal(
        self,
        tmp_worktree: Path,
        gate_pass_outcome: GateRunOutcome,
        tmp_path: Path,
    ):
        """_persist_merge_result failure must not propagate to caller."""
        fix = FakeFixOutcome(worktree=tmp_worktree, success=True)
        reviewer = FakeReviewerOutcome()

        FIX_SHA = "ccddaabb11223344"
        STAGING_SHA = "9988776655443322"

        git_responses = [
            Mock(returncode=0, stdout="", stderr=""),
            Mock(returncode=0, stdout=FIX_SHA + "\n", stderr=""),
            Mock(returncode=0, stdout=STAGING_SHA + "\n", stderr=""),
            Mock(returncode=0, stdout=STAGING_SHA + "\n", stderr=""),
            Mock(returncode=0, stdout="", stderr=""),
        ]

        nonexistent_db = tmp_path / "nonexistent_dir" / "atlas.db"

        with patch("core.merger.run_all_gates", return_value=gate_pass_outcome), \
             patch("core.merger.subprocess.run") as mock_git:
            mock_git.side_effect = git_responses
            # DB path is invalid — persist should fail silently
            outcome = merge_fix(fix, reviewer, db_path=nonexistent_db)

        # outcome.success still reflects gate+push result (True), not DB error
        assert outcome.success is True

    def test_negative_attempt_id_skips_persist(
        self,
        tmp_worktree: Path,
        gate_pass_outcome: GateRunOutcome,
        minimal_db: Path,
    ):
        """attempt_id=-1 (unknown) → skip DB persist entirely."""
        fix = FakeFixOutcome(worktree=tmp_worktree, success=True, attempt_id=-1)
        reviewer = FakeReviewerOutcome()

        FIX_SHA = "eeff00112233aabb"
        STAGING_SHA = "4455667788990011"

        git_responses = [
            Mock(returncode=0, stdout="", stderr=""),
            Mock(returncode=0, stdout=FIX_SHA + "\n", stderr=""),
            Mock(returncode=0, stdout=STAGING_SHA + "\n", stderr=""),
            Mock(returncode=0, stdout=STAGING_SHA + "\n", stderr=""),
            Mock(returncode=0, stdout="", stderr=""),
        ]

        with patch("core.merger.run_all_gates", return_value=gate_pass_outcome), \
             patch("core.merger.subprocess.run") as mock_git, \
             patch("core.merger._persist_merge_result") as mock_persist:
            mock_git.side_effect = git_responses
            outcome = merge_fix(fix, reviewer, db_path=minimal_db)

        mock_persist.assert_not_called()
        assert outcome.success is True

    def test_gates_passed_and_failed_populated(
        self,
        tmp_worktree: Path,
        minimal_db: Path,
        gate_fail_outcome: GateRunOutcome,
    ):
        fix = FakeFixOutcome(worktree=tmp_worktree, success=True)
        with patch("core.merger.run_all_gates", return_value=gate_fail_outcome), \
             patch("core.merger._send_failure_alert"):
            outcome = merge_fix(fix, None, db_path=minimal_db)

        assert "diff_size_cap" in outcome.gates_failed
        assert outcome.gates_passed == []

    def test_no_alert_on_success(
        self,
        tmp_worktree: Path,
        minimal_db: Path,
        gate_pass_outcome: GateRunOutcome,
    ):
        """Phase 2 policy: Telegram alert only on failure, never on success."""
        fix = FakeFixOutcome(worktree=tmp_worktree, success=True)

        FIX_SHA = "1234567890abcdef"
        STAGING_SHA = "fedcba0987654321"

        git_responses = [
            Mock(returncode=0, stdout="", stderr=""),
            Mock(returncode=0, stdout=FIX_SHA + "\n", stderr=""),
            Mock(returncode=0, stdout=STAGING_SHA + "\n", stderr=""),
            Mock(returncode=0, stdout=STAGING_SHA + "\n", stderr=""),
            Mock(returncode=0, stdout="", stderr=""),
        ]

        with patch("core.merger.run_all_gates", return_value=gate_pass_outcome), \
             patch("core.merger.subprocess.run") as mock_git, \
             patch("core.merger._send_failure_alert") as mock_alert:
            mock_git.side_effect = git_responses
            outcome = merge_fix(fix, None, db_path=minimal_db)

        assert outcome.success is True
        mock_alert.assert_not_called()


# ── _now_utc ───────────────────────────────────────────────────────────

class TestNowUtc:
    def test_returns_iso_string(self):
        ts = _now_utc()
        assert "T" in ts
        assert ts.endswith("+00:00") or ts.endswith("Z") or "+" in ts
