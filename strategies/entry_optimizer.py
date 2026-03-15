"""Entry price optimizer — refine daily signals using intraday data.

Strategies:
  - MR signals: look for opening dip below signal entry → set limit below open
  - Momentum signals: confirm breakout above opening range → market if confirmed
  - Default: use signal entry_price as-is (market-on-open)

Called from brokers/plan.py when config["intraday"]["entry_refinement"] is True.
Requires intraday bars from data/intraday.py.
"""
import logging
from dataclasses import dataclass
from typing import Dict, List, Optional

import pandas as pd

logger = logging.getLogger(__name__)

# Strategy groups for refinement logic
_MR_STRATEGIES = frozenset({"mean_reversion", "connors_rsi2", "short_term_mr"})
_MOMENTUM_STRATEGIES = frozenset({"momentum_breakout", "trend_following"})


@dataclass
class RefinedEntry:
    """Entry price refinement result for a single signal."""
    ticker: str
    original_entry: float
    refined_entry: float
    order_type: str          # "market" or "limit"
    limit_price: Optional[float] = None
    reason: str = ""
    atr: float = 0.0


def refine_entry_prices(
    signals: List[dict],
    intraday_data: Dict[str, pd.DataFrame],
    config: dict,
) -> List[RefinedEntry]:
    """Refine entry prices for a list of signals using intraday data.

    Returns one RefinedEntry per signal, with either:
    - order_type="market"  → use market-on-open (no price refinement)
    - order_type="limit"   → limit order at refined_entry / limit_price

    Args:
        signals:       List of signal dicts (or objects with .ticker, .strategy,
                       .entry_price, .features attributes).
        intraday_data: Dict of ticker → 15-min OHLCV DataFrame.
        config:        Atlas config dict (unused for now, reserved for tuning).

    Returns:
        List of RefinedEntry, same length and order as *signals*.
    """
    results: List[RefinedEntry] = []

    for signal in signals:
        # Support both dict signals and object signals (Signal dataclass)
        if isinstance(signal, dict):
            ticker   = signal.get("ticker", "")
            strategy = signal.get("strategy", "")
            entry    = float(signal.get("entry_price", 0.0))
            features = signal.get("features", {}) or {}
        else:
            ticker   = getattr(signal, "ticker", "")
            strategy = getattr(signal, "strategy", "")
            entry    = float(getattr(signal, "entry_price", 0.0))
            features = getattr(signal, "features", {}) or {}

        atr = float(features.get("atr_14", 0.0)) if isinstance(features, dict) else 0.0

        bars = intraday_data.get(ticker)
        if bars is None or bars.empty or atr <= 0:
            results.append(RefinedEntry(
                ticker=ticker,
                original_entry=entry,
                refined_entry=entry,
                order_type="market",
                reason="no intraday data",
                atr=atr,
            ))
            continue

        opening = get_opening_range(bars, minutes=30)

        if strategy in _MR_STRATEGIES:
            results.append(_refine_mr(ticker, entry, atr, opening))

        elif strategy in _MOMENTUM_STRATEGIES:
            results.append(_refine_momentum(ticker, entry, atr, opening))

        else:
            results.append(RefinedEntry(
                ticker=ticker,
                original_entry=entry,
                refined_entry=entry,
                order_type="market",
                reason="default: market-on-open",
                atr=atr,
            ))

    return results


def get_opening_range(bars: pd.DataFrame, minutes: int = 30) -> Dict[str, float]:
    """Compute opening range from intraday bars.

    Selects bars within the first *minutes* of market open (9:30 ET).

    Args:
        bars:    DataFrame with columns open/high/low and a DatetimeIndex.
                 Should be tz-aware (America/New_York) for correct slicing.
        minutes: Opening range window in minutes (default 30).

    Returns:
        {"open": float, "high_30m": float, "low_30m": float, "range": float}
        All values are 0.0 if bars is empty.
    """
    if bars is None or bars.empty:
        return {"open": 0.0, "high_30m": 0.0, "low_30m": 0.0, "range": 0.0}

    # Market open = 9:30 ET → 570 minutes from midnight
    _MARKET_OPEN_MIN = 9 * 60 + 30  # 570
    cutoff_min = _MARKET_OPEN_MIN + minutes  # e.g. 600 for 30-min range

    if hasattr(bars.index, "hour"):
        bar_minutes = bars.index.hour * 60 + bars.index.minute
        market_bars = bars[
            (bar_minutes >= _MARKET_OPEN_MIN) & (bar_minutes < cutoff_min)
        ]
    else:
        # Fallback: approximate by head (15-min bars: 30 min ≈ 2 bars)
        n_bars = max(1, minutes // 15)
        market_bars = bars.head(n_bars)

    if market_bars.empty:
        market_bars = bars.head(2)

    if market_bars.empty:
        return {"open": 0.0, "high_30m": 0.0, "low_30m": 0.0, "range": 0.0}

    h = float(market_bars["high"].max())
    l = float(market_bars["low"].min())
    return {
        "open":    float(market_bars.iloc[0]["open"]),
        "high_30m": h,
        "low_30m":  l,
        "range":    round(h - l, 6),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Strategy-specific refinement helpers
# ─────────────────────────────────────────────────────────────────────────────

def _refine_mr(
    ticker: str,
    entry: float,
    atr: float,
    opening: Dict[str, float],
) -> RefinedEntry:
    """Mean-reversion refinement: limit 0.25 ATR below opening low.

    The limit is floored at (entry − 1 ATR) to avoid extreme discounts.
    """
    limit = opening["low_30m"] - 0.25 * atr
    # Guard: don't go more than 1 ATR below the signal entry
    limit = max(limit, entry - atr)
    limit = round(limit, 4)
    return RefinedEntry(
        ticker=ticker,
        original_entry=entry,
        refined_entry=limit,
        order_type="limit",
        limit_price=limit,
        reason=(
            f"MR dip limit: open_low={opening['low_30m']:.2f},"
            f" limit={limit:.2f}"
        ),
        atr=atr,
    )


def _refine_momentum(
    ticker: str,
    entry: float,
    atr: float,
    opening: Dict[str, float],
) -> RefinedEntry:
    """Momentum refinement: market if breakout confirmed, limit at high otherwise."""
    high_30m = opening["high_30m"]
    if high_30m > entry:
        # Breakout already above signal entry → fill at market
        return RefinedEntry(
            ticker=ticker,
            original_entry=entry,
            refined_entry=entry,
            order_type="market",
            reason="breakout confirmed",
            atr=atr,
        )
    else:
        # Breakout not yet happened → limit buy at the opening range high
        limit = round(high_30m, 4)
        return RefinedEntry(
            ticker=ticker,
            original_entry=entry,
            refined_entry=limit,
            order_type="limit",
            limit_price=limit,
            reason=f"breakout not confirmed, limit at {limit:.2f}",
            atr=atr,
        )
