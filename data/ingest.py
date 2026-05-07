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

logger = logging.getLogger(__name__)


def _is_crypto_ticker(symbol: str) -> bool:
    """Return True if symbol looks like a crypto pair (e.g. BTC-USD, BTC/USD)."""
    crypto_suffixes = ('/USD', '/USDT', '-USD', '-USDT')
    return any(symbol.upper().endswith(s) for s in crypto_suffixes)

try:
    import yfinance as yf
    YF_AVAILABLE = True
except ImportError:
    YF_AVAILABLE = False
    logger.warning("yfinance not installed — yfinance fallback unavailable")

# Project paths
PROJECT_ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = PROJECT_ROOT / "data" / "cache"

# Ensure base cache directory exists
CACHE_DIR.mkdir(parents=True, exist_ok=True)

# Cache freshness threshold
CACHE_MAX_AGE_HOURS = 24

# Default market
DEFAULT_MARKET = "asx"

# Tickers that MUST use yfinance — not available or unreliable on Alpaca.
# Includes index tickers (^VIX, ^GSPC), futures (GC=F, HG=F), and broad ETFs
# that Alpaca's IEX feed doesn't carry consistently.
_YFINANCE_ONLY = {"^VIX", "^TNX", "^IRX", "^AXJO", "GC=F", "HG=F", "SPY", "^GSPC", "^SKEW", "RSP"}


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
            # Invalidate caches built with dividend-adjusted prices.
            # New format: split-adjusted raw prices only, no adj_close column.
            # Any cache that still has adj_close was written by the old pipeline.
            if "adj_close" in df.columns:
                logger.info(
                    f"Cache for {ticker} has adj_close — old adjusted format, "
                    "forcing re-download with split-adjusted raw prices"
                )
                return None
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
        path.parent.mkdir(parents=True, exist_ok=True)  # Fix 5: ensure per-market subdir exists
        tmp_path = path.with_suffix('.parquet.tmp')
        df.to_parquet(tmp_path, engine="pyarrow")
        import os
        os.replace(str(tmp_path), str(path))
        logger.debug(f"Cached {ticker}: {len(df)} rows -> {path}")
        # SQLite dual-write — batch insert for performance; failure is non-fatal
        try:
            from db.atlas_db import get_db as _get_db
            _rows = []
            for _t in df.itertuples():
                _date_str = _t.Index.strftime('%Y-%m-%d') if hasattr(_t.Index, 'strftime') else str(_t.Index)[:10]
                _rows.append((
                    ticker,
                    _date_str,
                    float(getattr(_t, 'open', 0) or 0),
                    float(getattr(_t, 'high', 0) or 0),
                    float(getattr(_t, 'low', 0) or 0),
                    float(getattr(_t, 'close', 0) or 0),
                    None,  # adj_close — not stored in canonical format
                    int(getattr(_t, 'volume', 0) or 0),
                    (market_id or 'sp500').lower(),
                    'yfinance',
                ))
            if _rows:
                with _get_db() as _db:
                    _db.executemany(
                        """
                        INSERT OR REPLACE INTO ohlcv
                            (ticker, date, open, high, low, close, adj_close, volume, universe, source)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        _rows,
                    )
                logger.debug(f"SQLite OHLCV dual-write: {len(_rows)} rows for {ticker}")
        except Exception as _db_exc:
            logger.warning(f"SQLite OHLCV dual-write failed for {ticker}: {_db_exc}")
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

    # Drop adj_close — canonical format is split-adjusted raw OHLCV only
    if "adj_close" in df.columns:
        df = df.drop(columns=["adj_close"])

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

    # Drop adj_close — canonical format is open, high, low, close, volume, ticker
    if "adj_close" in df.columns:
        df = df.drop(columns=["adj_close"])

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


def _apply_split_adjustments(df: pd.DataFrame, splits: pd.Series) -> pd.DataFrame:
    """Apply cumulative split adjustments to raw OHLCV data (backward adjustment).

    Adjusts all historical prices so they are comparable to the current
    (post-split) price scale.  For each split event, prices *before* the
    split date are divided by the split ratio and volumes are multiplied by
    the split ratio to preserve dollar-volume consistency.

    Only SPLIT adjustments are applied — dividend adjustments are intentionally
    omitted to prevent retroactive price drift when dividends are paid.

    Args:
        df:     DataFrame with at least open, high, low, close columns and a
                DatetimeIndex named 'date'.
        splits: Series indexed by split date (tz-naive), values are split
                ratios (e.g. 4.0 = 4:1 forward split — 1 old share → 4 new).

    Returns:
        New DataFrame with split-adjusted OHLCV.  Prices before each split
        date are divided by the cumulative split ratio; volumes are multiplied.
    """
    if splits is None or splits.empty:
        return df

    df = df.copy()
    price_cols = [c for c in ["open", "high", "low", "close"] if c in df.columns]

    # Only apply splits that fall within or before the data range.
    # Sort chronologically so earlier splits are applied first.
    relevant = splits[splits.index <= df.index.max()].sort_index()

    for split_date, ratio in relevant.items():
        if ratio <= 0 or ratio == 1.0:
            continue
        mask = df.index < split_date
        if not mask.any():
            continue
        df.loc[mask, price_cols] = df.loc[mask, price_cols] / ratio
        if "volume" in df.columns:
            df.loc[mask, "volume"] = (df.loc[mask, "volume"] * ratio).round()

    return df


def _download_via_yfinance(
    ticker: str,
    start_str: str,
    end_str: str,
) -> pd.DataFrame:
    """Download OHLCV for a single ticker via yfinance.

    Applies split adjustments (not dividend adjustments) to historical prices.

    Args:
        ticker:    Fully-formatted ticker symbol (e.g. 'AAPL', 'BHP.AX').
        start_str: Inclusive start date 'YYYY-MM-DD'.
        end_str:   Inclusive end date   'YYYY-MM-DD'.

    Returns:
        Cleaned split-adjusted DataFrame, or empty DataFrame on failure.
    """
    if not YF_AVAILABLE:
        logger.warning(f"{ticker}: yfinance not available")
        return pd.DataFrame()

    # yfinance end is exclusive — add 1 day so end_str is included
    fetch_end = (pd.Timestamp(end_str) + timedelta(days=1)).strftime("%Y-%m-%d")
    try:
        df = yf.download(ticker, start=start_str, end=fetch_end,
                         progress=False, auto_adjust=False)
        if not df.empty:
            cleaned = _clean_ohlcv(df, ticker)
            # Fetch split history and backward-adjust prices to current scale.
            # Only split adjustments — no dividend adjustments (prevents
            # retroactive price drift when dividends are paid).
            try:
                splits = yf.Ticker(ticker).splits
                if splits is not None and not splits.empty:
                    # Ensure tz-naive to match cleaned df index
                    if splits.index.tz is not None:
                        splits.index = splits.index.tz_localize(None)
                    cleaned = _apply_split_adjustments(cleaned, splits)
                    logger.debug(
                        f"{ticker}: applied {len(splits)} split adjustment(s)"
                    )
            except Exception as se:
                logger.warning(
                    f"{ticker}: split data fetch failed ({se}) "
                    "— proceeding without split adjustment"
                )
            return cleaned
    except Exception as e:
        logger.error(f"{ticker}: yfinance download failed: {e}")

    return pd.DataFrame()


def _download_via_alpaca(
    tickers: List[str],
    start_date: str,
    end_date: str,
    config: Optional[dict] = None,  # reserved for future source_priority reads
) -> Dict[str, pd.DataFrame]:
    """Download OHLCV via Alpaca API.

    Uses the AlpacaMarketData singleton (reads credentials from
    ``~/.atlas-secrets.json``).  Returns an empty dict if Alpaca is
    unavailable or credentials are missing — callers should fall back to
    yfinance in that case.

    Args:
        tickers:    List of Atlas-format US tickers.
        start_date: Start date 'YYYY-MM-DD'.
        end_date:   End date   'YYYY-MM-DD'.
        config:     Optional config dict (reserved; not used yet).

    Returns:
        Dict of ticker -> DataFrame (same schema as yfinance output).
        Empty dict on any failure.
    """
    try:
        from brokers.alpaca.market_data import get_alpaca_data_client
        client = get_alpaca_data_client()
        if client is None:
            logger.debug("_download_via_alpaca: no Alpaca client available")
            return {}
        result = client.download_universe_bars(tickers, start_date, end_date)
        logger.debug(
            "_download_via_alpaca: got %d/%d tickers",
            len(result), len(tickers),
        )
        return result
    except Exception as e:
        logger.warning("_download_via_alpaca failed: %s", e)
        return {}


def _fetch_ohlcv(
    ticker: str,
    start_str: str,
    end_str: str,
    market_id: Optional[str] = None,
) -> pd.DataFrame:
    """Download OHLCV from the best available source and return cleaned data.

    Routing rules:
    - Tickers in ``_YFINANCE_ONLY`` always go to yfinance (index tickers,
      futures, and ETFs not available on Alpaca).
    - SP500 market: Alpaca-primary with yfinance fallback.
    - All other markets (ASX, etc.): yfinance directly.

    Args:
        ticker:    Fully-formatted ticker (e.g. 'AAPL' for sp500, 'BHP.AX' for asx).
        start_str: Inclusive start date 'YYYY-MM-DD'.
        end_str:   Inclusive end date   'YYYY-MM-DD'.
        market_id: Market identifier (e.g. 'sp500', 'asx').

    Returns:
        Cleaned DataFrame (same format as ``_clean_ohlcv``), or empty DataFrame.
    """
    # Force yfinance for index/commodity tickers not available on Alpaca
    if ticker in _YFINANCE_ONLY:
        logger.debug(f"{ticker}: in _YFINANCE_ONLY — using yfinance directly")
        return _download_via_yfinance(ticker, start_str, end_str)

    # Crypto: route to Alpaca CryptoHistoricalDataClient
    if (market_id or "").lower() == "crypto" or _is_crypto_ticker(ticker):
        try:
            from brokers.alpaca.market_data import get_historical_bars as _alpaca_bars
            result = _alpaca_bars(ticker, start=start_str, end=end_str)
            df = result.get(ticker, pd.DataFrame())
            if not df.empty:
                logger.debug(f"{ticker}: Alpaca crypto bars ({len(df)} rows)")
                if "adj_close" in df.columns:
                    df = df.drop(columns=["adj_close"])
                return df
            logger.debug(f"{ticker}: Alpaca crypto returned empty — falling back to yfinance")
        except Exception as e:
            logger.debug(f"{ticker}: Alpaca crypto fetch failed ({e}) — falling back to yfinance")

    if (market_id or "").lower() == "sp500":
        # Alpaca-primary path for US equities
        try:
            from brokers.alpaca.market_data import get_historical_bars as _alpaca_bars
            result = _alpaca_bars(ticker, start=start_str, end=end_str)
            df = result.get(ticker, pd.DataFrame())
            if not df.empty:
                logger.debug(f"{ticker}: Alpaca historical bars ({len(df)} rows)")
                # Drop adj_close — canonical format is open, high, low, close, volume, ticker
                if "adj_close" in df.columns:
                    df = df.drop(columns=["adj_close"])
                return df  # already in Atlas format from get_historical_bars
            logger.debug(f"{ticker}: Alpaca returned empty — falling back to yfinance")
        except Exception as e:
            logger.debug(f"{ticker}: Alpaca fetch failed ({e}) — falling back to yfinance")

    # yfinance path (all non-sp500 markets, or sp500 fallback)
    return _download_via_yfinance(ticker, start_str, end_str)


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
        DataFrame with columns: open, high, low, close, volume, ticker.
        Prices are split-adjusted (no dividend adjustment).
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

    # Filter out auto-excluded tickers
    try:
        from data.auto_exclusions import get_excluded_tickers
        auto_excluded = get_excluded_tickers(market_id)
        if auto_excluded:
            before_count = len(tickers)
            tickers = [t for t in tickers if t.upper() not in auto_excluded 
                       and t.split('.')[0].upper() not in auto_excluded]
            if before_count != len(tickers):
                logger.info(
                    "Filtered %d auto-excluded tickers: %s",
                    before_count - len(tickers), auto_excluded,
                )
                total = len(tickers)
    except ImportError:
        pass

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

    # Ensure all downloaded data is written to SQLite (covers cache-hit paths
    # where _save_cache() was not called, and retries any failed dual-writes)
    sqlite_total = 0
    for ticker, df in results.items():
        if not df.empty:
            n = _sqlite_batch_write(ticker, df, (market_id or DEFAULT_MARKET).lower())
            sqlite_total += n
    if sqlite_total:
        logger.info(
            "download_universe SQLite sync: %d rows written for %d tickers (market=%s)",
            sqlite_total, len(results), market_id or DEFAULT_MARKET,
        )

    return results


# ---------------------------------------------------------------------------
# Multi-Universe Ingest (v2.0)
# ---------------------------------------------------------------------------

def _sqlite_batch_write(
    ticker: str,
    df: "pd.DataFrame",
    universe_name: str,
    source: str = "yfinance",
) -> int:
    """Write a DataFrame of OHLCV rows to the SQLite ohlcv table.

    Uses INSERT OR REPLACE so the universe column reflects the *calling*
    universe, regardless of what prior ingests wrote.  Returns row count.
    """
    try:
        from db.atlas_db import get_db as _get_db
        rows = []
        for t_row in df.itertuples():
            date_str = (
                t_row.Index.strftime("%Y-%m-%d")
                if hasattr(t_row.Index, "strftime")
                else str(t_row.Index)[:10]
            )
            rows.append((
                ticker,
                date_str,
                float(getattr(t_row, "open", 0) or 0),
                float(getattr(t_row, "high", 0) or 0),
                float(getattr(t_row, "low", 0) or 0),
                float(getattr(t_row, "close", 0) or 0),
                None,  # adj_close — not stored in canonical format
                int(getattr(t_row, "volume", 0) or 0),
                universe_name,
                source,
            ))
        if rows:
            with _get_db() as db:
                db.executemany(
                    """
                    INSERT OR REPLACE INTO ohlcv
                        (ticker, date, open, high, low, close, adj_close,
                         volume, universe, source)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    rows,
                )
            logger.debug(
                "SQLite batch write: %d rows for %s (universe=%s)",
                len(rows), ticker, universe_name,
            )
        return len(rows)
    except Exception as exc:
        logger.warning(
            "SQLite batch write failed for %s (universe=%s): %s",
            ticker, universe_name, exc,
        )
        return 0


def verify_sqlite_integrity(
    market_id: str,
    tickers: List[str],
    backfill: bool = True,
) -> dict:
    """Verify SQLite ohlcv has data for all tickers and optionally backfill gaps.

    Args:
        market_id: Universe/market name (used as the 'universe' column value).
        tickers: List of tickers that should have data.
        backfill: If True, backfill missing/stale data from parquet cache.

    Returns:
        dict with keys: market, total, present, missing, backfilled, still_missing.
    """
    from db.atlas_db import get_db as _get_db

    present = []
    missing = []

    with _get_db() as db:
        for ticker in tickers:
            count = db.execute(
                "SELECT COUNT(*) FROM ohlcv WHERE ticker = ? AND universe = ?",
                (ticker, market_id.lower()),
            ).fetchone()[0]
            if count > 0:
                present.append(ticker)
            else:
                missing.append(ticker)

    backfilled = []
    still_missing = []

    if backfill and missing:
        logger.warning(
            "verify_sqlite_integrity(%s): %d tickers missing from SQLite, attempting backfill",
            market_id, len(missing),
        )
        for ticker in missing:
            cached = _load_cache(ticker, market_id)
            if cached is not None and not cached.empty:
                n = _sqlite_batch_write(ticker, cached, market_id.lower())
                if n > 0:
                    backfilled.append(ticker)
                    logger.info(
                        "Backfilled %s from parquet -> SQLite (%d rows, universe=%s)",
                        ticker, n, market_id,
                    )
                else:
                    still_missing.append(ticker)
            else:
                still_missing.append(ticker)

        if still_missing:
            logger.error(
                "verify_sqlite_integrity(%s): %d tickers still missing after backfill: %s",
                market_id, len(still_missing), still_missing,
            )
            try:
                from alerting import get_alert_manager
                alert = (
                    f"🚨 <b>DATA INTEGRITY FAILURE [{market_id.upper()}]</b>\n\n"
                    f"{len(still_missing)} tickers have NO data in SQLite "
                    f"even after backfill attempt:\n"
                    + "\n".join(f"  • {t}" for t in still_missing)
                    + "\n\nParquet cache also missing. Manual investigation required."
                )
                get_alert_manager().send(alert)
            except Exception:
                pass
    else:
        still_missing = list(missing)

    result = {
        "market": market_id,
        "total": len(tickers),
        "present": present,
        "missing": missing,
        "backfilled": backfilled,
        "still_missing": still_missing,
    }

    logger.info(
        "verify_sqlite_integrity(%s): %d/%d present, %d backfilled, %d still missing",
        market_id, len(present), len(tickers), len(backfilled), len(still_missing),
    )
    return result


def ingest_universe(
    universe_name: str,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    force: bool = False,
) -> dict:
    """Fetch OHLCV data for all tickers in a static universe and write to SQLite.

    Args:
        universe_name: One of the 6 universe names from universe/definitions.py.
                       Must be a static universe (raises ValueError for 'sp500').
        start_date:    ISO date string (default: 7 years ago).
        end_date:      ISO date string (default: today).
        force:         If True, bypass parquet cache and re-fetch from API.

    Returns:
        dict with keys:
            universe        — universe name
            tickers_fetched — list of tickers successfully ingested
            tickers_failed  — list of tickers that failed (delisted, no data, etc.)
            rows_written    — total OHLCV rows written to SQLite
            start_date      — start date used
            end_date        — end date used
    """
    from universe.definitions import get_universe_tickers, get_universe

    # Validate universe and get ticker list (raises KeyError/ValueError for sp500)
    defn = get_universe(universe_name)
    if defn["method"] != "static":
        raise ValueError(
            f"ingest_universe() only supports static ETF universes; "
            f"{universe_name!r} uses method {defn['method']!r}. "
            f"Use the dedicated SP500 pipeline for dynamic universes."
        )
    tickers = get_universe_tickers(universe_name)

    # Resolve date range
    if end_date is None:
        end_date = datetime.now().strftime("%Y-%m-%d")
    if start_date is None:
        # Default: 7 years of history for ETFs
        start_date = (datetime.now() - timedelta(days=7 * 365)).strftime("%Y-%m-%d")

    total = len(tickers)
    tickers_fetched: List[str] = []
    tickers_failed: List[str] = []
    rows_written = 0

    logger.info(
        "ingest_universe(%r): %d tickers, %s → %s (force=%s)",
        universe_name, total, start_date, end_date, force,
    )
    print(
        f"[ingest] {universe_name}: {total} tickers, "
        f"{start_date} → {end_date}, force={force}"
    )

    for i, ticker in enumerate(tickers, 1):
        try:
            # ETF universes are US-listed and are best served by yfinance.
            # We bypass download_ticker() here because its _normalize_ticker()
            # call requires the market_id to be registered in the markets
            # registry (which only knows 'asx' and 'sp500'). Instead we
            # call the internal helpers directly:
            #   _load_cache / _download_via_yfinance / _save_cache
            # This avoids any market-registry dependency while still using
            # universe-namespaced parquet caches (data/cache/sector_etfs/...).

            df: pd.DataFrame = pd.DataFrame()

            # Step 1: try parquet cache (skip when force=True)
            if not force:
                cached = _load_cache(ticker, universe_name)
                if cached is not None and not cached.empty:
                    mask = (cached.index >= start_date) & (cached.index <= end_date)
                    filtered = cached[mask]
                    if not filtered.empty:
                        df = filtered
                        logger.debug("%s: served from cache (%d rows)", ticker, len(df))

            # Step 2: download if cache missed (Alpaca for crypto, yfinance for ETFs)
            if df.empty:
                if _is_crypto_ticker(ticker):
                    df = _fetch_ohlcv(ticker, start_date, end_date, universe_name)
                else:
                    df = _download_via_yfinance(ticker, start_date, end_date)
                if not df.empty:
                    # Save parquet cache under the universe-namespaced directory
                    _save_cache(ticker, df, universe_name)

            # Step 3: explicit SQLite write with correct universe tag
            # (covers both the fresh-download AND cache-hit paths, ensuring
            # cross-universe tickers like GLD are tagged for THIS universe)
            if not df.empty:
                n = _sqlite_batch_write(ticker, df, universe_name)
                tickers_fetched.append(ticker)
                rows_written += n
                logger.debug("%s: %d rows written (universe=%s)", ticker, n, universe_name)
            else:
                logger.warning("%s: no data returned — adding to failed list", ticker)
                tickers_failed.append(ticker)

        except Exception as exc:
            logger.warning("%s: fetch failed: %s", ticker, exc)
            tickers_failed.append(ticker)

        if i % 5 == 0 or i == total:
            print(
                f"[ingest] {universe_name}: {i}/{total} done "
                f"(fetched={len(tickers_fetched)}, failed={len(tickers_failed)})"
            )
            logger.info(
                "%s progress: %d/%d (fetched=%d, failed=%d)",
                universe_name, i, total,
                len(tickers_fetched), len(tickers_failed),
            )

        # Rate-limit courtesy delay — yfinance is generally tolerant but
        # avoids hammering the endpoint when running large batches.
        if i < total:
            time.sleep(0.5)

    result = {
        "universe": universe_name,
        "tickers_fetched": tickers_fetched,
        "tickers_failed": tickers_failed,
        "rows_written": rows_written,
        "start_date": start_date,
        "end_date": end_date,
    }
    logger.info(
        "ingest_universe(%r) complete: fetched=%d failed=%d rows=%d",
        universe_name,
        len(tickers_fetched),
        len(tickers_failed),
        rows_written,
    )
    print(
        f"[ingest] {universe_name} DONE: "
        f"fetched={len(tickers_fetched)}, "
        f"failed={len(tickers_failed)}, "
        f"rows={rows_written}"
    )
    return result


def ingest_all_etf_universes(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    force: bool = False,
) -> dict:
    """Ingest all 5 static ETF universes (excludes sp500 which has its own pipeline).

    Calls ingest_universe() for each ETF universe in definition order and
    aggregates the results.

    Args:
        start_date: ISO date string passed to each ingest_universe() call
                    (default: 7 years ago).
        end_date:   ISO date string (default: today).
        force:      If True, bypass parquet cache for all universes.

    Returns:
        dict with keys:
            universes_ingested     — list of universe names ingested
            total_tickers_fetched  — total successful ticker count
            total_tickers_failed   — deduplicated list of failed tickers
            total_rows_written     — total SQLite rows written
            results                — per-universe result dicts
    """
    from universe.definitions import list_universes

    etf_universes = [u for u in list_universes() if u != "sp500"]
    logger.info(
        "ingest_all_etf_universes: %d universes: %s", len(etf_universes), etf_universes
    )
    print(f"[ingest] Starting all ETF universes: {etf_universes}")

    aggregate: dict = {
        "universes_ingested": [],
        "total_tickers_fetched": 0,
        "total_tickers_failed": [],
        "total_rows_written": 0,
        "results": {},
    }

    failed_seen: set = set()
    for universe_name in etf_universes:
        result = ingest_universe(
            universe_name,
            start_date=start_date,
            end_date=end_date,
            force=force,
        )
        aggregate["universes_ingested"].append(universe_name)
        aggregate["total_tickers_fetched"] += len(result["tickers_fetched"])
        aggregate["total_rows_written"] += result["rows_written"]
        for t in result["tickers_failed"]:
            if t not in failed_seen:
                failed_seen.add(t)
                aggregate["total_tickers_failed"].append(t)
        aggregate["results"][universe_name] = result

    print(
        f"[ingest] ALL ETF UNIVERSES DONE: "
        f"universes={len(aggregate['universes_ingested'])}, "
        f"tickers_fetched={aggregate['total_tickers_fetched']}, "
        f"tickers_failed={len(aggregate['total_tickers_failed'])}, "
        f"rows={aggregate['total_rows_written']}"
    )
    logger.info(
        "ingest_all_etf_universes complete: universes=%d rows=%d",
        len(aggregate["universes_ingested"]),
        aggregate["total_rows_written"],
    )
    return aggregate


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


def refresh_macro_data(cache_max_age_hours: int = 24) -> bool:
    """Refresh macro regime data (gold, copper, VIX, yield curve).

    Downloads macro data from yfinance and FRED and caches it for use by
    the macro regime calculator (data.macro).  Called by cron scripts
    during the daily data refresh cycle so that macro signals are current
    before the trading session begins.

    The function is a no-op (returns False with a warning) when data.macro
    is not yet installed — this keeps the cron pipeline safe during deploys.

    Args:
        cache_max_age_hours: Skip network refresh when cached data is younger
                             than this many hours.  Defaults to 24 (daily).

    Returns:
        True if the refresh succeeded and returned non-empty data.
        False on any failure (import error, network error, empty result).
    """
    try:
        from data.macro import download_macro_data
    except ImportError as e:
        logger.warning(
            "data.macro not available — macro data refresh skipped "
            f"(install data/macro.py to enable): {e}"
        )
        return False

    logger.info("Refreshing macro regime data (gold, copper, VIX, yields)...")
    try:
        df = download_macro_data(cache_max_age_hours=cache_max_age_hours)
        if df is None or (hasattr(df, "empty") and df.empty):
            logger.warning("Macro data refresh returned empty result — check data sources")
            return False
        logger.info(
            f"Macro data refreshed successfully: {len(df)} rows, "
            f"columns={list(df.columns)}, "
            f"range=[{df.index.min().date()}, {df.index.max().date()}]"
        )
    except Exception as e:
        logger.error(f"Macro data refresh failed: {e}", exc_info=True)
        return False

    # Also persist to macro_indicators SQLite table (includes FRED data)
    try:
        from data.macro import fetch_macro_data
        db_df = fetch_macro_data(
            write_to_db=True,
            use_cache=True,
            # Propagate the caller's cache-age preference to FRED so that
            # refresh_macro_data(cache_max_age_hours=0) also forces a fresh
            # FRED API fetch (not just yfinance).
            fred_max_age_hours=cache_max_age_hours,
        )
        if db_df is not None and not db_df.empty:
            logger.info(
                f"Macro indicators written to DB: {len(db_df)} rows "
                f"[{db_df.index.min().date()}, {db_df.index.max().date()}]"
            )
        else:
            logger.warning("fetch_macro_data(write_to_db=True) returned empty — FRED data may be missing")
    except Exception as e:
        logger.warning(f"Macro DB write failed (yfinance cache still updated): {e}")

    return True


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


# ---------------------------------------------------------------------------
# Stale Data Detection (A5)
# ---------------------------------------------------------------------------

def _last_trading_day(reference_date: Optional[datetime] = None) -> datetime:
    """Return the most recent weekday on or before *reference_date* (at midnight).

    Handles weekends by walking back to Friday.  Does NOT account for
    US market holidays — callers relying on exact holiday-awareness should
    use a calendar library.  For the stale-data check, "weekend adjustment"
    is the dominant case and is sufficient for Atlas's needs.

    The returned datetime is always at midnight (00:00:00) so that date
    comparisons against DataFrame DatetimeIndex values are unambiguous.

    Args:
        reference_date: Date to anchor from (default: today).

    Returns:
        datetime at midnight of the last expected trading day.
    """
    if reference_date is None:
        reference_date = datetime.now()

    d = reference_date
    # Walk back from Sunday (6) and Saturday (5) to Friday (4)
    while d.weekday() >= 5:  # 5=Saturday, 6=Sunday
        d -= timedelta(days=1)
    # Normalise to midnight to avoid time-of-day comparison issues
    return d.replace(hour=0, minute=0, second=0, microsecond=0)


def check_data_freshness(
    data: Dict[str, "pd.DataFrame"],
    market_id: Optional[str] = None,
    max_lag_days: int = 1,
) -> dict:
    """Verify that downloaded data is fresh (not stale/cached from a prior day).

    Checks each ticker's most recent data date against the expected last
    trading day.  Returns a summary with overall pass/fail, stale ticker list,
    and the freshest/stalest dates found.

    Args:
        data:         Dict of ticker -> DataFrame (output of download_universe).
        market_id:    Market identifier for logging (informational only).
        max_lag_days: Maximum allowed lag in trading days.  Default 1 allows
                      for end-of-day data that arrives the morning after
                      (e.g. data as of yesterday is fresh when running pre-market).

    Returns:
        Dict with keys:
            is_fresh (bool):         True if all checked tickers meet freshness.
            stale_tickers (list):    Tickers whose data is too old.
            fresh_count (int):       Number of tickers with fresh data.
            stale_count (int):       Number of tickers with stale data.
            expected_date (str):     Expected minimum data date (YYYY-MM-DD).
            newest_date (str | None): Most recent data date across all tickers.
            oldest_date (str | None): Oldest most-recent-date across all tickers.
            message (str):           Human-readable summary.
    """
    import pandas as _pd

    if not data:
        return {
            "is_fresh": False,
            "stale_tickers": [],
            "fresh_count": 0,
            "stale_count": 0,
            "expected_date": "",
            "newest_date": None,
            "oldest_date": None,
            "message": "No data provided — nothing to check",
        }

    # expected_dt is the oldest date we still consider "fresh":
    # max_lag_days=1 → data from yesterday or today is acceptable
    # max_lag_days=0 → only today's data is acceptable
    expected_dt = _last_trading_day() - timedelta(days=max_lag_days)
    expected_date = expected_dt.strftime("%Y-%m-%d")

    stale_tickers = []
    all_latest_dates = []
    checked = 0

    for ticker, df in data.items():
        if df is None or (hasattr(df, "empty") and df.empty):
            continue
        checked += 1
        try:
            latest = df.index.max()
            if hasattr(latest, "to_pydatetime"):
                latest = latest.to_pydatetime()
            elif not isinstance(latest, datetime):
                latest = _pd.Timestamp(latest).to_pydatetime()
            # Strip time component
            latest_date_str = latest.strftime("%Y-%m-%d")
            all_latest_dates.append(latest_date_str)
            if latest < expected_dt:
                stale_tickers.append(ticker)
        except Exception as e:
            logger.debug("Freshness check for %s failed: %s", ticker, e)

    if not all_latest_dates:
        return {
            "is_fresh": False,
            "stale_tickers": [],
            "fresh_count": 0,
            "stale_count": 0,
            "expected_date": expected_date,
            "newest_date": None,
            "oldest_date": None,
            "message": "Could not determine data dates from downloaded data",
        }

    newest_date = max(all_latest_dates)
    oldest_date = min(all_latest_dates)
    stale_count = len(stale_tickers)
    fresh_count = checked - stale_count
    is_fresh = stale_count == 0

    if is_fresh:
        message = (
            f"Data is FRESH: {fresh_count}/{checked} tickers at or after {expected_date}. "
            f"Newest: {newest_date}."
        )
    else:
        sample = stale_tickers[:5]
        more = f" (+{stale_count - 5} more)" if stale_count > 5 else ""
        message = (
            f"STALE DATA DETECTED: {stale_count}/{checked} tickers older than "
            f"{expected_date}. Stale: {sample}{more}. "
            f"Oldest latest: {oldest_date}."
        )

    logger.info("Data freshness check: %s", message)
    return {
        "is_fresh": is_fresh,
        "stale_tickers": stale_tickers,
        "fresh_count": fresh_count,
        "stale_count": stale_count,
        "expected_date": expected_date,
        "newest_date": newest_date,
        "oldest_date": oldest_date,
        "message": message,
    }


def verify_ingest_freshness(
    data: Dict[str, "pd.DataFrame"],
    config: Optional[dict] = None,
    market_id: Optional[str] = None,
) -> bool:
    """Verify data freshness with smart auto-exclusion for stale tickers.

    Instead of binary halt/continue, applies graduated response:
    - ALL tickers stale → halt (real data provider issue)
    - >5% of tickers stale → halt (systemic problem)
    - 1-3 individual tickers stale → auto-exclude them, alert, continue
    - 0 stale → pass

    Auto-excluded tickers are:
    - Added to config/auto_excluded_tickers.json
    - Cache files quarantined
    - Telegram alert sent
    - Pipeline continues with remaining tickers

    Args:
        data:      Dict of ticker -> DataFrame (output of download_universe).
        config:    Active Atlas config dict.
        market_id: Market identifier for log/alert messages.

    Returns:
        True if data is fresh (possibly after auto-excluding stale tickers).
        False if stale data remains and halt_on_stale_data is False.

    Raises:
        RuntimeError: If stale data is systemic and halt_on_stale_data is True.
    """
    freshness = check_data_freshness(data, market_id=market_id)

    if freshness["is_fresh"]:
        logger.info(
            "Ingest freshness OK [%s]: %d tickers, newest=%s",
            market_id or "?",
            freshness["fresh_count"],
            freshness["newest_date"],
        )
        return True

    # Stale data detected — apply graduated response
    market_label = market_id or "?"
    stale_tickers = freshness["stale_tickers"]
    stale_count = freshness["stale_count"]
    total_checked = freshness["fresh_count"] + stale_count
    expected = freshness["expected_date"]
    oldest = freshness["oldest_date"]
    stale_pct = (stale_count / total_checked * 100) if total_checked > 0 else 100

    # Determine if this is systemic or individual
    all_stale = stale_count == total_checked
    systemic = all_stale or (total_checked > 20 and stale_pct > 5)
    auto_excludable = not systemic and stale_count <= 10

    logger.warning(
        "STALE DATA [%s]: %d/%d stale (%.1f%%). systemic=%s, auto_excludable=%s",
        market_label, stale_count, total_checked, stale_pct,
        systemic, auto_excludable,
    )

    if auto_excludable:
        # Auto-exclude individual stale tickers and continue
        from data.auto_exclusions import add_exclusion, quarantine_cache

        excluded_details = []
        for ticker in stale_tickers:
            # Get last data date for the alert
            df = data.get(ticker)
            last_date = "unknown"
            if df is not None and not df.empty:
                try:
                    last_date = df.index.max().strftime("%Y-%m-%d")
                except Exception:
                    pass

            add_exclusion(
                ticker=ticker,
                market_id=market_label,
                reason=f"stale_data: last data {last_date}, expected >= {expected}",
                last_data_date=last_date,
            )
            quarantine_cache(ticker, market_label)
            excluded_details.append(f"{ticker} (last: {last_date})")

            # Remove from data dict so downstream gets clean data
            data.pop(ticker, None)

        logger.info(
            "Auto-excluded %d stale tickers from %s: %s",
            len(excluded_details), market_label, excluded_details,
        )

        # Send Telegram alert for auto-exclusions
        try:
            from alerting import get_alert_manager
            ticker_lines = "\n".join(f"  • {d}" for d in excluded_details)
            alert = (
                f"⚠️ <b>AUTO-EXCLUDED STALE TICKERS [{market_label.upper()}]</b>\n\n"
                f"Auto-excluded <b>{len(excluded_details)}</b> ticker(s):\n"
                f"{ticker_lines}\n\n"
                f"Expected data >= {expected}\n"
                f"Pipeline continuing with {freshness['fresh_count']} fresh tickers.\n\n"
                f"💡 These tickers will be retried weekly. "
                f"Check if delisted or renamed."
            )
            get_alert_manager().send(alert)
        except Exception as tg_exc:
            logger.warning("Could not send auto-exclusion Telegram alert: %s", tg_exc)

        return True  # Pipeline continues

    # Systemic stale data — may need to halt
    logger.warning(
        "SYSTEMIC stale data [%s]: %d/%d (%.1f%%) stale. "
        "This suggests a data provider issue, not individual ticker problems.",
        market_label, stale_count, total_checked, stale_pct,
    )

    # Send Telegram alert for systemic issue
    try:
        from alerting import get_alert_manager
        stale_sample = stale_tickers[:10]
        halt = True  # safe default
        if config:
            halt = config.get("trading", {}).get(
                "live_safety", {}
            ).get("halt_on_stale_data", True)

        alert = (
            f"🛑 <b>SYSTEMIC STALE DATA [{market_label.upper()}]</b>\n\n"
            f"Stale tickers: <b>{stale_count}/{total_checked}</b> ({stale_pct:.1f}%)\n"
            f"Expected data >= {expected}\n"
            f"Oldest latest date: {oldest}\n"
            f"Sample: {stale_sample}\n\n"
        )
        if halt:
            alert += "🛑 Pipeline HALTED (halt_on_stale_data=true)\n"
            alert += "This looks like a data provider outage, not individual delistings."
        else:
            alert += "⚡ Continuing despite systemic stale data (halt_on_stale_data=false)"
        get_alert_manager().send(alert)
    except Exception as tg_exc:
        logger.warning("Could not send stale data Telegram alert: %s", tg_exc)

    # Decide whether to halt
    halt = True  # safe default
    if config:
        halt = config.get("trading", {}).get(
            "live_safety", {}
        ).get("halt_on_stale_data", True)

    if halt:
        raise RuntimeError(
            f"SYSTEMIC STALE DATA: {stale_count}/{total_checked} tickers ({stale_pct:.1f}%) "
            f"have data older than {expected}. This suggests a data provider issue. "
            "Set halt_on_stale_data=false in config to continue."
        )

    logger.warning("Continuing pipeline with systemic stale data (halt_on_stale_data=false)")
    return False



# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse
    import sys

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    parser = argparse.ArgumentParser(
        description="Atlas data ingestion CLI",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python3 -m data.ingest --universe sector_etfs
  python3 -m data.ingest --universe all_etfs
  python3 -m data.ingest --universe all_etfs --start 2019-01-01
  python3 -m data.ingest --universe gold_etfs --force
        """,
    )
    parser.add_argument(
        "--universe",
        required=True,
        help=(
            "Universe to ingest: one of the 6 named universes from definitions.py, "
            "or 'all_etfs' to ingest all 5 static ETF universes."
        ),
    )
    parser.add_argument("--start", dest="start_date", default=None, help="Start date YYYY-MM-DD (default: 7 years ago)")
    parser.add_argument("--end", dest="end_date", default=None, help="End date YYYY-MM-DD (default: today)")
    parser.add_argument("--force", action="store_true", default=False, help="Bypass parquet cache and re-fetch from API")

    args = parser.parse_args()

    if args.universe == "all_etfs":
        result = ingest_all_etf_universes(
            start_date=args.start_date,
            end_date=args.end_date,
            force=args.force,
        )
        print("\n=== Summary ===")
        print(f"Universes ingested : {result['universes_ingested']}")
        print(f"Total rows written : {result['total_rows_written']}")
        print(f"Total failed       : {result['total_tickers_failed']}")
    else:
        result = ingest_universe(
            args.universe,
            start_date=args.start_date,
            end_date=args.end_date,
            force=args.force,
        )
        print("\n=== Summary ===")
        print(f"Universe       : {result['universe']}")
        print(f"Tickers fetched: {len(result['tickers_fetched'])}")
        print(f"Tickers failed : {result['tickers_failed']}")
        print(f"Rows written   : {result['rows_written']}")

    sys.exit(0 if not result.get("tickers_failed") else 1)
