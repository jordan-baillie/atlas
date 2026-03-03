"""Moomoo protective orders — SL (stop) + TP (limit) for open positions.

Moomoo does NOT support OCA (One-Cancels-All) groups. SL and TP orders
are placed as separate independent orders. The intraday_monitor.py
must cancel the remaining order when the other fills.

## Design

For each open position we maintain two protective orders:
  - SL: STOP SELL at stop_price  (triggers when price drops to stop_price)
  - TP: LIMIT SELL at take_profit  (fills when price rises to take_profit)

Both are placed as DAY orders. The sync is idempotent — running it twice
will NOT create duplicate orders. Existing matching orders are detected
by scanning open orders for atlas_sl_ / atlas_tp_ prefixed remarks.

## Order identification

Remarks are formatted as:
  atlas_sl_{strategy}_{date}   e.g. atlas_sl_mtf_momentum_2026-03-03
  atlas_tp_{strategy}_{date}   e.g. atlas_tp_mtf_momentum_2026-03-03

Open-order scanning matches on:
  - side=SELL, type STOP (or STOP_LIMIT), price near stop_price  → SL present
  - side=SELL, type NORMAL/LIMIT, price near take_profit          → TP present

Price tolerance for "near" is ±2% to absorb small rounding differences.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

from brokers.base import OrderResult, OrderSide, OrderStatus, OrderType

logger = logging.getLogger("atlas.moomoo.protective_orders")

# Price tolerance for detecting matching existing orders (±%)
_PRICE_TOLERANCE = 0.02


# ═══════════════════════════════════════════════════════════════
# Result dataclass
# ═══════════════════════════════════════════════════════════════

@dataclass
class ProtectiveOrderResult:
    """Outcome of placing/syncing protective orders for one position."""

    ticker: str
    sl_order_id: str = ""          # stop-loss order ID (empty if not placed/not applicable)
    tp_order_id: str = ""          # take-profit order ID (empty if no TP in plan)
    sl_placed: bool = False        # True if SL was newly placed this run
    tp_placed: bool = False        # True if TP was newly placed this run
    sl_already_exists: bool = False
    tp_already_exists: bool = False
    sl_skipped: bool = False       # True if SL was skipped (no stop_price)
    tp_skipped: bool = False       # True if TP was skipped (no take_profit)
    dry_run: bool = False
    errors: list = field(default_factory=list)

    @property
    def success(self) -> bool:
        """True if the required protective orders are in place (or already existed)."""
        return not self.errors

    def summary_line(self) -> str:
        """Single-line human summary for logs / Telegram."""
        parts = []
        if self.sl_placed:
            parts.append(f"SL placed ({self.sl_order_id})")
        elif self.sl_already_exists:
            parts.append("SL already exists")
        elif self.sl_skipped:
            parts.append("SL skipped (no stop_price)")

        if self.tp_placed:
            parts.append(f"TP placed ({self.tp_order_id})")
        elif self.tp_already_exists:
            parts.append("TP already exists")
        elif self.tp_skipped:
            parts.append("TP skipped (no take_profit)")

        status = " | ".join(parts) if parts else "nothing to do"
        if self.errors:
            status += f" | ERRORS: {'; '.join(self.errors)}"
        prefix = "[DRY RUN] " if self.dry_run else ""
        return f"{prefix}{self.ticker}: {status}"


# ═══════════════════════════════════════════════════════════════
# Core helpers
# ═══════════════════════════════════════════════════════════════

def _prices_match(price_a: float, price_b: float, tol: float = _PRICE_TOLERANCE) -> bool:
    """Return True if two prices are within ±tol% of each other."""
    if price_a <= 0 or price_b <= 0:
        return False
    return abs(price_a - price_b) / price_b <= tol


def _find_existing_sl(open_orders: list[OrderResult], ticker: str, stop_price: float) -> Optional[str]:
    """Scan open orders for an existing SL (STOP SELL) near stop_price.

    Returns the order_id if found, else None.
    """
    for o in open_orders:
        if o.ticker != ticker:
            continue
        if o.side != OrderSide.SELL:
            continue
        raw_type = str(o.raw.get("order_type", "")).upper()
        # Accept STOP or STOP_LIMIT as SL
        is_stop = raw_type in ("STOP", "STOP_LIMIT") or "STOP" in raw_type
        if not is_stop:
            # Also check by remark
            remark = str(o.raw.get("remark", "")).lower()
            if not remark.startswith("atlas_sl_"):
                continue
        if _prices_match(o.requested_price, stop_price):
            return o.order_id
    return None


def _find_existing_tp(open_orders: list[OrderResult], ticker: str, take_profit: float) -> Optional[str]:
    """Scan open orders for an existing TP (LIMIT SELL) near take_profit.

    Returns the order_id if found, else None.
    """
    for o in open_orders:
        if o.ticker != ticker:
            continue
        if o.side != OrderSide.SELL:
            continue
        raw_type = str(o.raw.get("order_type", "")).upper()
        # TP is a LIMIT/NORMAL order (not STOP)
        is_stop = "STOP" in raw_type
        if is_stop:
            continue
        if _prices_match(o.requested_price, take_profit):
            return o.order_id
        # Also check by remark — tp orders placed by us always have atlas_tp_ prefix
        remark = str(o.raw.get("remark", "")).lower()
        if remark.startswith("atlas_tp_") and o.ticker == ticker:
            return o.order_id
    return None


# ═══════════════════════════════════════════════════════════════
# Primary API
# ═══════════════════════════════════════════════════════════════

def place_protective_orders(
    broker,
    ticker: str,
    qty: int,
    stop_price: float,
    take_profit: Optional[float],
    strategy: str,
    trade_date: str,
    *,
    dry_run: bool = False,
    config: dict = None,
) -> ProtectiveOrderResult:
    """Place SL (stop) + TP (limit) protective orders for one position.

    Args:
        broker:      Connected MomooBroker instance.
        ticker:      Atlas-format ticker (e.g. 'BHP.AX').
        qty:         Number of shares to protect.
        stop_price:  Stop-loss trigger price.
        take_profit: Take-profit limit price (None → skip TP).
        strategy:    Strategy name for the remark.
        trade_date:  YYYY-MM-DD for the remark.
        dry_run:     Log intent but do NOT send orders.
        config:      Full config dict (optional, for safety limits).

    Returns:
        ProtectiveOrderResult with order IDs and placement status.
    """
    result = ProtectiveOrderResult(ticker=ticker, dry_run=dry_run)
    config = config or {}

    # ── SL order ──────────────────────────────────────────────
    if not stop_price or stop_price <= 0:
        result.sl_skipped = True
        logger.warning("No stop_price for %s — skipping SL order", ticker)
    else:
        remark = f"atlas_sl_{strategy}_{trade_date}"[:64]

        if dry_run:
            logger.info(
                "[DRY RUN] Would place SL: STOP SELL %s %d × trigger=%.4f remark=%s",
                ticker, qty, stop_price, remark,
            )
            result.sl_placed = True
            result.sl_order_id = "DRY_RUN_SL"
        else:
            logger.info(
                "Placing SL: STOP SELL %s %d × trigger=%.4f",
                ticker, qty, stop_price,
            )
            sl_result = broker.place_order(
                ticker=ticker,
                side=OrderSide.SELL,
                qty=qty,
                price=round(stop_price, 2),
                order_type=OrderType.STOP,
                stop_price=round(stop_price, 2),
                remark=remark,
            )
            if sl_result.success:
                result.sl_placed = True
                result.sl_order_id = sl_result.order_id
                logger.info("SL placed: %s → order_id=%s", ticker, sl_result.order_id)
            else:
                err = f"SL placement failed: {sl_result.message}"
                result.errors.append(err)
                logger.error("SL FAILED %s: %s", ticker, sl_result.message)

    # ── TP order ──────────────────────────────────────────────
    if not take_profit or take_profit <= 0:
        result.tp_skipped = True
        logger.debug("No take_profit for %s — skipping TP order", ticker)
    else:
        remark = f"atlas_tp_{strategy}_{trade_date}"[:64]

        if dry_run:
            logger.info(
                "[DRY RUN] Would place TP: LIMIT SELL %s %d × %.4f remark=%s",
                ticker, qty, take_profit, remark,
            )
            result.tp_placed = True
            result.tp_order_id = "DRY_RUN_TP"
        else:
            logger.info(
                "Placing TP: LIMIT SELL %s %d × %.4f",
                ticker, qty, take_profit,
            )
            tp_result = broker.place_order(
                ticker=ticker,
                side=OrderSide.SELL,
                qty=qty,
                price=round(take_profit, 2),
                order_type=OrderType.LIMIT,
                remark=remark,
            )
            if tp_result.success:
                result.tp_placed = True
                result.tp_order_id = tp_result.order_id
                logger.info("TP placed: %s → order_id=%s", ticker, tp_result.order_id)
            else:
                err = f"TP placement failed: {tp_result.message}"
                result.errors.append(err)
                logger.error("TP FAILED %s: %s", ticker, tp_result.message)

    return result


def sync_protective_orders(
    broker,
    positions: list,
    open_orders: list[OrderResult],
    plan: Optional[dict],
    config: dict,
    *,
    trade_date: str = "",
    dry_run: bool = False,
) -> dict:
    """Sync protective orders for all live positions.

    Idempotent — existing SL/TP orders are detected and skipped.
    Only missing orders are placed.

    Args:
        broker:       Connected MomooBroker instance.
        positions:    List of Position objects (from live_portfolio).
        open_orders:  All currently open orders from broker.
        plan:         Today's trade plan dict (for stop_price / take_profit lookups).
        config:       Full config dict.
        trade_date:   YYYY-MM-DD (defaults to today if empty).
        dry_run:      Log intent but do NOT send orders.

    Returns:
        Summary dict with per-ticker results and aggregate counts.
    """
    from datetime import date as _date

    if not trade_date:
        trade_date = str(_date.today())

    # Build plan lookup: ticker → {stop_price, take_profit}
    plan_lookup: dict[str, dict] = {}
    if plan:
        for entry in plan.get("proposed_entries", []):
            t = entry.get("ticker", "")
            if t:
                plan_lookup[t] = {
                    "stop_price": entry.get("stop_price", 0),
                    "take_profit": entry.get("take_profit"),
                    "strategy": entry.get("strategy", "unknown"),
                }

    results: dict[str, ProtectiveOrderResult] = {}
    counts = {
        "positions_checked": 0,
        "sl_placed": 0,
        "tp_placed": 0,
        "sl_already_exists": 0,
        "tp_already_exists": 0,
        "sl_skipped": 0,
        "tp_skipped": 0,
        "errors": 0,
    }

    for pos in positions:
        ticker = pos.ticker
        qty = pos.shares
        counts["positions_checked"] += 1

        # Determine stop_price and take_profit
        # Priority: position.stop_price → plan → 0
        stop_price = getattr(pos, "stop_price", 0) or 0
        take_profit = getattr(pos, "take_profit", None)

        if ticker in plan_lookup:
            plan_entry = plan_lookup[ticker]
            if not stop_price:
                stop_price = plan_entry.get("stop_price", 0)
            if take_profit is None:
                take_profit = plan_entry.get("take_profit")

        strategy = getattr(pos, "strategy", "unknown") or "unknown"
        if ticker in plan_lookup:
            strategy = plan_lookup[ticker].get("strategy", strategy)

        # Check for existing SL order
        existing_sl_id = None
        stop_order_id = getattr(pos, "stop_order_id", "") or ""
        if stop_order_id:
            # Position already has a recorded stop order — verify it's still open
            still_open = any(
                o.order_id == stop_order_id
                for o in open_orders
                if o.status not in (OrderStatus.CANCELLED, OrderStatus.FILLED, OrderStatus.FAILED)
            )
            if still_open:
                existing_sl_id = stop_order_id

        if not existing_sl_id and stop_price > 0:
            # Scan open orders for a matching STOP SELL
            existing_sl_id = _find_existing_sl(open_orders, ticker, stop_price)

        # Check for existing TP order
        existing_tp_id = None
        tp_order_id = getattr(pos, "tp_order_id", "") or ""
        if tp_order_id:
            still_open = any(
                o.order_id == tp_order_id
                for o in open_orders
                if o.status not in (OrderStatus.CANCELLED, OrderStatus.FILLED, OrderStatus.FAILED)
            )
            if still_open:
                existing_tp_id = tp_order_id

        if not existing_tp_id and take_profit and take_profit > 0:
            existing_tp_id = _find_existing_tp(open_orders, ticker, take_profit)

        # Decide what to place
        need_sl = not existing_sl_id and stop_price > 0
        need_tp = not existing_tp_id and take_profit and take_profit > 0

        result = ProtectiveOrderResult(ticker=ticker, dry_run=dry_run)

        if existing_sl_id:
            result.sl_already_exists = True
            result.sl_order_id = existing_sl_id
            counts["sl_already_exists"] += 1
            logger.debug("SL already exists for %s: %s", ticker, existing_sl_id)
        elif not stop_price:
            result.sl_skipped = True
            counts["sl_skipped"] += 1
        else:
            # Place SL
            remark = f"atlas_sl_{strategy}_{trade_date}"[:64]
            if dry_run:
                logger.info("[DRY RUN] Would place SL: STOP SELL %s %d × trigger=%.4f",
                            ticker, qty, stop_price)
                result.sl_placed = True
                result.sl_order_id = "DRY_RUN_SL"
                counts["sl_placed"] += 1
            else:
                logger.info("Placing SL: STOP SELL %s %d × trigger=%.4f", ticker, qty, stop_price)
                sl_result = broker.place_order(
                    ticker=ticker,
                    side=OrderSide.SELL,
                    qty=qty,
                    price=round(stop_price, 2),
                    order_type=OrderType.STOP,
                    stop_price=round(stop_price, 2),
                    remark=remark,
                )
                if sl_result.success:
                    result.sl_placed = True
                    result.sl_order_id = sl_result.order_id
                    counts["sl_placed"] += 1
                    logger.info("SL placed: %s → %s", ticker, sl_result.order_id)
                else:
                    result.errors.append(f"SL failed: {sl_result.message}")
                    counts["errors"] += 1
                    logger.error("SL FAILED %s: %s", ticker, sl_result.message)

        if existing_tp_id:
            result.tp_already_exists = True
            result.tp_order_id = existing_tp_id
            counts["tp_already_exists"] += 1
            logger.debug("TP already exists for %s: %s", ticker, existing_tp_id)
        elif not take_profit or take_profit <= 0:
            result.tp_skipped = True
            counts["tp_skipped"] += 1
        else:
            remark = f"atlas_tp_{strategy}_{trade_date}"[:64]
            if dry_run:
                logger.info("[DRY RUN] Would place TP: LIMIT SELL %s %d × %.4f",
                            ticker, qty, take_profit)
                result.tp_placed = True
                result.tp_order_id = "DRY_RUN_TP"
                counts["tp_placed"] += 1
            else:
                logger.info("Placing TP: LIMIT SELL %s %d × %.4f", ticker, qty, take_profit)
                tp_result = broker.place_order(
                    ticker=ticker,
                    side=OrderSide.SELL,
                    qty=qty,
                    price=round(take_profit, 2),
                    order_type=OrderType.LIMIT,
                    remark=remark,
                )
                if tp_result.success:
                    result.tp_placed = True
                    result.tp_order_id = tp_result.order_id
                    counts["tp_placed"] += 1
                    logger.info("TP placed: %s → %s", ticker, tp_result.order_id)
                else:
                    result.errors.append(f"TP failed: {tp_result.message}")
                    counts["errors"] += 1
                    logger.error("TP FAILED %s: %s", ticker, tp_result.message)

        results[ticker] = result
        logger.info(result.summary_line())

    return {
        "trade_date": trade_date,
        "dry_run": dry_run,
        "counts": counts,
        "results": {t: {
            "sl_order_id": r.sl_order_id,
            "tp_order_id": r.tp_order_id,
            "sl_placed": r.sl_placed,
            "tp_placed": r.tp_placed,
            "sl_already_exists": r.sl_already_exists,
            "tp_already_exists": r.tp_already_exists,
            "sl_skipped": r.sl_skipped,
            "tp_skipped": r.tp_skipped,
            "errors": r.errors,
            "summary": r.summary_line(),
        } for t, r in results.items()},
    }


def get_protective_order_status(
    broker,
    stop_order_id: str,
    tp_order_id: str,
) -> dict:
    """Query the live status of SL and TP orders from the broker.

    Args:
        broker:         Connected MomooBroker instance.
        stop_order_id:  SL order ID (empty string → skip).
        tp_order_id:    TP order ID (empty string → skip).

    Returns:
        Dict with 'sl' and 'tp' keys, each containing OrderResult.status.value
        and the full order data.
    """
    result = {
        "sl": {"order_id": stop_order_id, "status": "NOT_SET", "filled_qty": 0},
        "tp": {"order_id": tp_order_id, "status": "NOT_SET", "filled_qty": 0},
    }

    if stop_order_id:
        sl_status = broker.get_order_status(stop_order_id)
        result["sl"]["status"] = sl_status.status.value
        result["sl"]["filled_qty"] = sl_status.filled_qty
        result["sl"]["fill_price"] = sl_status.fill_price
        result["sl"]["message"] = sl_status.message

    if tp_order_id:
        tp_status = broker.get_order_status(tp_order_id)
        result["tp"]["status"] = tp_status.status.value
        result["tp"]["filled_qty"] = tp_status.filled_qty
        result["tp"]["fill_price"] = tp_status.fill_price
        result["tp"]["message"] = tp_status.message

    return result
