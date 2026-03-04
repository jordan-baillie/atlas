"""
Atlas Macro Regime Module
==========================
Downloads and computes macro regime indicators used for position sizing
and entry filtering in the backtest engine.

Indicators:
    - ^VIX : CBOE Volatility Index
    - GC=F : Gold futures
    - HG=F : Copper futures (HG)
    - ^TNX : 10-Year Treasury Yield
    - ^IRX : 13-Week Treasury Bill Yield

Derived signals:
    - gold_copper_ratio  : GC/HG — proxy for risk appetite
    - gc_regime          : 1=risk-on, 2=neutral, 3=risk-off (expanding terciles)
    - vix_roc_5d         : 5-day % change in VIX
    - vix_spike          : bool, True when vix_roc_5d > threshold
    - yield_curve_10y_3m : yield_10y - yield_13w (positive = normal, negative = inverted)
    - yc_change_5d       : 5-day change in yield curve slope
    - yc_flattening      : bool, True when yc_change_5d < threshold (flattening/inverting)
    - macro_regime_scale : composite float multiplier (0.5 to 1.5)

Usage:
    from data.macro import download_macro_data, compute_macro_signals

    macro_raw = download_macro_data(start_date="2020-01-01")
    macro_signals = compute_macro_signals(macro_raw)

CRITICAL: All computations use backward-looking windows only (expanding
window for tercile classification, lagged ROC). Zero look-ahead bias.
"""

import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# Cache directory for macro data
PROJECT_ROOT = Path(__file__).resolve().parent.parent
MACRO_CACHE_DIR = PROJECT_ROOT / "data" / "cache" / "macro"
MACRO_CACHE_FILE = MACRO_CACHE_DIR / "macro_daily.parquet"

# Tickers to download
MACRO_TICKERS = {
    "vix": "^VIX",
    "gold": "GC=F",
    "copper": "HG=F",
    "yield_10y": "^TNX",
    "yield_13w": "^IRX",
}


def _cache_is_fresh(path: Path, max_age_hours: int = 24) -> bool:
    """Check if a cache file exists and is younger than max_age_hours."""
    if not path.exists():
        return False
    age = datetime.now() - datetime.fromtimestamp(path.stat().st_mtime)
    return age < timedelta(hours=max_age_hours)


def download_macro_data(
    start_date: str = "2000-01-01",
    use_cache: bool = True,
    cache_max_age_hours: int = 24,
) -> pd.DataFrame:
    """Download macro indicator data via yfinance.

    Downloads VIX, gold, copper, 10Y yield, and 13W yield.
    Caches result to data/cache/macro/macro_daily.parquet.

    Args:
        start_date: Start date string in YYYY-MM-DD format.
        use_cache:  If True, serve from cache when fresh (< cache_max_age_hours).
        cache_max_age_hours: Cache TTL in hours.

    Returns:
        DataFrame indexed by date with columns:
            vix, gold, copper, yield_10y, yield_13w
        NaN values are forward-filled then backward-filled to handle
        non-overlapping market calendars (e.g. futures vs equities).
    """
    MACRO_CACHE_DIR.mkdir(parents=True, exist_ok=True)

    # Serve from cache if fresh
    if use_cache and _cache_is_fresh(MACRO_CACHE_FILE, cache_max_age_hours):
        try:
            df = pd.read_parquet(MACRO_CACHE_FILE)
            # Filter to requested start date
            if isinstance(df.index, pd.DatetimeIndex):
                df = df.loc[df.index >= pd.Timestamp(start_date)]
            logger.info(
                f"Macro cache hit: {len(df)} rows "
                f"({df.index.min().date() if len(df) else 'empty'} to "
                f"{df.index.max().date() if len(df) else 'empty'})"
            )
            return df
        except Exception as e:
            logger.warning(f"Macro cache read failed: {e} — re-downloading")

    try:
        import yfinance as yf
    except ImportError:
        logger.error("yfinance not installed — cannot download macro data")
        return pd.DataFrame()

    logger.info(
        f"Downloading macro data: {list(MACRO_TICKERS.values())} "
        f"from {start_date}"
    )

    # Download all tickers in one batch call for efficiency
    tickers_str = " ".join(MACRO_TICKERS.values())
    try:
        raw = yf.download(
            tickers_str,
            start=start_date,
            progress=False,
            auto_adjust=True,
            threads=True,
        )
    except Exception as e:
        logger.error(f"Macro data download failed: {e}")
        return pd.DataFrame()

    if raw.empty:
        logger.warning("Macro download returned empty DataFrame")
        return pd.DataFrame()

    # Extract close prices for each series
    frames = {}
    for col_name, ticker in MACRO_TICKERS.items():
        try:
            if ("Close", ticker) in raw.columns:
                s = raw[("Close", ticker)]
            elif "Close" in raw.columns and isinstance(raw.columns, pd.MultiIndex):
                # Try alternate access
                s = raw["Close"][ticker]
            elif "Close" in raw.columns:
                # Single ticker returned
                s = raw["Close"]
            else:
                logger.warning(f"Cannot extract {ticker} from macro download")
                continue
            frames[col_name] = s
        except (KeyError, TypeError) as e:
            logger.warning(f"Failed to extract {ticker}: {e}")
            continue

    if not frames:
        logger.error("No macro data extracted from download")
        return pd.DataFrame()

    df = pd.DataFrame(frames)

    # Standardize index
    df.index = pd.to_datetime(df.index)
    if df.index.tz is not None:
        df.index = df.index.tz_localize(None)
    df.index.name = "date"
    df = df.sort_index()

    # Drop rows where ALL values are NaN
    df = df.dropna(how="all")

    # Forward-fill then backward-fill to handle non-overlapping calendars
    # (e.g. futures trade on different days than Treasury yields)
    df = df.ffill().bfill()

    # Ensure expected columns exist (fill with NaN if missing)
    for col in ["vix", "gold", "copper", "yield_10y", "yield_13w"]:
        if col not in df.columns:
            logger.warning(f"Macro column '{col}' missing — filling with NaN")
            df[col] = np.nan

    # Cache result
    try:
        tmp = MACRO_CACHE_FILE.with_suffix(".parquet.tmp")
        df.to_parquet(tmp, engine="pyarrow")
        import os
        os.replace(str(tmp), str(MACRO_CACHE_FILE))
        logger.info(
            f"Macro data cached: {len(df)} rows "
            f"({df.index.min().date()} to {df.index.max().date()})"
        )
    except Exception as e:
        logger.warning(f"Macro cache write failed: {e}")

    # Filter to requested start date
    df = df.loc[df.index >= pd.Timestamp(start_date)]

    return df


def compute_macro_signals(
    macro_df: pd.DataFrame,
    vix_roc_threshold: float = 0.30,
    yc_flattening_threshold: float = -0.10,
) -> pd.DataFrame:
    """Compute derived macro regime signals from raw macro data.

    CRITICAL: Uses only backward-looking computations:
      - Expanding window (cumulative history) for tercile classification
      - Lagged ROC (5-day percent change using past prices only)
      No look-ahead bias.

    Args:
        macro_df:               Raw macro DataFrame from download_macro_data().
        vix_roc_threshold:      VIX spike threshold (default 0.30 = 30%).
        yc_flattening_threshold: Yield curve flattening threshold (default -0.10).

    Returns:
        DataFrame indexed by date with columns:
            gold_copper_ratio   : float
            gc_regime           : int (1=risk-on, 2=neutral, 3=risk-off)
            vix_roc_5d          : float (5-day % change in VIX)
            vix_spike           : bool
            yield_curve_10y_3m  : float (10Y - 13W spread)
            yc_change_5d        : float (5-day change in spread)
            yc_flattening       : bool
            macro_regime_scale  : float (composite multiplier 0.5 to 1.5)
    """
    if macro_df is None or macro_df.empty:
        logger.warning("compute_macro_signals: empty input DataFrame")
        return pd.DataFrame()

    signals = pd.DataFrame(index=macro_df.index)

    # ------------------------------------------------------------------ #
    # 1. Gold / Copper Ratio                                               #
    # ------------------------------------------------------------------ #
    gold = macro_df.get("gold", pd.Series(dtype=float))
    copper = macro_df.get("copper", pd.Series(dtype=float))

    if isinstance(gold, pd.DataFrame):
        gold = gold.iloc[:, 0]
    if isinstance(copper, pd.DataFrame):
        copper = copper.iloc[:, 0]

    # Align indices
    gold = gold.reindex(macro_df.index).ffill()
    copper = copper.reindex(macro_df.index).ffill()

    # Avoid division by zero
    copper_safe = copper.replace(0, np.nan)
    gc_ratio = gold / copper_safe
    signals["gold_copper_ratio"] = gc_ratio

    # Classify into terciles using EXPANDING window (no look-ahead)
    # At each date, we only know the historical distribution up to that date.
    gc_regime = pd.Series(2, index=macro_df.index, dtype=int)  # default neutral
    min_periods = 60  # need at least 60 observations to compute reliable terciles

    for i in range(len(macro_df)):
        if i < min_periods:
            gc_regime.iloc[i] = 2  # insufficient history → neutral
            continue
        historical_values = gc_ratio.iloc[:i + 1].dropna()
        if len(historical_values) < min_periods:
            gc_regime.iloc[i] = 2
            continue
        p33 = historical_values.quantile(0.333)
        p67 = historical_values.quantile(0.667)
        current = gc_ratio.iloc[i]
        if pd.isna(current):
            gc_regime.iloc[i] = 2
        elif current <= p33:
            gc_regime.iloc[i] = 1  # risk-on: low ratio → copper strong vs gold
        elif current >= p67:
            gc_regime.iloc[i] = 3  # risk-off: high ratio → gold strong vs copper
        else:
            gc_regime.iloc[i] = 2  # neutral

    signals["gc_regime"] = gc_regime

    # ------------------------------------------------------------------ #
    # 2. VIX 5-Day Rate of Change                                          #
    # ------------------------------------------------------------------ #
    vix = macro_df.get("vix", pd.Series(dtype=float))
    if isinstance(vix, pd.DataFrame):
        vix = vix.iloc[:, 0]
    vix = vix.reindex(macro_df.index).ffill()

    # pct_change with periods=5: uses only past data, no look-ahead
    vix_roc_5d = vix.pct_change(periods=5)
    signals["vix_roc_5d"] = vix_roc_5d

    # VIX spike: True when 5d ROC exceeds threshold
    signals["vix_spike"] = vix_roc_5d > vix_roc_threshold

    # ------------------------------------------------------------------ #
    # 3. Yield Curve: 10Y - 13W (3M) Spread                               #
    # ------------------------------------------------------------------ #
    yield_10y = macro_df.get("yield_10y", pd.Series(dtype=float))
    yield_13w = macro_df.get("yield_13w", pd.Series(dtype=float))
    if isinstance(yield_10y, pd.DataFrame):
        yield_10y = yield_10y.iloc[:, 0]
    if isinstance(yield_13w, pd.DataFrame):
        yield_13w = yield_13w.iloc[:, 0]

    yield_10y = yield_10y.reindex(macro_df.index).ffill()
    yield_13w = yield_13w.reindex(macro_df.index).ffill()

    yc_spread = yield_10y - yield_13w
    signals["yield_curve_10y_3m"] = yc_spread

    # 5-day change in yield curve slope (backward-looking diff)
    yc_change_5d = yc_spread.diff(periods=5)
    signals["yc_change_5d"] = yc_change_5d

    # Flattening: True when 5d change < threshold (yield curve flattening/inverting)
    signals["yc_flattening"] = yc_change_5d < yc_flattening_threshold

    # ------------------------------------------------------------------ #
    # 4. Composite Macro Regime Scale (0.5 to 1.5)                         #
    # ------------------------------------------------------------------ #
    # Combine all signals into a single multiplier:
    #   gc_regime: risk-on (1) → +0.2, neutral (2) → 0.0, risk-off (3) → -0.4
    #   vix_spike: True → +0.1 (mean-reversion edge during panic)
    #   yc_flattening: True → -0.1 (macro headwinds)
    # Base = 1.0, clipped to [0.5, 1.5]

    gc_adj = pd.Series(0.0, index=macro_df.index, dtype=float)
    gc_adj[gc_regime == 1] = 0.2   # risk-on: scale up
    gc_adj[gc_regime == 3] = -0.4  # risk-off: scale down aggressively

    vix_adj = vix_roc_5d.gt(vix_roc_threshold).astype(float) * 0.1  # spike → slight boost

    yc_adj = yc_change_5d.lt(yc_flattening_threshold).astype(float) * -0.1  # flattening → reduce

    macro_scale = (1.0 + gc_adj + vix_adj + yc_adj).clip(0.5, 1.5)
    signals["macro_regime_scale"] = macro_scale

    # Fill any remaining NaN with neutral (1.0)
    signals = signals.ffill().fillna({
        "gold_copper_ratio": np.nan,
        "gc_regime": 2,
        "vix_roc_5d": 0.0,
        "vix_spike": False,
        "yield_curve_10y_3m": np.nan,
        "yc_change_5d": 0.0,
        "yc_flattening": False,
        "macro_regime_scale": 1.0,
    })

    # Enforce correct dtypes
    signals["gc_regime"] = signals["gc_regime"].astype(int)
    signals["vix_spike"] = signals["vix_spike"].astype(bool)
    signals["yc_flattening"] = signals["yc_flattening"].astype(bool)
    signals["macro_regime_scale"] = signals["macro_regime_scale"].astype(float)

    logger.info(
        f"Macro signals computed: {len(signals)} days | "
        f"gc_regime distribution: "
        f"risk-on={int((signals['gc_regime'] == 1).sum())}, "
        f"neutral={int((signals['gc_regime'] == 2).sum())}, "
        f"risk-off={int((signals['gc_regime'] == 3).sum())} | "
        f"vix_spikes={int(signals['vix_spike'].sum())} | "
        f"yc_flatten={int(signals['yc_flattening'].sum())} | "
        f"scale_mean={signals['macro_regime_scale'].mean():.3f}"
    )

    return signals
