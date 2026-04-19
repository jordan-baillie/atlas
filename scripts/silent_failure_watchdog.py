#!/usr/bin/env python3
"""Atlas Silent Failure Watchdog.

Detects services that exit 0 but produce degraded / empty output and sends
Telegram alerts.  Runs hourly via systemd timer.

Checks performed:
  1. atlas-discovery "Papers found: 0" in journald (stuck loop / upstream failure)
  2. atlas-director last heartbeat with low coverage_pct (research matrix stale)
  3. Zero-byte autoresearch log files modified in the last 24 h (LLM loop silent)

This watchdog always exits 0 — it must not itself become a source of paging.
Alerts are side-effects; failures inside checks are logged, never raised.
"""

from __future__ import annotations

import argparse
import logging
import re
import sqlite3
import subprocess
import sys
import time
from datetime import date as _date, datetime, timezone
from pathlib import Path

# ─── Paths ────────────────────────────────────────────────────────────────────

ATLAS_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ATLAS_ROOT / "data" / "atlas.db"
LOGS_DIR = ATLAS_ROOT / "logs"

sys.path.insert(0, str(ATLAS_ROOT))

# ─── Logging ──────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("silent_failure_watchdog")

# ─── Telegram import (soft — missing creds must not block other checks) ───────

try:
    from utils.telegram import send_message as _send_message
    _TELEGRAM_AVAILABLE = True
except Exception as _tg_exc:  # noqa: BLE001
    logger.warning("utils.telegram import failed (%s) — alerts will be printed only", _tg_exc)
    _TELEGRAM_AVAILABLE = False


def _alert(text: str, level: str = "WARNING", dry_run: bool = False) -> None:
    """Send (or print) a Telegram alert, swallowing all send errors."""
    if dry_run:
        print(f"[DRY-RUN | {level}] {text}")
        return
    if not _TELEGRAM_AVAILABLE:
        logger.warning("Would send %s alert: %s", level, text)
        return
    try:
        ok = _send_message(text)
        if ok:
            logger.info("Telegram alert sent (%s)", level)
        else:
            logger.warning("Telegram send_message returned False")
    except Exception as exc:  # noqa: BLE001
        logger.error("Telegram send failed: %s", exc)


# ─────────────────────────────────────────────────────────────────────────────
# Check 1 — atlas-discovery "Papers found: 0"
# ─────────────────────────────────────────────────────────────────────────────

def check_discovery(dry_run: bool = False) -> None:
    """Alert if atlas-discovery logged 'Papers found: 0' in the last 24 h."""
    logger.info("check_discovery: querying journald for atlas-discovery")
    try:
        result = subprocess.run(
            [
                "journalctl",
                "-u", "atlas-discovery",
                "--since", "24 hours ago",
                "--no-pager",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        output = result.stdout.strip()

        if not output:
            # Service did not run in the last 24 h — liveness covered by heartbeat_watchdog
            logger.info("check_discovery: no journal output in last 24h — skipping")
            return

        if "Papers found: 0" in output:
            logger.warning("check_discovery: detected 'Papers found: 0'")
            _alert(
                "🔴 [Atlas] atlas-discovery found 0 papers in last 24h"
                " — possible stuck loop or upstream failure",
                level="CRITICAL",
                dry_run=dry_run,
            )
        else:
            matches = re.findall(r"Papers found:\s*(\d+)", output)
            last_count = matches[-1] if matches else "?"
            logger.info("check_discovery: OK (last 'Papers found: %s')", last_count)

    except subprocess.TimeoutExpired:
        logger.error("check_discovery: journalctl timed out")
    except FileNotFoundError:
        logger.warning("check_discovery: journalctl not available on this host — skipping")
    except Exception as exc:  # noqa: BLE001
        logger.error("check_discovery: unexpected error: %s", exc)


# ─────────────────────────────────────────────────────────────────────────────
# Check 2 — atlas-director low coverage
# ─────────────────────────────────────────────────────────────────────────────

_COVERAGE_THRESHOLD_PCT = 10.0  # alert if below this


def _parse_coverage_pct(detail: str | None) -> float | None:
    """Extract a coverage percentage from a heartbeat detail string.

    Tries JSON first, then regex fallback.
    Returns None if unparseable.
    """
    if not detail:
        return None

    # ── JSON path ────────────────────────────────────────────────────────────
    if detail.lstrip().startswith("{"):
        import json as _json
        try:
            obj = _json.loads(detail)
            for key in ("coverage_pct", "coverage_percent", "coverage"):
                if key in obj:
                    val = obj[key]
                    if isinstance(val, (int, float)):
                        return float(val) if float(val) > 1 else float(val) * 100
        except Exception:  # noqa: BLE001
            pass

    # ── Regex path ────────────────────────────────────────────────────────────
    m = re.search(r"coverage[=:\s]+(\d+)/(\d+)", detail, re.IGNORECASE)
    if m:
        num, den = int(m.group(1)), int(m.group(2))
        if den > 0:
            return num / den * 100

    m = re.search(r"(\d+(?:\.\d+)?)\s*%", detail)
    if m:
        return float(m.group(1))

    return None


def check_director_coverage(dry_run: bool = False) -> None:
    """Alert if the latest atlas-director heartbeat shows low coverage."""
    logger.info("check_director_coverage: querying heartbeats table")
    try:
        if not DB_PATH.exists():
            logger.warning("check_director_coverage: DB not found at %s — skipping", DB_PATH)
            return

        conn = sqlite3.connect(str(DB_PATH))
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute(
            """
            SELECT timestamp, status, detail
            FROM heartbeats
            WHERE service = 'atlas-director'
            ORDER BY timestamp DESC
            LIMIT 1
            """
        )
        row = cur.fetchone()
        conn.close()

        if row is None:
            logger.info("check_director_coverage: no atlas-director heartbeat found — skipping")
            return

        ts_str: str = row["timestamp"]
        detail: str | None = row["detail"]

        try:
            ts = datetime.fromisoformat(ts_str).replace(tzinfo=timezone.utc)
        except ValueError:
            ts = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)

        now_utc = datetime.now(timezone.utc)
        age_hours = (now_utc - ts).total_seconds() / 3600

        if age_hours > 24:
            logger.info(
                "check_director_coverage: last run was %.1f h ago (>24h) — skipping", age_hours
            )
            return

        pct = _parse_coverage_pct(detail)
        if pct is None:
            logger.info(
                "check_director_coverage: could not parse coverage from detail=%r — skipping",
                detail,
            )
            return

        if pct < _COVERAGE_THRESHOLD_PCT:
            logger.warning(
                "check_director_coverage: coverage %.1f%% < %.0f%%", pct, _COVERAGE_THRESHOLD_PCT
            )
            _alert(
                f"⚠️ [Atlas] atlas-director last run coverage {pct:.1f}%"
                f" (<{_COVERAGE_THRESHOLD_PCT:.0f}%) — research matrix stale",
                level="WARNING",
                dry_run=dry_run,
            )
        else:
            logger.info("check_director_coverage: OK (coverage=%.1f%%)", pct)

    except Exception as exc:  # noqa: BLE001
        logger.error("check_director_coverage: unexpected error: %s", exc)


# ─────────────────────────────────────────────────────────────────────────────
# Check 3 — zero-byte autoresearch logs in last 24 h
# ─────────────────────────────────────────────────────────────────────────────


# ─── Autoresearch log helpers ─────────────────────────────────────────────────

_AR_RE = re.compile(r"^autoresearch_(?P<strat>[a-z_]+)_(?P<d>\d{8})\.log$")


def _is_rotation_stub(log_file: Path) -> bool:
    """Return True if *log_file* looks like a logrotate-created empty stub.

    Two independent signals are checked (either is sufficient):
    1. A rotated sibling ``<file>.log-YYYYMMDD`` exists with size > 0 in the
       same directory — the definitive rotation-stub signal.
    2. The date encoded in the filename is not today's local date.  The runner
       names files by the day it starts, so a past-dated zero-byte file is
       either a logrotate stub from a prior day or an older empty run — neither
       is actionable today.
    """
    # Signal 1 — a non-empty rotated sibling exists
    for sib in log_file.parent.glob(log_file.name + "-*"):
        try:
            if sib.stat().st_size > 0:
                return True
        except OSError:
            continue

    # Signal 2 — filename date is not today's local date
    m = _AR_RE.match(log_file.name)
    if m:
        try:
            file_date = _date.fromisoformat(
                f"{m['d'][0:4]}-{m['d'][4:6]}-{m['d'][6:8]}"
            )
            if file_date != _date.today():
                return True
        except ValueError:
            pass

    return False


def check_autoresearch_logs(dry_run: bool = False) -> None:
    """Alert if any autoresearch_*.log file written in the last 24 h is zero bytes."""
    logger.info("check_autoresearch_logs: scanning %s", LOGS_DIR)
    try:
        if not LOGS_DIR.exists():
            logger.warning(
                "check_autoresearch_logs: logs dir not found at %s — skipping", LOGS_DIR
            )
            return

        now_ts = time.time()
        cutoff_ts = now_ts - 86400  # 24 h ago

        zero_byte_files: list[str] = []
        for log_file in LOGS_DIR.glob("autoresearch_*.log"):
            try:
                st = log_file.stat()
            except OSError:
                continue
            if st.st_mtime >= cutoff_ts and st.st_size == 0 and not _is_rotation_stub(log_file):
                zero_byte_files.append(log_file.name)

        if zero_byte_files:
            n = len(zero_byte_files)
            names = ", ".join(sorted(zero_byte_files))
            logger.warning(
                "check_autoresearch_logs: %d zero-byte file(s) in last 24h: %s", n, names
            )
            _alert(
                f"⚠️ [Atlas] {n} zero-byte autoresearch log(s) in last 24h: {names}"
                " — autoresearch parameter-sweep runner produced no output" " (logrotate stubs filtered)",
                level="WARNING",
                dry_run=dry_run,
            )
        else:
            logger.info("check_autoresearch_logs: OK (no zero-byte logs in last 24h)")

    except Exception as exc:  # noqa: BLE001
        logger.error("check_autoresearch_logs: unexpected error: %s", exc)


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Atlas silent-failure watchdog — detect degraded services that exit 0"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be sent to Telegram instead of actually sending",
    )
    args = parser.parse_args()

    if args.dry_run:
        logger.info("Running in DRY-RUN mode — no Telegram messages will be sent")

    # Run all checks independently — one failing must not block the others
    check_discovery(dry_run=args.dry_run)
    check_director_coverage(dry_run=args.dry_run)
    check_autoresearch_logs(dry_run=args.dry_run)

    logger.info("Silent failure watchdog complete")
    return 0


if __name__ == "__main__":
    sys.exit(main())
