#!/usr/bin/env python3
"""Backfill errors table from 30 days of historical logs.

Sources:
  1. logs/atlas.log + rotations (.1, .2, ..., .gz)
  2. journalctl -u 'atlas-*' --since=30d
  3. data/atlas.db:system_log (existing telemetry table)

Idempotent: every record's fingerprint is computed; if already in errors,
occurrence_count is bumped, no duplicate row.

Usage:
  python3 scripts/backfill_errors_from_logs.py             # dry-run, all sources
  python3 scripts/backfill_errors_from_logs.py --apply --days 30
  python3 scripts/backfill_errors_from_logs.py --source python_logs --limit 100
"""
from __future__ import annotations

import argparse
import gzip
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterator

# Prevent the SQLiteErrorWriter (Phase 0 capture hook) from firing here —
# the backfill IS the population mechanism; writing to errors from within
# the backfill would create a write-loop.  Must be set before any Atlas import.
os.environ.setdefault("ATLAS_SQLITE_ERROR_WRITER", "0")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from utils.logging_config import setup_logging  # noqa: E402

# Fallback implementation used when utils/error_fingerprint.py has not yet
# been created by the parallel migration worker.  The fallback is SHA-256
# based and satisfies the same contract: same inputs → same 16-char hex,
# different meaningful inputs → different hex.
try:
    from utils.error_fingerprint import compute_fingerprint  # type: ignore
except ImportError:
    import hashlib as _hashlib

    def compute_fingerprint(  # type: ignore[misc]
        exc_type: str | None,
        message: str | None,
        file_path: str | None,
        line_number: int | None,
    ) -> str:
        key = f"{exc_type or ''}:{message or ''}:{file_path or ''}:{line_number or ''}"
        return _hashlib.sha256(key.encode()).hexdigest()[:16]

from db import atlas_db  # noqa: E402

logger = setup_logging("backfill_errors_from_logs", telegram_errors=False)

# ── Log-line regex ───────────────────────────────────────────────────────────
# Format: "2026-04-29 10:00:00 [ERROR] atlas.live_executor: Order failed for AAPL"
_LOG_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\s+"
    r"\[(?P<level>WARNING|ERROR|CRITICAL)\]\s+"
    r"(?P<logger>[^\s:]+):\s+"
    r"(?P<msg>.*)$"
)

# Overridable log directory — tests monkeypatch this to point at tmp_path.
_LOG_DIR: Path | None = None


# ── Parsing helpers ──────────────────────────────────────────────────────────

def _is_continuation(line: str) -> bool:
    """Return True if the line is a traceback continuation line."""
    return bool(line) and (
        line[0].isspace()
        or line.startswith("Traceback")
        or line.startswith("  File")
    )


def _parse_log_lines(lines: Iterator[str]) -> Iterator[dict]:
    """Yield one dict per log record with collected traceback lines.

    Each yielded dict has keys: ts, level, logger_name, message,
    traceback_lines (list[str]).
    """
    current: dict | None = None
    for raw_line in lines:
        line = raw_line.rstrip("\n")
        m = _LOG_RE.match(line)
        if m:
            if current is not None:
                yield current
            current = {
                "ts": m["ts"],
                "level": m["level"],
                "logger_name": m["logger"],
                "message": m["msg"],
                "traceback_lines": [],
            }
        elif current is not None and _is_continuation(line):
            current["traceback_lines"].append(line)
    if current is not None:
        yield current


def _read_log_file(path: Path) -> Iterator[str]:
    """Yield raw lines from a regular or gzip-compressed log file."""
    if path.suffix == ".gz":
        with gzip.open(path, "rt", errors="replace") as f:
            yield from f
    else:
        with open(path, errors="replace") as f:
            yield from f


# ── Source iterators ─────────────────────────────────────────────────────────

def iter_python_logs(days: int) -> Iterator[dict]:
    """Yield ERROR/CRITICAL/WARNING records from atlas.log and rotations.

    Rotations matched by glob: atlas.log, atlas.log.1, atlas.log.2, ...,
    atlas.log-YYYYMMDD.gz (TimedRotatingFileHandler pattern), etc.
    """
    cutoff = datetime.utcnow() - timedelta(days=days)
    log_dir: Path = _LOG_DIR if _LOG_DIR is not None else PROJECT_ROOT / "logs"
    if not log_dir.exists():
        logger.warning("Log directory not found: %s", log_dir)
        return
    # ascending so older records come first (chronological order)
    files = sorted(log_dir.glob("atlas.log*"))
    for f in files:
        for rec in _parse_log_lines(_read_log_file(f)):
            try:
                rec_ts = datetime.strptime(rec["ts"], "%Y-%m-%d %H:%M:%S")
            except ValueError:
                continue
            if rec_ts < cutoff:
                continue
            yield {
                "ts": rec["ts"].replace(" ", "T"),
                "level": rec["level"],
                "logger_name": rec["logger_name"],
                "message": rec["message"],
                "traceback": "\n".join(rec["traceback_lines"]) or None,
                "source": "backfill",
            }


def iter_journald(days: int) -> Iterator[dict]:
    """Yield ERROR/CRITICAL records from journald for atlas-* units.

    Best-effort — if journald is not available or returns no records the
    function logs a warning and yields nothing without raising.
    """
    try:
        out = subprocess.check_output(
            [
                "journalctl",
                "-u", "atlas-*",
                f"--since={days} days ago",
                "--output=json",
                "--no-pager",
            ],
            stderr=subprocess.DEVNULL,
            timeout=120,
        ).decode("utf-8", errors="replace")
    except (
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
        FileNotFoundError,
        OSError,
    ) as e:
        logger.warning("journalctl unavailable or empty: %s", e)
        return

    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            j = json.loads(line)
        except Exception:
            continue
        msg = j.get("MESSAGE", "")
        # syslog priority: 0=emerg 1=alert 2=crit 3=err 4=warning 5=notice 6=info 7=debug
        try:
            priority = int(j.get("PRIORITY", 6))
        except (ValueError, TypeError):
            priority = 6
        if priority > 3:
            # only syslog error (3) and more severe
            continue
        ts_us_raw = j.get("__REALTIME_TIMESTAMP", 0) or 0
        try:
            ts_us = int(ts_us_raw)
        except (ValueError, TypeError):
            continue
        if ts_us == 0:
            continue
        ts = datetime.utcfromtimestamp(ts_us / 1_000_000).strftime("%Y-%m-%dT%H:%M:%S")
        unit = j.get("_SYSTEMD_UNIT", "")
        yield {
            "ts": ts,
            "level": "CRITICAL" if priority < 3 else "ERROR",
            "logger_name": j.get("SYSLOG_IDENTIFIER", unit),
            "service": unit.replace(".service", ""),
            "message": msg[:8000],
            "traceback": None,
            "source": "backfill",
        }


def iter_system_log(conn, days: int) -> Iterator[dict]:
    """Yield ERROR/CRITICAL records from the existing system_log telemetry table."""
    cutoff = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S")
    if not conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='system_log'"
    ).fetchone():
        return
    rows = conn.execute(
        "SELECT timestamp, service, level, message FROM system_log "
        "WHERE level IN ('error','critical') AND timestamp >= ? ORDER BY timestamp ASC",
        (cutoff,),
    ).fetchall()
    for r in rows:
        yield {
            "ts": r["timestamp"],
            "level": r["level"].upper(),
            "logger_name": r["service"],
            "service": r["service"],
            "message": (r["message"] or "")[:8000],
            "traceback": None,
            "source": "backfill",
        }


# ── Idempotent upsert ────────────────────────────────────────────────────────

def upsert_record(
    conn,
    *,
    ts: str,
    level: str,
    logger_name: str | None,
    message: str,
    traceback: str | None,
    source: str,
    service: str | None = None,
    exc_type: str | None = None,
    exc_message: str | None = None,
    file_path: str | None = None,
    line_number: int | None = None,
) -> str:
    """Insert or bump a record in the errors table.

    Returns:
        'inserted' — new row created
        'bumped'   — existing row's occurrence_count incremented
        'skipped'  — record filtered (WARNING level or yfinance logger)
    """
    # Only ERROR and CRITICAL enter the errors table
    if level not in ("ERROR", "CRITICAL"):
        return "skipped"
    # yfinance logs routine data failures at ERROR — not operator-actionable
    if logger_name and logger_name.startswith("yfinance"):
        return "skipped"

    fp = compute_fingerprint(exc_type, message, file_path, line_number)
    row = conn.execute(
        "SELECT id, occurrence_count FROM errors WHERE fingerprint = ?", (fp,)
    ).fetchone()
    if row:
        conn.execute(
            "UPDATE errors"
            " SET occurrence_count = occurrence_count + 1,"
            "     last_seen_ts = MAX(last_seen_ts, ?)"
            " WHERE id = ?",
            (ts, row["id"]),
        )
        return "bumped"

    conn.execute(
        """INSERT INTO errors (
            fingerprint, first_seen_ts, last_seen_ts, occurrence_count, ts,
            source, service, level, logger_name, message,
            exc_type, traceback, file_path, line_number,
            classification, tier, remediation_status
        ) VALUES (?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'UNCLASSIFIED', 99, 'NEW')""",
        (
            fp,
            ts,
            ts,
            ts,
            source,
            service,
            level,
            logger_name,
            (message or "")[:8000],
            exc_type,
            (traceback[:8000] if traceback else None),
            file_path,
            line_number,
        ),
    )
    return "inserted"


# ── Entry point ──────────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    """Run the backfill. Returns 0 on success, 2 if errors table missing."""
    p = argparse.ArgumentParser(
        description="Backfill errors table from historical logs (idempotent)."
    )
    p.add_argument(
        "--apply",
        action="store_true",
        help="Perform writes (default: dry-run — prints what would be processed).",
    )
    p.add_argument(
        "--days",
        type=int,
        default=30,
        help="How many days back to scan (default 30).",
    )
    p.add_argument(
        "--source",
        choices=["python_logs", "journald", "system_log", "all"],
        default="all",
        help="Which source(s) to process (default: all).",
    )
    p.add_argument(
        "--db",
        default=None,
        help="Override DB path (useful for tests).",
    )
    p.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Stop after processing N total records (for smoke testing).",
    )
    args = p.parse_args(argv)

    counts: dict[str, int] = {"inserted": 0, "bumped": 0, "skipped": 0}
    sample: list[tuple[str, str, str, str]] = []

    with atlas_db.get_db(args.db) as conn:
        # Gate: migration must have run first
        if not conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='errors'"
        ).fetchone():
            logger.error("errors table does not exist — run migration first")
            return 2

        # Build the ordered list of (source_name, iterator) pairs
        sources: list[tuple[str, Iterator[dict]]] = []
        if args.source in ("all", "python_logs"):
            sources.append(("python_logs", iter_python_logs(args.days)))
        if args.source in ("all", "journald"):
            sources.append(("journald", iter_journald(args.days)))
        if args.source in ("all", "system_log"):
            sources.append(("system_log", iter_system_log(conn, args.days)))

        n = 0  # total records processed (shared across all sources for --limit)
        for source_name, it in sources:
            for rec in it:
                if args.limit is not None and n >= args.limit:
                    break
                n += 1

                if not args.apply:
                    # Dry-run: only count what would actually be inserted
                    rec_level = rec.get("level", "")
                    rec_logger = rec.get("logger_name") or ""
                    if rec_level in ("ERROR", "CRITICAL") and not rec_logger.startswith("yfinance"):
                        if len(sample) < 20:
                            sample.append((
                                source_name,
                                rec["ts"],
                                rec_level,
                                rec["message"][:80],
                            ))
                        counts["inserted"] += 1
                    else:
                        counts["skipped"] += 1
                    continue

                outcome = upsert_record(conn, **rec)
                counts[outcome] = counts.get(outcome, 0) + 1

    logger.info("Backfill summary: %s", counts)
    if not args.apply:
        logger.info("DRY RUN — first 20 records that would be processed:")
        for s in sample:
            logger.info("  %s | %s | %s | %s", *s)
    return 0


if __name__ == "__main__":
    sys.exit(main())
