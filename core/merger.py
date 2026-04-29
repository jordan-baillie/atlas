"""Atlas auto-remediation Merger — Phase 2 ASSIST mode.

Phase 2 workflow per fix:
  1. Receive (FixOutcome, ReviewOutcome) from upstream
  2. Run all 15 merge gates
  3. Push branch to origin/auto-fix-staging (a SHARED staging branch — fixes are
     fast-forward merged onto it). Never push to main directly.
  4. Telegram alert on FAILURE only (user config telegram.on_success=NEVER)
  5. Persist fix_attempts row + fix_audit_log entries

Phase 3 will add auto_merger.py that promotes auto-fix-staging → main after
30-min healthcheck + fingerprint-clean window.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from core.merge_gates import GateRunOutcome, run_all_gates

PROJECT_ROOT = Path(__file__).resolve().parent.parent
STAGING_BRANCH = "auto-fix-staging"
DB_PATH_DEFAULT = PROJECT_ROOT / "data" / "atlas.db"

logger = logging.getLogger(__name__)


@dataclass
class MergeOutcome:
    success: bool
    error_id: int
    fingerprint: str
    branch: str
    staging_commit_sha: Optional[str] = None
    gates_passed: list = field(default_factory=list)
    gates_failed: list = field(default_factory=list)
    blocking_failures: list = field(default_factory=list)
    classification: str = "ASSIST"
    error: Optional[str] = None


def push_to_staging(worktree: Path, branch: str) -> tuple[bool, str]:
    """Merge branch onto auto-fix-staging in a worktree-safe way.

    For Phase 2 ASSIST mode, we don't actually push to a remote origin (Atlas
    isn't on github). Instead we fast-forward staging locally. The branch
    remains intact for human review.

    Returns (success, sha-or-error).
    """
    try:
        # Ensure auto-fix-staging exists (create from main if missing)
        check = subprocess.run(
            ["git", "rev-parse", "--verify", STAGING_BRANCH],
            cwd=PROJECT_ROOT, capture_output=True, timeout=10,
        )
        if check.returncode != 0:
            create = subprocess.run(
                ["git", "branch", STAGING_BRANCH, "main"],
                cwd=PROJECT_ROOT, capture_output=True, text=True, timeout=10,
            )
            if create.returncode != 0:
                return False, f"failed to create staging branch: {create.stderr}"

        # Get the fix-branch SHA
        sha_proc = subprocess.run(
            ["git", "rev-parse", branch],
            cwd=PROJECT_ROOT, capture_output=True, text=True, timeout=10,
        )
        if sha_proc.returncode != 0:
            return False, f"branch sha lookup failed: {sha_proc.stderr}"
        fix_sha = sha_proc.stdout.strip()

        # Get current staging sha
        cur = subprocess.run(
            ["git", "rev-parse", STAGING_BRANCH],
            cwd=PROJECT_ROOT, capture_output=True, text=True, timeout=10,
        )
        cur_sha = cur.stdout.strip() if cur.returncode == 0 else ""

        # Verify fast-forward is possible (staging must be ancestor of fix branch)
        merge_base = subprocess.run(
            ["git", "merge-base", STAGING_BRANCH, branch],
            cwd=PROJECT_ROOT, capture_output=True, text=True, timeout=10,
        )
        if merge_base.returncode != 0:
            return False, f"merge-base failed: {merge_base.stderr}"

        merge_base_sha = merge_base.stdout.strip()
        if cur_sha and merge_base_sha != cur_sha:
            return False, (
                f"not fast-forward: staging={cur_sha[:8]} "
                f"merge-base={merge_base_sha[:8]} — staging has diverged"
            )

        # Atomically advance staging to fix_sha using CAS update-ref.
        # If cur_sha is provided, git update-ref does a compare-and-swap —
        # the update only succeeds if staging is still at cur_sha (race guard).
        ref_args = ["git", "update-ref", f"refs/heads/{STAGING_BRANCH}", fix_sha]
        if cur_sha:
            ref_args.append(cur_sha)
        upd = subprocess.run(
            ref_args, cwd=PROJECT_ROOT, capture_output=True, text=True, timeout=10,
        )
        if upd.returncode != 0:
            return False, f"update-ref failed: {upd.stderr}"

        return True, fix_sha

    except subprocess.TimeoutExpired:
        return False, "timeout during git operations"
    except Exception as e:
        logger.exception("push_to_staging unexpected error")
        return False, str(e)


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _persist_merge_result(
    db_path: Path,
    attempt_id: int,
    error_id: int,
    gate_outcome: GateRunOutcome,
    staging_sha: Optional[str],
    success: bool,
    error_msg: Optional[str] = None,
) -> None:
    """Update fix_attempts + append fix_audit_log rows for this gate+merge cycle.

    Non-fatal: any DB error is logged but not re-raised (merge outcome already
    determined at this point).
    """
    gates_passed_json = json.dumps(gate_outcome.summary["passed"])
    gates_failed_json = json.dumps(gate_outcome.summary["failed"])
    blocking = gate_outcome.summary["blocking_failures"]
    blocked_by_gate = blocking[0] if blocking else None
    status = "merged" if success else "blocked"
    finished_ts = _now_utc()

    gate_payload: dict = {
        "gates_passed": gate_outcome.summary["passed"],
        "gates_failed": gate_outcome.summary["failed"],
        "blocking_failures": blocking,
        "staging_sha": staging_sha,
        "success": success,
    }
    if error_msg:
        gate_payload["error"] = error_msg

    conn = sqlite3.connect(str(db_path), timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        # Update fix_attempts row written by fix_worker
        conn.execute(
            """UPDATE fix_attempts SET
                status=?,
                finished_ts=?,
                gates_passed_json=?,
                gates_failed_json=?,
                blocked_by_gate=?
               WHERE id=?""",
            (status, finished_ts, gates_passed_json, gates_failed_json,
             blocked_by_gate, attempt_id),
        )

        # Append gate_check audit row — always written
        conn.execute(
            """INSERT INTO fix_audit_log
                (attempt_id, error_id, ts, phase, actor, decision,
                 payload_json, result_status, blocked_by_gate)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (
                attempt_id, error_id, finished_ts,
                "gate_check", "merger",
                "PASS" if gate_outcome.all_passed else "BLOCK",
                json.dumps(gate_payload),
                "success" if gate_outcome.all_passed else "blocked",
                blocked_by_gate,
            ),
        )

        # Append merge audit row — only if staging push succeeded
        if success and staging_sha:
            conn.execute(
                """INSERT INTO fix_audit_log
                    (attempt_id, error_id, ts, phase, actor, decision,
                     payload_json, result_status)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (
                    attempt_id, error_id, finished_ts,
                    "merge", "merger", "MERGED",
                    json.dumps({
                        "staging_branch": STAGING_BRANCH,
                        "staging_sha": staging_sha,
                    }),
                    "success",
                ),
            )

        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _send_failure_alert(outcome: MergeOutcome) -> None:
    """Send Telegram alert on merge failure. On success: never (Phase 2 policy)."""
    try:
        from utils.telegram import send_message  # type: ignore[import]

        lines = [
            "\U0001f534 <b>Auto-fix merge BLOCKED</b>",
            f"Fingerprint: <code>{outcome.fingerprint}</code>",
            f"Branch: <code>{outcome.branch}</code>",
        ]
        if outcome.blocking_failures:
            gates_str = ", ".join(outcome.blocking_failures)
            lines.append(f"Blocking gates: {gates_str}")
        if outcome.gates_failed:
            failed_str = ", ".join(outcome.gates_failed)
            lines.append(f"All failures: {failed_str}")
        if outcome.error:
            lines.append(f"Error: {outcome.error[:300]}")
        lines.append("\nHuman review required — check auto-fix-staging branch.")

        send_message("\n".join(lines))
    except Exception:
        logger.warning("Failed to send Telegram failure alert", exc_info=True)


def merge_fix(
    fix_outcome,
    reviewer_outcome,
    *,
    db_path: Optional[Path] = None,
    test_paths: Optional[list[str]] = None,
    diff_max_lines: int = 30,
    run_full_suite: bool = False,
) -> MergeOutcome:
    """Phase 2 ASSIST merge pipeline for one fix attempt.

    Args:
        fix_outcome:      Duck-typed object from fix_worker with attrs:
                          .attempt_id (int), .error_id (int), .fingerprint (str),
                          .branch (str), .worktree (Path), .success (bool)
        reviewer_outcome: core.reviewer.ReviewOutcome or None
        db_path:          Override for atlas.db path (tests pass tmp_path)
        test_paths:       Specific test files to run (None → tests/ directory)
        diff_max_lines:   Cap for gate 7 diff size (default 30)
        run_full_suite:   Run full pytest suite as gate 3 (slow; default False)

    Returns MergeOutcome with success=True iff ALL blocking gates passed
    AND staging branch was advanced.
    """
    resolved_db = db_path or DB_PATH_DEFAULT
    worktree: Path = Path(getattr(fix_outcome, "worktree", PROJECT_ROOT))
    branch: str = getattr(fix_outcome, "branch", "")
    attempt_id: int = getattr(fix_outcome, "attempt_id", -1)
    error_id: int = getattr(fix_outcome, "error_id", -1)
    fingerprint: str = getattr(fix_outcome, "fingerprint", "")

    # Short-circuit: if fix_outcome signals upstream failure, skip all gates
    if not getattr(fix_outcome, "success", True):
        outcome = MergeOutcome(
            success=False,
            error_id=error_id,
            fingerprint=fingerprint,
            branch=branch,
            error="fix_outcome.success=False — fix generation failed upstream",
        )
        _send_failure_alert(outcome)
        return outcome

    # ── Run all 15 gates ──────────────────────────────────────────────
    try:
        gate_outcome: GateRunOutcome = run_all_gates(
            worktree=worktree,
            branch=branch,
            reviewer_outcome=reviewer_outcome,
            test_paths=test_paths,
            diff_max_lines=diff_max_lines,
            run_full_suite=run_full_suite,
            db_path=resolved_db,
        )
    except Exception as e:
        logger.exception("run_all_gates raised unexpectedly")
        outcome = MergeOutcome(
            success=False,
            error_id=error_id,
            fingerprint=fingerprint,
            branch=branch,
            error=f"gate runner exception: {e}",
        )
        _send_failure_alert(outcome)
        return outcome

    gates_passed = gate_outcome.summary["passed"]
    gates_failed = gate_outcome.summary["failed"]
    blocking_failures = gate_outcome.summary["blocking_failures"]

    # ── Push to staging if all blocking gates passed ──────────────────
    staging_sha: Optional[str] = None
    push_success = False
    push_error: Optional[str] = None

    if gate_outcome.all_passed:
        push_success, sha_or_err = push_to_staging(worktree, branch)
        if push_success:
            staging_sha = sha_or_err
            logger.info(
                "fix pushed to %s: branch=%s sha=%s fingerprint=%s",
                STAGING_BRANCH, branch, staging_sha[:8] if staging_sha else "?", fingerprint,
            )
        else:
            push_error = sha_or_err
            logger.error("push_to_staging failed: %s", sha_or_err)

    success = gate_outcome.all_passed and push_success

    outcome = MergeOutcome(
        success=success,
        error_id=error_id,
        fingerprint=fingerprint,
        branch=branch,
        staging_commit_sha=staging_sha,
        gates_passed=gates_passed,
        gates_failed=gates_failed,
        blocking_failures=blocking_failures,
        classification="ASSIST",
        error=push_error if (not success and push_error) else None,
    )

    # ── Persist to DB (non-fatal) ─────────────────────────────────────
    if attempt_id >= 0:
        try:
            _persist_merge_result(
                db_path=resolved_db,
                attempt_id=attempt_id,
                error_id=error_id,
                gate_outcome=gate_outcome,
                staging_sha=staging_sha,
                success=success,
                error_msg=push_error,
            )
        except Exception:
            logger.exception("_persist_merge_result failed (non-fatal)")

    # ── Alert on failure only (Phase 2 config: telegram.on_success=NEVER) ──
    if not success:
        _send_failure_alert(outcome)

    return outcome
