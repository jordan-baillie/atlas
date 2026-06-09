"""live/providers.py — target-portfolio PROVIDERS for the live/shadow pipeline.

A provider is a callable ``asof_date -> {symbol: target_weight}`` registered by name; a DeployedStrategy
references it by name so the registry JSON stays declarative. Real providers wrap a frozen forge spec or the
BOREAS book and are registered here at import time (so ``live.daily`` sees them). Until a strategy is actually
productionized + deployed, its provider is a safe no-op (returns ``{}`` = no trades).
"""
from __future__ import annotations

from live.registry import register_provider


@register_provider("boreas_carry_trend")
def boreas_carry_trend(asof_date) -> dict:
    """BOREAS carry+trend two-premium micro-futures book (the planned FIRST deployment).

    STUB until: (1) the 2026-08-28 carry forward verdict PASSES, and (2) the book's daily target-weight
    computation is ported from /root/boreas into a deterministic asof_date function. Returns {} (no trades) so
    the shadow loop is a safe no-op until wired. When ported, this returns e.g.
    {"MES": w1, "MNQ": w2, "MGC": w3, ...} vol-targeted carry+trend weights on micro-futures.
    """
    return {}


def static_provider(weights: dict):
    """Constant target book — for end-to-end shadow tests / a manually pinned book."""
    def fn(asof_date) -> dict:
        return dict(weights)
    return fn
