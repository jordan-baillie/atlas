"""
scripts/backfill_intraday_5min.py
==================================
Idempotent 5-minute intraday OHLCV backfill via Tiingo IEX /prices endpoint.

Storage: per-ticker Parquet files at data/cache/intraday_5m/{TICKER}.parquet
         Columns: timestamp (UTC DatetimeIndex), open, high, low, close, volume
Checkpoint: data/cache/intraday_5m/_checkpoint.json
            Tracks completed (ticker, YYYY-MM) pairs to enable resume.

Usage
-----
    # Smoke test — single ticker, one week, dry-run
    python3 -m scripts.backfill_intraday_5min --ticker SPY --start 2026-05-12 --end 2026-05-16 --dry-run

    # Single ticker backfill
    python3 -m scripts.backfill_intraday_5min --ticker SPY --start 2024-01-01 --end 2026-05-17

    # Full universe backfill (sp500 — run overnight)
    python3 -m scripts.backfill_intraday_5min --universe sp500 --start 2024-01-01 --end 2026-05-17

    # Force-refresh a ticker/range
    python3 -m scripts.backfill_intraday_5min --ticker AAPL --start 2025-01-01 --end 2025-03-31 --force-refresh

DO NOT RUN FULL BACKFILL without operator approval — see design doc.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import pandas as pd
import requests

# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────
logger = logging.getLogger(__name__)

CACHE_DIR = Path(__file__).parent.parent / "data" / "cache" / "intraday_5m"
CHECKPOINT_FILE = CACHE_DIR / "_checkpoint.json"
SECRETS_PATH = Path.home() / ".atlas-secrets.json"

# Tiingo IEX historical endpoint for intraday bars
_TIINGO_IEX_URL = "https://api.tiingo.com/iex/{ticker}/prices"

# Conservative rate-limiting default: 1 call per 1.5s (~2400/hr — well under
# free-tier 50/hr limit only if TIINGO_CALL_DELAY env var is set to 72).
# For overnight production runs on a paid tier, default 1.5s is fine.
INTER_CALL_DELAY = float(os.environ.get("TIINGO_CALL_DELAY", "1.5"))

# Expected schema columns (order matters for Parquet interoperability)
EXPECTED_COLUMNS = ["open", "high", "low", "close", "volume"]


# ─────────────────────────────────────────────────────────────────────────────
# Secrets / credentials
# ─────────────────────────────────────────────────────────────────────────────

def load_tiingo_token() -> str:
    """Load Tiingo API token from ~/.atlas-secrets.json.

    Raises RuntimeError if not found.
    """
    try:
        secrets = json.loads(SECRETS_PATH.read_text())
        token = secrets.get("TIINGO_API_TOKEN", "")
        if not token:
            raise RuntimeError(
                f"TIINGO_API_TOKEN not set in {SECRETS_PATH}"
            )
        return token
    except FileNotFoundError as e:
        raise RuntimeError(f"Secrets file not found: {SECRETS_PATH}") from e
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Secrets file is invalid JSON: {e}") from e


# ─────────────────────────────────────────────────────────────────────────────
# Checkpoint management
# ─────────────────────────────────────────────────────────────────────────────

def load_checkpoint() -> dict:
    """Load checkpoint JSON: {ticker: {YYYY-MM: 'done', ...}, ...}"""
    if CHECKPOINT_FILE.exists():
        try:
            return json.loads(CHECKPOINT_FILE.read_text())
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("Checkpoint read failed, starting fresh: %s", e)
    return {}


def save_checkpoint(checkpoint: dict) -> None:
    """Persist checkpoint JSON (atomic write via .tmp file)."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = CHECKPOINT_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(checkpoint, indent=2, sort_keys=True))
    tmp.replace(CHECKPOINT_FILE)


def is_month_done(
    checkpoint: dict, ticker: str, month_key: str, force_refresh: bool = False
) -> bool:
    """Return True if (ticker, YYYY-MM) already backfilled and force_refresh=False."""
    if force_refresh:
        return False
    return checkpoint.get(ticker, {}).get(month_key) == "done"


def mark_month_done(checkpoint: dict, ticker: str, month_key: str) -> None:
    """Mark (ticker, YYYY-MM) as completed in the checkpoint dict."""
    checkpoint.setdefault(ticker, {})[month_key] = "done"


# ─────────────────────────────────────────────────────────────────────────────
# Storage helpers
# ─────────────────────────────────────────────────────────────────────────────

def parquet_path(ticker: str) -> Path:
    """Return the parquet file path for a ticker."""
    return CACHE_DIR / f"{ticker}.parquet"


def read_existing(ticker: str) -> Optional[pd.DataFrame]:
    """Read existing parquet for ticker, return None on miss/error."""
    path = parquet_path(ticker)
    if not path.exists():
        return None
    try:
        df = pd.read_parquet(path)
        return df
    except Exception as e:
        logger.warning("Could not read existing parquet for %s: %s", ticker, e)
        return None


def write_parquet(ticker: str, df: pd.DataFrame) -> None:
    """Write DataFrame to parquet (atomic overwrite — immutable-snapshot pattern)."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = parquet_path(ticker)
    tmp = path.with_suffix(".tmp")
    df.to_parquet(tmp, compression="snappy")
    tmp.replace(path)
    logger.debug("Wrote %d rows to %s", len(df), path)


def merge_bars(existing: Optional[pd.DataFrame], new_df: pd.DataFrame) -> pd.DataFrame:
    """Merge new bars into existing, dedup by timestamp, sort ascending."""
    if existing is None or existing.empty:
        return new_df.sort_index()
    combined = pd.concat([existing, new_df])
    combined = combined[~combined.index.duplicated(keep="last")]
    return combined.sort_index()


# ─────────────────────────────────────────────────────────────────────────────
# Tiingo API
# ─────────────────────────────────────────────────────────────────────────────

def fetch_5min_bars(
    ticker: str,
    start_date: str,
    end_date: str,
    token: str,
    session: requests.Session,
) -> pd.DataFrame:
    """Fetch 5-min bars for ticker from Tiingo IEX.

    Args:
        ticker:     US equity ticker (e.g. 'SPY')
        start_date: YYYY-MM-DD (inclusive)
        end_date:   YYYY-MM-DD (inclusive)
        token:      Tiingo API token
        session:    requests.Session with auth headers

    Returns:
        DataFrame with UTC DatetimeIndex 'timestamp' and columns:
        open, high, low, close, volume. Empty DataFrame on error.
    """
    url = _TIINGO_IEX_URL.format(ticker=ticker)
    params = {
        "startDate": start_date,
        "endDate": end_date,
        "resampleFreq": "5min",
        "columns": "open,high,low,close,volume",
        "token": token,
    }
    try:
        resp = session.get(url, params=params, timeout=30)
        if resp.status_code == 404:
            logger.warning("Tiingo 404 for %s — ticker may be delisted", ticker)
            return pd.DataFrame()
        if resp.status_code == 400:
            logger.warning(
                "Tiingo 400 for %s %s->%s: %s", ticker, start_date, end_date, resp.text[:200]
            )
            return pd.DataFrame()
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as e:
        logger.error("Tiingo request failed for %s %s->%s: %s", ticker, start_date, end_date, e)
        return pd.DataFrame()
    except (ValueError, json.JSONDecodeError) as e:
        logger.error("Tiingo JSON parse error for %s: %s", ticker, e)
        return pd.DataFrame()

    if not data:
        logger.debug("Tiingo returned empty data for %s %s->%s", ticker, start_date, end_date)
        return pd.DataFrame()

    rows = []
    for item in data:
        raw_ts = item.get("date", "")
        if not raw_ts:
            continue
        try:
            ts = pd.Timestamp(raw_ts, tz="UTC")
            rows.append({
                "timestamp": ts,
                "open":   float(item.get("open",   0) or 0),
                "high":   float(item.get("high",   0) or 0),
                "low":    float(item.get("low",    0) or 0),
                "close":  float(item.get("close",  0) or 0),
                "volume": int(float(item.get("volume", 0) or 0)),
            })
        except (ValueError, TypeError) as e:
            logger.debug("Skipping malformed bar for %s: %s — %s", ticker, raw_ts, e)

    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows).set_index("timestamp")
    df.index.name = "timestamp"
    df = df.sort_index()

    # Validate: reject bars with zero prices
    valid = (df["close"] > 0) & (df["open"] > 0)
    n_invalid = (~valid).sum()
    if n_invalid:
        logger.warning("Dropping %d zero-price bars for %s", n_invalid, ticker)
        df = df[valid]

    logger.info(
        "Tiingo 5m: %s [%s -> %s] -> %d bars",
        ticker, start_date, end_date, len(df),
    )
    return df


# ─────────────────────────────────────────────────────────────────────────────
# Month chunk generator
# ─────────────────────────────────────────────────────────────────────────────

def iter_monthly_windows(
    start: datetime, end: datetime
) -> list[tuple[str, str, str]]:
    """Generate (month_key, window_start, window_end) tuples.

    Splits [start, end] into calendar-month windows.

    Returns:
        List of (YYYY-MM, start_str, end_str) tuples.
    """
    windows = []
    cursor = start.replace(day=1)
    while cursor <= end:
        month_key = cursor.strftime("%Y-%m")
        window_start = max(cursor, start)
        # Last day of month
        if cursor.month == 12:
            month_end = cursor.replace(year=cursor.year + 1, month=1, day=1) - timedelta(days=1)
        else:
            month_end = cursor.replace(month=cursor.month + 1, day=1) - timedelta(days=1)
        window_end = min(month_end, end)
        windows.append((
            month_key,
            window_start.strftime("%Y-%m-%d"),
            window_end.strftime("%Y-%m-%d"),
        ))
        # Advance to next month
        if cursor.month == 12:
            cursor = cursor.replace(year=cursor.year + 1, month=1, day=1)
        else:
            cursor = cursor.replace(month=cursor.month + 1, day=1)
    return windows


# ─────────────────────────────────────────────────────────────────────────────
# Single-ticker backfill
# ─────────────────────────────────────────────────────────────────────────────

def backfill_ticker(
    ticker: str,
    start_date: str,
    end_date: str,
    token: str,
    session: requests.Session,
    checkpoint: dict,
    dry_run: bool = False,
    force_refresh: bool = False,
) -> int:
    """Backfill 5-min bars for a single ticker over a date range.

    Idempotent: skips already-completed (ticker, YYYY-MM) pairs unless
    force_refresh is True.

    Args:
        ticker:        US equity ticker
        start_date:    YYYY-MM-DD (inclusive)
        end_date:      YYYY-MM-DD (inclusive)
        token:         Tiingo API token
        session:       requests.Session
        checkpoint:    Mutable checkpoint dict (updated in-place)
        dry_run:       If True, print planned calls without making them
        force_refresh: If True, ignore checkpoint and re-fetch

    Returns:
        Total new rows fetched and written.
    """
    try:
        start_dt = datetime.strptime(start_date, "%Y-%m-%d")
        end_dt   = datetime.strptime(end_date,   "%Y-%m-%d")
    except ValueError as e:
        logger.error("Invalid date format: %s", e)
        return 0

    windows = iter_monthly_windows(start_dt, end_dt)
    total_new_rows = 0

    for month_key, win_start, win_end in windows:
        if is_month_done(checkpoint, ticker, month_key, force_refresh):
            logger.debug("Skipping %s %s (already done)", ticker, month_key)
            continue

        if dry_run:
            print(
                f"[DRY-RUN] Would fetch: GET /iex/{ticker}/prices"
                f"?startDate={win_start}&endDate={win_end}&resampleFreq=5min"
            )
            continue

        df = fetch_5min_bars(ticker, win_start, win_end, token, session)
        if not df.empty:
            existing = read_existing(ticker)
            merged = merge_bars(existing, df)
            write_parquet(ticker, merged)
            total_new_rows += len(df)

        mark_month_done(checkpoint, ticker, month_key)
        save_checkpoint(checkpoint)

        # Rate-limit delay between calls
        time.sleep(INTER_CALL_DELAY)

    return total_new_rows


# ─────────────────────────────────────────────────────────────────────────────
# Universe helpers
# ─────────────────────────────────────────────────────────────────────────────

def get_universe_tickers(universe: str) -> list[str]:
    """Return tickers for the named universe.

    For sp500 (dynamic), reads filenames from data/cache/sp500/.
    For ETF universes, reads from universe.definitions.
    """
    if universe == "sp500":
        sp500_cache = Path(__file__).parent.parent / "data" / "cache" / "sp500"
        if sp500_cache.exists():
            tickers = sorted(f.stem for f in sp500_cache.glob("*.parquet"))
            logger.info("sp500 tickers from cache: %d", len(tickers))
            return tickers
        logger.warning("sp500 cache dir not found, returning empty list")
        return []
    try:
        from universe.definitions import get_universe_tickers as _get
        return _get(universe)
    except Exception as e:
        logger.error("Could not load universe '%s': %s", universe, e)
        return []


# ─────────────────────────────────────────────────────────────────────────────
# Main entry point
# ─────────────────────────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Returns exit code."""
    parser = argparse.ArgumentParser(
        description="Idempotent 5-minute intraday OHLCV backfill via Tiingo IEX"
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--ticker", metavar="TICKER",
                       help="Single ticker (e.g. SPY)")
    group.add_argument("--universe", metavar="UNIVERSE",
                       choices=["sp500", "commodity_etfs", "sector_etfs"],
                       help="Backfill an entire universe")

    parser.add_argument("--start", required=True, metavar="YYYY-MM-DD",
                        help="Start date (inclusive)")
    parser.add_argument("--end", required=True, metavar="YYYY-MM-DD",
                        help="End date (inclusive)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print planned API calls without executing them")
    parser.add_argument("--force-refresh", action="store_true",
                        help="Re-fetch even if checkpoint says already done")

    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%SZ",
    )

    t0 = time.monotonic()

    # Load credentials
    try:
        token = load_tiingo_token()
    except RuntimeError as e:
        logger.error("%s", e)
        return 1

    # Resolve tickers
    if args.ticker:
        tickers = [args.ticker.upper()]
    else:
        tickers = get_universe_tickers(args.universe)
        if not tickers:
            logger.error("No tickers found for universe '%s'", args.universe)
            return 1

    logger.info(
        "Backfill config: %d tickers | %s -> %s | dry_run=%s | force_refresh=%s",
        len(tickers), args.start, args.end, args.dry_run, args.force_refresh,
    )

    if args.dry_run:
        print(f"\n[DRY-RUN] Would backfill {len(tickers)} tickers ({args.start} -> {args.end})")
        print(f"[DRY-RUN] Storage: {CACHE_DIR}")
        print(f"[DRY-RUN] Checkpoint: {CHECKPOINT_FILE}")
        print()

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    checkpoint = load_checkpoint()

    session = requests.Session()
    session.headers.update({
        "Content-Type": "application/json",
        "Authorization": f"Token {token}",
    })

    total_rows = 0
    errors: list[str] = []

    for i, ticker in enumerate(tickers, 1):
        logger.info("Processing %s (%d/%d)", ticker, i, len(tickers))
        try:
            rows = backfill_ticker(
                ticker=ticker,
                start_date=args.start,
                end_date=args.end,
                token=token,
                session=session,
                checkpoint=checkpoint,
                dry_run=args.dry_run,
                force_refresh=args.force_refresh,
            )
            total_rows += rows
        except Exception as e:
            logger.error("Unexpected error for %s: %s", ticker, e)
            errors.append(ticker)

    elapsed = time.monotonic() - t0

    # Summary
    print()
    print("=" * 60)
    print(f"Backfill complete: {len(tickers)} tickers processed")
    print(f"Total new rows:    {total_rows:,}")
    print(f"Elapsed:           {elapsed:.1f}s ({elapsed/60:.1f} min)")
    if errors:
        print(f"Errors ({len(errors)}):    {', '.join(errors)}")
    print(f"Cache dir:         {CACHE_DIR}")

    # Print sample output for smoke tests (single-ticker runs)
    if not args.dry_run and args.ticker:
        path = parquet_path(args.ticker.upper())
        if path.exists():
            df = pd.read_parquet(path)
            print()
            print(f"Parquet contents for {args.ticker.upper()}:")
            print(f"  Rows:  {len(df):,}")
            print(f"  First: {df.index.min()}")
            print(f"  Last:  {df.index.max()}")
            print(f"  Cols:  {list(df.columns)}")
            print()
            print("Sample (first 3 rows):")
            print(df.head(3).to_string())
            print()
            print("Sample (last 3 rows):")
            print(df.tail(3).to_string())

    return 0 if not errors else 2


if __name__ == "__main__":
    sys.exit(main())
