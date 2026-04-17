#!/usr/bin/env python3
"""Atlas Heartbeat Watchdog.

Queries the heartbeats table every 15 min (via systemd timer) and:
  1. Flips stale running→stalled when timestamp > 4 hours old.
  2. Alerts via Telegram when services are stale (past staleness threshold).

Staleness thresholds:
  - US market hours (14:30–21:00 UTC, weekdays): 2 hours
  - Outside market hours: 6 hours

Quiet services (skipped from alerts):
  - premarket*  — only runs 09:00–10:00 ET (13:00–14:00 UTC), quiet outside that window
"""

import argparse
import logging
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

# ─── Paths ───────────────────────────────────────────────────────────────────

ATLAS_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ATLAS_ROOT / "data" / "atlas.db"
sys.path.insert(0, str(ATLAS_ROOT))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("heartbeat_watchdog")

# ─── Known-quiet service patterns (skip from stale alerts) ──────────────────
# These services only run during narrow windows — don't alert outside them.
QUIET_PREFIXES = (
    "premarket",   # runs ~09:00–10:00 ET = 13:00–14:00 UTC only
    "test",        # test/verify entries, not real services
    "verify",
)


def _is_quiet_service(name: str) -> bool:
    """Return True if the service name matches a known-quiet pattern."""
    lower = name.lower()
    return any(lower.startswith(p) for p in QUIET_PREFIXES)


def _is_market_hours(now_utc: datetime) -> bool:
    """Return True if now falls within US equity market hours (14:30–21:00 UTC, weekdays)."""
    if now_utc.weekday() >= 5:  # Saturday=5, Sunday=6
        return False
    # 14:30 = 870 minutes, 21:00 = 1260 minutes
    minutes_since_midnight = now_utc.hour * 60 + now_utc.minute
    return 870 <= minutes_since_midnight <= 1260


def _hours_ago_sql(hours: int) -> str:
    return f"datetime('now', '-{hours} hours')"


def run_watchdog(dry_run: bool = False) -> None:
    """Main watchdog logic. Exits 0 always (monitor must not hard-fail)."""
    now_utc = datetime.now(timezone.utc)

    if not DB_PATH.exists():
        logger.warning("DB not found at %s — skipping", DB_PATH)
        return

    stale_threshold_hours = 2 if _is_market_hours(now_utc) else 6
    flip_threshold_hours = 4  # auto-flip running→stalled

    try:
        conn = sqlite3.connect(str(DB_PATH))
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        # ── Step 1: Flip stale running→stalled (>4 hours old) ────────────────
        cur.execute("""
            SELECT service, timestamp, status
            FROM heartbeats
            WHERE status = 'running'
              AND timestamp < datetime('now', ?)
        """, (f"-{flip_threshold_hours} hours",))
        to_flip = cur.fetchall()

        flipped_services = []
        if to_flip:
            for row in to_flip:
                cur.execute(
                    "UPDATE heartbeats SET status='stalled' WHERE service=?",
                    (row["service"],)
                )
                flipped_services.append(row["service"])
                logger.info("Flipped %s: running→stalled (ts=%s)", row["service"], row["timestamp"])
            conn.commit()

        # ── Step 2: Find stale services (past threshold) ──────────────────────
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
            WHERE timestamp < datetime('now', ?)
            ORDER BY age_hours DESC
        """, (f"-{stale_threshold_hours} hours",))
        stale_rows = cur.fetchall()
        conn.close()

    except Exception as exc:
        logger.error("DB query failed: %s", exc)
        return  # exit 0 — don't propagate

    # Filter out quiet services
    alert_rows = [r for r in stale_rows if not _is_quiet_service(r["service"])]

    if not alert_rows and not flipped_services:
        logger.info(
            "All services healthy (threshold=%dh, market_hours=%s)",
            stale_threshold_hours,
            _is_market_hours(now_utc),
        )
        return

    # ── Step 3: Build alert message ────────────────────────────────────────
    market_label = "market hours" if _is_market_hours(now_utc) else "off-hours"
    lines = [
        f"⚠️ <b>Atlas Heartbeat Alert</b> ({market_label}, threshold={stale_threshold_hours}h)",
        "",
    ]

    if flipped_services:
        lines.append(f"🔄 <b>Auto-flipped running→stalled ({flip_threshold_hours}h+ silent):</b>")
        for svc in flipped_services:
            lines.append(f"  • <code>{svc}</code>")
        lines.append("")

    if alert_rows:
        lines.append(f"🔴 <b>Stale services ({len(alert_rows)}):</b>")
        for row in alert_rows:
            status_icon = {"stalled": "🟠", "running": "🔵", "ok": "🟢", "completed": "✅"}.get(
                row["status"], "❓"
            )
            lines.append(
                f"  {status_icon} <code>{row['service']}</code> "
                f"— {row['status']} — {row['age_hours']}h ago"
            )

    lines.extend(["", f"<i>Checked: {now_utc.strftime('%Y-%m-%d %H:%M UTC')}</i>"])
    message = "\n".join(lines)

    if dry_run:
        print("=== DRY RUN — would send Telegram: ===")
        print(message)
        return

    # ── Step 4: Send Telegram ─────────────────────────────────────────────
    try:
        from utils.telegram import send_message
        ok = send_message(message)
        if ok:
            logger.info("Alert sent for %d stale service(s)", len(alert_rows))
        else:
            logger.warning("Telegram send returned False (check credentials)")
    except Exception as exc:
        logger.error("Telegram send failed: %s", exc)
        # Still exit 0 — monitor must not fail hard


def main() -> None:
    parser = argparse.ArgumentParser(description="Atlas heartbeat watchdog")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the alert message instead of sending to Telegram",
    )
    args = parser.parse_args()

    try:
        run_watchdog(dry_run=args.dry_run)
    except Exception as exc:
        # Belt-and-suspenders: never exit non-zero
        logger.error("Unexpected error in watchdog: %s", exc, exc_info=True)

    sys.exit(0)


if __name__ == "__main__":
    main()
