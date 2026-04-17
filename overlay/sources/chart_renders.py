"""
chart_renders.py — mplfinance-based chart renderer for Atlas overlay vision.

Public API:
    render_daily_1y(ticker, out_path) -> Path
    render_hourly_1w(ticker, out_path) -> Path
    render_reference_set(positions, out_dir, max_images) -> dict[str, Path]
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, date
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CACHE_ROOT = Path("/root/atlas/data/cache")
CHART_IMAGE_ROOT = CACHE_ROOT / "chart_images"
CHART_IMAGE_ROOT.mkdir(parents=True, exist_ok=True)

# Search order for ticker parquets
_SEARCH_DIRS = [
    CACHE_ROOT / "sector_etfs",
    CACHE_ROOT / "sp500",
    CACHE_ROOT / "defensive_etfs",
    CACHE_ROOT / "gold_etfs",
    CACHE_ROOT / "treasury_etfs",
    CACHE_ROOT / "commodity_etfs",
    CACHE_ROOT / "indices",  # VIX and other index benchmarks
]

_ET = ZoneInfo("America/New_York")

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _find_parquet(ticker: str) -> Path | None:
    """Search canonical directories for <TICKER>.parquet, return first match."""
    for d in _SEARCH_DIRS:
        candidate = d / f"{ticker}.parquet"
        if candidate.exists():
            return candidate
    return None


def _is_market_hours() -> bool:
    """Return True when US market is currently open (09:30–16:00 ET, any day)."""
    now = datetime.now(_ET)
    market_open = now.replace(hour=9, minute=30, second=0, microsecond=0)
    market_close = now.replace(hour=16, minute=0, second=0, microsecond=0)
    return market_open <= now <= market_close


def _cache_valid(path: Path) -> bool:
    """
    Return True when we should reuse an existing cached image.

    Rules:
    - If file doesn't exist → False
    - Parent dir must match today's YYYY-MM-DD (the dir scheme we use)
    - During market hours: reuse only if mtime < 4 h
    - Outside market hours: reuse if file exists for today's date
    """
    if not path.exists():
        return False
    today_str = date.today().isoformat()
    if path.parent.name != today_str:
        return False
    if _is_market_hours():
        age_secs = time.time() - path.stat().st_mtime
        return age_secs < 4 * 3600
    return True  # outside market hours, any today file is fine


def _load_parquet(ticker: str) -> pd.DataFrame | None:
    """Load parquet, return None (with WARNING) if missing or unreadable."""
    path = _find_parquet(ticker)
    if path is None:
        log.warning("chart_renders: no parquet found for %s — skipping", ticker)
        return None
    try:
        df = pd.read_parquet(path)
    except Exception as exc:
        log.warning("chart_renders: failed to read %s: %s — skipping", path, exc)
        return None
    if df.empty:
        log.warning("chart_renders: parquet for %s is empty — skipping", ticker)
        return None
    return df


def _normalise_df(df: pd.DataFrame) -> pd.DataFrame:
    """
    Normalise a price DataFrame for mplfinance.

    - Renames lowercase (open/high/low/close/volume) → title-case.
    - Ensures DatetimeIndex named 'Date'.
    - Drops rows with NaN in OHLC columns.
    - Drops extraneous columns (e.g. 'ticker').
    """
    rename_map = {
        "open": "Open", "high": "High", "low": "Low",
        "close": "Close", "volume": "Volume",
    }
    df = df.copy()
    df.columns = [rename_map.get(c.lower(), c) for c in df.columns]

    # Keep only known price columns
    keep = [c for c in ["Open", "High", "Low", "Close", "Volume"] if c in df.columns]
    df = df[keep]

    # Ensure datetime index
    if not isinstance(df.index, pd.DatetimeIndex):
        df.index = pd.to_datetime(df.index)
    df.index.name = "Date"

    df = df.dropna(subset=["Open", "High", "Low", "Close"])
    df = df.sort_index()
    return df


def _default_out_path(ticker: str, timeframe: str) -> Path:
    today = date.today().isoformat()
    d = CHART_IMAGE_ROOT / today
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{ticker}_{timeframe}.png"


def _render(df: pd.DataFrame, out_path: Path) -> Path:
    """Render a prepared OHLCV DataFrame to PNG via mplfinance."""
    import mplfinance as mpf  # localised import — keep heavy dep out of module init

    out_path.parent.mkdir(parents=True, exist_ok=True)

    # mplfinance volume kwarg MUST be a Python bool (not numpy.bool_)
    has_volume = bool(
        "Volume" in df.columns and df["Volume"].notna().any() and (df["Volume"] > 0).any()
    )

    mpf.plot(
        df,
        type="candle",
        volume=has_volume,
        style="yahoo",
        figsize=(20, 12),
        savefig=dict(fname=str(out_path), dpi=200),
    )
    log.info("chart_renders: saved %s", out_path)
    return out_path


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def render_daily_1y(ticker: str, out_path: Path) -> Path:
    """
    Render a 1-year daily candlestick chart for *ticker* to *out_path*.

    Returns *out_path* on success.  Raises ValueError if data unavailable.
    """
    if _cache_valid(out_path):
        log.info("chart_renders: cache hit for %s → %s", ticker, out_path)
        return out_path

    df_raw = _load_parquet(ticker)
    if df_raw is None:
        raise ValueError(f"No parquet data for {ticker}")

    df = _normalise_df(df_raw)
    df = df.tail(252)  # last ~252 trading days ≈ 1 year

    if len(df) < 2:
        raise ValueError(f"Insufficient data for {ticker} ({len(df)} rows after normalise)")

    return _render(df, out_path)


def render_hourly_1w(ticker: str, out_path: Path) -> Path:
    """
    Render a 1-week hourly chart for *ticker* to *out_path*.

    Strategy:
    1. Try load_hourly(ticker, days=7) for real 1-h bars (Alpaca).
    2. On success → normalise + render candles directly.
    3. On failure → fall back to last 60 days of daily data (logged at INFO).
    """
    if _cache_valid(out_path):
        log.info("chart_renders: cache hit for %s → %s", ticker, out_path)
        return out_path

    # ── Attempt 1: real hourly bars ───────────────────────────────────────
    try:
        from data.hourly_loader import load_hourly
        df_hourly = load_hourly(ticker, days=7)
        if df_hourly is not None and len(df_hourly) >= 2:
            df = _normalise_df(df_hourly)
            log.info(
                "chart_renders: rendering %d hourly bars for %s",
                len(df), ticker,
            )
            return _render(df, out_path)
    except Exception as exc:
        log.info(
            "chart_renders: hourly unavailable for %s — %s",
            ticker, exc,
        )

    # ── Attempt 2: 60-day daily fallback ─────────────────────────────────
    log.info(
        "chart_renders: hourly unavailable for %s — falling back to daily tail",
        ticker,
    )
    df_raw = _load_parquet(ticker)
    if df_raw is None:
        raise ValueError(f"No parquet data for {ticker}")

    df = _normalise_df(df_raw)
    df = df.tail(60)  # 60-day daily fallback

    if len(df) < 2:
        raise ValueError(f"Insufficient data for {ticker} ({len(df)} rows after normalise)")

    return _render(df, out_path)


def render_reference_set(
    positions: list[str] | None = None,
    out_dir: Path | None = None,
    max_images: int = 10,
) -> dict[str, Path]:
    """
    Render a standard reference image set and return a dict of {key: Path}.

    Always attempts: SPY (daily_1y), QQQ (daily_1y), VIX (daily_1y).
    Then renders each position ticker as daily_1y + hourly_1w.
    Caps total images at *max_images* (indices first, then positions).
    Missing parquets → WARNING + omit (no exception raised).

    Returns dict keyed by "<TICKER>_<timeframe>".
    """
    positions = list(positions) if positions else []

    if out_dir is None:
        today = date.today().isoformat()
        out_dir = CHART_IMAGE_ROOT / today
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    result: dict[str, Path] = {}

    # ---- Index benchmarks (highest priority) ----
    index_jobs: list[tuple[str, str, object]] = [
        ("SPY", "daily_1y", render_daily_1y),
        ("QQQ", "daily_1y", render_daily_1y),
        ("VIX", "daily_1y", render_daily_1y),
    ]

    for ticker, timeframe, fn in index_jobs:
        if len(result) >= max_images:
            break
        key = f"{ticker}_{timeframe}"
        out_path = out_dir / f"{ticker}_{timeframe}.png"
        try:
            fn(ticker, out_path)  # type: ignore[operator]
            result[key] = out_path
        except Exception as exc:
            log.warning("chart_renders: skipping %s — %s", key, exc)

    # ---- Position tickers (daily + hourly) ----
    for ticker in positions:
        for timeframe, fn in [("daily_1y", render_daily_1y), ("hourly_1w", render_hourly_1w)]:
            if len(result) >= max_images:
                break
            key = f"{ticker}_{timeframe}"
            out_path = out_dir / f"{ticker}_{timeframe}.png"
            try:
                fn(ticker, out_path)  # type: ignore[operator]
                result[key] = out_path
            except Exception as exc:
                log.warning("chart_renders: skipping %s — %s", key, exc)

    return result
