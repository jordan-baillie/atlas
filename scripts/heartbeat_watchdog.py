#!/usr/bin/env python3
"""Atlas Heartbeat Watchdog — schedule-aware.

Queries the heartbeats table every 15 min (via systemd timer) and:
  1. Flips stale running→stalled when timestamp > 4 hours old.
  2. Alerts via Telegram when services miss their scheduled runs.

Staleness is computed per-service using cron schedules from config/heartbeat.json
(all crons expressed in UTC).  Unknown services fall back to a global RTH/off-hours
threshold via utils.market_hours.is_rth() (holiday-aware).

Alert throttling via data/heartbeat_alert_state.json prevents 15-min Telegram spam.
Override the state-file location with the env var ATLAS_HEARTBEAT_STATE_FILE (useful
for tests).
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
from typing import Any, Callable

# ─── Paths ────────────────────────────────────────────────────────────────────

ATLAS_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ATLAS_ROOT / "data" / "atlas.db"
CONFIG_PATH = ATLAS_ROOT / "config" / "heartbeat.json"
DEFAULT_STATE_FILE = ATLAS_ROOT / "data" / "heartbeat_alert_state.json"

sys.path.insert(0, str(ATLAS_ROOT))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("heartbeat_watchdog")

FLIP_THRESHOLD_HOURS = 4


# ─── Config ──────────────────────────────────────────────────────────────────

def _load_config() -> dict[str, Any]:
    """Load heartbeat.json config. Returns safe defaults if missing."""
    try:
        return json.loads(CONFIG_PATH.read_text())
    except Exception as e:
        logger.warning("Could not load heartbeat config (%s) — using defaults", e)
        return {
            "default_threshold_hours": 6,
            "rth_threshold_hours": 2,
            "min_alert_gap_hours": 4,
            "services": {},
            "ignored_prefixes": ["test", "verify"],
        }


# ─── DB helpers (injectable for tests) ───────────────────────────────────────

def load_heartbeats(db_path: Path) -> list[dict[str, Any]]:
    """Load all heartbeat rows from SQLite. Separated so tests can monkey-patch."""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("""
        SELECT
            service,
            timestamp,
            status,
            ROUND(
                (julianday('now') - julianday(timestamp)) * 24.0,
                1
            ) AS age_hours
        FROM heartbeats
        ORDER BY age_hours DESC
    """)
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows


def flip_stalled(db_path: Path) -> list[str]:
    """Flip running→stalled for services silent >4 h. Returns flipped service names."""
    try:
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute(
            """
            SELECT service, timestamp
            FROM heartbeats
            WHERE status = 'running'
              AND timestamp < datetime('now', ?)
            """,
            (f"-{FLIP_THRESHOLD_HOURS} hours",),
        )
        to_flip = cur.fetchall()
        flipped: list[str] = []
        for row in to_flip:
            cur.execute(
                "UPDATE heartbeats SET status='stalled' WHERE service=?",
                (row["service"],),
            )
            flipped.append(row["service"])
            logger.info(
                "Flipped %s: running→stalled (ts=%s)", row["service"], row["timestamp"]
            )
        conn.commit()
        conn.close()
        return flipped
    except Exception as exc:
        logger.error("flip_stalled DB error: %s", exc)
        return []


# ─── Timestamp parsing ────────────────────────────────────────────────────────

def _parse_ts(ts_str: str) -> datetime:
    """Parse a UTC timestamp string (as stored by SQLite 'YYYY-MM-DD HH:MM:SS') to
    an aware UTC datetime."""
    dt = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S")
    return dt.replace(tzinfo=timezone.utc)


# ─── Schedule-aware staleness check ──────────────────────────────────────────

def _is_service_stale(
    last_run_ts: datetime,
    now_utc: datetime,
    service_cfg: dict[str, Any],
) -> tuple[bool, datetime]:
    """Check if a configured service is stale (missed its most recent scheduled run).

    Stale iff:
      1. last_run_ts < (prev_expected − 5 min)  [service missed the most recent run]
      2. now_utc     >= (prev_expected + threshold_hours)  [past the patience window]

    Returns (is_stale, prev_expected_utc).
    """
    from croniter import croniter  # lazy import — not always needed

    cron_expr: str = service_cfg["expected_cron"]
    threshold_hours: float = float(service_cfg.get("threshold_hours", 6))
    grace = timedelta(minutes=5)

    citer = croniter(cron_expr, now_utc)
    prev_expected: datetime = citer.get_prev(datetime)

    # croniter returns an aware datetime when given an aware reference; defensive guard.
    if prev_expected.tzinfo is None:
        prev_expected = prev_expected.replace(tzinfo=timezone.utc)

    missed = last_run_ts < (prev_expected - grace)
    past_patience = now_utc >= (prev_expected + timedelta(hours=threshold_hours))

    return (missed and past_patience), prev_expected


# ─── Alert-throttle state ─────────────────────────────────────────────────────

def _load_alert_state(state_file: Path) -> dict[str, Any]:
    """Load throttle state. Returns {} if file missing or corrupt."""
    try:
        return json.loads(state_file.read_text())
    except Exception:
        return {}


def _save_alert_state(state_file: Path, state: dict[str, Any]) -> None:
    """Atomically persist throttle state."""
    try:
        tmp = state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=2, default=str))
        tmp.replace(state_file)
    except Exception as e:
        logger.warning("Could not save alert state: %s", e)


def _should_alert(
    service: str,
    prev_expected: datetime,
    now_utc: datetime,
    state: dict[str, Any],
    min_gap_hours: float,
) -> bool:
    """Return True if an alert should fire for this service.

    Suppresses if:
      - last alert was < min_gap_hours ago, AND
      - prev_expected hasn't advanced (same missed run).

    Escalates (always alerts) if prev_expected advanced — the NEXT scheduled
    run also went missing.
    """
    if service not in state:
        return True

    entry = state[service]

    try:
        last_alert = datetime.fromisoformat(entry["last_alert_utc"])
        if last_alert.tzinfo is None:
            last_alert = last_alert.replace(tzinfo=timezone.utc)
    except (KeyError, ValueError):
        return True

    try:
        stored_prev = datetime.fromisoformat(entry["prev_expected_utc"])
        if stored_prev.tzinfo is None:
            stored_prev = stored_prev.replace(tzinfo=timezone.utc)
    except (KeyError, ValueError):
        return True

    # Escalate: prev_expected has advanced → new run was also missed
    if prev_expected > stored_prev:
        logger.info(
            "%s: prev_expected advanced %s → %s — escalating alert",
            service,
            stored_prev.isoformat(),
            prev_expected.isoformat(),
        )
        return True

    # Throttle if within gap window
    gap = now_utc - last_alert
    if gap < timedelta(hours=min_gap_hours):
        logger.info(
            "%s: last alert %.1f h ago (min_gap=%.1f h) — suppressing",
            service,
            gap.total_seconds() / 3600,
            min_gap_hours,
        )
        return False

    return True


# ─── Main watchdog logic ──────────────────────────────────────────────────────

def run_watchdog(
    dry_run: bool = False,
    load_heartbeats_fn: Callable[[Path], list[dict[str, Any]]] | None = None,
    flip_fn: Callable[[Path], list[str]] | None = None,
    now_utc: datetime | None = None,
    state_file: Path | None = None,
) -> None:
    """Main watchdog logic.  Always exits 0 — monitor must not hard-fail."""
    if now_utc is None:
        now_utc = datetime.now(timezone.utc)

    # Resolve state-file (env-var override → arg → default)
    if state_file is None:
        env_override = os.environ.get("ATLAS_HEARTBEAT_STATE_FILE")
        state_file = Path(env_override) if env_override else DEFAULT_STATE_FILE

    config = _load_config()
    ignored_prefixes: tuple[str, ...] = tuple(
        p.lower() for p in config.get("ignored_prefixes", ["test", "verify"])
    )
    services_cfg: dict[str, Any] = config.get("services", {})
    default_threshold: float = float(config.get("default_threshold_hours", 6))
    rth_threshold: float = float(config.get("rth_threshold_hours", 2))
    min_alert_gap: float = float(config.get("min_alert_gap_hours", 4))

    if not DB_PATH.exists():
        logger.warning("DB not found at %s — skipping", DB_PATH)
        return

    # ── Step 1: Flip stale running→stalled ───────────────────────────────────
    _flip = flip_fn if flip_fn is not None else flip_stalled
    try:
        flipped_services = _flip(DB_PATH)
    except Exception as exc:
        logger.error("flip step failed: %s", exc)
        flipped_services = []

    # ── Step 2: Load heartbeats ───────────────────────────────────────────────
    _load = load_heartbeats_fn if load_heartbeats_fn is not None else load_heartbeats
    try:
        all_rows = _load(DB_PATH)
    except Exception as exc:
        logger.error("DB query failed: %s", exc)
        return

    # ── Step 3: Evaluate staleness per service ────────────────────────────────
    from utils.market_hours import is_rth  # holiday-aware; replaces deleted _is_market_hours

    alert_rows: list[dict[str, Any]] = []
    prev_expected_map: dict[str, datetime] = {}

    for row in all_rows:
        svc: str = row["service"]

        # Skip ignored prefixes (test/verify/…)
        lower_svc = svc.lower()
        if any(lower_svc.startswith(p) for p in ignored_prefixes):
            continue

        try:
            last_run_ts = _parse_ts(row["timestamp"])
        except (ValueError, TypeError) as e:
            logger.warning("Could not parse timestamp for %s: %s", svc, e)
            continue

        is_stale = False

        if svc in services_cfg:
            # Schedule-aware: use cron to find the most recent expected run
            try:
                is_stale, prev_exp = _is_service_stale(
                    last_run_ts, now_utc, services_cfg[svc]
                )
                prev_expected_map[svc] = prev_exp
            except Exception as e:
                logger.warning("Schedule check failed for %s: %s", svc, e)
        else:
            # Fallback: global RTH / off-hours threshold (holiday-aware via is_rth)
            threshold = rth_threshold if is_rth(now_utc) else default_threshold
            age_hours = (now_utc - last_run_ts).total_seconds() / 3600
            is_stale = age_hours > threshold

        if is_stale:
            alert_rows.append(dict(row))

    # ── Step 4: Apply alert throttling ───────────────────────────────────────
    alert_state = _load_alert_state(state_file)

    final_alerts: list[dict[str, Any]] = []
    for row in alert_rows:
        svc = row["service"]
        prev_exp = prev_expected_map.get(svc)

        if prev_exp is not None:
            # Configured service — full schedule-aware throttle
            if _should_alert(svc, prev_exp, now_utc, alert_state, min_alert_gap):
                final_alerts.append(row)
        else:
            # Fallback service — use the heartbeat's own last_run_ts as the
            # stable prev_expected so the escalation override only fires when
            # the heartbeat is genuinely refreshed (not every cycle).
            try:
                last_run_ts = _parse_ts(row["timestamp"])
            except (ValueError, TypeError, KeyError):
                last_run_ts = now_utc  # defensive fallback
            if _should_alert(svc, last_run_ts, now_utc, alert_state, min_alert_gap):
                final_alerts.append(row)

    if not final_alerts and not flipped_services:
        logger.info("All services healthy (schedule-aware)")
        return

    # ── Step 5: Build alert message ───────────────────────────────────────────
    in_rth = is_rth(now_utc)
    market_label = "market hours" if in_rth else "off-hours"
    lines: list[str] = [
        f"⚠️ <b>Atlas Heartbeat Alert</b> ({market_label}, schedule-aware)",
        "",
    ]

    if flipped_services:
        lines.append(
            f"🔄 <b>Auto-flipped running→stalled ({FLIP_THRESHOLD_HOURS}h+ silent):</b>"
        )
        for svc_name in flipped_services:
            lines.append(f"  • <code>{svc_name}</code>")
        lines.append("")

    if final_alerts:
        lines.append(f"🔴 <b>Stale services ({len(final_alerts)}):</b>")
        for row in final_alerts:
            status_icon = {
                "stalled": "🟠",
                "running": "🔵",
                "ok": "🟢",
                "completed": "✅",
            }.get(row["status"], "❓")
            svc = row["service"]
            age = row.get("age_hours", "?")
            prev_exp = prev_expected_map.get(svc)
            sched_note = (
                f" (expected {prev_exp.strftime('%a %H:%M UTC')})" if prev_exp else ""
            )
            lines.append(
                f"  {status_icon} <code>{svc}</code>"
                f" — {row['status']} — {age}h ago{sched_note}"
            )

    lines += ["", f"<i>Checked: {now_utc.strftime('%Y-%m-%d %H:%M UTC')}</i>"]
    message = "\n".join(lines)

    if dry_run:
        print("=== DRY RUN — would send Telegram: ===")
        print(message)
        return  # do NOT update state during dry run

    # ── Step 6: Send Telegram + update throttle state ─────────────────────────
    try:
        from utils.telegram import send_message  # type: ignore[import]

        ok = send_message(message)
        if ok:
            logger.info("Alert sent for %d stale service(s)", len(final_alerts))
            for row in final_alerts:
                svc = row["service"]
                prev_exp = prev_expected_map.get(svc, now_utc)
                alert_state[svc] = {
                    "last_alert_utc": now_utc.isoformat(),
                    "prev_expected_utc": prev_exp.isoformat(),
                }
            _save_alert_state(state_file, alert_state)
        else:
            logger.warning("Telegram send returned False (check credentials)")
    except Exception as exc:
        logger.error("Telegram send failed: %s", exc)
        # Still exit 0 — monitor must not fail hard


# ─── CLI entry point ──────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Atlas heartbeat watchdog (schedule-aware)")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the alert message instead of sending to Telegram",
    )
    args = parser.parse_args()

    try:
        run_watchdog(dry_run=args.dry_run)
    except Exception as exc:
        logger.error("Unexpected error in watchdog: %s", exc, exc_info=True)

    sys.exit(0)


if __name__ == "__main__":
    main()
