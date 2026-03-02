"""Atlas Market Registry.

Central registry for all supported market profiles.
Markets are auto-registered on import.

Usage:
    from markets import get_market, list_markets

    asx = get_market("asx")
    sp500 = get_market("sp500")
"""

from __future__ import annotations

import logging
from typing import Dict, List

from markets.base import MarketProfile

logger = logging.getLogger("atlas.markets")

# Global registry
_registry: Dict[str, MarketProfile] = {}


class MarketRegistry:
    """Static registry of available market profiles."""

    @staticmethod
    def register(market: MarketProfile) -> None:
        """Register a market profile."""
        _registry[market.market_id] = market
        logger.debug("Registered market: %s (%s)", market.market_id, market.display_name)

    @staticmethod
    def get(market_id: str) -> MarketProfile:
        """Get a market profile by ID.

        Args:
            market_id: Market identifier (e.g., 'asx', 'sp500').

        Returns:
            MarketProfile instance.

        Raises:
            KeyError: If market_id is not registered.
        """
        market_id = market_id.lower().strip()
        if market_id not in _registry:
            available = ", ".join(sorted(_registry.keys())) or "(none)"
            raise KeyError(
                f"Unknown market '{market_id}'. Available: {available}"
            )
        return _registry[market_id]

    @staticmethod
    def list_ids() -> List[str]:
        """Return sorted list of registered market IDs."""
        return sorted(_registry.keys())

    @staticmethod
    def list_markets() -> List[MarketProfile]:
        """Return all registered market profiles."""
        return [_registry[k] for k in sorted(_registry.keys())]


# --- Convenience functions ---

def get_market(market_id: str) -> MarketProfile:
    """Get a market profile by ID. Shorthand for MarketRegistry.get()."""
    return MarketRegistry.get(market_id)


def list_markets() -> List[str]:
    """List registered market IDs. Shorthand for MarketRegistry.list_ids()."""
    return MarketRegistry.list_ids()


# --- Auto-register built-in markets on import ---

def _auto_register():
    """Register all built-in market profiles."""
    from markets.asx import ASXMarket
    from markets.hk import HKMarket
    from markets.sp500 import SP500Market

    for MarketClass in [ASXMarket, HKMarket, SP500Market]:
        instance = MarketClass()
        if instance.market_id not in _registry:
            MarketRegistry.register(instance)


_auto_register()
