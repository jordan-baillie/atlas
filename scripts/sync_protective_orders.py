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
      --market {asx,sp500,commodity_etfs,sector_etfs,all}       Market to sync (default: all)
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
_MARKETS = ("asx", "sp500", "commodity_etfs", "sector_etfs")
# Default broker per market (overridden by config)
_DEFAULT_BROKER: dict[str, str] = {
    "sp500": "alpaca",
    "commodity_etfs": "alpaca",
    "sector_etfs": "alpaca",
}

# State file tracking stop orders observed in "held" status
_HELD_STATE_FILE = PROJECT / "data" / "stops_held_state.json"

# Max consecutive held-cancel-resubmit cycles before giving up on a ticker.
# After this many attempts, the ticker is flagged permanently_skipped and
# the operator is alerted at most once per calendar day.
_HELD_MAX_RETRIES = 4

# PDT (Pattern Day Trader) deferred-stop tracking
_PDT_STATE_FILE = PROJECT / "data" / "pdt_deferred_state.json"
# Retry PDT-deferred positions ONLY before this UTC hour (pre-market / after-hours window).
# 00:00–13:59 UTC ≈ before US market open — same-day restriction has cleared overnight.
_PDT_RETRY_BEFORE_UTC_HOUR = 14


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
    """Load the most relevant trade plan for a market.

    Tries today's plan first, then falls back to the most recent plan file
    for this market. Protective orders need stop prices even for positions
    entered on prior days.
    """
    plans_dir = PROJECT / "plans"
    # Try today first
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

    # Fall back to most recent plan for this market (positions may span days)
    pattern = f"plan_{market_id}_*.json"
    plan_files = sorted(plans_dir.glob(pattern), reverse=True)
    for path in plan_files[:3]:  # check the 3 most recent
        with open(path) as f:
            plan = json.load(f)
        status = plan.get("status", "")
        if status in ("EXECUTED", "APPROVED", "PENDING_APPROVAL"):
            logger.info("Loaded recent plan: %s (status=%s)", path.name, status)
            return plan

    logger.info("No plan file found for %s — will use position data only", market_id)
    return None



# ═══════════════════════════════════════════════════════════════
# Held-stop detection and auto-resubmit
# ═══════════════════════════════════════════════════════════════

def _load_held_state(state_file: Path | None = None) -> dict:
    """Load per-ticker held-stop state from JSON file."""
    path = state_file or _HELD_STATE_FILE
    if path.exists():
        try:
            with open(path) as f:
                return json.load(f)
        except Exception as exc:
            logger.warning("Could not read held-stop state file %s: %s", path, exc)
    return {}


def _save_held_state(state: dict, state_file: Path | None = None) -> None:
    """Persist held-stop state to JSON file."""
    path = state_file or _HELD_STATE_FILE
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w") as f:
            json.dump(state, f, indent=2)
    except Exception as exc:
        logger.warning("Could not save held-stop state file %s: %s", path, exc)


def _maybe_alert_stuck(
    ticker: str,
    market_id: str,
    *,
    reason: str,
    state: dict,
    key: str,
    send_telegram: bool = True,
    permanent: bool = False,
    now_iso: str | None = None,
) -> bool:
    """Send ONE Telegram alert per calendar day for a stuck-held ticker.

    Updates state[key]["last_alerted_date"] so the same ticker does not
    spam the operator multiple times per day. Returns True if an alert
    was sent (or would have been in dry mode), False if suppressed.
    """
    today = (now_iso or datetime.now().isoformat())[:10]
    entry = state.get(key) or {}
    if entry.get("last_alerted_date") == today:
        return False  # already alerted today
    entry["last_alerted_date"] = today
    state[key] = entry
    if not send_telegram:
        return True
    try:
        from utils.telegram import send_message
        status_line = (
            "account-level reject (permanent)" if permanent else "retry cap reached"
        )
        from utils.telegram import tg_escape as _tge
        msg = (
            f"🚨 <b>Stop stuck-held for {ticker}</b>\n"
            f"Market: {market_id.upper()}\n"
            f"Reason: <code>{_tge(reason)}</code>\n"
            f"Status: {status_line}\n"
            f"<i>Manual intervention needed — not resubmitting further today.</i>"
        )
        send_message(msg)
    except Exception as tg_exc:
        logger.warning("_maybe_alert_stuck: Telegram alert failed (non-fatal): %s", tg_exc)
    return True


def _handle_held_stops(
    broker,
    market_id: str,
    *,
    dry_run: bool = False,
    send_telegram: bool = True,
    state_file: Path | None = None,
    now_iso: str | None = None,
    state_tickers: "set[str] | None" = None,
) -> dict:
    """Detect stop orders stuck in 'held' status and resubmit on the second consecutive cycle.

    First observed cycle: record in ``data/stops_held_state.json`` keyed by
    ``"{ticker}::{market_id}"``.  Second+ consecutive cycle: cancel the stuck
    order and let the subsequent ``sync_all_protective_orders`` call re-place it.

    On resubmit: logs WARNING + sends Telegram alert.

    Parameters
    ----------
    broker:        Connected broker instance (must support get_open_orders / cancel_order).
    market_id:     Market identifier string (e.g. "sp500").
    dry_run:       Log intent but do not cancel or write state.
    send_telegram: Send Telegram alert on resubmit.  Set False in tests.
    state_file:    Override state file path (for testing).
    now_iso:       Override current timestamp (for testing).

    Returns
    -------
    dict with keys:
      resubmitted (list[str]) — tickers whose held stop was cancelled this cycle.
      newly_held  (list[str]) — tickers newly observed as held (first cycle).
      errors      (list[str]) — tickers where cancel_order failed.
    """
    resubmitted: list[str] = []
    newly_held: list[str] = []
    errors: list[str] = []

    _now = now_iso or datetime.now().isoformat()

    try:
        open_orders = broker.get_open_orders()
    except Exception as exc:
        logger.warning("_handle_held_stops: get_open_orders failed (non-fatal): %s", exc)
        return {"resubmitted": resubmitted, "newly_held": newly_held, "errors": errors}

    # Identify stop SELL orders with raw status == "held"
    # Capture the full raw dict per ticker so we can inspect reject_reason.
    currently_held: dict[str, dict] = {}   # ticker → {"order_id": str, "raw": dict, "status": str}
    for order in open_orders:
        raw = getattr(order, "raw", {}) or {}
        order_status = raw.get("status", "")
        order_type = raw.get("order_type", "")
        side = raw.get("side", "")
        ticker = getattr(order, "ticker", "") or ""
        if (
            order_status == "held"
            and order_type in ("stop", "stop_limit", "trailing_stop")
            and side == "sell"
            and ticker
        ):
            currently_held[ticker] = {
                "order_id": getattr(order, "order_id", "") or "",
                "raw": raw,
                "status": order_status,
            }

    # ── Scope to this market's tickers ──────────────────────────────────
    # Without this filter, sp500 sync would also process held stops for
    # commodity_etfs positions (and vice versa), causing duplicate operations
    # and polluting the shared stops_held_state.json across markets.
    if state_tickers is not None:
        _before = len(currently_held)
        currently_held = {t: v for t, v in currently_held.items() if t in state_tickers}
        if _before != len(currently_held):
            logger.debug(
                "_handle_held_stops: filtered held stops %d→%d for %s by state_tickers",
                _before, len(currently_held), market_id,
            )

    if currently_held:
        logger.info(
            "_handle_held_stops: %d held stop(s) detected for %s: %s",
            len(currently_held), market_id, list(currently_held.keys()),
        )
    else:
        logger.debug("_handle_held_stops: no held stops for %s", market_id)

    state = _load_held_state(state_file)

    # Tokens in status/reject_reason that indicate a permanent account-level issue.
    _HARD_REJECT_TOKENS = (
        "pdt", "day_trading", "short_sale", "hard_to_borrow",
        "insufficient_buying_power", "insufficient_bp", "htb",
        "trade_suspended", "account_restricted",
    )

    for ticker, info in currently_held.items():
        order_id = info["order_id"]
        raw = info["raw"]
        order_status = info.get("status", "")
        _rr_raw = raw.get("reject_reason") or raw.get("rejected_reason") or ""
        reject_reason = _rr_raw.lower() if isinstance(_rr_raw, str) else ""
        hard_reject = any(
            tok in f"{reject_reason} {order_status}".lower()
            for tok in _HARD_REJECT_TOKENS
        )

        key = f"{ticker}::{market_id}"
        entry = state.get(key)  # None if first observation ever

        # Branch 0 — account-level hard reject: permanently skip, alert once
        if hard_reject:
            if not (entry or {}).get("permanently_skipped"):
                logger.error(
                    "_handle_held_stops: %s (%s) rejected for account-level reason "
                    "(reject_reason=%r status=%r) — will NOT resubmit",
                    ticker, market_id, reject_reason, order_status,
                )
            base = entry or {"first_seen": _now, "order_id": order_id, "retry_count": 0}
            new_entry = {
                "first_seen": base.get("first_seen", _now),
                "order_id": order_id,
                "retry_count": base.get("retry_count", 0),
                "last_alerted_date": base.get("last_alerted_date", ""),
                "permanently_skipped": True,
                "skip_reason": reject_reason or order_status or "account_level",
            }
            state[key] = new_entry
            _maybe_alert_stuck(
                ticker, market_id,
                reason=new_entry["skip_reason"],
                state=state, key=key,
                send_telegram=send_telegram,
                permanent=True, now_iso=_now,
            )
            errors.append(ticker)
            continue

        # Branch 1 — already permanently skipped: no cancel, maybe alert once/day
        if entry and entry.get("permanently_skipped"):
            _maybe_alert_stuck(
                ticker, market_id,
                reason=entry.get("skip_reason", "unknown"),
                state=state, key=key,
                send_telegram=send_telegram,
                permanent=True, now_iso=_now,
            )
            continue

        # Branch 2 — first observation: record, do not cancel
        if entry is None:
            logger.info(
                "_handle_held_stops: stop for %s (%s) is held for the first time "
                "(order_id=%s) — recording, will resubmit next cycle if still held",
                ticker, market_id, order_id,
            )
            state[key] = {
                "first_seen": _now,
                "order_id": order_id,
                "retry_count": 0,
                "last_alerted_date": "",
                "permanently_skipped": False,
                "skip_reason": "",
            }
            newly_held.append(ticker)
            continue

        # Branch 3 — retry cap reached: flip to permanently_skipped, alert once/day, NO cancel
        retry_count = int(entry.get("retry_count", 0))
        if retry_count >= _HELD_MAX_RETRIES:
            logger.warning(
                "_handle_held_stops: %s (%s) exceeded max retries (%d) — "
                "flagging permanently_skipped",
                ticker, market_id, _HELD_MAX_RETRIES,
            )
            entry["permanently_skipped"] = True
            entry["skip_reason"] = f"max_retries_{_HELD_MAX_RETRIES}"
            state[key] = entry
            _maybe_alert_stuck(
                ticker, market_id,
                reason=entry["skip_reason"],
                state=state, key=key,
                send_telegram=send_telegram,
                permanent=False, now_iso=_now,
            )
            continue

        # Branch 4 — within retry budget: cancel so sync re-places on the next pass
        entry["retry_count"] = retry_count + 1
        entry["order_id"] = order_id
        state[key] = entry  # persist attempt counter BEFORE cancel attempt

        if dry_run:
            logger.info(
                "[DRY RUN] _handle_held_stops: would cancel+resubmit stuck held stop "
                "for %s (was held since %s, order_id=%s, retry=%d/%d)",
                ticker, entry.get("first_seen", "?"), order_id,
                entry["retry_count"], _HELD_MAX_RETRIES,
            )
            resubmitted.append(ticker)
            continue

        logger.warning(
            "Stop for %s (%s) has been held for ≥2 consecutive sync cycles "
            "(first seen %s, order_id=%s, retry=%d/%d) — cancelling and resubmitting",
            ticker, market_id, entry.get("first_seen", "?"), order_id,
            entry["retry_count"], _HELD_MAX_RETRIES,
        )
        cancel_result = broker.cancel_order(order_id)
        if cancel_result and getattr(cancel_result, "success", False):
            logger.info(
                "_handle_held_stops: successfully cancelled held stop for %s (id=%s)",
                ticker, order_id,
            )
            resubmitted.append(ticker)
            # IMPORTANT: do NOT pop entry from state anymore — retry_count must
            # persist across cycles to enforce the cap. Cleanup happens only
            # when the ticker is no longer in currently_held (resolved_keys
            # block below).
            if send_telegram:
                try:
                    from utils.telegram import send_message
                    send_message(
                        f"⚠️ Resubmitted stuck <code>held</code> stop for "
                        f"<b>{ticker}</b>\n"
                        f"Market: {market_id.upper()} | "
                        f"Order: <code>{order_id[:16]}</code> | "
                        f"Retry: {entry['retry_count']}/{_HELD_MAX_RETRIES}"
                    )
                except Exception as tg_exc:
                    logger.warning(
                        "_handle_held_stops: Telegram alert failed (non-fatal): %s",
                        tg_exc,
                    )
        else:
            err_msg = (
                getattr(cancel_result, "message", "unknown")
                if cancel_result else "cancel_order returned None"
            )
            logger.error(
                "_handle_held_stops: cancel_order FAILED for %s (id=%s): %s",
                ticker, order_id, err_msg,
            )
            errors.append(ticker)

    # Clean up state entries for tickers that are no longer held (resolved on their own)
    # Only clean up entries for THIS market to avoid deleting other markets' held state.
    # E.g. when sp500 sync runs, it must not wipe CCJ::commodity_etfs from the shared file.
    resolved_keys = [
        k for k in list(state.keys())
        if k.endswith(f"::{market_id}") and k.split("::")[0] not in currently_held
    ]
    for k in resolved_keys:
        logger.debug("_handle_held_stops: clearing resolved held state for %s", k)
        state.pop(k, None)

    if not dry_run:
        _save_held_state(state, state_file)

    return {"resubmitted": resubmitted, "newly_held": newly_held, "errors": errors}


# ═══════════════════════════════════════════════════════════════
# PDT (Pattern Day Trader) deferred-stop helpers
# ═══════════════════════════════════════════════════════════════

def _load_pdt_state(state_file: Path | None = None) -> dict:
    """Load per-ticker PDT-deferral state from JSON file."""
    path = state_file or _PDT_STATE_FILE
    if path.exists():
        try:
            with open(path) as f:
                return json.load(f)
        except Exception as exc:
            logger.warning("Could not read PDT state file %s: %s", path, exc)
    return {}


def _save_pdt_state(state: dict, state_file: Path | None = None) -> None:
    """Persist PDT-deferral state to JSON file."""
    path = state_file or _PDT_STATE_FILE
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w") as f:
            json.dump(state, f, indent=2)
    except Exception as exc:
        logger.warning("Could not save PDT state file %s: %s", path, exc)


def _is_pdt_retry_window(now_utc: "datetime | None" = None) -> bool:
    """Return True if we are in the pre-market/AH window where PDT stops can be retried.

    PDT (Pattern Day Trader) rule on accounts < $25k prevents placing a stop-sell
    on a position entered the same calendar day.  Outside US RTH (hour < 14 UTC)
    the position is no longer 'same day' from Alpaca's perspective, so the stop
    can be placed successfully.
    """
    t = now_utc or datetime.utcnow()
    return t.hour < _PDT_RETRY_BEFORE_UTC_HOUR  # True = pre-market / AH (safe to retry)


def _pdt_should_skip(ticker: str, market_id: str, pdt_state: dict) -> bool:
    """Return True if *ticker* is recorded as PDT-deferred for *market_id*.

    Call this only when NOT in the retry window (_is_pdt_retry_window() == False),
    i.e. during RTH.  Avoids hammering Alpaca with repeat PDT rejections every 15 min.
    """
    return f"{ticker}::{market_id}" in pdt_state


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
    broker_name = config.get("trading", {}).get("broker", _DEFAULT_BROKER.get(market_id, "alpaca"))
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

    # ── Merge stop prices from state file ────────────────
    # The state file has CURRENT stop prices (updated by
    # _enrich_from_broker_stops → _update_state_positions).
    # Plan files only have the INITIAL stop from entry day.
    # Merge state stops into the plan so sync_all_protective_orders
    # uses current levels instead of falling back to 5% below entry.
    state_path = PROJECT / "brokers" / "state" / f"live_{market_id}.json"
    state_tickers: set[str] = set()  # tickers this market owns (for position scoping)
    try:
        if state_path.exists():
            import json as _json
            with open(state_path) as _sf:
                _state = _json.load(_sf)
            # Build state_tickers for per-market position scoping (P0-3 fix)
            state_tickers = {
                _sp.get("ticker", "") for _sp in _state.get("positions", [])
                if _sp.get("ticker", "")
            }
            _state_stops = {}
            for _sp in _state.get("positions", []):
                _t = _sp.get("ticker", "")
                _stop = _sp.get("stop_price", 0)
                if _t and _stop:
                    _state_stops[_t] = _stop

            if _state_stops:
                # Ensure plan has the right structure for merging
                if plan is None:
                    plan = {"proposed_entries": []}
                entries = plan.get("proposed_entries", [])
                plan_tickers = {e.get("ticker") for e in entries}

                # Add state-file stops for tickers not in the plan
                for _ticker, _stop_price in _state_stops.items():
                    if _ticker not in plan_tickers:
                        entries.append({
                            "ticker": _ticker,
                            "stop_price": _stop_price,
                        })
                        logger.info(
                            "Merged state-file stop for %s: $%.2f (not in plan)",
                            _ticker, _stop_price,
                        )
                    else:
                        # Update existing plan entries that have a stale/missing stop
                        for e in entries:
                            if e.get("ticker") == _ticker and not e.get("stop_price"):
                                e["stop_price"] = _stop_price
                                logger.info(
                                    "Updated plan stop for %s from state file: $%.2f",
                                    _ticker, _stop_price,
                                )

                plan["proposed_entries"] = entries
    except Exception as _state_err:
        logger.warning("Failed to merge state-file stops: %s", _state_err)

    # ── Connect to broker ────────────────────────────────────
    broker = None
    try:
        from brokers.registry import get_live_broker
        broker = get_live_broker(config)
        if not broker:
            result["error"] = f"No live broker available for {broker_name}"
            logger.error("get_live_broker returned None for %s", broker_name)
            return result

        if not broker.connect():
            result["error"] = f"Broker connect failed ({broker_name})"
            logger.error("Broker connect failed for %s", market_id)
            return result

        # ── Reconcile deferred fills (entries + exits) ─────────
        # LIMIT orders submitted pre-market fill after market open.
        # Protective stop fills happen asynchronously.  Record any
        # fills that were not captured at submission time.
        try:
            from brokers.live_executor import LiveExecutor
            _exec = LiveExecutor.__new__(LiveExecutor)
            _exec._broker = broker
            _exec._connected = True
            _exec.config = config

            reconciled_entries = _exec.reconcile_entry_fills(plan=plan)
            if reconciled_entries:
                logger.info(
                    "Reconciled %d deferred entry fills for %s",
                    len(reconciled_entries), market_id,
                )
                result["reconciled_fills"] = len(reconciled_entries)

            reconciled_exits = _exec.reconcile_exit_fills()
            if reconciled_exits:
                logger.info(
                    "Reconciled %d deferred exit fills for %s",
                    len(reconciled_exits), market_id,
                )
                result["reconciled_exits"] = len(reconciled_exits)
        except Exception as _recon_exc:
            logger.warning("Fill reconciliation failed (non-fatal): %s", _recon_exc)

        # ── Cancel orphaned orders (no matching position) ─────
        # When a trailing stop fills, the position exits but other
        # open orders for that ticker may remain.  Clean them up
        # on every sync cycle to prevent orphan accumulation.
        try:
            open_orders = broker.get_open_orders()
            all_positions = broker.get_positions()
            position_tickers = {p.ticker for p in all_positions}
            orphaned_count = 0

            for order in open_orders:
                order_ticker = getattr(order, 'ticker', None)
                # ── Guard: only cancel PROTECTIVE orders as orphans ──
                # Orphan cleanup targets protective-exit orders (stop-loss,
                # take-profit) left behind after a position closes.
                #
                # Order intent is encoded in client_order_id:
                #   atlas_entry_*  → entry order (BUY or SELL) — never cancel
                #   atlas_exit_*   → manual exit — never cancel as orphan
                #   atlas_stop_*   → protective stop-loss — cancel if no position
                #   atlas_tp_*     → protective take-profit — cancel if no position
                #
                # This replaces the old BUY/SELL side guard, which would break
                # when short strategies add legitimate SELL entry orders.
                client_oid = str(getattr(order, 'client_order_id', ''))
                is_protective = ('atlas_stop' in client_oid
                                 or 'atlas_tp' in client_oid)
                if not is_protective:
                    continue
                # Fetch side for logging only (guard logic uses client_oid above)
                side = getattr(order, 'side', '?')
                if order_ticker and order_ticker not in position_tickers:
                    order_id = getattr(order, 'order_id', None)
                    order_type = getattr(order, 'order_type', '?')
                    if dry_run:
                        logger.info(
                            "[DRY RUN] Would cancel orphaned %s %s order for %s (id=%s)",
                            side, order_type, order_ticker, order_id,
                        )
                    else:
                        logger.warning(
                            "Cancelling orphaned %s %s order for %s (id=%s) — no matching position",
                            side, order_type, order_ticker, order_id,
                        )
                        cancel_result = broker.cancel_order(order_id)
                        if not cancel_result.success:
                            logger.error(
                                "Failed to cancel orphaned order %s for %s: %s",
                                order_id, order_ticker, cancel_result.message,
                            )
                    orphaned_count += 1

            if orphaned_count:
                logger.info("Orphan cleanup: %s%d orphaned orders",
                            "[DRY RUN] " if dry_run else "", orphaned_count)
            _orphans_cancelled = orphaned_count
        except Exception as e:
            logger.warning("Orphan order cleanup failed (non-fatal): %s", e)
            _orphans_cancelled = 0

        # ── Check for stops stuck in "held" status ──────────────
        # Stops may sit in held (e.g., pre-market, non-RTH) for >1 cycle.
        # On second consecutive held observation: cancel so the main sync
        # re-places the order with a fresh GTC submission.
        _held_result: dict = {"resubmitted": [], "newly_held": [], "errors": []}
        try:
            _held_result = _handle_held_stops(
                broker,
                market_id,
                dry_run=dry_run,
                send_telegram=True,
                state_tickers=state_tickers,  # P0-3: scope to this market only
            )
            if _held_result["resubmitted"]:
                logger.info(
                    "Held-stop resubmit: %d cancelled — sync will re-place: %s",
                    len(_held_result["resubmitted"]), _held_result["resubmitted"],
                )
            if _held_result["newly_held"]:
                logger.info(
                    "Held-stop first-cycle: %s — will resubmit next cycle if still held",
                    _held_result["newly_held"],
                )
        except Exception as _held_exc:
            logger.warning("_handle_held_stops failed (non-fatal): %s", _held_exc)

        # ── Sync protective orders ────────────────────────────

        if broker_name == "alpaca":
            # ── Filter broker positions to this market's state-file tickers ────
            # ROOT CAUSE of P0-1 duplicate inserts: sp500 AND commodity_etfs syncs
            # both fetched all 7 broker positions and called sync_all_protective_orders
            # on the same tickers simultaneously → race → duplicate stops/errors.
            # Fix: only process positions whose ticker is in THIS market's state file.
            _raw_positions = broker.get_positions()
            if state_tickers:
                my_market_positions = [p for p in _raw_positions if p.ticker in state_tickers]
            else:
                # Empty or missing state file → no positions to process (safe default).
                my_market_positions = []
            logger.info(
                "%s: %d state-file tickers, %d matching broker positions (of %d total)",
                market_id.upper(), len(state_tickers), len(my_market_positions), len(_raw_positions),
            )

            if not my_market_positions:
                logger.info("No live positions in %s — nothing to protect", market_id)
                result["counts"] = {"positions_checked": 0, "orphans_cancelled": _orphans_cancelled}
                return result

            # ── PDT backoff: skip tickers deferred during RTH (P1-13) ────────
            # PDT rule on <$25k accounts rejects stop-sells placed on same-day entries.
            # Rather than hammering Alpaca every 15 min and getting 741 error lines,
            # we skip PDT-deferred tickers during RTH and only retry pre-market
            # (00:00–13:59 UTC), when the 'same day' restriction has cleared.
            _pdt_state = _load_pdt_state()
            _now_utc = datetime.utcnow()
            _pdt_skipped: list[str] = []
            _sync_positions: list = []
            for _pos in my_market_positions:
                if not _is_pdt_retry_window(_now_utc) and _pdt_should_skip(_pos.ticker, market_id, _pdt_state):
                    logger.info(
                        "PDT skip %s (%s) — stop deferred during RTH; retry pre-market "
                        "(before %02d:00 UTC)",
                        _pos.ticker, market_id, _PDT_RETRY_BEFORE_UTC_HOUR,
                    )
                    _pdt_skipped.append(_pos.ticker)
                else:
                    _sync_positions.append(_pos)
            if _pdt_skipped:
                logger.info(
                    "%s: %d PDT-deferred ticker(s) skipped (RTH): %s",
                    market_id.upper(), len(_pdt_skipped), _pdt_skipped,
                )

            positions = _sync_positions
            if not positions:
                logger.info(
                    "All %s in-scope positions are PDT-deferred — nothing to sync this cycle",
                    market_id.upper(),
                )
                result["counts"] = {
                    "positions_checked": len(my_market_positions),
                    "pdt_deferred": len(_pdt_skipped),
                    "sl_placed": 0, "tp_placed": 0, "sl_already_exists": 0,
                    "tp_already_exists": 0, "sl_skipped": 0, "tp_skipped": 0,
                    "errors": 0, "orphans_cancelled": _orphans_cancelled,
                }
                return result

            logger.info("%d live positions in %s", len(positions), market_id)

            sync_result = broker.sync_all_protective_orders(
                positions=positions,
                plan=plan,
                trade_date=trade_date,
                dry_run=dry_run,
            )

            # Normalise Alpaca result shape → script-expected shape
            result["counts"] = {
                "positions_checked": len(positions),
                "sl_placed": sync_result.get("sl_placed", 0),
                "sl_already_exists": sync_result.get("sl_already_exists", 0),
                "tp_placed": sync_result.get("tp_placed", 0),
                "tp_already_exists": sync_result.get("tp_already_exists", 0),
                "sl_skipped": 0,
                "tp_skipped": 0,
                "errors": sync_result.get("errors", 0),
                "pdt_deferred": sync_result.get("pdt_deferred", 0),
                "orphans_cancelled": _orphans_cancelled,
            }
            # Convert per_ticker → results with summary strings
            per_ticker = sync_result.get("per_ticker", {})

            # ── Update PDT deferred state ─────────────────────────────────────
            # Record newly PDT-deferred tickers so next RTH cycle skips them;
            # clear entries whose stop was successfully placed this cycle.
            try:
                _pdt_now_iso = _now_utc.isoformat()
                _pdt_changed = False
                for _t, _td in per_ticker.items():
                    _sl = _td.get("sl_action") or _td.get("action", "")
                    _key = f"{_t}::{market_id}"
                    if _sl == "pdt_deferred":
                        _prev = _pdt_state.get(_key, {})
                        _pdt_state[_key] = {
                            "first_seen": _prev.get("first_seen", _pdt_now_iso),
                            "last_retry": _pdt_now_iso,
                            "retry_count": _prev.get("retry_count", 0) + 1,
                            "market_id": market_id,
                        }
                        _pdt_changed = True
                        logger.info(
                            "PDT: recorded deferral for %s (%s), retry_count=%d",
                            _t, market_id, _pdt_state[_key]["retry_count"],
                        )
                    elif _key in _pdt_state and _sl not in ("error",):
                        _pdt_state.pop(_key)
                        _pdt_changed = True
                        logger.info(
                            "PDT: cleared deferral for %s (%s) — stop placed", _t, market_id,
                        )
                if _pdt_changed and not dry_run:
                    _save_pdt_state(_pdt_state)
            except Exception as _pdt_upd_exc:
                logger.warning("PDT state update failed (non-fatal): %s", _pdt_upd_exc)

            for ticker, tdata in per_ticker.items():
                sl_action = tdata.get("sl_action") or tdata.get("action", "unknown")
                tp_action = tdata.get("tp_action", "")

                # Build SL part of summary
                if sl_action == "trailing_placed":
                    sl_part = (
                        f"Trailing stop placed trail=${tdata.get('trail_distance', 0):.2f} "
                        f"qty={tdata.get('qty', '?')} (GTC)"
                    )
                elif sl_action == "placed":
                    sl_part = (
                        f"SL placed @ ${tdata.get('stop_price', 0):.2f} "
                        f"qty={tdata.get('qty', '?')} (GTC)"
                    )
                elif sl_action in ("skipped",):
                    sl_part = "SL exists"
                elif sl_action in ("dry_run_placed", "dry_run_trailing"):
                    trail = tdata.get("trail_distance")
                    if trail:
                        sl_part = (
                            f"[DRY RUN] Trailing stop trail=${trail:.2f} "
                            f"qty={tdata.get('qty', '?')}"
                        )
                    else:
                        sl_part = (
                            f"[DRY RUN] SL @ ${tdata.get('stop_price', 0):.2f} "
                            f"qty={tdata.get('qty', '?')}"
                        )
                elif sl_action == "pdt_deferred":
                    sl_part = "⏳ PDT deferred (same-day entry — stop will be placed pre-market tomorrow)"
                elif sl_action == "error":
                    sl_part = f"SL ERROR: {tdata.get('sl_message') or tdata.get('message', '?')}"
                    tdata["errors"] = [tdata.get("sl_message") or tdata.get("message", "unknown")]
                else:
                    sl_part = f"SL {sl_action}"

                # Build TP part of summary
                tp_part = ""
                if tp_action == "placed":
                    tp_part = f"TP placed @ ${tdata.get('take_profit', 0):.2f}"
                elif tp_action == "skipped" and tdata.get("tp_reason") == "tp_exists":
                    tp_part = "TP exists"
                elif tp_action == "skipped":
                    tp_part = "TP skipped (no target)"
                elif tp_action == "dry_run_placed":
                    tp_part = f"[DRY RUN] TP @ ${tdata.get('take_profit', 0):.2f}"
                elif tp_action == "pdt_deferred":
                    tp_part = ""  # already captured in sl_part for trailing stops
                elif tp_action == "error":
                    tp_part = f"TP ERROR: {tdata.get('tp_message', '?')}"
                    tdata.setdefault("errors", []).append(
                        tdata.get("tp_message", "unknown")
                    )

                parts = [p for p in [sl_part, tp_part] if p]
                tdata["summary"] = (
                    f"{ticker}: {' | '.join(parts)}" if parts else f"{ticker}: nothing to do"
                )
            result["results"] = per_ticker

        else:
            result["error"] = f"Unsupported broker: {broker_name}"
            return result

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
            from utils.telegram import tg_escape as _tge
            lines.append(f"❌ <b>{market}</b>: {_tge(error)}")
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
        pdt_deferred = counts.get("pdt_deferred", 0)
        sl_skip = counts.get("sl_skipped", 0)
        tp_skip = counts.get("tp_skipped", 0)
        orphans_cancelled = counts.get("orphans_cancelled", 0)

        icon = "❌" if errors else ("✅" if (sl_placed + tp_placed) > 0 else "ℹ️")
        if pdt_deferred and not errors and not (sl_placed + tp_placed):
            icon = "⏳"
        lines.append(
            f"{icon} <b>{market}</b> ({n_checked} positions)\n"
            f"  SL: {sl_placed} placed | {sl_exists} existed | {sl_skip} skipped\n"
            f"  TP: {tp_placed} placed | {tp_exists} existed | {tp_skip} skipped"
            + (f"\n  ⚠️ {errors} errors" if errors else "")
            + (f"\n  ⏳ {pdt_deferred} PDT-deferred (same-day entries, account &lt; $25k — "
               f"stops placed pre-market tomorrow)" if pdt_deferred else "")
            + (f"\n  🚫 {orphans_cancelled} orphaned orders cancelled" if orphans_cancelled else "")
        )

        # Per-ticker detail
        for ticker, tresult in r.get("results", {}).items():
            errs = tresult.get("errors", [])
            if errs:
                from utils.telegram import tg_escape as _tge
                for e in errs:
                    lines.append(f"  └─ {ticker}: ⚠️ {_tge(e)}")
            elif tresult.get("sl_action") == "pdt_deferred":
                lines.append(f"  └─ {ticker}: ⏳ PDT deferred — stop placed tomorrow pre-market")

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
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress output when all positions already protected (for frequent cron). "
             "Still logs and alerts on errors or new placements.",
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
        # pdt_deferred is a regulatory constraint, not an operational error

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
            if not args.quiet:
                print(f"  {market}: no live positions")
            continue
        sl_placed = counts.get("sl_placed", 0)
        tp_placed = counts.get("tp_placed", 0)
        errs = counts.get("errors", 0)
        anything_happened = sl_placed > 0 or tp_placed > 0 or errs > 0

        if not args.quiet or anything_happened:
            print(
                f"  {market}: {n_checked} positions checked | "
                f"SL placed={sl_placed} | TP placed={tp_placed} | errors={errs}"
            )
            for ticker, tresult in r.get("results", {}).items():
                print(f"    {tresult.get('summary', ticker)}")

    # In quiet mode, skip telegram/summary when all positions were already protected
    total_placed = sum(
        r.get("counts", {}).get("sl_placed", 0) + r.get("counts", {}).get("tp_placed", 0)
        for r in market_results
    )
    total_errors = sum(
        r.get("counts", {}).get("errors", 0)
        for r in market_results
    )
    nothing_to_report = args.quiet and total_placed == 0 and total_errors == 0

    if not nothing_to_report:
        print()

    # ── Telegram ─────────────────────────────────────────────
    # In quiet mode: only send telegram if stops were placed or errors occurred
    if not args.no_telegram and not nothing_to_report:
        ok = send_telegram_summary(market_results, trade_date, dry_run)
        if ok:
            logger.info("Telegram notification sent")
        else:
            logger.warning("Telegram notification failed (non-fatal)")
    elif nothing_to_report:
        logger.debug("Quiet mode: all positions protected, nothing to report")

    logger.info("=== sync_protective_orders done (errors=%s) ===", any_error)
    return 1 if any_error else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        # Top-level crash guard — alert via Telegram so cron failures aren't silent
        try:
            from utils.telegram import send_message
            from utils.telegram import tg_escape as _tge
            send_message(
                f"🚨 <b>sync_protective_orders CRASHED</b>\n\n"
                f"<pre>{_tge(type(exc).__name__)}: {_tge(str(exc)[:500])}</pre>\n\n"
                f"Check logs/sync_protective.log"
            )
        except Exception:
            pass
        raise
