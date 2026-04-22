#!/usr/bin/env python3
"""Data integrity monitor.

Canary for the "ETF cross-universe identical metrics" bug class (P1.1).
Flags when >2 distinct non-sp500 universes within the lookback window share
(strategy, ROUND(sharpe,4), trades) — almost certainly a config/universe leak.

Exit codes:
    0  — clean (no suspicious patterns)
    1  — suspicious patterns detected (cron can alert on non-zero exit)

Expected cron entry (Worker B adds to pi-cron.sh):
    0 */6 * * * /usr/bin/flock -n /tmp/integrity_monitor.lock bash -c \\
        'cd /root/atlas && timeout 2m python3 scripts/data_integrity_monitor.py --notify' \\
        >> /root/atlas/logs/integrity_monitor.log 2>&1
"""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

ATLAS_ROOT = Path(__file__).resolve().parent.parent
if str(ATLAS_ROOT) not in sys.path:
    sys.path.insert(0, str(ATLAS_ROOT))

DB_PATH = ATLAS_ROOT / "data" / "atlas.db"
DEFAULT_WINDOW_HOURS = 24

logger = logging.getLogger(__name__)


# ─── Logging ──────────────────────────────────────────────────────────────────

def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [integrity] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


# ─── Query ────────────────────────────────────────────────────────────────────

def query_suspicious(db_path: Path, window_hours: int) -> list[dict]:
    """Run the integrity canary query.

    Returns rows where >2 distinct non-sp500 universes share the same
    (strategy, ROUND(sharpe,4), trades) within the lookback window.
    """
    query = """
        SELECT strategy,
               ROUND(sharpe, 4) AS s4,
               trades,
               GROUP_CONCAT(DISTINCT universe) AS universes,
               COUNT(*) AS n
        FROM research_experiments
        WHERE created_at >= datetime('now', :window)
          AND universe != 'sp500'
        GROUP BY strategy, ROUND(sharpe, 4), trades
        HAVING COUNT(DISTINCT universe) > 2
        ORDER BY n DESC, strategy
    """
    window_param = f"-{window_hours} hours"

    with sqlite3.connect(str(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.execute(query, {"window": window_param})
        return [dict(r) for r in cursor.fetchall()]


# ─── Output formatting ────────────────────────────────────────────────────────

def _format_report(hits: list[dict], window_hours: int, now: datetime) -> str:
    date_str = now.strftime("%Y-%m-%d")
    lines: list[str] = [
        f"Data integrity monitor — {date_str} ({window_hours}h window)",
        "",
    ]

    if hits:
        lines.append(
            "⚠️ IDENTICAL METRICS across multiple universes (bug canary):"
        )
        for h in hits:
            universes_raw = h.get("universes") or ""
            n = h.get("n") or 0
            universe_list = [u.strip() for u in universes_raw.split(",") if u.strip()]
            lines.append(
                f"  {h['strategy']}: Sharpe={h['s4']}, trades={h['trades']} "
                f"appears in {n} universes: {', '.join(universe_list)}"
            )
        lines.extend([
            "",
            f"Status: {len(hits)} suspicious patterns detected — likely P1.1 regression.",
        ])
    else:
        lines.extend([
            "✅ No suspicious patterns detected.",
            "",
            "Status: clean.",
        ])

    return "\n".join(lines)


# ─── Telegram ─────────────────────────────────────────────────────────────────

def _send_telegram_alert(hits: list[dict], window_hours: int) -> bool:
    """Send Telegram alert for integrity violations."""
    if not hits:
        return True

    lines: list[str] = [
        "🚨 <b>Atlas — Data Integrity Alert</b>",
        "",
        f"{len(hits)} suspicious patterns: identical metrics across ≥3 ETF universes",
        "<i>(P1.1 bug canary: likely config/universe leak in research loop)</i>",
        "",
    ]
    for h in hits[:5]:
        lines.append(
            f"• {h['strategy']}: Sharpe={h['s4']}, trades={h['trades']}"
        )
    if len(hits) > 5:
        lines.append(f"  ... and {len(hits) - 5} more")

    lines.extend([
        "",
        "Run: python3 scripts/data_integrity_monitor.py",
    ])

    message = "\n".join(lines)
    try:
        from utils.telegram import send_message  # noqa: PLC0415
        return send_message(message)
    except Exception as exc:
        logger.error("Telegram send failed: %s", exc)
        return False


# ─── CLI ─────────────────────────────────────────────────────────────────────

def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Data integrity monitor — canary for cross-universe metric leaks.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python3 scripts/data_integrity_monitor.py\n"
            "  python3 scripts/data_integrity_monitor.py --notify\n"
            "  python3 scripts/data_integrity_monitor.py --window-hours 48\n"
        ),
    )
    parser.add_argument("--notify", action="store_true",
                        help="Send Telegram on any hit")
    parser.add_argument(
        "--window-hours",
        type=int,
        default=DEFAULT_WINDOW_HOURS,
        help=f"Lookback window in hours (default: {DEFAULT_WINDOW_HOURS})",
    )
    parser.add_argument(
        "--db",
        default=str(DB_PATH),
        help=f"SQLite DB path (default: {DB_PATH})",
    )
    parser.add_argument("--json", action="store_true", dest="json_output",
                        help="Output JSON instead of plain text")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    _setup_logging()

    db_path = Path(args.db)
    now = datetime.now(timezone.utc)

    if not db_path.exists():
        logger.error("Database not found: %s", db_path)
        return 1

    hits = query_suspicious(db_path, args.window_hours)

    if args.json_output:
        output = {
            "as_of": now.isoformat(),
            "window_hours": args.window_hours,
            "hit_count": len(hits),
            "hits": hits,
        }
        print(json.dumps(output, indent=2))
    else:
        print(_format_report(hits, args.window_hours, now))

    if args.notify and hits:
        sent = _send_telegram_alert(hits, args.window_hours)
        if not sent:
            logger.warning("Telegram alert failed")

    # Exit 1 if any suspicious patterns detected
    return 1 if hits else 0


if __name__ == "__main__":
    sys.exit(main())
