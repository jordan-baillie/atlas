"""brokers/preflight.py — Pre-flight safety validators.

Extracted from brokers/live_executor.py (decomposition #2 PR1.2).
Pure functions — no broker state, no class state.

Public surface
--------------
    PreflightError
        Exception raised when a pre-flight check fails.

    is_already_protected(broker, ticker) -> bool
        RCA #7: check if a SELL stop order already exists for the ticker.

    protective_ledger_enabled() -> bool
        Whether PROTECTIVE_LEDGER_WRITE_ENABLED env var is set.

    preflight_check_config(config) -> list[str]
        Validate trading config. Returns list of error strings (empty = OK).

    preflight_check_order(ticker, side, qty, price, safety, daily_order_count) -> list[str]
        Validate a single order against safety limits.
"""
from __future__ import annotations

import logging
import os

from brokers.base import OrderSide

logger = logging.getLogger("atlas.preflight")

def protective_ledger_enabled() -> bool:
    """Return True if position_protective_orders ledger writes are enabled.

    Controlled by env var PROTECTIVE_LEDGER_WRITE_ENABLED (default: true).
    Set to 'false', '0', or 'no' to disable all Phase B.0 ledger writes
    without touching order flow.  Allows instant rollback if issues arise.
    """
    val = os.environ.get("PROTECTIVE_LEDGER_WRITE_ENABLED", "true").lower()
    return val not in ("false", "0", "no")


def is_already_protected(broker, ticker: str) -> bool:
    """Return True if the position already has a SELL stop order live at the broker.

    RCA latent #7: Avoids double-placement when the entry order was an instant-fill
    bracket order (native OCO from order_class='bracket').  In that case, Alpaca
    already attached both stop-loss and take-profit legs atomically, so calling
    place_stops_for_plan afterwards would add a SECOND stop that races with the
    existing one.

    Args:
        broker: Connected broker instance (needs ``get_open_orders``).
        ticker: Atlas-format ticker symbol (AAPL, MSFT, ...).

    Returns:
        True  — a SELL stop/stop_limit/trailing_stop order is already open.
        False — no existing protective stop (safe to place).  Also returns False
                on any exception (conservative: let placement attempt).
    """
    try:
        open_orders = broker.get_open_orders()
    except (OSError, ConnectionError, TimeoutError, AttributeError, RuntimeError) as _exc:
        logger.debug(
            "_is_already_protected(%s): get_open_orders error (%s) — returning False (conservative)",
            ticker, _exc,
        )
        return False  # Be conservative — let placement attempt

    for o in open_orders:
        if getattr(o, "ticker", "") != ticker:
            continue
        # Side: OrderResult.side is an OrderSide enum
        side_val = getattr(o, "side", None)
        side_str = (side_val.value if hasattr(side_val, "value") else str(side_val)).lower()
        # Order type is in o.raw["order_type"] (set by _order_to_result)
        raw = getattr(o, "raw", {}) or {}
        order_type_str = raw.get("order_type", "").lower()
        if side_str == "sell" and order_type_str in ("stop", "stop_limit", "trailing_stop"):
            return True

    return False




class PreflightError(Exception):
    """Raised when a pre-flight safety check fails."""
    pass


def preflight_check_config(config: dict) -> list[str]:
    """Validate config has all required safety fields. Returns list of errors."""
    errors = []
    trading = config.get("trading", {})
    mode = trading.get("mode", "live")

    # Paper mode does not require live_enabled=True — it uses a virtual account.
    # live_enabled=False is still an error for mode="live" (safety gate for real money).
    if not trading.get("live_enabled", False) and mode != "paper":
        errors.append("trading.live_enabled is False")

    safety = trading.get("live_safety", {})
    if not safety:
        errors.append("trading.live_safety section missing")
    else:
        if safety.get("max_order_value", 0) <= 0:
            errors.append("live_safety.max_order_value must be > 0")
        if safety.get("max_daily_orders", 0) <= 0:
            errors.append("live_safety.max_daily_orders must be > 0")

    return errors


def preflight_check_order(
    ticker: str,
    side: OrderSide,
    qty: int,
    price: float,
    safety: dict,
    daily_order_count: int,
) -> list[str]:
    """Validate a single order against safety limits. Returns list of errors."""
    errors = []
    order_value = price * qty

    max_value = safety.get("max_order_value", 2000)
    if order_value > max_value:
        errors.append(
            f"Order value ${order_value:.2f} exceeds max ${max_value:.2f}"
        )

    max_daily = safety.get("max_daily_orders", 10)
    if daily_order_count >= max_daily:
        errors.append(
            f"Daily order limit ({max_daily}) reached"
        )

    if qty <= 0:
        errors.append(f"Invalid quantity: {qty}")

    if price <= 0:
        errors.append(f"Invalid price: {price}")

    return errors
