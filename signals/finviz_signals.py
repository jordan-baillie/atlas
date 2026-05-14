"""
signals.finviz_signals — Finviz screener-derived signal generation.

Reads Finviz fundamental snapshot records persisted to the ``news_intel``
table by ``overlay.sources.alt_data.AltDataCollector``, then converts
per-ticker metrics into a normalised [-1, +1] signal.

Score heuristic
---------------
    Positive: strong recent momentum (perf_week/month), low short float,
              high institutional ownership — suggests fundamental support.
    Negative: weak/negative momentum, high short float — bearish pressure.

Data model (news_intel rows)
----------------------------
    source    = "finviz"
    category  = "fundamentals"
    headline  = "TICKER Snapshot: P/E X, ..."
    url       = "https://finviz.com/quote.ashx?t=TICKER"
    summary   = JSON string with keys:
                  pe, eps_ttm, perf_week, perf_month, perf_quarter,
                  rel_volume, short_float, inst_own, market_cap

Public API
----------
    load_finviz_data(date=None) -> pd.DataFrame
    score_finviz_signal(ticker, df) -> float
"""

from __future__ import annotations

import json
import logging
import re
from datetime import date, datetime, timedelta, timezone
from typing import Optional
from urllib.parse import parse_qs, urlparse

import pandas as pd

logger = logging.getLogger(__name__)

# Module-level import for test patchability (patch "signals.finviz_signals.get_news")
try:
    from db.atlas_db import get_news
except ImportError:
    def get_news(*args, **kwargs):  # type: ignore[misc]
        return []

_DEFAULT_LOOKBACK_DAYS: int = 30
_SCORE_MIN: float = -1.0
_SCORE_MAX: float = 1.0


# ── Helpers ───────────────────────────────────────────────────────────────────

def _parse_pct(value: str | float | None) -> float | None:
    """Parse a percentage string like '4.41%' or '-2.3%' to float (0.0441)."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).replace("%", "").strip()) / 100.0
    except (ValueError, AttributeError):
        return None


def _parse_float(value: str | float | None) -> float | None:
    """Parse a float from a string, returning None on failure."""
    if value is None:
        return None
    try:
        return float(str(value).replace(",", "").strip())
    except (ValueError, AttributeError):
        return None


def _extract_ticker_from_url(url: str) -> str:
    """Extract ticker from a Finviz URL.

    URL format: ``https://finviz.com/quote.ashx?t=TICKER``
    """
    if not url:
        return ""
    try:
        params = parse_qs(urlparse(url).query)
        tickers = params.get("t", [])
        return tickers[0].upper() if tickers else ""
    except Exception:
        return ""


def _extract_ticker_from_headline(headline: str) -> str:
    """Fallback: extract leading ticker from 'TICKER Snapshot: ...'."""
    m = re.match(r"^([A-Z0-9\-\.]+)\s+Snapshot:", headline or "")
    return m.group(1) if m else ""


# ── Data loader ───────────────────────────────────────────────────────────────

def load_finviz_data(date_: Optional[date] = None) -> pd.DataFrame:
    """Load Finviz fundamental snapshot records from news_intel.

    Parameters
    ----------
    date_ : Upper-bound date (inclusive).  Defaults to today (UTC).

    Returns
    -------
    DataFrame with columns:
        ticker, pe, eps_ttm, perf_week, perf_month, perf_quarter,
        rel_volume, short_float, inst_own, market_cap, timestamp

    Returns an empty DataFrame (correct schema) when no data is available.
    """
    _SCHEMA = {
        "ticker": "object",
        "pe": "float64",
        "eps_ttm": "float64",
        "perf_week": "float64",
        "perf_month": "float64",
        "perf_quarter": "float64",
        "rel_volume": "float64",
        "short_float": "float64",
        "inst_own": "float64",
        "market_cap": "object",
        "timestamp": "object",
    }

    try:
        records = get_news(
            days=_DEFAULT_LOOKBACK_DAYS + 1,
            category="fundamentals",
        )
        # Filter to finviz snapshot source only (not finviz_news)
        records = [r for r in records if r.get("source") == "finviz"]

        if not records:
            logger.debug("finviz_signals: no finviz fundamentals in news_intel")
            return pd.DataFrame({k: pd.Series(dtype=v) for k, v in _SCHEMA.items()})

        rows: list[dict] = []
        for rec in records:
            # Try URL first, then headline.
            ticker = _extract_ticker_from_url(rec.get("url", ""))
            if not ticker:
                ticker = _extract_ticker_from_headline(rec.get("headline", ""))
            if not ticker:
                continue

            detail: dict = {}
            try:
                raw_summary = rec.get("summary") or "{}"
                detail = json.loads(raw_summary)
            except (json.JSONDecodeError, TypeError):
                pass

            rows.append(
                {
                    "ticker": ticker,
                    "pe": _parse_float(detail.get("pe")),
                    "eps_ttm": _parse_float(detail.get("eps_ttm")),
                    "perf_week": _parse_pct(detail.get("perf_week")),
                    "perf_month": _parse_pct(detail.get("perf_month")),
                    "perf_quarter": _parse_pct(detail.get("perf_quarter")),
                    "rel_volume": _parse_float(detail.get("rel_volume")),
                    "short_float": _parse_pct(detail.get("short_float")),
                    "inst_own": _parse_pct(detail.get("inst_own")),
                    "market_cap": detail.get("market_cap"),
                    "timestamp": rec.get("timestamp", ""),
                }
            )

        if not rows:
            return pd.DataFrame({k: pd.Series(dtype=v) for k, v in _SCHEMA.items()})

        df = pd.DataFrame(rows)
        # Keep only the most recent snapshot per ticker.
        df = df.sort_values("timestamp", ascending=False).drop_duplicates(
            subset=["ticker"], keep="first"
        )

        logger.info(
            "finviz_signals: loaded %d snapshot(s) for %d tickers",
            len(df),
            df["ticker"].nunique(),
        )
        return df

    except Exception as exc:
        logger.warning("finviz_signals: load failed — %s", exc)
        return pd.DataFrame({k: pd.Series(dtype=v) for k, v in _SCHEMA.items()})


# ── Signal scorer ─────────────────────────────────────────────────────────────

def score_finviz_signal(ticker: str, df: pd.DataFrame) -> float:
    """Return a normalised [-1, +1] Finviz fundamental signal for *ticker*.

    Scoring
    -------
    Four equally-weighted sub-scores are averaged then clamped:

    1. Momentum score  (perf_week + perf_month/2) — normalised at ±5%/±10%
    2. Short-float score — high short float → bearish (-1); low → neutral/bullish
    3. Institutional ownership — high inst_own → bullish (smart-money conviction)
    4. Relative volume    — rel_volume > 1.5 is a directional interest signal

    Returns 0.0 if ticker has no matching row or df is empty.
    """
    if df is None or df.empty:
        return 0.0

    row = df[df["ticker"].str.upper() == ticker.upper()]
    if row.empty:
        return 0.0

    r = row.iloc[0]

    sub_scores: list[float] = []

    # ── 1. Momentum ───────────────────────────────────────────────────────────
    perf_w = _parse_float(r.get("perf_week"))
    perf_m = _parse_float(r.get("perf_month"))
    if perf_w is not None or perf_m is not None:
        mom = 0.0
        if perf_w is not None:
            mom += perf_w / 0.05  # normalise: 5% weekly = score 1.0
        if perf_m is not None:
            mom += (perf_m / 0.10) * 0.5  # 10% monthly, half-weight
        sub_scores.append(max(-1.0, min(1.0, mom)))

    # ── 2. Short float (negative signal when high) ─────────────────────────────
    short_float = _parse_float(r.get("short_float"))
    if short_float is not None:
        # >20% short float → score = -1.0; <2% → score = +0.5
        short_score = -short_float / 0.20  # 20% = -1.0
        sub_scores.append(max(-1.0, min(0.5, short_score)))

    # ── 3. Institutional ownership (positive signal when high) ─────────────────
    inst_own = _parse_float(r.get("inst_own"))
    if inst_own is not None:
        # 80%+ → 1.0; 30% → 0.375
        inst_score = (inst_own - 0.30) / 0.50  # maps [30%, 80%] → [0, 1]
        sub_scores.append(max(-0.5, min(1.0, inst_score)))

    # ── 4. Relative volume ────────────────────────────────────────────────────
    rel_vol = _parse_float(r.get("rel_volume"))
    if rel_vol is not None:
        # >1.5 = above avg interest; <0.5 = low interest
        rv_score = (rel_vol - 1.0) / 1.0  # maps 0 → -1, 1 → 0, 2 → +1
        sub_scores.append(max(-1.0, min(1.0, rv_score)))

    if not sub_scores:
        return 0.0

    score = sum(sub_scores) / len(sub_scores)
    return float(max(_SCORE_MIN, min(_SCORE_MAX, score)))
