#!/usr/bin/env python3
"""Position Reconciliation Script for Atlas.

Compares internal position state against the broker and alerts on discrepancies.

Detects:
    PHANTOM: Position in internal state but NOT on broker
    UNTRACKED: Position on broker but NOT in internal state
    MISMATCH: Quantity differs between internal and broker
    DRIFT: Entry price differs by >1%

Safe to run multiple times — read-only by default, only writes when --fix is set.

Usage:
    python scripts/reconcile_positions.py [options]

    Options:
      --market {asx,sp500}       Market to reconcile (default: sp500)
      --quiet                    Only output on discrepancy (for cron)
      --fix                      Auto-correct internal state from broker
      --no-telegram              Suppress Telegram notification
      --dry-run                  Show what --fix would do without writing
      -v, --verbose              Enable DEBUG logging

Exit codes:
    0 = all positions match (or no positions)
    1 = discrepancies found
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

# ── Project root on path ─────────────────────────────────────
PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

from utils.logging_config import setup_logging  # noqa: E402

logger = logging.getLogger("atlas.reconcile_positions")

# Markets supported
_MARKETS = ("asx", "sp500")
_DEFAULT_BROKER = {
    "sp500": "alpaca",
}


# ═══════════════════════════════════════════════════════════════
# Config / State Loading
# ═══════════════════════════════════════════════════════════════

def load_config(market_id: str) -> dict:
    """Load active config for the given market."""
    path = PROJECT / "config" / "active" / f"{market_id}.json"
    if not path.exists():
        raise FileNotFoundError(f"Config not found: {path}")
    with open(path) as f:
        return json.load(f)


def load_internal_state(market_id: str) -> dict:
    """Load internal position state from brokers/state/live_{market}.json."""
    path = PROJECT / "brokers" / "state" / f"live_{market_id}.json"
    if not path.exists():
        logger.warning("Internal state file not found: %s", path)
        return {"positions": []}
    with open(path) as f:
        return json.load(f)


def save_internal_state(market_id: str, state: dict) -> None:
    """Save updated internal state to brokers/state/live_{market}.json."""
    path = PROJECT / "brokers" / "state" / f"live_{market_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    
    # Update last_saved timestamp
    state["last_saved"] = datetime.now().isoformat()
    
    with open(path, "w") as f:
        json.dump(state, f, indent=2)
    logger.info("Internal state saved: %s", path)


# ═══════════════════════════════════════════════════════════════
# Reconciliation Logic
# ═══════════════════════════════════════════════════════════════

def reconcile_positions(
    market_id: str,
    *,
    fix: bool = False,
    dry_run: bool = False,
) -> dict:
    """Reconcile internal positions against broker.

    Returns a result dict with:
      - market_id
      - discrepancies: list of {type, ticker, details}
      - summary: {internal_count, broker_count, phantom, untracked, mismatch, drift}
      - error: error string if connection failed
      - fixed: bool (True if --fix applied corrections)
    """
    result = {
        "market_id": market_id,
        "discrepancies": [],
        "summary": {
            "internal_count": 0,
            "broker_count": 0,
            "phantom": 0,
            "untracked": 0,
            "mismatch": 0,
            "drift": 0,
        },
        "error": "",
        "fixed": False,
    }

    # ── Load config ──────────────────────────────────────────
    try:
        config = load_config(market_id)
    except FileNotFoundError as e:
        result["error"] = str(e)
        logger.error("Config load failed: %s", e)
        return result

    # ── Load internal state ─────────────────────────────────
    internal_state = load_internal_state(market_id)
    internal_positions = internal_state.get("positions", [])
    result["summary"]["internal_count"] = len(internal_positions)

    # Build lookup: ticker -> internal position
    internal_map = {p["ticker"]: p for p in internal_positions}

    # ── Connect to broker ───────────────────────────────────
    broker_name = config.get("trading", {}).get("broker", _DEFAULT_BROKER.get(market_id, "alpaca"))
    logger.info("Reconciling %s via %s broker", market_id.upper(), broker_name)

    broker = None
    try:
        from brokers.registry import get_live_broker
        broker = get_live_broker(config)
        if not broker:
            result["error"] = f"No live broker available for {broker_name}"
            logger.error("get_live_broker returned None")
            return result

        if not broker.connect():
            result["error"] = f"Broker connect failed ({broker_name})"
            logger.error("Broker connect failed")
            return result

        # ── Get broker positions ──────────────────────────────
        broker_positions = broker.get_positions()
        result["summary"]["broker_count"] = len(broker_positions)

        # Build lookup: ticker -> broker position
        broker_map = {p.ticker: p for p in broker_positions}

        # ── Compare: internal vs broker ──────────────────────

        # 1. PHANTOM: in internal but not on broker
        for ticker in internal_map:
            if ticker not in broker_map:
                result["discrepancies"].append({
                    "type": "PHANTOM",
                    "ticker": ticker,
                    "details": (
                        f"Internal shows {internal_map[ticker]['shares']} shares "
                        f"@ ${internal_map[ticker]['entry_price']:.2f}, "
                        f"but broker has NO position"
                    ),
                })
                result["summary"]["phantom"] += 1

        # 2. UNTRACKED: on broker but not in internal
        for ticker in broker_map:
            if ticker not in internal_map:
                bp = broker_map[ticker]
                result["discrepancies"].append({
                    "type": "UNTRACKED",
                    "ticker": ticker,
                    "details": (
                        f"Broker shows {bp.shares} shares @ ${bp.entry_price:.2f}, "
                        f"but internal state has NO record"
                    ),
                })
                result["summary"]["untracked"] += 1

        # 3. MISMATCH / DRIFT: both have it, but values differ
        for ticker in internal_map:
            if ticker not in broker_map:
                continue  # already counted as PHANTOM

            internal_pos = internal_map[ticker]
            broker_pos = broker_map[ticker]

            internal_qty = internal_pos["shares"]
            broker_qty = broker_pos.shares

            internal_entry = internal_pos["entry_price"]
            broker_entry = broker_pos.entry_price

            # Quantity mismatch
            if internal_qty != broker_qty:
                result["discrepancies"].append({
                    "type": "MISMATCH",
                    "ticker": ticker,
                    "details": (
                        f"Quantity differs: internal={internal_qty} vs broker={broker_qty}"
                    ),
                })
                result["summary"]["mismatch"] += 1

            # Entry price drift (>1%)
            if broker_entry > 0:
                price_diff_pct = abs(internal_entry - broker_entry) / broker_entry * 100
                if price_diff_pct > 1.0:
                    result["discrepancies"].append({
                        "type": "DRIFT",
                        "ticker": ticker,
                        "details": (
                            f"Entry price differs by {price_diff_pct:.2f}%: "
                            f"internal=${internal_entry:.2f} vs broker=${broker_entry:.2f}"
                        ),
                    })
                    result["summary"]["drift"] += 1

        # ── Apply fixes if requested ────────────────────────
        if fix and result["discrepancies"] and not dry_run:
            logger.info("Applying fixes to internal state...")
            
            # Build corrected positions list from broker
            corrected_positions = []
            for bp in broker_positions:
                # Preserve strategy/entry_date/stop_price from internal if available
                internal_pos = internal_map.get(bp.ticker, {})
                corrected_positions.append({
                    "ticker": bp.ticker,
                    "strategy": internal_pos.get("strategy", "unknown"),
                    "entry_date": internal_pos.get("entry_date", datetime.now().strftime("%Y-%m-%d")),
                    "entry_price": bp.entry_price,
                    "shares": bp.shares,
                    "stop_price": internal_pos.get("stop_price", bp.entry_price * 0.95),
                    "order_id": internal_pos.get("order_id", ""),
                })
            
            internal_state["positions"] = corrected_positions
            save_internal_state(market_id, internal_state)
            result["fixed"] = True
            logger.info("Internal state corrected with %d positions from broker", len(corrected_positions))

        elif fix and dry_run:
            logger.info("DRY RUN: would fix %d discrepancies", len(result["discrepancies"]))

    except Exception as e:
        result["error"] = str(e)
        logger.error("Reconciliation error: %s", e, exc_info=True)

    finally:
        if broker:
            try:
                broker.disconnect()
            except Exception:
                pass

    return result


# ═══════════════════════════════════════════════════════════════
# Telegram Notification
# ═══════════════════════════════════════════════════════════════

def format_telegram_message(result: dict, fixed: bool) -> str:
    """Format Telegram HTML message for reconciliation results."""
    market = result["market_id"].upper()
    summary = result["summary"]
    discrepancies = result["discrepancies"]

    # Icon based on outcome
    if result.get("error"):
        icon = "❌"
        status = "ERROR"
    elif not discrepancies:
        icon = "✅"
        status = "CLEAN"
    elif fixed:
        icon = "🔧"
        status = "FIXED"
    else:
        icon = "⚠️"
        status = "DISCREPANCY"

    lines = [
        f"{icon} <b>Position Reconciliation — {market}</b>",
        f"Status: <b>{status}</b>",
        "",
        f"Internal: {summary['internal_count']} positions",
        f"Broker: {summary['broker_count']} positions",
        "",
    ]

    if result.get("error"):
        lines.append(f"<b>Error:</b> {result['error']}")
        return "\n".join(lines)

    if not discrepancies:
        lines.append("✓ All positions match")
    else:
        lines.append(f"<b>Issues Found:</b>")
        if summary["phantom"]:
            lines.append(f"  🔴 {summary['phantom']} PHANTOM (internal only)")
        if summary["untracked"]:
            lines.append(f"  🟡 {summary['untracked']} UNTRACKED (broker only)")
        if summary["mismatch"]:
            lines.append(f"  🟠 {summary['mismatch']} MISMATCH (quantity differs)")
        if summary["drift"]:
            lines.append(f"  🔵 {summary['drift']} DRIFT (entry price >1% off)")
        
        lines.append("")
        lines.append("<b>Details:</b>")
        for disc in discrepancies[:10]:  # Limit to first 10 to avoid message overflow
            lines.append(f"  • {disc['type']}: {disc['ticker']}")
            lines.append(f"    {disc['details']}")
        
        if len(discrepancies) > 10:
            lines.append(f"  ... and {len(discrepancies) - 10} more")

        if fixed:
            lines.append("")
            lines.append("✅ <b>Internal state corrected from broker</b>")

    lines.append(f"\n<i>Run at {datetime.now().strftime('%H:%M:%S')}</i>")
    return "\n".join(lines)


def send_telegram_summary(result: dict, fixed: bool) -> bool:
    """Send Telegram notification. Returns True on success."""
    try:
        from utils.telegram import send_message
        msg = format_telegram_message(result, fixed)
        return send_message(msg)
    except Exception as e:
        logger.warning("Telegram send failed: %s", e)
        return False


# ═══════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--market",
        choices=_MARKETS,
        default="sp500",
        help="Market to reconcile (default: sp500)",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Only output on discrepancy (suppress clean results)",
    )
    parser.add_argument(
        "--fix",
        action="store_true",
        help="Auto-correct internal state from broker (writes to brokers/state/)",
    )
    parser.add_argument(
        "--no-telegram",
        action="store_true",
        help="Suppress Telegram notification",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what --fix would do without writing",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Enable DEBUG logging",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    # ── Logging setup ────────────────────────────────────────
    log_level = logging.DEBUG if args.verbose else logging.INFO
    try:
        setup_logging("reconcile_positions", level=log_level)
    except Exception:
        logging.basicConfig(
            level=log_level,
            format="%(asctime)s %(name)s %(levelname)s %(message)s",
        )

    market_id = args.market
    logger.info(
        "=== reconcile_positions | market=%s fix=%s dry_run=%s ===",
        market_id, args.fix, args.dry_run,
    )

    if args.dry_run:
        logger.info("DRY RUN MODE — no state changes will be written")

    # ── Run reconciliation ─────────────────────────────────────
    result = reconcile_positions(
        market_id=market_id,
        fix=args.fix,
        dry_run=args.dry_run,
    )

    # ── Summary to stdout ──────────────────────────────────────
    has_discrepancies = bool(result["discrepancies"])
    has_error = bool(result.get("error"))

    # In quiet mode, only output if there's something wrong
    if not args.quiet or has_discrepancies or has_error:
        print()
        print(f"=== Position Reconciliation — {market_id.upper()} ===")
        if args.dry_run and args.fix:
            print("(DRY RUN — no changes written)")
        print()

        if result.get("error"):
            print(f"ERROR: {result['error']}")
        else:
            summary = result["summary"]
            print(f"Internal positions: {summary['internal_count']}")
            print(f"Broker positions: {summary['broker_count']}")
            print()

            if not has_discrepancies:
                print("✓ All positions match")
            else:
                print(f"⚠️  {len(result['discrepancies'])} discrepancies found:")
                print(f"   PHANTOM (internal only): {summary['phantom']}")
                print(f"   UNTRACKED (broker only): {summary['untracked']}")
                print(f"   MISMATCH (qty differs): {summary['mismatch']}")
                print(f"   DRIFT (entry price >1%): {summary['drift']}")
                print()

                for disc in result["discrepancies"]:
                    print(f"  • {disc['type']}: {disc['ticker']}")
                    print(f"    {disc['details']}")
                print()

                if result["fixed"]:
                    print("✅ Internal state corrected from broker")
                elif args.fix and args.dry_run:
                    print("ℹ️  Run without --dry-run to apply fixes")
                elif not args.fix:
                    print("ℹ️  Run with --fix to auto-correct internal state")

    # ── Telegram notification ──────────────────────────────────
    # Only send if there are discrepancies or errors (skip clean results in quiet mode)
    should_notify = (has_discrepancies or has_error) and not args.no_telegram
    
    if should_notify:
        ok = send_telegram_summary(result, result["fixed"])
        if ok:
            logger.info("Telegram notification sent")
        else:
            logger.warning("Telegram notification failed (non-fatal)")

    logger.info("=== reconcile_positions done (discrepancies=%s) ===", has_discrepancies)
    return 1 if has_discrepancies or has_error else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        # Top-level crash guard — alert via Telegram so cron failures aren't silent
        try:
            from utils.telegram import send_message
            send_message(
                f"🚨 <b>reconcile_positions CRASHED</b>\n\n"
                f"<pre>{type(exc).__name__}: {str(exc)[:500]}</pre>\n\n"
                f"Check logs/reconciliation.log"
            )
        except Exception:
            pass
        raise
