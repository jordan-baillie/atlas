"""Task #277 — Migrate 4 existing positions from independent stop+TP orders
to native Alpaca OCO (One-Cancels-Other) bracket orders.

Usage:
    python3 scripts/migrate_to_oco.py --reconnaissance
    python3 scripts/migrate_to_oco.py --ticker GLD --execute

Guardrails:
  - Only operates on the 4 known live positions: GLD, XLI, XLY, CAT
  - Requires --execute flag for any mutation
  - Uses broker._wait_for_cancel_confirmed (10s timeout) per Phase 2C
  - STOPS on first error — never proceeds blindly
  - Prints state BEFORE and AFTER every broker call
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

from utils.logging_config import setup_logging  # noqa: E402

setup_logging()
logger = logging.getLogger("atlas.migrate_oco")

# ── Constants ──────────────────────────────────────────────────────────────
ALLOWED_TICKERS: set[str] = {"GLD", "XLI", "XLY", "CAT"}

# Ticker → market_id mapping (used to load the right broker config)
TICKER_MARKET: dict[str, str] = {
    "GLD": "commodity_etfs",
    "XLI": "sector_etfs",
    "XLY": "sector_etfs",
    "CAT": "sp500",
}

# Recommended migration order (GLD→XLI→XLY→CAT)
MIGRATION_ORDER: list[str] = ["GLD", "XLI", "XLY", "CAT"]


# ── Config loading ──────────────────────────────────────────────────────────

def load_config(market_id: str) -> dict:
    path = PROJECT / "config" / "active" / f"{market_id}.json"
    if not path.exists():
        raise FileNotFoundError(f"Config not found: {path}")
    with open(path) as f:
        return json.load(f)


# ── Broker connection ───────────────────────────────────────────────────────

def connect_broker(market_id: str):
    """Load config and return a connected live broker for the given market."""
    config = load_config(market_id)
    from brokers.registry import get_live_broker
    broker = get_live_broker(config)
    if broker is None:
        raise RuntimeError(
            f"get_live_broker returned None for {market_id} "
            f"(live_enabled={config.get('trading',{}).get('live_enabled')})"
        )
    if not broker.connect():
        raise RuntimeError(f"Broker connect() failed for {market_id}")
    return broker


# ── Order inspection helpers ────────────────────────────────────────────────

def get_orders_for_ticker(broker, ticker: str) -> list:
    """Return open orders whose symbol matches ticker (Alpaca or Atlas format)."""
    all_orders = broker.get_open_orders()
    return [
        o for o in all_orders
        if (
            o.raw.get("symbol", "").upper() == ticker.upper()
            or getattr(o, "ticker", "").upper() == ticker.upper()
        )
    ]


def classify_order(o) -> str:
    """Return human-readable classification: stop|trailing_stop|limit|bracket|oco|unknown."""
    otype = (o.raw.get("order_type") or "").lower()
    oclass = (o.raw.get("order_class") or "").lower()
    if oclass in ("bracket", "oco"):
        return oclass
    return otype or "unknown"


def get_position_qty(broker, ticker: str) -> int | None:
    """Return position qty for ticker, or None if not held."""
    positions = broker.get_positions()
    for pos in positions:
        if pos.ticker.upper() == ticker.upper():
            return abs(int(pos.shares))
    return None


# ── Reconnaissance ──────────────────────────────────────────────────────────

def run_reconnaissance(verbose: bool = True) -> dict:
    """Read-only dump of current state for all 4 tickers.

    Returns a dict keyed by ticker with:
        - position_qty, entry_price, market_value
        - orders list (each with type, side, qty, stop, limit, id)
        - warnings list
        - needs_migration: bool
    """
    print("=" * 68)
    print("RECONNAISSANCE — current state (read-only)")
    print("=" * 68)

    # Group tickers by market to minimise broker connections
    from itertools import groupby
    results: dict[str, dict] = {}

    markets_seen: dict[str, object] = {}  # market_id → broker

    for ticker in MIGRATION_ORDER:
        market_id = TICKER_MARKET[ticker]
        if market_id not in markets_seen:
            try:
                markets_seen[market_id] = connect_broker(market_id)
            except Exception as e:
                print(f"\n[ERROR] Cannot connect to broker for {market_id}: {e}")
                for t in [tk for tk, m in TICKER_MARKET.items() if m == market_id]:
                    results[t] = {"error": str(e)}
                continue

        broker = markets_seen[market_id]

        # Position
        positions = broker.get_positions()
        pos = next((p for p in positions if p.ticker.upper() == ticker.upper()), None)

        # Orders
        orders = get_orders_for_ticker(broker, ticker)

        info: dict = {
            "ticker": ticker,
            "market": market_id,
            "position": None,
            "orders": [],
            "warnings": [],
            "needs_migration": False,
        }

        if pos is None:
            info["warnings"].append("NO POSITION FOUND — may already be closed")
        else:
            info["position"] = {
                "qty": abs(int(pos.shares)),
                "entry_price": pos.entry_price,
                "market_value": pos.market_value,
            }

        stop_orders = []
        tp_orders = []
        trailing_orders = []
        bracket_oco_orders = []

        for o in orders:
            otype = (o.raw.get("order_type") or "").lower()
            oclass = (o.raw.get("order_class") or "").lower()
            side = (o.raw.get("side") or "").lower()
            qty = o.raw.get("qty") or ""
            stop = o.raw.get("stop_price") or ""
            limit = o.raw.get("limit_price") or ""
            oid = (o.raw.get("id") or "")[:8]

            order_summary = {
                "type": otype,
                "class": oclass,
                "side": side,
                "qty": qty,
                "stop_price": stop,
                "limit_price": limit,
                "id": o.raw.get("id") or "",
                "id_short": oid,
            }
            info["orders"].append(order_summary)

            if oclass in ("bracket", "oco"):
                bracket_oco_orders.append(o)
            elif otype == "trailing_stop":
                trailing_orders.append(o)
            elif otype in ("stop", "stop_limit") and side == "sell":
                stop_orders.append(o)
            elif otype == "limit" and side == "sell":
                tp_orders.append(o)

        # Warnings + needs_migration logic
        if trailing_orders:
            info["warnings"].append(
                "HAS TRAILING STOP — will SKIP (trailing stops are dynamic, "
                "not suitable for static OCO migration)"
            )
        if bracket_oco_orders:
            info["warnings"].append(
                "ALREADY HAS bracket/oco ORDER — will SKIP (already migrated)"
            )
        if pos and stop_orders and tp_orders and not bracket_oco_orders and not trailing_orders:
            # Check qty coverage
            pos_qty = abs(int(pos.shares))
            stop_qty = int(float(stop_orders[0].raw.get("qty") or 0))
            tp_qty = int(float(tp_orders[0].raw.get("qty") or 0))
            if stop_qty != pos_qty or tp_qty != pos_qty:
                info["warnings"].append(
                    f"QTY MISMATCH — position qty={pos_qty} but "
                    f"stop qty={stop_qty} tp qty={tp_qty} — NEEDS MANUAL INVESTIGATION"
                )
            else:
                info["needs_migration"] = True

        if len(stop_orders) > 1:
            info["warnings"].append(f"MULTIPLE STOP ORDERS ({len(stop_orders)}) — unexpected, verify manually")
        if len(tp_orders) > 1:
            info["warnings"].append(f"MULTIPLE TP ORDERS ({len(tp_orders)}) — unexpected, verify manually")

        results[ticker] = info

    # ── Print ──────────────────────────────────────────────────────────────
    print()
    all_ok = True
    for ticker in MIGRATION_ORDER:
        info = results.get(ticker, {})
        if "error" in info:
            print(f"{ticker}: ERROR — {info['error']}")
            all_ok = False
            continue

        pos = info.get("position")
        if pos:
            print(
                f"{ticker}: position qty={pos['qty']} entry=${pos['entry_price']:.2f} "
                f"mv=${pos['market_value']:.2f} [{info['market']}]"
            )
        else:
            print(f"{ticker}: NO POSITION [{info['market']}]")

        orders = info.get("orders", [])
        print(f"  orders ({len(orders)} total):")
        if not orders:
            print("    (none)")
        for o in orders:
            parts = [f"[{o['type']}]"]
            if o["class"]:
                parts.append(f"class={o['class']}")
            parts.append(f"{o['side']}")
            if o["qty"]:
                parts.append(f"qty={o['qty']}")
            if o["stop_price"]:
                parts.append(f"stop=${o['stop_price']}")
            if o["limit_price"]:
                parts.append(f"limit=${o['limit_price']}")
            parts.append(f"id={o['id_short']}...")
            print(f"    {'  '.join(parts)}")

        for w in info.get("warnings", []):
            print(f"  ⚠️  WARNING: {w}")
            if "MISMATCH" in w or "MULTIPLE" in w:
                all_ok = False

        if info.get("needs_migration"):
            print(f"  → NEEDS MIGRATION (1 stop + 1 TP, qty ok)")
        elif not info.get("warnings"):
            print(f"  → (no obvious action needed)")
        print()

    print("=" * 68)
    if all_ok:
        print("Reconnaissance complete — no blocking issues found.")
    else:
        print("⚠️  BLOCKING ISSUES FOUND — resolve before migrating.")
    print("=" * 68)
    return results


# ── Migration ───────────────────────────────────────────────────────────────

def migrate_ticker(broker, ticker: str) -> None:
    """Migrate ONE ticker from independent stop+TP to native OCO bracket.

    Steps:
      1. Snapshot position + orders
      2. Identify stop and TP (abort if not exactly 1 each)
      3. Check for trailing_stop or already-bracket (skip)
      4. Verify qty coverage
      5. Cancel stop → wait for confirmation
      6. Cancel TP → wait for confirmation
      7. Place OCO bracket (LimitOrderRequest with OCO class)
      8. Verify post-state (new stop+limit SELL orders present)
    """
    from alpaca.trading.requests import LimitOrderRequest, TakeProfitRequest, StopLossRequest
    from alpaca.trading.enums import OrderSide as AlpacaSide, OrderClass, TimeInForce

    print(f"\n{'=' * 60}")
    print(f"MIGRATING: {ticker}")
    print(f"{'=' * 60}")

    # ── Step 1: Snapshot ──────────────────────────────────────
    print(f"[1/8] Snapshotting position + open orders for {ticker}...")
    positions = broker.get_positions()
    pos = next((p for p in positions if p.ticker.upper() == ticker.upper()), None)

    if pos is None:
        raise SystemExit(f"ABORT {ticker}: No position found at broker — cannot migrate")

    pos_qty = abs(int(pos.shares))
    print(f"      Position: qty={pos_qty} entry=${pos.entry_price:.2f} mv=${pos.market_value:.2f}")

    open_orders = get_orders_for_ticker(broker, ticker)
    print(f"      Open orders ({len(open_orders)}):")
    for o in open_orders:
        otype = o.raw.get("order_type", "")
        oclass = o.raw.get("order_class", "")
        side = o.raw.get("side", "")
        stop = o.raw.get("stop_price", "")
        limit = o.raw.get("limit_price", "")
        oid = (o.raw.get("id") or "")[:8]
        print(f"        [{otype}] class={oclass} {side} stop={stop} limit={limit} id={oid}...")

    # ── Step 2: Classify orders ───────────────────────────────
    trailing_orders = [
        o for o in open_orders
        if (o.raw.get("order_type") or "").lower() == "trailing_stop"
    ]
    bracket_oco_orders = [
        o for o in open_orders
        if (o.raw.get("order_class") or "").lower() in ("bracket", "oco")
    ]
    stop_orders = [
        o for o in open_orders
        if (o.raw.get("order_type") or "").lower() in ("stop", "stop_limit")
        and (o.raw.get("side") or "").lower() == "sell"
        and (o.raw.get("order_class") or "").lower() not in ("bracket", "oco")
    ]
    tp_orders = [
        o for o in open_orders
        if (o.raw.get("order_type") or "").lower() == "limit"
        and (o.raw.get("side") or "").lower() == "sell"
        and (o.raw.get("order_class") or "").lower() not in ("bracket", "oco")
    ]

    print(f"[2/8] Classification: stop={len(stop_orders)} tp={len(tp_orders)} "
          f"trailing={len(trailing_orders)} bracket_oco={len(bracket_oco_orders)}")

    # ── Step 3: Guard checks ──────────────────────────────────
    print("[3/8] Guard checks...")

    if trailing_orders:
        print(f"  SKIP {ticker} — has trailing_stop order(s), NOT migrating to static OCO")
        print("  (trailing stops are dynamic; static OCO would lock in the current trail level)")
        return

    if bracket_oco_orders:
        print(f"  SKIP {ticker} — already has bracket/oco order_class (already migrated)")
        return

    if len(stop_orders) != 1:
        raise SystemExit(
            f"ABORT {ticker}: expected exactly 1 STOP SELL order, "
            f"found {len(stop_orders)} — manual investigation required"
        )
    if len(tp_orders) != 1:
        raise SystemExit(
            f"ABORT {ticker}: expected exactly 1 LIMIT (TP) SELL order, "
            f"found {len(tp_orders)} — manual investigation required"
        )

    # ── Step 4: Qty coverage ──────────────────────────────────
    print("[4/8] Verifying qty coverage...")
    stop_qty = int(float(stop_orders[0].raw.get("qty") or 0))
    tp_qty = int(float(tp_orders[0].raw.get("qty") or 0))

    if stop_qty != pos_qty:
        raise SystemExit(
            f"ABORT {ticker}: stop order qty={stop_qty} != position qty={pos_qty} "
            f"— partial coverage detected, manual investigation required"
        )
    if tp_qty != pos_qty:
        raise SystemExit(
            f"ABORT {ticker}: TP order qty={tp_qty} != position qty={pos_qty} "
            f"— partial coverage detected, manual investigation required"
        )
    print(f"      qty coverage OK: position={pos_qty} stop={stop_qty} tp={tp_qty}")

    # ── Snapshot prices BEFORE any cancellation ───────────────
    stop_price_str = stop_orders[0].raw.get("stop_price") or ""
    tp_price_str = tp_orders[0].raw.get("limit_price") or ""

    if not stop_price_str or not tp_price_str:
        raise SystemExit(
            f"ABORT {ticker}: cannot read stop_price={stop_price_str!r} or "
            f"limit_price={tp_price_str!r} from order — prices must be present before cancel"
        )

    stop_price = float(stop_price_str)
    tp_price = float(tp_price_str)
    stop_id = stop_orders[0].raw.get("id") or ""
    tp_id = tp_orders[0].raw.get("id") or ""

    print(f"      Prices locked in: stop=${stop_price:.4f}  tp=${tp_price:.4f}")
    print(f"      Stop order id={stop_id[:8]}...  TP order id={tp_id[:8]}...")

    # Final human confirmation point — print what we are about to do
    print()
    print(f"  *** ABOUT TO EXECUTE for {ticker} ***")
    print(f"      Cancel stop id={stop_id[:8]}...")
    print(f"      Cancel TP   id={tp_id[:8]}...")
    print(f"      Place OCO: qty={pos_qty} stop=${stop_price:.4f} tp=${tp_price:.4f}")
    print()

    # ── Step 5: Cancel stop order ─────────────────────────────
    print(f"[5/8] Cancelling stop order {stop_id[:8]}...")
    cancel_result = broker.cancel_order(stop_id)
    if not cancel_result.success:
        raise SystemExit(
            f"ABORT {ticker}: cancel_order({stop_id[:8]}...) returned success=False: "
            f"{cancel_result.message} — POSITION STILL HAS TP ORDER (no action yet)"
        )
    print(f"      cancel_order submitted. Waiting for confirmation (10s timeout)...")

    stop_confirmed = broker._wait_for_cancel_confirmed(stop_id, timeout_s=10.0)
    if not stop_confirmed:
        raise SystemExit(
            f"ABORT {ticker}: stop cancel NOT confirmed within 10s "
            f"(id={stop_id[:8]}...). "
            f"POSITION STATE: stop order may still be active, TP is still active. "
            f"DO NOT proceed — manually verify at broker dashboard before retrying."
        )
    print(f"      ✓ Stop cancel confirmed (id={stop_id[:8]}...)")

    # ── Step 6: Cancel TP order ───────────────────────────────
    print(f"[6/8] Cancelling TP order {tp_id[:8]}...")
    cancel_tp_result = broker.cancel_order(tp_id)
    if not cancel_tp_result.success:
        raise SystemExit(
            f"ABORT {ticker}: cancel_order({tp_id[:8]}...) returned success=False: "
            f"{cancel_tp_result.message} — "
            f"⚠️  CRITICAL: STOP IS CANCELLED, TP MAY STILL BE ACTIVE. "
            f"Position is partially unprotected. Manually verify at broker immediately."
        )
    print(f"      cancel_order submitted. Waiting for confirmation (10s timeout)...")

    tp_confirmed = broker._wait_for_cancel_confirmed(tp_id, timeout_s=10.0)
    if not tp_confirmed:
        raise SystemExit(
            f"ABORT {ticker}: TP cancel NOT confirmed within 10s "
            f"(id={tp_id[:8]}...). "
            f"⚠️  CRITICAL: STOP IS CANCELLED, TP STATUS UNCERTAIN. "
            f"Position may be UNPROTECTED. Manually verify at broker immediately. "
            f"If TP is still live, position has one-sided protection only."
        )
    print(f"      ✓ TP cancel confirmed (id={tp_id[:8]}...)")
    print(f"      ⚡ POSITION NOW UNPROTECTED — placing OCO immediately...")

    # ── Step 7: Place OCO bracket ─────────────────────────────
    print(f"[7/8] Placing OCO bracket: qty={pos_qty} stop=${stop_price:.4f} tp=${tp_price:.4f}...")

    alpaca_symbol = ticker  # US equities: same symbol at Alpaca
    try:
        request = LimitOrderRequest(
            symbol=alpaca_symbol,
            qty=pos_qty,
            side=AlpacaSide.SELL,
            limit_price=round(tp_price, 2),
            order_class=OrderClass.OCO,
            take_profit=TakeProfitRequest(limit_price=round(tp_price, 2)),
            stop_loss=StopLossRequest(stop_price=round(stop_price, 2)),
            time_in_force=TimeInForce.GTC,
        )
        order = broker._broker_call(broker._trade_client.submit_order, request)
    except Exception as e:
        raise SystemExit(
            f"ABORT {ticker}: OCO place FAILED: {e} "
            f"⚠️  CRITICAL: POSITION IS UNPROTECTED (both stop and TP were cancelled). "
            f"Manually place protective orders at broker immediately. "
            f"stop=${stop_price:.4f}  tp=${tp_price:.4f}  qty={pos_qty}"
        )

    oco_id = str(getattr(order, "id", "?"))
    print(f"      ✓ OCO placed: id={oco_id[:8]}...")
    print(f"        stop=${stop_price:.4f}  tp=${tp_price:.4f}  qty={pos_qty}")

    # ── Step 8: Verify post-state ──────────────────────────────
    print("[8/8] Verifying post-state (sleeping 2s for broker to settle)...")
    time.sleep(2)

    new_orders = get_orders_for_ticker(broker, ticker)
    print(f"      Post-state orders ({len(new_orders)}):")
    for o in new_orders:
        otype = o.raw.get("order_type", "")
        oclass = o.raw.get("order_class", "")
        side = o.raw.get("side", "")
        stop = o.raw.get("stop_price", "")
        limit = o.raw.get("limit_price", "")
        oid = (o.raw.get("id") or "")[:8]
        print(f"        [{otype}] class={oclass} {side} stop={stop} limit={limit} id={oid}...")

    # Check for OCO presence
    oco_orders = [
        o for o in new_orders
        if (o.raw.get("order_class") or "").lower() in ("bracket", "oco")
    ]
    sell_orders = [
        o for o in new_orders
        if (o.raw.get("side") or "").lower() == "sell"
    ]
    has_stop = any(
        (o.raw.get("order_type") or "").lower() in ("stop", "stop_limit")
        for o in sell_orders
    )
    has_limit = any(
        (o.raw.get("order_type") or "").lower() == "limit"
        for o in sell_orders
    )

    if not oco_orders and not (has_stop and has_limit):
        raise SystemExit(
            f"ABORT {ticker}: post-state verification FAILED — "
            f"no OCO/bracket class AND no stop+limit SELL pair found. "
            f"⚠️  POSITION MAY BE UNPROTECTED. "
            f"Manually check broker dashboard immediately. OCO id={oco_id[:8]}..."
        )

    print(f"\n✅ {ticker} MIGRATED SUCCESSFULLY")
    if oco_orders:
        print(f"   OCO order confirmed in open orders (order_class=oco/bracket)")
    else:
        print(f"   Stop + limit SELL pair confirmed in open orders (OCO legs)")
    print(f"   stop=${stop_price:.4f}  tp=${tp_price:.4f}  qty={pos_qty}")
    print(f"   OCO id={oco_id}")


# ── CLI ─────────────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Migrate Atlas positions from independent stop+TP to Alpaca OCO brackets",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Read-only recon of all 4 tickers
  python3 scripts/migrate_to_oco.py --reconnaissance

  # Migrate GLD only (must add --execute to prevent accidents)
  python3 scripts/migrate_to_oco.py --ticker GLD --execute
""",
    )
    parser.add_argument(
        "--reconnaissance",
        action="store_true",
        help="Read-only dump of current state for all 4 tickers",
    )
    parser.add_argument(
        "--ticker",
        choices=sorted(ALLOWED_TICKERS),
        help="Ticker to migrate (one of: GLD XLI XLY CAT)",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Required flag to actually perform mutations (safety gate)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.reconnaissance and args.ticker:
        print("ERROR: --reconnaissance and --ticker are mutually exclusive")
        return 1

    if not args.reconnaissance and not args.ticker:
        parser.print_help()
        return 1

    if args.reconnaissance:
        run_reconnaissance()
        return 0

    # --ticker mode
    ticker = args.ticker

    if not args.execute:
        print(f"DRY-RUN: would migrate {ticker}. Add --execute to run.")
        print(f"Preview: run --reconnaissance first to see current state.")
        return 0

    market_id = TICKER_MARKET[ticker]
    print(f"Connecting to live broker for {market_id}...")
    try:
        broker = connect_broker(market_id)
    except Exception as e:
        print(f"FATAL: Cannot connect to broker for {market_id}: {e}")
        return 1

    try:
        migrate_ticker(broker, ticker)
    except SystemExit as e:
        print(f"\n{'!' * 60}")
        print(f"MIGRATION ABORTED: {e}")
        print(f"{'!' * 60}")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
