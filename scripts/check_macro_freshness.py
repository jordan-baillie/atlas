#!/usr/bin/env python3
"""Check macro indicator freshness and alert when any series goes stale.

Reads the macro_indicators SQLite table and checks when each key column
was last populated with a non-NULL value.  Exits with code 1 and sends a
Telegram alert if any series exceeds its staleness threshold.

Exit codes:
    0  — all series are fresh
    1  — one or more series are stale (or the table is empty / inaccessible)

Usage:
    python3 scripts/check_macro_freshness.py
    python3 scripts/check_macro_freshness.py --quiet   # suppress stdout, still alerts

Cron:
    # Daily at 09:30 UTC (after US pre-market cron runs at ~22:30 AEST / 12:30 UTC)
    30 9 * * * cd /root/atlas && python3 scripts/check_macro_freshness.py >> logs/check_macro_freshness.log 2>&1
"""
from __future__ import annotations

import argparse
import datetime
import logging
import sys
from pathlib import Path
from typing import Optional

# Allow running directly from the repo root without installing the package.
_ATLAS_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ATLAS_ROOT))

logger = logging.getLogger(__name__)

# ── Per-series staleness thresholds ────────────────────────────────────────
# FRED series have varying publication lags:
#   daily series (credit_oas, dxy): 3-7 day lag — threshold 10 days
#   weekly series (unemployment_claims / ICSA): published Thursdays — threshold 14 days
#   monthly series (fed_funds / FEDFUNDS): ~6-week lag — threshold 90 days
#   intraday derived (vix, vix3m, gold, spy): yfinance daily — threshold 7 days
#
# "dxy" specifically: FRED DTWEXBGS publishes with a 5-7 day lag; alert at 14d.
SERIES_THRESHOLDS: dict[str, tuple[str, int]] = {
    # column_name: (human_readable_name, threshold_days)
    "vix":                  ("VIX (yfinance ^VIX)",                    7),
    "credit_oas":           ("Credit OAS (BAMLC0A0CM)",                10),
    "dxy":                  ("DXY / Trade-Weighted USD (DTWEXBGS)",     14),
    "unemployment_claims":  ("Initial Claims / ICSA (FRED weekly)",    14),
    "fed_funds":            ("Fed Funds Rate / FEDFUNDS (monthly)",     90),
    "yield_curve_10y2y":    ("10Y-2Y Yield Curve",                      7),
    "gold":                 ("Gold (yfinance GC=F)",                     7),
    "spy_close":            ("SPY Close (yfinance)",                     7),
}


def check_freshness(
    db_path: Optional[str] = None,
    today: Optional[datetime.date] = None,
) -> tuple[list[tuple[str, str, int]], int]:
    """Check staleness for all tracked series.

    Returns:
        (stale_series, total_rows) where stale_series is a list of
        (human_name, last_date_str, threshold_days).
    """
    from db.atlas_db import get_db

    db_kwargs = {"db_path": db_path} if db_path else {}
    today = today or datetime.date.today()

    stale: list[tuple[str, str, int]] = []
    total_rows = 0

    with get_db(**db_kwargs) as db:
        # Quick count — if the table is empty that is itself a stale signal.
        row = db.execute("SELECT COUNT(*) AS n FROM macro_indicators").fetchone()
        total_rows = row["n"] if row else 0

        if total_rows == 0:
            # Treat empty table as every series missing.
            for col, (name, threshold) in SERIES_THRESHOLDS.items():
                stale.append((name, "NEVER", threshold))
            return stale, total_rows

        for col, (name, threshold) in SERIES_THRESHOLDS.items():
            row = db.execute(
                f"SELECT MAX(date) AS last FROM macro_indicators WHERE {col} IS NOT NULL"
            ).fetchone()
            last_str: Optional[str] = row["last"] if row else None

            if not last_str:
                stale.append((name, "NEVER", threshold))
                continue

            try:
                last_date = datetime.date.fromisoformat(last_str)
            except ValueError:
                stale.append((name, last_str, threshold))
                continue

            age_days = (today - last_date).days
            if age_days > threshold:
                stale.append((name, last_str, threshold))

    return stale, total_rows


def main(argv: list[str] | None = None) -> int:  # noqa: PLR0912
    """Entry point.  Returns exit code."""
    parser = argparse.ArgumentParser(description="Check macro indicator freshness")
    parser.add_argument(
        "--quiet", action="store_true",
        help="Suppress stdout output (Telegram alert still fires on stale)",
    )
    parser.add_argument(
        "--db-path", default=None,
        help="Override SQLite DB path (default: data/atlas.db)",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stderr,
    )

    try:
        stale, total_rows = check_freshness(db_path=args.db_path)
    except Exception as exc:
        logger.error("check_macro_freshness: DB check failed — %s", exc)
        _send_alert(f"⚠️ Macro freshness check FAILED (DB error): {exc}")
        return 1

    today = datetime.date.today()

    if not stale:
        msg = f"✅ All macro inputs fresh (table rows: {total_rows}, checked: {today})"
        if not args.quiet:
            print(msg)
        logger.info(msg)
        return 0

    # Build alert message
    lines = [f"⚠️ Stale macro inputs ({today}):"]
    for name, last_str, threshold in stale:
        lines.append(f"  • {name}: last={last_str} (threshold {threshold}d)")

    if total_rows == 0:
        lines.append("  ⛔ macro_indicators table is EMPTY — macro refresh may have failed")

    alert_msg = "\n".join(lines)

    if not args.quiet:
        print(alert_msg, file=sys.stderr)
    logger.warning(alert_msg)

    _send_alert(alert_msg)
    return 1


def _send_alert(msg: str) -> None:
    """Send Telegram alert — non-fatal if it fails."""
    try:
        from utils.telegram import send_message
        send_message(msg, parse_mode=None)
    except Exception as exc:
        logger.warning("Telegram alert failed: %s", exc)


if __name__ == "__main__":
    sys.exit(main())
