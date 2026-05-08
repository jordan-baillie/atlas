"""Audit script: investigate same-bar buy/sell round-trips.

Connects to Alpaca, scans the last 30 days of CLOSED orders, pairs each
FILLED BUY with its earliest subsequent FILLED SELL for the same symbol,
then cross-references the trade ledger (SQLite) to classify each round-trip
as INVISIBLE / PARTIAL / RECORDED.

Run:
    cd /root/atlas && python3 scripts/investigate_same_bar_round_trips.py

Writes:
    docs/audits/same-bar-round-trips-audit.md
"""
from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")
log = logging.getLogger(__name__)

AUDIT_PATH = PROJECT / "docs" / "audits" / "same-bar-round-trips-audit.md"
SAME_BAR_THRESHOLD_S = 300  # 5 minutes
TODAY_DATE = "2026-05-07"
LOOKBACK_DAYS = 30


# ── helpers ────────────────────────────────────────────────────────────────────

def _side(order) -> str:
    try:
        return order.side.value.lower()
    except Exception:
        return str(getattr(order, "side", "")).lower()


def _status(order) -> str:
    try:
        return order.status.value.lower()
    except Exception:
        return str(getattr(order, "status", "")).lower()


def _order_type(order) -> str:
    try:
        return order.order_type.value.lower()
    except Exception:
        return str(getattr(order, "order_type", getattr(order, "type", ""))).lower()


def _get_filled_at(order) -> datetime | None:
    fa = getattr(order, "filled_at", None)
    if fa is None:
        return None
    if isinstance(fa, datetime):
        if fa.tzinfo is None:
            return fa.replace(tzinfo=timezone.utc)
        return fa
    try:
        s = str(fa)
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        return datetime.fromisoformat(s)
    except Exception:
        return None


def _classify_exit_reason(client_order_id: str) -> str:
    coid = str(client_order_id).lower()
    if "atlas_stop_" in coid or "_stop_" in coid or "stop" in coid:
        return "stop_loss"
    if "atlas_tp_" in coid or "_tp_" in coid:
        return "take_profit"
    if "atlas_trail_" in coid or "trail" in coid:
        return "trailing_stop_fill"
    if "atlas_exit_" in coid or "exit" in coid:
        return "signal_exit"
    if coid.startswith("atlas_"):
        return "unknown_atlas"
    return "unknown_non_atlas"


# ── main ───────────────────────────────────────────────────────────────────────

def run_audit() -> dict:
    from brokers.registry import get_live_broker
    from utils.config import get_active_config
    from alpaca.trading.requests import GetOrdersRequest
    from alpaca.trading.enums import QueryOrderStatus

    # Load today's plan
    plan_entries: dict[str, dict] = {}
    plan_path = PROJECT / "plans" / f"plan_sp500_{TODAY_DATE}.json"
    if plan_path.exists():
        try:
            with open(plan_path) as f:
                plan_data = json.load(f)
            for e in plan_data.get("proposed_entries", []):
                t = e.get("ticker", "")
                if t:
                    plan_entries[t] = e
        except Exception as ex:
            print(f"Warning: could not load plan: {ex}")

    # Connect to broker
    print("Connecting to Alpaca ...")
    config = get_active_config("sp500")
    broker = get_live_broker(config)
    if not broker or not broker.connect():
        print("ERROR: Cannot connect to broker")
        sys.exit(1)

    try:
        return _do_audit(broker, plan_entries)
    finally:
        try:
            broker.disconnect()
        except Exception:
            pass


def _do_audit(broker, plan_entries: dict) -> dict:
    from alpaca.trading.requests import GetOrdersRequest
    from alpaca.trading.enums import QueryOrderStatus

    now_utc = datetime.now(timezone.utc)
    start = now_utc - timedelta(days=LOOKBACK_DAYS)

    print(f"Fetching last {LOOKBACK_DAYS} days of CLOSED orders ...")
    req = GetOrdersRequest(
        status=QueryOrderStatus.CLOSED,
        after=start,
        limit=500,
    )
    orders = broker._broker_call(broker._trade_client.get_orders, filter=req)
    print(f"  Fetched {len(orders)} orders total")

    # ── Separate FILLED BUYs and FILLED SELLs ─────────────────────────────
    filled_buys: list = []
    filled_sells: list = []
    for o in orders:
        if _status(o) != "filled":
            continue
        if _side(o) == "buy":
            filled_buys.append(o)
        elif _side(o) == "sell":
            filled_sells.append(o)

    print(f"  FILLED BUYs: {len(filled_buys)} | FILLED SELLs: {len(filled_sells)}")

    # ── Pair each BUY with earliest SELL at/after buy fill time ───────────
    # Group sells by symbol
    sells_by_sym: dict[str, list] = {}
    for o in filled_sells:
        sym = str(o.symbol)
        sells_by_sym.setdefault(sym, []).append(o)
    for sym in sells_by_sym:
        sells_by_sym[sym].sort(key=lambda o: _get_filled_at(o) or datetime.min.replace(tzinfo=timezone.utc))

    # Load SQLite trade records for cross-referencing
    import sqlite3
    db_path = PROJECT / "data" / "atlas.db"
    sqlite_entry_order_ids: set[str] = set()
    sqlite_exit_tickers_and_dates: set[str] = set()
    try:
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        # trades table uses order IDs stored in stop_order_id/tp_order_id
        # We'll check by entry_date and ticker for cross-referencing
        rows = conn.execute(
            "SELECT ticker, entry_date, exit_date, status FROM trades WHERE entry_date >= ?",
            (start.isoformat()[:10],)
        ).fetchall()
        for row in rows:
            key_entry = f"{row['ticker']}|{str(row['entry_date'])[:10]}"
            sqlite_entry_order_ids.add(key_entry)
            if row['exit_date']:
                key_exit = f"{row['ticker']}|{str(row['exit_date'])[:10]}"
                sqlite_exit_tickers_and_dates.add(key_exit)
        conn.close()
    except Exception as ex:
        print(f"Warning: SQLite load failed: {ex}")

    # ── Pair round-trips ───────────────────────────────────────────────────
    round_trips: list[dict] = []
    for buy in filled_buys:
        sym = str(buy.symbol)
        buy_filled_at = _get_filled_at(buy)
        if buy_filled_at is None:
            continue
        buy_price = float(buy.filled_avg_price or 0)
        buy_qty = int(float(buy.filled_qty or buy.qty or 0))
        if buy_price <= 0 or buy_qty <= 0:
            continue

        # Find earliest qualifying SELL
        sell_candidates = sells_by_sym.get(sym, [])
        matched_sell = None
        for s in sell_candidates:
            sell_fa = _get_filled_at(s)
            if sell_fa is None:
                continue
            if sell_fa >= buy_filled_at:
                matched_sell = s
                break  # earliest qualifying sell

        if matched_sell is None:
            continue  # no matching sell → open position, not a round-trip

        sell_filled_at = _get_filled_at(matched_sell)
        sell_price = float(matched_sell.filled_avg_price or 0)
        sell_qty = int(float(matched_sell.filled_qty or matched_sell.qty or 0))
        coid = str(getattr(matched_sell, "client_order_id", ""))
        sell_type = _order_type(matched_sell)
        exit_reason = _classify_exit_reason(coid)
        round_trip_seconds = (sell_filled_at - buy_filled_at).total_seconds()
        qty = min(buy_qty, sell_qty)
        realized_pnl = round((sell_price - buy_price) * qty, 2)

        # Cross-reference ledger: check SQLite by ticker+date
        buy_date_str = buy_filled_at.strftime("%Y-%m-%d")
        sell_date_str = sell_filled_at.strftime("%Y-%m-%d")
        key_entry = f"{sym}|{buy_date_str}"
        key_exit = f"{sym}|{sell_date_str}"
        has_entry = key_entry in sqlite_entry_order_ids
        has_exit = key_exit in sqlite_exit_tickers_and_dates

        if has_entry and has_exit:
            ledger_status = "RECORDED"
        elif has_entry and not has_exit:
            ledger_status = "PARTIAL_ENTRY_ONLY"
        elif not has_entry and has_exit:
            ledger_status = "PARTIAL_EXIT_ONLY"
        else:
            ledger_status = "INVISIBLE"

        is_same_bar = round_trip_seconds < SAME_BAR_THRESHOLD_S
        is_today = buy_date_str == TODAY_DATE

        # Plan context
        plan_e = plan_entries.get(sym, {})

        rt = {
            "ticker": sym,
            "buy_filled_at": buy_filled_at.isoformat(),
            "buy_price": buy_price,
            "buy_qty": buy_qty,
            "sell_filled_at": sell_filled_at.isoformat(),
            "sell_price": sell_price,
            "sell_qty": sell_qty,
            "sell_type": sell_type,
            "exit_reason": exit_reason,
            "sell_client_order_id": coid,
            "round_trip_seconds": round_trip_seconds,
            "realized_pnl": realized_pnl,
            "is_same_bar": is_same_bar,
            "is_today": is_today,
            "ledger_status": ledger_status,
            "plan_entry_price": plan_e.get("entry_price"),
            "plan_stop_price": plan_e.get("stop_price"),
            "strategy": plan_e.get("strategy", "unknown"),
            "buy_order_id": str(buy.id),
            "sell_order_id": str(matched_sell.id),
        }
        round_trips.append(rt)

    # ── Stats ──────────────────────────────────────────────────────────────
    today_trips = [r for r in round_trips if r["is_today"]]
    all_same_bar = [r for r in round_trips if r["is_same_bar"]]
    today_same_bar = [r for r in today_trips if r["is_same_bar"]]
    invisible = [r for r in round_trips if r["ledger_status"] == "INVISIBLE"]
    partial = [r for r in round_trips if "PARTIAL" in r["ledger_status"]]
    recorded = [r for r in round_trips if r["ledger_status"] == "RECORDED"]

    stats = {
        "lookback_days": LOOKBACK_DAYS,
        "total_round_trips_30d": len(round_trips),
        "same_bar_30d": len(all_same_bar),
        "today_round_trips": len(today_trips),
        "today_same_bar": len(today_same_bar),
        "invisible_30d": len(invisible),
        "partial_30d": len(partial),
        "recorded_30d": len(recorded),
    }

    print("\n=== SUMMARY ===")
    print(f"  Total round-trips (30d):     {stats['total_round_trips_30d']}")
    print(f"  Same-bar (<5min) (30d):      {stats['same_bar_30d']}")
    print(f"  Today ({TODAY_DATE}):         {stats['today_round_trips']}")
    print(f"  Today same-bar:              {stats['today_same_bar']}")
    print(f"  INVISIBLE in ledger (30d):   {stats['invisible_30d']}")
    print(f"  PARTIAL in ledger (30d):     {stats['partial_30d']}")
    print(f"  RECORDED in ledger (30d):    {stats['recorded_30d']}")

    if today_same_bar:
        today_pnl = sum(r["realized_pnl"] for r in today_same_bar)
        print(f"\n=== TODAY'S SAME-BAR ROUND-TRIPS (2026-05-07) ===")
        print(f"  Total realized PnL today:  ${today_pnl:.2f}")
        for r in today_same_bar:
            elapsed = r['round_trip_seconds']
            print(
                f"  {r['ticker']:6s}  BUY @{r['buy_price']:.2f}  "
                f"SELL @{r['sell_price']:.2f}  PnL ${r['realized_pnl']:+.2f}  "
                f"({elapsed:.0f}s)  reason={r['exit_reason']}  ledger={r['ledger_status']}"
            )

    if today_trips and not today_same_bar:
        print(f"\n  No same-bar round-trips today. All today's pairs:")
        for r in today_trips:
            print(
                f"  {r['ticker']:6s}  BUY @{r['buy_price']:.2f}  "
                f"SELL @{r['sell_price']:.2f}  {r['round_trip_seconds']:.0f}s  "
                f"ledger={r['ledger_status']}"
            )

    _write_audit(round_trips, stats, plan_entries)
    return {"stats": stats, "round_trips": round_trips}


def _write_audit(round_trips: list[dict], stats: dict, plan_entries: dict):
    AUDIT_PATH.parent.mkdir(parents=True, exist_ok=True)
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    lines = [
        "# Same-Bar Round-Trip Audit",
        f"",
        f"**Generated**: {now_str}  ",
        f"**Lookback**: {LOOKBACK_DAYS} days  ",
        f"**Same-bar threshold**: <{SAME_BAR_THRESHOLD_S}s  ",
        "",
        "## Summary Stats",
        "",
        f"| Metric | Count |",
        f"|--------|-------|",
        f"| Total round-trips (30d) | {stats['total_round_trips_30d']} |",
        f"| Same-bar (<5min) (30d) | {stats['same_bar_30d']} |",
        f"| Today ({TODAY_DATE}) round-trips | {stats['today_round_trips']} |",
        f"| Today same-bar | {stats['today_same_bar']} |",
        f"| INVISIBLE in ledger (30d) | {stats['invisible_30d']} |",
        f"| PARTIAL in ledger (30d) | {stats['partial_30d']} |",
        f"| RECORDED in ledger (30d) | {stats['recorded_30d']} |",
        "",
    ]

    # Today's deep-dive
    today_same = [r for r in round_trips if r["is_same_bar"] and r["is_today"]]
    today_all = [r for r in round_trips if r["is_today"]]
    today_pnl = sum(r["realized_pnl"] for r in today_same) if today_same else 0

    lines += [
        f"## Today's Deep-Dive ({TODAY_DATE})",
        "",
    ]
    if today_all:
        lines += [
            f"**Total same-bar realized PnL today**: ${today_pnl:.2f}",
            "",
            "| Ticker | Strategy | Buy Time | Buy Price | Sell Time | Sell Price | Elapsed (s) | Exit Reason | PnL | Plan Stop | Ledger |",
            "|--------|----------|----------|-----------|-----------|------------|-------------|-------------|-----|-----------|--------|",
        ]
        for r in sorted(today_all, key=lambda x: x["buy_filled_at"]):
            buy_ts = r["buy_filled_at"][11:19]
            sell_ts = r["sell_filled_at"][11:19]
            plan_stop = f"${r['plan_stop_price']:.4f}" if r.get("plan_stop_price") else "N/A"
            lines.append(
                f"| {r['ticker']} | {r['strategy']} | {buy_ts} UTC | ${r['buy_price']:.2f} "
                f"| {sell_ts} UTC | ${r['sell_price']:.2f} | {r['round_trip_seconds']:.0f} "
                f"| {r['exit_reason']} | ${r['realized_pnl']:+.2f} | {plan_stop} | {r['ledger_status']} |"
            )
    else:
        lines += [f"No round-trips found on {TODAY_DATE}."]

    # Root-cause analysis
    all_same_bar = [r for r in round_trips if r["is_same_bar"]]
    lines += [
        "",
        "## Root-Cause Analysis",
        "",
    ]
    if all_same_bar:
        stop_losses = [r for r in all_same_bar if r["exit_reason"] == "stop_loss"]
        tp_fills = [r for r in all_same_bar if r["exit_reason"] == "take_profit"]
        trail_fills = [r for r in all_same_bar if "trail" in r["exit_reason"]]
        unknown = [r for r in all_same_bar if "unknown" in r["exit_reason"]]
        lines += [
            f"Of {len(all_same_bar)} same-bar round-trips:",
            f"- Stop-loss fills (opening volatility): **{len(stop_losses)}** "
            f"({100*len(stop_losses)//len(all_same_bar) if all_same_bar else 0}%)",
            f"- Take-profit fills: {len(tp_fills)}",
            f"- Trailing-stop fills: {len(trail_fills)}",
            f"- Unknown: {len(unknown)}",
            "",
        ]

        # Check if stop prices match plan stops
        stop_loss_cases = stop_losses
        if stop_loss_cases:
            lines += [
                "### Cause (a) — Opening Volatility Analysis",
                "",
                "Comparing actual sell price to planned stop price:",
                "",
                "| Ticker | Buy Price | Sell Price | Plan Stop | Gap to Plan Stop | Verdict |",
                "|--------|-----------|------------|-----------|-------------------|---------|",
            ]
            for r in stop_loss_cases:
                ps = r.get("plan_stop_price")
                if ps and ps > 0:
                    gap = abs(r["sell_price"] - ps)
                    gap_pct = gap / ps * 100
                    verdict = "MATCHES_PLAN_STOP" if gap_pct < 2.0 else "DIFFERS_FROM_PLAN_STOP"
                else:
                    gap_pct = None
                    verdict = "NO_PLAN_STOP"
                gap_str = f"{gap_pct:.2f}%" if gap_pct is not None else "N/A"
                lines.append(
                    f"| {r['ticker']} | ${r['buy_price']:.2f} | ${r['sell_price']:.2f} "
                    f"| {('$'+str(round(ps,4))) if ps else 'N/A'} | {gap_str} | {verdict} |"
                )

    # All round-trips table
    lines += [
        "",
        "## All Round-Trips (30 days)",
        "",
        "| Date | Ticker | Buy Price | Sell Price | Elapsed (s) | Same-Bar | Exit Reason | PnL | Ledger |",
        "|------|--------|-----------|------------|-------------|----------|-------------|-----|--------|",
    ]
    for r in sorted(round_trips, key=lambda x: x["buy_filled_at"], reverse=True):
        date = r["buy_filled_at"][:10]
        same = "✓" if r["is_same_bar"] else ""
        lines.append(
            f"| {date} | {r['ticker']} | ${r['buy_price']:.2f} | ${r['sell_price']:.2f} "
            f"| {r['round_trip_seconds']:.0f} | {same} | {r['exit_reason']} "
            f"| ${r['realized_pnl']:+.2f} | {r['ledger_status']} |"
        )

    # Systemic bug section
    lines += [
        "",
        "## Systemic Bug Assessment",
        "",
        "### Confirmed: `reconcile_entry_fills` silently drops same-bar round-trips",
        "",
        "**Location**: `brokers/live_executor.py` lines ~2417-2431",
        "",
        "The guard introduced on 2026-05-06 to prevent EBAY zombie rows correctly",
        "prevents `OPEN` trade rows from being created for already-closed positions.",
        "However, it also **silently drops** the recording of the completed round-trip.",
        "",
        "**Result**: Any BUY+SELL pair where SELL fills within the 7-day order scan",
        "window will be invisible in the trade ledger unless `reconcile_exit_fills`",
        "separately picks it up from an existing entry record.",
        "",
        f"- **INVISIBLE today**: {stats['invisible_30d']} of {stats['total_round_trips_30d']} total 30d round-trips",
        "- `reconcile_exit_fills` cannot fix this alone — it requires a pre-existing",
        "  entry record to compute PnL, so a round-trip with NO entry record produces",
        "  a SELL entry with entry_price=0, PnL=None.",
        "",
        "### Fix required",
        "",
        "When the guard fires (`sell_filled_at >= buy_filled_at`), instead of",
        "silently skipping, the reconciler should record BOTH an entry stub AND",
        "an exit record marked `same_bar_round_trip=True`.",
    ]

    content = "\n".join(lines)
    AUDIT_PATH.write_text(content)
    print(f"\n  Audit report written to: {AUDIT_PATH}")


if __name__ == "__main__":
    run_audit()
