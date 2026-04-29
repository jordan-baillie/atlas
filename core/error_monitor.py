"""Atlas auto-remediation error monitor — Phase 1 dispatcher.

Pulls UNCLASSIFIED errors from the errors table, runs the deterministic
triage classifier, updates the row with classification + reason, and
appends an audit log entry.

Phase 1: DRY_RUN — does NOT dispatch fix workers (no Phase 2 yet).
Phase 2: triggers Fix Worker for ASSIST classifications.
Phase 3: also triggers Auto Merger for AUTO_FIX classifications.

Run via systemd timer (atlas-error-remediation.timer):
  - Every 5 min during US RTH (13:30-21:00 UTC)
  - Every 15 min off-hours

Or invoke once: python3 -m core.error_monitor --once

Hard halt checks (run BEFORE any classification):
  - data/HALT (trading kill switch)
  - .live_halt (legacy halt file)
  - data/AUTO_REMEDIATION_HALT (remediation-specific halt)
  - env ATLAS_AUTO_REMEDIATION_DISABLED=1
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# Disable SQLiteErrorWriter for the monitor itself (no recursive writes)
os.environ.setdefault("ATLAS_SQLITE_ERROR_WRITER", "0")

from utils.logging_config import setup_logging
from db import atlas_db

logger = setup_logging("error_monitor", telegram_errors=False)

HALT_FILES = (
    PROJECT_ROOT / "data" / "HALT",
    PROJECT_ROOT / ".live_halt",
    PROJECT_ROOT / "data" / "AUTO_REMEDIATION_HALT",
)


def is_disabled_via_env() -> bool:
    """Return True when ATLAS_AUTO_REMEDIATION_DISABLED=1."""
    return os.environ.get("ATLAS_AUTO_REMEDIATION_DISABLED", "0") == "1"


def find_halt_reason() -> Optional[str]:
    """Return the first present halt-file path string, or None if clear."""
    if is_disabled_via_env():
        return "env:ATLAS_AUTO_REMEDIATION_DISABLED=1"
    for p in HALT_FILES:
        if p.exists():
            return str(p)
    return None


def fetch_unclassified(conn, limit: int) -> list[dict]:
    """Return up to *limit* rows from errors where classification=UNCLASSIFIED."""
    rows = conn.execute(
        """SELECT id, fingerprint, ts, source, service, level, logger_name, message,
                  exc_type, exc_message, traceback, file_path, line_number, function_name,
                  classification, tier, remediation_status, occurrence_count
           FROM errors
           WHERE classification = 'UNCLASSIFIED'
             AND remediation_status = 'NEW'
           ORDER BY last_seen_ts DESC
           LIMIT ?""",
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def update_classification(conn, error_id: int, result, dry_run: bool) -> None:
    """Apply triage classification to errors row + append fix_audit_log entry.

    In dry_run mode the SQL still runs (so subsequent cycles don't re-process the
    same row), but no fix worker is dispatched.  The audit log row notes
    phase='triage', actor='classifier', dry_run=True.

    Status mapping:
      IGNORE              → IGNORED
      ESCALATE            → ESCALATED
      IGNORE_PENDING_CLEAR / ESCALATE_DEFERRED → NEW (re-evaluate next cycle)
      everything else     → TRIAGED
    """
    if result.classification == "IGNORE":
        new_status = "IGNORED"
    elif result.classification == "ESCALATE":
        new_status = "ESCALATED"
    elif result.classification in ("IGNORE_PENDING_CLEAR", "ESCALATE_DEFERRED"):
        # Leave NEW so we re-evaluate after the halt/condition clears.
        new_status = "NEW"
    else:
        new_status = "TRIAGED"

    now_ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")

    conn.execute(
        """UPDATE errors
           SET classification      = ?,
               triage_reason       = ?,
               tier                = ?,
               remediation_status  = ?,
               last_attempt_at     = ?
           WHERE id = ?""",
        (
            result.classification,
            result.reason,
            result.tier,
            new_status,
            now_ts,
            error_id,
        ),
    )
    conn.execute(
        """INSERT INTO fix_audit_log (
               error_id, ts, phase, actor, model, decision, reasoning,
               payload_json, result_status
           ) VALUES (?, ?, 'triage', 'classifier', NULL, ?, ?, ?, 'success')""",
        (
            error_id,
            now_ts,
            result.classification,
            f"rule={result.rule_id}; tier={result.tier}; reason={result.reason}",
            json.dumps({
                "dry_run": dry_run,
                "rule_id": result.rule_id,
                "tier": result.tier,
            }),
        ),
    )


def run_once(*, db_path: Optional[str] = None, batch_size: int = 50, dry_run: bool = True) -> dict:
    """One classifier sweep. Returns metrics dict.

    Phase 1 (dry_run=True): only classifies + writes audit. No fix dispatch.
    Phase 2 (dry_run=False): for ASSIST classifications, dispatches fix_worker →
    reviewer → merger pipeline. Budget + kill-switch checked before each dispatch.
    """
    halt = find_halt_reason()
    if halt:
        logger.warning("Halted by %s — no classification this cycle", halt)
        return {"halted": True, "halt_reason": halt, "processed": 0,
                "by_class": {}, "errors": 0, "fixes_attempted": 0,
                "fixes_succeeded": 0, "fixes_failed": 0}

    # Lazy import — avoids circular imports
    try:
        from core.triage import TriageClassifier
    except Exception as e:
        logger.error("core.triage not importable: %s", e)
        return {"halted": False, "halt_reason": None, "processed": 0,
                "by_class": {}, "errors": 1, "import_error": str(e),
                "fixes_attempted": 0, "fixes_succeeded": 0, "fixes_failed": 0}

    classifier = TriageClassifier()

    metrics = {"halted": False, "halt_reason": None, "processed": 0,
               "by_class": {}, "errors": 0, "dry_run": dry_run,
               "fixes_attempted": 0, "fixes_succeeded": 0, "fixes_failed": 0,
               "fixes_skipped_budget": 0, "fixes_blocked_kill_switch": 0}

    with atlas_db.get_db(db_path) as conn:
        rows = fetch_unclassified(conn, limit=batch_size)
        logger.info("Fetched %d UNCLASSIFIED errors (batch_size=%d)", len(rows), batch_size)
        for row in rows:
            try:
                result = classifier.classify(row)
                update_classification(conn, row["id"], result, dry_run=dry_run)
                metrics["processed"] += 1
                metrics["by_class"][result.classification] = metrics["by_class"].get(result.classification, 0) + 1
            except Exception as e:
                logger.exception("Classifier crash on error_id=%s: %s", row["id"], e)
                metrics["errors"] += 1
                continue

            # ── Phase 2 dispatch — only when dry_run=False AND classification=ASSIST ──
            if dry_run:
                continue
            if result.classification not in ("ASSIST", "AUTO_FIX"):
                continue
            # AUTO_FIX must be additionally gated by phase_3_enabled — Phase 2 only ASSIST
            if result.classification == "AUTO_FIX":
                # Phase 3 not yet enabled; downgrade to ASSIST for safety
                logger.info("AUTO_FIX classified but Phase 3 disabled — treating as ASSIST")
                result_classification = "ASSIST"
            else:
                result_classification = result.classification

            # Budget check — refuse to dispatch if at cap or rate-halt
            try:
                from core.budget import enforce_budget
                bd = enforce_budget(db_path=db_path, send_alert=True)
                if bd.action == "HALT":
                    metrics["fixes_skipped_budget"] += 1
                    logger.warning("Budget HALT — skipping dispatch for error %s", row["id"])
                    continue
            except Exception as e:
                logger.warning("Budget check crashed (failing closed): %s", e)
                continue

            # Kill-switch — re-check (something might have happened mid-cycle)
            try:
                from core.remediation_kill_switch import check_all_layers
                blk = check_all_layers(db_path=db_path)
                if blk:
                    metrics["fixes_blocked_kill_switch"] += 1
                    logger.warning("Kill-switch %s — skipping dispatch for error %s", blk.layer, row["id"])
                    continue
            except Exception as e:
                logger.warning("Kill-switch check crashed (failing closed): %s", e)
                continue

            # Dispatch fix_worker
            metrics["fixes_attempted"] += 1
            try:
                from core import fix_worker, reviewer, merger
                fx = fix_worker.run_fix(dict(row), classification=result_classification)
                if not fx.success:
                    metrics["fixes_failed"] += 1
                    _audit_dispatch_outcome(conn, row["id"], "fix_worker_failed", fx)
                    continue

                # Reviewer (separate process, adversarial)
                rv = reviewer.review_fix(dict(row), fx.diff or "",
                                         test_output="", diagnosis=fx.diagnosis or "")

                # Merger — Phase 2 path: branch only, no auto-merge to main
                # We pass the FixOutcome + ReviewOutcome to merger.merge_fix
                fix_outcome_for_merger = type("FXO", (), {
                    "attempt_id": -1,  # not yet persisted
                    "error_id": row["id"],
                    "fingerprint": row["fingerprint"],
                    "branch": fx.branch,
                    "worktree": fx.worktree_path,
                    "success": fx.success,
                    "diff": fx.diff,
                    "diff_lines": fx.diff_lines,
                    "classification": result_classification,
                })()

                mg = merger.merge_fix(fix_outcome_for_merger, rv, db_path=db_path)
                if mg.success:
                    metrics["fixes_succeeded"] += 1
                else:
                    metrics["fixes_failed"] += 1
            except Exception as e:
                logger.exception("Phase 2 dispatch crashed for error %s: %s", row["id"], e)
                metrics["fixes_failed"] += 1

    logger.info("Cycle complete: %s", metrics)
    return metrics


def _audit_dispatch_outcome(conn, error_id: int, outcome_label: str, fx) -> None:
    """Write an audit row noting why a fix attempt skipped or failed."""
    try:
        conn.execute(
            """INSERT INTO fix_audit_log (error_id, ts, phase, actor, decision, reasoning, payload_json, result_status)
               VALUES (?, ?, 'fix', 'fix_worker', ?, ?, ?, 'error')""",
            (error_id,
             datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"),
             outcome_label,
             (fx.error or "")[:500],
             json.dumps({"branch": fx.branch, "diff_lines": fx.diff_lines, "duration": fx.duration_seconds})),
        )
    except Exception as e:
        logger.warning("Audit write failed: %s", e)


def main(argv: Optional[list[str]] = None) -> int:
    """CLI entry point."""
    p = argparse.ArgumentParser(
        description="Atlas error-remediation monitor (Phase 1)"
    )
    p.add_argument(
        "--once",
        action="store_true",
        help="Run a single classification cycle then exit",
    )
    p.add_argument("--batch-size", type=int, default=50)
    p.add_argument(
        "--dry-run",
        action="store_true",
        default=True,
        dest="dry_run",
        help="Write classification labels but do NOT dispatch fix workers (default: on)",
    )
    p.add_argument(
        "--no-dry-run",
        dest="dry_run",
        action="store_false",
        help="Enable live fix dispatch (Phase 2+, not yet implemented)",
    )
    p.add_argument("--db", default=None, help="Override DB path (testing only)")
    args = p.parse_args(argv)

    if not args.once:
        # Phase 1 has no daemon mode — must be invoked from systemd timer or manually.
        logger.error("--once is required in Phase 1 (daemon mode not implemented)")
        return 2

    metrics = run_once(
        db_path=args.db,
        batch_size=args.batch_size,
        dry_run=args.dry_run,
    )
    print(json.dumps(metrics, indent=2))
    return 0 if metrics.get("errors", 0) == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
