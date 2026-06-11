"""brokers/target_executor.py — Target-weight executor (the productionization bridge).

Turns a strategy's ``{symbol: target_weight}`` into live orders on ANY ``BrokerAdapter``. This is the NEW
execution model for the forge->live system: **target-weight, long-SHORT, vol-targeted upstream, NO per-trade
stops** (portfolio risk = the L1–L4 kill-switch + vol-targeting done in the weights). It REPLACES the long-only
entry+stop ``plan.py`` / ``live_executor.py`` swing model.

Reuses the Atlas substrate: ``brokers/base.py`` (BrokerAdapter + types) + ``core/remediation_kill_switch``.

Sizing:  ``target_qty[s] = round( w[s] * deployable_equity / (price[s] * multiplier[s]) )`` (rounded to lot).
Diff vs current positions -> orders. Negative target_qty => short (SELL to open). ``dry_run`` previews only.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from atlas.brokers.base import BrokerAdapter, OrderResult, OrderSide, OrderType

logger = logging.getLogger("atlas.target_executor")


@dataclass
class ContractSpec:
    """Per-symbol economics. Equities: multiplier=1, lot=1. Futures: multiplier=$/point, lot=1 contract."""
    multiplier: float = 1.0
    lot: int = 1
    min_notional: float = 0.0   # skip a rebalance delta below this $ notional (no dust trades)


@dataclass
class TargetOrder:
    ticker: str
    side: OrderSide
    qty: int
    ref_price: float
    target_weight: float
    delta_qty: int


@dataclass
class RebalanceReport:
    trade_date: str
    deployable_equity: float
    target_weights: dict
    current_qty: dict
    target_qty: dict
    orders: list                       # list[TargetOrder]
    results: list = field(default_factory=list)   # list[OrderResult]
    turnover_notional: float = 0.0
    blocked: Optional[str] = None
    dry_run: bool = True

    @property
    def n_orders(self) -> int:
        return len(self.orders)

    @property
    def executed(self) -> list:
        return [r for r in self.results if getattr(r, "success", False)]


class TargetExecutor:
    """Diff a target-weight book against the broker's current positions and trade the difference."""

    def __init__(self, broker: BrokerAdapter, *, specs: Optional[dict] = None,
                 default_spec: Optional[ContractSpec] = None, min_delta_notional: float = 25.0,
                 max_order_notional: Optional[float] = None, order_type: OrderType = OrderType.MARKET,
                 db_path: Optional[str] = None):
        self.broker = broker
        self.specs = specs or {}
        self.default_spec = default_spec or ContractSpec()
        self.min_delta_notional = min_delta_notional
        self.max_order_notional = max_order_notional
        self.order_type = order_type
        self.db_path = db_path

    def _spec(self, sym: str) -> ContractSpec:
        return self.specs.get(sym, self.default_spec)

    def _current_qty(self) -> dict:
        out: dict = {}
        for p in self.broker.get_positions():
            out[p.ticker] = out.get(p.ticker, 0) + int(getattr(p, "shares", 0))
        return out

    def _resolve_prices(self, tickers, prices) -> dict:
        if prices:
            return {k: float(v) for k, v in prices.items()}
        try:
            return {k: float(v) for k, v in (self.broker.get_prices(list(tickers)) or {}).items()}
        except Exception:
            return {}

    def compute_orders(self, target_weights: dict, prices: dict, deployable_equity: float,
                       current_qty: dict) -> tuple[dict, list]:
        """Pure: target weights + prices + equity + current positions -> (target_qty, [TargetOrder]). No side effects."""
        target_qty: dict = {}
        for sym, w in target_weights.items():
            px = prices.get(sym)
            if not px or px <= 0:
                continue
            spec = self._spec(sym)
            raw = w * deployable_equity / (px * spec.multiplier)
            target_qty[sym] = int(round(raw / spec.lot) * spec.lot)
        for sym in current_qty:           # held names absent from targets -> exit to 0
            target_qty.setdefault(sym, 0)

        orders: list = []
        for sym, tq in target_qty.items():
            delta = tq - current_qty.get(sym, 0)
            px = prices.get(sym)
            if delta == 0 or not px or px <= 0:
                continue
            spec = self._spec(sym)
            if abs(delta) * px * spec.multiplier < max(self.min_delta_notional, spec.min_notional):
                continue                  # dust
            qty = abs(delta)
            if self.max_order_notional:   # cap a single order's notional
                cap = int(self.max_order_notional / (px * spec.multiplier))
                qty = min(qty, max(cap, spec.lot))
            if qty < spec.lot:
                continue
            orders.append(TargetOrder(sym, OrderSide.BUY if delta > 0 else OrderSide.SELL,
                                      int(qty), float(px), float(target_weights.get(sym, 0.0)), int(delta)))
        return target_qty, orders

    def rebalance(self, target_weights: dict, *, prices: Optional[dict] = None,
                  deployable_equity: Optional[float] = None, dry_run: bool = True,
                  check_kill_switch: bool = True, current_qty: Optional[dict] = None) -> RebalanceReport:
        """current_qty: positions to diff against. Default = the broker account's positions (single-
        strategy accounts). Multi-strategy shared accounts MUST pass their own virtual book's positions
        (live/virtual_book.py) or strategies will liquidate each other's holdings."""
        td = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        blocked = None
        if check_kill_switch:
            try:
                from atlas.execution.kill_switch import check_all_layers
                br = check_all_layers(db_path=self.db_path)
                if br:
                    blocked = f"{br.layer}: {br.reason}"
            except Exception as e:           # fail-CLOSED: if the gate can't be evaluated, do not trade
                logger.warning("kill-switch check unavailable (fail-closed): %s", e)
                blocked = f"kill-switch check error: {e}"

        if deployable_equity is None:
            try:
                deployable_equity = float(self.broker.get_account_info().equity)
            except Exception:
                deployable_equity = 0.0

        if current_qty is None:
            current_qty = self._current_qty()
        px = self._resolve_prices(set(target_weights) | set(current_qty), prices)
        target_qty, orders = self.compute_orders(target_weights, px, deployable_equity, current_qty)
        turnover = sum(o.qty * o.ref_price * self._spec(o.ticker).multiplier for o in orders)
        report = RebalanceReport(td, float(deployable_equity), dict(target_weights), current_qty, target_qty,
                                 orders, turnover_notional=turnover, blocked=blocked, dry_run=dry_run)

        if dry_run or blocked:
            if blocked:
                logger.warning("rebalance BLOCKED (%s) — %d orders computed, none placed", blocked, len(orders))
            return report

        for o in orders:
            try:
                res = self.broker.place_order(ticker=o.ticker, side=o.side, qty=o.qty, price=o.ref_price,
                                              order_type=self.order_type, remark="target_rebalance")
            except Exception as e:
                res = OrderResult(success=False, ticker=o.ticker, side=o.side, message=str(e))
            report.results.append(res)
        logger.info("rebalance executed: %d/%d orders filled, turnover $%.0f",
                    len(report.executed), len(orders), turnover)
        return report
