"""Atlas Broker Registry.

Maps market IDs to broker implementations. Provides factory functions
to instantiate the correct broker for a given market and config.

Broker selection:
    1. config["trading"]["broker"] == "paper" → PaperBroker (always safe)
    2. config["trading"]["broker"] == "moomoo" && live_enabled=True → MomooBroker(live=True)
    3. config["trading"]["broker"] == "moomoo" && live_enabled=False → MomooBroker(live=False) or paper
    4. Otherwise → PaperBroker (safe default)

Live trading is gated behind TWO flags:
    - trading.broker = "moomoo"
    - trading.live_enabled = True (default: False)

Usage:
    from brokers.registry import get_broker, get_live_executor

    broker = get_broker("asx", config)          # Paper broker (default)
    executor = get_live_executor(config)         # LiveExecutor (None if disabled)
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from brokers.base import BrokerAdapter

logger = logging.getLogger("atlas.brokers")


def get_broker(market_id: str, config: Dict[str, Any]) -> BrokerAdapter:
    """Instantiate the appropriate broker for a market.

    This is the standard broker used by paper trading, signal generation,
    and EOD settlement. It is ALWAYS the paper broker unless explicitly
    configured otherwise AND live_enabled is True.

    Args:
        market_id: Market identifier (e.g., 'asx', 'sp500').
        config: Active configuration dict.

    Returns:
        BrokerAdapter instance.
    """
    market_id = market_id.lower().strip()
    broker_name = config.get("trading", {}).get("broker", "paper").lower()
    live_enabled = config.get("trading", {}).get("live_enabled", False)

    if broker_name == "paper" or not live_enabled:
        return _make_paper_broker(market_id, config)

    if broker_name == "moomoo" and market_id in ("asx", "sp500"):
        return _make_moomoo_broker(config, live=live_enabled)

    logger.warning(
        "No broker '%s' available for market '%s' — falling back to paper",
        broker_name, market_id,
    )
    return _make_paper_broker(market_id, config)


def get_live_executor(config: Dict[str, Any]) -> Optional["LiveExecutor"]:
    """Create a LiveExecutor if live trading is configured.

    Returns None if live trading is not enabled — callers should check
    before using.

    The executor is NOT connected on return. Call executor.connect()
    explicitly after any pre-flight checks.
    """
    live_enabled = config.get("trading", {}).get("live_enabled", False)
    broker_name = config.get("trading", {}).get("broker", "paper").lower()

    if not live_enabled or broker_name != "moomoo":
        logger.debug(
            "Live executor not available (broker=%s, live_enabled=%s)",
            broker_name, live_enabled,
        )
        return None

    from brokers.live_executor import LiveExecutor
    return LiveExecutor(config)


def _make_paper_broker(market_id: str, config: Dict[str, Any]) -> BrokerAdapter:
    """Create a paper broker adapter."""
    from brokers.paper import PaperBroker
    return PaperBroker(config)


def _make_moomoo_broker(
    config: Dict[str, Any], live: bool = False,
) -> BrokerAdapter:
    """Create a Moomoo broker for ASX trading."""
    from brokers.moomoo.broker import MomooBroker
    return MomooBroker(config, live=live)
