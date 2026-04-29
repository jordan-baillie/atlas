"""Atlas auto-remediation Auto Merger — Phase 3 path.

Extends core/merger.py with the AUTO_FIX promotion path. Only invoked when:
  1. config/auto_remediation.yaml phase.phase_3_enabled = true
  2. Error classification = AUTO_FIX (per triage classifier with whitelist match)
  3. ALL 15 merge gates pass
  4. Reviewer APPROVED with confidence >= 0.75

Even Phase 3 fixes go to auto-fix-staging FIRST. The promotion to main is gated
by a SEPARATE cron (scripts/promote_auto_fix_staging.py) that runs every 30 min:
  • 30 minutes elapsed since last merge?
  • Healthchecks clean for 30 min post-merge?
  • No same-fingerprint recurrence?
  → only then fast-forward merge staging → main

This is defense-in-depth: even an erroneous gate-passing fix has a 30-min
window where it can be auto-reverted before reaching production.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
STAGING_BRANCH = "auto-fix-staging"
CFG_PATH = PROJECT_ROOT / "config" / "auto_remediation.yaml"
CLASSES_PATH = PROJECT_ROOT / "config" / "auto_fix_classes.yaml"

logger = logging.getLogger(__name__)


@dataclass
class AutoMergeOutcome:
    success: bool
    error_id: int
    fingerprint: str
    branch: str
    matched_class: Optional[str] = None
    classification: str = "AUTO_FIX"
    promoted_to_main: bool = False
    staging_sha: Optional[str] = None
    error: Optional[str] = None
    gates_passed: list = field(default_factory=list)
    gates_failed: list = field(default_factory=list)


def _load_phase_3_state() -> bool:
    """Read phase.phase_3_enabled — multiple gates: env override + config."""
    import os

    env = os.environ.get("AUTO_REMEDIATION_PHASE_3_ENABLED", "").lower()
    if env == "true":
        return True
    if env == "false":
        return False
    # Fall through to config
    if not CFG_PATH.exists():
        return False
    try:
        cfg = yaml.safe_load(CFG_PATH.read_text()) or {}
    except Exception:
        return False
    return bool((cfg.get("phase") or {}).get("phase_3_enabled", False))


def _load_classes() -> list:
    """Load the whitelist classes from auto_fix_classes.yaml."""
    if not CLASSES_PATH.exists():
        return []
    try:
        cfg = yaml.safe_load(CLASSES_PATH.read_text()) or {}
    except Exception:
        return []
    return list(cfg.get("classes") or [])


def match_auto_fix_class(error: dict) -> Optional[dict]:
    """Match an error against the whitelist. Returns the matching class dict or None."""
    import fnmatch
    import re

    classes = _load_classes()
    msg = error.get("message") or ""
    exc_type = error.get("exc_type") or ""
    file_path = error.get("file_path") or ""

    for cls in classes:
        # Required: message_regex
        msg_re = cls.get("message_regex")
        if msg_re and not re.search(msg_re, msg, re.IGNORECASE):
            continue

        # Required: at least one file_path_globs match
        globs = cls.get("file_path_globs") or []
        glob_match = False
        for g in globs:
            if "**" in g:
                prefix = g.split("**")[0].rstrip("/")
                if prefix:
                    if file_path.startswith(prefix) and fnmatch.fnmatch(
                        file_path, g.replace("**", "*")
                    ):
                        glob_match = True
                        break
                else:
                    # Pattern like **/*.md — match anywhere
                    if fnmatch.fnmatch(file_path, g.replace("**", "*")):
                        glob_match = True
                        break
            elif fnmatch.fnmatch(file_path, g):
                glob_match = True
                break
        if not glob_match:
            continue

        # Excluded: file_path_block_globs
        block_globs = cls.get("file_path_block_globs") or []
        blocked = False
        for g in block_globs:
            if fnmatch.fnmatch(file_path, g):
                blocked = True
                break
        if blocked:
            continue

        # Optional: exc_type_regex
        exc_re = cls.get("exc_type_regex")
        if exc_re and not re.match(exc_re, exc_type):
            continue

        return cls

    return None


def auto_merge(
    fix_outcome,
    review_outcome,
    *,
    error: dict,
    db_path: Optional[str] = None,
) -> AutoMergeOutcome:
    """Phase 3 auto-merge path: ALL 15 gates + reviewer APPROVE + whitelist match."""
    out = AutoMergeOutcome(
        success=False,
        error_id=error.get("id", 0),
        fingerprint=error.get("fingerprint", ""),
        branch=getattr(fix_outcome, "branch", "") or "",
        gates_passed=[],
        gates_failed=[],
    )

    # Phase 3 enable check
    if not _load_phase_3_state():
        out.error = "phase_3_enabled=false — auto-merge not active"
        return out

    # Whitelist match
    cls = match_auto_fix_class(error)
    if cls is None:
        out.error = "error not in AUTO_FIX whitelist"
        return out
    out.matched_class = cls["name"]

    # Diff cap (per-class override)
    max_lines = int(cls.get("max_diff_lines", 30))
    diff_lines = getattr(fix_outcome, "diff_lines", 0) or 0
    if diff_lines > max_lines:
        out.error = f"diff {diff_lines} > class cap {max_lines}"
        return out

    # Reviewer must APPROVE
    if not (
        getattr(review_outcome, "success", False)
        and getattr(review_outcome, "verdict", "") == "APPROVE"
    ):
        out.error = (
            f"reviewer did not APPROVE: verdict={getattr(review_outcome, 'verdict', '?')}"
        )
        return out

    # Reviewer confidence threshold (per-class override)
    threshold = float(cls.get("reviewer_confidence_threshold", 0.75))
    rv_confidence = getattr(review_outcome, "confidence", 0.0) or 0.0
    if rv_confidence < threshold:
        out.error = f"reviewer confidence {rv_confidence:.2f} < {threshold}"
        return out

    # Run all 15 gates with run_full_suite=True (Phase 3 = strict)
    try:
        from core import merge_gates

        gate_outcome = merge_gates.run_all_gates(
            Path(getattr(fix_outcome, "worktree_path", "/tmp")),
            getattr(fix_outcome, "branch", ""),
            reviewer_outcome=review_outcome,
            run_full_suite=True,
            diff_max_lines=max_lines,
            db_path=db_path,
        )
        out.gates_passed = list(gate_outcome.summary["passed"])
        out.gates_failed = list(gate_outcome.summary["failed"])
        if not gate_outcome.all_passed:
            out.error = f"gates blocked: {gate_outcome.summary['blocking_failures']}"
            return out
    except Exception as e:
        out.error = f"gate run crashed: {e}"
        return out

    # Push to staging (NOT main yet)
    try:
        from core.merger import push_to_staging

        ok, sha = push_to_staging(
            Path(getattr(fix_outcome, "worktree_path", "/tmp")),
            getattr(fix_outcome, "branch", ""),
        )
        if not ok:
            out.error = f"staging push failed: {sha}"
            return out
        out.staging_sha = sha
    except Exception as e:
        out.error = f"staging push crashed: {e}"
        return out

    # Persist fix_attempts row + audit (non-fatal)
    try:
        _persist_auto_merge(db_path, out, fix_outcome, review_outcome, cls)
    except Exception as e:
        logger.warning("audit persist failed: %s", e)

    out.success = True
    return out


def _persist_auto_merge(
    db_path: Optional[str],
    outcome: AutoMergeOutcome,
    fx,
    rv,
    cls: dict,
) -> None:
    """Insert fix_attempts row + fix_audit_log entries."""
    if not db_path:
        return
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    with sqlite3.connect(db_path, timeout=10) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        cur = conn.execute(
            """INSERT INTO fix_attempts (
                error_id, fingerprint, started_ts, finished_ts,
                status, classification,
                fix_branch, fix_commit_sha, fix_diff_lines,
                review_verdict, review_confidence, review_reason,
                gates_passed_json, gates_failed_json,
                monitor_outcome, total_wall_seconds, notes
            ) VALUES (?, ?, ?, ?, 'merged', 'AUTO_FIX', ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)""",
            (
                outcome.error_id,
                outcome.fingerprint,
                now,
                now,
                outcome.branch,
                outcome.staging_sha,
                getattr(fx, "diff_lines", 0) or 0,
                "APPROVE",
                getattr(rv, "confidence", 0.0) or 0.0,
                (getattr(rv, "reason", "") or "")[:500],
                json.dumps(outcome.gates_passed),
                json.dumps(outcome.gates_failed),
                getattr(fx, "duration_seconds", 0.0) or 0.0,
                f"matched_class={cls['name']} staging_only=True awaiting_promotion=True",
            ),
        )
        attempt_id = cur.lastrowid
        conn.execute(
            """INSERT INTO fix_audit_log (
                attempt_id, error_id, ts, phase, actor, decision, reasoning,
                payload_json, result_status
            ) VALUES (?, ?, ?, 'merge', 'auto_merger', 'AUTO_FIX_STAGED',
                      ?, ?, 'success')""",
            (
                attempt_id,
                outcome.error_id,
                now,
                f"matched_class={cls['name']}, staging_sha={outcome.staging_sha}",
                json.dumps({"branch": outcome.branch, "matched_class": cls["name"]}),
            ),
        )
        conn.commit()
