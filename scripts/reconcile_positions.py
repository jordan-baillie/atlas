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
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from atlas_bootstrap import PROJECT_ROOT as PROJECT

from utils.logging_config import setup_logging  # noqa: E402

logger = logging.getLogger("atlas.reconcile_positions")


def _health_log(level, message, detail=None):
    """Write to system_log table. Non-fatal."""
    try:
        from monitor.health_writer import log_error, log_warning, log_info
        fn = {"error": log_error, "warning": log_warning}.get(level, log_info)
        fn("reconcile_positions", message, detail)
    except Exception:
        pass


# Markets supported
_MARKETS = ("asx", "sp500", "commodity_etfs")
_DEFAULT_BROKER = {
    "sp500": "alpaca",
    "commodity_etfs": "alpaca",
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

        # Load universe tickers for this market
        universe_tickers: set = set()
        try:
            from universe.builder import get_universe_tickers
            universe_tickers = set(get_universe_tickers(market_id))
        except Exception as _u_exc:
            logger.warning("Could not load universe tickers for %s: %s", market_id, _u_exc)

        # Load state-file tickers (already loaded into internal_map above — reuse)
        state_tickers = set(internal_map.keys())

        # Load tickers tracked by OTHER markets — positions managed elsewhere should
        # not be flagged as UNTRACKED for this market even if they are in the universe
        # (e.g. FCX is in commodity_etfs universe but managed under sp500).
        other_market_tickers: set = set()
        for _other_market in _MARKETS:
            if _other_market == market_id:
                continue
            _other_path = PROJECT / "brokers" / "state" / f"live_{_other_market}.json"
            if _other_path.exists():
                try:
                    import json as _json
                    _other_state = _json.loads(_other_path.read_text())
                    for _op in _other_state.get("positions", []):
                        other_market_tickers.add(_op["ticker"])
                except Exception as _om_exc:
                    logger.debug("Could not load other-market state %s: %s", _other_market, _om_exc)

        # Accept a broker position if EITHER: it's in the universe OR it's in this
        # market's state file. This catches tickers held by the market but outside
        # the universe definition (e.g. sector ETFs tracked in live_<market>.json).
        # BUT: exclude tickers actively managed by another market (avoids cross-market UNTRACKED noise).
        if universe_tickers or state_tickers:
            _allow = (universe_tickers - other_market_tickers) | state_tickers
            broker_map = {p.ticker: p for p in broker_positions if p.ticker in _allow}
            _skipped = len(broker_positions) - len(broker_map)
            if _skipped:
                logger.info(
                    "Filtered broker positions for %s: %d in-scope (universe=%d, state_file=%d, other_market_exclusions=%d), %d skipped",
                    market_id, len(broker_map), len(universe_tickers), len(state_tickers), len(other_market_tickers), _skipped,
                )
        else:
            broker_map = {p.ticker: p for p in broker_positions}
            logger.warning(
                "Could not load universe OR state tickers for %s — using ALL broker positions",
                market_id,
            )

        result["summary"]["broker_count"] = len(broker_map)  # report in-scope count, not raw broker count

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
            
            # Build corrected positions list from in-scope broker positions only.
            # Use broker_map (already filtered by universe∪state) to avoid pulling
            # cross-market positions (e.g. commodity ETFs) into this market's state.
            corrected_positions = []
            for bp in broker_map.values():
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

            # Dual-write to SQLite: upsert each corrected position with 3 guards:
            #   1. Dedup guard: check across ALL universes by ticker (not just current market)
            #      to prevent cross-market ghost rows; UPDATE stop_price + upgrade strategy
            #      if existing row has a poison strategy.
            #   2. No-unknown guard: never write strategy='unknown'; use 'reconciled' fallback.
            #   3. No-zero-stop guard: if stop_price<=0, skip INSERT and log WARNING — a row
            #      with stop_price=0 is a ghost that breaks downstream risk checks.
            # Failures are logged but non-fatal — JSON state file is source of truth.
            try:
                from db import atlas_db
                _tickers_in_scope = tuple(cp["ticker"] for cp in corrected_positions)
                with atlas_db.get_db() as _db:
                    if _tickers_in_scope:
                        _ph = ",".join("?" * len(_tickers_in_scope))
                        _open_rows = {
                            row["ticker"]: {"id": row["id"], "strategy": row["strategy"],
                                            "stop_price": row["stop_price"]}
                            for row in _db.execute(
                                f"SELECT id, ticker, strategy, stop_price FROM trades "
                                f"WHERE status='open' AND ticker IN ({_ph})",
                                _tickers_in_scope,
                            ).fetchall()
                        }
                    else:
                        _open_rows = {}

                for cp in corrected_positions:
                    _ticker = cp["ticker"]

                    # Guard 2: resolve strategy — never write 'unknown'
                    _strategy = cp.get("strategy") or None
                    if not _strategy or _strategy == "unknown":
                        _strategy = "reconciled"

                    # Guard 3: skip if stop_price is zero or missing
                    _stop_price = float(cp.get("stop_price") or 0)
                    if _stop_price <= 0:
                        logger.warning(
                            "reconcile_positions: skipping SQLite dual-write for %s — "
                            "stop_price=0 (no reliable stop data). "
                            "Resolve via sync_protective_orders.",
                            _ticker,
                        )
                        continue

                    existing = _open_rows.get(_ticker)
                    if existing:
                        # Guard 1: dedup — UPDATE instead of INSERT
                        _ex_id = existing["id"]
                        _ex_strategy = existing["strategy"]
                        _updates: list[str] = []
                        _params: list = []
                        # Update stop_price if we have a better value
                        if _stop_price > 0 and _stop_price != float(existing.get("stop_price") or 0):
                            _updates.append("stop_price = ?")
                            _params.append(_stop_price)
                        # Upgrade strategy if existing is poison
                        if _ex_strategy in ("unknown", "reconciled", "") and _strategy not in ("unknown", "reconciled", ""):
                            _updates.append("strategy = ?")
                            _params.append(_strategy)
                            _updates.append("entry_price = ?")
                            _params.append(float(cp.get("entry_price") or 0))
                        if _updates:
                            _params.append(_ex_id)
                            try:
                                with atlas_db.get_db() as _db:
                                    _db.execute(
                                        f"UPDATE trades SET {', '.join(_updates)} WHERE id = ?",
                                        _params,
                                    )
                                logger.info(
                                    "reconcile_positions: dedup_guard updated id=%d %s fields=%s",
                                    _ex_id, _ticker, _updates,
                                )
                            except Exception as _upd_exc:
                                logger.error(
                                    "reconcile_positions: UPDATE failed for %s id=%d: %s",
                                    _ticker, _ex_id, _upd_exc, exc_info=True,
                                )
                        else:
                            logger.debug(
                                "reconcile_positions: dedup_guard: no-op for %s id=%d",
                                _ticker, _ex_id,
                            )
                    else:
                        # INSERT new row
                        try:
                            atlas_db.record_trade_entry(
                                ticker=_ticker,
                                strategy=_strategy,
                                universe=market_id,
                                entry_price=float(cp.get("entry_price") or 0),
                                shares=int(cp.get("shares") or 0),
                                stop_price=_stop_price,
                                take_profit=None,
                                confidence=0.0,
                                regime_state=None,
                                direction="long",
                            )
                            logger.info(
                                "reconcile_positions: SQLite dual-write inserted %s/%s",
                                _ticker, _strategy,
                            )
                        except Exception as _db_exc:
                            logger.error(
                                "reconcile_positions: SQLite dual-write FAILED for %s: %s",
                                _ticker, _db_exc, exc_info=True,
                            )
            except Exception as _dw_exc:
                logger.error(
                    "reconcile_positions: SQLite dual-write block failed: %s",
                    _dw_exc, exc_info=True,
                )

            result["fixed"] = True
            logger.info("Internal state corrected with %d positions from broker", len(corrected_positions))

        elif fix and dry_run:
            logger.info("DRY RUN: would fix %d discrepancies", len(result["discrepancies"]))

        _health_log("info", "Reconciliation completed", {
            "market": market_id,
            "discrepancies": len(result.get("discrepancies", [])),
            "fixed": result.get("fixed", False),
        })

    except Exception as e:
        result["error"] = str(e)
        logger.error("Reconciliation error: %s", e, exc_info=True)
        _health_log("error", f"Reconciliation error: {e}", {"market": market_id})

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
        _health_log("critical", f"reconcile_positions CRASHED: {exc}")
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
