"""Atlas Broker Registry.

Maps market IDs to broker implementations. Provides a factory function
to instantiate the correct broker for a given market and config.

Usage:
    from brokers.registry import get_broker

    broker = get_broker("asx", config)       # Paper or Moomoo based on config
    broker = get_broker("sp500", config)     # Paper (IBKR future)
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from brokers.base import BrokerAdapter

logger = logging.getLogger("atlas.brokers")


def get_broker(market_id: str, config: Dict[str, Any]) -> BrokerAdapter:
    """Instantiate the appropriate broker for a market.

    Broker selection logic:
        1. config["trading"]["broker"] == "paper" → PaperBrokerAdapter
        2. market_id == "asx" && broker == "moomoo" → MomooBroker
        3. Otherwise → PaperBrokerAdapter (safe default)

    Args:
        market_id: Market identifier (e.g., 'asx', 'sp500').
        config: Active configuration dict.

    Returns:
        BrokerAdapter instance ready for use.
    """
    market_id = market_id.lower().strip()
    broker_name = config.get("trading", {}).get("broker", "paper").lower()

    if broker_name == "paper":
        return _make_paper_broker(market_id, config)

    if broker_name == "moomoo" and market_id == "asx":
        return _make_moomoo_broker(config)

    logger.warning(
        "No broker '%s' available for market '%s' — falling back to paper",
        broker_name, market_id,
    )
    return _make_paper_broker(market_id, config)


def _make_paper_broker(market_id: str, config: Dict[str, Any]) -> BrokerAdapter:
    """Create a paper broker adapter."""
    from brokers.paper import PaperBrokerAdapter
    from paper_engine.engine import PaperPortfolio

    portfolio = PaperPortfolio(config=config, market_id=market_id)
    return PaperBrokerAdapter(portfolio)


def _make_moomoo_broker(config: Dict[str, Any]) -> BrokerAdapter:
    """Create a Moomoo broker for ASX trading."""
    from brokers.moomoo.broker import MoomooBroker
    return MoomooBroker(config)
