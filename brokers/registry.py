"""Atlas Broker Registry.

Single source of truth for broker instantiation. All code that needs
a broker MUST use this module — never import broker classes directly.

Supported brokers:
    alpaca — Alpaca Markets REST API (SP500, commission-free)

Broker selection is driven by config:
    trading.broker      = "alpaca"
    trading.live_enabled = true | false

Live trading requires live_enabled == true and a valid broker configured.
The live broker is the sole source of truth — no paper fallback.

Usage:
    from brokers.registry import get_broker, get_live_broker

    broker = get_broker("sp500", config)        # Live broker (or None)
    live   = get_live_broker(config)             # Live broker (or None)
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from brokers.base import BrokerAdapter

logger = logging.getLogger("atlas.brokers")

# ═══════════════════════════════════════════════════════════════
# Broker catalogue — add new brokers here
# ═══════════════════════════════════════════════════════════════

_BROKER_FACTORIES = {}  # populated lazily to avoid import cycles


def _register_defaults():
    """Register built-in broker factories (lazy, called once)."""
    if _BROKER_FACTORIES:
        return

    try:
        from brokers.alpaca.broker import AlpacaBroker  # noqa: F401
        _BROKER_FACTORIES["alpaca"] = _make_alpaca_broker
    except Exception:
        logger.debug("alpaca broker not available (install: pip install alpaca-py)")


def available_brokers() -> list[str]:
    """Return names of all brokers whose dependencies are installed."""
    _register_defaults()
    return list(_BROKER_FACTORIES.keys())


# ═══════════════════════════════════════════════════════════════
# Public API
# ═══════════════════════════════════════════════════════════════

_KNOWN_BROKERS = ("alpaca",)


def get_broker(market_id: str, config: Dict[str, Any]) -> Optional[BrokerAdapter]:
    """Instantiate the configured live broker for a market.

    Returns the live broker if configured and available, or None
    if live trading is not configured.
    """
    _register_defaults()
    market_id = market_id.lower().strip()
    broker_name = _resolve_broker_name(config)
    live_enabled = config.get("trading", {}).get("live_enabled", False)

    if not live_enabled or broker_name not in _KNOWN_BROKERS:
        logger.debug(
            "Broker not configured for live trading (broker=%s, live_enabled=%s)",
            broker_name, live_enabled,
        )
        return None

    factory = _BROKER_FACTORIES.get(broker_name)
    if factory:
        return factory(market_id, config, live=live_enabled)

    logger.warning(
        "Broker '%s' not available (installed: %s)",
        broker_name, list(_BROKER_FACTORIES.keys()),
    )
    return None


def get_live_broker(config: Dict[str, Any]) -> Optional[BrokerAdapter]:
    """Create a live broker instance if configured and available.

    Returns None if live trading is not enabled. The broker is NOT
    connected — call broker.connect() after pre-flight checks.
    """
    _register_defaults()
    live_enabled = config.get("trading", {}).get("live_enabled", False)
    broker_name = _resolve_broker_name(config)

    if not live_enabled or broker_name not in _KNOWN_BROKERS:
        logger.debug(
            "Live broker not available (broker=%s, live_enabled=%s)",
            broker_name, live_enabled,
        )
        return None

    factory = _BROKER_FACTORIES.get(broker_name)
    if not factory:
        logger.warning("Broker '%s' not registered or unavailable", broker_name)
        return None

    market_id = config.get("market", "sp500")
    return factory(market_id, config, live=True)


def get_live_executor(config: Dict[str, Any]) -> Optional["LiveExecutor"]:
    """Create a LiveExecutor if live trading is configured.

    Returns None if live trading is not enabled. The executor is NOT
    connected — call executor.connect() after pre-flight checks.

    Backward-compatible wrapper — new code should use get_live_broker().
    """
    live_enabled = config.get("trading", {}).get("live_enabled", False)
    broker_name = _resolve_broker_name(config)

    if not live_enabled or broker_name not in _KNOWN_BROKERS:
        logger.debug(
            "Live executor not available (broker=%s, live_enabled=%s)",
            broker_name, live_enabled,
        )
        return None

    from brokers.live_executor import LiveExecutor
    return LiveExecutor(config)


# ═══════════════════════════════════════════════════════════════
# Internal helpers
# ═══════════════════════════════════════════════════════════════

def _resolve_broker_name(config: Dict[str, Any]) -> str:
    """Extract and normalise broker name from config."""
    return config.get("trading", {}).get("broker", "alpaca").lower().strip()


def _make_alpaca_broker(
    market_id: str, config: Dict[str, Any], live: bool = False, **kwargs,
) -> BrokerAdapter:
    from brokers.alpaca.broker import AlpacaBroker
    return AlpacaBroker(config, live=live)
