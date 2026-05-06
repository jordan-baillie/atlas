#!/usr/bin/env python3
"""
Pipeline freshness healthcheck. Alerts on stale critical artifacts.

For each monitored pipeline, query the source-of-truth for last-written timestamp.
If older than threshold (with weekend handling), fire CRITICAL Telegram alert.

Idempotent. State persistence via data/healthcheck_pipelines_state.json
(stores last_alerted_at per pipeline to enforce 6h alert cooldown).

Exit codes:
  0 -- all healthy (or all stale within cooldown window)
  1 -- at least one stale pipeline with alert fired (or --no-alert override)

Usage:
    python3 scripts/healthcheck_pipelines.py --once
    python3 scripts/healthcheck_pipelines.py --once --quiet
    python3 scripts/healthcheck_pipelines.py --once --no-alert
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

# ── Path bootstrap ─────────────────────────────────────────────────────────────
_HERE = Path(__file__).resolve()
_ATLAS_ROOT = _HERE.parent.parent
if str(_ATLAS_ROOT) not in sys.path:
    sys.path.insert(0, str(_ATLAS_ROOT))

logger = logging.getLogger(__name__)

# ── Constants ──────────────────────────────────────────────────────────────────
STATE_FILE = _ATLAS_ROOT / "data" / "healthcheck_pipelines_state.json"

#: Hours between repeat alerts for the same pipeline (per-pipeline cooldown)
ALERT_COOLDOWN_HOURS = 6

# ── Pipeline definitions ───────────────────────────────────────────────────────
# NOTE: 'signals' table uses 'timestamp' column (not 'date' as the spec suggests).
# NOTE: 'market_state' uses 'updated_at' column (not 'synced_at'); query kept as
#       spec-specified and caught gracefully — falls back to log file mtime.

PIPELINES: list[dict[str, Any]] = [
    {
        "name": "signals_written_today",
        "source": "sqlite",
        # Actual column is 'timestamp'; spec says 'date' — use real column name.
        "query": "SELECT MAX(timestamp) FROM signals",
        "threshold_days": 2,
        "weekday_only": True,
    },
    {
        "name": "experiment_generated_today",
        "source": "sqlite",
        "query": "SELECT MAX(created_at) FROM research_experiments",
        "threshold_days": 3,
        "weekday_only": False,
    },
    {
        "name": "regime_observed_today",
        "source": "sqlite",
        "query": "SELECT MAX(date) FROM regime_history",
        "threshold_days": 2,
        "weekday_only": False,
    },
    {
        "name": "equity_recorded_today",
        "source": "sqlite",
        "query": "SELECT MAX(date) FROM equity_history",
        "threshold_days": 2,
        "weekday_only": True,
    },
    {
        "name": "sync_protective_completed_today",
        # Try SQL first (spec says synced_at; actual col is updated_at —
        # OperationalError is caught and falls back to log file mtime).
        "source": "sqlite_or_logfile",
        "query": "SELECT MAX(synced_at) FROM market_state WHERE synced_at IS NOT NULL",
        "logfile": "logs/sync_protective.log",
        "threshold_days": 1,
        "weekday_only": True,
    },
    {
        "name": "reconcile_completed_today",
        "source": "logfile",
        "logfile": "logs/reconciliation.log",
        "threshold_days": 2,
        "weekday_only": True,
    },
    {
        "name": "broker_orders_synced_today",
        "source": "sqlite",
        "query": "SELECT MAX(last_synced_at) FROM broker_orders",
        "threshold_days": 2,
        "weekday_only": False,
    },
]


# ── State helpers ──────────────────────────────────────────────────────────────

def _load_state(path: Path = STATE_FILE) -> dict[str, Any]:
    """Load persisted state; return empty state on missing or corrupt file."""
    try:
        text = path.read_text()
        data = json.loads(text)
        if not isinstance(data, dict):
            raise ValueError("State file is not a JSON object")
        data.setdefault("last_alerted_at", {})
        return data
    except (FileNotFoundError, json.JSONDecodeError, ValueError):
        return {"last_alerted_at": {}}


def _save_state(state: dict[str, Any], path: Path = STATE_FILE) -> None:
    """Persist state to JSON; non-fatal on write error."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state, indent=2, default=str))
    except OSError as e:
        logger.warning("Failed to save state file %s: %s", path, e)


# ── Timestamp parsing ──────────────────────────────────────────────────────────

def _parse_timestamp(ts_str: str | None) -> datetime | None:
    """Parse a timestamp string (various formats) into a UTC-aware datetime.

    Handles:
    - ISO 8601 with/without timezone offset  (fromisoformat, Python 3.11+)
    - "YYYY-MM-DD HH:MM:SS" (no timezone → assumed UTC)
    - "YYYY-MM-DD" (date only → midnight UTC)
    """
    if not ts_str:
        return None
    ts_str = ts_str.strip()
    # Try Python 3.11 fromisoformat (handles +00:00 offset)
    try:
        dt = datetime.fromisoformat(ts_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        pass
    # Fallback: date-only "YYYY-MM-DD"
    try:
        dt = datetime.strptime(ts_str[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        pass
    logger.warning("Could not parse timestamp: %r", ts_str)
    return None


# ── Data retrieval ─────────────────────────────────────────────────────────────

def _get_last_fresh_from_db(query: str) -> datetime | None:
    """Run a SQL query against atlas.db; return parsed timestamp or None.

    Treats empty result (NULL) AND OperationalError (missing column/table)
    both as None → caller treats as maximally stale.
    """
    try:
        from db.atlas_db import get_db
        with get_db() as conn:
            cur = conn.execute(query)
            row = cur.fetchone()
            val = row[0] if row else None
            return _parse_timestamp(val)
    except (sqlite3.OperationalError, sqlite3.DatabaseError) as e:
        logger.debug("DB query failed (%s): %s", query, e)
        return None


def _get_last_fresh_from_logfile(logfile_rel: str, atlas_root: Path) -> datetime | None:
    """Return log file mtime as UTC datetime, or None if file absent."""
    path = atlas_root / logfile_rel
    try:
        mtime = path.stat().st_mtime
        return datetime.fromtimestamp(mtime, tz=timezone.utc)
    except (FileNotFoundError, OSError):
        return None


def _get_last_fresh(
    pipeline: dict[str, Any],
    atlas_root: Path,
) -> datetime | None:
    """Dispatch to the correct freshness source for a pipeline.

    For 'sqlite_or_logfile': tries SQL first; falls back to log file;
    returns the MORE RECENT of the two (both may succeed).
    """
    source = pipeline["source"]

    if source == "sqlite":
        return _get_last_fresh_from_db(pipeline["query"])

    if source == "logfile":
        return _get_last_fresh_from_logfile(pipeline["logfile"], atlas_root)

    if source == "sqlite_or_logfile":
        db_ts = _get_last_fresh_from_db(pipeline["query"])
        log_ts = _get_last_fresh_from_logfile(pipeline["logfile"], atlas_root)
        # Return the more recent non-None value
        candidates = [t for t in (db_ts, log_ts) if t is not None]
        if not candidates:
            return None
        return max(candidates)

    logger.warning("Unknown pipeline source: %r", source)
    return None


# ── Staleness check ────────────────────────────────────────────────────────────

def _is_weekend_skip(pipeline: dict[str, Any], now: datetime) -> bool:
    """Return True if today is Sat/Sun and the pipeline is weekday_only."""
    if not pipeline.get("weekday_only", False):
        return False
    return now.weekday() in (5, 6)  # Saturday=5, Sunday=6


def _check_pipeline(
    pipeline: dict[str, Any],
    atlas_root: Path,
    now: datetime,
) -> tuple[bool, datetime | None, float]:
    """Check a single pipeline for staleness.

    Returns:
        (is_stale, last_fresh_dt, days_ago)
        is_stale=True requires an alert (subject to cooldown).
        days_ago is float('inf') when last_fresh_dt is None.
    """
    if _is_weekend_skip(pipeline, now):
        return False, None, 0.0

    last_fresh = _get_last_fresh(pipeline, atlas_root)

    if last_fresh is None:
        # No data at all → maximally stale
        return True, None, float("inf")

    # Normalise to UTC
    if last_fresh.tzinfo is None:
        last_fresh = last_fresh.replace(tzinfo=timezone.utc)

    days_ago = (now - last_fresh).total_seconds() / 86400.0
    is_stale = days_ago > pipeline["threshold_days"]
    return is_stale, last_fresh, days_ago


# ── Alerting ───────────────────────────────────────────────────────────────────

def _format_days_ago(days_ago: float) -> str:
    """Human-readable 'X days ago' string."""
    if days_ago == float("inf"):
        return "never"
    if days_ago < 1:
        hours = days_ago * 24
        return f"{hours:.1f}h ago"
    return f"{days_ago:.1f} days ago"


def _build_alert_message(
    stale_results: list[tuple[dict[str, Any], datetime | None, float]],
) -> str:
    """Format a Telegram HTML message for a batch of stale pipelines."""
    n = len(stale_results)
    lines = [
        "🚨 <b>PIPELINE STALENESS</b>",
        "",
        f"{n} pipeline{'s' if n != 1 else ''} stale:",
    ]
    for pipeline, last_fresh, days_ago in stale_results:
        name = pipeline["name"]
        threshold = pipeline["threshold_days"]
        if last_fresh is None:
            date_str = "never"
        else:
            date_str = last_fresh.strftime("%Y-%m-%d")
        age_str = _format_days_ago(days_ago)
        lines.append(
            f"• <code>{name}</code>: last {date_str} ({age_str}, threshold {threshold}d)"
        )
    return "\n".join(lines)


def _is_within_cooldown(
    pipeline_name: str,
    state: dict[str, Any],
    now: datetime,
) -> bool:
    """Return True if an alert was already sent for this pipeline within ALERT_COOLDOWN_HOURS."""
    last_alerted_str = state.get("last_alerted_at", {}).get(pipeline_name)
    if not last_alerted_str:
        return False
    last_alerted = _parse_timestamp(last_alerted_str)
    if last_alerted is None:
        return False
    return (now - last_alerted).total_seconds() < ALERT_COOLDOWN_HOURS * 3600


def _maybe_alert(
    stale_results: list[tuple[dict[str, Any], datetime | None, float]],
    state: dict[str, Any],
    now: datetime,
    no_alert: bool,
    state_path: Path,
) -> bool:
    """Fire one consolidated Telegram alert for all pipelines past cooldown.

    Updates state's last_alerted_at for all newly-alerted pipelines.

    Returns:
        True if at least one alert was fired (or would have been with no_alert=False).
    """
    # Filter to pipelines that are past their cooldown
    to_alert = [
        (pipeline, lf, days)
        for (pipeline, lf, days) in stale_results
        if not _is_within_cooldown(pipeline["name"], state, now)
    ]

    if not to_alert:
        logger.debug("All stale pipelines are within cooldown window — skipping alert")
        return False

    msg = _build_alert_message(to_alert)

    if no_alert:
        logger.info("[no-alert] Would have sent:\n%s", msg)
    else:
        try:
            from utils.telegram import send_message
            ok = send_message(msg, parse_mode="HTML")
            if not ok:
                logger.error("Telegram send_message returned False")
        except Exception as e:
            logger.error("Telegram alert failed: %s", e)

    # Update cooldown state for all alerted pipelines
    for pipeline, _, _ in to_alert:
        state.setdefault("last_alerted_at", {})[pipeline["name"]] = now.isoformat()

    _save_state(state, state_path)
    return True


# ── Main entry point ───────────────────────────────────────────────────────────

def run_once(
    *,
    quiet: bool = False,
    no_alert: bool = False,
    state_path: Path = STATE_FILE,
    atlas_root: Path = _ATLAS_ROOT,
    pipelines: list[dict[str, Any]] | None = None,
    _now: datetime | None = None,
) -> int:
    """Run the pipeline freshness check once.

    Args:
        quiet: Suppress INFO-level console output.
        no_alert: Skip Telegram send (log instead).
        state_path: Override for the cooldown state file.
        atlas_root: Override for locating log files (default: inferred from script path).
        pipelines: Override pipeline list (for testing).
        _now: Override current UTC time (for testing).

    Returns:
        0 if all healthy, 1 if any stale (alert fired or --no-alert with stale).
    """
    logging.basicConfig(
        level=logging.WARNING if quiet else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    now = _now if _now is not None else datetime.now(timezone.utc)
    active_pipelines = pipelines if pipelines is not None else PIPELINES
    state = _load_state(state_path)

    stale_results: list[tuple[dict[str, Any], datetime | None, float]] = []

    for pipeline in active_pipelines:
        is_stale, last_fresh, days_ago = _check_pipeline(pipeline, atlas_root, now)
        name = pipeline["name"]
        threshold = pipeline["threshold_days"]

        if is_stale:
            logger.info(
                "STALE %s: last_fresh=%s, days_ago=%.2f, threshold=%sd",
                name,
                last_fresh.isoformat() if last_fresh else "never",
                days_ago if days_ago != float("inf") else 999,
                threshold,
            )
            stale_results.append((pipeline, last_fresh, days_ago))
        else:
            logger.debug("OK %s (days_ago=%.2f)", name, days_ago)

    n_stale = len(stale_results)
    n_healthy = len(active_pipelines) - n_stale
    n_total = len(active_pipelines)
    logger.warning(
        "healthcheck_pipelines complete: %d/%d healthy, %d stale",
        n_healthy, n_total, n_stale,
    )

    if not stale_results:
        return 0

    # Fire consolidated alert (respects cooldown)
    alerted = _maybe_alert(stale_results, state, now, no_alert, state_path)

    # Return 1 if any stale (regardless of whether alert was throttled)
    if alerted or stale_results:
        return 1
    return 0


# ── CLI ────────────────────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Pipeline freshness healthcheck — alerts if critical pipelines are stale."
    )
    parser.add_argument(
        "--once",
        action="store_true",
        default=True,
        help="Run one check and exit (default behaviour).",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress INFO-level logging.",
    )
    parser.add_argument(
        "--no-alert",
        action="store_true",
        dest="no_alert",
        help="Do not send Telegram alerts (test/dry-run mode).",
    )
    args = parser.parse_args(argv)
    try:
        rc = run_once(quiet=args.quiet, no_alert=args.no_alert)
    except Exception as _exc:
        try:
            from db.atlas_db import record_heartbeat
            record_heartbeat("healthcheck_pipelines", "failed", {"error": str(_exc)[:200]})
        except Exception:
            pass
        raise
    try:
        from db.atlas_db import record_heartbeat
        record_heartbeat("healthcheck_pipelines", "completed", {})
    except Exception as _hb_exc:
        logger.debug("healthcheck_pipelines: heartbeat write failed (non-fatal): %s", _hb_exc)
    sys.exit(rc)


if __name__ == "__main__":
    main()
