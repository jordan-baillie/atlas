#!/usr/bin/env python3
"""Ledger-Broker Reconciliation for Atlas.

Compares broker positions against SQLite trade ledger and fixes discrepancies:
- Broker has position not in ledger → backfill trade entry from broker fill data
- Ledger has open trade not at broker → mark as closed (reconcile_phantom)

Usage:
    python scripts/reconcile_ledger.py [--market sp500] [--dry-run]
"""

import argparse
import sqlite3
import json
import logging
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from atlas_bootstrap import PROJECT_ROOT as PROJECT

from utils.logging_config import setup_logging

log = setup_logging("reconcile_ledger", extra_log_file="reconcile_ledger")

from brokers.routing_policy import BrokerRoutingPolicy


def load_config(market_id: str) -> dict:
    """Load active config for the given market (consults overrides)."""
    from utils.config import get_active_config
    return get_active_config(market_id)



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
            except (json.JSONDecodeError, OSError, KeyError, ValueError) as _plan_exc:
                log.debug("Skipping plan file %s: %s", path, _plan_exc)
                continue
    except (ImportError, OSError, AttributeError) as _glob_exc:
        log.debug("Plan glob failed for %s/%s: %s", ticker, market_id, _glob_exc)

    # 3. Fallback — flagged so audits can find them
    log.warning("Strategy lookup for %s/%s fell through to 'reconciled' — audit this",
                ticker, market_id)
    return "reconciled"


def reconcile_ledger(market_id: str, dry_run: bool = False, broker=None, mode_override: str | None = None) -> dict:
    """Main reconciliation logic. Returns dict with stats.

    Args:
        market_id:     Market identifier (e.g. 'sp500').
        dry_run:       If True, log what would change without writing anything.
        broker:        Optional pre-connected broker instance.  When provided the
                       caller is responsible for its lifecycle (connect/disconnect).
                       When None, this function creates and owns its own connection.
        mode_override: When set to 'live' or 'paper', overrides the trading.mode
                       value from config for this call.  Used by main() for
                       dual-pass routing: call once with 'live', once with 'paper'.
    """
    from db import atlas_db
    from brokers.registry import get_live_broker

    stats: dict = {
        "backfilled": [],
        "closed_phantom": [],
        "matched": 0,
        "errors": [],
    }

    # ── Mode detection ────────────────────────────────────────
    _rl_config = load_config(market_id)
    _rl_mode_from_config = _rl_config.get("trading", {}).get("mode", "live")
    _rl_mode = mode_override if mode_override is not None else _rl_mode_from_config
    _rl_mode_label = f"[{_rl_mode.upper()}]"

    # ── Broker connection ─────────────────────────────────────
    own_broker = False
    if broker is None:
        # When mode_override is set, apply it to the config so get_live_broker
        # returns the appropriate broker (live vs paper account).
        _rl_policy = BrokerRoutingPolicy(_rl_config, market_id=market_id)
        if mode_override == "paper":
            _rl_policy = _rl_policy.for_paper()
        elif mode_override is not None and mode_override != _rl_policy.mode:
            # Handle mode_override="live" or other explicit overrides
            _rl_policy = BrokerRoutingPolicy(
                {**_rl_config, "trading": {**_rl_config.get("trading", {}), "mode": mode_override}},
                market_id=market_id,
            )
        config = _rl_policy.config
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
        except (ImportError, ModuleNotFoundError, AttributeError, RuntimeError, OSError) as _ub_exc:
            log.debug("universe.builder unavailable (%s), trying definitions fallback", _ub_exc)
            try:
                from universe.definitions import get_universe_tickers as _def_tickers
                universe_tickers = set(_def_tickers(market_id))
            except (ImportError, ModuleNotFoundError, AttributeError, RuntimeError, OSError) as _ud_exc:
                log.debug("universe.definitions also unavailable: %s", _ud_exc)
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
        # Paper mode reads from paper_trades; live mode reads from trades
        if _rl_mode == "paper":
            try:
                open_trades = atlas_db.get_open_paper_trades()
            except AttributeError:
                log.warning("%s get_open_paper_trades not available — falling back to trades", _rl_mode_label)
                open_trades = atlas_db.get_open_positions()
        else:
            open_trades = atlas_db.get_open_positions()
        ledger_map = {t["ticker"]: t for t in open_trades
                      if t.get("universe") == market_id}
        log.info("%s Ledger open trades for %s: %d", _rl_mode_label, market_id, len(ledger_map))

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
                # Priority 1a: get_fill_price(order_id) — exact match, most authoritative
                _fill_order_id: str | None = getattr(fill, "order_id", None) if fill else None
                _cached_fill: float | None = None
                if _fill_order_id:
                    _cached_fill = atlas_db.get_fill_price(_fill_order_id)
                # Priority 1b: symbol-based broker_orders scan (broader, less precise)
                if _cached_fill is None:
                    _cached_fill = atlas_db.get_broker_fill_price(ticker, side="buy")
                if _cached_fill and _cached_fill > 0:
                    entry_price = _cached_fill
                    log.info(
                        "reconcile_ledger: [P1] broker_orders fill for %s: $%.4f "
                        "(order_id=%s)",
                        ticker, _cached_fill, _fill_order_id or "symbol-scan",
                    )
                else:
                    # Priority 2: inferred — live order history fill or broker position avg_entry
                    # Emit WARNING: these are NOT broker-confirmed prices.
                    _inferred = (
                        fill.fill_price
                        if fill and fill.fill_price and fill.fill_price > 0
                        else bp.entry_price
                    )
                    if _inferred and _inferred > 0:
                        entry_price = _inferred
                        log.warning(
                            "[fill-price] using inferred price for ticker=%s order_id=%s, "
                            "broker_orders empty",
                            ticker, _fill_order_id or "n/a",
                        )
                    else:
                        # Priority 3: no price at all — skip INSERT, alert (do NOT fabricate)
                        log.error(
                            "[fill-price] ticker=%s: no fill price from broker_orders or "
                            "inference (P3 skip) — run sync_broker_orders.py to populate",
                            ticker,
                        )
                        try:
                            from utils.telegram import send_message as _tg_p3
                            _tg_p3(
                                f"🚨 [fill-price P3] reconcile_ledger: {ticker} has no fill "
                                f"price. Run sync_broker_orders.py to populate broker_orders."
                            )
                        except (ImportError, OSError, ConnectionError, RuntimeError) as _tg_exc:
                            log.debug("Telegram P3 fill-price alert failed: %s", _tg_exc)
                        stats["errors"].append(f"{ticker}: no fill price (P3 skip)")
                        continue
                shares = (
                    int(fill.raw.get("filled_qty", bp.shares))
                    if (fill and hasattr(fill, "raw") and fill.raw.get("filled_qty"))
                    else int(bp.shares)
                )

                # ── Stop-price fallback chain (P1 → P2 → P3 → P4) ──────────────────
                # OCO bracket stop legs often have status='held' and are NOT returned
                # by broker.get_open_orders() — so we use a multi-source fallback
                # chain instead of relying solely on the live order list.
                _broker_stop: float = 0.0

                # P1: broker_orders table — most authoritative; includes held/oco rows
                try:
                    with atlas_db.get_db() as _bo_db:
                        _bo_rows = _bo_db.execute(
                            """
                            SELECT raw_alpaca_json, submitted_at
                            FROM broker_orders
                            WHERE symbol = ?
                              AND side = 'sell'
                              AND order_class IN ('oco', 'bracket', 'simple')
                              AND status IN ('held', 'new', 'accepted', 'pending_new')
                              AND raw_alpaca_json LIKE '%stop_price%'
                            ORDER BY submitted_at DESC
                            LIMIT 5
                            """,
                            (ticker,),
                        ).fetchall()
                    for _bo_row in _bo_rows:
                        _raw_j = _bo_row[0]
                        if not _raw_j:
                            continue
                        try:
                            _parsed_j = json.loads(_raw_j)
                        except (json.JSONDecodeError, TypeError):
                            continue
                        _sp_raw = _parsed_j.get("stop_price")
                        if _sp_raw and str(_sp_raw).lower() not in ("none", "null", ""):
                            try:
                                _candidate = float(_sp_raw)
                                if _candidate > 0:
                                    _broker_stop = _candidate
                                    log.info(
                                        "reconcile_ledger: [P1] stop_price=%.4f for %s "
                                        "from broker_orders (status=%s)",
                                        _broker_stop, ticker,
                                        _parsed_j.get("status", "?"),
                                    )
                                    break
                            except (ValueError, TypeError):
                                continue
                except Exception as _p1_exc:
                    log.debug(
                        "reconcile_ledger: P1 broker_orders lookup failed for %s: %s",
                        ticker, _p1_exc,
                    )

                # P2: position_protective_orders table
                if _broker_stop <= 0:
                    try:
                        with atlas_db.get_db() as _ppo_db:
                            _ppo_row = _ppo_db.execute(
                                """
                                SELECT stop_price FROM position_protective_orders
                                WHERE market_id = ? AND ticker = ? AND status = 'active'
                                ORDER BY created_at DESC
                                LIMIT 1
                                """,
                                (market_id, ticker),
                            ).fetchone()
                        if _ppo_row and _ppo_row[0] is not None:
                            _ppo_stop = float(_ppo_row[0])
                            if _ppo_stop > 0:
                                _broker_stop = _ppo_stop
                                log.info(
                                    "reconcile_ledger: [P2] stop_price=%.4f for %s "
                                    "from position_protective_orders",
                                    _broker_stop, ticker,
                                )
                    except Exception as _p2_exc:
                        log.debug(
                            "reconcile_ledger: P2 position_protective_orders lookup "
                            "failed for %s: %s",
                            ticker, _p2_exc,
                        )

                # P3: most recent plan files for this market (entry_price within ±2%)
                if _broker_stop <= 0:
                    try:
                        import glob as _glob_p3
                        _plan_pattern = str(PROJECT / "plans" / f"plan_{market_id}_*.json")
                        _plan_paths = sorted(
                            _glob_p3.glob(_plan_pattern),
                            key=lambda _pp: Path(_pp).stat().st_mtime,
                            reverse=True,
                        )
                        for _plan_path in _plan_paths[:5]:
                            try:
                                with open(_plan_path) as _pf:
                                    _plan_data = json.load(_pf)
                                for _pe in _plan_data.get("proposed_entries", []) or []:
                                    if _pe.get("ticker") != ticker:
                                        continue
                                    _plan_ep = float(_pe.get("entry_price", 0) or 0)
                                    _plan_sp = float(_pe.get("stop_price", 0) or 0)
                                    if _plan_sp > 0 and _plan_ep > 0 and entry_price > 0:
                                        _diff_pct = abs(_plan_ep - entry_price) / entry_price
                                        if _diff_pct <= 0.02:
                                            _broker_stop = _plan_sp
                                            log.info(
                                                "reconcile_ledger: [P3] stop_price=%.4f "
                                                "for %s from plan %s "
                                                "(plan_entry=%.4f broker_fill=%.4f "
                                                "diff=%.2f%%)",
                                                _broker_stop, ticker,
                                                Path(_plan_path).name,
                                                _plan_ep, entry_price,
                                                _diff_pct * 100,
                                            )
                                            break
                                if _broker_stop > 0:
                                    break
                            except (json.JSONDecodeError, OSError, ValueError, KeyError) as _pfe:
                                log.debug(
                                    "reconcile_ledger: P3 plan file %s error: %s",
                                    _plan_path, _pfe,
                                )
                    except Exception as _p3_exc:
                        log.debug(
                            "reconcile_ledger: P3 plan glob failed for %s: %s",
                            ticker, _p3_exc,
                        )

                # P4: no stop_price found in any source — defer backfill
                if _broker_stop <= 0:
                    log.warning(
                        "reconcile_ledger: backfill deferred for %s — "
                        "no stop_price found in P1 (broker_orders) / "
                        "P2 (position_protective_orders) / P3 (plans/). "
                        "Will retry next cycle.",
                        ticker,
                    )
                    stats["errors"].append(
                        f"{ticker}: backfill deferred (no stop_price found in P1/P2/P3)"
                    )
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

                if _rl_mode == "paper":
                    try:
                        _paper_acct_rl = getattr(broker, "account_number", None) if broker else None
                        atlas_db.record_paper_trade_entry(
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
                            paper_account_id=_paper_acct_rl,
                        )
                    except AttributeError:
                        log.warning("%s record_paper_trade_entry not available — falling back to trades", _rl_mode_label)
                        atlas_db.record_trade_entry(
                            ticker=ticker, strategy=_backfill_strategy,
                            universe=_derived_universe or market_id,
                            entry_price=entry_price, shares=shares,
                            stop_price=_stop_to_write, take_profit=None,
                            confidence=0.0, regime_state=None, direction="long",
                        )
                else:
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
                    "%s Backfilled ledger entry for %s: %d shares @ $%.2f stop=%s",
                    _rl_mode_label, ticker,
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
                # Priority 1a: get_fill_price(stop_order_id) — exact match on stop order
                _stop_oid: str | None = trade.get("stop_order_id")
                _cached_sell: float | None = None
                if _stop_oid:
                    _cached_sell = atlas_db.get_fill_price(_stop_oid)
                # Priority 1b: get_fill_price(sell fill order_id)
                if _cached_sell is None:
                    _sell_fill_oid: str | None = getattr(fill, "order_id", None) if fill else None
                    if _sell_fill_oid:
                        _cached_sell = atlas_db.get_fill_price(_sell_fill_oid)
                # Priority 1c: symbol-based broker_orders scan
                if _cached_sell is None:
                    _cached_sell = atlas_db.get_broker_fill_price(ticker, side="sell")
                if _cached_sell and _cached_sell > 0:
                    exit_price = _cached_sell
                    log.info(
                        "reconcile_ledger: [P1] broker_orders sell fill for %s: $%.4f "
                        "(stop_order_id=%s)",
                        ticker, _cached_sell, _stop_oid or "n/a",
                    )
                    exit_reason = "reconcile_fill_cached"
                else:
                    # Priority 2: inferred — live fill or entry_price (last resort)
                    # Emit WARNING: these are NOT broker-confirmed prices.
                    _inferred_exit = (
                        fill.fill_price
                        if fill and fill.fill_price and fill.fill_price > 0
                        else float(trade.get("entry_price", 0) or 0)
                    )
                    if _inferred_exit and _inferred_exit > 0:
                        exit_price = _inferred_exit
                        exit_reason = "reconcile_fill" if fill else "reconcile_phantom"
                        log.warning(
                            "[fill-price] using inferred exit price for ticker=%s "
                            "stop_order_id=%s, broker_orders empty",
                            ticker, _stop_oid or "n/a",
                        )
                    else:
                        # Priority 3: no exit price — skip close, alert (do NOT fabricate)
                        log.error(
                            "[fill-price] ticker=%s: no exit price from broker_orders or "
                            "inference (P3 skip) — phantom stays open until next reconcile",
                            ticker,
                        )
                        try:
                            from utils.telegram import send_message as _tg_p3
                            _tg_p3(
                                f"🚨 [fill-price P3] reconcile_ledger: {ticker} has no exit "
                                f"price. Phantom stays open — run sync_broker_orders.py."
                            )
                        except (ImportError, OSError, ConnectionError, RuntimeError) as _tg_exc:
                            log.debug("Telegram P3 exit-price alert failed: %s", _tg_exc)
                        stats["errors"].append(f"{ticker}: no exit price (P3 skip, stays open)")
                        continue

                if _rl_mode == "paper":
                    try:
                        _paper_acct_rl_exit = getattr(broker, "account_number", None) if broker else None
                        atlas_db.record_paper_trade_exit(
                            ticker=ticker,
                            strategy=trade.get("strategy", "unknown"),
                            exit_price=exit_price,
                            exit_reason=exit_reason,
                            regime_at_exit=None,
                            paper_account_id=_paper_acct_rl_exit,
                        )
                    except AttributeError:
                        log.warning("%s record_paper_trade_exit not available — falling back to trades", _rl_mode_label)
                        atlas_db.record_trade_exit(
                            ticker=ticker, strategy=trade.get("strategy", "unknown"),
                            exit_price=exit_price, exit_reason=exit_reason, regime_at_exit=None,
                        )
                else:
                    atlas_db.record_trade_exit(
                        ticker=ticker,
                        strategy=trade.get("strategy", "unknown"),
                        exit_price=exit_price,
                        exit_reason=exit_reason,
                        regime_at_exit=None,
                    )
                stats["closed_phantom"].append(ticker)
                log.info(
                    "%s Closed phantom ledger entry for %s (exit_price=$%.2f, reason=%s)",
                    _rl_mode_label, ticker,
                    exit_price,
                    exit_reason,
                )
            except (sqlite3.Error, OSError, AttributeError, ValueError, RuntimeError) as exc:
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
            except (OSError, ConnectionError, AttributeError, RuntimeError) as _disc_exc:
                log.debug("Broker disconnect error (non-fatal): %s", _disc_exc)

    return stats


def main() -> int:
    parser = argparse.ArgumentParser(description="Ledger-Broker Reconciliation")
    parser.add_argument("--market", "-m", default="sp500", help="Market ID (default: sp500)")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview what would change without writing to DB",
    )
    parser.add_argument(
        "--no-fix",
        action="store_true",
        help="Alias for --dry-run: report without writing (matches reconcile_positions.py convention)",
    )
    args = parser.parse_args()
    # --no-fix is an alias for --dry-run
    if args.no_fix:
        args.dry_run = True

    log.info("=" * 50)
    log.info("LEDGER-BROKER RECONCILIATION [%s]", args.market.upper())
    log.info("Mode: %s", "DRY RUN" if args.dry_run else "LIVE")
    log.info("=" * 50)

    try:
        # ── LIVE pass ─────────────────────────────────────────────
        result_live = reconcile_ledger(args.market, dry_run=args.dry_run, mode_override="live")
        if "error" in result_live:
            log.error("LIVE reconciliation failed: %s", result_live["error"])
            return 1
        log.info("LIVE pass complete: backfilled=%d closed=%d matched=%d",
                 len(result_live.get("backfilled", [])),
                 len(result_live.get("closed_phantom", [])),
                 result_live.get("matched", 0))

        # ── PAPER pass (only if open paper trades exist) ───────────────
        result_paper: dict = {}
        _main_policy = BrokerRoutingPolicy(load_config(args.market), market_id=args.market)
        if _main_policy.needs_paper_pass():
            log.info("needs_paper_pass()=True for %s — running PAPER reconcile pass", args.market)
            result_paper = reconcile_ledger(args.market, dry_run=args.dry_run, mode_override="paper")
            if "error" in result_paper:
                log.error("PAPER reconciliation failed: %s", result_paper["error"])
            else:
                log.info("PAPER pass complete: backfilled=%d closed=%d matched=%d",
                         len(result_paper.get("backfilled", [])),
                         len(result_paper.get("closed_phantom", [])),
                         result_paper.get("matched", 0))
        else:
            log.debug("No open paper trades for %s — skipping PAPER reconcile pass", args.market)

        combined = {"live": result_live}
        if result_paper:
            combined["paper"] = result_paper
        print(json.dumps(combined, indent=2, default=str))

        # ── Heartbeat: success ───────────────────────────────────
        _total_errors = (
            len(result_live.get("errors", []))
            + len(result_paper.get("errors", []))
        )
        try:
            from db.atlas_db import record_heartbeat as _hb
            _hb(
                "reconcile_ledger",
                "completed",
                {"markets": [args.market], "errors": _total_errors},
            )
        except Exception as _hb_exc:
            log.debug("reconcile_ledger: heartbeat write failed (non-fatal): %s", _hb_exc)

        return 0

    except Exception as _main_exc:
        log.error("Unexpected error in reconcile_ledger main: %s", _main_exc, exc_info=True)
        # ── Heartbeat: failure ───────────────────────────────────
        try:
            from db.atlas_db import record_heartbeat as _hb_fail
            _hb_fail(
                "reconcile_ledger",
                "failed",
                {"error": str(_main_exc)},
            )
        except Exception as _hb_fail_exc:
            log.debug("reconcile_ledger: failure heartbeat write failed (non-fatal): %s", _hb_fail_exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
