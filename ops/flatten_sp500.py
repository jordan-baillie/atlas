#!/usr/bin/env python3
"""Idempotent flatten of the RETIRED SP500 Alpaca **paper** account.

Context: the old long-only entry+stop swing system was retired (2026-06-09). Its paper account held
4 positions (DOW/SCHW/UNG/XLE) plus stale OCO bracket exit-legs. Those OCO cancels + liquidations only
process when the US market is open, so this runs as a market-open timer. It is fully idempotent and
self-disables its own timer once the account is confirmed flat (0 positions, 0 open orders).

SAFETY: refuses to run unless the broker resolves to PAPER. The new system does NOT trade this account
(it deploys via live/daily.py on its own brokers), so keeping it flat is harmless.
"""
from __future__ import annotations

import json
import logging
import subprocess
import time
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("flatten_sp500")
ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    from atlas.brokers.registry import get_live_broker
    cfg = json.loads((ROOT / "config" / "active" / "sp500.json").read_text())
    br = get_live_broker(cfg)
    if br is None or br.is_live:
        log.error("REFUSING: broker is not paper (is_live=%s)", getattr(br, "is_live", "?"))
        return 2
    br.connect()

    # cancel all orders (releases shares held by OCO legs once the market is open)
    br.cancel_all_orders()
    time.sleep(5)

    positions = br.get_positions()
    for p in positions:
        try:
            br._trade_client.close_position(p.ticker)
            log.info("liquidate %s x%s submitted", p.ticker, p.shares)
        except Exception as e:
            log.warning("liquidate %s failed (will retry next session): %s", p.ticker, str(e)[:120])

    time.sleep(5)
    remaining = br.get_positions()
    open_orders = br.get_open_orders()
    log.info("after flatten: %d positions, %d open orders", len(remaining), len(open_orders))

    # self-disable once truly flat (no positions, no lingering orders)
    if not remaining and not open_orders:
        log.info("SP500 paper account is FLAT — disabling the flatten timer.")
        subprocess.run(["systemctl", "disable", "--now", "atlas-sp500-flatten.timer"], check=False)
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
