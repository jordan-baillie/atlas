"""
Atlas Common Helper Functions
==================================
Utility functions for date parsing, formatting, technical indicators,
and position sizing used across the trading lab.

Usage:
    from utils.helpers import (
        parse_date, format_aud, format_pct,
        calc_atr, calc_rsi, calc_zscore, calc_volume_ratio, calc_position_size
    )
"""

import math
import logging
from datetime import datetime, date
from typing import Optional, Union

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Date Parsing
# ---------------------------------------------------------------------------

def parse_date(value: Union[str, datetime, date, pd.Timestamp]) -> pd.Timestamp:
    """Parse various date formats into a pandas Timestamp.

    Accepts:
        - ISO strings: '2024-01-15', '2024-01-15T10:30:00'
        - Common formats: '15/01/2024', '01-15-2024', '15 Jan 2024'
        - datetime, date, or pd.Timestamp objects

    Args:
        value: Date value in any supported format.

    Returns:
        pd.Timestamp (timezone-naive).

    Raises:
        ValueError: If the date cannot be parsed.
    """
    if isinstance(value, pd.Timestamp):
        return value.tz_localize(None) if value.tzinfo else value
    if isinstance(value, datetime):
        return pd.Timestamp(value).tz_localize(None)
    if isinstance(value, date):
        return pd.Timestamp(value)
    if isinstance(value, str):
        try:
            ts = pd.Timestamp(value)
            return ts.tz_localize(None) if ts.tzinfo else ts
        except (ValueError, TypeError):
            pass
        # Try common AU date formats
        for fmt in ("%d/%m/%Y", "%d-%m-%Y", "%d %b %Y", "%d %B %Y", "%m/%d/%Y"):
            try:
                return pd.Timestamp(datetime.strptime(value, fmt))
            except ValueError:
                continue
    raise ValueError(f"Cannot parse date: {value!r}")


def today() -> pd.Timestamp:
    """Return today's date as a timezone-naive pd.Timestamp."""
    return pd.Timestamp(date.today())


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

_CURRENCY_SYMBOLS = {
    "AUD": "A$", "USD": "$", "GBP": "£", "EUR": "€",
    "JPY": "¥", "HKD": "HK$", "SGD": "S$", "CAD": "C$",
}


def format_currency(amount: float, currency: str = "AUD", decimals: int = 2) -> str:
    """Format a number in any supported currency.

    Args:
        amount: Monetary amount.
        currency: ISO 4217 currency code (default 'AUD').
        decimals: Decimal places (default 2).

    Returns:
        Formatted string, e.g. '$1,234.50', '£500.00', '-A$500.00'.
    """
    symbol = _CURRENCY_SYMBOLS.get(currency.upper(), f"{currency} ")
    if amount < 0:
        return f"-{symbol}{abs(amount):,.{decimals}f}"
    return f"{symbol}{amount:,.{decimals}f}"


def format_aud(amount: float, decimals: int = 2) -> str:
    """Format a number as Australian dollars.

    Backward-compatible alias for format_currency(amount, 'AUD').

    Args:
        amount: Dollar amount.
        decimals: Decimal places (default 2).

    Returns:
        Formatted string, e.g. '$1,234.56' or '-$500.00'.

    Examples:
        >>> format_aud(1234.5)
        '$1,234.50'
        >>> format_aud(-500)
        '-$500.00'
    """
    # Keep original format (no A$ prefix) for backward compat
    if amount < 0:
        return f"-${abs(amount):,.{decimals}f}"
    return f"${amount:,.{decimals}f}"


def format_pct(value: float, decimals: int = 2, multiply: bool = True) -> str:
    """Format a number as a percentage string.

    Args:
        value: The value to format.
        decimals: Decimal places (default 2).
        multiply: If True, multiply by 100 first (i.e., 0.05 -> '5.00%').
                  If False, treat value as already in percent (5.0 -> '5.00%').

    Returns:
        Formatted percentage string.

    Examples:
        >>> format_pct(0.0523)
        '5.23%'
        >>> format_pct(5.23, multiply=False)
        '5.23%'
    """
    pct = value * 100 if multiply else value
    return f"{pct:+.{decimals}f}%" if pct != 0 else f"{pct:.{decimals}f}%"


# ---------------------------------------------------------------------------
# Technical Indicators
# ---------------------------------------------------------------------------

def calc_atr(high: pd.Series, low: pd.Series, close: pd.Series,
             period: int = 14) -> pd.Series:
    """Calculate Average True Range (ATR).

    ATR measures market volatility by decomposing the entire range of
    an asset price for a given period.

    Args:
        high: Series of high prices.
        low: Series of low prices.
        close: Series of close prices.
        period: Lookback period (default 14).

    Returns:
        pd.Series of ATR values (NaN for first `period` rows).
    """
    if len(close) < period + 1:
        logger.warning(f"Insufficient data for ATR({period}): got {len(close)} rows")
        return pd.Series(np.nan, index=close.index)

    prev_close = close.shift(1)
    tr1 = high - low
    tr2 = (high - prev_close).abs()
    tr3 = (low - prev_close).abs()
    true_range = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)

    # Wilder's smoothing (EMA with alpha = 1/period)
    atr = true_range.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()
    return atr


def calc_rsi(close: pd.Series, period: int = 14) -> pd.Series:
    """Calculate Relative Strength Index (RSI).

    Uses Wilder's smoothing method (exponential moving average).

    Args:
        close: Series of close prices.
        period: Lookback period (default 14).

    Returns:
        pd.Series of RSI values (0-100). NaN for insufficient data.
    """
    if len(close) < period + 1:
        logger.warning(f"Insufficient data for RSI({period}): got {len(close)} rows")
        return pd.Series(np.nan, index=close.index)

    delta = close.diff()
    gains = delta.clip(lower=0)
    losses = (-delta).clip(lower=0)

    # Wilder's smoothing
    alpha = 1.0 / period
    avg_gain = gains.ewm(alpha=alpha, min_periods=period, adjust=False).mean()
    avg_loss = losses.ewm(alpha=alpha, min_periods=period, adjust=False).mean()

    # Avoid division by zero
    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi = 100.0 - (100.0 / (1.0 + rs))

    return rsi


def calc_zscore(series: pd.Series, lookback: int = 20) -> pd.Series:
    """Calculate rolling Z-score.

    Z-score = (value - rolling_mean) / rolling_std

    Useful for mean-reversion signals: values below -2 suggest
    the price is significantly below its recent average.

    Args:
        series: Input price or return series.
        lookback: Rolling window size (default 20).

    Returns:
        pd.Series of Z-score values.
    """
    if len(series) < lookback:
        logger.warning(f"Insufficient data for Z-score({lookback}): got {len(series)} rows")
        return pd.Series(np.nan, index=series.index)

    rolling_mean = series.rolling(window=lookback).mean()
    rolling_std = series.rolling(window=lookback).std(ddof=1)

    # Avoid division by zero
    zscore = (series - rolling_mean) / rolling_std.replace(0, np.nan)
    return zscore




def calc_volume_ratio(volume: pd.Series, lookback: int = 20) -> pd.Series:
    """Calculate volume ratio (current volume / average volume).

    A ratio > 1.0 means above-average volume (conviction).
    A ratio < 1.0 means below-average volume (weak move).

    Args:
        volume: Series of volume data.
        lookback: Rolling window for average (default 20).

    Returns:
        pd.Series of volume ratios.
    """
    if len(volume) < lookback:
        logger.warning(f"Insufficient data for volume_ratio({lookback}): got {len(volume)} rows")
        return pd.Series(np.nan, index=volume.index)

    avg_volume = volume.rolling(window=lookback).mean()
    # Avoid division by zero
    ratio = volume / avg_volume.replace(0, np.nan)
    return ratio



def calc_ibs(high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    """Calculate Internal Bar Strength (IBS).

    IBS = (Close - Low) / (High - Low)
    Range: 0.0 (closed at low) to 1.0 (closed at high).
    Low IBS (< 0.2) suggests selling pressure exhaustion.

    Args:
        high: Series of high prices.
        low: Series of low prices.
        close: Series of close prices.

    Returns:
        pd.Series of IBS values (0.0 to 1.0). NaN where range is zero.
    """
    range_ = high - low
    ibs = (close - low) / range_.replace(0, np.nan)
    return ibs

# ---------------------------------------------------------------------------
# Position Sizing
# ---------------------------------------------------------------------------

def calc_position_size(equity: float, risk_pct: float,
                       entry_price: float, stop_price: float,
                       commission_per_trade: float = 5.0,
                       commission_pct: float = 0.0008,
                       min_shares: int = 1) -> dict:
    """Calculate position size based on fixed-fractional risk.

    Determines how many shares to buy so that if the stop-loss is hit,
    the total loss (including commissions) does not exceed the risk budget.

    Formula:
        risk_budget = equity * risk_pct
        risk_per_share = |entry_price - stop_price|
        shares = floor(risk_budget / risk_per_share)
        (adjusted for commissions)

    Args:
        equity: Current account equity in AUD.
        risk_pct: Maximum risk as a fraction (e.g., 0.005 = 0.5%).
        entry_price: Planned entry price per share.
        stop_price: Stop-loss price per share.
        commission_per_trade: Flat commission per trade in AUD (default $5).
        commission_pct: Commission as fraction of trade value (default 0.08%).
        min_shares: Minimum number of shares (default 1).

    Returns:
        Dict with keys:
            - shares: Number of shares to buy (int).
            - position_value: Total cost of position in AUD.
            - risk_per_share: Dollar risk per share.
            - total_risk: Total dollar risk (including commissions).
            - risk_pct_actual: Actual risk as fraction of equity.

    Raises:
        ValueError: If inputs are invalid (e.g., stop >= entry for long).
    """
    if equity <= 0:
        raise ValueError(f"Equity must be positive, got {equity}")
    if risk_pct <= 0 or risk_pct > 1:
        raise ValueError(f"risk_pct must be in (0, 1], got {risk_pct}")
    if entry_price <= 0:
        raise ValueError(f"entry_price must be positive, got {entry_price}")
    if stop_price <= 0:
        raise ValueError(f"stop_price must be positive, got {stop_price}")

    risk_per_share = abs(entry_price - stop_price)
    if risk_per_share == 0:
        raise ValueError("entry_price and stop_price cannot be equal")

    risk_budget = equity * risk_pct

    # Account for round-trip commissions in the risk budget
    # Two trades: entry + exit, each has flat + percentage commission
    # We iteratively solve for shares since commission depends on position size
    # Start with a rough estimate ignoring commissions
    shares_raw = risk_budget / risk_per_share

    # Refine: subtract estimated commissions from risk budget
    for _ in range(5):  # converges quickly
        position_value = shares_raw * entry_price
        exit_value = shares_raw * stop_price
        total_commission = (
            2 * commission_per_trade +
            commission_pct * position_value +
            commission_pct * exit_value
        )
        adjusted_budget = risk_budget - total_commission
        if adjusted_budget <= 0:
            shares_raw = 0
            break
        shares_raw = adjusted_budget / risk_per_share

    shares = max(int(math.floor(shares_raw)), 0)

    # Check if position value exceeds equity (can't buy more than we have)
    if shares * entry_price > equity:
        shares = int(math.floor(equity / entry_price))

    # Enforce minimum
    if 0 < shares < min_shares:
        shares = 0  # Can't meet minimum, don't trade

    # Calculate actuals
    position_value = shares * entry_price
    exit_value = shares * stop_price
    total_commission = (
        2 * commission_per_trade +
        commission_pct * position_value +
        commission_pct * exit_value
    )
    total_risk = (shares * risk_per_share) + total_commission if shares > 0 else 0
    risk_pct_actual = total_risk / equity if equity > 0 else 0

    result = {
        "shares": shares,
        "position_value": round(position_value, 2),
        "risk_per_share": round(risk_per_share, 4),
        "total_risk": round(total_risk, 2),
        "risk_pct_actual": round(risk_pct_actual, 6),
        "commission_estimate": round(total_commission, 2),
    }

    logger.debug(
        f"Position size: {shares} shares @ {format_aud(entry_price)}, "
        f"stop {format_aud(stop_price)}, risk {format_aud(total_risk)} "
        f"({format_pct(risk_pct_actual)})"
    )

    return result


if __name__ == "__main__":
    # Self-test
    logging.basicConfig(level=logging.INFO)
    print("=== Helpers Module Self-Test ===")

    # Date parsing
    print("\n--- Date Parsing ---")
    for d in ["2024-01-15", "15/01/2024", "15 Jan 2024", datetime(2024, 1, 15)]:
        print(f"  {d!r:30s} -> {parse_date(d)}")

    # Formatting
    print("\n--- Formatting ---")
    print(f"  format_aud(1234.5)     = {format_aud(1234.5)}")
    print(f"  format_aud(-500)       = {format_aud(-500)}")
    print(f"  format_aud(0.5)        = {format_aud(0.5)}")
    print(f"  format_pct(0.0523)     = {format_pct(0.0523)}")
    print(f"  format_pct(-0.032)     = {format_pct(-0.032)}")
    print(f"  format_pct(5.23, multiply=False) = {format_pct(5.23, multiply=False)}")

    # Technical indicators with synthetic data
    print("\n--- Technical Indicators ---")
    np.random.seed(42)
    n = 100
    prices = pd.Series(50 + np.cumsum(np.random.randn(n) * 0.5))
    highs = prices + np.random.rand(n) * 1.0
    lows = prices - np.random.rand(n) * 1.0

    atr = calc_atr(highs, lows, prices, period=14)
    print(f"  ATR(14) last 5: {atr.tail().values.round(4)}")

    rsi = calc_rsi(prices, period=14)
    print(f"  RSI(14) last 5: {rsi.tail().values.round(2)}")

    zs = calc_zscore(prices, lookback=20)
    print(f"  Z-score(20) last 5: {zs.tail().values.round(4)}")

    # Position sizing
    print("\n--- Position Sizing ---")
    pos = calc_position_size(
        equity=5000, risk_pct=0.005,
        entry_price=45.50, stop_price=43.50,
        commission_per_trade=5.0, commission_pct=0.0008
    )
    print(f"  Equity=$5,000, Risk=0.5%, Entry=$45.50, Stop=$43.50")
    print(f"  -> Shares: {pos['shares']}")
    print(f"  -> Position value: {format_aud(pos['position_value'])}")
    print(f"  -> Risk/share: {format_aud(pos['risk_per_share'])}")
    print(f"  -> Total risk: {format_aud(pos['total_risk'])}")
    print(f"  -> Actual risk %: {format_pct(pos['risk_pct_actual'])}")
    print(f"  -> Commission est: {format_aud(pos['commission_estimate'])}")

    # Edge case: tiny equity
    pos2 = calc_position_size(
        equity=100, risk_pct=0.005,
        entry_price=45.50, stop_price=43.50
    )
    print(f"\n  Edge case (equity=$100): shares={pos2['shares']}")

    print("\n=== Helpers Module OK ===")
