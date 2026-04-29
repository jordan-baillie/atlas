#!/usr/bin/env python3
"""Promote auto-fix-staging -> main after a 30-min healthcheck-clean window.

Cron: */30 * * * *  (every 30 minutes)

Workflow:
  1. Find fix_attempts with status='merged' AND classification='AUTO_FIX'
     AND monitor_outcome='pending' AND started_ts older than 30 min
  2. For each:
     a. Check no same-fingerprint recurrence in errors table
     b. Check healthchecks not regressed since merge
     c. If clean: fast-forward main -> staging tip; mark monitor_outcome='clean'
     d. If regression: revert (git revert), mark monitor_outcome='reverted',
        create AUTO_REMEDIATION_HALT, Telegram alert
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
os.environ.setdefault("ATLAS_SQLITE_ERROR_WRITER", "0")

from utils.logging_config import setup_logging
from db import atlas_db

logger = setup_logging("promote_auto_fix_staging", telegram_errors=False)

MONITOR_WINDOW_MIN = 30


def find_pending_promotions(conn) -> list:
    """Return fix_attempts rows ready for promotion review (window elapsed)."""
    cutoff = (
        datetime.now(timezone.utc) - timedelta(minutes=MONITOR_WINDOW_MIN)
    ).strftime("%Y-%m-%dT%H:%M:%S")
    rows = conn.execute(
        """SELECT id, error_id, fingerprint, fix_branch, fix_commit_sha, started_ts
           FROM fix_attempts
           WHERE status='merged' AND classification='AUTO_FIX'
             AND monitor_outcome='pending'
             AND started_ts <= ?
           ORDER BY started_ts ASC""",
        (cutoff,),
    ).fetchall()
    return rows


def check_fingerprint_recurrence(conn, fingerprint: str, since_ts: str) -> int:
    """Return number of times this fingerprint has been seen since the merge."""
    row = conn.execute(
        "SELECT COUNT(*) FROM errors WHERE fingerprint=? AND last_seen_ts > ?",
        (fingerprint, since_ts),
    ).fetchone()
    return row[0] if row else 0


def check_healthchecks_clean(conn, since_ts: str) -> bool:
    """Any new CRITICAL healthcheck failures since the merge?"""
    row = conn.execute(
        """SELECT COUNT(*) FROM errors
           WHERE source='healthcheck' AND level='CRITICAL'
             AND first_seen_ts > ?""",
        (since_ts,),
    ).fetchone()
    return (row[0] if row else 0) == 0


def fast_forward_main_to_staging() -> tuple[bool, str]:
    """Fast-forward main -> auto-fix-staging tip via git update-ref CAS."""
    sha_proc = subprocess.run(
        ["git", "rev-parse", "auto-fix-staging"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=10,
    )
    if sha_proc.returncode != 0:
        return False, f"staging branch missing: {sha_proc.stderr.strip()}"
    staging_sha = sha_proc.stdout.strip()

    cur = subprocess.run(
        ["git", "rev-parse", "main"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=10,
    )
    cur_main = cur.stdout.strip() if cur.returncode == 0 else ""

    # Verify staging is descendant of main (true fast-forward)
    base = subprocess.run(
        ["git", "merge-base", "main", "auto-fix-staging"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=10,
    )
    if base.returncode != 0 or base.stdout.strip() != cur_main:
        return False, "not a fast-forward (main has diverged)"

    upd = subprocess.run(
        ["git", "update-ref", "refs/heads/main", staging_sha, cur_main],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=10,
    )
    if upd.returncode != 0:
        return False, f"update-ref failed: {upd.stderr.strip()}"
    return True, staging_sha


def revert_promotion(commit_sha: str, reason: str) -> tuple[bool, str]:
    """Revert the fix commit on main. Sets AUTO_REMEDIATION_HALT."""
    rv = subprocess.run(
        ["git", "revert", "--no-edit", commit_sha],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if rv.returncode != 0:
        return False, f"revert failed: {rv.stderr.strip()}"
    revert_sha_proc = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=10,
    )
    revert_sha = revert_sha_proc.stdout.strip() if revert_sha_proc.returncode == 0 else ""
    # Set halt
    try:
        from core.remediation_kill_switch import halt

        halt(f"auto-revert: {reason}", source="promote_auto_fix_staging")
    except Exception as e:
        logger.warning("halt set failed: %s", e)
    return True, revert_sha


def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def main(argv: list | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Promote auto-fix-staging -> main after 30-min clean window"
    )
    p.add_argument("--db", help="Override atlas.db path (default: data/atlas.db)")
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would happen without modifying DB or git",
    )
    args = p.parse_args(argv)

    promoted: list = []
    reverted: list = []
    skipped: list = []

    with atlas_db.get_db(args.db) as conn:
        pending = find_pending_promotions(conn)
        logger.info(
            "Found %d AUTO_FIX commits awaiting promotion (>%d min old)",
            len(pending),
            MONITOR_WINDOW_MIN,
        )

        for row in pending:
            since = row["started_ts"] if hasattr(row, "__getitem__") else row[5]
            fp = row["fingerprint"] if hasattr(row, "__getitem__") else row[2]
            attempt_id = row["id"] if hasattr(row, "__getitem__") else row[0]
            error_id = row["error_id"] if hasattr(row, "__getitem__") else row[1]
            fix_commit_sha = (
                row["fix_commit_sha"] if hasattr(row, "__getitem__") else row[4]
            )

            recurrence = check_fingerprint_recurrence(conn, fp, since)
            hc_clean = check_healthchecks_clean(conn, since)
            now_ts = _now_utc()

            if recurrence > 0:
                logger.warning(
                    "Fingerprint recurred %d times since merge — reverting: fp=%s",
                    recurrence,
                    fp,
                )
                if not args.dry_run:
                    ok, sha_or_err = revert_promotion(fix_commit_sha or "", "fingerprint recurred")
                    conn.execute(
                        """UPDATE fix_attempts SET monitor_outcome='reverted',
                           revert_commit_sha=?, revert_reason=?, reverted_ts=? WHERE id=?""",
                        (
                            sha_or_err if ok else None,
                            "fingerprint recurred",
                            now_ts,
                            attempt_id,
                        ),
                    )
                    conn.execute(
                        """INSERT INTO fix_audit_log (attempt_id, error_id, ts, phase, actor,
                              decision, reasoning, result_status)
                           VALUES (?, ?, ?, 'revert', 'monitor', 'AUTO_REVERT_FINGERPRINT', ?, ?)""",
                        (
                            attempt_id,
                            error_id,
                            now_ts,
                            f"recurrence={recurrence}",
                            "success" if ok else "error",
                        ),
                    )
                reverted.append(fp)
                continue

            if not hc_clean:
                logger.warning(
                    "Healthcheck regression detected since merge — reverting: fp=%s", fp
                )
                if not args.dry_run:
                    ok, sha_or_err = revert_promotion(
                        fix_commit_sha or "", "healthcheck regression"
                    )
                    conn.execute(
                        """UPDATE fix_attempts SET monitor_outcome='reverted',
                           revert_commit_sha=?, revert_reason=?, reverted_ts=? WHERE id=?""",
                        (
                            sha_or_err if ok else None,
                            "healthcheck regression",
                            now_ts,
                            attempt_id,
                        ),
                    )
                    conn.execute(
                        """INSERT INTO fix_audit_log (attempt_id, error_id, ts, phase, actor,
                              decision, reasoning, result_status)
                           VALUES (?, ?, ?, 'revert', 'monitor', 'AUTO_REVERT_HEALTHCHECK', ?, ?)""",
                        (
                            attempt_id,
                            error_id,
                            now_ts,
                            "healthcheck_regression",
                            "success" if ok else "error",
                        ),
                    )
                reverted.append(fp)
                continue

            # CLEAN — promote staging -> main
            if not args.dry_run:
                ok, sha_or_err = fast_forward_main_to_staging()
                if ok:
                    conn.execute(
                        "UPDATE fix_attempts SET monitor_outcome='clean' WHERE id=?",
                        (attempt_id,),
                    )
                    conn.execute(
                        """INSERT INTO fix_audit_log (attempt_id, error_id, ts, phase, actor,
                              decision, reasoning, result_status)
                           VALUES (?, ?, ?, 'merge', 'monitor', 'PROMOTED_TO_MAIN', ?, 'success')""",
                        (
                            attempt_id,
                            error_id,
                            now_ts,
                            f"sha={sha_or_err}",
                        ),
                    )
                    logger.info("Promoted to main: fp=%s sha=%s", fp, sha_or_err)
                    promoted.append((fp, sha_or_err))
                else:
                    logger.error("Fast-forward failed: fp=%s err=%s", fp, sha_or_err)
                    skipped.append((fp, sha_or_err))
            else:
                logger.info(
                    "DRY-RUN would promote: fp=%s sha=%s",
                    fp,
                    fix_commit_sha,
                )
                promoted.append((fp, "dry-run"))

    summary = {
        "promoted": [list(x) for x in promoted],
        "reverted": reverted,
        "skipped": [list(x) for x in skipped],
    }
    logger.info(
        "Promotion summary: promoted=%d reverted=%d skipped=%d",
        len(promoted),
        len(reverted),
        len(skipped),
    )
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
