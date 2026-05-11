#!/usr/bin/env python3
"""F-12 one-shot backfill: write CAT stop_price from broker bracket OCO order.

Idempotent — safe to re-run. Reads the live broker to find the active SELL-stop
order for CAT, validates it against the CHECK constraint (stop < entry for long),
and writes it to trades.stop_price.

Note: if the stop is above entry (profit-locking trailing stop), the CHECK
constraint will prevent writing and the script will log a warning — this is
expected and correct.
"""
from __future__ import annotations

import json
import logging
import os
import sys

sys.path.insert(0, '/root/atlas')

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)


def _load_config() -> dict:
    """Load the sp500 active config (used for broker construction)."""
    cfg_path = os.path.join('/root/atlas', 'config', 'active', 'sp500.json')
    with open(cfg_path) as f:
        return json.load(f)


def _str_lower(v: object) -> str:
    """Convert broker enum or string value to lowercase string."""
    if v is None:
        return ""
    if hasattr(v, "value"):
        return str(v.value).lower()
    return str(v).lower()


def main() -> None:
    from brokers.registry import get_live_broker
    from db.atlas_db import get_db

    config = _load_config()
    broker = get_live_broker(config)
    if broker is None:
        logger.error("get_live_broker returned None — check config trading.mode/live_enabled")
        return

    if not broker.connect():
        logger.error("Failed to connect to broker — cannot fetch orders")
        return

    try:
        orders = broker.get_open_orders()
        logger.info("Fetched %d open orders from broker", len(orders or []))
    except Exception as exc:
        logger.error("Failed to fetch orders: %s", exc)
        return
    finally:
        broker.disconnect()

    # Find a SELL stop-type order for CAT that is active (not cancelled/expired/filled)
    stop_price: float | None = None
    stop_order_id: str | None = None
    TERMINAL_STATUSES = {"canceled", "cancelled", "expired", "filled", "rejected"}

    for o in (orders or []):
        sym = (
            getattr(o, "symbol", "")
            or getattr(o, "ticker", "")
            or ""
        ).upper()
        side = _str_lower(getattr(o, "side", ""))
        raw = getattr(o, "raw", {}) or {}
        otype = _str_lower(getattr(o, "order_type", "") or raw.get("order_type", "") or "")
        status = _str_lower(getattr(o, "status", ""))
        if hasattr(status, 'split'):
            # Handle "OrderStatus.HELD" style strings
            status = status.split(".")[-1] if "." in status else status

        if sym != "CAT":
            continue
        if "sell" not in side:
            continue
        if status in TERMINAL_STATUSES:
            continue
        if "stop" not in otype:
            continue

        # Get stop_price — try attribute first, then raw dict
        sp_attr = getattr(o, "stop_price", None)
        sp_raw = raw.get("stop_price") or raw.get("stop_limit_price")
        sp_val = sp_attr if (sp_attr is not None and sp_attr != "") else sp_raw
        if sp_val is not None and sp_val != "":
            try:
                stop_price = float(sp_val)
            except (ValueError, TypeError):
                continue
            stop_order_id = (
                getattr(o, "order_id", None)
                or getattr(o, "id", None)
                or raw.get("id")
            )
            logger.info(
                "Found CAT stop order: id=%s type=%s status=%s stop_price=$%.2f",
                stop_order_id, otype, status, stop_price,
            )
            break

    if stop_price is None:
        logger.warning(
            "No active SELL-stop order for CAT found on broker. "
            "Leaving trades.stop_price unchanged."
        )
        return

    with get_db() as db:
        row = db.execute(
            "SELECT id, direction, entry_price, stop_price FROM trades "
            "WHERE ticker='CAT' AND status='open' AND superseded=0"
        ).fetchone()
        if not row:
            logger.warning("No open CAT trade found in trades table. Skipping.")
            return

        trade_id, direction, entry_price, existing_stop = row
        direction = (direction or "long").lower()
        entry_price = float(entry_price or 0.0)

        if existing_stop is not None:
            logger.info(
                "CAT trade %d already has stop_price=%.2f — no-op.",
                trade_id, float(existing_stop),
            )
            return

        # Validate CHECK constraint
        if direction == "long" and stop_price >= entry_price:
            logger.warning(
                "CAT stop_price=%.2f >= entry=%.2f: violates CHECK constraint for long "
                "trade %d. Leaving NULL. (Stop may be a profit-locking trailing stop "
                "set above entry — the trades table cannot store this; stop_order_id "
                "column tracks broker order linkage instead.)",
                stop_price, entry_price, trade_id,
            )
            return
        if direction == "short" and stop_price <= entry_price:
            logger.warning(
                "CAT stop_price=%.2f <= entry=%.2f: violates CHECK constraint for short "
                "trade %d. Leaving NULL.",
                stop_price, entry_price, trade_id,
            )
            return

        db.execute(
            "UPDATE trades SET stop_price=?, updated_at=datetime('now') WHERE id=?",
            (stop_price, trade_id),
        )
        logger.info(
            "✅ Updated trade %d: CAT stop_price -> $%.2f (entry=$%.2f, %s)",
            trade_id, stop_price, entry_price, direction,
        )


if __name__ == "__main__":
    main()
