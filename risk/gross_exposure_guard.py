"""Global gross-exposure cap guard for entry orders.

Apr 27 2026 post-mortem: combined positions across all universes reached
174% gross exposure ($9,491 MV / $5,428 equity), leaving only $1,342 buying
power. A UNG entry of $1,371 was blocked at the broker level — but by then
the over-leverage had already occurred with no proactive guard in place.

This guard enforces a proactive cap: if a new entry WOULD push gross exposure
above ``max_gross_exposure_pct`` (config key, per-market risk block), the entry
is rejected BEFORE order submission with a structured reason + Telegram alert.

Design:
  - Cap is read from the calling market's config: risk.max_gross_exposure_pct
  - Gross is computed from BROKER live state (ALL universes via the account
    endpoint — long_market_value is an account-level aggregate from Alpaca).
  - Prospective notional is included: would THIS trade push us over?
  - Fail-OPEN on missing/zero cap (log warning once per process)
  - Exits, stops, TPs are NEVER gated (guard only called from _execute_entry)
"""
from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)

# Warn once per process when cap is unconfigured (don't spam on every signal)
_warned_missing_cap: set[str] = set()


# ── Config helpers ────────────────────────────────────────────────────────────


def _get_gross_exposure_cap(market_config: dict) -> float:
    """Extract ``max_gross_exposure_pct`` from market config.

    Returns 0.0 if absent, zero, or unparseable (triggers fail-open).
    """
    risk = (market_config or {}).get("risk", {})
    v = risk.get("max_gross_exposure_pct", 0)
    try:
        return float(v) if v else 0.0
    except (TypeError, ValueError):
        return 0.0


# ── Broker state query ────────────────────────────────────────────────────────


def _get_broker_gross_state(broker) -> tuple[float, float]:
    """Query broker for (equity, total_long_market_value) across ALL universes.

    Uses the account-level endpoint which aggregates ALL positions regardless
    of which Atlas universe they belong to.

    Priority:
      1. ``get_account_info()`` — AlpacaBroker adapter style (AccountInfo dataclass).
      2. ``get_account()`` — raw dict / trade_client style.

    Returns:
        (equity, long_market_value) or (0.0, 0.0) on any failure.
    """
    if broker is None:
        return 0.0, 0.0
    try:
        # Path 1: AlpacaBroker adapter → returns AccountInfo dataclass
        if hasattr(broker, "get_account_info"):
            acct = broker.get_account_info()
            if acct is not None:
                equity = float(getattr(acct, "equity", 0) or 0)
                mv = float(getattr(acct, "market_value", 0) or 0)
                if equity > 0:
                    return equity, mv
        # Path 2: raw trade_client-style → returns dict-like object
        if hasattr(broker, "get_account"):
            acct = broker.get_account()
            if acct is not None:
                if isinstance(acct, dict):
                    equity = float(acct.get("equity", 0) or 0)
                    mv = float(acct.get("long_market_value", 0) or 0)
                else:
                    equity = float(getattr(acct, "equity", 0) or 0)
                    mv = float(getattr(acct, "long_market_value", 0) or 0)
                if equity > 0:
                    return equity, mv
    except Exception as e:
        logger.error("gross_exposure_guard: failed to query broker state: %s", e)
    return 0.0, 0.0


# ── Primary guard function ────────────────────────────────────────────────────


def check_gross_exposure(
    broker,
    prospective_order_notional: float,
    market_config: dict,
    *,
    market_id: str = "unknown",
) -> tuple[bool, str]:
    """Evaluate whether a prospective entry would breach the gross exposure cap.

    Args:
        broker:                     Broker handle (AlpacaBroker or compatible).
        prospective_order_notional: Cost of the prospective entry (qty × price).
        market_config:              Per-market config dict with ``risk.max_gross_exposure_pct``.
        market_id:                  Used for deduplicated warning key and logging.

    Returns:
        ``(True, reason)``  — entry allowed (below/at cap, or cap not configured).
        ``(False, reason)`` — entry rejected (would breach cap). ``reason`` is a
                              structured string suitable for Telegram alerts.
    """
    global _warned_missing_cap

    cap = _get_gross_exposure_cap(market_config)

    if cap <= 0.0:
        if market_id not in _warned_missing_cap:
            _warned_missing_cap.add(market_id)
            logger.warning(
                "gross_exposure_guard: max_gross_exposure_pct not set or zero for "
                "market=%s — gross exposure cap enforcement DISABLED (fail-open)",
                market_id,
            )
        return True, "no cap configured"

    equity, current_mv = _get_broker_gross_state(broker)

    if equity <= 0.0:
        logger.warning(
            "gross_exposure_guard: equity=%.2f — cannot compute gross exposure for "
            "market=%s, fail-open",
            equity, market_id,
        )
        return True, "equity unavailable, fail-open"

    prospective_mv = current_mv + float(prospective_order_notional)
    prospective_gross = prospective_mv / equity

    if prospective_gross <= cap:
        return True, (
            f"gross exposure {prospective_gross:.1%} \u2264 cap {cap:.1%} "
            f"(MV=${current_mv:.0f}, +${prospective_order_notional:.0f}, "
            f"equity=${equity:.0f})"
        )

    reason = (
        f"max_gross_exposure_pct: would reach {prospective_gross:.1%}, "
        f"cap is {cap:.1%} "
        f"(current MV=${current_mv:.0f}, +${prospective_order_notional:.0f}, "
        f"equity=${equity:.0f})"
    )
    return False, reason


# ── Telegram alert ────────────────────────────────────────────────────────────


def telegram_alert_gross_exposure(
    ticker: str,
    universe: str,
    reason: str,
) -> None:
    """Best-effort Telegram alert when the gross-exposure cap rejects an entry."""
    try:
        from utils.telegram import send_message, tg_escape
        msg = (
            f"\u26a0\ufe0f [risk] max_gross_exposure_pct exceeded: "
            f"ticker={tg_escape(ticker)} universe={tg_escape(universe)} "
            f"\u2014 {tg_escape(reason)}"
        )
        send_message(msg)
    except Exception as e:
        logger.warning("gross_exposure_guard: telegram alert failed: %s", e)
