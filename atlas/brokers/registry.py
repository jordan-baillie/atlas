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
    from atlas.brokers.registry import get_broker, get_live_broker

    broker = get_broker("sp500", config)        # Live broker (or None)
    live   = get_live_broker(config)             # Live broker (or None)
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from atlas.brokers.base import BrokerAdapter

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
        from atlas.brokers.alpaca.broker import AlpacaBroker  # noqa: F401
        _BROKER_FACTORIES["alpaca"] = _make_alpaca_broker
    except Exception as e:
        logger.debug(f"alpaca broker not available (install: pip install alpaca-py): {e}")
    try:
        from atlas.brokers.ib.broker import IBBroker  # noqa: F401
        _BROKER_FACTORIES["ib"] = _make_ib_broker
    except Exception as e:
        logger.debug(f"ib broker not available (install: pip install ib_insync): {e}")
    try:
        from atlas.brokers.ib_web.broker import IBWebBroker  # noqa: F401
        _BROKER_FACTORIES["ib_web"] = _make_ib_web_broker
    except Exception as e:
        logger.debug(f"ib_web broker not available: {e}")


def available_brokers() -> list[str]:
    """Return names of all brokers whose dependencies are installed."""
    _register_defaults()
    return list(_BROKER_FACTORIES.keys())


# ═══════════════════════════════════════════════════════════════
# Public API
# ═══════════════════════════════════════════════════════════════

_KNOWN_BROKERS = ("alpaca", "ib", "ib_web")


def get_broker(market_id: str, config: Dict[str, Any]) -> Optional[BrokerAdapter]:
    """Instantiate the configured live broker for a market.

    Returns the live broker if configured and available, or None
    if live trading is not configured.

    A broker is created when either:
      - trading.live_enabled = true  (full trading mode)
      - trading.monitoring_enabled = true  (read-only: positions + account info,
        no order execution — read-only monitoring mode)
    """
    _register_defaults()
    market_id = market_id.lower().strip()
    broker_name = _resolve_broker_name(config)
    live_enabled = config.get("trading", {}).get("live_enabled", False)
    monitoring_enabled = config.get("trading", {}).get("monitoring_enabled", False)

    if not (live_enabled or monitoring_enabled) or broker_name not in _KNOWN_BROKERS:
        logger.debug(
            "Broker not configured (broker=%s, live_enabled=%s, monitoring_enabled=%s)",
            broker_name, live_enabled, monitoring_enabled,
        )
        return None

    factory = _BROKER_FACTORIES.get(broker_name)
    if factory:
        # monitoring_enabled connects to the REAL account for position reading
        # even though live_enabled=False (no order execution).  Pass live=True
        # so the broker selects TrdEnv.REAL, but calling code (LivePortfolio)
        # will not place any orders since live_enabled is False.
        connect_as_live = live_enabled or monitoring_enabled
        return factory(market_id, config, live=connect_as_live)

    logger.warning(
        "Broker '%s' not available (installed: %s)",
        broker_name, list(_BROKER_FACTORIES.keys()),
    )
    return None


def get_live_broker(config: Dict[str, Any]) -> Optional[BrokerAdapter]:
    """Create a live broker instance if configured and available.

    Supports three modes (trading.mode in config):
      "live"  — real-money trading, requires live_enabled=True
      "paper" — Alpaca paper account, does NOT require live_enabled=True
      "passive" — monitoring only, no orders

    Returns None if neither live_enabled nor mode=="paper". The broker is NOT
    connected — call broker.connect() after pre-flight checks.
    """
    _register_defaults()
    live_enabled = config.get("trading", {}).get("live_enabled", False)
    mode = _resolve_mode(config)
    broker_name = _resolve_broker_name(config)

    # Paper mode works even without live_enabled (it targets a simulated account)
    if not (live_enabled or mode == "paper") or broker_name not in _KNOWN_BROKERS:
        logger.debug(
            "Live broker not available (broker=%s, live_enabled=%s, mode=%s)",
            broker_name, live_enabled, mode,
        )
        return None

    factory = _BROKER_FACTORIES.get(broker_name)
    if not factory:
        logger.warning("Broker '%s' not registered or unavailable", broker_name)
        return None

    market_id = config.get("market", "sp500")
    return factory(market_id, config, live=True)


# ═══════════════════════════════════════════════════════════════
# Internal helpers
# ═══════════════════════════════════════════════════════════════

def _resolve_broker_name(config: Dict[str, Any]) -> str:
    """Extract and normalise broker name from config."""
    return config.get("trading", {}).get("broker", "alpaca").lower().strip()


def _resolve_mode(config: Dict[str, Any]) -> str:
    """Extract and normalise trading mode from config.

    Valid values: "live" (default), "paper", "passive".
    Unknown values are coerced to "live" for safety.
    """
    mode = config.get("trading", {}).get("mode", "live").lower().strip()
    if mode not in ("live", "paper", "passive"):
        logger.warning("Unknown trading mode '%s' — defaulting to 'live'", mode)
        return "live"
    return mode


def _make_alpaca_broker(
    market_id: str, config: Dict[str, Any], live: bool = False, **kwargs,
) -> BrokerAdapter:
    from atlas.brokers.alpaca.broker import AlpacaBroker
    mode = _resolve_mode(config)
    return AlpacaBroker(config, live=live, mode=mode)


def _make_ib_broker(
    market_id: str, config: Dict[str, Any], live: bool = False, **kwargs,
) -> BrokerAdapter:
    from atlas.brokers.ib.broker import IBBroker
    return IBBroker(config)


def _make_ib_web_broker(
    market_id: str, config: Dict[str, Any], live: bool = False, **kwargs,
) -> BrokerAdapter:
    from atlas.brokers.ib_web.broker import IBWebBroker
    return IBWebBroker(config)
