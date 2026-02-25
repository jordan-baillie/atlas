"""Atlas Dividend Calendar Utility
=====================================
Fetches and caches ex-dividend dates, amounts, and estimated franking
percentages for tickers. Supports the DividendCapture strategy.

Franking credits are unique to the Australian tax system. Companies pay
tax at 30%, and fully franked dividends carry a tax credit equal to
30/70 of the cash dividend. This means a $0.70 cash dividend has a
grossed-up value of $1.00 ($0.70 cash + $0.30 franking credit).

For non-AU markets, franking is not applicable and estimate_franking_pct
returns 0.0. The grossed-up yield equals the raw yield.

Since yfinance doesn't provide franking percentages, we use sector-based
heuristics for ASX. Most large ASX companies (banks, miners, industrials)
pay fully franked dividends. REITs and some infrastructure stocks pay
unfranked or partially franked.

Usage:
    from utils.dividends import get_dividend_calendar, get_upcoming_exdates
    from utils.dividends import estimate_franking_pct, calc_grossed_up_yield
"""

import logging
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd
import yfinance as yf

logger = logging.getLogger(__name__)

# Cache directory for dividend data
_CACHE_DIR = Path(__file__).resolve().parent.parent / "data" / "cache" / "dividends"
_CACHE_DIR.mkdir(parents=True, exist_ok=True)

# In-memory cache to avoid repeated disk reads within a single run
_memory_cache: dict = {}

# Sector-based franking heuristics for ASX companies
_SECTOR_FRANKING_MAP = {
    "financial services": 1.0,
    "financials": 1.0,
    "banks": 1.0,
    "insurance": 1.0,
    "materials": 1.0,
    "basic materials": 1.0,
    "energy": 1.0,
    "industrials": 1.0,
    "consumer discretionary": 1.0,
    "consumer cyclical": 1.0,
    "consumer defensive": 1.0,
    "consumer staples": 1.0,
    "technology": 1.0,
    "healthcare": 1.0,
    "health care": 1.0,
    "communication services": 1.0,
    "communication": 1.0,
    "utilities": 0.8,
    "real estate": 0.0,
    "reit": 0.0,
    "a-reit": 0.0,
}

# Company-specific overrides for known franking patterns
_TICKER_FRANKING_OVERRIDES = {
    # Major banks - always 100% franked
    "CBA.AX": 1.0, "WBC.AX": 1.0, "NAB.AX": 1.0, "ANZ.AX": 1.0,
    "MQG.AX": 1.0, "BEN.AX": 1.0, "BOQ.AX": 1.0,
    # Major miners - typically 100% franked
    "BHP.AX": 1.0, "RIO.AX": 1.0, "FMG.AX": 1.0,
    # Telcos - fully franked
    "TLS.AX": 1.0,
    # REITs - unfranked (trust distributions)
    "GMG.AX": 0.0, "SCG.AX": 0.0, "GPT.AX": 0.0, "MGR.AX": 0.0,
    "DXS.AX": 0.0, "SGP.AX": 0.0, "CHC.AX": 0.0, "VCX.AX": 0.0,
    "BWP.AX": 0.0, "GOZ.AX": 0.0, "ARF.AX": 0.0, "CIP.AX": 0.0,
    "CLW.AX": 0.0, "ABP.AX": 0.0, "CNI.AX": 0.0, "DDR.AX": 0.0,
    # Infrastructure - stapled securities, typically unfranked
    "TCL.AX": 0.0, "SYD.AX": 0.0,
}


def _cache_path(ticker: str) -> Path:
    """Return the cache file path for a given ticker."""
    safe_name = ticker.replace(".", "_")
    return _CACHE_DIR / f"{safe_name}_dividends.json"


def _load_cached_dividends(ticker: str) -> Optional[Dict]:
    """Load cached dividend data. Stale after 7 days."""
    if ticker in _memory_cache:
        entry = _memory_cache[ticker]
        if (datetime.now() - entry["fetched"]).days < 7:
            return entry["data"]

    path = _cache_path(ticker)
    if not path.exists():
        return None

    try:
        with open(path, "r") as f:
            data = json.load(f)
        fetched = datetime.fromisoformat(data["fetched"])
        if (datetime.now() - fetched).days >= 7:
            return None
        _memory_cache[ticker] = {"data": data, "fetched": fetched}
        return data
    except (json.JSONDecodeError, KeyError, ValueError) as e:
        logger.debug("%s: corrupt dividend cache: %s", ticker, e)
        return None


def _save_cached_dividends(ticker: str, data: Dict) -> None:
    """Save dividend data to disk and memory cache."""
    path = _cache_path(ticker)
    now = datetime.now()
    data["fetched"] = now.isoformat()
    try:
        with open(path, "w") as f:
            json.dump(data, f, indent=2)
        _memory_cache[ticker] = {"data": data, "fetched": now}
    except OSError as e:
        logger.warning("%s: failed to cache dividends: %s", ticker, e)


def estimate_franking_pct(ticker: str, sector: str = "") -> float:
    """Estimate franking percentage based on sector heuristics.

    Args:
        ticker: ASX ticker symbol (e.g., 'BHP.AX').
        sector: GICS sector name (optional).

    Returns:
        Estimated franking percentage (0.0 to 1.0).
    """
    if ticker in _TICKER_FRANKING_OVERRIDES:
        return _TICKER_FRANKING_OVERRIDES[ticker]

    sector_lower = sector.lower().strip() if sector else ""
    if sector_lower:
        for key, franking in _SECTOR_FRANKING_MAP.items():
            if key in sector_lower:
                return franking

    # Default: assume 100% franked (most large ASX companies are)
    return 1.0


def calc_grossed_up_yield(
    dividend_amount: float,
    share_price: float,
    franking_pct: float = 1.0,
    corporate_tax_rate: float = 0.30,
) -> float:
    """Calculate grossed-up dividend yield including franking credits.

    For a fully franked dividend at 30% tax rate:
        grossed_up = cash_div / (1 - 0.30) = cash_div / 0.70

    Args:
        dividend_amount: Cash dividend per share.
        share_price: Current share price.
        franking_pct: Franking percentage (0.0 to 1.0).
        corporate_tax_rate: Australian corporate tax rate (default 30%).

    Returns:
        Grossed-up yield as a decimal (e.g., 0.03 = 3%).
    """
    if share_price <= 0 or dividend_amount <= 0:
        return 0.0

    franking_credit = dividend_amount * (
        franking_pct * corporate_tax_rate / (1 - corporate_tax_rate)
    )
    grossed_up_div = dividend_amount + franking_credit
    return grossed_up_div / share_price


def fetch_dividend_calendar(
    ticker: str,
    use_cache: bool = True,
) -> List[Dict[str, Any]]:
    """Fetch historical and upcoming ex-dividend dates for a ticker.

    Uses yfinance .dividends property which returns ex-dividend dates
    and cash dividend amounts.

    Args:
        ticker: ASX ticker symbol (e.g., 'BHP.AX').
        use_cache: Whether to use cached data (default True).

    Returns:
        List of dicts with keys: ex_date, amount, ticker.
    """
    if use_cache:
        cached = _load_cached_dividends(ticker)
        if cached is not None:
            return cached.get("dividends", [])

    dividends = []
    try:
        stock = yf.Ticker(ticker)
        divs = stock.dividends

        if divs is not None and len(divs) > 0:
            for dt, amount in divs.items():
                ts = pd.Timestamp(dt)
                if ts.tzinfo:
                    ts = ts.tz_localize(None)
                dividends.append({
                    "ex_date": ts.strftime("%Y-%m-%d"),
                    "amount": round(float(amount), 6),
                    "ticker": ticker,
                })
    except Exception as e:
        logger.warning("%s: failed to fetch dividends: %s", ticker, e)

    if use_cache:
        _save_cached_dividends(ticker, {
            "ticker": ticker,
            "dividends": dividends,
        })

    return dividends


def get_exdates_in_range(
    ticker: str,
    start_date: pd.Timestamp,
    end_date: pd.Timestamp,
) -> List[Dict[str, Any]]:
    """Get ex-dividend dates within a date range.

    Args:
        ticker: ASX ticker symbol.
        start_date: Range start (inclusive).
        end_date: Range end (inclusive).

    Returns:
        List of dividend event dicts within the range.
    """
    start_date = pd.Timestamp(start_date).normalize()
    end_date = pd.Timestamp(end_date).normalize()

    all_divs = fetch_dividend_calendar(ticker)
    return [
        div for div in all_divs
        if start_date <= pd.Timestamp(div["ex_date"]) <= end_date
    ]


def get_next_exdate(
    ticker: str,
    reference_date: pd.Timestamp,
    max_lookahead_days: int = 30,
) -> Optional[Dict[str, Any]]:
    """Get the next ex-dividend date on or after reference_date.

    Args:
        ticker: ASX ticker symbol.
        reference_date: Date to search from.
        max_lookahead_days: Maximum days to look ahead.

    Returns:
        Dividend event dict if found, else None.
    """
    reference_date = pd.Timestamp(reference_date).normalize()
    cutoff = reference_date + pd.Timedelta(days=max_lookahead_days)

    all_divs = fetch_dividend_calendar(ticker)
    for div in all_divs:
        ex_dt = pd.Timestamp(div["ex_date"])
        if reference_date <= ex_dt <= cutoff:
            return div
    return None


def is_ex_dividend_date(
    ticker: str,
    target_date: pd.Timestamp,
) -> Optional[Dict[str, Any]]:
    """Check if a specific date is an ex-dividend date.

    Args:
        ticker: ASX ticker symbol.
        target_date: Date to check.

    Returns:
        Dividend event dict if target_date is an ex-date, else None.
    """
    target_str = pd.Timestamp(target_date).strftime("%Y-%m-%d")
    all_divs = fetch_dividend_calendar(ticker)
    for div in all_divs:
        if div["ex_date"] == target_str:
            return div
    return None


def get_sector_for_ticker(ticker: str) -> str:
    """Get sector for a ticker from the sector map cache.

    Args:
        ticker: ASX ticker symbol.

    Returns:
        Sector string or empty string if unknown.
    """
    sector_map_path = Path(__file__).resolve().parent.parent / "data" / "processed" / "sector_map.json"
    try:
        if sector_map_path.exists():
            with open(sector_map_path, "r") as f:
                sector_map = json.load(f)
            return sector_map.get(ticker, "")
    except Exception:
        pass
    return ""


def clear_cache() -> int:
    """Clear all cached dividend data. Returns count of files removed."""
    count = 0
    _memory_cache.clear()
    for f in _CACHE_DIR.glob("*_dividends.json"):
        f.unlink()
        count += 1
    return count
