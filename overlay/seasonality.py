"""
overlay/seasonality.py — Seasonality-based sizing multiplier.

Computes a sizing adjustment factor based on historical monthly returns
and turn-of-month effects derived from SPY OHLCV data.

Key patterns exploited:
  - "Sell in May" effect: May-Sep historically weaker → reduced sizing
  - "Halloween indicator": Nov-Apr historically stronger → full/boosted sizing
  - Turn-of-month: last 1 + first 4 trading days tend to be strongest

Returns a single float multiplier in [0.75, 1.1] range.
"""

import logging
from datetime import datetime
from typing import Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


def _load_spy_history(min_years: int = 5) -> pd.DataFrame:
    """Load SPY OHLCV history from SQLite, falling back to parquet cache.

    Returns DataFrame with DatetimeIndex and at minimum a 'close' column.
    Raises ValueError if insufficient history available.
    """
    df = pd.DataFrame()

    # Try SQLite first
    try:
        from db.atlas_db import get_db
        with get_db() as db:
            rows = db.execute(
                "SELECT date, open, high, low, close, volume FROM ohlcv "
                "WHERE ticker='SPY' ORDER BY date"
            ).fetchall()
        if rows:
            df = pd.DataFrame(
                [dict(r) for r in rows],
                columns=["date", "open", "high", "low", "close", "volume"],
            )
            df["date"] = pd.to_datetime(df["date"])
            df = df.set_index("date").sort_index()
    except Exception as e:
        logger.warning("SQLite SPY load failed: %s", e)

    # Fallback to download_ticker
    if df.empty:
        try:
            from data.ingest import download_ticker
            df = download_ticker(
                "SPY",
                start=f"{datetime.now().year - 10}-01-01",
                market_id="sp500",
            )
        except Exception as e:
            logger.warning("download_ticker SPY failed: %s", e)

    if df.empty or len(df) < 252 * min_years:
        raise ValueError(
            f"Insufficient SPY history: {len(df)} rows "
            f"(need {252 * min_years} for {min_years} years)"
        )

    return df


def compute_monthly_multipliers(spy_df: pd.DataFrame) -> dict:
    """Compute average monthly return multipliers from historical SPY data.

    Args:
        spy_df: SPY OHLCV DataFrame with DatetimeIndex and 'close' column.

    Returns:
        Dict mapping month number (1-12) to sizing multiplier (float).
        Weaker months get reduced sizing, stronger months get boosted.
    """
    # Compute monthly returns
    monthly = spy_df["close"].resample("ME").last().pct_change().dropna()

    # Average return by month
    avg_by_month = monthly.groupby(monthly.index.month).mean()

    # Normalize: map returns to multiplier range [0.80, 1.10]
    # Use rank-based scaling to be robust to outliers
    min_ret = avg_by_month.min()
    max_ret = avg_by_month.max()
    ret_range = max_ret - min_ret

    if ret_range < 1e-8:
        # All months roughly equal — return neutral
        return {m: 1.0 for m in range(1, 13)}

    multipliers = {}
    for month in range(1, 13):
        if month in avg_by_month.index:
            # Scale from 0.80 (worst month) to 1.10 (best month)
            normalized = (avg_by_month[month] - min_ret) / ret_range
            multipliers[month] = round(0.80 + normalized * 0.30, 4)
        else:
            multipliers[month] = 1.0

    return multipliers


def is_turn_of_month(
    date: Optional[datetime] = None,
    spy_df: Optional[pd.DataFrame] = None,
) -> bool:
    """Check if the given date falls in the turn-of-month window.

    Turn-of-month = last 1 trading day of previous month + first 4 trading
    days of current month. This window historically captures ~80% of monthly
    equity returns.

    Args:
        date: Date to check (default: today).
        spy_df: Optional SPY DataFrame for computing trading days.
                If None, uses a weekday-based approximation.

    Returns:
        True if date is within the turn-of-month window.
    """
    if date is None:
        date = datetime.now()

    if isinstance(date, str):
        date = pd.Timestamp(date)

    target_date = pd.Timestamp(date).normalize()

    if spy_df is not None and not spy_df.empty:
        # Use actual trading calendar
        trading_days = spy_df.index.normalize()
        month_start = target_date.replace(day=1)
        prev_month_end = month_start - pd.Timedelta(days=1)
        prev_month_start = prev_month_end.replace(day=1)

        # Last 1 trading day of previous month
        prev_month_days = trading_days[
            (trading_days >= prev_month_start) & (trading_days <= prev_month_end)
        ]
        if len(prev_month_days) >= 1:
            last_day_prev = prev_month_days[-1:]
            if target_date in last_day_prev:
                return True

        # First 4 trading days of current month
        next_month_start = (
            month_start + pd.offsets.MonthEnd(1) + pd.Timedelta(days=1)
        )
        curr_month_days = trading_days[
            (trading_days >= month_start) & (trading_days < next_month_start)
        ]
        if len(curr_month_days) >= 4:
            first_4_days = curr_month_days[:4]
            if target_date in first_4_days:
                return True
        elif target_date in curr_month_days:
            # Less than 4 trading days so far this month — we're in the window
            return True

        return False

    # Weekday-based approximation (no trading calendar)
    day = target_date.day
    days_in_month = pd.Timestamp(target_date.year, target_date.month, 1).days_in_month

    # Last 2 calendar days of month (approximates last 1 trading day)
    if day >= days_in_month - 1:
        return True
    # First 6 calendar days of month (approximates first 4 trading days)
    if day <= 6:
        return True

    return False


def get_seasonality_multiplier(
    date: Optional[datetime] = None,
    enabled: bool = True,
) -> float:
    """Compute the seasonality sizing multiplier for a given date.

    Combines:
      1. Monthly historical return pattern (0.80 to 1.10)
      2. Turn-of-month boost (+0.05 during turn-of-month window)

    Final result is clamped to [0.75, 1.10].

    Args:
        date: Target date (default: today).
        enabled: If False, returns 1.0 (neutral). For config-driven enable/disable.

    Returns:
        Float multiplier in [0.75, 1.10] range.
        1.0 = neutral, <1.0 = reduce sizing, >1.0 = boost sizing.
    """
    if not enabled:
        return 1.0

    if date is None:
        date = datetime.now()

    if isinstance(date, str):
        date = pd.Timestamp(date)

    try:
        spy_df = _load_spy_history(min_years=3)
    except (ValueError, Exception) as e:
        logger.warning(
            "Seasonality: insufficient SPY history (%s) — returning 1.0", e
        )
        return 1.0

    # 1. Monthly multiplier
    monthly_mults = compute_monthly_multipliers(spy_df)
    month = date.month
    monthly_mult = monthly_mults.get(month, 1.0)

    # 2. Turn-of-month boost
    tom_boost = 0.05 if is_turn_of_month(date, spy_df) else 0.0

    # Combine and clamp
    final = round(min(1.10, max(0.75, monthly_mult + tom_boost)), 4)

    logger.info(
        "Seasonality multiplier for %s: %.4f "
        "(month=%d monthly=%.4f tom_boost=%.2f)",
        date.strftime("%Y-%m-%d") if hasattr(date, "strftime") else str(date),
        final,
        month,
        monthly_mult,
        tom_boost,
    )

    return final


def get_seasonality_report(date: Optional[datetime] = None) -> dict:
    """Generate a full seasonality report for diagnostics.

    Returns:
        Dict with monthly_multipliers, current settings, and final multiplier.
    """
    if date is None:
        date = datetime.now()

    try:
        spy_df = _load_spy_history(min_years=3)
        monthly_mults = compute_monthly_multipliers(spy_df)
        tom = is_turn_of_month(date, spy_df)
        final = get_seasonality_multiplier(date)

        return {
            "date": (
                date.strftime("%Y-%m-%d") if hasattr(date, "strftime") else str(date)
            ),
            "month": date.month,
            "monthly_multiplier": monthly_mults.get(date.month, 1.0),
            "is_turn_of_month": tom,
            "tom_boost": 0.05 if tom else 0.0,
            "final_multiplier": final,
            "all_monthly_multipliers": monthly_mults,
            "spy_history_days": len(spy_df),
        }
    except Exception as e:
        return {
            "date": str(date),
            "error": str(e),
            "final_multiplier": 1.0,
        }
