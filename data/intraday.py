"""Intraday OHLCV data — 15-minute bars via Alpaca for entry timing.

Downloads pre-market and regular session bars for planned tickers.
Caches in parquet with 5-day retention.
"""
import logging
import os
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional

import pandas as pd

logger = logging.getLogger(__name__)

CACHE_DIR = Path(__file__).parent / "cache" / "intraday"
RETENTION_DAYS = 5


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def download_intraday_bars(
    tickers: List[str],
    lookback_days: int = 2,
    timeframe: str = "15Min",
    config: Optional[dict] = None,
) -> Dict[str, pd.DataFrame]:
    """Download 15-minute bars for tickers via Alpaca.

    Returns DataFrames with columns: open, high, low, close, volume.
    Index is a tz-aware (America/New_York) DatetimeIndex named 'timestamp'.
    Handles rate limiting and per-ticker parquet caching.

    Args:
        tickers:      Atlas-format US tickers.
        lookback_days: How many calendar days to look back (default 2).
        timeframe:    Bar resolution — "15Min" (default), "1Min", "1Hour".
        config:       Atlas config dict (for Alpaca credentials).

    Returns:
        Dict of ticker → DataFrame.  Tickers with no data are omitted.
    """
    if not tickers:
        return {}

    config = config or {}
    today_str = datetime.now().strftime("%Y-%m-%d")
    end_date = datetime.now()
    start_date = end_date - timedelta(days=lookback_days + 1)
    start_str = start_date.strftime("%Y-%m-%d")
    end_str = end_date.strftime("%Y-%m-%d")

    result: Dict[str, pd.DataFrame] = {}
    tickers_to_fetch: List[str] = []

    # Cache check — skip network if we already have today's bars
    for ticker in tickers:
        cached = get_cached_bars(ticker, today_str)
        if cached is not None:
            result[ticker] = cached
        else:
            tickers_to_fetch.append(ticker)

    if tickers_to_fetch:
        downloaded = _download_via_alpaca(
            tickers=tickers_to_fetch,
            start_date=start_str,
            end_date=end_str,
            timeframe=timeframe,
            config=config,
        )
        for ticker, df in downloaded.items():
            if not df.empty:
                _cache_bars(ticker, today_str, df)
                result[ticker] = df
                logger.info("Intraday bars downloaded: %s (%d bars)", ticker, len(df))

    cleanup_old_cache()
    return result


def get_cached_bars(ticker: str, date_str: str) -> Optional[pd.DataFrame]:
    """Get cached intraday bars for a ticker/date.

    Returns DataFrame or None if cache miss.
    """
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = CACHE_DIR / f"{ticker}_{date_str}.parquet"
    if path.exists():
        try:
            df = pd.read_parquet(path)
            logger.debug("Cache hit: %s %s", ticker, date_str)
            return df
        except Exception as e:
            logger.warning("Cache read failed for %s: %s", ticker, e)
    return None


def cleanup_old_cache():
    """Remove intraday cache files older than RETENTION_DAYS."""
    if not CACHE_DIR.exists():
        return
    cutoff = datetime.now() - timedelta(days=RETENTION_DAYS)
    removed = 0
    for path in CACHE_DIR.glob("*.parquet"):
        try:
            if datetime.fromtimestamp(path.stat().st_mtime) < cutoff:
                path.unlink()
                removed += 1
        except Exception as e:
            logger.debug("Could not clean cache file %s: %s", path, e)
    if removed:
        logger.info("Cleaned %d old intraday cache files", removed)


def get_opening_range(bars: pd.DataFrame, minutes: int = 30) -> Dict[str, float]:
    """Compute opening range from intraday bars.

    Selects bars within the first *minutes* of market open (9:30 ET).

    Args:
        bars:    DataFrame with columns open/high/low and a DatetimeIndex
                 (preferably tz-aware America/New_York).
        minutes: Opening range window in minutes (default 30).

    Returns:
        {"open": float, "high_30m": float, "low_30m": float, "range": float}
    """
    if bars is None or bars.empty:
        return {"open": 0.0, "high_30m": 0.0, "low_30m": 0.0, "range": 0.0}

    # Market open = 9:30 ET → 570 minutes from midnight
    _MARKET_OPEN_MIN = 9 * 60 + 30  # 570
    cutoff_min = _MARKET_OPEN_MIN + minutes  # e.g. 600 for 30-min range

    if hasattr(bars.index, "hour"):
        bar_minutes = bars.index.hour * 60 + bars.index.minute
        market_bars = bars[
            (bar_minutes >= _MARKET_OPEN_MIN) & (bar_minutes < cutoff_min)
        ]
    else:
        # Fallback: approximate by taking first N 15-min bars
        n_bars = max(1, minutes // 15)
        market_bars = bars.head(n_bars)

    if market_bars.empty:
        market_bars = bars.head(2)

    if market_bars.empty:
        return {"open": 0.0, "high_30m": 0.0, "low_30m": 0.0, "range": 0.0}

    h = float(market_bars["high"].max())
    l = float(market_bars["low"].min())
    return {
        "open":    float(market_bars.iloc[0]["open"]),
        "high_30m": h,
        "low_30m":  l,
        "range":    round(h - l, 6),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Internal helpers
# ─────────────────────────────────────────────────────────────────────────────

def _cache_bars(ticker: str, date_str: str, df: pd.DataFrame) -> None:
    """Write intraday bars to parquet cache (silent on error)."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = CACHE_DIR / f"{ticker}_{date_str}.parquet"
    try:
        df.to_parquet(path)
        logger.debug("Cached intraday bars: %s", path)
    except Exception as e:
        logger.warning("Cache write failed for %s: %s", ticker, e)


def _download_via_alpaca(
    tickers: List[str],
    start_date: str,
    end_date: str,
    timeframe: str,
    config: dict,
) -> Dict[str, pd.DataFrame]:
    """Download intraday bars from Alpaca, batched 50 tickers at a time."""
    try:
        from alpaca.data.historical import StockHistoricalDataClient
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
        from brokers.alpaca.mapper import to_alpaca_list, to_atlas
    except ImportError as e:
        logger.warning("Alpaca imports unavailable — skipping intraday download: %s", e)
        return {}

    # Resolve credentials: env vars → config
    api_key = (
        os.environ.get("ALPACA_API_KEY")
        or config.get("broker", {}).get("api_key", "")
        or config.get("alpaca", {}).get("api_key", "")
        or config.get("trading", {}).get("alpaca_key", "")
    )
    api_secret = (
        os.environ.get("ALPACA_API_SECRET")
        or config.get("broker", {}).get("api_secret", "")
        or config.get("alpaca", {}).get("api_secret", "")
        or config.get("trading", {}).get("alpaca_secret", "")
    )
    feed = config.get("alpaca", {}).get("feed", "iex")

    if not api_key or not api_secret:
        logger.warning("No Alpaca credentials found — skipping intraday download")
        return {}

    try:
        client = StockHistoricalDataClient(api_key=api_key, secret_key=api_secret)
    except Exception as e:
        logger.warning("AlpacaMarketData client init failed: %s", e)
        return {}

    # Timeframe mapping
    _tf_map = {
        "1Min":    TimeFrame.Minute,
        "1Minute": TimeFrame.Minute,
        "5Min":    TimeFrame(5,  TimeFrameUnit.Minute),
        "15Min":   TimeFrame(15, TimeFrameUnit.Minute),
        "30Min":   TimeFrame(30, TimeFrameUnit.Minute),
        "1Hour":   TimeFrame.Hour,
        "1Day":    TimeFrame.Day,
    }
    tf = _tf_map.get(timeframe)
    if tf is None:
        logger.warning("Unknown timeframe '%s', defaulting to 15Min", timeframe)
        tf = TimeFrame(15, TimeFrameUnit.Minute)

    BATCH_SIZE = 50
    batches = [tickers[i:i + BATCH_SIZE] for i in range(0, len(tickers), BATCH_SIZE)]
    result: Dict[str, pd.DataFrame] = {}

    for batch_idx, batch in enumerate(batches):
        if batch_idx > 0:
            time.sleep(0.3)  # rate-limit: free tier 200 req/min

        alpaca_symbols = to_alpaca_list(batch)

        try:
            req = StockBarsRequest(
                symbol_or_symbols=alpaca_symbols,
                timeframe=tf,
                start=start_date,
                end=end_date,
                feed=feed,
            )
            barset = client.get_stock_bars(req)
            barset_data = (
                getattr(barset, "data", None)
                or (barset if isinstance(barset, dict) else {})
            )

            for symbol, bars in (barset_data or {}).items():
                atlas_ticker = to_atlas(symbol)
                if not bars:
                    continue
                rows = []
                for bar in bars:
                    ts = getattr(bar, "timestamp", None)
                    if ts is None:
                        continue
                    rows.append({
                        "timestamp": pd.Timestamp(ts),
                        "open":   float(getattr(bar, "open",   0) or 0),
                        "high":   float(getattr(bar, "high",   0) or 0),
                        "low":    float(getattr(bar, "low",    0) or 0),
                        "close":  float(getattr(bar, "close",  0) or 0),
                        "volume": int(getattr(bar,   "volume", 0) or 0),
                    })
                if not rows:
                    continue
                df = pd.DataFrame(rows).set_index("timestamp")
                df.index.name = "timestamp"
                # Normalise to ET so get_opening_range's hour check works
                if df.index.tz is not None:
                    df.index = df.index.tz_convert("America/New_York")
                result[atlas_ticker] = df

        except Exception as e:
            logger.warning("Intraday batch %d fetch error: %s", batch_idx + 1, e)

    logger.info(
        "download_intraday_bars: completed %d/%d tickers",
        len(result), len(tickers),
    )
    return result
