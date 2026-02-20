"""
Atlas-ASX Data Ingestion Module
================================
Download, cache, and manage ASX OHLCV data via yfinance.

Features:
    - Single ticker and batch universe downloads
    - Parquet-based caching with freshness checks (< 1 day)
    - Incremental updates (only fetch missing dates)
    - Hardcoded list of 150+ liquid ASX tickers

Usage:
    from data.ingest import download_ticker, download_universe, get_asx200_tickers
"""

import logging
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Union

import numpy as np
import pandas as pd
import yfinance as yf

logger = logging.getLogger(__name__)

# Project paths
PROJECT_ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = PROJECT_ROOT / "data" / "cache"

# Ensure cache directory exists
CACHE_DIR.mkdir(parents=True, exist_ok=True)

# Cache freshness threshold
CACHE_MAX_AGE_HOURS = 24


# ---------------------------------------------------------------------------
# ASX 200 Ticker List (hardcoded for reliability)
# ---------------------------------------------------------------------------

def get_asx200_tickers() -> List[str]:
    """Return a hardcoded list of well-known liquid ASX tickers.

    This list contains 180+ of the most liquid ASX-listed companies,
    covering all major GICS sectors. Tickers include the .AX suffix
    required by yfinance.

    Returns:
        List of ticker strings (e.g., ['BHP.AX', 'CBA.AX', ...]).
    """
    tickers = [
        # Financials
        "CBA", "NAB", "WBC", "ANZ", "MQG", "SUN", "IAG", "QBE", "BEN",
        "BOQ", "AMP", "PPT", "HUB", "NWL", "CGF", "IFL", "PNI", "JHG",
        "ASX", "MPL", "TYR", "PDN", "GQG", "INR", "NHF",

        # Materials / Mining
        "BHP", "RIO", "FMG", "MIN", "S32", "NCM", "NST", "EVN", "GOR",
        "SFR", "OZL", "IGO", "LYC", "ILU", "AWC", "BSL", "JHX", "AMC",
        "ORA", "BLD", "ABC", "SGM", "WHC", "NHC", "CRN", "PLS", "LTR",
        "PIQ", "DEG", "CMM", "RRL", "STO", "WDS", "RED", "SLR", "WAF",
        "NIC", "TIE", "AGI", "BGL", "NMT", "AIS", "MGX",

        # Healthcare
        "CSL", "COH", "RMD", "SHL", "FPH", "PME", "PRU", "ANN", "EBO",
        "NAN", "IMU", "PNV", "TLX", "NXS", "MSB", "NEU", "SDR", "MVP",

        # Consumer Discretionary
        "WES", "HVN", "JBH", "SUL", "PMV", "LOV", "BRG", "ADH", "NCK",
        "KGN", "TPW", "WEB", "FLT", "ALL", "TAH", "SGR", "SLC", "ARB",
        "PWR", "CAR", "REA", "DHG", "SEK", "IEL", "DSK", "AX1", "BBN",
        "CTT", "EVT", "HMC", "GWA",

        # Consumer Staples
        "WOW", "COL", "TWE", "A2M", "ING", "GNC", "CGC", "BGA", "ELD",
        "CCL", "TGR", "HUO", "BAL",

        # Industrials
        "TCL", "SYD", "BXB", "QAN", "AZJ", "DOW", "SVW", "NWH", "CIM",
        "IPL", "DRR", "QUB", "ALQ", "WOR", "SSM", "MND", "VNT", "AIA",
        "REH", "IFM", "AUB", "NWS", "BKW", "GNG", "ACF", "CVL",

        # Information Technology
        "XRO", "WTC", "CPU", "TNE", "ALU", "MP1", "NXT", "APX", "TYR",
        "DTC", "FCL", "LNK", "IRE", "AD8", "SQ2", "PME", "TLG", "DGL",
        "EML", "UBN", "OFX", "PPH", "DDR",

        # Communication Services
        "TLS", "TPG", "REA", "CAR", "NWS", "SWM", "OML", "NEC", "UNI",

        # Energy
        "WDS", "STO", "ORG", "APA", "VEA", "KAR", "BPT", "WHC", "NHC",
        "STX", "COE", "CVN", "NRG", "WGR",

        # Real Estate (REITs)
        "GMG", "SCG", "VCX", "MGR", "GPT", "SGP", "DXS", "CHC", "CLW",
        "BWP", "CIP", "ABP", "CQR", "NSR", "HMC", "LLC", "CNI", "ARF",
        "HDN", "GOZ", "GDG",

        # Utilities
        "AGL", "ORG", "APA", "SKI", "AST", "MCY",

        # Additional large/mid caps
        "EDV", "RHC", "ORI", "CTD", "IVC", "GUD", "BAP", "APE",
        "SDF", "NUF", "DMP", "CWY", "SKC", "PDN", "BOE", "ERA",
        "TLC", "PXA", "AVZ", "LKE", "VUL", "SYA", "AGY",
        "29M", "LPD", "AKE", "CXO", "GL1",
        "SHL", "AMI", "PBH", "CAJ", "THL",
    ]

    # Deduplicate and add .AX suffix
    seen = set()
    result = []
    for t in tickers:
        t_upper = t.upper().strip()
        if t_upper not in seen:
            seen.add(t_upper)
            result.append(f"{t_upper}.AX")

    logger.info(f"ASX ticker universe: {len(result)} tickers")
    return result


# ---------------------------------------------------------------------------
# Cache Management
# ---------------------------------------------------------------------------

def _cache_path(ticker: str) -> Path:
    """Return the parquet cache file path for a ticker."""
    safe_name = ticker.replace(".", "_").upper()
    return CACHE_DIR / f"{safe_name}.parquet"


def _cache_is_fresh(path: Path, max_age_hours: int = CACHE_MAX_AGE_HOURS) -> bool:
    """Check if a cache file exists and is younger than max_age_hours."""
    if not path.exists():
        return False
    age = datetime.now() - datetime.fromtimestamp(path.stat().st_mtime)
    return age < timedelta(hours=max_age_hours)


def _load_cache(ticker: str) -> Optional[pd.DataFrame]:
    """Load cached data for a ticker if fresh.

    Returns:
        DataFrame if cache is fresh, None otherwise.
    """
    path = _cache_path(ticker)
    if _cache_is_fresh(path):
        try:
            df = pd.read_parquet(path)
            logger.debug(f"Cache hit for {ticker}: {len(df)} rows")
            return df
        except Exception as e:
            logger.warning(f"Cache read error for {ticker}: {e}")
    return None


def _save_cache(ticker: str, df: pd.DataFrame) -> None:
    """Save DataFrame to parquet cache."""
    if df.empty:
        return
    path = _cache_path(ticker)
    try:
        df.to_parquet(path, engine="pyarrow")
        logger.debug(f"Cached {ticker}: {len(df)} rows -> {path}")
    except Exception as e:
        logger.warning(f"Cache write error for {ticker}: {e}")


# ---------------------------------------------------------------------------
# Data Download
# ---------------------------------------------------------------------------

def _normalize_ticker(ticker: str) -> str:
    """Ensure ticker has .AX suffix."""
    ticker = ticker.upper().strip()
    if not ticker.endswith(".AX"):
        ticker = f"{ticker}.AX"
    return ticker


def _clean_ohlcv(df: pd.DataFrame, ticker: str) -> pd.DataFrame:
    """Clean and standardize OHLCV DataFrame from yfinance.

    Ensures consistent column names, drops NaN rows, and sorts by date.
    """
    if df.empty:
        return df

    # yfinance may return MultiIndex columns for single ticker
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)

    # Standardize column names
    col_map = {
        "Open": "open",
        "High": "high",
        "Low": "low",
        "Close": "close",
        "Adj Close": "adj_close",
        "Volume": "volume",
    }
    df = df.rename(columns=col_map)

    # Ensure expected columns exist
    expected = ["open", "high", "low", "close", "volume"]
    for col in expected:
        if col not in df.columns:
            logger.warning(f"{ticker}: missing column '{col}'")
            df[col] = np.nan

    # Add adj_close if missing (use close)
    if "adj_close" not in df.columns:
        df["adj_close"] = df["close"]

    # Add ticker column
    df["ticker"] = ticker

    # Sort by date, drop full-NaN rows
    df = df.sort_index()
    df = df.dropna(subset=["close"])

    # Ensure index is DatetimeIndex named 'date'
    if not isinstance(df.index, pd.DatetimeIndex):
        df.index = pd.to_datetime(df.index)
    df.index.name = "date"

    # Remove timezone info if present
    if df.index.tz is not None:
        df.index = df.index.tz_localize(None)

    return df


def download_ticker(
    ticker: str,
    start: Optional[str] = None,
    end: Optional[str] = None,
    use_cache: bool = True,
) -> pd.DataFrame:
    """Download OHLCV data for a single ASX ticker.

    Supports caching and incremental updates. If cached data exists
    and is fresh, returns it directly. If cached data exists but is
    stale or incomplete, downloads only the missing date range.

    Args:
        ticker: ASX ticker symbol (with or without .AX suffix).
        start: Start date string (default: 3 years ago).
        end: End date string (default: today).
        use_cache: Whether to use parquet cache (default True).

    Returns:
        DataFrame with columns: open, high, low, close, adj_close, volume, ticker.
        Index is DatetimeIndex named 'date'.
    """
    ticker = _normalize_ticker(ticker)

    if end is None:
        end_dt = datetime.now()
    else:
        end_dt = pd.Timestamp(end).to_pydatetime()

    if start is None:
        start_dt = end_dt - timedelta(days=3 * 365)
    else:
        start_dt = pd.Timestamp(start).to_pydatetime()

    start_str = start_dt.strftime("%Y-%m-%d")
    end_str = end_dt.strftime("%Y-%m-%d")

    # Try cache first
    if use_cache:
        cached = _load_cache(ticker)
        if cached is not None and not cached.empty:
            cache_start = cached.index.min()
            cache_end = cached.index.max()

            # Check if cache covers the requested range
            need_before = start_dt.date() < cache_start.date()
            need_after = end_dt.date() > cache_end.date() + timedelta(days=1)

            if not need_before and not need_after:
                # Cache fully covers requested range
                mask = (cached.index >= start_str) & (cached.index <= end_str)
                logger.info(f"{ticker}: served from cache ({len(cached[mask])} rows)")
                return cached[mask]

            # Incremental update: download only missing portions
            frames = [cached]

            if need_before:
                logger.info(f"{ticker}: fetching earlier data {start_str} to {cache_start.strftime('%Y-%m-%d')}")
                try:
                    earlier = yf.download(
                        ticker, start=start_str,
                        end=cache_start.strftime("%Y-%m-%d"),
                        progress=False, auto_adjust=False
                    )
                    if not earlier.empty:
                        earlier = _clean_ohlcv(earlier, ticker)
                        frames.insert(0, earlier)
                except Exception as e:
                    logger.warning(f"{ticker}: failed to fetch earlier data: {e}")

            if need_after:
                fetch_from = (cache_end + timedelta(days=1)).strftime("%Y-%m-%d")
                logger.info(f"{ticker}: fetching newer data {fetch_from} to {end_str}")
                try:
                    later = yf.download(
                        ticker, start=fetch_from, end=end_str,
                        progress=False, auto_adjust=False
                    )
                    if not later.empty:
                        later = _clean_ohlcv(later, ticker)
                        frames.append(later)
                except Exception as e:
                    logger.warning(f"{ticker}: failed to fetch newer data: {e}")

            # Merge and deduplicate
            combined = pd.concat(frames)
            combined = combined[~combined.index.duplicated(keep="last")]
            combined = combined.sort_index()
            _save_cache(ticker, combined)

            mask = (combined.index >= start_str) & (combined.index <= end_str)
            logger.info(f"{ticker}: incremental update complete ({len(combined[mask])} rows)")
            return combined[mask]

    # Full download
    logger.info(f"{ticker}: downloading {start_str} to {end_str}")
    try:
        df = yf.download(ticker, start=start_str, end=end_str,
                         progress=False, auto_adjust=False)
    except Exception as e:
        logger.error(f"{ticker}: download failed: {e}")
        return pd.DataFrame()

    if df.empty:
        logger.warning(f"{ticker}: no data returned")
        return pd.DataFrame()

    df = _clean_ohlcv(df, ticker)

    if use_cache:
        _save_cache(ticker, df)

    logger.info(f"{ticker}: downloaded {len(df)} rows")
    return df


def download_universe(
    tickers: List[str],
    start: Optional[str] = None,
    end: Optional[str] = None,
    use_cache: bool = True,
    delay: float = 0.1,
) -> Dict[str, pd.DataFrame]:
    """Download OHLCV data for multiple ASX tickers.

    Downloads each ticker sequentially with a small delay to avoid
    rate limiting. Uses caching and incremental updates.

    Args:
        tickers: List of ASX ticker symbols.
        start: Start date string (default: 3 years ago).
        end: End date string (default: today).
        use_cache: Whether to use parquet cache (default True).
        delay: Seconds to wait between downloads (default 0.1).

    Returns:
        Dict mapping ticker -> DataFrame.
        Tickers that failed to download are excluded.
    """
    results = {}
    total = len(tickers)
    success = 0
    failed = []

    logger.info(f"Downloading universe: {total} tickers")

    for i, ticker in enumerate(tickers, 1):
        ticker = _normalize_ticker(ticker)
        try:
            df = download_ticker(ticker, start=start, end=end, use_cache=use_cache)
            if not df.empty:
                results[ticker] = df
                success += 1
            else:
                failed.append(ticker)
        except Exception as e:
            logger.error(f"{ticker}: unexpected error: {e}")
            failed.append(ticker)

        if i % 20 == 0:
            logger.info(f"Progress: {i}/{total} ({success} ok, {len(failed)} failed)")

        if delay > 0 and i < total:
            time.sleep(delay)

    logger.info(
        f"Universe download complete: {success}/{total} successful, "
        f"{len(failed)} failed"
    )
    if failed:
        logger.warning(f"Failed tickers: {failed}")

    return results


def clear_cache(ticker: Optional[str] = None) -> int:
    """Clear cached parquet files.

    Args:
        ticker: If provided, clear only this ticker's cache.
                If None, clear all cached files.

    Returns:
        Number of files deleted.
    """
    count = 0
    if ticker:
        path = _cache_path(_normalize_ticker(ticker))
        if path.exists():
            path.unlink()
            count = 1
            logger.info(f"Cleared cache for {ticker}")
    else:
        for f in CACHE_DIR.glob("*.parquet"):
            f.unlink()
            count += 1
        logger.info(f"Cleared {count} cached files")
    return count


def cache_stats() -> Dict:
    """Return statistics about the cache.

    Returns:
        Dict with keys: file_count, total_size_mb, oldest, newest, tickers.
    """
    files = list(CACHE_DIR.glob("*.parquet"))
    if not files:
        return {"file_count": 0, "total_size_mb": 0, "oldest": None,
                "newest": None, "tickers": []}

    sizes = [f.stat().st_size for f in files]
    mtimes = [datetime.fromtimestamp(f.stat().st_mtime) for f in files]
    tickers = [f.stem.replace("_AX", ".AX") for f in files]

    return {
        "file_count": len(files),
        "total_size_mb": round(sum(sizes) / (1024 * 1024), 2),
        "oldest": min(mtimes).isoformat(),
        "newest": max(mtimes).isoformat(),
        "tickers": sorted(tickers),
    }


if __name__ == "__main__":
    # Self-test: download a few well-known ASX tickers
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    print("=== Data Ingest Module Self-Test ===")

    # Test ticker list
    tickers = get_asx200_tickers()
    print(f"\nASX ticker universe: {len(tickers)} tickers")
    print(f"First 10: {tickers[:10]}")
    print(f"Last 10: {tickers[-10:]}")

    # Test single download
    print("\n--- Single Ticker Download (BHP.AX) ---")
    df = download_ticker("BHP", start="2024-01-01", end="2024-06-30")
    if not df.empty:
        print(f"  Rows: {len(df)}")
        print(f"  Columns: {list(df.columns)}")
        print(f"  Date range: {df.index.min()} to {df.index.max()}")
        print(f"  Sample:")
        print(df.head(3).to_string(max_cols=6))
    else:
        print("  WARNING: No data returned (network issue?)")

    # Test cache
    print("\n--- Cache Test (re-download BHP.AX) ---")
    df2 = download_ticker("BHP", start="2024-01-01", end="2024-06-30")
    if not df2.empty:
        print(f"  Rows: {len(df2)} (should match above)")

    # Test universe download (small sample)
    print("\n--- Universe Download (3 tickers) ---")
    universe = download_universe(["CBA", "CSL", "NAB"],
                                  start="2024-06-01", end="2024-06-30")
    for t, d in universe.items():
        print(f"  {t}: {len(d)} rows, {d.index.min().date()} to {d.index.max().date()}")

    # Cache stats
    print(f"\n--- Cache Stats ---")
    stats = cache_stats()
    print(f"  Files: {stats['file_count']}")
    print(f"  Size: {stats['total_size_mb']} MB")

    print("\n=== Data Ingest Module OK ===")
