"""IBKR protective order management — SL + TP as OCA groups (ib_insync).

Places stop-loss and take-profit orders as an OCA (One-Cancels-All) group
so that when either fills, the other is automatically cancelled.

Architecture:
    - OCA type 1: "cancel all remaining orders with block"
    - SL: StopOrder(SELL) — triggers at stop_price, fills at market
    - TP: LimitOrder(SELL) — GTC limit at take_profit_price
    - Both share the same ocaGroup name (atlas_oca_{symbol}_{epoch})
    - TIF = GTC for all protective orders (persist across sessions)
    - overridePercentageConstraints = True (we do our own safety checks)

When only SL is requested (no TP), a plain StopOrder is placed with no OCA
group — this is the common case for trailing-stop strategies that manage
take-profit outside the broker.

OCA naming convention:
    group:     "atlas_oca_{symbol}_{epoch_seconds}"
    SL ref:    "atlas_sl_{symbol}"
    TP ref:    "atlas_tp_{symbol}"

These patterns are used by get_existing_protective_orders() to identify
orders placed by Atlas vs. any manual orders on the account.
"""

from __future__ import annotations

import logging
import time
from typing import Optional

logger = logging.getLogger("atlas.broker.ibkr.protective_orders")


# ─── OCA group helpers ────────────────────────────────────────────────────────

def _make_oca_group(symbol: str) -> str:
    """Create a unique OCA group name for a symbol."""
    return f"atlas_oca_{symbol}_{int(time.time())}"


def _is_our_sl_order(order) -> bool:
    """Return True if this is an Atlas-placed stop-loss order."""
    order_type = (order.orderType or "").upper()
    ref = (order.orderRef or "")
    oca = (order.ocaGroup or "")
    return (
        order.action == "SELL"
        and order_type in ("STP", "STOP", "STP LMT")
        and (ref.startswith("atlas_sl_") or oca.startswith("atlas_oca_"))
    )


def _is_our_tp_order(order) -> bool:
    """Return True if this is an Atlas-placed take-profit order."""
    order_type = (order.orderType or "").upper()
    ref = (order.orderRef or "")
    oca = (order.ocaGroup or "")
    return (
        order.action == "SELL"
        and order_type in ("LMT", "LIMIT")
        and (ref.startswith("atlas_tp_") or oca.startswith("atlas_oca_"))
    )


def _contracts_match(c1, c2) -> bool:
    """Check if two contracts refer to the same security (symbol + secType)."""
    if c1.symbol != c2.symbol:
        return False
    if c1.secType and c2.secType and c1.secType != c2.secType:
        return False
    return True


# ─── Core functions ───────────────────────────────────────────────────────────

def place_protective_orders(
    ib,
    contract,
    position_qty: int,
    stop_price: float,
    take_profit_price: Optional[float] = None,
    account_id: str = "",
) -> dict:
    """Place SL + TP protective orders for an existing long position.

    When take_profit_price is provided, both SL and TP are placed as an OCA
    group — if SL fills, TP is automatically cancelled, and vice versa.
    When take_profit_price is None, only a plain GTC stop-loss is placed
    (typical for trailing-stop strategies where TP is managed by the system).

    Args:
        ib:                 Connected ib_insync.IB instance.
        contract:           Qualified IB contract (call ib.qualifyContracts first).
        position_qty:       Number of shares to protect (positive integer).
        stop_price:         Hard stop price for the SL order (auxPrice).
        take_profit_price:  Limit price for the TP order.
                            Pass None or 0 to place SL only.
        account_id:         IB account ID. Uses account default if empty.

    Returns:
        dict with keys:
            success:       bool
            sl_order_id:   str | None
            tp_order_id:   str | None
            oca_group:     str (empty string when SL-only)
            message:       str
    """
    from ib_insync import StopOrder, LimitOrder

    symbol = contract.symbol
    use_oca = bool(take_profit_price and take_profit_price > 0)
    oca_group = _make_oca_group(symbol) if use_oca else ""

    result: dict = {
        "success": False,
        "sl_order_id": None,
        "tp_order_id": None,
        "oca_group": oca_group,
        "message": "",
    }

    try:
        # ── Stop-loss order ────────────────────────────────────────────────
        sl_order = StopOrder("SELL", position_qty, round(stop_price, 4))
        sl_order.tif = "GTC"
        sl_order.overridePercentageConstraints = True
        sl_order.orderRef = f"atlas_sl_{symbol}"[:32]
        if account_id:
            sl_order.account = account_id
        if use_oca:
            sl_order.ocaGroup = oca_group
            sl_order.ocaType = 1  # Cancel all remaining with block
        # transmit=True — OCA orders are independent (no parent/child chain)
        sl_order.transmit = True

        sl_trade = ib.placeOrder(contract, sl_order)
        logger.info(
            "Placed SL order: %s SELL %d @ stop=%.4f [%s]",
            symbol, position_qty, stop_price,
            f"OCA={oca_group}" if use_oca else "SL-only",
        )

        # ── Take-profit order (optional) ───────────────────────────────────
        tp_trade = None
        if use_oca:
            tp_order = LimitOrder("SELL", position_qty, round(take_profit_price, 4))
            tp_order.tif = "GTC"
            tp_order.overridePercentageConstraints = True
            tp_order.orderRef = f"atlas_tp_{symbol}"[:32]
            if account_id:
                tp_order.account = account_id
            tp_order.ocaGroup = oca_group
            tp_order.ocaType = 1
            tp_order.transmit = True

            tp_trade = ib.placeOrder(contract, tp_order)
            logger.info(
                "Placed TP order: %s SELL %d @ limit=%.4f [OCA=%s]",
                symbol, position_qty, take_profit_price, oca_group,
            )

        ib.sleep(2)  # Wait for order acknowledgement from gateway

        # ── Collect results ────────────────────────────────────────────────
        sl_status = sl_trade.orderStatus.status if sl_trade.orderStatus else "?"
        sl_id = str(sl_trade.order.orderId)

        tp_id = None
        if tp_trade:
            tp_status = tp_trade.orderStatus.status if tp_trade.orderStatus else "?"
            tp_id = str(tp_trade.order.orderId)
            logger.info(
                "Protective orders confirmed: SL=%s(%s) TP=%s(%s) OCA=%s [%s]",
                sl_id, sl_status, tp_id, tp_status, oca_group, symbol,
            )
        else:
            logger.info(
                "SL-only order confirmed: SL=%s(%s) [%s]",
                sl_id, sl_status, symbol,
            )

        result.update({
            "success": True,
            "sl_order_id": sl_id,
            "tp_order_id": tp_id,
            "message": (
                f"SL={sl_id} TP={tp_id} OCA={oca_group}"
                if tp_id else f"SL={sl_id} (SL-only)"
            ),
        })

    except Exception as e:
        logger.error(
            "place_protective_orders failed for %s: %s", symbol, e, exc_info=True,
        )
        result["message"] = str(e)[:200]

    return result


def get_existing_protective_orders(
    ib,
    contract,
    account_id: str = "",
) -> dict:
    """Return active SL/TP orders placed by Atlas for a contract.

    Scans open trades for stop-loss orders (identified by orderRef prefix
    "atlas_sl_" or ocaGroup prefix "atlas_oca_") and take-profit limit orders
    (orderRef prefix "atlas_tp_" or same OCA group).

    Args:
        ib:          Connected ib_insync.IB instance.
        contract:    IB contract to filter by (matched on symbol + secType).
        account_id:  If provided, only return orders for this account.

    Returns:
        dict with keys:
            sl_orders:  list[Trade] — active stop-loss trades
            tp_orders:  list[Trade] — active take-profit trades
            oca_groups: set[str]   — OCA group names found
            has_sl:     bool
            has_tp:     bool
            protected:  bool (True if at least one SL exists)
    """
    sl_orders: list = []
    tp_orders: list = []
    oca_groups: set[str] = set()

    for trade in ib.openTrades():
        t_contract = trade.contract
        t_order = trade.order

        # Match contract by symbol and secType
        if not _contracts_match(t_contract, contract):
            continue

        # Optionally filter by account
        if account_id and t_order.account and t_order.account != account_id:
            continue

        if _is_our_sl_order(t_order):
            sl_orders.append(trade)
            if t_order.ocaGroup:
                oca_groups.add(t_order.ocaGroup)

        elif _is_our_tp_order(t_order):
            tp_orders.append(trade)
            if t_order.ocaGroup:
                oca_groups.add(t_order.ocaGroup)

    return {
        "sl_orders": sl_orders,
        "tp_orders": tp_orders,
        "oca_groups": oca_groups,
        "has_sl": len(sl_orders) > 0,
        "has_tp": len(tp_orders) > 0,
        "protected": len(sl_orders) > 0,
    }


def cancel_protective_orders(
    ib,
    contract,
    account_id: str = "",
) -> dict:
    """Cancel all Atlas-managed SL/TP orders for a contract.

    Finds and cancels all stop-loss and take-profit orders placed by Atlas
    for the given contract. Safe to call when no orders exist (returns 0 cancelled).

    Args:
        ib:          Connected ib_insync.IB instance.
        contract:    IB contract whose protective orders should be cancelled.
        account_id:  If provided, restrict cancellation to this account.

    Returns:
        dict with keys:
            cancelled:  int  — number of orders successfully cancelled
            failed:     int  — number of cancellation attempts that failed
            order_ids:  list[str] — order IDs that were cancelled
    """
    symbol = contract.symbol
    existing = get_existing_protective_orders(ib, contract, account_id)
    all_trades = existing["sl_orders"] + existing["tp_orders"]

    if not all_trades:
        logger.debug("cancel_protective_orders: no Atlas orders to cancel for %s", symbol)
        return {"cancelled": 0, "failed": 0, "order_ids": []}

    cancelled, failed = 0, 0
    order_ids: list[str] = []

    for trade in all_trades:
        order_id = str(trade.order.orderId)
        order_type = trade.order.orderType or "?"
        try:
            ib.cancelOrder(trade.order)
            ib.sleep(0.3)  # brief settle between cancels
            logger.info(
                "Cancelled %s order %s for %s", order_type, order_id, symbol,
            )
            cancelled += 1
            order_ids.append(order_id)
        except Exception as e:
            logger.warning(
                "Failed to cancel %s order %s for %s: %s",
                order_type, order_id, symbol, e,
            )
            failed += 1

    if cancelled > 0:
        ib.sleep(1)  # final settle after batch cancel

    logger.info(
        "cancel_protective_orders for %s: cancelled=%d failed=%d",
        symbol, cancelled, failed,
    )
    return {"cancelled": cancelled, "failed": failed, "order_ids": order_ids}


def update_stop_price(
    ib,
    contract,
    new_stop: float,
    account_id: str = "",
) -> dict:
    """Modify the trigger price of an existing Atlas SL order.

    Finds the first active Atlas stop-loss order for the contract and
    updates its auxPrice (trigger price) via ib.placeOrder() — IB's
    standard order modification API (same orderId, new auxPrice).

    Args:
        ib:          Connected ib_insync.IB instance.
        contract:    The contract whose SL order to modify.
        new_stop:    New stop-loss trigger price.
        account_id:  If provided, restrict lookup to this account.

    Returns:
        dict with keys:
            success:    bool
            order_id:   str
            old_stop:   float
            new_stop:   float
            message:    str
    """
    symbol = contract.symbol
    existing = get_existing_protective_orders(ib, contract, account_id)

    if not existing["sl_orders"]:
        msg = f"No Atlas SL order found for {symbol}"
        logger.warning("update_stop_price: %s", msg)
        return {"success": False, "message": msg, "order_id": "", "old_stop": 0, "new_stop": new_stop}

    # Take the first (there should only be one per contract from Atlas)
    trade = existing["sl_orders"][0]
    order = trade.order
    old_stop = float(order.auxPrice or 0)
    order_id = str(order.orderId)

    try:
        order.auxPrice = round(new_stop, 4)
        ib.placeOrder(contract, order)
        ib.sleep(1)

        logger.info(
            "Updated SL for %s: order=%s %.4f → %.4f",
            symbol, order_id, old_stop, new_stop,
        )
        return {
            "success": True,
            "order_id": order_id,
            "old_stop": old_stop,
            "new_stop": new_stop,
            "message": f"SL updated {old_stop:.4f} → {new_stop:.4f}",
        }

    except Exception as e:
        logger.error("update_stop_price failed for %s: %s", symbol, e, exc_info=True)
        return {
            "success": False,
            "order_id": order_id,
            "old_stop": old_stop,
            "new_stop": new_stop,
            "message": str(e)[:200],
        }


def sync_protective_orders(
    ib,
    positions: list[dict],
    plan_entries: list[dict] | None = None,
    account_id: str = "",
) -> dict:
    """Ensure every position in the list has broker-side protective orders.

    For each position:
      1. Check if an Atlas SL order already exists — skip if protected.
      2. Merge stop_price / take_profit_price from position dict and plan_entries.
      3. Place protective orders if stop_price is known and SL is missing.

    Args:
        ib:           Connected ib_insync.IB instance.
        positions:    List of dicts describing live positions. Expected keys:
                          contract:          ib_insync qualified Contract
                          qty:               int — shares held (positive)
                          ticker:            str — Atlas ticker for logging
                          stop_price:        float — hard stop (may be 0 if unknown)
                          take_profit_price: float — TP target (optional, 0 = none)
        plan_entries: Optional supplementary plan data (list of dicts with
                      ticker, stop_price, take_profit / take_profit_price).
                      Used to fill in stop_price when not in position dict.
        account_id:   IB account ID.

    Returns:
        dict:
            positions_checked:  int
            already_protected:  int
            orders_placed:      int
            no_stop_price:      int
            failed:             int
            details:            list[dict] — per-position action taken
    """
    # Build plan lookup by ticker for supplementary stop/tp data
    plan_by_ticker: dict[str, dict] = {}
    for entry in (plan_entries or []):
        t = entry.get("ticker", "")
        if t:
            plan_by_ticker[t] = entry

    summary: dict = {
        "positions_checked": 0,
        "already_protected": 0,
        "orders_placed": 0,
        "no_stop_price": 0,
        "failed": 0,
        "details": [],
    }

    for pos in positions:
        contract = pos.get("contract")
        if contract is None:
            logger.warning("sync_protective_orders: position missing contract, skipping")
            continue

        ticker = pos.get("ticker", contract.symbol)
        qty = int(pos.get("qty", 0))
        summary["positions_checked"] += 1

        if qty <= 0:
            logger.debug("sync: %s has qty=%d (zero/short), skipping", ticker, qty)
            continue

        # Merge stop/tp from position dict + plan data
        plan = plan_by_ticker.get(ticker, {})
        stop_price: float = pos.get("stop_price") or plan.get("stop_price", 0)
        raw_tp = (
            pos.get("take_profit_price")
            or plan.get("take_profit_price")
            or plan.get("take_profit", 0)
        )
        tp_price: Optional[float] = float(raw_tp) if raw_tp else None

        # Check existing Atlas protective orders
        existing = get_existing_protective_orders(ib, contract, account_id)
        if existing["protected"]:
            summary["already_protected"] += 1
            n_sl = len(existing["sl_orders"])
            n_tp = len(existing["tp_orders"])
            logger.info(
                "sync: %s already protected — SL=%d TP=%d", ticker, n_sl, n_tp,
            )
            summary["details"].append({
                "ticker": ticker,
                "action": "skipped",
                "reason": f"already protected (SL={n_sl} TP={n_tp})",
                "has_sl": existing["has_sl"],
                "has_tp": existing["has_tp"],
            })
            continue

        if not stop_price:
            summary["no_stop_price"] += 1
            logger.warning("sync: no stop_price for %s — cannot place SL", ticker)
            summary["details"].append({
                "ticker": ticker,
                "action": "skipped",
                "reason": "no stop_price available",
            })
            continue

        logger.info(
            "sync: placing protective orders for %s qty=%d sl=%.4f tp=%s",
            ticker, qty, stop_price,
            f"{tp_price:.4f}" if tp_price else "none",
        )

        placed = place_protective_orders(
            ib, contract, qty,
            stop_price=stop_price,
            take_profit_price=tp_price,
            account_id=account_id,
        )

        if placed["success"]:
            summary["orders_placed"] += 1
        else:
            summary["failed"] += 1

        summary["details"].append({
            "ticker": ticker,
            "action": "placed" if placed["success"] else "failed",
            "sl_order_id": placed.get("sl_order_id"),
            "tp_order_id": placed.get("tp_order_id"),
            "stop_price": stop_price,
            "take_profit_price": tp_price,
            "oca_group": placed.get("oca_group", ""),
            "message": placed.get("message", ""),
        })

    logger.info(
        "sync_protective_orders complete: checked=%d ok=%d placed=%d no_stop=%d failed=%d",
        summary["positions_checked"],
        summary["already_protected"],
        summary["orders_placed"],
        summary["no_stop_price"],
        summary["failed"],
    )
    return summary
