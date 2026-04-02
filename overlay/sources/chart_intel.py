"""
overlay.sources.chart_intel — Technical chart analysis from cached OHLCV data.

Uses parquet files already cached in data/cache/.  No live network calls.
For ETFs not in the sp500 cache directory, the module searches sibling
directories (sector_etfs, gold_etfs, treasury_etfs, commodity_etfs) before
falling back to a yfinance download with a 24-hour on-disk cache.

Public API
----------
    get_chart_analysis(tickers=None) -> dict
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# ── Paths ────────────────────────────────────────────────────────────────────


def _find_atlas_root() -> Path:
    """
    Walk up the directory tree from this file to find the Atlas project root.

    The root is the first directory that contains ``data/cache``.  This
    handles both the main worktree and swarm builder worktrees (which don't
    have a local data/ copy).
    """
    candidate = Path(__file__).resolve()
    for _ in range(10):
        candidate = candidate.parent
        if (candidate / "data" / "cache").exists():
            return candidate
    # Fallback: three levels up from this file
    return Path(__file__).resolve().parent.parent.parent


_ATLAS_ROOT = _find_atlas_root()
_CACHE_ROOT = _ATLAS_ROOT / "data" / "cache"

# Sub-directories searched in order for a ticker's parquet file.
_CACHE_SUBDIRS = [
    "sp500",
    "sector_etfs",
    "gold_etfs",
    "commodity_etfs",
    "treasury_etfs",
    "defensive_etfs",
    "asx",
]

# Default tickers to analyse when none are supplied.
_DEFAULT_TICKERS = ["SPY", "QQQ", "IWM", "XLF", "XLE", "GLD", "TLT"]

# Minimum trading days of history required for indicator computation.
_MIN_ROWS = 60

# 24-hour cache TTL for yfinance fallback downloads (seconds).
_YF_CACHE_TTL = 86_400


# ── Parquet helpers ──────────────────────────────────────────────────────────

def _find_parquet(ticker: str) -> Optional[Path]:
    """Return the first existing parquet path across all cache sub-directories."""
    for subdir in _CACHE_SUBDIRS:
        p = _CACHE_ROOT / subdir / f"{ticker}.parquet"
        if p.exists():
            return p
    return None


def _load_ohlcv(ticker: str) -> Optional[pd.DataFrame]:
    """
    Load OHLCV data for *ticker*.

    Priority:
        1. Parquet from any cache sub-directory (no network).
        2. yfinance download, persisted to data/cache/sp500/{ticker}.parquet
           with a 24-hour TTL.

    Returns a DataFrame with columns [open, high, low, close, volume] indexed
    by date (ascending), or None on failure.
    """
    path = _find_parquet(ticker)
    if path is not None:
        try:
            df = pd.read_parquet(path)
            df = _normalise_df(df)
            if len(df) >= _MIN_ROWS:
                return df
            logger.warning("chart_intel: %s parquet too short (%d rows)", ticker, len(df))
        except Exception as exc:
            logger.warning("chart_intel: failed to read parquet for %s — %s", ticker, exc)

    # ── yfinance fallback (ETFs not in any cache dir) ──────────────────────
    fallback_path = _CACHE_ROOT / "sp500" / f"{ticker}.parquet"
    if fallback_path.exists():
        import time
        age = time.time() - fallback_path.stat().st_mtime
        if age < _YF_CACHE_TTL:
            try:
                df = pd.read_parquet(fallback_path)
                df = _normalise_df(df)
                if len(df) >= _MIN_ROWS:
                    logger.debug("chart_intel: %s loaded from yf fallback cache", ticker)
                    return df
            except Exception as exc:
                logger.warning("chart_intel: yf cache read failed for %s — %s", ticker, exc)

    # Attempt live download and cache result.
    try:
        import yfinance as yf  # only imported when needed

        logger.info("chart_intel: downloading %s from yfinance (fallback)", ticker)
        raw = yf.download(ticker, period="6mo", auto_adjust=True, progress=False)
        if raw.empty:
            logger.warning("chart_intel: yfinance returned empty data for %s", ticker)
            return None

        # yfinance may return MultiIndex columns — flatten to simple lowercase strings.
        if isinstance(raw.columns, pd.MultiIndex):
            raw.columns = [c[0].lower() for c in raw.columns]
        else:
            raw.columns = [c.lower() if isinstance(c, str) else str(c).lower() for c in raw.columns]
        raw.index.name = "date"
        raw.index = pd.to_datetime(raw.index)
        raw = raw.sort_index()

        # Persist so future calls within 24h skip the download.
        fallback_path.parent.mkdir(parents=True, exist_ok=True)
        raw.to_parquet(fallback_path)

        df = _normalise_df(raw)
        return df if len(df) >= _MIN_ROWS else None

    except Exception as exc:
        logger.error("chart_intel: yfinance download failed for %s — %s", ticker, exc)
        return None


def _normalise_df(df: pd.DataFrame) -> pd.DataFrame:
    """
    Normalise a raw parquet DataFrame to a consistent schema.

    Expected input columns (case-insensitive): close, high, low, open, volume.
    Returns DataFrame sorted ascending by date index, extra columns dropped.
    """
    df = df.copy()
    df.columns = [c.lower() for c in df.columns]
    required = {"open", "high", "low", "close", "volume"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"Missing columns: {missing}")

    df = df[["open", "high", "low", "close", "volume"]]
    df.index = pd.to_datetime(df.index)
    df.index.name = "date"
    df = df.sort_index()
    df = df.dropna(subset=["close"])
    return df


# ── Technical indicator helpers ──────────────────────────────────────────────

def _sma(series: pd.Series, period: int) -> pd.Series:
    return series.rolling(period, min_periods=period).mean()


def _compute_rsi(close: pd.Series, period: int = 14) -> float:
    """Wilder-smoothed RSI — returns the most recent value (0-100)."""
    delta = close.diff().dropna()
    gains = delta.clip(lower=0)
    losses = (-delta).clip(lower=0)
    # Use Wilder's EMA (alpha = 1/period)
    avg_gain = gains.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    avg_loss = losses.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    last_gain = avg_gain.iloc[-1]
    last_loss = avg_loss.iloc[-1]
    if last_loss == 0:
        return 100.0
    rs = last_gain / last_loss
    return float(100.0 - (100.0 / (1.0 + rs)))


def _rsi_status(rsi: float) -> str:
    if rsi >= 70:
        return "overbought"
    if rsi <= 30:
        return "oversold"
    return "neutral"


def _swing_support_resistance(df: pd.DataFrame, window: int = 20) -> tuple[float, float]:
    """
    Simple swing-based support/resistance over the last *window* trading days.

    Support  — minimum of lows in the window.
    Resistance — maximum of highs in the window.
    """
    recent = df.tail(window)
    support = float(recent["low"].min())
    resistance = float(recent["high"].max())
    return support, resistance


def _volume_ratio(df: pd.DataFrame, avg_window: int = 20) -> float:
    """Current day volume / 20-day average volume."""
    avg_vol = df["volume"].rolling(avg_window, min_periods=avg_window).mean()
    last_avg = avg_vol.iloc[-1]
    if last_avg == 0 or np.isnan(last_avg):
        return 1.0
    return float(df["volume"].iloc[-1] / last_avg)


def _momentum_20d(close: pd.Series) -> float:
    """Rate of change over the last 20 trading days (as a decimal, not %)."""
    if len(close) < 21:
        return 0.0
    past = close.iloc[-21]
    current = close.iloc[-1]
    if past == 0 or np.isnan(past):
        return 0.0
    return float((current - past) / past)


def _trend_label(close_last: float, sma20: float, sma50: float, sma200: float) -> str:
    """Classify trend as bullish / bearish / neutral."""
    above_50 = not np.isnan(sma50) and close_last > sma50
    above_200 = not np.isnan(sma200) and close_last > sma200
    above_20 = not np.isnan(sma20) and close_last > sma20

    bullish_count = sum([above_20, above_50, above_200])
    if bullish_count >= 2:
        return "bullish"
    if bullish_count == 0:
        return "bearish"
    return "neutral"


# ── Per-ticker analysis ──────────────────────────────────────────────────────

def _analyse_ticker(ticker: str) -> Optional[dict]:
    """
    Run technical analysis for a single ticker.

    Returns a dict with keys matching the documented schema, or None if data
    is unavailable / insufficient.
    """
    df = _load_ohlcv(ticker)
    if df is None or len(df) < _MIN_ROWS:
        logger.warning("chart_intel: insufficient data for %s (got %s rows)", ticker, len(df) if df is not None else 0)
        return None

    close = df["close"]
    last_close = float(close.iloc[-1])

    # ── Moving averages ──
    sma20_series = _sma(close, 20)
    sma50_series = _sma(close, 50)
    sma200_series = _sma(close, 200)

    sma20 = float(sma20_series.iloc[-1]) if not sma20_series.isna().iloc[-1] else float("nan")
    sma50 = float(sma50_series.iloc[-1]) if not sma50_series.isna().iloc[-1] else float("nan")
    sma200 = float(sma200_series.iloc[-1]) if not sma200_series.isna().iloc[-1] else float("nan")

    above_20sma = not np.isnan(sma20) and last_close > sma20
    above_50sma = not np.isnan(sma50) and last_close > sma50
    above_200sma = not np.isnan(sma200) and last_close > sma200

    trend = _trend_label(last_close, sma20, sma50, sma200)

    # ── RSI ──
    rsi = _compute_rsi(close, period=14) if len(close) >= 28 else float("nan")
    rsi_stat = _rsi_status(rsi) if not np.isnan(rsi) else "neutral"

    # ── Support / Resistance ──
    support, resistance = _swing_support_resistance(df, window=20)

    # ── Volume ratio ──
    vol_ratio = _volume_ratio(df, avg_window=20)

    # ── Momentum ──
    mom_20d = _momentum_20d(close)

    return {
        "trend": trend,
        "above_200sma": above_200sma,
        "above_50sma": above_50sma,
        "above_20sma": above_20sma,
        "sma20": round(sma20, 2) if not np.isnan(sma20) else None,
        "sma50": round(sma50, 2) if not np.isnan(sma50) else None,
        "sma200": round(sma200, 2) if not np.isnan(sma200) else None,
        "rsi": round(rsi, 1) if not np.isnan(rsi) else None,
        "rsi_status": rsi_stat,
        "volume_ratio": round(vol_ratio, 2),
        "momentum_20d": round(mom_20d, 4),
        "support": round(support, 2),
        "resistance": round(resistance, 2),
        "last_close": round(last_close, 2),
    }


# ── Summary narrative ────────────────────────────────────────────────────────

def _build_summary(results: dict) -> str:
    """
    Generate a one-sentence plain-English summary of the aggregate picture.

    Uses SPY as the primary reference, then counts bullish/bearish/neutral
    across all tickers and checks whether volume is confirming.
    """
    spy = results.get("spy")
    if spy is None:
        # Fallback: count across available results
        ticker_results = {k: v for k, v in results.items() if isinstance(v, dict)}
        if not ticker_results:
            return "Insufficient data for market summary"
        bullish = sum(1 for v in ticker_results.values() if v.get("trend") == "bullish")
        total = len(ticker_results)
        if bullish >= total * 0.6:
            return f"Broadly bullish ({bullish}/{total} tickers)"
        if bullish <= total * 0.3:
            return f"Broadly bearish ({bullish}/{total} tickers bullish)"
        return f"Mixed market conditions ({bullish}/{total} tickers bullish)"

    # SPY-anchored summary
    spy_trend = spy["trend"]
    above_200 = spy.get("above_200sma", False)
    above_50 = spy.get("above_50sma", False)
    rsi_stat = spy.get("rsi_status", "neutral")
    vol_ratio = spy.get("volume_ratio", 1.0)

    ticker_results = {k: v for k, v in results.items() if isinstance(v, dict) and k != "spy"}
    bullish_count = sum(1 for v in ticker_results.values() if v.get("trend") == "bullish")
    total_others = len(ticker_results)

    parts = []
    if spy_trend == "bullish":
        ma_desc = []
        if above_200:
            ma_desc.append("200")
        if above_50:
            ma_desc.append("50")
        if ma_desc:
            parts.append(f"SPY above {'/'.join(ma_desc)}SMA")
        else:
            parts.append("SPY trending bullish")
    elif spy_trend == "bearish":
        parts.append("SPY in downtrend")
    else:
        parts.append("SPY neutral/consolidating")

    if rsi_stat == "overbought":
        parts.append("RSI overbought — watch for pullback")
    elif rsi_stat == "oversold":
        parts.append("RSI oversold — potential bounce")

    if vol_ratio > 1.3:
        parts.append("high volume confirming move")
    elif vol_ratio < 0.7:
        parts.append("low-volume — conviction suspect")

    if total_others > 0:
        if bullish_count >= int(total_others * 0.6):
            parts.append(f"broad participation ({bullish_count}/{total_others} ETFs bullish)")
        elif bullish_count <= int(total_others * 0.3):
            parts.append(f"limited breadth ({bullish_count}/{total_others} ETFs bullish)")

    # Prefix based on SPY trend
    prefix_map = {
        "bullish": "Broadly bullish",
        "bearish": "Broadly bearish",
        "neutral": "Mixed market",
    }
    prefix = prefix_map.get(spy_trend, "Mixed market")
    detail = ", ".join(parts)
    return f"{prefix} — {detail}" if detail else prefix


# ── Public API ───────────────────────────────────────────────────────────────

def get_chart_analysis(tickers: Optional[list[str]] = None) -> dict:
    """
    Compute technical chart analysis for the given tickers.

    Parameters
    ----------
    tickers : list[str] or None
        Ticker symbols to analyse.  Defaults to SPY, QQQ, IWM, XLF, XLE,
        GLD, TLT.

    Returns
    -------
    dict
        Keys are lower-cased ticker symbols, each mapping to a dict with:
            trend, above_200sma, above_50sma, rsi, rsi_status, volume_ratio,
            momentum_20d, support, resistance, last_close
        Plus a ``"summary"`` key with a plain-English narrative string.

    This function never raises — if a ticker fails, it is omitted from the
    result dict.  If *all* tickers fail, the dict contains only ``"summary"``.
    """
    if tickers is None:
        tickers = _DEFAULT_TICKERS

    results: dict = {}
    for ticker in tickers:
        key = ticker.lower()
        try:
            analysis = _analyse_ticker(ticker)
            if analysis is not None:
                results[key] = analysis
            else:
                logger.warning("chart_intel: no analysis produced for %s", ticker)
        except Exception as exc:
            logger.error("chart_intel: unexpected error for %s — %s", ticker, exc)

    results["summary"] = _build_summary(results)
    return results
