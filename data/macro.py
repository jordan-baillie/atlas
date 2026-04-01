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
from typing import Dict, Optional

import numpy as np
import pandas as pd

# Imported at module level so tests can patch data.macro.fetch_regime_macro_series.
from data.fred import fetch_regime_macro_series

logger = logging.getLogger(__name__)

# Cache directory for macro data
PROJECT_ROOT = Path(__file__).resolve().parent.parent
MACRO_CACHE_DIR = PROJECT_ROOT / "data" / "cache" / "macro"
MACRO_CACHE_FILE = MACRO_CACHE_DIR / "macro_daily.parquet"

# Tickers to download
MACRO_TICKERS = {
    "vix": "^VIX",
    "vix3m": "^VIX3M",   # CBOE 3-Month Volatility Index
    "gold": "GC=F",
    "copper": "HG=F",
    "yield_10y": "^TNX",
    "yield_13w": "^IRX",
    "spy": "SPY",        # S&P 500 ETF — for 200 DMA calculation
}

# Minimum expected columns in the cached parquet (invalidates stale cache
# from before the vix3m / spy columns were added).
_REQUIRED_CACHE_COLS = frozenset({"vix", "vix3m", "gold", "copper", "yield_10y", "yield_13w", "spy"})


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

    # Serve from cache if fresh *and* contains all required columns
    if use_cache and _cache_is_fresh(MACRO_CACHE_FILE, cache_max_age_hours):
        try:
            df = pd.read_parquet(MACRO_CACHE_FILE)
            missing = _REQUIRED_CACHE_COLS - set(df.columns)
            if missing:
                logger.info(
                    f"Macro cache missing columns {missing} — re-downloading"
                )
            else:
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
    for col in ["vix", "vix3m", "gold", "copper", "yield_10y", "yield_13w", "spy"]:
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
    # 4. VIX Term Structure (VIX / VIX3M ratio)                            #
    # ------------------------------------------------------------------ #
    vix3m = macro_df.get("vix3m", pd.Series(dtype=float))
    if isinstance(vix3m, pd.DataFrame):
        vix3m = vix3m.iloc[:, 0]
    vix3m = vix3m.reindex(macro_df.index).ffill()

    # VIX term ratio: spot VIX / 3-month VIX.  >1 = inverted term structure
    # (short-term fear exceeds long-term implied vol — often a panic signal).
    # Guard against division by zero.
    vix3m_safe = vix3m.replace(0, np.nan)
    signals["vix_term_ratio"] = vix.div(vix3m_safe)

    # ------------------------------------------------------------------ #
    # 5. SPY Price vs 200-Day Moving Average                               #
    # ------------------------------------------------------------------ #
    spy = macro_df.get("spy", pd.Series(dtype=float))
    if isinstance(spy, pd.DataFrame):
        spy = spy.iloc[:, 0]
    spy = spy.reindex(macro_df.index).ffill()

    # Pass the raw close through so callers can read it from signals df.
    signals["spy_close"] = spy

    # 200-day simple moving average (min_periods=1 so early rows are not NaN).
    spy_200dma = spy.rolling(200, min_periods=1).mean()
    signals["spy_200dma"] = spy_200dma

    # 1 if SPY is above its 200 DMA, else 0.
    signals["spy_above_200dma"] = (spy > spy_200dma).astype(int)

    # 20-day rate of change of the 200 DMA (percentage, backward-looking).
    # Positive = DMA rising (trend intact); negative = DMA declining (trend broken).
    signals["spy_200dma_slope"] = spy_200dma.pct_change(20)

    # ------------------------------------------------------------------ #
    # 6. Composite Macro Regime Scale (0.5 to 1.5)                         #
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

    # Fill any remaining NaN with neutral values
    signals = signals.ffill().fillna({
        "gold_copper_ratio": np.nan,
        "gc_regime": 2,
        "vix_roc_5d": 0.0,
        "vix_spike": False,
        "yield_curve_10y_3m": np.nan,
        "yc_change_5d": 0.0,
        "yc_flattening": False,
        "macro_regime_scale": 1.0,
        "vix_term_ratio": np.nan,
        "spy_close": np.nan,
        "spy_200dma": np.nan,
        "spy_above_200dma": 0,
        "spy_200dma_slope": np.nan,
    })

    # Enforce correct dtypes
    signals["gc_regime"] = signals["gc_regime"].astype(int)
    signals["vix_spike"] = signals["vix_spike"].astype(bool)
    signals["yc_flattening"] = signals["yc_flattening"].astype(bool)
    signals["macro_regime_scale"] = signals["macro_regime_scale"].astype(float)
    signals["spy_above_200dma"] = signals["spy_above_200dma"].astype(int)

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


# ───────────────────────────────────────────────────────────────────────────────
# Regime model — unified macro data pipeline
# ───────────────────────────────────────────────────────────────────────────────


def fetch_macro_data(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    use_cache: bool = True,
    write_to_db: bool = False,
    fred_api_key: Optional[str] = None,
) -> pd.DataFrame:
    """Fetch, compute, and optionally persist all macro regime indicators.

    This is the single entry-point for the regime model’s data needs.
    It combines:

    * **yfinance** data: VIX, VIX3M, gold, copper, SPY, 10Y yield, 3M yield
    * **FRED** data: 2Y yield (GS2), credit OAS (BAMLC0A0CM), DXY (DTWEXBGS),
      fed funds rate (FEDFUNDS), initial claims (ICSA)
    * **Derived** indicators: vix_term_ratio, gold_copper_ratio, spy_200dma,
      spy_above_200dma, spy_200dma_slope, yield curves

    The returned DataFrame columns map 1-to-1 with the ``macro_indicators``
    SQLite table schema.  All FRED series are forward-filled to the yfinance
    trading-day calendar.

    Args:
        start_date:   Earliest date to include ``'YYYY-MM-DD'`` (default: 5 yrs ago).
        end_date:     Latest date to include ``'YYYY-MM-DD'`` (default: today).
        use_cache:    Pass to :func:`download_macro_data` cache logic.
        write_to_db:  If ``True``, write results to SQLite via
                      :func:`write_macro_indicators_to_db`.
        fred_api_key: Optional FRED API key override.

    Returns:
        :class:`pandas.DataFrame` indexed by date with all macro_indicators
        columns (except ``updated_at``).  Returns empty DataFrame on failure.

    Example::

        from data.macro import fetch_macro_data

        df = fetch_macro_data(start_date="2022-01-01", write_to_db=True)
    """
    if not start_date:
        start_date = (datetime.now() - timedelta(days=5 * 365)).strftime("%Y-%m-%d")
    if not end_date:
        end_date = datetime.now().strftime("%Y-%m-%d")

    # --- 1. yfinance raw data -------------------------------------------
    raw = download_macro_data(
        start_date=start_date,
        use_cache=use_cache,
    )
    if raw.empty:
        logger.error("fetch_macro_data: yfinance download returned empty DataFrame")
        return pd.DataFrame()

    # Filter to requested range after download (download may return more).
    raw = raw.loc[
        (raw.index >= pd.Timestamp(start_date)) &
        (raw.index <= pd.Timestamp(end_date))
    ]
    if raw.empty:
        logger.warning("fetch_macro_data: no data in [%s, %s]", start_date, end_date)
        return pd.DataFrame()

    # --- 2. FRED data ----------------------------------------------------
    fred_series: Dict[str, pd.Series] = {}
    try:
        fred_series = fetch_regime_macro_series(
            start_date=start_date,
            end_date=end_date,
            api_key=fred_api_key,
        )
    except Exception as exc:
        logger.warning("fetch_macro_data: FRED fetch failed — %s", exc)

    # --- 3. Derived signals (yfinance-based) ----------------------------
    signals = compute_macro_signals(raw)

    # --- 4. Assemble unified DataFrame ----------------------------------
    # Start from the yfinance trading-day calendar.
    df = pd.DataFrame(index=raw.index)
    df.index.name = "date"

    # Raw yfinance columns
    df["vix"] = raw.get("vix", pd.Series(dtype=float)).reindex(df.index).ffill()
    df["vix3m"] = raw.get("vix3m", pd.Series(dtype=float)).reindex(df.index).ffill()
    df["yield_10y"] = raw.get("yield_10y", pd.Series(dtype=float)).reindex(df.index).ffill()
    # yield_3m: rename from yield_13w (same instrument, 13-week T-bill ≈ 3 months)
    df["yield_3m"] = raw.get("yield_13w", pd.Series(dtype=float)).reindex(df.index).ffill()
    df["gold"] = raw.get("gold", pd.Series(dtype=float)).reindex(df.index).ffill()
    df["copper"] = raw.get("copper", pd.Series(dtype=float)).reindex(df.index).ffill()
    df["spy_close"] = raw.get("spy", pd.Series(dtype=float)).reindex(df.index).ffill()

    # Derived signals from compute_macro_signals
    for col in [
        "vix_term_ratio", "gold_copper_ratio",
        "spy_200dma", "spy_above_200dma", "spy_200dma_slope",
    ]:
        if col in signals.columns:
            df[col] = signals[col].reindex(df.index)

    # Yield curves (yfinance-based)
    df["yield_curve_10y3m"] = df["yield_10y"] - df["yield_3m"]

    # FRED series — reindex to trading-day calendar and forward-fill
    if "yield_2y" in fred_series and not fred_series["yield_2y"].empty:
        df["yield_2y"] = (
            fred_series["yield_2y"]
            .reindex(df.index.union(fred_series["yield_2y"].index))
            .ffill()
            .reindex(df.index)
        )
        df["yield_curve_10y2y"] = df["yield_10y"] - df["yield_2y"]
    else:
        df["yield_2y"] = np.nan
        df["yield_curve_10y2y"] = np.nan

    if "credit_oas" in fred_series and not fred_series["credit_oas"].empty:
        df["credit_oas"] = (
            fred_series["credit_oas"]
            .reindex(df.index.union(fred_series["credit_oas"].index))
            .ffill()
            .reindex(df.index)
        )
    else:
        df["credit_oas"] = np.nan

    if "dxy" in fred_series and not fred_series["dxy"].empty:
        df["dxy"] = (
            fred_series["dxy"]
            .reindex(df.index.union(fred_series["dxy"].index))
            .ffill()
            .reindex(df.index)
        )
    else:
        df["dxy"] = np.nan

    if "fed_funds" in fred_series and not fred_series["fed_funds"].empty:
        df["fed_funds"] = (
            fred_series["fed_funds"]
            .reindex(df.index.union(fred_series["fed_funds"].index))
            .ffill()
            .reindex(df.index)
        )
    else:
        df["fed_funds"] = np.nan

    if "unemployment_claims" in fred_series and not fred_series["unemployment_claims"].empty:
        df["unemployment_claims"] = (
            fred_series["unemployment_claims"]
            .reindex(df.index.union(fred_series["unemployment_claims"].index))
            .ffill()
            .reindex(df.index)
        )
    else:
        df["unemployment_claims"] = np.nan

    logger.info(
        "fetch_macro_data: %d rows [%s, %s]",
        len(df),
        df.index.min().date() if len(df) else "empty",
        df.index.max().date() if len(df) else "empty",
    )

    # --- 5. Optionally persist to SQLite --------------------------------
    if write_to_db and not df.empty:
        n = write_macro_indicators_to_db(df)
        logger.info("fetch_macro_data: wrote %d rows to macro_indicators", n)

    return df


def write_macro_indicators_to_db(df: pd.DataFrame) -> int:
    """Write a macro indicators DataFrame to the SQLite macro_indicators table.

    Each row in *df* is upserted (INSERT OR REPLACE) into the
    ``macro_indicators`` table.  The DataFrame index must be a
    :class:`pandas.DatetimeIndex` (as returned by :func:`fetch_macro_data`).

    NaN/inf values are stored as NULL.  Integer columns
    (``spy_above_200dma``, ``unemployment_claims``) are cast before writing.

    Args:
        df: DataFrame with date index and macro indicator columns.

    Returns:
        Number of rows written.
    """
    from db.atlas_db import upsert_macro_indicators

    if df.empty:
        return 0

    # Integer columns that must be cast before writing
    int_cols = {"spy_above_200dma", "unemployment_claims"}

    n = 0
    for ts, row in df.iterrows():
        date_str = ts.strftime("%Y-%m-%d") if hasattr(ts, "strftime") else str(ts)
        fields: Dict = {}
        for col, val in row.items():
            if col in ("date", "updated_at"):
                continue
            # Convert numpy scalars to native Python types
            if hasattr(val, "item"):
                val = val.item()
            if col in int_cols and val is not None:
                try:
                    # NaN -> None, otherwise round to int
                    import math
                    if isinstance(val, float) and (math.isnan(val) or math.isinf(val)):
                        val = None
                    else:
                        val = int(round(val))
                except (TypeError, ValueError):
                    val = None
            fields[col] = val

        upsert_macro_indicators(date_str, **fields)
        n += 1

    return n


def backfill_macro_indicators(
    start_date: str,
    end_date: Optional[str] = None,
    fred_api_key: Optional[str] = None,
) -> pd.DataFrame:
    """Fetch and persist macro indicators for a historical date range.

    Downloads data starting **300 calendar days before** *start_date* to
    ensure the 200-day moving average is properly warmed up, then filters
    the output to ``[start_date, end_date]`` before writing to SQLite.

    Intended for one-time or periodic history backfills.  The cache is
    bypassed (``use_cache=False``) so we always get fresh data.

    Args:
        start_date:   First date to write ``'YYYY-MM-DD'``.
        end_date:     Last date to write (default: today).
        fred_api_key: Optional FRED API key override.

    Returns:
        The filtered DataFrame that was written to SQLite (indexed by date).

    Example::

        from data.macro import backfill_macro_indicators

        df = backfill_macro_indicators("2015-01-01", "2024-12-31")
        print(f"Backfilled {len(df)} rows")
    """
    if not end_date:
        end_date = datetime.now().strftime("%Y-%m-%d")

    # Download starting 300 days before requested start to warm up the 200 DMA.
    warmup_start = (
        pd.Timestamp(start_date) - timedelta(days=300)
    ).strftime("%Y-%m-%d")

    logger.info(
        "backfill_macro_indicators: downloading [%s, %s] (warmup from %s)",
        start_date, end_date, warmup_start,
    )

    # Fetch full dataset (no cache to guarantee freshness)
    df_full = fetch_macro_data(
        start_date=warmup_start,
        end_date=end_date,
        use_cache=False,
        write_to_db=False,   # we'll write only the filtered slice
        fred_api_key=fred_api_key,
    )

    if df_full.empty:
        logger.warning("backfill_macro_indicators: no data returned")
        return pd.DataFrame()

    # Filter to the requested range
    df = df_full.loc[
        (df_full.index >= pd.Timestamp(start_date)) &
        (df_full.index <= pd.Timestamp(end_date))
    ].copy()

    if df.empty:
        logger.warning(
            "backfill_macro_indicators: no rows in [%s, %s] after filtering",
            start_date, end_date,
        )
        return df

    n = write_macro_indicators_to_db(df)
    logger.info(
        "backfill_macro_indicators: wrote %d rows to macro_indicators [%s, %s]",
        n, start_date, end_date,
    )
    return df
