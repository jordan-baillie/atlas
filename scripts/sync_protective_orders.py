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
import sqlite3
import os
import sys
import time
from datetime import date, datetime
from pathlib import Path

# ── Project root on path ─────────────────────────────────────
PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

from utils.logging_config import setup_logging  # noqa: E402

logger = logging.getLogger("atlas.sync_protective_orders")

from brokers.routing_policy import BrokerRoutingPolicy

# PDT backoff: expiry-based check (wired C3 — covers pre-market same-day denials)
from brokers.pdt_state import (  # noqa: E402
    is_pdt_deferred,
    set_pdt_deferred,
    clear_expired as _clear_pdt_expired,
    _rth_close_today,
)

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

# ── Phase B.0: Protective ledger feature flag ───────────────
def _protective_ledger_enabled() -> bool:
    """Return True if position_protective_orders ledger writes are enabled.

    Controlled by env var PROTECTIVE_LEDGER_WRITE_ENABLED (default: true).
    Set to 'false', '0', or 'no' to disable all Phase B.0 ledger writes
    without touching order flow.  Allows instant rollback if issues arise.
    """
    val = os.environ.get("PROTECTIVE_LEDGER_WRITE_ENABLED", "true").lower()
    return val not in ("false", "0", "no")


# 00:00–13:59 UTC ≈ before US market open — same-day restriction has cleared overnight.
_PDT_RETRY_BEFORE_UTC_HOUR = 14


# ═══════════════════════════════════════════════════════════════
# Config loading
# ═══════════════════════════════════════════════════════════════

def load_config(market_id: str, config_path: str = "") -> dict:
    """Load Atlas config for the given market.

    If config_path is provided, reads that exact file (raw JSON, no overrides) —
    used by tests and CLI overrides. Otherwise consults canonical loader with
    overrides applied.
    """
    if config_path:
        path = Path(config_path)
        if not path.exists():
            raise FileNotFoundError(f"Config not found: {path}")
        with open(path) as f:
            return json.load(f)
    from utils.config import get_active_config
    return get_active_config(market_id)


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
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Could not read held-stop state file %s: %s", path, exc)
    return {}


def _save_held_state(state: dict, state_file: Path | None = None) -> None:
    """Persist held-stop state to JSON file."""
    path = state_file or _HELD_STATE_FILE
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w") as f:
            json.dump(state, f, indent=2)
    except (OSError, TypeError) as exc:
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
            f"🚨 <b>Stop stuck-held for {_tge(ticker)}</b>\n"
            f"Market: {market_id.upper()}\n"
            f"Reason: <code>{_tge(reason)}</code>\n"
            f"Status: {status_line}\n"
            f"<i>Manual intervention needed — not resubmitting further today.</i>"
        )
        send_message(msg)
    except (ImportError, OSError, ConnectionError, RuntimeError) as tg_exc:  # Telegram non-fatal
        logger.warning("_maybe_alert_stuck: Telegram alert failed (non-fatal): %s", tg_exc)
    return True


def _wait_for_cancel_confirm(
    broker,
    order_id: str,
    timeout_sec: float | None = None,
    poll_interval_sec: float = 0.25,
) -> bool:
    """Poll broker until a cancel is confirmed or timeout elapses.

    Addresses the Alpaca 40310000 "insufficient qty" race: after calling
    ``broker.cancel_order(order_id)`` the cancellation may still be in
    ``pending_cancel`` state at Alpaca's side.  If a replacement order is
    placed immediately, Alpaca rejects it because the old order still
    "holds" the position quantity.  Polling until the status is terminal
    eliminates the race.

    Args:
        broker:            Connected broker instance (needs ``get_order_status``).
        order_id:          The order ID that was just cancelled.
        timeout_sec:       Max seconds to wait.  Defaults to the value of
                           ``ATLAS_SYNC_PROTECTIVE_CANCEL_TIMEOUT_SEC`` env var
                           (default 5.0).  Pass explicitly in tests to avoid
                           reading the env var.
        poll_interval_sec: Seconds between polls (default 0.25 s = 250 ms).

    Returns:
        True   — cancel confirmed: status is CANCELLED (covers canceled/expired/
                 replaced/stopped) or FAILED (covers rejected/suspended).
        False  — order FILLED before cancel settled (race lost; position may
                 have exited) OR timeout elapsed without confirmation.
    """
    from brokers.base import OrderStatus  # lazy import — avoids circular deps

    if timeout_sec is None:
        timeout_sec = float(
            os.environ.get("ATLAS_SYNC_PROTECTIVE_CANCEL_TIMEOUT_SEC", "5.0")
        )

    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        try:
            result = broker.get_order_status(order_id)
        except Exception as poll_exc:  # noqa: BLE001 — broker RPC can raise any exception
            logger.warning(
                "_wait_for_cancel_confirm: get_order_status(%s) failed: %s — retrying",
                order_id, poll_exc,
            )
            time.sleep(poll_interval_sec)
            continue

        status = result.status
        if status in (OrderStatus.CANCELLED, OrderStatus.FAILED):
            logger.debug(
                "_wait_for_cancel_confirm: order %s confirmed terminal (status=%s)",
                order_id, status.value,
            )
            return True
        if status == OrderStatus.FILLED:
            logger.warning(
                "_wait_for_cancel_confirm: order %s FILLED before cancel confirmed "
                "— race lost (position may have exited before stop was cancelled)",
                order_id,
            )
            return False

        # PENDING / SUBMITTED / UNKNOWN — still settling; wait and retry
        time.sleep(poll_interval_sec)

    logger.error(
        "_wait_for_cancel_confirm: order %s did not reach terminal status within "
        "%.1fs — skipping placement this cycle to avoid 40310 race "
        "(will retry next cron pass)",
        order_id, timeout_sec,
    )
    return False


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
    except Exception as exc:  # noqa: BLE001 — broker call can raise any exception
        logger.warning("_handle_held_stops: get_open_orders failed (non-fatal): %s", exc)
        return {"resubmitted": resubmitted, "newly_held": newly_held, "errors": errors}

    # Identify stop SELL orders with raw status == "held"
    # Capture the full raw dict per ticker so we can inspect reject_reason.
    # CRITICAL: skip OCO/bracket/OTO child legs — Alpaca OCO stop legs are
    # PERMANENTLY status=HELD by design (the stop only activates if the TP
    # limit doesn't fill). Treating these as "stuck" causes cancel+resubmit
    # loops every 15-min cycle. See commit 35d2286a (OCO migration) + fix commit.
    currently_held: dict[str, dict] = {}   # ticker → {"order_id": str, "raw": dict, "status": str}
    for order in open_orders:
        raw = getattr(order, "raw", {}) or {}
        order_status = raw.get("status", "")
        order_type = raw.get("order_type", "")
        side = raw.get("side", "")
        ticker = getattr(order, "ticker", "") or ""
        order_class = (raw.get("order_class") or "").lower()
        # Skip OCO / bracket / OTO child legs — HELD is normal for these.
        if order_class in ("oco", "bracket", "oto"):
            continue
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
            # Phase 2B: wait for cancel to fully settle at the broker before returning.
            # Without this, sync_all_protective_orders (called immediately after this
            # function) sees no stop for the position → tries to place one → Alpaca
            # 40310000 "insufficient qty" because the cancel is still in
            # pending_cancel state and the shares are allocated against the old order.
            # Env override: ATLAS_SYNC_PROTECTIVE_CANCEL_TIMEOUT_SEC (default 5.0 s).
            if not _wait_for_cancel_confirm(broker, order_id):
                logger.error(
                    "_handle_held_stops: cancel confirmation failed for %s "
                    "(order_id=%s) — skipping resubmit this cycle to avoid "
                    "40310 race (will retry next cron pass)",
                    ticker, order_id,
                )
                _maybe_alert_stuck(
                    ticker, market_id,
                    reason="cancel_confirm_timeout",
                    state=state, key=key,
                    send_telegram=send_telegram, permanent=False, now_iso=_now,
                )
                errors.append(ticker)
                continue  # NOT added to resubmitted — sync won't race-place this cycle
            resubmitted.append(ticker)
            # IMPORTANT: do NOT pop entry from state anymore — retry_count must
            # persist across cycles to enforce the cap. Cleanup happens only
            # when the ticker is no longer in currently_held (resolved_keys
            # block below).
            if send_telegram:
                try:
                    from utils.telegram import send_message, tg_escape as _tge
                    send_message(
                        f"⚠️ Resubmitted stuck <code>held</code> stop for "
                        f"<b>{_tge(ticker)}</b>\n"
                        f"Market: {market_id.upper()} | "
                        f"Order: <code>{_tge(order_id[:16])}</code> | "
                        f"Retry: {entry['retry_count']}/{_HELD_MAX_RETRIES}"
                    )
                except (ImportError, OSError, ConnectionError, RuntimeError) as tg_exc:  # Telegram non-fatal
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
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Could not read PDT state file %s: %s", path, exc)
    return {}


def _save_pdt_state(state: dict, state_file: Path | None = None) -> None:
    """Persist PDT-deferral state to JSON file."""
    path = state_file or _PDT_STATE_FILE
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w") as f:
            json.dump(state, f, indent=2)
    except (OSError, TypeError) as exc:
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
# DB consistency helper — persist new operative order IDs
# ═══════════════════════════════════════════════════════════════

# Actions from sync_all_protective_orders that place a NEW operative order.
# Used by _apply_db_consistency to decide which tickers need a DB update.
_DB_UPDATE_ACTIONS: frozenset[str] = frozenset({
    "oco_placed", "tightened", "placed_pdt_fallback",
    "placed_fallback", "trailing_upgraded",
})


def _apply_db_consistency(broker, market_id: str, sync_result: dict, pass_label: str = "live") -> None:
    """Persist new operative stop/tp order IDs to trades/paper_trades table after a sync cycle.

    Args:
        pass_label: "live" (default) writes to trades + position_protective_orders;
                    "paper" writes to paper_trades + paper_position_protective_orders.

    Reads per_ticker from sync_result, fetches current open orders from broker
    (OCO child legs already flattened by broker.get_open_orders), resolves the
    operative SELL-stop and SELL-limit IDs per ticker, then writes them to
    trades.stop_order_id / trades.tp_order_id for any ticker whose action
    placed a NEW order (oco_placed, tightened, placed_pdt_fallback, …).

    Skipped, pdt_deferred, dry_run, and error actions are no-ops.
    Non-fatal — any exception is logged as WARNING and swallowed.
    """
    try:
        from db.atlas_db import update_trade_protective_orders
        from brokers.base import OrderSide

        per_ticker: dict = sync_result.get("per_ticker") or {}
        if not per_ticker:
            return

        # Fetch all open orders; broker.get_open_orders() already includes
        # OCO child legs (stop leg that Alpaca marks HELD inside the parent).
        open_orders = broker.get_open_orders()

        # Build ticker → {stop: id, tp: id} by scanning all SELL orders.
        # First match wins per slot so we don't overwrite with a second order.
        ticker_ops: dict[str, dict[str, str]] = {}
        for order in open_orders or []:
            if order.side != OrderSide.SELL:
                continue
            otype = (order.raw or {}).get("order_type", "").lower()
            ticker = order.ticker
            if not ticker:
                continue
            slot = ticker_ops.setdefault(ticker, {"stop": "", "tp": ""})
            if otype in ("stop", "stop_limit", "trailing_stop") and not slot["stop"]:
                slot["stop"] = order.order_id
            elif otype == "limit" and not slot["tp"]:
                slot["tp"] = order.order_id

        # Update DB for tickers that received a NEW operative order this cycle.
        for ticker, tres in per_ticker.items():
            sl_action = tres.get("sl_action") or ""
            tp_action = tres.get("tp_action") or ""
            if (sl_action not in _DB_UPDATE_ACTIONS
                    and tp_action not in _DB_UPDATE_ACTIONS):
                continue
            ops = ticker_ops.get(ticker, {"stop": "", "tp": ""})
            # None = leave field unchanged; empty string = no resolved ID (skip arg)
            new_stop: str | None = ops["stop"] or None
            new_tp: str | None = ops["tp"] or None
            if not new_stop and not new_tp:
                logger.debug(
                    "sync_protective DB: no operative IDs resolved for %s/%s",
                    ticker, market_id,
                )
                continue
            try:
                if pass_label == "paper":
                    from db.atlas_db import update_paper_trade_protective_orders as _uptp
                    n = _uptp(
                        ticker=ticker,
                        universe=market_id,
                        stop_order_id=new_stop,
                        tp_order_id=new_tp,
                    )
                else:
                    n = update_trade_protective_orders(
                        ticker=ticker,
                        universe=market_id,
                        stop_order_id=new_stop,
                        tp_order_id=new_tp,
                    )
                logger.info(
                    "sync_protective DB update [%s]: %s/%s stop=%s tp=%s rows=%d ",
                    pass_label, ticker, market_id,
                    (new_stop or "")[:8], (new_tp or "")[:8], n,
                )
            except sqlite3.Error as db_exc:  # DB update; only sqlite3 errors escape atlas_db
                logger.warning(
                    "sync_protective DB update failed for %s/%s (non-fatal): %s",
                    ticker, market_id, db_exc,
                )

            # Phase B.0 (c): also upsert position_protective_orders with same IDs
            if _protective_ledger_enabled():
                try:
                    if pass_label == "paper":
                        from db.atlas_db import upsert_paper_protective_record as _upr_dbc
                    else:
                        from db.atlas_db import upsert_protective_record as _upr_dbc
                    _tres = per_ticker[ticker]
                    _qty = int(_tres.get("qty", 0) or 0)
                    _sp = _tres.get("stop_price")
                    _tp = _tres.get("take_profit")
                    _sp_f = float(_sp) if _sp is not None else None
                    _tp_f = float(_tp) if _tp is not None else None
                    _oco = "oco" if _tp_f else "stop"
                    _upr_dbc(
                        market_id=market_id,
                        ticker=ticker,
                        trade_id=None,
                        position_qty=_qty,
                        stop_order_id=new_stop,
                        stop_price=_sp_f,
                        tp_order_id=new_tp,
                        tp_price=_tp_f,
                        oco_class=_oco,
                    )
                    logger.debug(
                        "Protective ledger (c): upserted %s/%s stop=%s tp=%s",
                        ticker, market_id,
                        (new_stop or "")[:8], (new_tp or "")[:8],
                    )
                except (ImportError, sqlite3.Error, AttributeError, ValueError) as _prot_dbc_exc:  # DB upsert
                    logger.warning(
                        "Protective ledger upsert (DB consistency) failed for %s/%s (non-fatal): %s",
                        ticker, market_id, _prot_dbc_exc,
                    )

        # Phase B.0 (a): upsert for ALL tickers with resolved IDs (including "skipped"/existing).
        # This updates last_synced_at on every sync cycle so freshness is trackable.
        if _protective_ledger_enabled():
            try:
                if pass_label == "paper":
                    from db.atlas_db import upsert_paper_protective_record as _upr_all
                else:
                    from db.atlas_db import upsert_protective_record as _upr_all
                for _all_ticker, _all_tres in per_ticker.items():
                    _all_ops = ticker_ops.get(_all_ticker, {"stop": "", "tp": ""})
                    _all_stop = _all_ops["stop"] or None
                    _all_tp = _all_ops["tp"] or None
                    if not _all_stop and not _all_tp:
                        continue  # no resolved IDs at broker → skip
                    _all_qty = int(_all_tres.get("qty", 0) or 0)
                    _all_sp = _all_tres.get("stop_price")
                    _all_tpp = _all_tres.get("take_profit")
                    _all_sp_f = float(_all_sp) if _all_sp is not None else None
                    _all_tp_f = float(_all_tpp) if _all_tpp is not None else None
                    try:
                        _upr_all(
                            market_id=market_id,
                            ticker=_all_ticker,
                            trade_id=None,
                            position_qty=_all_qty,
                            stop_order_id=_all_stop,
                            stop_price=_all_sp_f,
                            tp_order_id=_all_tp,
                            tp_price=_all_tp_f,
                            oco_class="oco" if _all_tp else "stop",
                        )
                        logger.debug(
                            "Protective ledger (a): refreshed %s/%s stop=%s tp=%s",
                            _all_ticker, market_id,
                            (_all_stop or "")[:8], (_all_tp or "")[:8],
                        )
                    except (sqlite3.Error, AttributeError, ValueError) as _upr_all_exc:  # DB upsert loop
                        logger.warning(
                            "Protective ledger refresh (a) failed for %s/%s (non-fatal): %s",
                            _all_ticker, market_id, _upr_all_exc,
                        )
            except (ImportError, sqlite3.Error, AttributeError) as _prot_all_exc:  # import or batch DB fail
                logger.warning(
                    "Protective ledger batch refresh failed (non-fatal): %s", _prot_all_exc,
                )

    except Exception as wrap_exc:  # noqa: BLE001 — outer DB consistency block catches all
        logger.warning(
            "sync_protective DB consistency block failed (non-fatal): %s", wrap_exc,
        )

# ═══════════════════════════════════════════════════════════════
# Paper-pass sync helper (dual-pass routing)
# ═══════════════════════════════════════════════════════════════


def _run_paper_sync_pass(
    market_id: str,
    trade_date: str,
    *,
    dry_run: bool = False,
    config_path: str = "",
    base_config: dict | None = None,
) -> dict:
    """Run a protective-order sync pass for PAPER positions.

    Uses the paper Alpaca account (mode="paper") and reads positions from
    ``brokers/state/paper_{market_id}.json`` (or ``paper_trades`` table if
    the state file is absent). Writes protective records to
    ``paper_position_protective_orders`` and order IDs to ``paper_trades``.

    Called by :func:`sync_market` when there is at least one open paper trade
    for *market_id*. Non-fatal: any exception is logged and an error dict is
    returned so the live pass result is not affected.

    Args:
        market_id:   Universe / market identifier (e.g. ``"sp500"``).
        trade_date:  YYYY-MM-DD string.
        dry_run:     If True, no orders are placed.
        config_path: Override config file path (used in tests).
        base_config: Pre-loaded base config (avoids re-reading config file).

    Returns:
        Result dict compatible with the *per-market* result shape.
    """
    result: dict = {
        "market_id": market_id,
        "pass_label": "paper",
        "trade_date": trade_date,
        "dry_run": dry_run,
        "counts": {},
        "results": {},
        "error": "",
    }

    try:
        # ── Build paper config ────────────────────────────────
        if base_config is None:
            base_config = load_config(market_id, config_path)
        _base_policy = BrokerRoutingPolicy(base_config, market_id=market_id)
        paper_config: dict = _base_policy.paper_config

        logger.info("[PAPER] Syncing protective orders for %s (dry_run=%s)", market_id, dry_run)

        # ── Load paper plan (same plan file — stop prices are strategy-level) ──
        paper_plan = load_plan(market_id, trade_date)

        # ── Get state-file tickers for paper positions ───────────────────
        paper_state_path = PROJECT / "brokers" / "state" / f"paper_{market_id}.json"
        paper_state_tickers: set[str] = set()

        if paper_state_path.exists():
            try:
                import json as _json
                with open(paper_state_path) as _psf:
                    _ps = _json.load(_psf)
                paper_state_tickers = {
                    _pp.get("ticker", "") for _pp in _ps.get("positions", [])
                    if _pp.get("ticker")
                }
                # Merge state-file stops into plan
                _state_stops = {
                    _pp.get("ticker"): _pp.get("stop_price", 0)
                    for _pp in _ps.get("positions", [])
                    if _pp.get("ticker") and _pp.get("stop_price", 0)
                }
                if _state_stops:
                    if paper_plan is None:
                        paper_plan = {"proposed_entries": []}
                    entries = paper_plan.get("proposed_entries", [])
                    plan_tickers = {e.get("ticker") for e in entries}
                    for _t, _sp in _state_stops.items():
                        if _t not in plan_tickers:
                            entries.append({"ticker": _t, "stop_price": _sp})
                    paper_plan["proposed_entries"] = entries
            except Exception as _ps_err:
                logger.warning("[PAPER] Could not read paper state file: %s", _ps_err)
        else:
            # Fall back to paper_trades DB to discover paper tickers
            try:
                from db.atlas_db import get_open_paper_trades
                paper_rows = get_open_paper_trades()
                paper_state_tickers = {
                    r.get("ticker", "") for r in paper_rows
                    if r.get("universe") == market_id and r.get("ticker")
                }
                # Build minimal plan entries from paper_trades stop prices
                if paper_state_tickers:
                    if paper_plan is None:
                        paper_plan = {"proposed_entries": []}
                    entries = paper_plan.get("proposed_entries", [])
                    plan_tickers = {e.get("ticker") for e in entries}
                    for row in paper_rows:
                        t = row.get("ticker", "")
                        sp = row.get("stop_price")
                        if t and sp and t not in plan_tickers:
                            entries.append({"ticker": t, "stop_price": sp})
                    paper_plan["proposed_entries"] = entries
            except Exception as _pt_err:
                logger.warning("[PAPER] Could not read paper_trades: %s", _pt_err)

        if not paper_state_tickers:
            logger.info("[PAPER] No paper positions found for %s — skipping paper pass", market_id)
            result["counts"] = {"positions_checked": 0}
            return result

        # ── Connect to paper broker ───────────────────────────
        from brokers.registry import get_live_broker
        paper_broker = get_live_broker(paper_config)
        if not paper_broker:
            result["error"] = "No paper broker available"
            logger.error("[PAPER] get_live_broker returned None for paper config")
            return result

        if not paper_broker.connect():
            result["error"] = "Paper broker connect failed"
            logger.error("[PAPER] Paper broker connect failed for %s", market_id)
            return result

        try:
            # ── Get paper broker positions filtered to paper state ────────
            all_paper_positions = paper_broker.get_positions()
            paper_positions = [p for p in all_paper_positions if p.ticker in paper_state_tickers]
            logger.info(
                "[PAPER] %s: %d paper state tickers, %d matching paper broker positions",
                market_id.upper(), len(paper_state_tickers), len(paper_positions),
            )

            if not paper_positions:
                logger.info("[PAPER] No paper broker positions for %s — skipping sync", market_id)
                result["counts"] = {"positions_checked": 0}
                return result

            paper_sync_result = paper_broker.sync_all_protective_orders(
                positions=paper_positions,
                plan=paper_plan,
                trade_date=trade_date,
                dry_run=dry_run,
            )

            result["counts"] = {
                "positions_checked": len(paper_positions),
                "sl_placed": paper_sync_result.get("sl_placed", 0),
                "sl_already_exists": paper_sync_result.get("sl_already_exists", 0),
                "tp_placed": paper_sync_result.get("tp_placed", 0),
                "tp_already_exists": paper_sync_result.get("tp_already_exists", 0),
                "errors": paper_sync_result.get("errors", 0),
            }
            result["results"] = paper_sync_result.get("per_ticker", {})

            if not dry_run:
                _apply_db_consistency(paper_broker, market_id, paper_sync_result, pass_label="paper")

        finally:
            try:
                paper_broker.disconnect()
            except Exception as _disc_exc:
                logger.debug("[PAPER] Broker disconnect error (non-fatal): %s", _disc_exc)

    except Exception as _outer_exc:
        result["error"] = str(_outer_exc)
        logger.error("[PAPER] Paper sync pass failed for %s: %s", market_id, _outer_exc, exc_info=True)

    return result


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
    policy = BrokerRoutingPolicy(config, market_id=market_id)
    _mode_label = f"[{policy.mode.upper()}]"

    if policy.should_skip():
        result["error"] = f"policy.should_skip() True (mode={policy.mode}, live_enabled={policy.live_enabled}) — skipping {market_id}"
        logger.info("Skipping %s: policy.should_skip() True", market_id)
        try:
            from monitor.health_writer import heartbeat as _hb
            _hb("sync_protective_orders", "skipped", {"market": market_id, "reason": "market_disabled"})
        except Exception:
            pass
        return result

    logger.info(
        "%s Syncing %s via %s broker (dry_run=%s)",
        _mode_label, market_id.upper(), broker_name, dry_run,
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
    except Exception as _state_err:  # noqa: BLE001 — state merge block touches file+dict ops
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
            _exec._mode = config.get("trading", {}).get("mode", "live")
            # ── Initialize policy (required by reconcile_entry_fills / reconcile_exit_fills) ──
            # BrokerRoutingPolicy is imported at module top (line 54) — do NOT re-import
            # locally here; a local import inside a try block would shadow the module-level
            # name and cause UnboundLocalError at the earlier policy= call on line ~1050.
            _exec._policy = BrokerRoutingPolicy(
                config, market_id=config.get("market_id", market_id),
            )

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
        except Exception as _recon_exc:  # noqa: BLE001 — broker+DB reconciliation can raise any exception
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
        except Exception as e:  # noqa: BLE001 — broker position fetch + order cancel can raise any exception
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
        except Exception as _held_exc:  # noqa: BLE001 — inner broker call wrapping
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
            # Clear expired entries from the new expiry-based state at the start
            # of each market cycle (no-op if no entries have expired).
            try:
                _clear_pdt_expired()
            except Exception as _pdt_clr_exc:  # noqa: BLE001 — optional PDT housekeeping
                logger.debug("PDT clear_expired (non-fatal): %s", _pdt_clr_exc)

            for _pos in my_market_positions:
                # Old check: retry-window based (keyed ticker::market_id)
                _old_skip = (
                    not _is_pdt_retry_window(_now_utc)
                    and _pdt_should_skip(_pos.ticker, market_id, _pdt_state)
                )
                # New check: expiry-based (ticker-only key, covers ANY hour today)
                _new_skip = is_pdt_deferred(_pos.ticker)
                if _old_skip or _new_skip:
                    logger.info(
                        "pdt_skip: %s (%s) — PDT deferred until RTH close"
                        " (old=%s new=%s)",
                        _pos.ticker, market_id, _old_skip, _new_skip,
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
                        # Also record in new expiry-based state (ticker-only key)
                        try:
                            set_pdt_deferred(_t, _rth_close_today())
                            logger.info(
                                "pdt_deferred: %s recorded in pdt_state.json until 21:00 UTC",
                                _t,
                            )
                        except (OSError, ValueError, AttributeError, RuntimeError) as _pdt_new_exc:  # pdt_state write
                            logger.debug(
                                "pdt_state set_pdt_deferred (non-fatal): %s", _pdt_new_exc
                            )
                    elif _key in _pdt_state and _sl not in ("error",):
                        _pdt_state.pop(_key)
                        _pdt_changed = True
                        logger.info(
                            "PDT: cleared deferral for %s (%s) — stop placed", _t, market_id,
                        )
                if _pdt_changed and not dry_run:
                    _save_pdt_state(_pdt_state)
            except Exception as _pdt_upd_exc:  # noqa: BLE001 — PDT state update touches file+dict ops
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

            # ── DB consistency: persist new operative order IDs to trades ─────
            # Write newly placed/replaced stop/tp order IDs to trades.stop_order_id
            # / trades.tp_order_id so the SQLite column stays current.
            # Non-fatal — sync succeeds even if the DB update fails.
            if not dry_run:
                _apply_db_consistency(broker, market_id, sync_result)

                # Phase B.0 (b): close protective records for broker-detached positions.
                # state_tickers = all tickers the state file says we own.
                # my_market_positions = those actually at broker (pre-PDT-filter).
                # Difference = positions gone at broker → mark ledger closed.
                if _protective_ledger_enabled() and state_tickers:
                    try:
                        from db.atlas_db import close_protective_record as _cpr_det
                        _broker_tickers = {p.ticker for p in my_market_positions}
                        for _det_ticker in state_tickers - _broker_tickers:
                            try:
                                _cpr_det(market_id=market_id, ticker=_det_ticker)
                                logger.debug(
                                    "Protective ledger (b): closed detached record %s/%s",
                                    _det_ticker, market_id,
                                )
                            except (sqlite3.Error, AttributeError) as _det_exc:  # DB close call
                                logger.warning(
                                    "Protective ledger close (detached) failed for %s/%s "
                                    "(non-fatal): %s",
                                    _det_ticker, market_id, _det_exc,
                                )
                    except (ImportError, sqlite3.Error, AttributeError) as _det_outer_exc:  # outer import+close
                        logger.warning(
                            "Protective ledger detached-position close block failed "
                            "(non-fatal): %s", _det_outer_exc,
                        )

        else:
            result["error"] = f"Unsupported broker: {broker_name}"
            return result

        # Log per-ticker summary
        for ticker, tresult in result["results"].items():
            logger.info("  %s", tresult.get("summary", ticker))

    except Exception as e:  # noqa: BLE001 — sync_market outer catch-all; broker/DB/FS errors
        result["error"] = str(e)
        logger.error("Error syncing %s: %s", market_id, e, exc_info=True)

    finally:
        if broker:
            try:
                broker.disconnect()
            except (RuntimeError, OSError, AttributeError) as e:  # disconnect can fail if connection dropped
                logger.warning("Broker disconnect error during sync-protective cleanup: %s", e)

    # ── Paper pass: dual-pass routing for PAPER lifecycle strategies ─────
    # Runs AFTER the live pass (and its broker is disconnected).  Only if
    # there is at least one open paper trade for this universe.
    if policy.needs_paper_pass():
        logger.info("[PAPER] Open paper trades detected for %s — running paper sync pass", market_id)
        try:
            _paper_result = _run_paper_sync_pass(
                market_id=market_id,
                trade_date=trade_date,
                dry_run=dry_run,
                config_path=config_path,
                base_config=config if 'config' in dir() else None,
            )
            # Merge paper pass counts and results into the main result dict
            result.setdefault("paper_pass", {})
            result["paper_pass"] = _paper_result
            _paper_counts = _paper_result.get("counts", {})
            _live_counts = result.get("counts", {})
            result["counts"] = {k: _live_counts.get(k, 0) + _paper_counts.get(k, 0)
                                 for k in set(_live_counts) | set(_paper_counts)}
            if _paper_result.get("error"):
                logger.warning("[PAPER] Paper pass error: %s", _paper_result["error"])
        except Exception as _pp_exc:  # noqa: BLE001 — paper pass is non-fatal to live pass result
            logger.error("[PAPER] Paper pass crashed (non-fatal to live result): %s", _pp_exc)
    else:
        logger.debug("[PAPER] No open paper trades for %s — skipping paper pass", market_id)

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
        from utils.telegram import tg_escape as _tge
        for ticker, tresult in r.get("results", {}).items():
            errs = tresult.get("errors", [])
            if errs:
                for e in errs:
                    lines.append(f"  └─ {_tge(ticker)}: ⚠️ {_tge(e)}")
            elif tresult.get("sl_action") == "pdt_deferred":
                lines.append(f"  └─ {_tge(ticker)}: ⏳ PDT deferred — stop placed tomorrow pre-market")

        lines.append("")

    if all_ok and not any(r.get("error") for r in market_results):
        lines.append("<i>All positions protected ✓</i>")

    lines.append(
        f"\n<i>Run at {datetime.now().strftime('%H:%M:%S')}</i>"
    )
    return "\n".join(lines)


# TODO(#PERF-TG-CONSOLIDATE): rewrite to use utils.telegram.notify() if formatting can move into caller
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
    except (ImportError, OSError, ConnectionError, RuntimeError) as e:  # Telegram non-fatal
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
    except (ImportError, OSError, AttributeError, RuntimeError) as _setup_e:  # logging setup
        logging.basicConfig(
            level=log_level,
            format="%(asctime)s %(name)s %(levelname)s %(message)s",
        )
        logger.warning("setup_logging failed, using basicConfig fallback: %s", _setup_e)

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

    # ── Heartbeat ─────────────────────────────────────────────────────
    try:
        from db.atlas_db import record_heartbeat
        record_heartbeat(
            "sync_protective_orders",
            "completed",
            {
                "markets_processed": [r["market_id"] for r in market_results],
                "errors": total_errors,
            },
        )
    except Exception as _hb_exc:  # noqa: BLE001 — heartbeat is non-fatal
        logger.debug("sync_protective_orders: heartbeat write failed (non-fatal): %s", _hb_exc)

    return 1 if any_error else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001 — top-level crash guard; must catch all
        # Top-level crash guard — alert via Telegram so cron failures aren't silent
        try:
            from utils.telegram import send_message
            from utils.telegram import tg_escape as _tge
            send_message(
                f"🚨 <b>sync_protective_orders CRASHED</b>\n\n"
                f"<pre>{_tge(type(exc).__name__)}: {_tge(str(exc)[:500])}</pre>\n\n"
                f"Check logs/sync_protective.log"
            )
        except (ImportError, OSError, ConnectionError, RuntimeError) as e:  # Telegram in crash guard
            logger.warning("Crash-alert Telegram notification failed: %s", e)
        try:
            from db.atlas_db import record_heartbeat
            record_heartbeat(
                "sync_protective_orders",
                "failed",
                {"error": str(exc)[:200]},
            )
        except Exception as _hb_exc2:  # noqa: BLE001
            logger.debug("sync_protective_orders: failed heartbeat write error: %s", _hb_exc2)
        raise
