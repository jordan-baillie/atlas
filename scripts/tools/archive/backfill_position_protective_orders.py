#!/usr/bin/env python3
"""Backfill position_protective_orders from current open trades + broker state.

For each open trade in the SQLite `trades` table:
  1. Reads stop_order_id / tp_order_id from the trades row
  2. Verifies those IDs are still active at the broker (get_order_status)
  3. Extracts stop_price / tp_price from the live order
  4. Calls upsert_protective_record() to create/update the canonical row

Modes:
    --dry-run (default): print planned actions, no writes
    --apply:             perform upserts
    --market <name>:     limit to one market/universe

Expected result (4 open positions as of 2026-04-29):
    CAT  | sp500          | stop_order_id + tp_order_id
    GLD  | commodity_etfs | stop_order_id + tp_order_id
    XLY  | sector_etfs    | stop_order_id + tp_order_id
    XLI  | sector_etfs    | stop_order_id + tp_order_id

Usage:
    python3 scripts/backfill_position_protective_orders.py              # dry-run
    python3 scripts/backfill_position_protective_orders.py --apply
    python3 scripts/backfill_position_protective_orders.py --apply --market sp500
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

# ── Project bootstrap ─────────────────────────────────────────────────────────
ATLAS_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ATLAS_ROOT))

from utils.logging_config import setup_logging           # noqa: E402
from db.atlas_db import get_db, upsert_protective_record # noqa: E402
from brokers.registry import get_live_broker             # noqa: E402
from utils.config import get_active_config               # noqa: E402

log = setup_logging("backfill_protective_orders")

# Broker uses sp500 config for auth; all markets share the same Alpaca account.
_CONFIG_MARKET = "sp500"


def _safe_float(val: Any) -> Optional[float]:
    """Convert to float, return None if invalid."""
    if val is None:
        return None
    try:
        f = float(val)
        return f if f == f and f != 0.0 else None   # reject NaN and 0.0
    except (TypeError, ValueError):
        return None


def _get_open_trades(market_filter: Optional[str]) -> list[dict]:
    """Return all open non-superseded trades, optionally filtered by universe."""
    with get_db() as db:
        if market_filter:
            rows = db.execute(
                "SELECT id, ticker, universe, shares, stop_order_id, tp_order_id, "
                "stop_price, take_profit, entry_price, entry_date "
                "FROM trades "
                "WHERE status='open' AND superseded=0 AND universe=? "
                "ORDER BY id",
                (market_filter,),
            ).fetchall()
        else:
            rows = db.execute(
                "SELECT id, ticker, universe, shares, stop_order_id, tp_order_id, "
                "stop_price, take_profit, entry_price, entry_date "
                "FROM trades "
                "WHERE status='open' AND superseded=0 "
                "ORDER BY id",
            ).fetchall()
    return [dict(r) for r in rows]


def _verify_order(broker: Any, order_id: str) -> dict:
    """Query broker for an order. Returns dict with status, stop_price, limit_price.

    Returns empty dict if order_id is blank or broker call fails.
    """
    if not order_id or order_id.strip() == "":
        return {}
    try:
        result = broker.get_order_status(order_id)
        if not result.success:
            log.warning("Order %s not found at broker: %s", order_id, result.message)
            return {}
        raw = result.raw or {}
        return {
            "order_id": order_id,
            "status": raw.get("status", ""),
            "order_type": raw.get("order_type", ""),
            "stop_price": _safe_float(raw.get("stop_price")),
            "limit_price": _safe_float(raw.get("limit_price")),
        }
    except Exception as exc:
        log.warning("get_order_status(%s) failed: %s", order_id, exc)
        return {}


def _infer_oco_class(stop_info: dict, tp_info: dict) -> Optional[str]:
    """Infer oco_class from order types (best-effort)."""
    if not stop_info and not tp_info:
        return None
    if stop_info and tp_info:
        return "oco"
    return None


def _process_trade(
    trade: dict,
    broker: Any,
    apply: bool,
) -> dict:
    """Build protective record for one trade. Returns a result dict for reporting."""
    ticker = trade["ticker"]
    universe = trade["universe"] or "sp500"
    trade_id = trade["id"]
    shares = float(trade["shares"])
    stop_order_id = (trade.get("stop_order_id") or "").strip()
    tp_order_id = (trade.get("tp_order_id") or "").strip()

    # Start with DB-stored prices as fallback
    db_stop_price = _safe_float(trade.get("stop_price"))
    db_tp_price = _safe_float(trade.get("take_profit"))

    # Verify with broker
    stop_info = _verify_order(broker, stop_order_id)
    tp_info = _verify_order(broker, tp_order_id)

    # Resolve best stop price: broker > DB
    resolved_stop_price = (
        stop_info.get("stop_price") or stop_info.get("limit_price") or db_stop_price
    )
    resolved_tp_price = (
        tp_info.get("limit_price") or tp_info.get("stop_price") or db_tp_price
    )

    oco_class = _infer_oco_class(stop_info, tp_info)

    result = {
        "ticker": ticker,
        "market_id": universe,
        "trade_id": trade_id,
        "position_qty": shares,
        "stop_order_id": stop_order_id or None,
        "stop_price": resolved_stop_price,
        "tp_order_id": tp_order_id or None,
        "tp_price": resolved_tp_price,
        "oco_class": oco_class,
        "stop_verified": bool(stop_info),
        "tp_verified": bool(tp_info),
    }

    if apply:
        upsert_protective_record(
            market_id=universe,
            ticker=ticker,
            trade_id=trade_id,
            position_qty=shares,
            stop_order_id=stop_order_id or None,
            stop_price=resolved_stop_price,
            tp_order_id=tp_order_id or None,
            tp_price=resolved_tp_price,
            oco_class=oco_class,
        )
        log.info(
            "Upserted protective record: %s/%s  stop=%s tp=%s",
            universe, ticker, stop_order_id or "—", tp_order_id or "—",
        )

    return result


def _print_results(results: list[dict], apply: bool) -> None:
    mode = "APPLIED" if apply else "DRY-RUN"
    print(f"\n{'='*72}")
    print(f"Backfill position_protective_orders  [{mode}]")
    print(f"{'='*72}")
    print(f"{'Market':<18} {'Ticker':<8} {'Stop ID':<38} {'TP ID':<38} {'Stop$':>8} {'TP$':>8}")
    print(f"{'-'*18} {'-'*8} {'-'*38} {'-'*38} {'-'*8} {'-'*8}")
    for r in results:
        stop_id = (r.get("stop_order_id") or "—")[:36]
        tp_id = (r.get("tp_order_id") or "—")[:36]
        stop_p = f"${r['stop_price']:.2f}" if r.get("stop_price") else "—"
        tp_p = f"${r['tp_price']:.2f}" if r.get("tp_price") else "—"
        verified = "✓" if r.get("stop_verified") or r.get("tp_verified") else "⚠"
        print(f"{r['market_id']:<18} {r['ticker']:<8} {verified}{stop_id:<37} {tp_id:<38} {stop_p:>8} {tp_p:>8}")
    print(f"\nProcessed: {len(results)} open trade(s)")
    if not apply:
        print("Re-run with --apply to write records.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true",
                        help="Write records (default: dry-run)")
    parser.add_argument("--market", default=None,
                        help="Limit to one market/universe (e.g. sp500)")
    args = parser.parse_args(argv)

    # ── Connect to broker ────────────────────────────────────────────────────
    try:
        config = get_active_config(_CONFIG_MARKET)
        broker = get_live_broker(config)
        if broker is None:
            log.error("get_live_broker returned None -- is live_enabled=true in config?")
            return 1
        broker.connect()
        log.info("Broker connected.")
    except Exception as exc:
        log.error("Broker connection failed: %s", exc)
        try:
            from utils.telegram import send_message
            send_message(
                f"⚠️ <b>backfill_protective_orders</b> — broker connect failed:\n<code>{exc}</code>"
            )
        except Exception:
            pass
        return 1

    # ── Load open trades ──────────────────────────────────────────────────────
    trades = _get_open_trades(args.market)
    log.info("Found %d open trade(s) to process.", len(trades))

    if not trades:
        print("No open trades found. Nothing to backfill.")
        return 0

    # ── Process each trade ───────────────────────────────────────────────────
    results: list[dict] = []
    for trade in trades:
        try:
            r = _process_trade(trade, broker, apply=args.apply)
            results.append(r)
        except Exception as exc:
            log.error(
                "Failed to process %s/%s: %s",
                trade.get("universe"), trade.get("ticker"), exc,
                exc_info=True,
            )

    _print_results(results, apply=args.apply)
    return 0


if __name__ == "__main__":
    sys.exit(main())
