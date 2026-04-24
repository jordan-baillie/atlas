#!/usr/bin/env python3
"""Regenerate data/stops_held_state.json from current broker truth.

Iterates each live-enabled market's state file, queries Alpaca for open
orders in "held" status, and builds a fresh state containing only
legitimate atlas_stop entries scoped to their correct market.

Cross-market namespace drift (e.g. XLK::commodity_etfs from the P0-3
scoping bug) is eliminated by construction — each key is derived from
the market being queried, not from cached state.

Usage:
    python3 scripts/regen_stops_held_state.py
"""
from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

from brokers.registry import get_live_broker  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)

STATE_FILE = PROJECT / "data" / "stops_held_state.json"
_MARKETS = ("sp500", "commodity_etfs", "sector_etfs", "asx")


def main() -> None:
    new_state: dict = {}

    for market in _MARKETS:
        cfg_path = PROJECT / "config" / "active" / f"{market}.json"
        if not cfg_path.exists():
            logger.debug("No config for %s — skipping", market)
            continue

        with open(cfg_path) as f:
            cfg = json.load(f)

        if not cfg.get("trading", {}).get("live_enabled"):
            logger.info("market=%s live_enabled=false — skipping", market)
            continue

        try:
            broker = get_live_broker(cfg)
            broker.connect()
            orders = broker.get_open_orders()
            held_count = 0
            for o in orders:
                status = getattr(o, "status", "")
                if not isinstance(status, str):
                    status = str(status)
                if status.lower() != "held":
                    continue
                client_oid = getattr(o, "client_order_id", "") or ""
                if "atlas_stop" not in client_oid:
                    continue
                ticker = getattr(o, "ticker", "") or "?"
                key = f"{ticker}::{market}"
                new_state[key] = {
                    "first_seen": datetime.now(timezone.utc).isoformat(),
                    "order_id": getattr(o, "order_id", "") or "",
                    "retry_count": 0,
                    "last_alerted_date": "",
                    "permanently_skipped": False,
                    "skip_reason": "",
                }
                held_count += 1
                logger.info("  held stop: %s order_id=%s", key, new_state[key]["order_id"])
            logger.info("market=%s: %d held stop(s) found", market, held_count)
        except Exception as exc:
            logger.warning("market=%s: broker query failed (%s) — skipping", market, exc)

    with open(STATE_FILE, "w") as f:
        json.dump(new_state, f, indent=2)

    logger.info("Wrote %d held-stop entries to %s", len(new_state), STATE_FILE)
    print(f"Wrote {len(new_state)} held-stop entries to {STATE_FILE}")
    print(json.dumps(new_state, indent=2))


if __name__ == "__main__":
    main()
