#!/usr/bin/env python3
"""Diagnostic script: investigate CHTR and ON held orders on Alpaca.

Prints:
  1. Alpaca account details (PDT status, buying power, shorting, etc.)
  2. For CHTR and ON: current held order(s) with reject_reason, asset metadata.

Read-only — does NOT send Telegram, does NOT modify any state.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

# ── Project root on sys.path ───────────────────────────────────────────────
PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))


def _safe_get(obj, attr: str, default="<unavailable>") -> str:
    val = getattr(obj, attr, default)
    if val is None:
        return "<null>"
    return str(val)


def _print_section(title: str) -> None:
    print()
    print("=" * 70)
    print(f"  {title}")
    print("=" * 70)


def main() -> None:
    # ── Connect to broker ──────────────────────────────────────────────────
    from brokers.registry import get_live_broker

    cfg_path = PROJECT / "config" / "active" / "sp500.json"
    if not cfg_path.exists():
        raise SystemExit(f"Config not found: {cfg_path}")

    with open(cfg_path) as f:
        cfg = json.load(f)

    print(f"Connecting to live broker (config: {cfg_path.name}) ...")
    b = get_live_broker(cfg)
    if not b.connect():
        raise SystemExit("Broker connect failed — check API keys and network.")

    tc = b._trade_client  # alpaca-py TradingClient instance

    # ── 1. Account summary ────────────────────────────────────────────────
    _print_section("ALPACA ACCOUNT SUMMARY")
    try:
        acct = tc.get_account()
        fields = [
            "pattern_day_trader",
            "daytrade_count",
            "buying_power",
            "shorting_enabled",
            "trade_suspended_by_user",
            "status",
            "equity",
            "account_number",
            "currency",
        ]
        for field in fields:
            print(f"  {field:35s}: {_safe_get(acct, field)}")
    except Exception as exc:
        print(f"  ERROR fetching account: {exc}")

    # ── 2. Current open orders for CHTR and ON ────────────────────────────
    _print_section("OPEN ORDERS (all) — filtered for CHTR / ON")
    TARGETS = {"CHTR", "ON"}
    try:
        open_orders = b.get_open_orders()
        target_orders = [o for o in open_orders if getattr(o, "ticker", "") in TARGETS]
        if target_orders:
            for o in target_orders:
                raw = getattr(o, "raw", {}) or {}
                print(f"\n  Ticker       : {o.ticker}")
                print(f"  order_id     : {getattr(o, 'order_id', 'N/A')}")
                print(f"  status       : {raw.get('status', 'N/A')}")
                print(f"  order_type   : {raw.get('order_type', 'N/A')}")
                print(f"  side         : {raw.get('side', 'N/A')}")
                print(f"  qty          : {raw.get('qty', 'N/A')}")
                print(f"  reject_reason: {raw.get('reject_reason', '<none>')}")
                print(f"  failed_at    : {raw.get('failed_at', '<none>')}")
                print(f"  filled_at    : {raw.get('filled_at', '<none>')}")
                print(f"  stop_price   : {raw.get('stop_price', 'N/A')}")
                print(f"  limit_price  : {raw.get('limit_price', 'N/A')}")
        else:
            print(f"  No open orders found for {TARGETS}")
    except Exception as exc:
        print(f"  ERROR fetching open orders: {exc}")

    # ── 3. Recent orders (all statuses) for CHTR and ON ──────────────────
    _print_section("RECENT ORDERS (all statuses, last 10) for CHTR / ON")
    try:
        from alpaca.trading.requests import GetOrdersRequest
        from alpaca.trading.enums import QueryOrderStatus

        for symbol in sorted(TARGETS):
            print(f"\n--- {symbol} ---")
            try:
                orders_raw = tc.get_orders(filter=GetOrdersRequest(
                    status=QueryOrderStatus.ALL,
                    symbols=[symbol],
                    limit=10,
                ))
                if not orders_raw:
                    print(f"  No order history found for {symbol}")
                    continue
                for o in orders_raw:
                    oid = _safe_get(o, "id")
                    status = _safe_get(o, "status")
                    otype = _safe_get(o, "order_type")
                    side = _safe_get(o, "side")
                    qty = _safe_get(o, "qty")
                    stop_price = _safe_get(o, "stop_price")
                    rejected_at = _safe_get(o, "failed_at")
                    submitted_at = _safe_get(o, "submitted_at")
                    reject_reason = _safe_get(o, "reject_reason", "<none>")
                    print(
                        f"  {oid[:16]}  status={status:<12}  type={otype:<15}  "
                        f"side={side:<5}  qty={qty:<8}  stop={stop_price:<10}  "
                        f"submitted={submitted_at}  failed={rejected_at}  "
                        f"reject_reason={reject_reason}"
                    )
            except Exception as exc:
                print(f"  ERROR fetching orders for {symbol}: {exc}")
    except ImportError as imp_exc:
        print(f"  alpaca-py not available or missing enums: {imp_exc}")
    except Exception as exc:
        print(f"  ERROR fetching all-status orders: {exc}")

    # ── 4. Asset metadata for CHTR and ON ────────────────────────────────
    _print_section("ASSET METADATA for CHTR / ON")
    asset_fields = [
        "tradable",
        "shortable",
        "marginable",
        "fractionable",
        "easy_to_borrow",
        "maintenance_margin_requirement",
        "status",
        "asset_class",
        "exchange",
    ]
    for symbol in sorted(TARGETS):
        print(f"\n  [{symbol}]")
        try:
            asset = tc.get_asset(symbol)
            for field in asset_fields:
                print(f"    {field:40s}: {_safe_get(asset, field)}")
        except Exception as exc:
            print(f"    ERROR fetching asset {symbol}: {exc}")

    # ── 5. Held-stop state file ───────────────────────────────────────────
    _print_section("HELD-STOP STATE FILE (data/stops_held_state.json)")
    state_path = PROJECT / "data" / "stops_held_state.json"
    if state_path.exists():
        try:
            state = json.loads(state_path.read_text())
            if state:
                for key, entry in state.items():
                    print(f"  {key}: {json.dumps(entry, indent=4)}")
            else:
                print("  (empty — no stuck orders tracked)")
        except Exception as exc:
            print(f"  ERROR reading state file: {exc}")
    else:
        print(f"  (file does not exist: {state_path})")

    _print_section("DONE")


if __name__ == "__main__":
    main()
