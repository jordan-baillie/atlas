"""
scripts/refresh_stale_ohlcv.py
================================
Ongoing maintenance script for stale OHLCV data (audit F-11).

Queries tickers in the ohlcv table that have not been updated for more than
STALE_THRESHOLD_DAYS trading days.  For each:

  - If the ticker is already in auto_exclusions → skip (already handled).
  - If the ticker is in a passive universe (mode=passive or live_enabled=False)
    → add to auto_exclusions with reason=passive_universe_no_daily_refresh.
  - Otherwise → attempt a refresh via data.ingest.download_ticker.
      - Success → log the outcome.
      - Failure / empty data → add to auto_exclusions with reason=refresh_failed.

Wire into pi-cron.sh (do not edit pi-cron.sh from here).

Usage:
    python3 scripts/refresh_stale_ohlcv.py
    python3 scripts/refresh_stale_ohlcv.py --threshold 5
    python3 scripts/refresh_stale_ohlcv.py --dry-run
"""

from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
from datetime import date
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent.parent
LOG_DIR = PROJECT_ROOT / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = PROJECT_ROOT / "data" / "atlas.db"

# ---------------------------------------------------------------------------
# Logging — file + stdout
# ---------------------------------------------------------------------------
LOG_FILE = LOG_DIR / "refresh_stale_ohlcv.log"
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
DEFAULT_STALE_THRESHOLD_DAYS = 7


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_excluded_tickers() -> set[str]:
    """Return set of auto-excluded ticker symbols."""
    sys.path.insert(0, str(PROJECT_ROOT))
    from data.auto_exclusions import get_excluded_tickers
    return get_excluded_tickers()


def _get_stale_tickers(
    conn: sqlite3.Connection, threshold: int
) -> list[tuple[str, str, str, float]]:
    """Query ohlcv for tickers stale beyond threshold days.

    Returns list of (ticker, universe, last_date, days_stale).
    """
    rows = conn.execute(
        """
        SELECT
            ticker,
            universe,
            MAX(date)                                         AS last_date,
            ROUND(julianday('now') - julianday(MAX(date)), 1) AS days_stale
        FROM ohlcv
        GROUP BY ticker
        HAVING days_stale > ?
        ORDER BY days_stale DESC
        """,
        (threshold,),
    ).fetchall()
    return [(r[0], r[1], r[2], r[3]) for r in rows]


def _passive_universe(universe: str) -> bool:
    """Return True if the named universe config has mode=passive or live_enabled=False."""
    import json
    cfg_file = PROJECT_ROOT / "config" / "active" / f"{universe}.json"
    if not cfg_file.exists():
        return False
    try:
        cfg = json.loads(cfg_file.read_text())
        trading = cfg.get("trading", {})
        mode = trading.get("mode", "")
        live_enabled = trading.get("live_enabled", True)
        return mode == "passive" or not live_enabled
    except (json.JSONDecodeError, OSError):
        return False


def _attempt_refresh(ticker: str, universe: str, start: str = "2024-01-01") -> bool:
    """Try to refresh a ticker via data.ingest.download_ticker.

    Returns True on success (non-empty data fetched and cached), False otherwise.
    """
    from data.ingest import download_ticker
    try:
        df = download_ticker(ticker, start=start, use_cache=True, market_id=universe)
        if df is not None and not df.empty:
            last = (
                df.index.max().date()
                if hasattr(df.index.max(), "date")
                else str(df.index.max())[:10]
            )
            logger.info(
                "Refreshed %s (%s): %d rows, last=%s", ticker, universe, len(df), last
            )
            return True
        logger.warning("Refresh returned empty data for %s (%s)", ticker, universe)
        return False
    except Exception as exc:
        logger.error("Refresh failed for %s (%s): %s", ticker, universe, exc)
        return False


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def run(
    threshold: int = DEFAULT_STALE_THRESHOLD_DAYS,
    dry_run: bool = False,
) -> dict[str, list[str]]:
    """Execute the stale-OHLCV refresh/exclusion pass.

    Returns summary dict with keys:
        refreshed, excluded_passive, excluded_failed, skipped_already_excluded.
    """
    sys.path.insert(0, str(PROJECT_ROOT))
    from data.auto_exclusions import add_exclusion

    summary: dict[str, list[str]] = {
        "refreshed": [],
        "excluded_passive": [],
        "excluded_failed": [],
        "skipped_already_excluded": [],
    }

    logger.info(
        "refresh_stale_ohlcv starting — threshold=%d days, dry_run=%s",
        threshold,
        dry_run,
    )

    excluded = _get_excluded_tickers()
    logger.info("Currently %d auto-excluded tickers", len(excluded))

    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    stale = _get_stale_tickers(conn, threshold)
    conn.close()

    logger.info("Found %d stale tickers (>%d days)", len(stale), threshold)

    for ticker, universe, last_date, days_stale in stale:

        if ticker in excluded:
            logger.debug("Skip %s — already in auto_exclusions", ticker)
            summary["skipped_already_excluded"].append(ticker)
            continue

        logger.info(
            "Stale: %s (%s) — last=%s (%.1f days)",
            ticker, universe, last_date, days_stale,
        )

        # ASX tickers (.AX suffix) are passive by design (Moomoo manual holdings)
        if ticker.upper().endswith(".AX") or _passive_universe(universe):
            reason = "passive_universe_no_daily_refresh"
            logger.info("Excluding %s — passive universe (%s)", ticker, universe)
            if not dry_run:
                add_exclusion(
                    ticker=ticker,
                    market_id=universe,
                    reason=reason,
                    last_data_date=last_date,
                )
            summary["excluded_passive"].append(ticker)
            continue

        # Active universe — attempt refresh
        logger.info("Attempting refresh for %s (%s)...", ticker, universe)
        if dry_run:
            logger.info("[DRY-RUN] Would attempt refresh for %s", ticker)
            summary["refreshed"].append(ticker)
            continue

        success = _attempt_refresh(ticker, universe)
        if success:
            summary["refreshed"].append(ticker)
        else:
            reason = f"refresh_failed_{date.today().isoformat()}"
            logger.warning(
                "Refresh failed — adding %s to auto_exclusions (reason=%s)",
                ticker, reason,
            )
            add_exclusion(
                ticker=ticker,
                market_id=universe,
                reason=reason,
                last_data_date=last_date,
            )
            summary["excluded_failed"].append(ticker)

    logger.info("=== refresh_stale_ohlcv complete ===")
    logger.info("  Refreshed:                 %s", summary["refreshed"] or "none")
    logger.info("  Excluded (passive):        %s", summary["excluded_passive"] or "none")
    logger.info("  Excluded (refresh_failed): %s", summary["excluded_failed"] or "none")
    logger.info(
        "  Skipped (already excl.):   %s", summary["skipped_already_excluded"] or "none"
    )
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Refresh or exclude stale OHLCV tickers (audit F-11 maintenance)"
    )
    parser.add_argument(
        "--threshold",
        type=int,
        default=DEFAULT_STALE_THRESHOLD_DAYS,
        metavar="DAYS",
        help=f"Days-stale threshold (default: {DEFAULT_STALE_THRESHOLD_DAYS})",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would happen without making any changes",
    )
    args = parser.parse_args()

    summary = run(threshold=args.threshold, dry_run=args.dry_run)

    failed = len(summary["excluded_failed"])
    if failed:
        logger.warning(
            "%d ticker(s) could not be refreshed and were added to auto_exclusions.",
            failed,
        )
        sys.exit(1)

    logger.info("Done. All stale tickers are refreshed or excluded.")
    sys.exit(0)


if __name__ == "__main__":
    main()
