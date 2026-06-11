"""brokers/ib_web/broker.py — Interactive Brokers adapter over the HEADLESS Web REST API.

Implements the Atlas ``BrokerAdapter`` against ``api.ibkr.com`` / the Client Portal Gateway (localhost), so no
TWS/IB-Gateway GUI is needed — the right transport for the autonomous VPS. The HTTP layer is INJECTABLE, so the
endpoint translation + the order-reply-confirm loop are unit-testable with a fake (no live gateway). Reuses the
MICRO_FUTURES contract table from the ib_insync adapter. See tasks/IB_WEBAPI_INTEGRATION.md for the endpoint map.
"""
from __future__ import annotations

import logging
import re
from typing import Optional

from atlas.brokers.base import (AccountInfo, BrokerAdapter, OrderResult, OrderSide, OrderStatus, OrderType, PositionInfo)
from atlas.brokers.ib.broker import MICRO_FUTURES

logger = logging.getLogger("atlas.broker.ib_web")

_ORDER_TYPE = {"MARKET": "MKT", "LIMIT": "LMT", "STOP": "STP", "STOP_LIMIT": "STOP_LIMIT"}


def _map_status(s: str) -> OrderStatus:
    s = (s or "").lower()
    return {"filled": OrderStatus.FILLED, "submitted": OrderStatus.SUBMITTED, "presubmitted": OrderStatus.SUBMITTED,
            "pendingsubmit": OrderStatus.PENDING, "cancelled": OrderStatus.CANCELLED,
            "pendingcancel": OrderStatus.PENDING, "inactive": OrderStatus.FAILED}.get(s, OrderStatus.UNKNOWN)


def _num(v) -> Optional[float]:
    m = re.search(r"-?\d+(\.\d+)?", str(v)) if v is not None else None
    return float(m.group()) if m else None


class _RealHttp:
    """Default HTTP client over the CP-Gateway (self-signed localhost) or api.ibkr.com (Bearer)."""

    def __init__(self, base_url: str, bearer: Optional[str] = None, verify: bool = False):
        import requests
        self._s = requests.Session()
        self._base = base_url.rstrip("/")
        self._verify = verify
        if bearer:
            self._s.headers["Authorization"] = f"Bearer {bearer}"

    def get(self, path, params=None):
        r = self._s.get(self._base + path, params=params, verify=self._verify, timeout=15); r.raise_for_status(); return r.json()

    def post(self, path, json=None):
        r = self._s.post(self._base + path, json=json, verify=self._verify, timeout=15); r.raise_for_status(); return r.json()

    def delete(self, path):
        r = self._s.delete(self._base + path, verify=self._verify, timeout=15); r.raise_for_status(); return r.json()


class IBWebBroker(BrokerAdapter):
    def __init__(self, config: dict, *, http=None):
        super().__init__(config)
        ib = (config or {}).get("ib", {})
        self._mode = (config or {}).get("trading", {}).get("mode", "paper")
        self.base_url = ib.get("base_url", "https://localhost:5000/v1/api")
        self.account_id = ib.get("account_id")
        self._bearer = ib.get("bearer")
        self._http = http                       # injectable; built on connect() if None
        self._conids: dict = {}
        self._rev: dict = {}

    @property
    def name(self) -> str:
        return "ib_web"

    @property
    def is_live(self) -> bool:
        return self._mode == "live"

    # ── lifecycle ──────────────────────────────────────────────
    def connect(self) -> bool:
        if self._http is None:
            try:
                self._http = _RealHttp(self.base_url, self._bearer)
            except Exception as e:
                logger.error("ib_web HTTP init failed (pip install requests / start CP gateway): %s", e)
                return False
        try:
            self._http.post("/iserver/auth/ssodh/init", {"publish": True, "compete": True})
        except Exception:
            pass                                # CP gateway may already hold a session
        try:
            st = self._http.post("/iserver/auth/status", {}) or {}
            self._connected = bool(st.get("authenticated"))
        except Exception as e:
            logger.error("ib_web auth status failed: %s", e)
            return False
        if self._connected:
            try:
                accts = self._http.get("/iserver/accounts") or {}
                self.account_id = self.account_id or accts.get("selectedAccount") or (accts.get("accounts") or [None])[0]
                self._http.get("/portfolio/accounts")   # required pre-flight for /portfolio endpoints
            except Exception as e:
                logger.warning("ib_web account preflight failed: %s", e)
        return self._connected

    def disconnect(self):
        self._connected = False

    # ── contract resolution ────────────────────────────────────
    def _conid(self, ticker: str) -> int:
        t = ticker.upper()
        if t in self._conids:
            return self._conids[t]
        spec = MICRO_FUTURES.get(t)
        if not spec:
            raise ValueError(f"unknown micro-futures symbol '{ticker}'")
        r = self._http.get("/trsrv/futures", {"symbols": t, "exchange": spec["exchange"]}) or {}
        lst = r.get(t) or []
        if not lst:
            raise ValueError(f"no live futures conid for {ticker}")
        conid = int(lst[0]["conid"])            # front month (earliest non-expired)
        self._conids[t] = conid
        self._rev[conid] = t
        return conid

    def multiplier(self, ticker: str) -> float:
        return float(MICRO_FUTURES[ticker.upper()]["multiplier"])

    def _symbol_for(self, pos: dict) -> str:
        """Base symbol for a position: reverse conid map, else match the local symbol (e.g. 'MESZ5' -> 'MES')."""
        c = int(pos.get("conid", 0) or 0)
        if c in self._rev:
            return self._rev[c]
        desc = (pos.get("description") or pos.get("contractDesc") or "").upper()
        for sym in MICRO_FUTURES:
            if desc.startswith(sym):
                return sym
        return desc

    # ── account / positions / prices ───────────────────────────
    def get_account_info(self) -> AccountInfo:
        try:
            s = self._http.get(f"/iserver/account/{self.account_id}/summary") or {}
        except Exception as e:
            logger.warning("get_account_info failed: %s", e)
            s = {}
        return AccountInfo(equity=float(s.get("netLiquidationValue", 0) or 0), cash=float(s.get("totalCashValue", 0) or 0),
                           buying_power=float(s.get("buyingPower", 0) or 0), currency="USD", market_id="ib_futures")

    def get_positions(self) -> list[PositionInfo]:
        out: list[PositionInfo] = []
        try:
            for p in (self._http.get(f"/portfolio2/{self.account_id}/positions") or []):
                qty = int(p.get("position", 0) or 0)
                if qty == 0:
                    continue
                sym = self._symbol_for(p)
                out.append(PositionInfo(ticker=sym, shares=qty,
                                        entry_price=float(p.get("avgPrice", p.get("avgCost", 0)) or 0),
                                        current_price=float(p.get("marketPrice", 0) or 0),
                                        market_value=float(p.get("marketValue", 0) or 0),
                                        currency=p.get("currency", "USD")))
        except Exception as e:
            logger.warning("get_positions failed: %s", e)
        return out

    def get_prices(self, tickers: list[str]) -> dict[str, float]:
        out: dict = {}
        c2t = {}
        for t in tickers:
            try:
                c2t[self._conid(t)] = t
            except Exception:
                continue
        if not c2t:
            return out
        try:
            snap = self._http.get("/iserver/marketdata/snapshot",
                                  {"conids": ",".join(str(c) for c in c2t), "fields": "31"}) or []
            for row in snap:
                c = int(row.get("conid", 0) or 0)
                px = _num(row.get("31"))
                if c in c2t and px is not None:
                    out[c2t[c]] = px
        except Exception as e:
            logger.warning("get_prices failed: %s", e)
        return out

    # ── orders ─────────────────────────────────────────────────
    def place_order(self, ticker: str, side: OrderSide, qty: int, price: float,
                    order_type: OrderType = OrderType.MARKET, stop_price: Optional[float] = None,
                    remark: str = "") -> OrderResult:
        try:
            ot = _ORDER_TYPE.get(order_type.name, "MKT")
            order = {"conid": self._conid(ticker), "orderType": ot, "tif": "DAY",
                     "side": "BUY" if side == OrderSide.BUY else "SELL", "quantity": abs(int(qty))}
            if ot in ("LMT", "STOP_LIMIT"):
                order["price"] = float(price)
            if stop_price and ot in ("STP", "STOP_LIMIT"):
                order["auxPrice"] = float(stop_price)
            if remark:
                order["cOID"] = remark[:40]
            resp = self._http.post(f"/iserver/account/{self.account_id}/orders", {"orders": [order]})
            return self._resolve_reply(resp, ticker, side, abs(int(qty)), price)
        except Exception as e:
            return OrderResult(success=False, ticker=ticker, side=side, status=OrderStatus.FAILED, message=str(e))

    def _resolve_reply(self, resp, ticker, side, qty, price, depth: int = 0) -> OrderResult:
        """Walk the order-reply-confirm chain: warnings carry a replyId that must be confirmed before the order
        is live. Returns once a real order_id (or an error) is reached."""
        if isinstance(resp, dict):
            if resp.get("error"):
                return OrderResult(success=False, ticker=ticker, side=side, status=OrderStatus.FAILED, message=str(resp["error"]))
            resp = [resp]
        if not resp:
            return OrderResult(success=False, ticker=ticker, side=side, message="empty order response")
        first = resp[0]
        if "order_id" in first:
            return OrderResult(success=True, order_id=str(first["order_id"]), ticker=ticker, side=side,
                               status=_map_status(first.get("order_status", "")), requested_qty=qty,
                               requested_price=float(price), raw=first)
        if "id" in first and ("message" in first or "messageIds" in first):
            if depth >= 5:
                return OrderResult(success=False, ticker=ticker, side=side, message="too many order-reply prompts")
            confirm = self._http.post(f"/iserver/reply/{first['id']}", {"confirmed": True})
            return self._resolve_reply(confirm, ticker, side, qty, price, depth + 1)
        return OrderResult(success=False, ticker=ticker, side=side, message=str(first))

    def cancel_order(self, order_id: str) -> OrderResult:
        try:
            r = self._http.delete(f"/iserver/account/{self.account_id}/order/{order_id}") or {}
            ok = bool(r.get("order_id") or r.get("msg"))
            return OrderResult(success=ok, order_id=str(order_id),
                               status=OrderStatus.CANCELLED if ok else OrderStatus.UNKNOWN, message=str(r.get("error", "")))
        except Exception as e:
            return OrderResult(success=False, order_id=str(order_id), message=str(e))

    def cancel_all_orders(self) -> list[OrderResult]:
        return [self.cancel_order(o.order_id) for o in self.get_open_orders() if o.order_id]

    def get_open_orders(self) -> list[OrderResult]:
        out = []
        try:
            for o in ((self._http.get("/iserver/account/orders") or {}).get("orders") or []):
                out.append(OrderResult(success=True, order_id=str(o.get("orderId", "")), ticker=o.get("ticker", ""),
                                       side=OrderSide.BUY if o.get("side") == "BUY" else OrderSide.SELL,
                                       status=_map_status(o.get("status", "")),
                                       filled_qty=int(_num(o.get("filledQuantity", 0)) or 0)))
        except Exception as e:
            logger.warning("get_open_orders failed: %s", e)
        return out

    def get_order_status(self, order_id: str) -> OrderResult:
        try:
            o = self._http.get(f"/iserver/account/order/status/{order_id}") or {}
            return OrderResult(success=True, order_id=str(order_id), status=_map_status(o.get("order_status", "")),
                               filled_qty=int(_num(o.get("cum_fill", 0)) or 0), fill_price=float(_num(o.get("average_price", 0)) or 0))
        except Exception as e:
            return OrderResult(success=False, order_id=str(order_id), message=str(e))
