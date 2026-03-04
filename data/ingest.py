"""
Atlas Data Ingestion Module
================================
Download, cache, and manage OHLCV data via yfinance for any market.

Features:
    - Single ticker and batch universe downloads
    - Parquet-based caching with freshness checks (< 1 day)
    - Incremental updates (only fetch missing dates)
    - Per-market cache directories (data/cache/asx/, data/cache/sp500/)
    - Backward compatible: get_asx200_tickers() still works

Usage:
    from data.ingest import download_ticker, download_universe
    from markets import get_market

    market = get_market("asx")
    data = download_universe(market.get_formatted_tickers(), market_id="asx")
"""

import logging
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Union

import numpy as np
import pandas as pd

try:
    import yfinance as yf
    YF_AVAILABLE = True
except ImportError:
    YF_AVAILABLE = False
    logger.warning("yfinance not installed — yfinance fallback unavailable")

logger = logging.getLogger(__name__)

# Project paths
PROJECT_ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = PROJECT_ROOT / "data" / "cache"

# Ensure base cache directory exists
CACHE_DIR.mkdir(parents=True, exist_ok=True)

# Cache freshness threshold
CACHE_MAX_AGE_HOURS = 24

# Default market
DEFAULT_MARKET = "asx"


def _market_cache_dir(market_id: Optional[str] = None) -> Path:
    """Return the cache directory for a market. Creates it if needed."""
    market_id = (market_id or DEFAULT_MARKET).lower().strip()
    d = CACHE_DIR / market_id
    d.mkdir(parents=True, exist_ok=True)
    return d


# ---------------------------------------------------------------------------
# Backward-compatible ticker list (delegates to market profile)
# ---------------------------------------------------------------------------

def get_asx200_tickers() -> List[str]:
    """Return ASX 200 tickers with .AX suffix.

    Backward-compatible wrapper — delegates to the ASX market profile.
    """
    from markets import get_market
    market = get_market("asx")
    tickers = market.get_formatted_tickers()
    logger.info(f"ASX ticker universe: {len(tickers)} tickers")
    return tickers


def get_market_tickers(market_id: str) -> List[str]:
    """Return formatted tickers for any registered market.

    Args:
        market_id: Market identifier (e.g., 'asx', 'sp500').

    Returns:
        List of yfinance-ready ticker strings.
    """
    from markets import get_market
    market = get_market(market_id)
    tickers = market.get_formatted_tickers()
    logger.info(f"{market.display_name} ticker universe: {len(tickers)} tickers")
    return tickers


# ---------------------------------------------------------------------------
# Cache Management
# ---------------------------------------------------------------------------

def _cache_path(ticker: str, market_id: Optional[str] = None) -> Path:
    """Return the parquet cache file path for a ticker."""
    safe_name = ticker.replace(".", "_").upper()
    return _market_cache_dir(market_id) / f"{safe_name}.parquet"


def _cache_is_fresh(path: Path, max_age_hours: int = CACHE_MAX_AGE_HOURS) -> bool:
    """Check if a cache file exists and is younger than max_age_hours."""
    if not path.exists():
        return False
    age = datetime.now() - datetime.fromtimestamp(path.stat().st_mtime)
    return age < timedelta(hours=max_age_hours)


def _load_cache(ticker: str, market_id: Optional[str] = None) -> Optional[pd.DataFrame]:
    """Load cached data for a ticker if fresh."""
    path = _cache_path(ticker, market_id)
    if _cache_is_fresh(path):
        try:
            df = pd.read_parquet(path)
            logger.debug(f"Cache hit for {ticker}: {len(df)} rows")
            return df
        except Exception as e:
            logger.warning(f"Cache read error for {ticker}: {e}")
    return None


def _save_cache(ticker: str, df: pd.DataFrame, market_id: Optional[str] = None) -> None:
    """Save DataFrame to parquet cache.

    Writes to the market-namespaced path only (e.g. data/cache/asx/).
    All readers search subdirs — no root-level duplicates needed.
    """
    if df.empty:
        return
    path = _cache_path(ticker, market_id)
    try:
        # Audit H8: atomic write to prevent corruption from concurrent reads
        tmp_path = path.with_suffix('.parquet.tmp')
        df.to_parquet(tmp_path, engine="pyarrow")
        import os
        os.replace(str(tmp_path), str(path))
        logger.debug(f"Cached {ticker}: {len(df)} rows -> {path}")
    except Exception as e:
        logger.warning(f"Cache write error for {ticker}: {e}")


# ---------------------------------------------------------------------------
# Data Download
# ---------------------------------------------------------------------------

def _normalize_ticker(ticker: str, market_id: Optional[str] = None) -> str:
    """Ensure ticker has the correct market suffix.

    For ASX: adds .AX if missing.
    For SP500: returns as-is (no suffix).
    For other markets: uses the market profile.
    """
    ticker = ticker.upper().strip()

    if market_id:
        from markets import get_market
        market = get_market(market_id)
        return market.format_ticker(ticker)

    # Legacy default: ASX
    if not ticker.endswith(".AX") and "." not in ticker:
        ticker = f"{ticker}.AX"
    return ticker


def _clean_ohlcv(df: pd.DataFrame, ticker: str) -> pd.DataFrame:
    """Clean and standardize OHLCV DataFrame from yfinance."""
    if df.empty:
        return df

    # yfinance may return MultiIndex columns for single ticker
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)

    # Standardize column names
    col_map = {
        "Open": "open", "High": "high", "Low": "low",
        "Close": "close", "Adj Close": "adj_close", "Volume": "volume",
    }
    df = df.rename(columns=col_map)

    # Ensure expected columns exist
    expected = ["open", "high", "low", "close", "volume"]
    for col in expected:
        if col not in df.columns:
            logger.warning(f"{ticker}: missing column '{col}'")
            df[col] = np.nan

    # Add adj_close if missing
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


def _clean_alpaca_bars(df: pd.DataFrame, ticker: str) -> pd.DataFrame:
    """Clean and standardize OHLCV DataFrame from Alpaca ``get_historical_bars()``.

    Alpaca returns lowercase OHLCV columns (open, high, low, close, volume,
    vwap) with a tz-naive DatetimeIndex named 'date'.  This normalizer
    produces exactly the same output shape as ``_clean_ohlcv()`` so the
    rest of the pipeline is unaware of the data source.
    """
    if df.empty:
        return df

    # Ensure expected columns exist
    expected = ["open", "high", "low", "close", "volume"]
    for col in expected:
        if col not in df.columns:
            logger.warning(f"{ticker}: Alpaca missing column '{col}'")
            df[col] = np.nan

    # adj_close: Alpaca does not separately split-adjust — use close as proxy
    if "adj_close" not in df.columns:
        df["adj_close"] = df["close"]

    # Add ticker column
    df["ticker"] = ticker

    # Sort, drop rows with null close
    df = df.sort_index()
    df = df.dropna(subset=["close"])

    # Ensure DatetimeIndex named 'date' (Alpaca already provides this, but guard anyway)
    if not isinstance(df.index, pd.DatetimeIndex):
        df.index = pd.to_datetime(df.index)
    df.index.name = "date"

    # Remove timezone info if present (Atlas standard: tz-naive)
    if df.index.tz is not None:
        df.index = df.index.tz_localize(None)

    return df


def _fetch_ohlcv(
    ticker: str,
    start_str: str,
    end_str: str,
    market_id: Optional[str] = None,
) -> pd.DataFrame:
    """Download OHLCV from the best available source and return cleaned data.

    For SP500: tries Alpaca ``get_historical_bars()`` first; falls back to
    yfinance if Alpaca returns empty or is unavailable.
    For all other markets: uses yfinance directly.

    Args:
        ticker:    Fully-formatted ticker (e.g. 'AAPL' for sp500, 'BHP.AX' for asx).
        start_str: Inclusive start date 'YYYY-MM-DD'.
        end_str:   Inclusive end date   'YYYY-MM-DD' (yfinance +1-day offset handled internally).
        market_id: Market identifier (e.g. 'sp500', 'asx').

    Returns:
        Cleaned DataFrame (same format as ``_clean_ohlcv``), or empty DataFrame.
    """
    if (market_id or "").lower() == "sp500":
        try:
            from brokers.alpaca.market_data import get_historical_bars as _alpaca_bars
            result = _alpaca_bars(ticker, start=start_str, end=end_str)
            df = result.get(ticker, pd.DataFrame())
            if not df.empty:
                logger.debug(f"{ticker}: Alpaca historical bars ({len(df)} rows)")
                return df  # already in Atlas format from get_historical_bars
            logger.debug(f"{ticker}: Alpaca returned empty — falling back to yfinance")
        except Exception as e:
            logger.debug(f"{ticker}: Alpaca fetch failed ({e}) — falling back to yfinance")

    # yfinance path (all non-sp500 markets, or sp500 fallback)
    if not YF_AVAILABLE:
        logger.warning(f"{ticker}: yfinance not available and Alpaca returned no data")
        return pd.DataFrame()

    # yfinance end is exclusive — add 1 day so end_str is included
    fetch_end = (pd.Timestamp(end_str) + timedelta(days=1)).strftime("%Y-%m-%d")
    try:
        df = yf.download(ticker, start=start_str, end=fetch_end,
                         progress=False, auto_adjust=False)
        if not df.empty:
            return _clean_ohlcv(df, ticker)
    except Exception as e:
        logger.error(f"{ticker}: yfinance download failed: {e}")

    return pd.DataFrame()


def download_ticker(
    ticker: str,
    start: Optional[str] = None,
    end: Optional[str] = None,
    use_cache: bool = True,
    market_id: Optional[str] = None,
) -> pd.DataFrame:
    """Download OHLCV data for a single ticker.

    Supports caching and incremental updates.

    Args:
        ticker: Ticker symbol (with or without market suffix).
        start: Start date string (default: 3 years ago).
        end: End date string (default: today).
        use_cache: Whether to use parquet cache (default True).
        market_id: Market identifier for cache namespacing and suffix normalization.

    Returns:
        DataFrame with columns: open, high, low, close, adj_close, volume, ticker.
        Index is DatetimeIndex named 'date'.
    """
    ticker = _normalize_ticker(ticker, market_id)

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
        cached = _load_cache(ticker, market_id)
        if cached is not None and not cached.empty:
            cache_start = cached.index.min()
            cache_end = cached.index.max()

            need_before = start_dt.date() < cache_start.date()
            need_after = end_dt.date() > cache_end.date()

            if not need_before and not need_after:
                mask = (cached.index >= start_str) & (cached.index <= end_str)
                logger.info(f"{ticker}: served from cache ({len(cached[mask])} rows)")
                return cached[mask]

            # Incremental update
            frames = [cached]

            if need_before:
                # Inclusive end for _fetch_ohlcv: day before cache_start
                before_end = (cache_start - timedelta(days=1)).strftime("%Y-%m-%d")
                logger.info(f"{ticker}: fetching earlier data {start_str} to {before_end}")
                earlier = _fetch_ohlcv(ticker, start_str, before_end, market_id)
                if not earlier.empty:
                    frames.insert(0, earlier)
                else:
                    logger.debug(f"{ticker}: no earlier data fetched")

            if need_after:
                fetch_from = (cache_end + timedelta(days=1)).strftime("%Y-%m-%d")
                logger.info(f"{ticker}: fetching newer data {fetch_from} to {end_str}")
                later = _fetch_ohlcv(ticker, fetch_from, end_str, market_id)
                if not later.empty:
                    frames.append(later)
                else:
                    logger.debug(f"{ticker}: no newer data fetched")

            combined = pd.concat(frames)
            combined = combined[~combined.index.duplicated(keep="last")]
            combined = combined.sort_index()
            _save_cache(ticker, combined, market_id)

            mask = (combined.index >= start_str) & (combined.index <= end_str)
            logger.info(f"{ticker}: incremental update complete ({len(combined[mask])} rows)")
            return combined[mask]

    # Full download — Alpaca-first for sp500, yfinance for others/fallback
    logger.info(f"{ticker}: downloading {start_str} to {end_str}")
    df = _fetch_ohlcv(ticker, start_str, end_str, market_id)

    if df.empty:
        logger.warning(f"{ticker}: no data returned from any source")
        return pd.DataFrame()

    if use_cache:
        _save_cache(ticker, df, market_id)

    logger.info(f"{ticker}: downloaded {len(df)} rows")
    return df


def download_universe(
    tickers: List[str],
    start: Optional[str] = None,
    end: Optional[str] = None,
    use_cache: bool = True,
    delay: float = 0.1,
    market_id: Optional[str] = None,
) -> Dict[str, pd.DataFrame]:
    """Download OHLCV data for multiple tickers.

    Args:
        tickers: List of ticker symbols.
        start: Start date string (default: 3 years ago).
        end: End date string (default: today).
        use_cache: Whether to use parquet cache.
        delay: Seconds to wait between downloads.
        market_id: Market identifier for cache namespacing.

    Returns:
        Dict mapping ticker -> DataFrame.
    """
    results = {}
    total = len(tickers)
    success = 0
    failed = []

    logger.info(f"Downloading universe: {total} tickers (market={market_id or DEFAULT_MARKET})")

    for i, ticker in enumerate(tickers, 1):
        ticker = _normalize_ticker(ticker, market_id)
        try:
            df = download_ticker(ticker, start=start, end=end,
                                 use_cache=use_cache, market_id=market_id)
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


def clear_cache(ticker: Optional[str] = None, market_id: Optional[str] = None) -> int:
    """Clear cached parquet files.

    Args:
        ticker: If provided, clear only this ticker's cache.
        market_id: Market to clear cache for. If None with no ticker, clears ALL markets.

    Returns:
        Number of files deleted.
    """
    count = 0
    if ticker:
        ticker = _normalize_ticker(ticker, market_id)
        path = _cache_path(ticker, market_id)
        if path.exists():
            path.unlink()
            count = 1
            logger.info(f"Cleared cache for {ticker}")
    elif market_id:
        cache_dir = _market_cache_dir(market_id)
        for f in cache_dir.glob("*.parquet"):
            f.unlink()
            count += 1
        logger.info(f"Cleared {count} cached files for {market_id}")
    else:
        for f in CACHE_DIR.rglob("*.parquet"):
            f.unlink()
            count += 1
        logger.info(f"Cleared {count} cached files (all markets)")
    return count


def cache_stats(market_id: Optional[str] = None) -> Dict:
    """Return statistics about the cache.

    Args:
        market_id: If provided, stats for this market only. Otherwise all.

    Returns:
        Dict with keys: file_count, total_size_mb, oldest, newest, tickers.
    """
    if market_id:
        search_dir = _market_cache_dir(market_id)
        files = list(search_dir.glob("*.parquet"))
    else:
        files = list(CACHE_DIR.rglob("*.parquet"))

    if not files:
        return {"file_count": 0, "total_files": 0, "total_size_mb": 0,
                "oldest": None, "newest": None, "tickers": []}

    sizes = [f.stat().st_size for f in files]
    mtimes = [datetime.fromtimestamp(f.stat().st_mtime) for f in files]

    # Reconstruct ticker names from filenames
    tickers = []
    for f in files:
        name = f.stem
        # Try to reconstruct original ticker: BHP_AX -> BHP.AX, AAPL -> AAPL
        if "_AX" in name:
            tickers.append(name.replace("_AX", ".AX"))
        elif "_L" in name:
            tickers.append(name.replace("_L", ".L"))
        else:
            tickers.append(name)

    return {
        "file_count": len(files),
        "total_files": len(files),
        "total_size_mb": round(sum(sizes) / (1024 * 1024), 2),
        "oldest": min(mtimes).isoformat(),
        "newest": max(mtimes).isoformat(),
        "tickers": sorted(tickers),
    }
