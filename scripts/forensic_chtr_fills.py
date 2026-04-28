#!/usr/bin/env python3
"""
forensic_chtr_fills.py — Pull Alpaca FILL activities for CHTR and reconcile
against ledger rows 172 and 184.

Usage:
    python3 scripts/forensic_chtr_fills.py
"""
from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ── path setup ──────────────────────────────────────────────────────────────
ATLAS_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ATLAS_ROOT))

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("forensic_chtr")


def get_chtr_fills(
    start: str = "2026-04-20",
    end: str = "2026-04-26",
    symbol: str = "CHTR",
) -> list[dict[str, Any]]:
    """Query Alpaca /v2/account/activities/FILL and filter to CHTR fills."""
    from brokers.secrets import get_secret
    from alpaca.trading.client import TradingClient

    api_key = get_secret("ALPACA_API_KEY", prompt=False)
    api_secret = get_secret("ALPACA_SECRET_KEY", prompt=False)
    if not api_key or not api_secret:
        raise RuntimeError("Alpaca credentials not found")

    client = TradingClient(api_key=api_key, secret_key=api_secret, paper=False)

    # Use raw GET to hit /v2/account/activities/FILL with date filters
    params = {
        "after": f"{start}T00:00:00Z",
        "until": f"{end}T23:59:59Z",
        "direction": "asc",
        "page_size": 100,
    }
    raw = client.get("/account/activities/FILL", data=params)

    # raw is a list of activity dicts
    if not isinstance(raw, list):
        raw = [raw]

    chtr_fills: list[dict[str, Any]] = []
    for act in raw:
        sym = act.get("symbol", "") if isinstance(act, dict) else getattr(act, "symbol", "")
        if isinstance(act, dict):
            act_sym = act.get("symbol", "")
        else:
            act_sym = getattr(act, "symbol", "")
        if act_sym == symbol:
            if isinstance(act, dict):
                chtr_fills.append(act)
            else:
                # Pydantic model — convert to dict
                chtr_fills.append(act.dict() if hasattr(act, "dict") else vars(act))

    return chtr_fills


def classify_fills(fills: list[dict[str, Any]]) -> dict[str, Any]:
    """Split fills into BUY (entries) and SELL (exits), return summary."""
    buys = []
    sells = []
    for f in fills:
        side = str(f.get("side", "")).lower()
        qty = float(f.get("qty", 0) or 0)
        price = float(f.get("price", 0) or 0)
        txn_time = f.get("transaction_time") or f.get("transactionTime") or ""
        order_id = f.get("order_id") or f.get("orderId") or ""
        record = {
            "side": side,
            "qty": qty,
            "price": price,
            "transaction_time": str(txn_time),
            "order_id": str(order_id),
            "raw": f,
        }
        if side == "buy":
            buys.append(record)
        elif side == "sell":
            sells.append(record)

    total_bought_qty = sum(r["qty"] for r in buys)
    total_sold_qty = sum(r["qty"] for r in sells)

    return {
        "buys": buys,
        "sells": sells,
        "total_bought_qty": total_bought_qty,
        "total_sold_qty": total_sold_qty,
        "round_trips": min(int(total_bought_qty), int(total_sold_qty)),
    }


def compute_pnl(entry_price: float, exit_price: float, qty: float) -> tuple[float, float]:
    """Return (pnl, pnl_pct) for a long trade."""
    pnl = round((exit_price - entry_price) * qty, 4)
    pnl_pct = round((exit_price - entry_price) / entry_price * 100, 12) if entry_price else 0.0
    return pnl, pnl_pct


def main() -> None:
    print("=" * 70)
    print("CHTR ALPACA FILL FORENSIC — Phase 1B RCA")
    print("=" * 70)

    print("\n[1] Pulling FILL activities from Alpaca (2026-04-20 → 2026-04-26)…")
    fills = get_chtr_fills()
    print(f"    Found {len(fills)} CHTR fill activities")

    if not fills:
        print("    ⚠  No fills found — check date range or symbol")
        return

    print("\n[2] All CHTR fill events:")
    print(f"  {'side':5s} {'qty':>6s} {'price':>10s} {'transaction_time':30s} {'order_id'}")
    print("  " + "-" * 80)
    for f in fills:
        side = str(f.get("side", "")).lower()
        qty = f.get("qty", "?")
        price = f.get("price", "?")
        txn = f.get("transaction_time") or f.get("transactionTime") or "?"
        oid = f.get("order_id") or f.get("orderId") or "?"
        print(f"  {side:5s} {str(qty):>6s} {str(price):>10s} {str(txn):30s} {oid}")

    print("\n[3] Classification:")
    summary = classify_fills(fills)
    print(f"    BUY  fills: {len(summary['buys'])} (total qty={summary['total_bought_qty']})")
    for b in summary["buys"]:
        print(f"      → price={b['price']:.4f}  qty={b['qty']}  time={b['transaction_time']}")
    print(f"    SELL fills: {len(summary['sells'])} (total qty={summary['total_sold_qty']})")
    for s in summary["sells"]:
        print(f"      → price={s['price']:.4f}  qty={s['qty']}  time={s['transaction_time']}")
    print(f"    Round-trips inferred: {summary['round_trips']}")

    print("\n[4] Ledger comparison:")
    print("    Row 172: entry=243.9300 exit=241.8368 qty=1 pnl=-2.0932 strategy=momentum_breakout")
    print("    Row 184: entry=243.9300 exit=241.8368 qty=1 pnl=-2.0932 strategy=reconciled")

    # Compute what the pnl should be per Alpaca actuals
    buys = summary["buys"]
    sells = summary["sells"]

    if summary["round_trips"] == 1 and len(buys) >= 1 and len(sells) >= 1:
        actual_entry = buys[0]["price"]
        actual_exit = sells[0]["price"]
        actual_pnl, actual_pnl_pct = compute_pnl(actual_entry, actual_exit, buys[0]["qty"])
        print(f"\n    Actual entry (Alpaca): {actual_entry:.4f}")
        print(f"    Actual exit  (Alpaca): {actual_exit:.4f}")
        print(f"    Actual pnl   (Alpaca): {actual_pnl:.4f}")
        print(f"\n    Ledger entry:          243.9300")
        print(f"    Ledger exit:           241.8368")
        print(f"    Ledger pnl:            -2.0932")
        delta_pnl = round(actual_pnl - (-2.0932), 4)
        print(f"\n    PnL delta (actual - ledger): {delta_pnl:+.4f}")
        print(f"\n    VERDICT: ONE round-trip → Case A (duplicate row 184 should be removed)")
    elif summary["round_trips"] == 2:
        print(f"\n    Two complete round-trips found → Case B (both rows legitimate)")
        for i, (b, s) in enumerate(zip(buys[:2], sells[:2])):
            pnl, pnl_pct = compute_pnl(b["price"], s["price"], b["qty"])
            print(f"    Trip {i+1}: entry={b['price']:.4f} exit={s['price']:.4f} pnl={pnl:.4f}")
    else:
        print(f"\n    Inconclusive — {len(buys)} buys, {len(sells)} sells")

    print("\n[5] Raw fill JSON (for migration script):")
    print(json.dumps(fills, indent=2, default=str))


if __name__ == "__main__":
    main()
