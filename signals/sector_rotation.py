"""Sector ETF rotation signals — defensive rotation detection.

Ranks the 11 SPDR sector ETFs by 63-day rate of change (ROC).
Flags risk-off when defensive sectors (XLU, XLP) rank in the top 3.

Usage:
    from signals.sector_rotation import get_sector_rotation_signal
"""
from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

from db.atlas_db import get_db

logger = logging.getLogger(__name__)

# The 11 SPDR Select Sector ETFs
SPDR_SECTORS = {
    "XLB": "Materials",
    "XLC": "Communication Services",
    "XLE": "Energy",
    "XLF": "Financials",
    "XLI": "Industrials",
    "XLK": "Technology",
    "XLP": "Consumer Staples",
    "XLRE": "Real Estate",
    "XLU": "Utilities",
    "XLV": "Health Care",
    "XLY": "Consumer Discretionary",
}

DEFENSIVE_ETFS = {"XLU", "XLP"}

# yfinance fallback cache
CACHE_PATH = Path(__file__).parent.parent / "data" / "cache" / "sector_etfs.json"
_CACHE_TTL_DAYS = 7


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _fetch_from_yfinance(
    end_date: date,
    roc_period: int,
) -> dict[str, list[tuple[str, float]]]:
    """Fetch ETF closing prices from yfinance with a 7-day file cache.

    Returns ``{ticker: [(date_str, close), ...]}`` sorted ascending by date.
    """
    import yfinance as yf  # optional dependency — only used as fallback

    # ---- cache hit? --------------------------------------------------------
    if CACHE_PATH.exists():
        try:
            with CACHE_PATH.open() as fh:
                cache = json.load(fh)
            cached_at = datetime.fromisoformat(cache.get("cached_at", "2000-01-01"))
            age_days = (datetime.now() - cached_at).days
            if age_days < _CACHE_TTL_DAYS:
                logger.info(
                    "sector_rotation: using cached ETF prices from %s (%d days old)",
                    cached_at.date(),
                    age_days,
                )
                return {
                    ticker: [(d, c) for d, c in rows]
                    for ticker, rows in cache["prices"].items()
                }
        except Exception as exc:
            logger.warning("sector_rotation: cache read error, re-fetching: %s", exc)

    # ---- live fetch --------------------------------------------------------
    tickers = list(SPDR_SECTORS.keys())
    # 63 trading days ≈ 90 calendar days; double + buffer to be safe
    start = end_date - timedelta(days=roc_period * 2 + 30)

    logger.info(
        "sector_rotation: fetching from yfinance %s → %s", start, end_date
    )
    prices: dict[str, list[tuple[str, float]]] = {}
    try:
        raw = yf.download(
            tickers,
            start=start.isoformat(),
            end=(end_date + timedelta(days=1)).isoformat(),
            auto_adjust=True,
            progress=False,
        )
        close_df = raw["Close"] if hasattr(raw, "columns") and "Close" in raw.columns else raw
        for ticker in tickers:
            if ticker not in close_df.columns:
                logger.warning("sector_rotation: %s missing from yfinance response", ticker)
                continue
            series = close_df[ticker].dropna()
            prices[ticker] = [(str(idx.date()), float(val)) for idx, val in series.items()]
    except Exception as exc:
        logger.error("sector_rotation: yfinance fetch failed: %s", exc)
        return {}

    # ---- persist cache -----------------------------------------------------
    try:
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        with CACHE_PATH.open("w") as fh:
            json.dump({"cached_at": datetime.now().isoformat(), "prices": prices}, fh)
        logger.info("sector_rotation: wrote ETF cache to %s", CACHE_PATH)
    except Exception as exc:
        logger.warning("sector_rotation: cache write failed: %s", exc)

    return prices


def _load_prices_from_db(
    end_date: date,
    roc_period: int,
) -> dict[str, list[tuple[str, float]]]:
    """Query ohlcv for all 11 SPDR ETFs.

    Returns ``{ticker: [(date_str, close), ...]}`` sorted ascending by date.
    """
    # 63 trading days ≈ 90 calendar days; 2× buffer is generous
    start = end_date - timedelta(days=roc_period * 2 + 30)
    tickers = list(SPDR_SECTORS.keys())
    placeholders = ",".join("?" * len(tickers))

    with get_db() as db:
        rows = db.execute(
            f"SELECT ticker, date, close FROM ohlcv "
            f"WHERE ticker IN ({placeholders}) "
            f"AND date BETWEEN ? AND ? "
            f"ORDER BY ticker, date",
            tickers + [start.isoformat(), end_date.isoformat()],
        ).fetchall()

    prices: dict[str, list[tuple[str, float]]] = {}
    for r in rows:
        ticker = r["ticker"]
        close = r["close"]
        if close is None:
            continue
        prices.setdefault(ticker, []).append((r["date"], float(close)))

    return prices


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def rank_sectors_by_momentum(
    end_date: Optional[date] = None,
    roc_period: int = 63,
) -> list[dict]:
    """Rank the 11 SPDR sector ETFs by 63-day rate of change (ROC).

    Tries the local SQLite DB first.  Falls back to yfinance (with 7-day
    file cache) if any ETF is missing or lacks enough trading-day history.

    Args:
        end_date:   Reference date for "today" (defaults to :func:`date.today`).
        roc_period: Number of *trading* days for the ROC window (default 63).

    Returns:
        List of dicts sorted by ROC descending::

            [
                {"etf": "XLK", "sector": "Technology", "roc_63d": 12.5, "rank": 1},
                ...
            ]
    """
    end = end_date or date.today()
    min_rows_needed = roc_period + 1  # need close[0] and close[roc_period]

    # ---- attempt DB path ---------------------------------------------------
    prices = _load_prices_from_db(end, roc_period)

    # Check coverage: every ETF must have enough rows
    db_ok = all(
        etf in prices and len(prices[etf]) >= min_rows_needed
        for etf in SPDR_SECTORS
    )

    if not db_ok:
        missing = [
            etf
            for etf in SPDR_SECTORS
            if etf not in prices or len(prices[etf]) < min_rows_needed
        ]
        logger.info(
            "sector_rotation: DB insufficient for %s ETF(s) (%s) — falling back to yfinance",
            len(missing),
            ", ".join(missing),
        )
        yf_prices = _fetch_from_yfinance(end, roc_period)
        # Merge: yfinance wins for missing/insufficient tickers
        for etf in missing:
            if etf in yf_prices:
                prices[etf] = yf_prices[etf]

    # ---- compute ROC for each ETF ------------------------------------------
    rankings: list[dict] = []
    for etf, sector in SPDR_SECTORS.items():
        rows = prices.get(etf, [])
        if len(rows) < min_rows_needed:
            logger.warning(
                "sector_rotation: %s has only %d rows (need %d) — skipping",
                etf,
                len(rows),
                min_rows_needed,
            )
            continue

        # rows are date-ascending; use last entry as current, step back roc_period
        current_close = rows[-1][1]
        past_close = rows[-(roc_period + 1)][1]  # roc_period trading days ago

        if past_close <= 0:
            logger.warning("sector_rotation: %s past_close=%.4f <= 0, skipping", etf, past_close)
            continue

        roc = ((current_close - past_close) / past_close) * 100.0
        rankings.append(
            {
                "etf": etf,
                "sector": sector,
                "roc_63d": round(roc, 2),
                "rank": 0,  # filled below
            }
        )

    # Sort descending by ROC and assign rank
    rankings.sort(key=lambda x: x["roc_63d"], reverse=True)
    for i, entry in enumerate(rankings):
        entry["rank"] = i + 1

    return rankings


def detect_defensive_rotation(rankings: list[dict]) -> dict:
    """Inspect a rankings list for defensive-sector dominance.

    Args:
        rankings: Output of :func:`rank_sectors_by_momentum`.

    Returns:
        ::

            {
                "defensive_rotation": True/False,
                "defensive_in_top3": ["XLU"],
                "top3": [{"etf": "XLU", "roc_63d": 8.2}, ...],
                "bottom3": [{"etf": "XLY", "roc_63d": -5.1}, ...],
                "severity": "none" | "moderate" | "high",
            }
    """
    if not rankings:
        return {
            "defensive_rotation": False,
            "defensive_in_top3": [],
            "top3": [],
            "bottom3": [],
            "severity": "none",
        }

    top3 = rankings[:3]
    bottom3 = rankings[-3:]

    defensive_in_top3 = [r["etf"] for r in top3 if r["etf"] in DEFENSIVE_ETFS]
    defensive_rotation = len(defensive_in_top3) > 0

    if len(defensive_in_top3) >= 2:
        severity = "high"
    elif len(defensive_in_top3) == 1:
        severity = "moderate"
    else:
        severity = "none"

    return {
        "defensive_rotation": defensive_rotation,
        "defensive_in_top3": defensive_in_top3,
        "top3": [{"etf": r["etf"], "roc_63d": r["roc_63d"]} for r in top3],
        "bottom3": [{"etf": r["etf"], "roc_63d": r["roc_63d"]} for r in bottom3],
        "severity": severity,
    }


def get_sector_rotation_signal(end_date: Optional[date] = None) -> dict:
    """Main entry point — compute sector rotation signal.

    Args:
        end_date: Reference date (defaults to today).

    Returns:
        ::

            {
                "as_of": "2026-04-13",
                "rankings": [...],
                "defensive_rotation": True/False,
                "defensive_in_top3": [...],
                "severity": "none" | "moderate" | "high",
                "risk_off_signal": True/False,
                "top3_sectors": ["Utilities", "Consumer Staples", "Health Care"],
                "bottom3_sectors": ["Technology", "Energy", "Consumer Discretionary"],
            }
    """
    end = end_date or date.today()

    rankings = rank_sectors_by_momentum(end_date=end)
    detection = detect_defensive_rotation(rankings)

    top3_sectors = [SPDR_SECTORS.get(r["etf"], r["etf"]) for r in rankings[:3]]
    bottom3_sectors = [SPDR_SECTORS.get(r["etf"], r["etf"]) for r in rankings[-3:]]

    return {
        "as_of": end.isoformat(),
        "rankings": rankings,
        "defensive_rotation": detection["defensive_rotation"],
        "defensive_in_top3": detection["defensive_in_top3"],
        "severity": detection["severity"],
        "risk_off_signal": detection["defensive_rotation"],  # alias for regime integration
        "top3_sectors": top3_sectors,
        "bottom3_sectors": bottom3_sectors,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import logging

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    signal = get_sector_rotation_signal()

    print("\nSECTOR ROTATION SIGNAL")
    print("=" * 60)
    print(f"As of:               {signal['as_of']}")
    print(f"Defensive rotation:  {signal['defensive_rotation']}")
    print(f"Severity:            {signal['severity']}")
    print(f"Risk-off signal:     {signal['risk_off_signal']}")
    if signal["defensive_in_top3"]:
        names = ", ".join(
            f"{etf} ({SPDR_SECTORS[etf]})" for etf in signal["defensive_in_top3"]
        )
        print(f"Defensive in top 3:  {names}")
    print()
    print("RANKINGS (63-day ROC):")
    print(f"  {'Rank':<5} {'ETF':<6} {'Sector':<28} {'ROC %':>8}")
    print("  " + "-" * 52)
    for r in signal["rankings"]:
        marker = " ◄ DEF" if r["etf"] in DEFENSIVE_ETFS else ""
        print(f"  {r['rank']:<5} {r['etf']:<6} {r['sector']:<28} {r['roc_63d']:>8.2f}{marker}")
    print()
    print(f"Top 3 sectors:    {', '.join(signal['top3_sectors'])}")
    print(f"Bottom 3 sectors: {', '.join(signal['bottom3_sectors'])}")
    print("=" * 60)
