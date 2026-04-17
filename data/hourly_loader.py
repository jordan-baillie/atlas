"""
hourly_loader.py — Load 1-hour OHLCV bars for a ticker.

Public API:
    load_hourly(ticker, days) -> pd.DataFrame | None

Cache location: data/cache/hourly/<TICKER>.parquet

Cache validity rules:
  - During market hours (09:30–16:00 ET):  reuse if mtime < 1 h
  - Outside market hours:                  reuse if file is from today
  - Always refresh if cache is absent

Data source: Alpaca StockBarsRequest with TimeFrame.Hour via the
existing singleton client (brokers/alpaca/market_data.py).
Timestamps are preserved at full hourly resolution (NOT .normalize()).
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, date
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

HOURLY_CACHE = Path(__file__).parent / "cache" / "hourly"
_ET = ZoneInfo("America/New_York")
_MARKET_OPEN_HOUR = 9
_MARKET_OPEN_MINUTE = 30
_MARKET_CLOSE_HOUR = 16
_CACHE_STALE_MARKET_HOURS = 3600  # 1 h during market hours


# ---------------------------------------------------------------------------
# Cache helpers
# ---------------------------------------------------------------------------


def _cache_path(ticker: str) -> Path:
    return HOURLY_CACHE / f"{ticker}.parquet"


def _is_market_hours() -> bool:
    """Return True when US equity market is currently open."""
    now = datetime.now(_ET)
    open_t = now.replace(hour=_MARKET_OPEN_HOUR, minute=_MARKET_OPEN_MINUTE, second=0, microsecond=0)
    close_t = now.replace(hour=_MARKET_CLOSE_HOUR, minute=0, second=0, microsecond=0)
    return open_t <= now <= close_t


def _cache_valid(path: Path) -> bool:
    """Return True when the cached parquet is fresh enough to reuse."""
    if not path.exists():
        return False
    mtime = path.stat().st_mtime
    age = time.time() - mtime
    if _is_market_hours():
        return age < _CACHE_STALE_MARKET_HOURS
    # Outside market hours: valid if written today
    mtime_date = datetime.fromtimestamp(mtime).date()
    return mtime_date >= date.today()


# ---------------------------------------------------------------------------
# Fetch from Alpaca
# ---------------------------------------------------------------------------


def _fetch_alpaca(ticker: str, days: int) -> pd.DataFrame | None:
    """Fetch hourly bars from Alpaca for the last *days* calendar days."""
    try:
        from brokers.alpaca.market_data import get_alpaca_data_client
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame
    except ImportError as exc:
        logger.warning("hourly_loader: Alpaca SDK not available — %s", exc)
        return None

    client = get_alpaca_data_client()
    if client is None or not getattr(client, "is_available", False):
        logger.warning("hourly_loader: Alpaca client unavailable")
        return None

    end_dt = datetime.now()
    start_dt = end_dt - timedelta(days=days + 1)  # +1 buffer for timezone seams

    try:
        req = StockBarsRequest(
            symbol_or_symbols=[ticker],
            timeframe=TimeFrame.Hour,
            start=start_dt.strftime("%Y-%m-%d"),
            end=end_dt.strftime("%Y-%m-%d"),
        )
        barset = client._client.get_stock_bars(req)
        barset_data = getattr(barset, "data", None) or (barset if isinstance(barset, dict) else {})
        bars = barset_data.get(ticker, [])
    except Exception as exc:
        logger.warning("hourly_loader: Alpaca fetch failed for %s — %s", ticker, exc)
        return None

    if not bars:
        logger.warning("hourly_loader: Alpaca returned 0 hourly bars for %s", ticker)
        return None

    rows = []
    for bar in bars:
        ts = getattr(bar, "timestamp", None)
        if ts is None:
            continue
        # Preserve full timestamp — do NOT normalize()
        ts_naive = pd.Timestamp(ts)
        if ts_naive.tzinfo is not None:
            ts_naive = ts_naive.tz_convert("UTC").tz_localize(None)
        rows.append({
            "timestamp": ts_naive,
            "open":   float(getattr(bar, "open",   0) or 0),
            "high":   float(getattr(bar, "high",   0) or 0),
            "low":    float(getattr(bar, "low",    0) or 0),
            "close":  float(getattr(bar, "close",  0) or 0),
            "volume": int(getattr(bar,   "volume", 0) or 0),
            "ticker": ticker,
        })

    if not rows:
        logger.warning("hourly_loader: no usable bars after parsing for %s", ticker)
        return None

    df = pd.DataFrame(rows).set_index("timestamp")
    df.index.name = "timestamp"
    df = df.sort_index()
    logger.info(
        "hourly_loader: fetched %d hourly bars for %s (%s → %s)",
        len(df), ticker,
        str(df.index[0])[:19],
        str(df.index[-1])[:19],
    )
    return df


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def load_hourly(ticker: str, days: int = 30) -> pd.DataFrame | None:
    """
    Load the last *days* days of 1-hour bars for *ticker*.

    Cache policy:
    - Hit (fresh):  return cached parquet immediately.
    - Miss / stale: fetch from Alpaca, update cache, return.
    - Failure:      log WARNING, return None (never raises).

    Returns a DataFrame with columns [open, high, low, close, volume, ticker]
    and a UTC-naive DatetimeIndex named 'timestamp' at hourly resolution.
    """
    path = _cache_path(ticker)

    if _cache_valid(path):
        try:
            df = pd.read_parquet(path)
            logger.debug("hourly_loader: cache hit for %s (%d rows)", ticker, len(df))
            # Trim to requested window to keep callers honest
            cutoff = pd.Timestamp.utcnow().tz_localize(None) - pd.Timedelta(days=days)
            return df[df.index >= cutoff]
        except Exception as exc:
            logger.warning("hourly_loader: cache read failed for %s — %s", ticker, exc)

    df = _fetch_alpaca(ticker, days=days)
    if df is None:
        return None

    # Atomic write
    HOURLY_CACHE.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp.parquet")
    try:
        df.to_parquet(tmp, compression="snappy")
        tmp.rename(path)
    except Exception as exc:
        logger.warning("hourly_loader: cache write failed for %s — %s", ticker, exc)
        tmp.unlink(missing_ok=True)
        # Return data anyway even if we couldn't cache
        return df

    return df
