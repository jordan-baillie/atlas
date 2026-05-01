#!/usr/bin/env python3
"""
TP-coverage healthcheck. Runs every 15 min during RTH.

For each open broker position across sp500, commodity_etfs, sector_etfs:
  - Query open orders for that ticker
  - Assert at least one SELL stop order exists (stop coverage)
  - Assert at least one SELL limit order exists (TP coverage, unless TP-less strategy)
  - If either is missing AND last_check showed missing too AND >5 min elapsed since first missing:
    fire CRITICAL Telegram alert with full context

TP-less strategies (profit_target_atr_mult=0 / tp_pct=0 / uses_tp=false in active config)
are exempted from TP-coverage alerts.  They still MUST have a stop order.

Idempotent. State persistence via data/healthcheck_tp_coverage_state.json
(timestamps of first-missed-time per ticker).

Exit codes:
  0 — all positions covered
  1 — at least one position missing stop or TP for >5 min (alert fired)
  2 — broker connection failed (telegram alert too)

Usage:
    python3 scripts/healthcheck_tp_coverage.py --once
    python3 scripts/healthcheck_tp_coverage.py --once --quiet
    python3 scripts/healthcheck_tp_coverage.py --once --no-alert
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ── Path bootstrap ─────────────────────────────────────────────────────────────
_HERE = Path(__file__).resolve()
_ATLAS_ROOT = _HERE.parent.parent
if str(_ATLAS_ROOT) not in sys.path:
    sys.path.insert(0, str(_ATLAS_ROOT))

logger = logging.getLogger(__name__)

# ── Constants ──────────────────────────────────────────────────────────────────
STATE_FILE = _ATLAS_ROOT / "data" / "healthcheck_tp_coverage_state.json"

#: Markets to check for open positions
MARKETS = ("sp500", "commodity_etfs", "sector_etfs")

#: Minutes of missing coverage before an alert fires (debounce)
MISSING_THRESHOLD_MINUTES = 5

#: Raw Alpaca status strings that are terminal / inactive (NOT active coverage)
_INACTIVE_RAW_STATUSES = frozenset({
    "canceled", "cancelled", "done_for_day", "expired",
    "replaced", "stopped", "rejected", "suspended",
})

#: Order types that constitute stop coverage (SELL side only)
_STOP_ORDER_TYPES = frozenset({"stop", "stop_limit", "trailing_stop"})

#: Order types that constitute TP (take-profit) coverage (SELL side only)
_TP_ORDER_TYPES = frozenset({"limit"})

#: OCO/bracket order classes satisfy BOTH stop and TP
_BRACKET_ORDER_CLASSES = frozenset({"oco", "bracket"})

# ── TP-less strategy cache ─────────────────────────────────────────────────────
#: Module-level cache to avoid re-loading configs every call.
#: Keyed by (market_id, strategy_name) → bool.
_TP_LESS_CACHE: dict[tuple[str, str], bool] = {}


# ── State helpers ──────────────────────────────────────────────────────────────

def _load_state(path: Path = STATE_FILE) -> dict[str, Any]:
    """Load state from JSON; return empty state on missing/corrupt file."""
    try:
        text = path.read_text()
        data = json.loads(text)
        if not isinstance(data, dict):
            raise ValueError("State file is not a JSON object")
        # Ensure required keys exist
        if "first_missing_at" not in data:
            data["first_missing_at"] = {}
        return data
    except FileNotFoundError:
        logger.debug("State file not found (%s) — starting fresh", path)
    except (json.JSONDecodeError, ValueError) as exc:
        logger.warning("State file corrupted (%s: %s) — resetting", path, exc)
    return {"first_missing_at": {}, "last_run_at": None}


def _save_state(state: dict[str, Any], path: Path = STATE_FILE) -> None:
    """Persist state to JSON file atomically (temp-then-rename)."""
    tmp = path.with_suffix(".json.tmp")
    try:
        tmp.write_text(json.dumps(state, indent=2))
        tmp.replace(path)
    except OSError as exc:
        logger.error("Failed to save state file %s: %s", path, exc)


# ── Strategy TP-less detection ─────────────────────────────────────────────────

def _lookup_strategy_from_state(market_id: str, ticker: str) -> str:
    """Read strategy for ticker from the broker state file. Empty string if not found."""
    try:
        state_path = _ATLAS_ROOT / "brokers" / "state" / f"live_{market_id}.json"
        if not state_path.exists():
            return ""
        data = json.loads(state_path.read_text())
        for p in data.get("positions", []):
            if p.get("ticker") == ticker:
                return p.get("strategy") or ""
    except Exception as exc:
        logger.debug("Could not load strategy for %s/%s: %s", market_id, ticker, exc)
    return ""


def _is_strategy_tp_less(market_id: str, strategy: str) -> bool:
    """Return True if the strategy is configured WITHOUT a take-profit.

    A strategy is TP-less when ANY of these is true in active config:
      - profit_target_atr_mult == 0 (or absent — default is 0)
      - tp_pct == 0
      - uses_tp is explicitly False

    Cached per (market_id, strategy) tuple.  The healthcheck runs every 15 min;
    a long-lived process would benefit from cache invalidation, but each
    invocation is one-shot so this is fine.

    Returns False on any config-load error (fail-safe: alert rather than miss).
    Unknown strategies (not in config) also return False — they are treated as
    TP-using so an alert fires if TP is missing, surfacing the misconfiguration.
    """
    if not strategy:
        return False
    key = (market_id, strategy)
    if key in _TP_LESS_CACHE:
        return _TP_LESS_CACHE[key]

    try:
        from utils.config import get_active_config
        cfg = get_active_config(market_id)
    except Exception as exc:
        logger.warning(
            "Could not load config for %s — assuming strategy %s uses TP: %s",
            market_id, strategy, exc,
        )
        _TP_LESS_CACHE[key] = False
        return False

    strategies_root = (cfg or {}).get("strategies", {})
    # Active config schema can be either:
    #   {strategies: {strategies: {NAME: {...}}}}   (nested — some test fixtures)
    #   {strategies: {NAME: {...}}}                  (flat — production)
    if (
        isinstance(strategies_root, dict)
        and "strategies" in strategies_root
        and isinstance(strategies_root["strategies"], dict)
    ):
        per_strategy = strategies_root["strategies"]
    elif isinstance(strategies_root, dict):
        per_strategy = strategies_root
    else:
        per_strategy = {}

    params = per_strategy.get(strategy, None)
    if params is None:
        # Strategy not in config — fail-safe: assume TP-using (will alert if TP missing).
        # An unknown strategy is a sign of misconfiguration; the operator should know.
        _TP_LESS_CACHE[key] = False
        return False

    if not isinstance(params, dict):
        _TP_LESS_CACHE[key] = False
        return False

    # Check explicit uses_tp=False override
    if params.get("uses_tp") is False:
        _TP_LESS_CACHE[key] = True
        return True

    # If uses_tp is explicitly True, honour it regardless of multipliers
    if params.get("uses_tp") is True:
        _TP_LESS_CACHE[key] = False
        return False

    # Check profit_target_atr_mult
    ptm = params.get("profit_target_atr_mult", 0)
    try:
        ptm_val = float(ptm) if ptm is not None else 0.0
    except (ValueError, TypeError):
        ptm_val = 0.0

    # Check tp_pct
    tp_pct = params.get("tp_pct", 0)
    try:
        tp_pct_val = float(tp_pct) if tp_pct is not None else 0.0
    except (ValueError, TypeError):
        tp_pct_val = 0.0

    # TP-less when BOTH multipliers are zero AND no uses_tp override
    is_tp_less = ptm_val == 0.0 and tp_pct_val == 0.0
    _TP_LESS_CACHE[key] = is_tp_less
    return is_tp_less


# ── Coverage classification ────────────────────────────────────────────────────

def _is_active_order(order: Any) -> bool:
    """Return True if the order is in an active (working) state.

    Held orders ARE active — Alpaca places stop legs as 'held' during
    pre-market/non-RTH, and they activate automatically at market open.
    Cancelled/expired/rejected orders are NOT active.
    """
    raw_status = (order.raw or {}).get("status", "").lower()
    return raw_status not in _INACTIVE_RAW_STATUSES


def classify_orders(
    orders: list[Any],
    ticker: str,
) -> tuple[bool, bool]:
    """Return (has_stop, has_tp) for the given ticker from the order list.

    Args:
        orders: Full list of OrderResult objects (all tickers).
        ticker: The Atlas-format ticker to classify.

    Returns:
        (has_stop_coverage, has_tp_coverage)
    """
    has_stop = False
    has_tp = False

    for order in orders:
        if order.ticker != ticker:
            continue
        # Only SELL orders provide protective coverage
        if order.side.value != "SELL":
            continue
        # Skip inactive (cancelled/expired) orders
        if not _is_active_order(order):
            continue

        raw = order.raw or {}
        order_type = raw.get("order_type", "").lower()
        order_class = raw.get("order_class", "").lower() if raw.get("order_class") else ""

        # OCO/bracket: a single order counts as BOTH stop AND TP coverage
        if order_class in _BRACKET_ORDER_CLASSES:
            has_stop = True
            has_tp = True
            break

        if order_type in _STOP_ORDER_TYPES:
            has_stop = True
        if order_type in _TP_ORDER_TYPES:
            has_tp = True

    return has_stop, has_tp


# ── Telegram alert ─────────────────────────────────────────────────────────────

def _build_alert_message(missing_items: list[dict[str, Any]]) -> str:
    """Build the CRITICAL Telegram alert message.

    Args:
        missing_items: List of dicts with keys: ticker, market, has_stop, has_tp,
                       is_tp_less, strategy, first_missing_at (ISO string).
    """
    lines = ["🚨 <b>TP-COVERAGE ALERT</b>", "", "Position lacking protection &gt;5 min:"]

    for item in missing_items:
        ticker = item["ticker"]
        market = item["market"]
        has_stop = item["has_stop"]
        has_tp = item["has_tp"]
        is_tp_less = item.get("is_tp_less", False)
        strategy = item.get("strategy", "")

        if is_tp_less:
            # Only fires when !has_stop (TP not required for this strategy)
            detail = f"MISSING STOP (TP-less strategy: {strategy})"
        elif not has_stop and not has_tp:
            detail = "MISSING STOP and TP"
        elif not has_stop:
            detail = "MISSING STOP (TP ok)"
        else:
            detail = "MISSING TP (stop ok)"

        lines.append(f"• {ticker} ({market}): {detail}")

    lines.append("")

    # Unique markets affected
    affected_markets = sorted({item["market"] for item in missing_items})
    for mkt in affected_markets:
        lines.append(f"Run: python3 scripts/sync_protective_orders.py --market {mkt}")

    return "\n".join(lines)


def _send_alert(message: str, no_alert: bool = False) -> None:
    """Send Telegram alert; no-op when no_alert=True (prints instead)."""
    if no_alert:
        logger.info("[--no-alert] Would send Telegram: %s", message)
        print(f"[no-alert] Would fire:\n{message}")
        return
    try:
        from utils.telegram import send_message
        send_message(message, parse_mode="HTML")
    except Exception as exc:
        logger.error("Telegram alert failed: %s", exc)


# ── Per-market check ───────────────────────────────────────────────────────────

def check_market(
    market_id: str,
) -> tuple[list[dict[str, Any]] | None, str | None]:
    """Connect to broker for market_id and check position coverage.

    Returns:
        (results, error_message)
        results: list of dicts with keys ticker, market, has_stop, has_tp,
                 strategy, is_tp_less
        error_message: non-None on connection failure
    """
    try:
        from utils.config import get_active_config
        from brokers.registry import get_live_broker
    except ImportError as exc:
        return None, f"Import failed for {market_id}: {exc}"

    try:
        cfg = get_active_config(market_id)
    except FileNotFoundError:
        logger.warning("No config for market %s — skipping", market_id)
        return [], None  # Not an error — market may not be configured

    broker = get_live_broker(cfg)
    if broker is None:
        logger.info("No live broker for market %s (live_enabled=false) — skipping", market_id)
        return [], None

    try:
        connected = broker.connect()
    except Exception as exc:
        return None, f"Broker connect() raised for {market_id}: {exc}"

    if not connected:
        return None, f"Broker connect() returned False for {market_id}"

    try:
        positions = broker.get_positions()
    except Exception as exc:
        return None, f"get_positions() failed for {market_id}: {exc}"

    if not positions:
        logger.info("No open positions for %s", market_id)
        return [], None

    try:
        orders = broker.get_open_orders()
    except Exception as exc:
        return None, f"get_open_orders() failed for {market_id}: {exc}"

    results = []
    for pos in positions:
        has_stop, has_tp = classify_orders(orders, pos.ticker)

        # Resolve strategy: PositionInfo.strategy if populated, else state-file fallback
        strategy = getattr(pos, "strategy", "") or _lookup_strategy_from_state(market_id, pos.ticker)
        is_tp_less = _is_strategy_tp_less(market_id, strategy)

        results.append({
            "ticker": pos.ticker,
            "market": market_id,
            "has_stop": has_stop,
            "has_tp": has_tp,
            "strategy": strategy,
            "is_tp_less": is_tp_less,
        })

        if is_tp_less and has_stop:
            coverage_status = "✓✓ (TP-less, only stop required)"
        elif is_tp_less and not has_stop:
            coverage_status = "✗ MISSING STOP (TP-less, stop still required)"
        else:
            tp_label = "✓" if has_tp else "✗"
            coverage_status = (
                f"stop={'✓' if has_stop else '✗'} "
                f"tp={tp_label}"
            )

        logger.info(
            "  %s (%s, %s): %s",
            pos.ticker, market_id, strategy or "?", coverage_status,
        )

    return results, None


# ── Main logic ─────────────────────────────────────────────────────────────────

def run_check(
    markets: tuple[str, ...] = MARKETS,
    no_alert: bool = False,
    state_path: Path = STATE_FILE,
) -> int:
    """Run the full TP-coverage check across all markets.

    Returns exit code: 0=all covered, 1=alerts fired, 2=broker error.
    """
    now_utc = datetime.now(tz=timezone.utc)
    now_iso = now_utc.isoformat()

    state = _load_state(state_path)
    first_missing: dict[str, str] = state.get("first_missing_at", {})

    connection_errors: list[str] = []
    all_results: list[dict[str, Any]] = []

    for market_id in markets:
        logger.info("Checking market: %s", market_id)
        results, error = check_market(market_id)

        if error is not None:
            logger.error("Broker error for %s: %s", market_id, error)
            connection_errors.append(f"{market_id}: {error}")
            continue

        if results:
            all_results.extend(results)

    # ── Handle broker connection errors ───────────────────────────────────────
    if connection_errors:
        error_summary = "\n".join(connection_errors)
        alert_msg = (
            f"🚨 <b>TP-COVERAGE CHECK FAILED</b>\n\n"
            f"Broker connection error(s):\n{error_summary}\n\n"
            f"Healthcheck aborted — coverage unknown."
        )
        _send_alert(alert_msg, no_alert=no_alert)
        return 2

    # ── Classify coverage gaps ─────────────────────────────────────────────────

    # Mark positions that are now covered (remove from state)
    covered_keys = set()
    for r in all_results:
        if (r.get("is_tp_less") and r["has_stop"]) or (r["has_stop"] and r["has_tp"]):
            key = f"{r['market']}:{r['ticker']}"
            covered_keys.add(key)

    for key in covered_keys:
        if key in first_missing:
            logger.info("Coverage restored for %s — clearing state", key)
            del first_missing[key]

    # Find currently missing positions
    missing_now: list[dict[str, Any]] = []
    for r in all_results:
        # TP-less strategies: only require stop
        if r.get("is_tp_less"):
            if r["has_stop"]:
                # Stop covered — fully OK for this strategy
                continue
            # No stop — fall through to debounce/alert logic below
        elif r["has_stop"] and r["has_tp"]:
            continue  # fully covered

        key = f"{r['market']}:{r['ticker']}"

        if key not in first_missing:
            # First time we see this missing — record timestamp, no alert yet
            first_missing[key] = now_iso
            logger.info(
                "First observation of missing coverage for %s — recorded at %s",
                key, now_iso,
            )
        else:
            # Already recorded — check if threshold exceeded
            first_seen_str = first_missing[key]
            try:
                first_seen = datetime.fromisoformat(first_seen_str)
                elapsed_minutes = (now_utc - first_seen).total_seconds() / 60.0
            except (ValueError, TypeError):
                # Corrupted timestamp — treat as just seen
                first_missing[key] = now_iso
                elapsed_minutes = 0.0

            if elapsed_minutes > MISSING_THRESHOLD_MINUTES:
                r["first_missing_at"] = first_seen_str
                missing_now.append(r)
                logger.warning(
                    "MISSING coverage for %s for %.1f min (threshold %d min)",
                    key, elapsed_minutes, MISSING_THRESHOLD_MINUTES,
                )
            else:
                logger.info(
                    "Coverage missing for %s for %.1f min — below threshold",
                    key, elapsed_minutes,
                )

    # ── Persist updated state ──────────────────────────────────────────────────
    state["first_missing_at"] = first_missing
    state["last_run_at"] = now_iso
    _save_state(state, state_path)

    # ── Log summary ───────────────────────────────────────────────────────────
    total_positions = len(all_results)
    fully_covered = sum(
        1 for r in all_results
        if (r.get("is_tp_less") and r["has_stop"]) or (r["has_stop"] and r["has_tp"])
    )
    tp_less_count = sum(1 for r in all_results if r.get("is_tp_less"))

    logger.info(
        "Summary: %d/%d positions fully covered (%d TP-less); %d alerting",
        fully_covered, total_positions, tp_less_count, len(missing_now),
    )

    if not missing_now:
        logger.info("✅ All positions covered (or within debounce window)")
        return 0

    # ── Fire alert ─────────────────────────────────────────────────────────────
    alert_msg = _build_alert_message(missing_now)
    logger.warning("Firing TP-coverage alert for %d position(s)", len(missing_now))
    _send_alert(alert_msg, no_alert=no_alert)
    return 1


# ── CLI entry point ────────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="TP-coverage healthcheck — assert every position has stop+TP at broker",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        default=True,
        help="One-shot run (default; reserved for future --daemon mode)",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress INFO logs (only ERRORs and WARNINGs to stderr)",
    )
    parser.add_argument(
        "--no-alert",
        dest="no_alert",
        action="store_true",
        help="Print what would be sent to Telegram but do not actually send it",
    )
    args = parser.parse_args(argv)

    log_level = logging.WARNING if args.quiet else logging.INFO
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stderr,
    )

    return run_check(no_alert=args.no_alert)


if __name__ == "__main__":
    sys.exit(main())
