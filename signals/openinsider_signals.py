"""
signals.openinsider_signals — OpenInsider-derived signal generation.

Reads insider-trade records persisted to the ``news_intel`` table by
``overlay.sources.alt_data.AltDataCollector``, then converts them into a
normalised [-1, +1] signal per ticker.

Score heuristic
---------------
    +1 : significant net insider buying (many large buy transactions)
    -1 : significant net insider selling (many large sell transactions)
     0 : no data or neutral net flow

Data model (news_intel rows)
----------------------------
    source    = "openinsider"
    category  = "insider_trade"
    headline  = "Insider Buy: ... bought N shares of TICKER ($VALUE)"
    url       = "http://openinsider.com/TICKER"
    summary   = JSON string with keys: trade_type, value, qty, price, ...
    relevance_score = 0.6 (buys, scaled up) / 0.4 (sells)

Public API
----------
    load_openinsider_data(date=None) -> pd.DataFrame
    score_insider_signal(ticker, df) -> float
"""

from __future__ import annotations

import json
import logging
import re
from datetime import date, datetime, timedelta, timezone
from typing import Optional
from urllib.parse import urlparse

import pandas as pd

logger = logging.getLogger(__name__)

# Module-level import for test patchability (patch "signals.openinsider_signals.get_news")
try:
    from db.atlas_db import get_news
except ImportError:  # minimal test environments
    def get_news(*args, **kwargs):  # type: ignore[misc]
        return []

# Lookback window (days) when no explicit date is given.
_DEFAULT_LOOKBACK_DAYS: int = 30

# Weighting: CEO/President/Chairman insider trades worth more.
_SENIOR_TITLE_MULT: float = 1.5
_SENIOR_TITLES = frozenset({"ceo", "cfo", "president", "chairman", "director"})

# Score clamp bounds.
_SCORE_MIN: float = -1.0
_SCORE_MAX: float = 1.0


# ── Data loader ───────────────────────────────────────────────────────────────


def load_openinsider_data(date_: Optional[date] = None) -> pd.DataFrame:
    """Load OpenInsider records from news_intel for the recent *lookback* window.

    Parameters
    ----------
    date_ : Upper-bound date (inclusive).  Defaults to today (UTC).

    Returns
    -------
    DataFrame with columns:
        ticker, trade_date, insider_name, title, trade_type,
        value, qty, price, filing_date, relevance_score, timestamp

    Returns an empty DataFrame (correct schema) when no data is available
    or the DB is unreachable.
    """
    _SCHEMA = {
        "ticker": "object",
        "trade_date": "object",
        "insider_name": "object",
        "title": "object",
        "trade_type": "object",
        "value": "float64",
        "qty": "float64",
        "price": "float64",
        "filing_date": "object",
        "relevance_score": "float64",
        "timestamp": "object",
    }

    try:
        if date_ is None:
            date_ = datetime.now(tz=timezone.utc).date()

        cutoff_dt = datetime.combine(
            date_ - timedelta(days=_DEFAULT_LOOKBACK_DAYS),
            datetime.min.time(),
        ).isoformat()

        records = get_news(
            days=_DEFAULT_LOOKBACK_DAYS + 1,
            category="insider_trade",
        )
        # Filter to openinsider source only
        records = [r for r in records if r.get("source") == "openinsider"]

        if not records:
            logger.debug("openinsider_signals: no records in news_intel")
            return pd.DataFrame({k: pd.Series(dtype=v) for k, v in _SCHEMA.items()})

        rows: list[dict] = []
        for rec in records:
            ticker = _extract_ticker_from_url(rec.get("url", ""))
            if not ticker:
                continue

            # Parse JSON summary for trade details.
            detail: dict = {}
            try:
                raw_summary = rec.get("summary") or "{}"
                detail = json.loads(raw_summary)
            except (json.JSONDecodeError, TypeError):
                pass

            rows.append(
                {
                    "ticker": ticker,
                    "trade_date": detail.get("trade_date", ""),
                    "insider_name": detail.get("insider_name", ""),
                    "title": detail.get("title", ""),
                    "trade_type": detail.get("trade_type", ""),
                    "value": float(detail.get("value") or 0),
                    "qty": float(detail.get("qty") or 0),
                    "price": float(detail.get("price") or 0),
                    "filing_date": detail.get("filing_date", ""),
                    "relevance_score": float(rec.get("relevance_score") or 0),
                    "timestamp": rec.get("timestamp", ""),
                }
            )

        if not rows:
            return pd.DataFrame({k: pd.Series(dtype=v) for k, v in _SCHEMA.items()})

        df = pd.DataFrame(rows)
        for col, dtype in _SCHEMA.items():
            if col in df.columns:
                df[col] = df[col].astype(dtype, errors="ignore")

        logger.info(
            "openinsider_signals: loaded %d records for %d tickers",
            len(df),
            df["ticker"].nunique() if len(df) else 0,
        )
        return df

    except Exception as exc:
        logger.warning("openinsider_signals: load failed — %s", exc)
        return pd.DataFrame({k: pd.Series(dtype=v) for k, v in _SCHEMA.items()})


def _extract_ticker_from_url(url: str) -> str:
    """Extract ticker symbol from an OpenInsider URL.

    URL format: ``http://openinsider.com/TICKER``
    """
    if not url:
        return ""
    try:
        path = urlparse(url).path  # e.g. "/AAPL"
        parts = [p for p in path.split("/") if p]
        return parts[-1].upper() if parts else ""
    except Exception:
        return ""


# ── Signal scorer ─────────────────────────────────────────────────────────────


def score_insider_signal(ticker: str, df: pd.DataFrame) -> float:
    """Return a normalised [-1, +1] insider signal for *ticker*.

    Scoring
    -------
    Each transaction contributes a signed weighted value:
        sign = +1 for purchases, -1 for sales
        weight = abs(value) * title_multiplier

    Final score = tanh(net_weighted_flow / normalisation_constant)

    Returns 0.0 if ticker has no records in *df* or df is empty.
    """
    if df is None or df.empty:
        return 0.0

    ticker_df = df[df["ticker"].str.upper() == ticker.upper()]
    if ticker_df.empty:
        return 0.0

    net_flow = 0.0
    total_abs_flow = 0.0

    for _, row in ticker_df.iterrows():
        trade_type = str(row.get("trade_type", "")).lower()
        raw_value = float(row.get("value", 0))
        title = str(row.get("title", "")).lower()

        # Determine buy (+) or sell (-) from trade_type and sign of value.
        if "purchase" in trade_type or "buy" in trade_type or ("p - " in trade_type):
            sign = +1.0
        elif "sale" in trade_type or "sell" in trade_type or ("s - " in trade_type):
            sign = -1.0
        else:
            # Infer from sign of value/qty (positive = buy, negative = sell).
            sign = +1.0 if raw_value >= 0 else -1.0

        abs_value = abs(raw_value)

        # Senior title multiplier.
        title_mult = _SENIOR_TITLE_MULT if any(t in title for t in _SENIOR_TITLES) else 1.0

        weighted = sign * abs_value * title_mult
        net_flow += weighted
        total_abs_flow += abs_value * title_mult

    if total_abs_flow == 0:
        return 0.0

    import math

    # tanh squashes to (-1, +1) regardless of dollar magnitude.
    # Normalise so that $10M net flow produces a score ≈ 0.46.
    norm_constant = 10_000_000.0
    raw_score = math.tanh(net_flow / norm_constant)
    return float(max(_SCORE_MIN, min(_SCORE_MAX, raw_score)))
