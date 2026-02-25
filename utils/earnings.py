"""Atlas Earnings Calendar Utility
=====================================
Fetches and caches earnings dates for tickers to support
earnings blackout windows in trading strategies.

Two tiers of data:
  1. Precise dates  - from yfinance earnings_dates / calendar
  2. Estimated dates - derived from income_stmt FY-end + fixed offset

Blackout windows:
  Precise   : ±(days_before, days_after) around the known date (default 5/1)
  Estimated : wider window (default 20 before / 15 after) to cover
              the realistic reporting range

Works with any market supported by yfinance.

Usage:
    from utils.earnings import is_near_earnings, get_next_earnings_date
"""

import logging
import json
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, List

import pandas as pd
import yfinance as yf

logger = logging.getLogger(__name__)

# Cache directory
_CACHE_DIR = Path(__file__).resolve().parent.parent / "data" / "cache" / "earnings"
_CACHE_DIR.mkdir(parents=True, exist_ok=True)

# In-memory cache
_memory_cache: dict = {}

# Estimated date offset: FY/H1 end → announcement (median ~45 days for ASX)
_ESTIMATED_OFFSET_DAYS = 45


def _cache_path(ticker: str) -> Path:
    safe_name = ticker.replace(".", "_")
    return _CACHE_DIR / f"{safe_name}_earnings.json"


def _load_cached_earnings(ticker: str) -> Optional[dict]:
    """Load cached entry. Returns dict with 'dates' and 'estimated_dates', or None."""
    if ticker in _memory_cache:
        entry = _memory_cache[ticker]
        if (datetime.now() - entry["fetched"]).days < 7:
            return {"dates": entry["dates"], "estimated_dates": entry.get("estimated_dates", [])}

    path = _cache_path(ticker)
    if not path.exists():
        return None

    try:
        with open(path, "r") as f:
            data = json.load(f)
        fetched = datetime.fromisoformat(data["fetched"])
        if (datetime.now() - fetched).days >= 7:
            return None
        result = {
            "dates": data.get("dates", []),
            "estimated_dates": data.get("estimated_dates", []),
        }
        _memory_cache[ticker] = {
            "dates": result["dates"],
            "estimated_dates": result["estimated_dates"],
            "fetched": fetched,
        }
        return result
    except (json.JSONDecodeError, KeyError, ValueError) as e:
        logger.debug(f"{ticker}: corrupt earnings cache: {e}")
        return None


def _save_cached_earnings(ticker: str, dates: list, estimated_dates: list) -> None:
    """Save both precise and estimated earnings dates to disk and memory."""
    path = _cache_path(ticker)
    now = datetime.now()
    data = {
        "ticker": ticker,
        "fetched": now.isoformat(),
        "dates": dates,
        "estimated_dates": estimated_dates,
    }
    try:
        with open(path, "w") as f:
            json.dump(data, f, indent=2)
        _memory_cache[ticker] = {
            "dates": dates,
            "estimated_dates": estimated_dates,
            "fetched": now,
        }
    except OSError as e:
        logger.warning(f"{ticker}: failed to cache earnings: {e}")


def _fetch_estimated_from_financials(ticker: str) -> List[str]:
    """Generate estimated earnings announcement dates from income_stmt FY-end dates.

    ASX companies must lodge preliminary results within 2 months of FY end.
    Typical delay is 40-60 days.  We use _ESTIMATED_OFFSET_DAYS (45) as center.

    For each FY-end date we generate:
      - FY  announcement : fy_end + offset
      - H1  announcement : (fy_end - 6 months) + offset

    Returns sorted list of ISO date strings.
    """
    estimated = []
    try:
        stock = yf.Ticker(ticker)
        inc = stock.income_stmt
        if inc is None or inc.empty:
            return []
        for col in inc.columns:
            fy_end = pd.Timestamp(col)
            # Full-year announcement
            fy_ann = fy_end + timedelta(days=_ESTIMATED_OFFSET_DAYS)
            estimated.append(fy_ann.strftime("%Y-%m-%d"))
            # Half-year announcement (6 months before FY end)
            h1_end = fy_end - pd.DateOffset(months=6)
            h1_ann = h1_end + timedelta(days=_ESTIMATED_OFFSET_DAYS)
            estimated.append(h1_ann.strftime("%Y-%m-%d"))
    except Exception as e:
        logger.debug(f"{ticker}: income_stmt estimation failed: {e}")
    return sorted(set(estimated))


def fetch_earnings_dates(ticker: str, use_cache: bool = True) -> dict:
    """Fetch precise and estimated earnings dates for a ticker.

    Returns dict:
        {
          'dates':           [list of ISO strings] - precise yfinance dates
          'estimated_dates': [list of ISO strings] - income_stmt-derived estimates
        }
    """
    if use_cache:
        cached = _load_cached_earnings(ticker)
        if cached is not None:
            return cached

    # --- 1. Precise dates from yfinance ---
    precise = []
    try:
        stock = yf.Ticker(ticker)
        try:
            ed = stock.earnings_dates
            if ed is not None and not ed.empty:
                for dt in ed.index:
                    ts = pd.Timestamp(dt)
                    if ts.tzinfo:
                        ts = ts.tz_localize(None)
                    precise.append(ts.strftime("%Y-%m-%d"))
        except Exception as e:
            logger.debug(f"{ticker}: earnings_dates failed: {e}")

        if not precise:
            try:
                cal = stock.calendar
                if cal is not None:
                    if isinstance(cal, dict):
                        for key in ["Earnings Date", "Earnings Average"]:
                            if key in cal:
                                val = cal[key]
                                if isinstance(val, list):
                                    for d in val:
                                        precise.append(pd.Timestamp(d).strftime("%Y-%m-%d"))
                                elif val is not None:
                                    precise.append(pd.Timestamp(val).strftime("%Y-%m-%d"))
                    elif isinstance(cal, pd.DataFrame) and not cal.empty:
                        if "Earnings Date" in cal.index:
                            val = cal.loc["Earnings Date"]
                            if hasattr(val, "__iter__"):
                                for v in val:
                                    if pd.notna(v):
                                        precise.append(pd.Timestamp(v).strftime("%Y-%m-%d"))
            except Exception as e:
                logger.debug(f"{ticker}: calendar failed: {e}")
    except Exception as e:
        logger.debug(f"{ticker}: yfinance error: {e}")

    precise = sorted(set(precise))

    # --- 2. Estimated dates from income_stmt ---
    estimated = _fetch_estimated_from_financials(ticker)

    if use_cache:
        _save_cached_earnings(ticker, precise, estimated)

    return {"dates": precise, "estimated_dates": estimated}


def get_next_earnings_date(
    ticker: str,
    reference_date: Optional[pd.Timestamp] = None,
) -> Optional[pd.Timestamp]:
    """Get the next precise earnings date on or after reference_date."""
    if reference_date is None:
        reference_date = pd.Timestamp.now().normalize()
    else:
        reference_date = pd.Timestamp(reference_date).normalize()

    data = fetch_earnings_dates(ticker)
    for d in data["dates"]:
        dt = pd.Timestamp(d)
        if dt >= reference_date:
            return dt
    return None


def is_near_earnings(
    ticker: str,
    reference_date: Optional[pd.Timestamp] = None,
    blackout_days_before: int = 5,
    blackout_days_after: int = 1,
    estimated_days_before: int = 20,
    estimated_days_after: int = 15,
) -> bool:
    """Check if a date falls within an earnings blackout window.

    Checks two tiers:
      - Precise dates   : narrow window (blackout_days_before / blackout_days_after)
      - Estimated dates : wide window   (estimated_days_before / estimated_days_after)

    Args:
        ticker: ASX ticker symbol.
        reference_date: Date to check (default: today).
        blackout_days_before: Days before precise earnings to block (default 5).
        blackout_days_after:  Days after  precise earnings to block (default 1).
        estimated_days_before: Days before estimated date to block (default 20).
        estimated_days_after:  Days after  estimated date to block (default 15).

    Returns:
        True if the date falls within any blackout window.
    """
    if reference_date is None:
        reference_date = pd.Timestamp.now().normalize()
    else:
        reference_date = pd.Timestamp(reference_date).normalize()

    data = fetch_earnings_dates(ticker)

    # Check precise dates (narrow window)
    for d in data["dates"]:
        earnings_dt = pd.Timestamp(d)
        window_start = earnings_dt - pd.Timedelta(days=blackout_days_before)
        window_end   = earnings_dt + pd.Timedelta(days=blackout_days_after)
        if window_start <= reference_date <= window_end:
            logger.debug(
                f"{ticker}: {reference_date.date()} in PRECISE earnings blackout "
                f"(earnings={earnings_dt.date()}, window {window_start.date()}–{window_end.date()})"
            )
            return True

    # Check estimated dates (wide window)
    for d in data["estimated_dates"]:
        est_dt = pd.Timestamp(d)
        window_start = est_dt - pd.Timedelta(days=estimated_days_before)
        window_end   = est_dt + pd.Timedelta(days=estimated_days_after)
        if window_start <= reference_date <= window_end:
            logger.debug(
                f"{ticker}: {reference_date.date()} in ESTIMATED earnings blackout "
                f"(est_date={est_dt.date()}, window {window_start.date()}–{window_end.date()})"
            )
            return True

    return False


def clear_cache() -> int:
    """Clear all cached earnings data. Returns count of files removed."""
    count = 0
    _memory_cache.clear()
    for f in _CACHE_DIR.glob("*_earnings.json"):
        f.unlink()
        count += 1
    return count
