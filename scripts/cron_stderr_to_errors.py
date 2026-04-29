#!/usr/bin/env python3
"""Receive cron stderr + exit code from cron_stderr_capture.sh and insert
a row into the errors table.

Called only on non-zero exit. Reads the stderr buffer file, computes a
fingerprint over the (job_name, last meaningful stderr lines, exit_code),
and upserts into errors.

Idempotent: same job + same recent stderr → bumps occurrence_count.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import datetime
from pathlib import Path

# Disable the SQLite error writer for this script (we ARE the writer)
os.environ["ATLAS_SQLITE_ERROR_WRITER"] = "0"

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

from utils.logging_config import setup_logging

try:
    from utils.error_fingerprint import compute_fingerprint
except ImportError:
    # Fallback for when utils/error_fingerprint.py hasn't been migrated yet.
    # Produces a stable 16-char hex digest from (exc_type, message[:200]).
    def compute_fingerprint(  # type: ignore[misc]
        exc_type: str,
        message: str,
        file_path: object = None,
        line_number: object = None,
    ) -> str:
        raw = f"{exc_type}:{(message or '')[:200]}"
        return hashlib.sha256(raw.encode()).hexdigest()[:16]

from db import atlas_db

logger = setup_logging("cron_stderr_capture", telegram_errors=False)

# Signals that indicate process termination by the OS — use CRITICAL level
_CRITICAL_EXIT_CODES: frozenset[int] = frozenset({134, 137, 139})

# Maximum length for the stored message (avoid blob bloat)
_MAX_MSG_LEN: int = 8000


def _last_lines(path: Path, n: int = 50) -> str:
    """Return the last *n* non-empty lines from *path*, joined by newline.

    Returns an empty string if the file does not exist or cannot be read.
    """
    if not path.exists():
        return ""
    try:
        text = path.read_text(errors="replace")
    except Exception:
        return ""
    lines = [line for line in text.splitlines() if line.strip()]
    return "\n".join(lines[-n:])


def _level_for_exit(code: int) -> str:
    """Map an exit code to a log level string.

    Signals 134 (ABRT), 137 (SIGKILL/OOM), 139 (SIGSEGV) are CRITICAL;
    everything else is ERROR.
    """
    if code in _CRITICAL_EXIT_CODES:
        return "CRITICAL"
    return "ERROR"


def _build_context_json(exit_code: int, command: str) -> str:
    """Serialise supplemental context for the context_json column."""
    return json.dumps({"exit_code": exit_code, "command": command})


def main() -> int:
    p = argparse.ArgumentParser(
        description="Write a cron error row to the errors table.",
    )
    p.add_argument("--job", required=True, help="Cron job name (e.g. eod_settlement)")
    p.add_argument("--exit-code", type=int, required=True, dest="exit_code")
    p.add_argument("--stderr-file", required=True, dest="stderr_file")
    p.add_argument("--command", default="", help="The full command that was wrapped")
    p.add_argument(
        "--db",
        default=None,
        help="Override DB path (for tests; defaults to atlas_db._db_path_override or DB_PATH)",
    )
    args = p.parse_args()

    # Read last 50 non-empty stderr lines
    stderr_tail = _last_lines(Path(args.stderr_file), n=50)
    level = _level_for_exit(args.exit_code)

    # Build the human-readable message
    msg = f"Cron job '{args.job}' exited {args.exit_code}"
    if stderr_tail:
        msg += f"\n--- stderr tail ---\n{stderr_tail}"
    msg = msg[:_MAX_MSG_LEN]

    # Fingerprint: different exit codes for the same job → distinct records
    fingerprint_input = f"cron:{args.job}:exit{args.exit_code}"
    fp = compute_fingerprint(
        exc_type=fingerprint_input,
        message=stderr_tail or msg,
        file_path=None,
        line_number=None,
    )
    ts = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S")
    context_json = _build_context_json(args.exit_code, args.command)

    try:
        with atlas_db.get_db(args.db) as conn:
            # Graceful degradation: if the errors table hasn't been migrated yet,
            # log a warning and exit cleanly rather than crashing the cron job.
            table_exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='errors'"
            ).fetchone()
            if not table_exists:
                logger.warning(
                    "errors table missing — migration not run; cron capture skipped"
                )
                return 0

            existing = conn.execute(
                "SELECT id FROM errors WHERE fingerprint = ?",
                (fp,),
            ).fetchone()

            if existing:
                conn.execute(
                    "UPDATE errors "
                    "SET occurrence_count = occurrence_count + 1, "
                    "    last_seen_ts = ?, ts = ? "
                    "WHERE id = ?",
                    (ts, ts, existing["id"]),
                )
            else:
                conn.execute(
                    """INSERT INTO errors (
                        fingerprint, first_seen_ts, last_seen_ts, occurrence_count, ts,
                        source, service, level, logger_name, message,
                        classification, tier, remediation_status, context_json
                    ) VALUES (?, ?, ?, 1, ?, 'cron', ?, ?, ?, ?, 'UNCLASSIFIED', 99, 'NEW', ?)""",
                    (
                        fp, ts, ts, ts,
                        args.job, level, args.job, msg,
                        context_json,
                    ),
                )
    except Exception as e:
        # Best-effort — never crash a cron job because the DB is unavailable
        logger.warning("Failed to write cron error to errors table: %s", e)
        return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
