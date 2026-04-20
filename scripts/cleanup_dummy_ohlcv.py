#!/usr/bin/env python3
"""
scripts/cleanup_dummy_ohlcv.py — Idempotent repair: remove phantom dummy
OHLCV rows leaked into data/atlas.db by tests, then upsert real parquet
values where available.

Background
----------
Tests in tests/test_auto_exclusions.py (and tests/test_ingest.py) leaked
dummy OHLCV rows into the production SQLite DB via data/ingest.py::_save_cache().
The phantom rows have the pattern: open=0, high=0, low=0, volume=1000,
source='yfinance', universe='sp500', tickers AAPL and MSFT, dates
2026-04-06..2026-04-20 (22 rows total = 11 weekdays × 2 tickers).

What this script does
---------------------
Phase 1 — Count before   : Count dummy rows so we know what existed.
Phase 2 — DELETE          : Remove all matching dummy rows from ohlcv.
Phase 3 — UPSERT parquet  : For rows in parquet cache (2026-04-06..2026-04-16),
                            re-insert real price data (18 rows = 9 per ticker).
                            Dates 2026-04-17 and 2026-04-20 are NOT yet in
                            parquet — they are left absent; the next daily
                            ingest cron will populate them.
Phase 4 — Verification    : Assert 0 dummy rows remain and ≥18 real rows exist.
                            Exits non-zero on failure.

Idempotent
----------
Safe to re-run at any time.  If dummy rows are already gone and real rows
already present, Phases 2 and 3 are no-ops and Phase 4 still passes.
"""

import logging
import sys
from pathlib import Path

import pandas as pd

# Allow running directly from project root or from scripts/ directory.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db.atlas_db import get_db, upsert_ohlcv  # noqa: E402

# ── Constants ─────────────────────────────────────────────────────────────────

DUMMY_TICKERS: tuple[str, ...] = ("AAPL", "MSFT")
PARQUET_START_DATE: str = "2026-04-06"

PARQUET_DIR = Path(__file__).resolve().parent.parent / "data" / "cache" / "sp500"

# Dummy-pattern WHERE clause components (used in both SELECT and DELETE).
_DUMMY_WHERE = (
    "open = 0.0 AND high = 0.0 AND low = 0.0 AND volume = 1000 "
    "AND source = 'yfinance' AND universe = 'sp500' "
    "AND ticker IN ('AAPL', 'MSFT')"
)
_DUMMY_SQL_COUNT = f"SELECT COUNT(*) FROM ohlcv WHERE {_DUMMY_WHERE}"
_DUMMY_SQL_DELETE = f"DELETE FROM ohlcv WHERE {_DUMMY_WHERE}"

# ── Logger ────────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


# ── Phases ────────────────────────────────────────────────────────────────────


def phase1_count_before() -> int:
    """Return how many dummy rows exist before any changes."""
    with get_db() as db:
        row = db.execute(_DUMMY_SQL_COUNT).fetchone()
        count = row[0] if row else 0
    logger.info("Phase 1 — dummy rows before cleanup: %d", count)
    return count


def phase2_delete() -> int:
    """Delete all dummy OHLCV rows. Returns number of rows deleted."""
    with get_db() as db:
        db.execute(_DUMMY_SQL_DELETE)
        deleted = db.execute(
            "SELECT changes()"  # SQLite built-in: rows affected by last DML
        ).fetchone()[0]
    logger.info("Phase 2 — deleted %d dummy rows", deleted)
    return deleted


def phase3_upsert_parquet() -> int:
    """
    Load parquet for each ticker, filter to rows >= PARQUET_START_DATE,
    and upsert real OHLCV values.  Returns total rows upserted across all
    tickers.
    """
    total_upserted = 0

    for ticker in DUMMY_TICKERS:
        parquet_path = PARQUET_DIR / f"{ticker}.parquet"
        if not parquet_path.exists():
            logger.warning(
                "Phase 3 — parquet not found for %s at %s; skipping",
                ticker,
                parquet_path,
            )
            continue

        df = pd.read_parquet(parquet_path)

        # Index is datetime64; compare against string date via normalize.
        mask = df.index >= PARQUET_START_DATE
        subset = df.loc[mask]

        if subset.empty:
            logger.warning(
                "Phase 3 — no rows >= %s in parquet for %s; skipping",
                PARQUET_START_DATE,
                ticker,
            )
            continue

        ticker_upserted = 0
        for ts, row_data in subset.iterrows():
            date_str = ts.strftime("%Y-%m-%d")
            upsert_ohlcv(
                ticker=ticker,
                date=date_str,
                o=float(row_data["open"]),
                h=float(row_data["high"]),
                l=float(row_data["low"]),
                c=float(row_data["close"]),
                adj=None,
                vol=int(row_data["volume"]),
                universe="sp500",
                source="yfinance",
            )
            ticker_upserted += 1

        logger.info(
            "Phase 3 — upserted %d real rows for %s (>= %s)",
            ticker_upserted,
            ticker,
            PARQUET_START_DATE,
        )
        total_upserted += ticker_upserted

    logger.info("Phase 3 — total rows upserted from parquet: %d", total_upserted)
    return total_upserted


def phase4_verify() -> bool:
    """
    Verify that:
      - 0 dummy rows remain in the DB.
      - At least 18 real (open > 0) rows exist for AAPL/MSFT from 2026-04-06.

    Returns True on success; logs ERROR and returns False on failure.
    """
    ok = True

    # ── 4a: dummy count must be 0 ──────────────────────────────────────────
    with get_db() as db:
        dummy_remaining = db.execute(_DUMMY_SQL_COUNT).fetchone()[0]

    if dummy_remaining != 0:
        logger.error(
            "Phase 4 FAIL — %d dummy rows still remain after cleanup",
            dummy_remaining,
        )
        ok = False
    else:
        logger.info("Phase 4 — dummy rows remaining: 0 ✓")

    # ── 4b: inspect real rows ──────────────────────────────────────────────
    query = (
        "SELECT ticker, date, open, volume FROM ohlcv "
        "WHERE ticker IN ('AAPL', 'MSFT') "
        "  AND date >= '2026-04-06' "
        "  AND source = 'yfinance' "
        "  AND universe = 'sp500' "
        "ORDER BY ticker, date"
    )

    real_count = 0
    leftover_dummy_count = 0

    with get_db() as db:
        rows = db.execute(query).fetchall()

    for r in rows:
        is_real = float(r["open"]) > 0
        is_dummy = float(r["open"]) == 0.0 and int(r["volume"]) == 1000
        open_flag = "✓" if is_real else "✗ DUMMY"
        logger.info(
            "  %s  %s  open=%.4f  vol=%d  [%s]",
            r["ticker"],
            r["date"],
            float(r["open"]),
            int(r["volume"]),
            open_flag,
        )
        if is_real:
            real_count += 1
        if is_dummy:
            leftover_dummy_count += 1

    logger.info(
        "Phase 4 — real rows (open>0): %d  |  leftover dummy rows: %d",
        real_count,
        leftover_dummy_count,
    )

    if real_count < 18:
        logger.error(
            "Phase 4 FAIL — expected >= 18 real rows, got %d", real_count
        )
        ok = False
    else:
        logger.info("Phase 4 — real row count %d >= 18 ✓", real_count)

    if leftover_dummy_count > 0:
        logger.error(
            "Phase 4 FAIL — %d leftover dummy rows detected", leftover_dummy_count
        )
        ok = False

    return ok


# ── Entry point ───────────────────────────────────────────────────────────────


def main() -> int:
    logger.info("=== cleanup_dummy_ohlcv.py starting ===")

    phase1_count_before()
    phase2_delete()
    phase3_upsert_parquet()

    success = phase4_verify()

    if success:
        logger.info("=== cleanup_dummy_ohlcv.py DONE — all checks passed ===")
        return 0
    else:
        logger.error("=== cleanup_dummy_ohlcv.py FAILED — see errors above ===")
        return 1


if __name__ == "__main__":
    sys.exit(main())
