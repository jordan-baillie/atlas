"""OHLCV data normalization for Atlas ingestion pipeline.

Handles ticker formatting, column standardization, and split adjustments.
No intra-ingest dependencies -- safe to import from any sub-module.
"""
import logging
from typing import Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Ticker normalisation
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


# ---------------------------------------------------------------------------
# OHLCV cleaning
# ---------------------------------------------------------------------------

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

    # Drop adj_close -- canonical format is split-adjusted raw OHLCV only
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

    # Drop adj_close -- canonical format is open, high, low, close, volume, ticker
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


# ---------------------------------------------------------------------------
# Split adjustments
# ---------------------------------------------------------------------------

def _apply_split_adjustments(df: pd.DataFrame, splits: pd.Series) -> pd.DataFrame:
    """Apply cumulative split adjustments to raw OHLCV data (backward adjustment).

    Adjusts all historical prices so they are comparable to the current
    (post-split) price scale.  For each split event, prices *before* the
    split date are divided by the split ratio and volumes are multiplied by
    the split ratio to preserve dollar-volume consistency.

    Only SPLIT adjustments are applied -- dividend adjustments are intentionally
    omitted to prevent retroactive price drift when dividends are paid.

    Args:
        df:     DataFrame with at least open, high, low, close columns and a
                DatetimeIndex named 'date'.
        splits: Series indexed by split date (tz-naive), values are split
                ratios (e.g. 4.0 = 4:1 forward split -- 1 old share -> 4 new).

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
