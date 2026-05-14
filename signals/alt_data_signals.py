"""
signals.alt_data_signals — Combined alt-data signal integration.

Combines OpenInsider and Finviz signals into a single blended score that can
be injected into the plan generator as an optional input.

Feature flag
------------
Controlled by ``config/active/{market_id}.json`` → ``alt_data.enabled``.
When ``enabled=false`` (the default for new deployments), all functions
return ``None`` (log-only mode).  When ``enabled=true``, scores are computed
and returned for use by the plan generator.

Public API
----------
    get_alt_data_score(ticker, market_id="sp500") -> float | None
    get_alt_data_scores_bulk(tickers, market_id="sp500") -> dict[str, float]
    is_alt_data_enabled(market_id="sp500") -> bool
"""

from __future__ import annotations

import logging
from functools import lru_cache
from typing import Optional

import pandas as pd

logger = logging.getLogger(__name__)

# Module-level imports for test patchability
try:
    from utils.config import get_active_config
except ImportError:
    def get_active_config(market_id: str = "sp500") -> dict:  # type: ignore[misc]
        return {}

# Weight split: insider signal more predictive, Finviz adds supporting evidence.
_INSIDER_WEIGHT: float = 0.6
_FINVIZ_WEIGHT: float = 0.4

# In-process cache TTL: reload DataFrames at most once per N calls per process.
# Reset between plan cycles automatically (process restarts or LRU eviction).
_CACHE_MAXSIZE: int = 4

# ── Feature flag ──────────────────────────────────────────────────────────────


def is_alt_data_enabled(market_id: str = "sp500") -> bool:
    """Return True if alt_data is enabled in the active config for *market_id*.

    Config key: ``config/active/{market_id}.json`` → ``alt_data.enabled``
    Default: False (log-only mode) if key is missing or config load fails.
    """
    try:
        cfg = get_active_config(market_id)
        return bool(cfg.get("alt_data", {}).get("enabled", False))
    except Exception as exc:
        logger.debug("alt_data_signals: config load failed for %s — %s", market_id, exc)
        return False


# ── Cached data loaders ───────────────────────────────────────────────────────

@lru_cache(maxsize=_CACHE_MAXSIZE)
def _cached_openinsider_df(_cache_key: str) -> pd.DataFrame:
    """Load OpenInsider data (cached per cache-key to avoid repeated DB calls)."""
    from signals.openinsider_signals import load_openinsider_data
    return load_openinsider_data()


@lru_cache(maxsize=_CACHE_MAXSIZE)
def _cached_finviz_df(_cache_key: str) -> pd.DataFrame:
    """Load Finviz data (cached per cache-key)."""
    from signals.finviz_signals import load_finviz_data
    return load_finviz_data()


def _today_key() -> str:
    """Cache key based on today's UTC date — ensures daily refresh."""
    from datetime import datetime, timezone
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")


# ── Public scoring functions ──────────────────────────────────────────────────


def get_alt_data_score(
    ticker: str,
    market_id: str = "sp500",
) -> Optional[float]:
    """Return a blended alt-data signal score in [-1, +1] for *ticker*.

    Returns None when:
    - alt_data is disabled in config (log-only mode)
    - no data available for this ticker
    - any error occurs (fail-soft)

    Parameters
    ----------
    ticker    : Ticker symbol (case-insensitive).
    market_id : Active market config key (default "sp500").
    """
    if not is_alt_data_enabled(market_id):
        logger.debug(
            "alt_data_signals: feature disabled for %s — score=None (log-only mode)",
            market_id,
        )
        return None

    try:
        from signals.openinsider_signals import score_insider_signal
        from signals.finviz_signals import score_finviz_signal

        cache_key = _today_key()
        insider_df = _cached_openinsider_df(cache_key)
        finviz_df = _cached_finviz_df(cache_key)

        insider_score = score_insider_signal(ticker, insider_df)
        finviz_score = score_finviz_signal(ticker, finviz_df)

        # Detect if we have actual data for this ticker.
        has_insider = not insider_df.empty and (
            ticker.upper() in insider_df["ticker"].str.upper().values
        )
        has_finviz = not finviz_df.empty and (
            ticker.upper() in finviz_df["ticker"].str.upper().values
        )

        if not has_insider and not has_finviz:
            logger.debug("alt_data_signals: no data for %s — returning None", ticker)
            return None

        # Blend using available weights.
        if has_insider and has_finviz:
            blended = _INSIDER_WEIGHT * insider_score + _FINVIZ_WEIGHT * finviz_score
        elif has_insider:
            blended = insider_score
        else:
            blended = finviz_score

        blended = float(max(-1.0, min(1.0, blended)))

        logger.debug(
            "alt_data_signals: %s insider=%.3f finviz=%.3f blended=%.3f",
            ticker,
            insider_score,
            finviz_score,
            blended,
        )
        return blended

    except Exception as exc:
        logger.warning("alt_data_signals: score failed for %s — %s", ticker, exc)
        return None


def get_alt_data_scores_bulk(
    tickers: list[str],
    market_id: str = "sp500",
) -> dict[str, float]:
    """Return alt-data scores for multiple tickers in one DB-efficient call.

    Returns an empty dict when alt_data is disabled or all lookups fail.

    Scores with no data are omitted (tickers with score=None are excluded).
    """
    if not is_alt_data_enabled(market_id):
        return {}

    scores: dict[str, float] = {}
    for ticker in tickers:
        score = get_alt_data_score(ticker, market_id=market_id)
        if score is not None:
            scores[ticker] = score

    if scores:
        logger.info(
            "alt_data_signals: bulk scored %d/%d tickers for %s",
            len(scores),
            len(tickers),
            market_id,
        )

    return scores


def inject_alt_data_into_signals(
    signals_list: list[dict],
    market_id: str = "sp500",
) -> list[dict]:
    """Inject alt-data scores into an existing signals list (non-destructive).

    Each signal dict in *signals_list* gains an ``"alt_data_score"`` key when
    the feature is enabled AND data is available.  The list is returned
    unchanged (scores added in-place) when disabled.

    This is the hook for plan generators:

        signals = strategy.generate_signals(data)
        signals = inject_alt_data_into_signals(signals, market_id)

    Parameters
    ----------
    signals_list : List of signal dicts; each must have a ``"ticker"`` key.
    market_id    : Market to check feature flag against.
    """
    if not is_alt_data_enabled(market_id):
        return signals_list

    tickers = [s.get("ticker", "") for s in signals_list if s.get("ticker")]
    if not tickers:
        return signals_list

    scores = get_alt_data_scores_bulk(tickers, market_id=market_id)

    for signal in signals_list:
        ticker = signal.get("ticker", "")
        if ticker in scores:
            signal["alt_data_score"] = scores[ticker]

    return signals_list
