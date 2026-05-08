"""
backfill_vix.py — Fetch and cache VIX daily OHLCV data.

Usage:
    python -m scripts.backfill_vix           # fetch ~3y of daily VIX history
    python -m scripts.backfill_vix --days 7  # short incremental refresh

Strategy:
    1. Try yfinance (^VIX is a Yahoo index — Tiingo does not carry index symbols).
    2. Normalise to the canonical Atlas parquet schema (lowercase OHLCV columns +
       DatetimeIndex named 'date') matching data/cache/sp500/SPY.parquet.
    3. Write to data/cache/indices/VIX.parquet (directory created if absent).

Exit codes: 0 = success, 1 = failure.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger("backfill_vix")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

INDICES_CACHE = Path(__file__).parent.parent / "data" / "cache" / "indices"
VIX_PARQUET = INDICES_CACHE / "VIX.parquet"
DEFAULT_DAYS = 365 * 3  # ~3 years
VIX_YAHOO_SYMBOL = "^VIX"


# ---------------------------------------------------------------------------
# Fetch helpers
# ---------------------------------------------------------------------------


def _fetch_yfinance(start: date, end: date) -> pd.DataFrame | None:
    """Download VIX daily bars from Yahoo Finance via yfinance."""
    try:
        import yfinance as yf
    except ImportError:
        logger.error("yfinance not installed — cannot fetch VIX")
        return None

    logger.info("Fetching VIX from yfinance (%s → %s) …", start, end)
    try:
        raw = yf.download(
            VIX_YAHOO_SYMBOL,
            start=start.isoformat(),
            end=end.isoformat(),
            auto_adjust=True,
            progress=False,
            multi_level_index=False,  # yfinance ≥ 0.2 — request flat columns
        )
    except TypeError:
        # Older yfinance API without multi_level_index kwarg
        raw = yf.download(
            VIX_YAHOO_SYMBOL,
            start=start.isoformat(),
            end=end.isoformat(),
            auto_adjust=True,
            progress=False,
        )

    if raw is None or raw.empty:
        logger.warning("yfinance returned empty result for %s", VIX_YAHOO_SYMBOL)
        return None

    return raw


# ---------------------------------------------------------------------------
# Normalise
# ---------------------------------------------------------------------------


def _normalise(raw: pd.DataFrame) -> pd.DataFrame:
    """
    Normalise a raw yfinance DataFrame to the Atlas parquet schema:
      - Columns: open, high, low, close, volume  (lowercase)
      - Optional extra: ticker = 'VIX'
      - DatetimeIndex named 'date', UTC-naive
    """
    df = raw.copy()

    # Flatten multi-level columns that yfinance ≥ 0.2 produces:
    # e.g. [('Close', '^VIX'), ('High', '^VIX'), …]
    if isinstance(df.columns, pd.MultiIndex):
        # Drop the ticker level — keep price-type level only
        df.columns = [col[0] if isinstance(col, tuple) else col for col in df.columns]

    # Lower-case all column names
    df.columns = [c.lower() for c in df.columns]

    # Select only OHLCV columns (ignore 'dividends', 'stock splits', etc.)
    keep = [c for c in ("open", "high", "low", "close", "volume") if c in df.columns]
    if not keep:
        raise ValueError(f"No usable OHLCV columns found. Got: {df.columns.tolist()}")
    df = df[keep].copy()

    # Ensure DatetimeIndex named 'date'
    if not isinstance(df.index, pd.DatetimeIndex):
        df.index = pd.to_datetime(df.index)
    df.index.name = "date"
    # Drop timezone (store as UTC-naive to match existing parquets)
    if df.index.tz is not None:
        df.index = df.index.tz_localize(None)

    # Drop rows where all price columns are NaN
    df = df.dropna(subset=["close"])
    df = df.sort_index()

    # Add ticker column (matches SPY.parquet schema)
    df["ticker"] = "VIX"

    return df


# ---------------------------------------------------------------------------
# Public entrypoint
# ---------------------------------------------------------------------------


def backfill(days: int = DEFAULT_DAYS) -> bool:
    """
    Fetch VIX daily history for the last *days* days and write to cache.

    Returns True on success, False on failure.
    """
    end = date.today()
    start = end - timedelta(days=days)

    raw = _fetch_yfinance(start, end)
    if raw is None:
        logger.error("All VIX data sources failed — aborting")
        return False

    try:
        df = _normalise(raw)
    except Exception as exc:
        logger.error("Failed to normalise VIX data: %s", exc)
        return False

    if df.empty:
        logger.error("Normalised VIX DataFrame is empty — nothing to write")
        return False

    # Atomic write: write to tmp then rename
    INDICES_CACHE.mkdir(parents=True, exist_ok=True)
    tmp_path = INDICES_CACHE / f"VIX.tmp.{os.getpid()}.{time.time_ns()}.parquet"
    try:
        df.to_parquet(tmp_path, compression="snappy")
        tmp_path.rename(VIX_PARQUET)
    except Exception as exc:
        logger.error("Failed to write %s: %s", VIX_PARQUET, exc)
        tmp_path.unlink(missing_ok=True)
        return False

    first_date = df.index[0].date() if hasattr(df.index[0], "date") else str(df.index[0])[:10]
    last_date = df.index[-1].date() if hasattr(df.index[-1], "date") else str(df.index[-1])[:10]
    logger.info(
        "Wrote %d rows to %s (first %s → last %s)",
        len(df),
        VIX_PARQUET,
        first_date,
        last_date,
    )
    return True


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Fetch and cache VIX daily OHLCV parquet."
    )
    parser.add_argument(
        "--days",
        type=int,
        default=DEFAULT_DAYS,
        help=f"Number of calendar days to backfill (default: {DEFAULT_DAYS})",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    success = backfill(days=args.days)
    sys.exit(0 if success else 1)
