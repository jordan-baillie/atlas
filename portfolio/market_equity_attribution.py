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
        Sum of allocated_equity across markets equals broker_equity within rounding tolerance.
    """
    market_ids = list(positions_by_market.keys())
    market_mv: dict[str, float] = {
        m: sum(float(p.get("market_value", 0.0) or 0.0) for p in positions_by_market[m])
        for m in market_ids
    }
    total_mv = sum(market_mv.values())

    out: dict[str, dict[str, float]] = {}
    if total_mv > 0:
        # Cash split pro-rata to MV
        for m in market_ids:
            mv = market_mv[m]
            cash_share = broker_cash * (mv / total_mv)
            out[m] = {
                "position_mv": round(mv, 2),
                "cash_attributed": round(cash_share, 2),
                "allocated_equity": round(mv + cash_share, 2),
            }
    else:
        # No positions anywhere — split cash equally
        n = max(1, len(market_ids))
        cash_share = broker_cash / n
        for m in market_ids:
            out[m] = {
                "position_mv": 0.0,
                "cash_attributed": round(cash_share, 2),
                "allocated_equity": round(cash_share, 2),
            }
    return out
