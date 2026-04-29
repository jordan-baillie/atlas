#!/usr/bin/env python3
"""Ledger-Broker Reconciliation for Atlas.

Compares broker positions against SQLite trade ledger and fixes discrepancies:
- Broker has position not in ledger → backfill trade entry from broker fill data
- Ledger has open trade not at broker → mark as closed (reconcile_phantom)

Usage:
    python scripts/reconcile_ledger.py [--market sp500] [--dry-run]
"""

import argparse
import json
import logging
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from atlas_bootstrap import PROJECT_ROOT as PROJECT

from utils.logging_config import setup_logging

log = setup_logging("reconcile_ledger", extra_log_file="reconcile_ledger")


def load_config(market_id: str) -> dict:
    """Load active config for the given market."""
    path = PROJECT / "config" / "active" / f"{market_id}.json"
    if not path.exists():
        raise FileNotFoundError(f"Config not found: {path}")
    with open(path) as f:
        return json.load(f)



def _lookup_strategy(ticker: str, market_id: str, state_positions: dict) -> str:
    """Resolve strategy for an untracked broker position.

    Priority:
      1. brokers/state/live_{market}.json position record (if strategy != 'unknown')
      2. Most recent plans/plan_{market}_*.json proposed_entries matching ticker
      3. Fallback: 'reconciled' (last resort — loses strategy attribution)
    """
    # 1. Broker state
    sp = state_positions.get(ticker) or {}
    strat = sp.get("strategy") or ""
    if strat and strat != "unknown":
        return strat

    # 2. Plan files — newest first
    try:
        import glob
        pattern = str(PROJECT / "plans" / f"plan_{market_id}_*.json")
        for path in sorted(glob.glob(pattern), reverse=True):
            try:
                with open(path) as f:
                    plan = json.load(f)
                for entry in plan.get("proposed_entries", []) or []:
                    if entry.get("ticker") == ticker and entry.get("strategy"):
                        return entry["strategy"]
            except Exception:
                continue
    except Exception:
        pass

    # 3. Fallback — flagged so audits can find them
    log.warning("Strategy lookup for %s/%s fell through to 'reconciled' — audit this",
                ticker, market_id)
    return "reconciled"


def reconcile_ledger(market_id: str, dry_run: bool = False, broker=None) -> dict:
    """Main reconciliation logic. Returns dict with stats.

    Args:
        market_id: Market identifier (e.g. 'sp500').
        dry_run:   If True, log what would change without writing anything.
        broker:    Optional pre-connected broker instance.  When provided the
                   caller is responsible for its lifecycle (connect/disconnect).
                   When None, this function creates and owns its own connection.
    """
    from db import atlas_db
    from brokers.registry import get_live_broker

    stats: dict = {
        "backfilled": [],
        "closed_phantom": [],
        "matched": 0,
        "errors": [],
    }

    # ── Broker connection ─────────────────────────────────────
    own_broker = False
    if broker is None:
        config = load_config(market_id)
        broker = get_live_broker(config)
        if not broker or not broker.connect():
            log.error("Cannot connect to broker")
            return {"error": "broker_connect_failed"}
        own_broker = True

    try:
        # 1. Get broker positions ──────────────────────────────
        broker_positions = broker.get_positions()

        # Filter to only positions in this market's universe
        try:
            from universe.builder import get_universe_tickers as _builder_tickers
            universe_tickers = set(_builder_tickers(market_id))
        except Exception:
            try:
                from universe.definitions import get_universe_tickers as _def_tickers
                universe_tickers = set(_def_tickers(market_id))
            except Exception:
                universe_tickers = None

        # Load state-file tickers for this market — broker JSON is ground truth for
        # "what positions this market actually holds", independent of universe membership.
        state_tickers: set = set()
        state_positions: dict = {}
        _state_path = PROJECT / "brokers" / "state" / f"live_{market_id}.json"
        if _state_path.exists():
            try:
                with open(_state_path) as _sf:
                    _state = json.load(_sf)
                _state_pos_list = _state.get("positions", []) or []
                state_tickers = {p["ticker"] for p in _state_pos_list if p.get("ticker")}
                state_positions = {p["ticker"]: p for p in _state_pos_list if p.get("ticker")}
            except Exception as _sf_exc:
                log.warning("Could not load state file %s: %s", _state_path, _sf_exc)

        # Accept a broker position if EITHER: it's in the universe OR it's in this
        # market's state file. This catches tickers held by the market but outside
        # the universe definition (e.g. sector ETFs tracked in live_sp500.json).
        if universe_tickers or state_tickers:
            _allow = (universe_tickers or set()) | state_tickers
            broker_map = {p.ticker: p for p in broker_positions if p.ticker in _allow}
            skipped = len(broker_positions) - len(broker_map)
            if skipped:
                log.info(
                    "Filtered broker positions: %d in-scope (universe=%d, state_file=%d), "
                    "%d skipped (other markets)",
                    len(broker_map), len(universe_tickers or set()), len(state_tickers), skipped,
                )
        else:
            broker_map = {p.ticker: p for p in broker_positions}
            log.warning(
                "Could not load universe OR state tickers for %s — using ALL broker positions",
                market_id,
            )

        log.info("Broker positions for %s: %d", market_id, len(broker_map))

        # 2. Get ledger open trades for this market ────────────
        open_trades = atlas_db.get_open_positions()
        ledger_map = {t["ticker"]: t for t in open_trades
                      if t.get("universe") == market_id}
        log.info("Ledger open trades for %s: %d", market_id, len(ledger_map))

        # 3. Fetch recent order history for fill data ─────────
        try:
            recent_orders = broker.get_history_orders(days=7)
        except Exception as exc:
            log.warning("Could not fetch order history (non-fatal): %s", exc)
            recent_orders = []

        buy_fills: dict = {}
        sell_fills: dict = {}
        for order in recent_orders:
            side = (
                order.side.value.upper()
                if hasattr(order.side, "value")
                else str(order.side).upper()
            )
            status = (
                order.status.value.upper()
                if hasattr(order.status, "value")
                else str(order.status).upper()
            )
            if status != "FILLED":
                continue
            target = buy_fills if side == "BUY" else sell_fills if side == "SELL" else None
            if target is None:
                continue
            prev = target.get(order.ticker)
            filled_at = order.raw.get("filled_at", "") if hasattr(order, "raw") else ""
            prev_filled_at = prev.raw.get("filled_at", "") if (prev and hasattr(prev, "raw")) else ""
            if prev is None or filled_at > prev_filled_at:
                target[order.ticker] = order

        log.info(
            "Order history: %d buy fills, %d sell fills across all tickers",
            len(buy_fills),
            len(sell_fills),
        )

        # 4. Broker has position NOT in ledger → backfill ─────
        for ticker, bp in broker_map.items():
            if ticker in ledger_map:
                continue

            log.warning(
                "UNTRACKED in ledger: %s (%s shares @ $%.2f) — backfilling",
                ticker,
                bp.shares,
                bp.entry_price,
            )

            if dry_run:
                stats["backfilled"].append(f"{ticker} (dry-run)")
                log.info("DRY RUN: would backfill %s", ticker)
                continue

            try:
                fill = buy_fills.get(ticker)
                # Priority 1: broker_orders local cache (source-of-truth fill price)
                # This eliminates phantom-price inference bugs (CHTR pattern).
                _cached_fill = atlas_db.get_broker_fill_price(ticker, side="buy")
                if _cached_fill and _cached_fill > 0:
                    entry_price = _cached_fill
                    log.info(
                        "reconcile_ledger: used broker_orders fill price for %s: $%.4f "
                        "(vs inferred $%.4f)",
                        ticker, _cached_fill,
                        (fill.fill_price if fill and fill.fill_price else bp.entry_price),
                    )
                else:
                    # Priority 2: live order history fill (from this session's fetch)
                    # Priority 3: broker position avg_entry_price (inference fallback)
                    entry_price = (
                        fill.fill_price
                        if fill and fill.fill_price and fill.fill_price > 0
                        else bp.entry_price
                    )
                shares = (
                    int(fill.raw.get("filled_qty", bp.shares))
                    if (fill and hasattr(fill, "raw") and fill.raw.get("filled_qty"))
                    else int(bp.shares)
                )

                # No-zero-stop guard: look up actual broker stop order before INSERT.
                # Never use synthetic entry_price * 0.95 — that's a ghost risk value.
                # Check open orders for a SELL stop targeting this ticker.
                _broker_stop: float = 0.0
                try:
                    for _ord in broker.get_open_orders():
                        _is_sell = getattr(_ord, "side", None)
                        _is_sell_str = (
                            _is_sell.value.upper()
                            if hasattr(_is_sell, "value")
                            else str(_is_sell).upper()
                        )
                        _ord_type = str(getattr(_ord, "type", "") or "").lower()
                        if (
                            _ord.ticker == ticker
                            and _is_sell_str == "SELL"
                            and _ord_type in ("stop", "trailing_stop", "stop_limit")
                        ):
                            _broker_stop = float(getattr(_ord, "stop_price", 0) or 0)
                            if _broker_stop > 0:
                                break
                except Exception as _stop_exc:
                    log.debug("reconcile_ledger: stop lookup failed for %s: %s", ticker, _stop_exc)

                if _broker_stop <= 0:
                    log.warning(
                        "reconcile_ledger: skipping backfill for %s — "
                        "no broker stop order found (stop_price=0 would create ghost row). "
                        "Run sync_protective_orders to place stop, then re-run reconcile_ledger.",
                        ticker,
                    )
                    stats["errors"].append(f"{ticker}: no broker stop (skipped INSERT)")
                    continue

                # Direction sanity-check: for long positions, stop must be BELOW entry.
                # A trailing stop that has moved above entry (profit-locking) is valid
                # operationally but must not be stored as stop_price — write NULL instead.
                # The DB CHECK constraint (stop_price < entry for longs) will reject it
                # otherwise. The stop_order_id already tracks the actual broker order.
                _direction_backfill = "long"  # reconcile_ledger only backfills longs
                _stop_to_write: float | None = _broker_stop
                if (
                    _direction_backfill == "long"
                    and _broker_stop > 0
                    and _broker_stop >= entry_price
                ):
                    log.warning(
                        "reconcile_ledger: refusing inverted stop for %s: "
                        "entry=%.4f stop=%.4f — writing NULL. "
                        "stop_order_id will track the broker trailing stop.",
                        ticker, entry_price, _broker_stop,
                    )
                    try:
                        from utils.telegram import send_message as _tg_send
                        _tg_send(
                            f"⚠ Backfill refused inverted stop: {ticker} "
                            f"entry={entry_price:.4f} stop={_broker_stop:.4f}"
                        )
                    except Exception as _tg_exc:
                        log.debug("reconcile_ledger: telegram send failed: %s", _tg_exc)
                    _stop_to_write = None

                _backfill_strategy = _lookup_strategy(ticker, market_id, state_positions)
                from universe.membership import derive_universe
                _derived_universe = derive_universe(ticker, market_id)
                if _derived_universe != market_id:
                    log.warning(
                        "reconcile_ledger: ticker=%s market_id=%s → universe resolved to %s "
                        "(ticker not in %s universe)",
                        ticker, market_id, _derived_universe, market_id,
                    )

                # Pre-insert duplicate guard (belt-and-suspenders over DB UNIQUE index).
                # The ledger_map check above catches the common case; this catches the
                # edge case where a concurrent process inserted the row after we built
                # ledger_map, or where the universe mismatch changes the effective key.
                _resolved_universe = _derived_universe or market_id
                try:
                    with atlas_db.get_db() as _chk_db:
                        _existing = _chk_db.execute(
                            "SELECT id, strategy FROM trades "
                            "WHERE ticker=? AND universe=? AND exit_date IS NULL",
                            (ticker, _resolved_universe),
                        ).fetchone()
                    if _existing:
                        log.info(
                            "reconcile_skip_duplicate_open: ticker=%s universe=%s "
                            "existing_id=%s existing_strategy=%s — skipping INSERT",
                            ticker, _resolved_universe,
                            _existing[0], _existing[1],
                        )
                        stats["errors"].append(
                            f"{ticker}: duplicate open row already exists (id={_existing[0]})"
                        )
                        continue
                except Exception as _chk_exc:
                    log.warning(
                        "reconcile_ledger: pre-insert duplicate check failed for %s (non-fatal): %s",
                        ticker, _chk_exc,
                    )

                atlas_db.record_trade_entry(
                    ticker=ticker,
                    strategy=_backfill_strategy,
                    universe=_derived_universe or market_id,
                    entry_price=entry_price,
                    shares=shares,
                    stop_price=_stop_to_write,
                    take_profit=None,
                    confidence=0.0,
                    regime_state=None,
                    direction="long",
                )
                stats["backfilled"].append(ticker)
                log.info(
                    "Backfilled ledger entry for %s: %d shares @ $%.2f stop=%s",
                    ticker,
                    shares,
                    entry_price,
                    f"{_stop_to_write:.4f}" if _stop_to_write is not None else "NULL",
                )
            except Exception as exc:
                log.error("Failed to backfill %s: %s", ticker, exc)
                stats["errors"].append(f"{ticker}: {exc}")

        # 5. Ledger has open trade NOT at broker → close as phantom ─
        for ticker, trade in ledger_map.items():
            if ticker in broker_map:
                stats["matched"] += 1
                continue

            log.warning(
                "PHANTOM in ledger: %s (open trade but no broker position) — closing",
                ticker,
            )

            if dry_run:
                stats["closed_phantom"].append(f"{ticker} (dry-run)")
                log.info("DRY RUN: would close phantom %s", ticker)
                continue

            try:
                fill = sell_fills.get(ticker)
                # Priority 1: broker_orders local cache (source-of-truth fill price)
                _cached_sell = atlas_db.get_broker_fill_price(ticker, side="sell")
                if _cached_sell and _cached_sell > 0:
                    exit_price = _cached_sell
                    log.info(
                        "reconcile_ledger: used broker_orders sell fill for %s: $%.4f",
                        ticker, _cached_sell,
                    )
                    exit_reason = "reconcile_fill_cached"
                else:
                    # Priority 2: live order history fill
                    # Priority 3: entry price as last resort (inference fallback)
                    exit_price = (
                        fill.fill_price
                        if fill and fill.fill_price and fill.fill_price > 0
                        else float(trade.get("entry_price", 0) or 0)
                    )
                    exit_reason = "reconcile_fill" if fill else "reconcile_phantom"

                atlas_db.record_trade_exit(
                    ticker=ticker,
                    strategy=trade.get("strategy", "unknown"),
                    exit_price=exit_price,
                    exit_reason=exit_reason,
                    regime_at_exit=None,
                )
                stats["closed_phantom"].append(ticker)
                log.info(
                    "Closed phantom ledger entry for %s (exit_price=$%.2f, reason=%s)",
                    ticker,
                    exit_price,
                    exit_reason,
                )
            except Exception as exc:
                log.error("Failed to close phantom %s: %s", ticker, exc)
                stats["errors"].append(f"{ticker}: {exc}")

        # 6. Telegram summary ──────────────────────────────────
        changes = stats["backfilled"] + stats["closed_phantom"]
        if changes and not dry_run:
            try:
                from utils.telegram import send_message

                parts = ["🔄 <b>Ledger Reconciliation</b>\n"]
                if stats["backfilled"]:
                    parts.append(f"📥 Backfilled: {', '.join(stats['backfilled'])}")
                if stats["closed_phantom"]:
                    parts.append(f"👻 Closed phantoms: {', '.join(stats['closed_phantom'])}")
                if stats["errors"]:
                    parts.append(f"❌ Errors: {len(stats['errors'])}")
                parts.append(f"✅ Matched: {stats['matched']}")
                send_message("\n".join(parts))
            except Exception as exc:
                log.warning("Telegram notification failed: %s", exc)

        log.info(
            "Reconciliation complete: backfilled=%d, closed=%d, matched=%d, errors=%d",
            len(stats["backfilled"]),
            len(stats["closed_phantom"]),
            stats["matched"],
            len(stats["errors"]),
        )

    finally:
        if own_broker:
            try:
                broker.disconnect()
            except Exception:
                pass

    return stats


def main() -> int:
    parser = argparse.ArgumentParser(description="Ledger-Broker Reconciliation")
    parser.add_argument("--market", "-m", default="sp500", help="Market ID (default: sp500)")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview what would change without writing to DB",
    )
    args = parser.parse_args()

    log.info("=" * 50)
    log.info("LEDGER-BROKER RECONCILIATION [%s]", args.market.upper())
    log.info("Mode: %s", "DRY RUN" if args.dry_run else "LIVE")
    log.info("=" * 50)

    result = reconcile_ledger(args.market, dry_run=args.dry_run)

    if "error" in result:
        log.error("Reconciliation failed: %s", result["error"])
        return 1

    print(json.dumps(result, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
