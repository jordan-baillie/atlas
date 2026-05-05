#!/usr/bin/env python3
"""Force-close open positions in consolidated universes (sector_etfs, commodity_etfs).

CONSOLIDATION CLOSURE — manual operator script. Run during US RTH on 2026-05-05
(or later) to force-close positions in markets being shut down. Cancels broker
OCO brackets, submits MARKET SELL, updates DB+state via LivePortfolio.execute_exit.

Lineage: modelled on scripts/tools/archive/close_mrvl_orphan.py (2026-04-12)
which successfully closed an orphaned MRVL position using the same
_cancel_open_orders_for_ticker → MARKET SELL → LivePortfolio.execute_exit pattern.

USAGE:
    # Dry-run (DEFAULT — shows what would happen, no orders placed):
    python3 scripts/consolidation_close_positions.py

    # Live execution (operator must explicitly opt in):
    python3 scripts/consolidation_close_positions.py --live

    # Single market only:
    python3 scripts/consolidation_close_positions.py --market sector_etfs --live

    # Specific tickers only (comma-separated):
    python3 scripts/consolidation_close_positions.py --tickers GLD --live

SAFETY GUARDS:
    - HARD universe whitelist: only commodity_etfs and sector_etfs. Raises
      AssertionError if any other universe is requested. NEVER touches sp500.
    - Default mode is DRY-RUN. Operator must pass --live explicitly.
    - Per-ticker error handling: one failure doesn't abort the others.
    - Verifies position exists at broker before submitting any order.
    - Verifies market is open before --live execution (bypass: --skip-clock-check).
    - Logs every step with timestamps to logs/consolidation_close.log.
"""
from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

# ── Universe guards ───────────────────────────────────────────────────────────
ALLOWED_UNIVERSES: frozenset[str] = frozenset({"commodity_etfs", "sector_etfs"})
FORBIDDEN_UNIVERSES: frozenset[str] = frozenset({
    "sp500", "asx", "crypto", "gold_etfs", "treasury_etfs", "defensive_etfs"
})

STATE_DIR = PROJECT / "brokers" / "state"
logger = logging.getLogger("atlas.consolidation_close")


# ── Result dataclass ──────────────────────────────────────────────────────────

@dataclass
class CloseResult:
    ticker: str
    universe: str
    action: str = "DRY RUN"
    fill_price: float = 0.0
    entry_price: float = 0.0
    shares: int = 0
    pnl: float = 0.0
    status: str = "pending"
    error: str = ""


# ── Helpers ───────────────────────────────────────────────────────────────────

def _broker_is_open(broker) -> bool:
    """Return True if market is currently open.

    Tries broker.get_clock() first (works for test mocks and any broker that
    exposes a public get_clock method). Falls back to the Alpaca internal
    client pattern used in services/api/dashboard.py.
    """
    try:
        if hasattr(broker, "get_clock") and callable(broker.get_clock):
            clock = broker.get_clock()
            if hasattr(clock, "is_open"):
                return bool(clock.is_open)
    except Exception:
        pass
    try:
        clock = broker._broker_call(broker._trade_client.get_clock)
        return bool(clock.is_open)
    except Exception as exc:
        logger.warning("Could not check market clock: %s — assuming closed", exc)
        return False


def _poll_fill(broker, order_id: str, poll_secs: float = 30.0,
               interval: float = 2.0) -> float:
    """Poll broker.get_order_status() until filled; return fill_price or 0."""
    deadline = time.monotonic() + poll_secs
    while time.monotonic() < deadline:
        time.sleep(interval)
        try:
            status = broker.get_order_status(order_id)
            if status.fill_price and status.fill_price > 0:
                return float(status.fill_price)
        except Exception as exc:
            logger.debug("Poll fill status error (will retry): %s", exc)
    return 0.0


def _cancel_protective_orders(broker, ticker: str) -> int:
    """Cancel all open sell-side orders for ticker via LiveExecutor shell.

    Returns count of cancelled orders.
    """
    from brokers.live_executor import LiveExecutor
    _exec = LiveExecutor.__new__(LiveExecutor)
    _exec._broker = broker
    _exec._connected = True
    try:
        return _exec._cancel_open_orders_for_ticker(ticker)
    except Exception as exc:
        logger.warning("Could not cancel protective orders for %s: %s", ticker, exc)
        return 0


def _update_protective_orders_db(market_id: str, ticker: str) -> None:
    """Mark position_protective_orders rows as 'cancelled' for this position.

    Uses direct SQL with status='cancelled' (not 'closed') to reflect that
    the OCO bracket was explicitly cancelled, not filled.
    """
    from db.atlas_db import get_db
    try:
        with get_db() as db:
            db.execute(
                "UPDATE position_protective_orders "
                "SET status = 'cancelled', last_synced_at = ? "
                "WHERE market_id = ? AND ticker = ? AND status = 'active'",
                (
                    datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    market_id,
                    ticker,
                ),
            )
        logger.info("position_protective_orders: marked %s/%s as cancelled", market_id, ticker)
    except Exception as exc:
        logger.warning(
            "Could not update position_protective_orders for %s/%s: %s",
            market_id, ticker, exc,
        )


def _load_state_file(market: str) -> dict:
    """Load and return the JSON state file for the given market."""
    path = STATE_DIR / f"live_{market}.json"
    if not path.exists():
        raise FileNotFoundError(f"State file not found: {path}")
    with open(path) as fh:
        return json.load(fh)


# ── Per-ticker close logic ────────────────────────────────────────────────────

def _close_ticker(
    ticker: str,
    state_pos_dict: dict,
    broker,
    all_state_positions: list[dict],
    config: dict,
    market: str,
    live: bool,
    skip_clock_check: bool,
) -> CloseResult:
    """Attempt to close a single ticker position.

    Returns a CloseResult describing what happened.
    """
    from brokers.base import OrderSide, OrderType
    from brokers.live_portfolio import LivePortfolio
    from brokers.position import Position
    from db import atlas_db

    entry_price = float(state_pos_dict.get("entry_price", 0.0))
    shares = int(state_pos_dict.get("shares", 0))
    strategy = state_pos_dict.get("strategy", "unknown")
    today_str = datetime.now().strftime("%Y-%m-%d")

    result = CloseResult(
        ticker=ticker,
        universe=market,
        entry_price=entry_price,
        shares=shares,
    )

    # ── Step 1: Verify position exists at broker ──────────────────────────────
    try:
        broker_positions = broker.get_positions()
    except Exception as exc:
        result.status = "error"
        result.error = f"get_positions failed: {exc}"
        logger.error("[%s] get_positions() failed: %s", ticker, exc)
        return result

    broker_pos = next((p for p in broker_positions if p.ticker == ticker), None)
    if broker_pos is None:
        logger.warning("[%s] Not held at broker — skipping", ticker)
        result.action = "SKIP (not held)"
        result.status = "skipped"
        return result

    current_price = broker_pos.current_price
    logger.info(
        "[%s] Broker position: %d shares @ current $%.2f (entry $%.2f)",
        ticker, shares, current_price, entry_price,
    )

    # ── Step 2: Market-hours guard (live only) ────────────────────────────────
    if live and not skip_clock_check:
        if not _broker_is_open(broker):
            logger.error("[%s] Market is CLOSED — skipping (use --skip-clock-check to override)", ticker)
            result.action = "SKIP (market closed)"
            result.status = "skipped"
            return result

    # ── Step 3: Cancel OCO protective orders ──────────────────────────────────
    cancelled = _cancel_protective_orders(broker, ticker)
    logger.info("[%s] Cancelled %d protective order(s)", ticker, cancelled)

    if cancelled:
        logger.info("[%s] Sleeping 1.0s to let Alpaca release held shares...", ticker)
        time.sleep(1.0)

    # ── Step 4: Dry-run exit ──────────────────────────────────────────────────
    if not live:
        result.action = "DRY RUN (would MARKET SELL)"
        result.fill_price = current_price
        result.pnl = round((current_price - entry_price) * shares, 2)
        result.status = "dry_run"
        logger.info(
            "[DRY RUN] Would sell %d shares of %s @ ~$%.2f  (est. PnL $%.2f)",
            shares, ticker, current_price, result.pnl,
        )
        return result

    # ── Step 5: Submit MARKET SELL ────────────────────────────────────────────
    logger.info("[%s] Submitting MARKET SELL %d shares...", ticker, shares)
    order_result = broker.place_order(
        ticker=ticker,
        side=OrderSide.SELL,
        qty=shares,
        price=0.0,
        order_type=OrderType.MARKET,
        remark="consolidation_close_2026_05_04",
    )

    if not order_result.success:
        result.status = "error"
        result.error = f"place_order failed: {order_result.message}"
        logger.error("[%s] MARKET SELL failed: %s", ticker, order_result.message)
        return result

    logger.info("[%s] Order submitted: id=%s", ticker, order_result.order_id)

    # ── Step 6: Capture fill price ────────────────────────────────────────────
    fill_price: float = 0.0
    if order_result.fill_price and order_result.fill_price > 0:
        fill_price = float(order_result.fill_price)
        logger.info("[%s] Immediate fill @ $%.4f", ticker, fill_price)
    else:
        logger.info("[%s] No immediate fill_price — polling up to 30s...", ticker)
        fill_price = _poll_fill(broker, order_result.order_id)

    if fill_price <= 0:
        # Fallback: use current broker price
        fill_price = current_price
        logger.warning(
            "[%s] Fill not confirmed in 30s — using current price $%.2f as fallback",
            ticker, fill_price,
        )

    result.fill_price = fill_price
    result.action = "MARKET SELL"

    # ── Step 7: Update SQLite trades table ───────────────────────────────────
    try:
        atlas_db.record_trade_exit(
            ticker=ticker,
            strategy=strategy,
            exit_price=fill_price,
            exit_reason="manual_consolidation_close",
        )
        logger.info("[%s] trades table updated (exit_price=$%.4f)", ticker, fill_price)
    except Exception as exc:
        logger.warning("[%s] trades table update failed (non-fatal): %s", ticker, exc)

    # ── Step 8: Update state file + closed_trades via LivePortfolio ──────────
    try:
        lp = LivePortfolio(config, market_id=market)
        # Populate positions from state file so execute_exit can find the ticker
        lp.positions = [Position.from_dict(p) for p in all_state_positions]
        lp.broker_data_valid = True  # allow save_state() to write
        trade_record = lp.execute_exit(
            ticker=ticker,
            exit_price=fill_price,
            trade_date=today_str,
            exit_type="manual_consolidation_close",
        )
        if trade_record:
            result.pnl = trade_record.get("pnl", 0.0)
            logger.info(
                "[%s] execute_exit done — PnL $%.2f (%.2f%%)",
                ticker,
                trade_record.get("pnl", 0.0),
                trade_record.get("pnl_pct", 0.0),
            )
        else:
            logger.warning("[%s] execute_exit returned None (position not found in LP)", ticker)
            result.pnl = round((fill_price - entry_price) * shares, 2)
    except Exception as exc:
        logger.warning("[%s] LivePortfolio.execute_exit failed (non-fatal): %s", ticker, exc)
        result.pnl = round((fill_price - entry_price) * shares, 2)

    # ── Step 9: Update position_protective_orders ─────────────────────────────
    _update_protective_orders_db(market, ticker)

    result.status = "closed"
    logger.info(
        "[%s] ✓ Closed successfully: fill=$%.4f PnL=$%.2f", ticker, fill_price, result.pnl
    )
    return result


# ── Per-market loop ───────────────────────────────────────────────────────────

def _close_market(market: str, args: argparse.Namespace) -> list[CloseResult]:
    """Close all targeted positions for a single market universe.

    Returns list of CloseResult, one per targeted ticker.
    """
    assert market in ALLOWED_UNIVERSES, (
        f"BLOCKED: '{market}' not in allowlist {sorted(ALLOWED_UNIVERSES)}. "
        f"This script must NEVER touch sp500 or other live markets."
    )

    logger.info("=== Processing market: %s ===", market)

    # ── Load config ───────────────────────────────────────────────────────────
    from utils.config import get_active_config
    try:
        config = get_active_config(market)
    except Exception as exc:
        logger.error("Could not load config for %s: %s", market, exc)
        return []

    if "_consolidation_note" not in config:
        logger.warning(
            "Config for %s has no '_consolidation_note' key — "
            "verify the correct config was loaded", market
        )

    # ── Load state file ───────────────────────────────────────────────────────
    try:
        state = _load_state_file(market)
    except FileNotFoundError as exc:
        logger.error("State file missing for %s: %s", market, exc)
        return []

    all_state_positions: list[dict] = state.get("positions", [])
    if not all_state_positions:
        logger.info("No open positions in state file for %s", market)
        return []

    # ── Apply --tickers filter ────────────────────────────────────────────────
    ticker_filter: set[str] | None = None
    if args.tickers:
        ticker_filter = {t.strip().upper() for t in args.tickers.split(",")}

    target_positions = [
        p for p in all_state_positions
        if ticker_filter is None or p.get("ticker", "").upper() in ticker_filter
    ]

    if not target_positions:
        logger.info(
            "No matching positions for %s after ticker filter (%s)", market, args.tickers
        )
        return []

    # ── Connect broker ────────────────────────────────────────────────────────
    from brokers.registry import get_live_broker
    broker = get_live_broker(config)
    if broker is None:
        logger.error("No live broker available for %s (live_enabled=False?)", market)
        return []

    if not broker.connect():
        logger.error("Broker connect() failed for %s", market)
        return []

    logger.info("Broker connected for %s", market)
    results: list[CloseResult] = []

    try:
        for pos_dict in target_positions:
            ticker = pos_dict.get("ticker", "")
            if not ticker:
                logger.warning("Skipping position dict with no ticker: %s", pos_dict)
                continue
            logger.info("--- Ticker: %s ---", ticker)
            try:
                res = _close_ticker(
                    ticker=ticker,
                    state_pos_dict=pos_dict,
                    broker=broker,
                    all_state_positions=all_state_positions,
                    config=config,
                    market=market,
                    live=args.live,
                    skip_clock_check=args.skip_clock_check,
                )
            except Exception as exc:
                logger.error("[%s] Unhandled error (continuing): %s", ticker, exc, exc_info=True)
                res = CloseResult(
                    ticker=ticker, universe=market, status="error", error=str(exc)
                )
            results.append(res)
    finally:
        broker.disconnect()
        logger.info("Broker disconnected for %s", market)

    return results


# ── Summary table ─────────────────────────────────────────────────────────────

def _print_summary(all_results: list[CloseResult]) -> None:
    """Print a formatted per-ticker summary to stdout and log."""
    if not all_results:
        print("\n[consolidation_close] No positions targeted.")
        return

    header = f"{'ticker':<8} {'universe':<16} {'action':<28} {'fill_price':<12} {'pnl':<10} {'status'}"
    separator = "-" * len(header)
    print(f"\n{separator}")
    print(header)
    print(separator)
    for r in all_results:
        fill_str = f"${r.fill_price:.2f}" if r.fill_price > 0 else "—"
        pnl_str = f"${r.pnl:+.2f}" if r.fill_price > 0 else "—"
        print(f"{r.ticker:<8} {r.universe:<16} {r.action:<28} {fill_str:<12} {pnl_str:<10} {r.status}")
    print(separator)
    closed = sum(1 for r in all_results if r.status == "closed")
    errors = sum(1 for r in all_results if r.status == "error")
    print(f"\nSummary: {closed} closed, {errors} errors, {len(all_results)} total\n")
    logger.info("Final summary: %d closed / %d errors / %d total", closed, errors, len(all_results))


# ── CLI ───────────────────────────────────────────────────────────────────────

def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Force-close positions in consolidated markets (sector_etfs, commodity_etfs).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--live",
        action="store_true",
        default=False,
        help="Execute real orders. Default is DRY-RUN (safe to run without --live).",
    )
    parser.add_argument(
        "--market",
        metavar="UNIVERSE",
        default=None,
        help="Restrict to one universe (must be in ALLOWED_UNIVERSES). "
             "Default: both commodity_etfs and sector_etfs.",
    )
    parser.add_argument(
        "--tickers",
        metavar="T1,T2",
        default=None,
        help="Comma-separated list of tickers to close. Default: all in state file.",
    )
    parser.add_argument(
        "--skip-clock-check",
        action="store_true",
        default=False,
        help="Bypass broker.get_clock().is_open check. USE ONLY FOR TESTING.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Entry point. Returns 0 on success, 1 if any live attempt failed."""
    from utils.logging_config import setup_logging
    setup_logging("consolidation_close", extra_log_file="consolidation_close")

    args = _parse_args(argv)

    mode_label = "LIVE" if args.live else "DRY-RUN"
    logger.info("=" * 60)
    logger.info("consolidation_close_positions starting [%s]", mode_label)
    logger.info("market=%s  tickers=%s  skip_clock=%s",
                args.market or "ALL", args.tickers or "ALL", args.skip_clock_check)
    logger.info("=" * 60)

    if args.live:
        logger.warning("⚠  LIVE MODE — real orders WILL be placed")
    else:
        logger.info("DRY-RUN mode — no orders will be placed (pass --live to execute)")

    # ── Determine target universes ────────────────────────────────────────────
    if args.market:
        assert args.market in ALLOWED_UNIVERSES, (
            f"BLOCKED: '{args.market}' not in allowlist {sorted(ALLOWED_UNIVERSES)}. "
            f"This script must NEVER touch sp500 or other live markets."
        )
        target_markets = [args.market]
    else:
        target_markets = sorted(ALLOWED_UNIVERSES)

    # ── Process each market ───────────────────────────────────────────────────
    all_results: list[CloseResult] = []
    had_error = False

    for market in target_markets:
        try:
            market_results = _close_market(market, args)
            all_results.extend(market_results)
            if args.live and any(r.status == "error" for r in market_results):
                had_error = True
        except AssertionError:
            raise  # re-raise universe guard violations immediately
        except Exception as exc:
            logger.error("Error processing market %s: %s", market, exc, exc_info=True)
            had_error = True

    _print_summary(all_results)

    return 1 if had_error else 0


if __name__ == "__main__":
    sys.exit(main())
