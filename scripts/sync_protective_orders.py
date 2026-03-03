#!/usr/bin/env python3
"""Sync protective orders (SL + TP) for all live positions.

Standalone script that can be called from cron or manually after trade
execution to ensure every open position has a stop-loss and (if available)
a take-profit order on the broker.

Safe to run multiple times — idempotent.  Existing matching orders are
detected and skipped; only missing orders are placed.

## What it does
1. Connects to the broker for each requested market
2. Loads live positions from the broker
3. Loads today's trade plan (for stop_price / take_profit lookups)
4. Checks existing open orders for each position
5. Places missing SL/TP orders
6. Sends a Telegram summary of what was placed / skipped / errored

## Usage
    python scripts/sync_protective_orders.py [options]

    Options:
      --market {asx,sp500,hk,all}   Market to sync (default: all)
      --dry-run                     Log intent but do NOT send orders
      --no-telegram                 Suppress Telegram notification
      --date YYYY-MM-DD             Trade date override (default: today)
      --config PATH                 Config file path (default: auto-detect)
      -v, --verbose                 Enable DEBUG logging

## Output format
Exit code 0 = success (orders placed or already exist)
Exit code 1 = at least one error (order placement failed)
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import date, datetime
from pathlib import Path

# ── Project root on path ─────────────────────────────────────
PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

from utils.logging_config import setup_logging  # noqa: E402

logger = logging.getLogger("atlas.sync_protective_orders")

# Markets supported by this script
_MARKETS = ("asx", "sp500", "hk")
# Default broker per market (overridden by config)
_DEFAULT_BROKER: dict[str, str] = {
    "asx": "moomoo",
    "sp500": "ibkr",
    "hk": "ibkr",
}


# ═══════════════════════════════════════════════════════════════
# Config loading
# ═══════════════════════════════════════════════════════════════

def load_config(market_id: str, config_path: str = "") -> dict:
    """Load Atlas config for the given market."""
    if config_path:
        path = Path(config_path)
    else:
        path = PROJECT / "config" / "active" / f"{market_id}.json"
    if not path.exists():
        raise FileNotFoundError(f"Config not found: {path}")
    with open(path) as f:
        return json.load(f)


# ═══════════════════════════════════════════════════════════════
# Plan loading
# ═══════════════════════════════════════════════════════════════

def load_plan(market_id: str, trade_date: str) -> dict | None:
    """Load today's approved trade plan for a market."""
    plans_dir = PROJECT / "plans"
    candidates = [
        plans_dir / f"plan_{market_id}_{trade_date}.json",
        plans_dir / f"plan_{trade_date}.json",
    ]
    for path in candidates:
        if path.exists():
            with open(path) as f:
                plan = json.load(f)
            logger.info("Loaded plan: %s (status=%s)", path.name, plan.get("status"))
            return plan
    logger.info("No plan file found for %s %s — will use position data only", market_id, trade_date)
    return None


# ═══════════════════════════════════════════════════════════════
# Per-market sync
# ═══════════════════════════════════════════════════════════════

def sync_market(
    market_id: str,
    trade_date: str,
    *,
    dry_run: bool = False,
    config_path: str = "",
) -> dict:
    """Sync protective orders for one market.

    Returns a result dict with:
      - market_id, trade_date, dry_run
      - counts: positions_checked, sl_placed, tp_placed, ...
      - results: per-ticker breakdown
      - error: error string if connection failed
    """
    result: dict = {
        "market_id": market_id,
        "trade_date": trade_date,
        "dry_run": dry_run,
        "counts": {},
        "results": {},
        "error": "",
    }

    # ── Load config ──────────────────────────────────────────
    try:
        config = load_config(market_id, config_path)
    except FileNotFoundError as e:
        result["error"] = str(e)
        logger.error("Config load failed for %s: %s", market_id, e)
        return result

    # ── Determine broker ─────────────────────────────────────
    broker_name = config.get("trading", {}).get("broker", _DEFAULT_BROKER.get(market_id, "ibkr"))
    live_enabled = config.get("trading", {}).get("live_enabled", False)

    if not live_enabled:
        result["error"] = f"live_enabled=False in config — skipping {market_id}"
        logger.info("Skipping %s: live trading not enabled", market_id)
        return result

    logger.info(
        "Syncing %s via %s broker (dry_run=%s)",
        market_id.upper(), broker_name, dry_run,
    )

    # ── Load plan ────────────────────────────────────────────
    plan = load_plan(market_id, trade_date)

    # ── Connect to broker ────────────────────────────────────
    broker = None
    try:
        if broker_name == "moomoo":
            from brokers.moomoo.broker import MomooBroker
            broker = MomooBroker(config, live=True)
        elif broker_name == "ibkr":
            from brokers.ibkr.broker import IBKRBroker
            broker = IBKRBroker(config, live=True)
        else:
            result["error"] = f"Unknown broker: {broker_name}"
            logger.error("Unknown broker '%s' for %s", broker_name, market_id)
            return result

        if not broker.connect():
            result["error"] = f"Broker connect failed ({broker_name})"
            logger.error("Broker connect failed for %s", market_id)
            return result

        # ── Fetch live positions from broker ─────────────────
        from brokers.live_portfolio import LivePortfolio
        portfolio = LivePortfolio(config, market_id=market_id)
        portfolio._broker = broker
        portfolio._connected = True
        portfolio._refresh_from_broker()

        if not portfolio.positions:
            logger.info("No live positions in %s — nothing to protect", market_id)
            result["counts"] = {"positions_checked": 0}
            return result

        logger.info("%d live positions in %s", len(portfolio.positions), market_id)

        # ── Fetch open orders once ────────────────────────────
        open_orders = broker.get_open_orders()
        logger.info("%d open orders fetched from broker", len(open_orders))

        # ── Sync protective orders ────────────────────────────
        from brokers.moomoo.protective_orders import sync_protective_orders

        # For IBKR broker, use IBKR's sync if available, otherwise fall back to moomoo module
        if broker_name == "ibkr" and hasattr(broker, "sync_all_protective_orders"):
            sync_result = broker.sync_all_protective_orders(
                positions=portfolio.positions,
                plan=plan,
                trade_date=trade_date,
                dry_run=dry_run,
            )
        elif broker_name == "moomoo" and hasattr(broker, "sync_all_protective_orders"):
            sync_result = broker.sync_all_protective_orders(
                positions=portfolio.positions,
                plan=plan,
                trade_date=trade_date,
                dry_run=dry_run,
            )
        else:
            # Generic fallback using moomoo protective_orders module
            sync_result = sync_protective_orders(
                broker=broker,
                positions=portfolio.positions,
                open_orders=open_orders,
                plan=plan,
                config=config,
                trade_date=trade_date,
                dry_run=dry_run,
            )

        result["counts"] = sync_result.get("counts", {})
        result["results"] = sync_result.get("results", {})

        # Log per-ticker summary
        for ticker, tresult in result["results"].items():
            logger.info("  %s", tresult.get("summary", ticker))

    except Exception as e:
        result["error"] = str(e)
        logger.error("Error syncing %s: %s", market_id, e, exc_info=True)

    finally:
        if broker:
            try:
                broker.disconnect()
            except Exception:
                pass

    return result


# ═══════════════════════════════════════════════════════════════
# Telegram summary
# ═══════════════════════════════════════════════════════════════

def format_telegram_message(
    market_results: list[dict],
    trade_date: str,
    dry_run: bool,
) -> str:
    """Format Telegram HTML message summarising the sync run."""
    prefix = "🔵 [DRY RUN] " if dry_run else "🟢 "
    lines = [
        f"{prefix}<b>Protective Orders Sync</b> — {trade_date}",
        "",
    ]

    all_ok = True
    for r in market_results:
        market = r["market_id"].upper()
        error = r.get("error", "")

        if error:
            lines.append(f"❌ <b>{market}</b>: {error}")
            all_ok = False
            continue

        counts = r.get("counts", {})
        n_checked = counts.get("positions_checked", 0)

        if n_checked == 0:
            lines.append(f"⚪ <b>{market}</b>: no live positions")
            continue

        sl_placed = counts.get("sl_placed", 0)
        tp_placed = counts.get("tp_placed", 0)
        sl_exists = counts.get("sl_already_exists", 0)
        tp_exists = counts.get("tp_already_exists", 0)
        errors = counts.get("errors", 0)
        sl_skip = counts.get("sl_skipped", 0)
        tp_skip = counts.get("tp_skipped", 0)

        icon = "❌" if errors else ("✅" if (sl_placed + tp_placed) > 0 else "ℹ️")
        lines.append(
            f"{icon} <b>{market}</b> ({n_checked} positions)\n"
            f"  SL: {sl_placed} placed | {sl_exists} existed | {sl_skip} skipped\n"
            f"  TP: {tp_placed} placed | {tp_exists} existed | {tp_skip} skipped"
            + (f"\n  ⚠️ {errors} errors" if errors else "")
        )

        # Per-ticker detail
        for ticker, tresult in r.get("results", {}).items():
            errs = tresult.get("errors", [])
            if errs:
                for e in errs:
                    lines.append(f"  └─ {ticker}: ⚠️ {e}")

        lines.append("")

    if all_ok and not any(r.get("error") for r in market_results):
        lines.append("<i>All positions protected ✓</i>")

    lines.append(
        f"\n<i>Run at {datetime.now().strftime('%H:%M:%S')}</i>"
    )
    return "\n".join(lines)


def send_telegram_summary(
    market_results: list[dict],
    trade_date: str,
    dry_run: bool,
) -> bool:
    """Send Telegram summary. Returns True on success."""
    try:
        from utils.telegram import send_message
        msg = format_telegram_message(market_results, trade_date, dry_run)
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
        choices=list(_MARKETS) + ["all"],
        default="all",
        help="Market to sync (default: all)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Log intent but do NOT send orders",
    )
    parser.add_argument(
        "--no-telegram",
        action="store_true",
        help="Suppress Telegram notification",
    )
    parser.add_argument(
        "--date",
        default=str(date.today()),
        help="Trade date override YYYY-MM-DD (default: today)",
    )
    parser.add_argument(
        "--config",
        default="",
        metavar="PATH",
        help="Config file path (default: config/active/{market}.json)",
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
        setup_logging("sync_protective_orders", level=log_level)
    except Exception:
        logging.basicConfig(
            level=log_level,
            format="%(asctime)s %(name)s %(levelname)s %(message)s",
        )

    trade_date = args.date
    dry_run = args.dry_run
    markets = list(_MARKETS) if args.market == "all" else [args.market]

    logger.info(
        "=== sync_protective_orders | date=%s markets=%s dry_run=%s ===",
        trade_date, markets, dry_run,
    )

    if dry_run:
        logger.info("DRY RUN MODE — no orders will be sent")

    # ── Run per market ────────────────────────────────────────
    market_results: list[dict] = []
    any_error = False

    for market_id in markets:
        logger.info("── %s ──────────────────────────────", market_id.upper())
        result = sync_market(
            market_id=market_id,
            trade_date=trade_date,
            dry_run=dry_run,
            config_path=args.config,
        )
        market_results.append(result)
        if result.get("error"):
            any_error = True
        elif result["counts"].get("errors", 0) > 0:
            any_error = True

    # ── Summary to stdout ─────────────────────────────────────
    print()
    print(f"=== Protective Orders Sync Summary — {trade_date} ===")
    if dry_run:
        print("(DRY RUN — no orders sent)")
    print()

    for r in market_results:
        market = r["market_id"].upper()
        error = r.get("error", "")
        if error:
            print(f"  {market}: ERROR — {error}")
            continue
        counts = r.get("counts", {})
        n_checked = counts.get("positions_checked", 0)
        if n_checked == 0:
            print(f"  {market}: no live positions")
            continue
        sl_placed = counts.get("sl_placed", 0)
        tp_placed = counts.get("tp_placed", 0)
        errs = counts.get("errors", 0)
        print(
            f"  {market}: {n_checked} positions checked | "
            f"SL placed={sl_placed} | TP placed={tp_placed} | errors={errs}"
        )
        for ticker, tresult in r.get("results", {}).items():
            print(f"    {tresult.get('summary', ticker)}")

    print()

    # ── Telegram ─────────────────────────────────────────────
    if not args.no_telegram:
        ok = send_telegram_summary(market_results, trade_date, dry_run)
        if ok:
            logger.info("Telegram notification sent")
        else:
            logger.warning("Telegram notification failed (non-fatal)")

    logger.info("=== sync_protective_orders done (errors=%s) ===", any_error)
    return 1 if any_error else 0


if __name__ == "__main__":
    sys.exit(main())
