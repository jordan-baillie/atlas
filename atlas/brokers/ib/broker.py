"""brokers/ib/broker.py — Interactive Brokers adapter, MICRO-FUTURES first (for the BOREAS carry+trend book).

Implements the Atlas ``BrokerAdapter`` contract over ``ib_insync``, so the ``TargetExecutor`` + reconcile +
kill-switch substrate work unchanged. Supports BUY and SELL (long-SHORT), integer contracts, contract
multipliers, paper (port 7497) and live (7496). The ``ib`` client is injectable for testing — live connection
needs a running IB Gateway / TWS (lands when BOREAS deploys, ~2026-08-28).
"""
from __future__ import annotations

import logging
from typing import Optional

from atlas.brokers.base import (AccountInfo, BrokerAdapter, DealInfo, OrderResult, OrderSide, OrderStatus, OrderType,
                          PositionInfo)

logger = logging.getLogger("atlas.broker.ib")

# Micro-futures contract table: symbol -> (exchange, currency, multiplier $/point).
MICRO_FUTURES = {
    "MES": {"exchange": "CME", "currency": "USD", "multiplier": 5.0},     # Micro E-mini S&P 500
    "MNQ": {"exchange": "CME", "currency": "USD", "multiplier": 2.0},     # Micro E-mini Nasdaq-100
    "M2K": {"exchange": "CME", "currency": "USD", "multiplier": 5.0},     # Micro E-mini Russell 2000
    "MYM": {"exchange": "CBOT", "currency": "USD", "multiplier": 0.5},    # Micro E-mini Dow
    "MGC": {"exchange": "COMEX", "currency": "USD", "multiplier": 10.0},  # Micro Gold
    "SIL": {"exchange": "COMEX", "currency": "USD", "multiplier": 1000.0},  # Micro Silver
    "MCL": {"exchange": "NYMEX", "currency": "USD", "multiplier": 100.0},   # Micro WTI Crude
    "M6E": {"exchange": "CME", "currency": "USD", "multiplier": 12500.0},   # Micro EUR/USD
    "MBT": {"exchange": "CME", "currency": "USD", "multiplier": 0.1},     # Micro Bitcoin
}


class IBBroker(BrokerAdapter):
    def __init__(self, config: dict, *, ib=None):
        super().__init__(config)
        ibcfg = (config or {}).get("ib", {})
        self._mode = (config or {}).get("trading", {}).get("mode", "paper")
        self.host = ibcfg.get("host", "127.0.0.1")
        self.port = int(ibcfg.get("port", 7497 if self._mode != "live" else 7496))
        self.client_id = int(ibcfg.get("client_id", 17))
        self._ib = ib                       # injectable; None until connect()
        self._contracts: dict = {}

    # ── identity ───────────────────────────────────────────────
    @property
    def name(self) -> str:
        return "ib"

    @property
    def is_live(self) -> bool:
        return self._mode == "live"

    @property
    def is_connected(self) -> bool:
        return bool(self._ib and self._ib.isConnected())

    # ── contract construction ──────────────────────────────────
    def _spec(self, ticker: str) -> dict:
        spec = MICRO_FUTURES.get(ticker.upper())
        if not spec:
            raise ValueError(f"unknown micro-futures symbol '{ticker}' (add to MICRO_FUTURES)")
        return spec

    def multiplier(self, ticker: str) -> float:
        return float(self._spec(ticker)["multiplier"])

    def _contract(self, ticker: str):
        """Build (and cache) a continuous-future contract for the front month."""
        if ticker in self._contracts:
            return self._contracts[ticker]
        from ib_insync import ContFuture
        s = self._spec(ticker)
        c = ContFuture(symbol=ticker.upper(), exchange=s["exchange"], currency=s["currency"])
        if self._ib:
            try:
                (c,) = self._ib.qualifyContracts(c) or (c,)
            except Exception as e:
                logger.warning("qualifyContracts(%s) failed: %s", ticker, e)
        self._contracts[ticker] = c
        return c

    # ── lifecycle ──────────────────────────────────────────────
    def connect(self) -> bool:
        if self.is_connected:
            return True
        try:
            from ib_insync import IB
        except ImportError as e:
            logger.error("ib_insync not installed (pip install ib_insync): %s", e)
            return False
        self._ib = self._ib or IB()
        try:
            self._ib.connect(self.host, self.port, clientId=self.client_id, timeout=15)
            self._connected = self._ib.isConnected()
            logger.info("IB connected %s:%s (mode=%s)", self.host, self.port, self._mode)
            return self._connected
        except Exception as e:
            logger.error("IB connect failed %s:%s — %s", self.host, self.port, e)
            return False

    def disconnect(self):
        try:
            if self._ib:
                self._ib.disconnect()
        finally:
            self._connected = False

    # ── account / positions ────────────────────────────────────
    def get_account_info(self) -> AccountInfo:
        equity = cash = bp = 0.0
        try:
            for v in self._ib.accountSummary():
                if v.tag == "NetLiquidation":
                    equity = float(v.value)
                elif v.tag in ("TotalCashValue", "CashBalance") and v.currency in ("USD", "BASE", ""):
                    cash = float(v.value)
                elif v.tag in ("BuyingPower", "AvailableFunds"):
                    bp = float(v.value)
        except Exception as e:
            logger.warning("get_account_info failed: %s", e)
        return AccountInfo(equity=equity, cash=cash, buying_power=bp or cash, currency="USD",
                           market_id="ib_futures", num_positions=len(self.get_positions()))

    def get_positions(self) -> list[PositionInfo]:
        out: list[PositionInfo] = []
        try:
            for p in self._ib.positions():
                sym = getattr(p.contract, "symbol", "") or getattr(p.contract, "localSymbol", "")
                qty = int(p.position)
                if qty == 0:
                    continue
                mult = MICRO_FUTURES.get(sym, {}).get("multiplier", 1.0)
                avg = float(p.avgCost) / mult if mult else float(p.avgCost)   # IB avgCost is per-contract notional
                out.append(PositionInfo(ticker=sym, shares=qty, entry_price=avg, cost_basis=float(p.avgCost),
                                        currency=getattr(p.contract, "currency", "USD")))
        except Exception as e:
            logger.warning("get_positions failed: %s", e)
        return out

    def get_prices(self, tickers: list[str]) -> dict[str, float]:
        out: dict = {}
        try:
            cons = [self._contract(t) for t in tickers]
            for tk in self._ib.reqTickers(*cons):
                sym = getattr(tk.contract, "symbol", "")
                px = tk.marketPrice()
                if px and px == px:   # not NaN
                    out[sym] = float(px)
        except Exception as e:
            logger.warning("get_prices failed: %s", e)
        return out

    # ── orders ─────────────────────────────────────────────────
    def place_order(self, ticker: str, side: OrderSide, qty: int, price: float,
                    order_type: OrderType = OrderType.MARKET, stop_price: Optional[float] = None,
                    remark: str = "") -> OrderResult:
        try:
            from ib_insync import LimitOrder, MarketOrder
            action = "BUY" if side == OrderSide.BUY else "SELL"
            order = (MarketOrder(action, abs(int(qty))) if order_type == OrderType.MARKET
                     else LimitOrder(action, abs(int(qty)), float(price)))
            if remark:
                order.orderRef = remark[:32]
            trade = self._ib.placeOrder(self._contract(ticker), order)
            st = getattr(getattr(trade, "orderStatus", None), "status", "") or ""
            filled = int(getattr(getattr(trade, "orderStatus", None), "filled", 0) or 0)
            avg = float(getattr(getattr(trade, "orderStatus", None), "avgFillPrice", 0.0) or 0.0)
            return OrderResult(success=True, order_id=str(getattr(getattr(trade, "order", None), "orderId", "")),
                               ticker=ticker, side=side, status=_map_status(st), requested_qty=abs(int(qty)),
                               filled_qty=filled, requested_price=float(price), fill_price=avg or float(price),
                               raw={"status": st})
        except Exception as e:
            logger.error("place_order(%s %s %s) failed: %s", action if 'action' in dir() else side, ticker, qty, e)
            return OrderResult(success=False, ticker=ticker, side=side, status=OrderStatus.FAILED, message=str(e))

    def cancel_order(self, order_id: str) -> OrderResult:
        try:
            for t in self._ib.openTrades():
                if str(getattr(t.order, "orderId", "")) == str(order_id):
                    self._ib.cancelOrder(t.order)
                    return OrderResult(success=True, order_id=order_id, status=OrderStatus.CANCELLED)
        except Exception as e:
            return OrderResult(success=False, order_id=order_id, message=str(e))
        return OrderResult(success=False, order_id=order_id, message="order not found")

    def cancel_all_orders(self) -> list[OrderResult]:
        res = []
        try:
            for t in list(self._ib.openTrades()):
                self._ib.cancelOrder(t.order)
                res.append(OrderResult(success=True, order_id=str(getattr(t.order, "orderId", "")),
                                       status=OrderStatus.CANCELLED))
        except Exception as e:
            logger.warning("cancel_all_orders failed: %s", e)
        return res

    def get_open_orders(self) -> list[OrderResult]:
        out = []
        try:
            for t in self._ib.openTrades():
                out.append(OrderResult(success=True, order_id=str(getattr(t.order, "orderId", "")),
                                       ticker=getattr(t.contract, "symbol", ""),
                                       status=_map_status(getattr(t.orderStatus, "status", ""))))
        except Exception as e:
            logger.warning("get_open_orders failed: %s", e)
        return out

    def get_order_status(self, order_id: str) -> OrderResult:
        try:
            for t in self._ib.trades():
                if str(getattr(t.order, "orderId", "")) == str(order_id):
                    os_ = t.orderStatus
                    return OrderResult(success=True, order_id=order_id, status=_map_status(os_.status),
                                       filled_qty=int(getattr(os_, "filled", 0) or 0),
                                       fill_price=float(getattr(os_, "avgFillPrice", 0.0) or 0.0))
        except Exception as e:
            return OrderResult(success=False, order_id=order_id, message=str(e))
        return OrderResult(success=False, order_id=order_id, status=OrderStatus.UNKNOWN, message="not found")


def _map_status(ib_status: str) -> OrderStatus:
    s = (ib_status or "").lower()
    return {
        "filled": OrderStatus.FILLED, "submitted": OrderStatus.SUBMITTED, "presubmitted": OrderStatus.SUBMITTED,
        "pendingsubmit": OrderStatus.PENDING, "cancelled": OrderStatus.CANCELLED, "apicancelled": OrderStatus.CANCELLED,
        "inactive": OrderStatus.FAILED,
    }.get(s, OrderStatus.UNKNOWN)
