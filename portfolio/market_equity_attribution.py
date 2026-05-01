"""Per-market virtual equity attribution.

v1: pro-rata distribution by position MV. Each market's allocated_equity =
    broker_equity * (market_position_mv / total_position_mv).
    Cash distributed proportionally; if no positions in any market, cash split equally.
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def attribute_equity_pro_rata(
    broker_equity: float,
    broker_cash: float,
    positions_by_market: dict[str, list[dict[str, Any]]],
) -> dict[str, dict[str, float]]:
    """Distribute broker equity across markets pro-rata to position MV.

    Args:
        broker_equity: Total broker equity (positions_value + cash).
        broker_cash:   Total broker cash.
        positions_by_market: {market_id: [{ticker, market_value, ...}, ...]}

    Returns:
        {market_id: {allocated_equity, position_mv, cash_attributed}}

    NOTE on ``allocated_equity`` vs ``position_mv + cash_attributed``:
    ``allocated_equity`` is computed as ``broker_equity * (mv / total_mv)``
    so that SUM(allocated_equity) == broker_equity exactly (within rounding).
    This is the value used in the **legacy** proportional-scaling fallback path
    inside ``_get_per_market_equity``.

    ``cash_attributed`` is computed as ``broker_cash * (mv / total_mv)`` and
    is used by the **live cash attribution formula** (new path).  It is NOT
    scaled to broker_equity because broker_equity may include unsettled items
    not reflected in broker_cash.  [FIX-PMEQ-AUDIT-003]
    """
    market_ids = list(positions_by_market.keys())
    market_mv: dict[str, float] = {
        m: sum(float(p.get("market_value", 0.0) or 0.0) for p in positions_by_market[m])
        for m in market_ids
    }
    total_mv = sum(market_mv.values())

    out: dict[str, dict[str, float]] = {}
    if total_mv > 0:
        # allocated_equity: pro-rata share of broker_equity (so sum == broker_equity)
        # cash_attributed:  pro-rata share of broker_cash  (for live intraday formula)
        for m in market_ids:
            mv = market_mv[m]
            weight = mv / total_mv
            allocated = broker_equity * weight          # sums to broker_equity
            cash_share = broker_cash * weight           # sums to broker_cash
            out[m] = {
                "position_mv": round(mv, 2),
                "cash_attributed": round(cash_share, 2),
                "allocated_equity": round(allocated, 2),
            }
    else:
        # No positions anywhere — split cash equally
        n = max(1, len(market_ids))
        cash_share = broker_cash / n
        for m in market_ids:
            out[m] = {
                "position_mv": 0.0,
                "cash_attributed": round(cash_share, 2),
                "allocated_equity": round(cash_share, 2),  # broker_cash/n ≈ broker_equity/n
            }
    return out
