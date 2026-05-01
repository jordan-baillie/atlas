"""Per-market realized cash flow since the last EOD snapshot.

Used by brokers.live_portfolio._get_per_market_equity to compute an accurate
intraday per-market cash balance by tracking actual trade fills and dividends
since the snapshot_time, rather than scaling the stale snapshot cash
proportionally with broker equity.

Root cause this module fixes (FIX-PMEQ-001, 2026-05-01):
  When a position exits intraday, its value moves from position_mv → broker cash.
  The old formula used ``snap_cash * cash_scale`` which locked snap_cash to the
  previous EOD value, ignoring the newly realised proceeds.  This caused phantom
  drawdowns on exit days (the real per-market equity was higher than calculated).

Public API
----------
compute_realized_cash_flow_since(broker, since_ts, market_symbols) → (flows, degraded)

  Returns realized cash flows per market since the snapshot timestamp.
  On Alpaca activities API failure, returns degraded=True so the caller can
  suppress the kill switch (don't HALT on stale snap_cash estimates).

_clear_cache() — test helper to reset the in-process TTL cache.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level TTL cache
# ---------------------------------------------------------------------------
# key: (since_ts_isoformat, tuple(sorted(market_ids)))
# value: (monotonic_timestamp, cash_flow_by_market, degraded)
_CACHE: dict[tuple, tuple[float, dict[str, float], bool]] = {}


def _clear_cache() -> None:
    """Clear the in-process activity cache (test helper)."""
    _CACHE.clear()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def compute_realized_cash_flow_since(
    broker: Any,
    since_ts: datetime,
    market_symbols: dict[str, set[str]],
    cache_ttl_seconds: float = 30.0,
) -> tuple[dict[str, float], bool]:
    """Return (cash_flow_by_market, degraded).

    cash_flow_by_market[m] = sum of realized cash flows on symbols in market_symbols[m]
                             since since_ts.  Positive = cash IN to that market.

    How cash flows are attributed:
      FILL (sell): +qty * price → increases the market's cash pool
      FILL (buy):  -qty * price → decreases the market's cash pool
      DIV:         +net_amount → increases the market's cash pool
      CSD/CSW/JNLC/FEE/CFEE: NOT attributed per-market (global broker-level events,
                               small and would distort per-market attribution).

    degraded = True iff the Alpaca activities API call failed.
      In degraded mode returns ({m: 0.0 for m in market_symbols}, True).
      Caller MUST treat degraded=True as "do not trip kill switch on this cycle".

    Attribution: each symbol is routed to a market via ``universe.membership.derive_universe``.
      If derive_universe returns None, or returns a market not in market_symbols, the
      activity is silently skipped (logged at DEBUG level).

    Cache: module-level dict keyed by (since_ts.isoformat(), tuple(sorted(market_ids)))
      with ``cache_ttl_seconds`` TTL.  Prevents hammering Alpaca on every
      check_daily_drawdown call (which can fire many times per minute under load).
    """
    # Normalise to tz-aware UTC
    if since_ts.tzinfo is None:
        since_ts = since_ts.replace(tzinfo=timezone.utc)

    cache_key = (since_ts.isoformat(), tuple(sorted(market_symbols.keys())))
    now_mono = time.monotonic()

    # ── Cache lookup ─────────────────────────────────────────────────────────
    if cache_key in _CACHE:
        cached_ts, cached_flows, cached_deg = _CACHE[cache_key]
        if now_mono - cached_ts < cache_ttl_seconds:
            logger.debug(
                "compute_realized_cash_flow_since: cache HIT (age=%.1fs ttl=%.1fs)",
                now_mono - cached_ts, cache_ttl_seconds,
            )
            return cached_flows, cached_deg

    zeros: dict[str, float] = {m: 0.0 for m in market_symbols}

    # ── Fetch activities from Alpaca ─────────────────────────────────────────
    # Mirrors the pattern in services/api/dashboard.py::_get_portfolio_history.
    # SDK note: GetAccountActivitiesRequest is in alpaca.broker.requests
    # (NOT alpaca.trading.requests). TradingClient has no get_account_activities
    # method; we call the raw HTTP endpoint via _trade_client.get().
    try:
        from alpaca.broker.requests import GetAccountActivitiesRequest
        from alpaca.trading.enums import ActivityType

        _act_req = GetAccountActivitiesRequest(
            activity_types=[ActivityType.FILL, ActivityType.DIV],
            after=since_ts,
        )

        def _fetch(req: Any) -> list:
            fields = req.to_request_fields()
            return broker._trade_client.get("/account/activities", fields) or []

        activities = broker._broker_call(_fetch, _act_req) or []

    except Exception as exc:
        logger.warning(
            "compute_realized_cash_flow_since: activities API failed — "
            "entering degraded mode (snap_cash frozen): %s", exc,
        )
        _CACHE[cache_key] = (now_mono, zeros, True)
        return zeros, True

    # ── Attribute each activity to a market ──────────────────────────────────
    from universe.membership import derive_universe

    cash_flow_by_market: dict[str, float] = {m: 0.0 for m in market_symbols}

    for act in activities:
        # ── Extract fields — handle both dict (raw HTTP JSON response) and
        #    SDK model objects (SimpleNamespace, SDK-typed models) ─────────────
        if isinstance(act, dict):
            _at_raw = act.get("activity_type") or act.get("type") or ""
            symbol = act.get("symbol")
            side = act.get("side")
            qty = act.get("qty")
            price = act.get("price")
            net_amount = act.get("net_amount")
            tx_time_raw = act.get("transaction_time")
        else:
            _at_obj = getattr(act, "activity_type", None)
            if _at_obj is not None and hasattr(_at_obj, "value"):
                _at_raw = str(_at_obj.value)   # str-Enum subclass: .value gives "FILL"
            elif _at_obj is not None:
                _at_raw = str(_at_obj)
            else:
                _at_raw = ""
            symbol = getattr(act, "symbol", None)
            side = getattr(act, "side", None)
            qty = getattr(act, "qty", None)
            price = getattr(act, "price", None)
            net_amount = getattr(act, "net_amount", None)
            tx_time_raw = getattr(act, "transaction_time", None)

        act_type = _at_raw.upper() if _at_raw else ""

        # ── Defensive time filter ─────────────────────────────────────────────
        # Alpaca's `after` parameter is sometimes slightly loose; re-check.
        if tx_time_raw is not None:
            try:
                if isinstance(tx_time_raw, datetime):
                    tx_dt = tx_time_raw
                else:
                    tx_str = str(tx_time_raw).replace("Z", "+00:00")
                    tx_dt = datetime.fromisoformat(tx_str)
                if tx_dt.tzinfo is None:
                    tx_dt = tx_dt.replace(tzinfo=timezone.utc)
                if tx_dt < since_ts:
                    logger.debug(
                        "compute_realized_cash_flow_since: skip %s activity "
                        "at %s (before since_ts=%s)",
                        act_type or "?", tx_dt.isoformat(), since_ts.isoformat(),
                    )
                    continue
            except (ValueError, TypeError, AttributeError):
                pass  # Can't parse — trust Alpaca's `after` filter

        # ── Only process FILL and DIV ─────────────────────────────────────────
        if act_type not in ("FILL", "DIV"):
            continue

        if not symbol:
            logger.debug(
                "compute_realized_cash_flow_since: skip %s activity (no symbol)",
                act_type,
            )
            continue

        # ── Route symbol to its market ────────────────────────────────────────
        market = derive_universe(symbol)
        if market is None or market not in market_symbols:
            logger.debug(
                "compute_realized_cash_flow_since: skip %s "
                "(market=%r not in tracked markets %s)",
                symbol, market, sorted(market_symbols.keys()),
            )
            continue

        # ── Compute cash delta ────────────────────────────────────────────────
        if act_type == "FILL":
            if side is None or qty is None or price is None:
                logger.debug(
                    "compute_realized_cash_flow_since: FILL for %s missing "
                    "side/qty/price — skip", symbol,
                )
                continue
            try:
                side_str = str(side).lower()
                direction = 1 if side_str == "sell" else -1
                cash_delta = direction * float(qty) * float(price)
            except (ValueError, TypeError) as exc:
                logger.debug(
                    "compute_realized_cash_flow_since: FILL for %s — "
                    "could not parse qty/price: %s", symbol, exc,
                )
                continue
            cash_flow_by_market[market] = cash_flow_by_market[market] + cash_delta

        else:  # DIV
            if net_amount is None:
                continue
            try:
                cash_flow_by_market[market] = (
                    cash_flow_by_market[market] + float(net_amount)
                )
            except (ValueError, TypeError) as exc:
                logger.debug(
                    "compute_realized_cash_flow_since: DIV for %s — "
                    "could not parse net_amount: %s", symbol, exc,
                )
                continue

    logger.debug(
        "compute_realized_cash_flow_since: flows=%s (since=%s, %d activities processed)",
        cash_flow_by_market, since_ts.isoformat(), len(activities),
    )
    _CACHE[cache_key] = (now_mono, cash_flow_by_market, False)
    return cash_flow_by_market, False
