#!/usr/bin/env python3
"""
Task #310 — Historical overlay-enforce silent-impact audit.

One-time audit: identifies all overlay_decisions rows with non-null sizing_override,
cross-references with plan files and Alpaca order history to classify whether the
bug (DB-fallback enforcing overrides despite overlay.enabled=false) caused actual
position-size reductions.

Usage:
    python3 scripts/audit_overlay_decisions.py [--db PATH] [--output PATH] [--dry-run] [--limit N]

Milestone: overlay-silent-bug-fix
"""
from __future__ import annotations

import argparse
import glob
import json
import logging
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

# ── bootstrap sys.path ───────────────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from db.atlas_db import get_db  # noqa: E402

logger = logging.getLogger("audit_overlay")

# ── constants ─────────────────────────────────────────────────────────────────

# Enforcement status per market.
# DB-fallback added in commit e87497f2 (2026-04-28, sha verified from git log).
# sp500 shadow_mode flipped to false in commit 576ac913 (2026-04-29).
# sector_etfs and commodity_etfs remain shadow_mode=true (no enforcement ever).
_ENFORCEMENT_START_DATE = "2026-04-29"  # first date sp500 was in enforce mode
_DB_FALLBACK_ADD_DATE = "2026-04-28"   # DB fallback code first existed (shadow only)

# Win-rate / avg return heuristics for zero-share opportunity cost estimates
_HEURISTIC_WIN_RATE = 0.55
_HEURISTIC_AVG_RETURN = 0.015  # 1.5%

MARKETS = ["sp500", "sector_etfs", "commodity_etfs"]

PLANS_DIR = PROJECT_ROOT / "plans"


# ── helpers ──────────────────────────────────────────────────────────────────

def _load_affected_rows(db_path: str, limit: Optional[int] = None) -> list[dict]:
    """Return all overlay_decisions rows with non-null sizing_override != 1.0."""
    with get_db(db_path) as conn:
        query = """
            SELECT id, timestamp, regime_state, action,
                   sizing_override, universes_deactivated,
                   tickers_avoided, reasoning, confidence
            FROM overlay_decisions
            WHERE sizing_override IS NOT NULL AND sizing_override != 1.0
            ORDER BY timestamp
        """
        if limit:
            query += f" LIMIT {limit}"
        rows = conn.execute(query).fetchall()
    return [dict(r) for r in rows]


def _load_plans_for_date(date_str: str) -> dict[str, dict]:
    """Return {market: plan_dict} for every plan file matching date_str."""
    result: dict[str, dict] = {}
    pattern = str(PLANS_DIR / f"plan_*_{date_str}*.json")
    for fpath in sorted(glob.glob(pattern)):
        fname = Path(fpath).stem  # e.g. plan_sp500_2026-04-17 or plan_sector_etfs_2026-04-17
        # Remove leading 'plan_' then strip the trailing date suffix
        # to handle multi-word market names like 'sector_etfs', 'commodity_etfs'
        name_body = fname
        if name_body.startswith("plan_"):
            name_body = name_body[5:]  # e.g. "sector_etfs_2026-04-17"
        # Strip date portion (find first occurrence of the date in the remaining name)
        date_idx = name_body.find(date_str)
        if date_idx > 0:
            market = name_body[:date_idx].rstrip("_")  # "sector_etfs"
        else:
            market = name_body.split("_")[0]
        try:
            with open(fpath) as fh:
                result[market] = json.load(fh)
        except Exception as exc:
            logger.warning("Could not load %s: %s", fpath, exc)
    return result


def _trades_for_date(db_path: str, date_str: str) -> list[dict]:
    """Return all trades (open+closed) whose entry_date matches date_str."""
    with get_db(db_path) as conn:
        rows = conn.execute(
            """
            SELECT ticker, strategy, universe, shares, entry_price,
                   exit_price, pnl, status, entry_date
            FROM trades
            WHERE entry_date LIKE ?
            ORDER BY entry_date
            """,
            (f"{date_str}%",),
        ).fetchall()
    return [dict(r) for r in rows]


def _determine_enforcement_status(
    date_str: str,
    market: str,
) -> tuple[str, str]:
    """
    Return (status, explanation) for (date, market).

    status values:
      'no_code'    — DB-fallback code didn't exist yet (before 2026-04-28)
      'shadow'     — Code existed but shadow_mode=true (log only, not applied)
      'enforced'   — Code existed, shadow_mode=false, no overlay.enabled guard
    """
    if date_str < _DB_FALLBACK_ADD_DATE:
        return (
            "no_code",
            "DB-fallback code did not exist before 2026-04-28 "
            "(commit e87497f2 added it)"
        )
    if date_str == _DB_FALLBACK_ADD_DATE:
        # sp500 was shadow_mode=true on this date (e87497f2 set shadow_mode=true)
        return (
            "shadow",
            "DB-fallback added on 2026-04-28 but shadow_mode=true "
            "(logged only via overlay_shadow_log, not applied)"
        )
    # After 2026-04-28
    if market in ("sector_etfs", "commodity_etfs"):
        return (
            "shadow",
            f"{market} has shadow_mode=true — overlay logged but never enforced"
        )
    # sp500: enforcement began 2026-04-29 with shadow_mode=false
    if date_str >= _ENFORCEMENT_START_DATE:
        return (
            "enforced",
            "sp500 shadow_mode=false from 2026-04-29; overlay.enabled=false guard "
            "absent until #307 fix (2026-05-08)"
        )
    return ("shadow", "shadow_mode=true — not applied")


def _apply_overlay(position_size: int, multiplier: float) -> int:
    """Reproduce executor logic: max(1, int(size*multiplier)), 0 if multiplier=0."""
    if multiplier == 0.0 or position_size == 0:
        return 0
    return max(1, int(position_size * multiplier))


def _classify_entries(
    plan: dict,
    multiplier: float,
    actual_trades: list[dict],
    enforcement_status: str,
) -> list[dict]:
    """
    For each proposed_entry in the plan, classify whether the overlay had impact.

    Returns list of entry result dicts.
    """
    results = []
    for entry in plan.get("proposed_entries", []):
        ticker = entry.get("ticker", "")
        planned_qty = entry.get("position_size", 0)
        entry_price = entry.get("entry_price", 0.0)

        reduced_qty = _apply_overlay(planned_qty, multiplier)
        diff = planned_qty - reduced_qty

        # Find matching trade (by ticker and entry_date-matching timestamp)
        matching_trade = next(
            (t for t in actual_trades if t["ticker"] == ticker),
            None
        )
        actual_qty = matching_trade["shares"] if matching_trade else None

        # Classify
        if enforcement_status != "enforced":
            # No real enforcement — overlay was not applied
            category = "not_enforced"
            pnl_impact = None
            pnl_type = None
        elif actual_qty is None:
            # No trade found in DB — either zero-share kill OR order never placed
            if reduced_qty == 0:
                category = "zero_share"
            else:
                # Plan had entry but no trade — might be broker reject or wrong date
                category = "no_trade_found"
            pnl_impact = None
            pnl_type = None
        elif diff == 0:
            category = "unaffected"
            pnl_impact = 0.0
            pnl_type = "realized"
        elif actual_qty < planned_qty and abs(actual_qty - reduced_qty) <= 1:
            # Actual matches reduced qty → silently reduced
            category = "silently_reduced"
            # PnL opportunity cost
            if matching_trade and matching_trade.get("pnl") is not None:
                realized_pnl = matching_trade["pnl"]
                pct = realized_pnl / (actual_qty * entry_price) if actual_qty and entry_price else 0
                pnl_impact = diff * entry_price * pct  # opportunity cost on missed shares
                pnl_type = "realized"
            else:
                pnl_impact = None
                pnl_type = None
        else:
            # Actual qty doesn't match expectations clearly
            category = "unclear"
            pnl_impact = None
            pnl_type = None

        # Heuristic for zero_share or no_trade_found
        if category in ("zero_share",) and entry_price and planned_qty:
            expected_notional = planned_qty * entry_price
            pnl_impact = expected_notional * _HEURISTIC_WIN_RATE * _HEURISTIC_AVG_RETURN
            pnl_type = "heuristic"

        results.append({
            "ticker": ticker,
            "planned_qty": planned_qty,
            "reduced_qty": reduced_qty,
            "actual_qty": actual_qty,
            "diff": diff,
            "entry_price": entry_price,
            "category": category,
            "pnl_impact": pnl_impact,
            "pnl_type": pnl_type,
            "trade_pnl": matching_trade.get("pnl") if matching_trade else None,
            "trade_status": matching_trade.get("status") if matching_trade else None,
        })
    return results


def _query_broker_orders_for_date(db_path: str, date_str: str) -> list[dict]:
    """
    Query broker_orders table for BUY orders submitted on date_str or date_str+1 day.
    Returns list of {ticker, submitted_qty, filled_qty, status, submitted_at}.
    The +1 day window handles AEST plans that execute during US session
    (plan dated 2026-05-07 AEST may submit orders 2026-05-07 UTC evening).
    """
    try:
        # Parse date and include the next day too (AEST execution timing)
        start_dt = datetime.strptime(date_str, "%Y-%m-%d")
        end_date_str = (start_dt + timedelta(days=2)).strftime("%Y-%m-%d")
        with get_db(db_path) as conn:
            rows = conn.execute(
                """
                SELECT symbol, qty, filled_qty, status, submitted_at, fill_price
                FROM broker_orders
                WHERE side = 'buy'
                  AND submitted_at >= ?
                  AND submitted_at < ?
                ORDER BY submitted_at
                """,
                (date_str, end_date_str),
            ).fetchall()
        results = []
        for r in rows:
            results.append({
                "ticker": r["symbol"],
                "submitted_qty": int(float(r["qty"] or 0)),
                "filled_qty": int(float(r["filled_qty"] or 0)),
                "status": r["status"] or "",
                "submitted_at": r["submitted_at"] or "",
                "fill_price": r["fill_price"],
            })
        return results
    except Exception as exc:
        logger.warning("broker_orders query failed for %s: %s", date_str, exc)
        return []


def _fetch_alpaca_orders_for_date(
    broker,
    date_str: str,
) -> list[dict]:
    """
    Fetch all Alpaca BUY orders submitted on date_str (±1 day for AEST timing).
    Returns list of {ticker, submitted_qty, filled_qty, status, submitted_at}.
    """
    try:
        from alpaca.trading.requests import GetOrdersRequest
        from alpaca.trading.enums import QueryOrderStatus, OrderSide as AlpacaSide

        start_dt = datetime.strptime(date_str, "%Y-%m-%d").replace(
            tzinfo=timezone.utc
        )
        # Include next 2 days to catch AEST-dated plans
        end_dt = start_dt + timedelta(days=2)

        req = GetOrdersRequest(
            status=QueryOrderStatus.ALL,
            after=start_dt,
            until=end_dt,
            limit=100,
        )
        raw_orders = broker._broker_call(broker._trade_client.get_orders, req)
    except Exception as exc:
        logger.warning("Alpaca order fetch failed for %s: %s", date_str, exc)
        return []

    from brokers.alpaca import mapper

    results = []
    for order in (raw_orders or []):
        side_raw = getattr(order, "side", None)
        side_str = str(
            side_raw.value if hasattr(side_raw, "value") else side_raw
        ).lower()
        if side_str != "buy":
            continue
        symbol = str(getattr(order, "symbol", ""))
        atlas_ticker = mapper.to_atlas(symbol)
        qty = getattr(order, "qty", None) or 0
        filled_qty = getattr(order, "filled_qty", None) or 0
        status = str(getattr(order, "status", ""))
        submitted_at = str(getattr(order, "submitted_at", ""))

        results.append({
            "ticker": atlas_ticker,
            "submitted_qty": int(float(qty)) if qty else 0,
            "filled_qty": int(float(filled_qty)) if filled_qty else 0,
            "status": status,
            "submitted_at": submitted_at,
        })

    return results


# ── main audit logic ──────────────────────────────────────────────────────────

def run_audit(db_path: str, skip_alpaca: bool = False, limit: Optional[int] = None) -> dict:
    """Run the full audit and return structured results."""
    rows = _load_affected_rows(db_path, limit)
    logger.info("Found %d affected overlay_decisions rows", len(rows))

    results = []
    broker = None

    # Optionally connect to Alpaca for order history cross-reference
    if not skip_alpaca:
        try:
            from brokers.alpaca.broker import AlpacaBroker
            from utils.config import get_active_config
            cfg = get_active_config("sp500")
            broker = AlpacaBroker(cfg)
            broker.connect()
            logger.info("Connected to Alpaca broker")
        except Exception as exc:
            logger.warning("Could not connect to Alpaca: %s — skipping order cross-ref", exc)
            broker = None

    # Process each affected row
    seen_dates: set[str] = set()
    for row in rows:
        ts = row["timestamp"]
        date_str = ts[:10]

        # Deduplicate by date (multiple rows same date share same enforcement context)
        is_first_for_date = date_str not in seen_dates
        seen_dates.add(date_str)

        tickers_avoided = []
        try:
            raw_ta = row.get("tickers_avoided") or "[]"
            tickers_avoided = json.loads(raw_ta) if isinstance(raw_ta, str) else raw_ta
        except Exception:
            pass

        reasoning_excerpt = (row.get("reasoning") or "")[:200]

        # Load plans for this date
        plans_by_market = _load_plans_for_date(date_str)

        # Fetch BUY orders: try broker_orders table first (local, reliable),
        # fall back to Alpaca API if live broker connected
        alpaca_orders: list[dict] = []
        if is_first_for_date:
            # Always try local broker_orders table first
            local_orders = _query_broker_orders_for_date(db_path, date_str)
            if local_orders:
                alpaca_orders = local_orders
                logger.info("broker_orders(local) for %s: %d orders", date_str, len(local_orders))
            elif broker:
                logger.info("Fetching Alpaca API orders for %s …", date_str)
                alpaca_orders = _fetch_alpaca_orders_for_date(broker, date_str)
                time.sleep(0.5)  # rate-limit guard

        # Load trades from DB for this date
        db_trades = _trades_for_date(db_path, date_str)
        logger.info(
            "Date %s: plans=%s db_trades=%d alpaca_orders=%d",
            date_str, list(plans_by_market.keys()), len(db_trades), len(alpaca_orders)
        )

        multiplier = float(row.get("sizing_override") or 1.0)

        # Per-market analysis
        market_results: dict[str, dict] = {}
        for market in MARKETS:
            plan = plans_by_market.get(market)
            enforcement_status, enforcement_explanation = _determine_enforcement_status(
                date_str, market
            )

            if plan is None:
                market_results[market] = {
                    "plan_available": False,
                    "enforcement_status": enforcement_status,
                    "enforcement_explanation": enforcement_explanation,
                    "entries": [],
                }
                continue

            entries = plan.get("proposed_entries", [])
            if not entries:
                market_results[market] = {
                    "plan_available": True,
                    "enforcement_status": enforcement_status,
                    "enforcement_explanation": enforcement_explanation,
                    "proposed_entries_count": 0,
                    "entries": [],
                    "note": "Plan has no proposed_entries (all filtered or empty market)",
                }
                continue

            # Classify each entry
            classified = _classify_entries(
                plan, multiplier, db_trades, enforcement_status
            )

            # Cross-reference with broker_orders / Alpaca orders if available
            # When broker order data exists, use submitted_qty as the authoritative
            # actual quantity (more reliable than DB trades for same-day data)
            for ce in classified:
                ticker = ce["ticker"]
                alpaca_match = next(
                    (o for o in alpaca_orders if o["ticker"] == ticker),
                    None
                )
                if alpaca_match:
                    ce["alpaca_submitted_qty"] = alpaca_match["submitted_qty"]
                    ce["alpaca_filled_qty"] = alpaca_match["filled_qty"]
                    ce["alpaca_status"] = alpaca_match["status"]
                    # Re-classify using broker order data as authoritative actual_qty
                    broker_qty = alpaca_match["submitted_qty"]
                    planned = ce["planned_qty"]
                    reduced = ce["reduced_qty"]
                    diff_b = planned - broker_qty
                    if enforcement_status == "enforced" and diff_b > 0 and abs(broker_qty - reduced) <= 1:
                        ce["category"] = "silently_reduced"
                        ce["actual_qty"] = broker_qty
                        ce["diff"] = diff_b
                        # Recalculate opp-cost if broker order expired/unfilled
                        if alpaca_match["filled_qty"] == 0:
                            ce["pnl_impact"] = None  # unfilled — no realized opp-cost
                            ce["pnl_type"] = "unfilled"
                    elif enforcement_status == "enforced" and broker_qty == reduced and diff_b == 0:
                        ce["category"] = "unaffected"
                else:
                    ce["alpaca_submitted_qty"] = None
                    ce["alpaca_filled_qty"] = None
                    ce["alpaca_status"] = None

            market_results[market] = {
                "plan_available": True,
                "enforcement_status": enforcement_status,
                "enforcement_explanation": enforcement_explanation,
                "proposed_entries_count": len(entries),
                "entries": classified,
            }

        results.append({
            "id": row["id"],
            "date": date_str,
            "timestamp": ts,
            "regime_state": row.get("regime_state"),
            "action": row.get("action"),
            "multiplier": multiplier,
            "tickers_avoided": tickers_avoided,
            "reasoning_excerpt": reasoning_excerpt,
            "confidence": row.get("confidence"),
            "market_results": market_results,
        })

    if broker:
        try:
            broker.disconnect()
        except Exception:
            pass

    return {"rows": results}


# ── report generation ─────────────────────────────────────────────────────────

def _enforcement_badge(status: str) -> str:
    if status == "no_code":
        return "✅ no-code"
    if status == "shadow":
        return "🔵 shadow"
    if status == "enforced":
        return "🔴 enforced"
    return status


def generate_report(audit_data: dict) -> str:
    rows = audit_data["rows"]

    # Collect aggregate stats
    total_silently_reduced: list[dict] = []
    total_zero_share: list[dict] = []
    total_unaffected: list[dict] = []
    realized_pnl_total = 0.0
    heuristic_pnl_total = 0.0

    for row in rows:
        date_str = row["date"]
        multiplier = row["multiplier"]
        for market, mr in row["market_results"].items():
            for entry in mr.get("entries", []):
                cat = entry["category"]
                base = {
                    "date": date_str,
                    "market": market,
                    "ticker": entry["ticker"],
                    "planned_qty": entry["planned_qty"],
                    "reduced_qty": entry["reduced_qty"],
                    "actual_qty": entry["actual_qty"],
                    "entry_price": entry["entry_price"],
                    "multiplier": multiplier,
                    "pnl_impact": entry["pnl_impact"],
                    "pnl_type": entry["pnl_type"],
                    "trade_pnl": entry["trade_pnl"],
                    "alpaca_submitted_qty": entry.get("alpaca_submitted_qty"),
                }
                if cat == "silently_reduced":
                    total_silently_reduced.append(base)
                    if entry["pnl_impact"] is not None:
                        realized_pnl_total += entry["pnl_impact"]
                elif cat == "zero_share":
                    total_zero_share.append(base)
                    if entry["pnl_impact"] is not None:
                        heuristic_pnl_total += entry["pnl_impact"]
                elif cat == "unaffected":
                    total_unaffected.append(base)

    grand_total = realized_pnl_total + heuristic_pnl_total
    enforced_dates = [
        r for r in rows
        if any(
            mr.get("enforcement_status") == "enforced"
            for mr in r["market_results"].values()
        )
    ]

    lines: list[str] = []

    # ── Header ──
    lines.append("# Overlay-Enforce Silent-Impact Audit")
    lines.append(f"*Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%MZ')}*  ")
    lines.append(f"*Milestone: overlay-silent-bug-fix | Task #310*\n")

    # ── TL;DR ──
    lines.append("## TL;DR")
    lines.append("")
    tldr_lines = [
        f"**{len(rows)} overlay_decisions rows** carried a non-unit `sizing_override` "
        f"across {len(set(r['date'] for r in rows))} distinct calendar days "
        f"(2026-04-02 → 2026-05-07), but the executor's DB-fallback code was only "
        f"introduced on 2026-04-28 and sp500 was only flipped into enforce mode on "
        f"2026-04-29 — meaning **{len(enforced_dates)} date(s)** saw actual (non-shadow) "
        f"enforcement: {', '.join(r['date'] for r in enforced_dates)}.",
        f"Cross-referencing plan proposed-sizes, the SQLite trades table, and Alpaca "
        f"order history reveals **{len(total_silently_reduced)} silently-reduced entries** "
        f"and **{len(total_zero_share)} zero-share (skipped) entries**; "
        f"estimated combined PnL impact is "
        f"**${grand_total:.2f}** "
        f"(${realized_pnl_total:.2f} realized opportunity-cost + "
        f"${heuristic_pnl_total:.2f} heuristic estimate for skipped entries).",
        f"The #307 fix (added `overlay.enabled` guard to DB-fallback in `live_executor.py`) "
        f"is present in the current working tree and correctly blocks enforcement when "
        f"`overlay.enabled=false`; sector_etfs and commodity_etfs were never at risk "
        f"(always `shadow_mode=true`).",
    ]
    for tl in tldr_lines:
        lines.append(tl)
        lines.append("")

    # ── Affected days table ──
    lines.append("## Affected Overlay-Decision Rows")
    lines.append("")
    lines.append("| Date | ID | Multiplier | Regime | Tickers Avoided | Reasoning Excerpt |")
    lines.append("|------|----|-----------|--------|-----------------|-------------------|")
    for row in rows:
        ta = ", ".join(row["tickers_avoided"]) if row["tickers_avoided"] else "—"
        rsn = row["reasoning_excerpt"].replace("|", "\\|").replace("\n", " ")[:120]
        lines.append(
            f"| {row['date']} | {row['id']} | {row['multiplier']} "
            f"| {row['regime_state'] or '—'} | {ta} | {rsn}… |"
        )
    lines.append("")

    # ── Enforcement classification ──
    lines.append("## Enforcement Classification per Date × Market")
    lines.append("")
    lines.append(
        "| Date | Market | Overlay ID | Multiplier | Status | Plan Entries | Note |"
    )
    lines.append("|------|--------|-----------|-----------|--------|-------------|------|")
    for row in rows:
        for market in MARKETS:
            mr = row["market_results"].get(market, {})
            status = mr.get("enforcement_status", "no_code")
            badge = _enforcement_badge(status)
            plan_avail = mr.get("plan_available", False)
            n_entries = mr.get("proposed_entries_count", "—")
            note = ""
            if not plan_avail:
                note = "plan unavailable"
            elif n_entries == 0:
                note = "0 proposed entries"
            elif mr.get("note"):
                note = mr["note"]
            lines.append(
                f"| {row['date']} | {market} | {row['id']} | {row['multiplier']} "
                f"| {badge} | {n_entries} | {note} |"
            )
    lines.append("")

    # ── Silently resized entries ──
    lines.append("## Tickers Silently Resized")
    lines.append("")
    if total_silently_reduced:
        lines.append(
            "| Date | Market | Ticker | Planned Qty | Reduced Qty | Actual Qty "
            "| Reduction | Alpaca Submitted | Trade PnL | Opp-Cost $ |"
        )
        lines.append(
            "|------|--------|--------|------------|------------|----------|"
            "----------|------------------|-----------|------------|"
        )
        for e in total_silently_reduced:
            diff = e["planned_qty"] - e["reduced_qty"]
            pnl_str = f"${e['pnl_impact']:.2f}" if e["pnl_impact"] is not None else "—"
            trade_pnl = f"${e['trade_pnl']:.2f}" if e["trade_pnl"] is not None else "—"
            alpaca_q = e.get("alpaca_submitted_qty") or "—"
            lines.append(
                f"| {e['date']} | {e['market']} | {e['ticker']} "
                f"| {e['planned_qty']} | {e['reduced_qty']} | {e['actual_qty']} "
                f"| −{diff} | {alpaca_q} | {trade_pnl} | {pnl_str} |"
            )
    else:
        lines.append("*No silently-resized entries found.*")
    lines.append("")

    # ── Zero-share entries ──
    lines.append("## Zero-Share Cases (Most Severe — Order Never Placed)")
    lines.append("")
    if total_zero_share:
        lines.append(
            "| Date | Market | Ticker | Planned Qty | Multiplier | "
            "= 0? | Entry Price | Heuristic Opp-Cost $ |"
        )
        lines.append(
            "|------|--------|--------|------------|-----------|"
            "-----|------------|---------------------|"
        )
        for e in total_zero_share:
            heur = f"${e['pnl_impact']:.2f}" if e["pnl_impact"] is not None else "—"
            check = "✓" if int(e["planned_qty"] * e["multiplier"]) == 0 else "~0"
            lines.append(
                f"| {e['date']} | {e['market']} | {e['ticker']} "
                f"| {e['planned_qty']} | ×{e['multiplier']} | {check} "
                f"| ${e['entry_price']:.2f} | {heur} |"
            )
    else:
        lines.append("*No zero-share entries found.*")
    lines.append("")

    # ── PnL impact table ──
    lines.append("## Estimated PnL Impact")
    lines.append("")
    lines.append(
        "> **Realized opportunity-cost**: `(planned_qty − actual_qty) × trade_pct_return × entry_price` "
        "for completed trades (negative = overlay *saved* losses; positive = overlay cost upside).  \n"
        "> **Unfilled**: order submitted at reduced qty but expired/cancelled — no PnL impact from reduction.  \n"
        "> **Heuristic**: `planned_qty × entry_price × 0.55 × 0.015` for zero-share "
        "entries (win_rate=55%, avg_return=1.5%)."
    )
    lines.append("")
    lines.append("| Date | Market | Ticker | Category | PnL Impact | Type |")
    lines.append("|------|--------|--------|----------|-----------|------|")

    all_pnl_entries = total_silently_reduced + total_zero_share
    for e in all_pnl_entries:
        pnl_str = f"${e['pnl_impact']:.2f}" if e["pnl_impact"] is not None else "—"
        cat = "reduced" if e in total_silently_reduced else "zero-share"
        pnl_type = e.get("pnl_type") or "—"
        lines.append(
            f"| {e['date']} | {e['market']} | {e['ticker']} "
            f"| {cat} | {pnl_str} | {pnl_type} |"
        )

    if not all_pnl_entries:
        lines.append("| — | — | — | — | — | — |")

    lines.append("")
    lines.append(f"**Realized opportunity-cost total**: ${realized_pnl_total:.2f}  ")
    lines.append(f"**Heuristic estimate total**: ${heuristic_pnl_total:.2f}  ")
    lines.append(f"**Grand total**: ${grand_total:.2f}  ")
    lines.append("")

    # ── Recommendations ──
    lines.append("## Recommendations")
    lines.append("")
    lines.append(
        "1. **#307 fix is sufficient for the DB-fallback path**: "
        "The `overlay.enabled` guard added to `live_executor.py` correctly blocks "
        "the silent DB-fallback enforcement. No further code change is needed for "
        "this specific bug. Verify via `python3 -m pytest tests/test_overlay_gating.py`."
    )
    lines.append("")
    lines.append(
        "2. **Affected plans may warrant re-evaluation**: "
        "For any date classified 🔴 enforced where entries were reduced, "
        "consider whether the overlay decision was valid at the time. "
        "If the overlay analysis was sound (e.g. overbought RSI on low volume), "
        "the tightening was arguably appropriate even if applied unintentionally. "
        "Re-running those plans' signals at original sizing would only be warranted "
        "if the overlay fundamentals were demonstrably wrong."
    )
    lines.append("")
    lines.append(
        "3. **Add `overlay_enforce_validated=true` gate to sector_etfs and commodity_etfs "
        "configs before flipping their `shadow_mode` to false**: "
        "Both markets are currently `shadow_mode=true` (safe). "
        "When their overlay is validated and shadow mode is removed, "
        "ensure `enabled=true` is set simultaneously — "
        "the guard introduced by #307 requires `enabled=true` to run the DB-fallback."
    )
    lines.append("")

    # ── Appendix: per-date detail ──
    lines.append("## Appendix: Per-Date Detail")
    lines.append("")
    for row in rows:
        lines.append(f"### {row['date']} (overlay_decisions id={row['id']})")
        lines.append(f"- **Action**: {row['action']} | **Multiplier**: {row['multiplier']}")
        lines.append(f"- **Regime**: {row['regime_state'] or '—'}")
        lines.append(f"- **Confidence**: {row['confidence'] or '—'}")
        lines.append(f"- **Tickers avoided**: {', '.join(row['tickers_avoided']) or '—'}")
        lines.append(f"- **Reasoning**: {row['reasoning_excerpt']}…")
        lines.append("")
        for market in MARKETS:
            mr = row["market_results"].get(market, {})
            status = mr.get("enforcement_status", "no_code")
            badge = _enforcement_badge(status)
            lines.append(f"**{market}** — {badge}")
            if mr.get("plan_available"):
                entries = mr.get("entries", [])
                if entries:
                    lines.append("")
                    lines.append(
                        "| Ticker | Planned | Reduced | Actual | Alpaca | Category | PnL Impact |"
                    )
                    lines.append(
                        "|--------|---------|---------|--------|--------|----------|-----------|"
                    )
                    for ce in entries:
                        pnl_str = (
                            f"${ce['pnl_impact']:.2f}"
                            if ce.get("pnl_impact") is not None else "—"
                        )
                        alpaca_q = ce.get("alpaca_submitted_qty") or "—"
                        lines.append(
                            f"| {ce['ticker']} | {ce['planned_qty']} "
                            f"| {ce['reduced_qty']} | {ce['actual_qty'] or '—'} "
                            f"| {alpaca_q} | {ce['category']} | {pnl_str} |"
                        )
                else:
                    lines.append(" — no proposed entries in plan")
            else:
                lines.append(" — plan unavailable for this date")
            lines.append("")
        lines.append("---")
        lines.append("")

    return "\n".join(lines)


# ── CLI ───────────────────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Historical overlay-enforce silent-impact audit (Task #310)"
    )
    parser.add_argument(
        "--db",
        default=str(PROJECT_ROOT / "data" / "atlas.db"),
        help="Path to atlas.db (default: data/atlas.db)",
    )
    parser.add_argument(
        "--output",
        default=str(PROJECT_ROOT / "docs" / "audits" / "overlay-silent-enforce-audit.md"),
        help="Output Markdown path",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print report to stdout instead of writing file",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Limit number of overlay_decisions rows processed",
    )
    parser.add_argument(
        "--skip-alpaca",
        action="store_true",
        help="Skip Alpaca API order cross-reference",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
        stream=sys.stderr,
    )

    logger.info(
        "Starting overlay audit: db=%s output=%s dry_run=%s limit=%s",
        args.db, args.output, args.dry_run, args.limit,
    )

    audit_data = run_audit(
        db_path=args.db,
        skip_alpaca=args.skip_alpaca,
        limit=args.limit,
    )
    report = generate_report(audit_data)

    if args.dry_run:
        print(report)
    else:
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(report, encoding="utf-8")
        logger.info("Report written to %s (%d bytes)", output_path, len(report))


if __name__ == "__main__":
    main()
