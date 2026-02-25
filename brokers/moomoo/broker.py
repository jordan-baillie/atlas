"""Moomoo broker implementation for Atlas.

Connects to OpenD gateway via the moomoo-api Python SDK to execute
real (or simulated) trades on the ASX through Moomoo AU.

Requirements:
    - pip install moomoo-api
    - OpenD gateway running (local or cloud)
    - Trade password set in env var MOOMOO_TRADE_PWD

All tickers at the Atlas boundary use .AX format.
Conversion to AU. format happens inside this module.
"""

from __future__ import annotations

import logging
import os
import time
import uuid
from typing import Optional

from brokers.base import (
    BrokerAdapter, OrderResult, PositionInfo, AccountInfo, DealInfo,
    OrderStatus, OrderSide, OrderType,
)
from brokers.moomoo import mapper
from brokers.secrets import get_secret

logger = logging.getLogger("atlas.broker.moomoo")

try:
    import moomoo as ft
    MOOMOO_AVAILABLE = True
except ImportError:
    MOOMOO_AVAILABLE = False
    logger.warning("moomoo-api not installed. Run: pip install moomoo-api")


# ═══════════════════════════════════════════════════════════════
# Status mapping
# ═══════════════════════════════════════════════════════════════

def _map_order_status(moomoo_status) -> OrderStatus:
    """Map Moomoo order status to Atlas OrderStatus."""
    if not MOOMOO_AVAILABLE:
        return OrderStatus.UNKNOWN
    status_map = {
        ft.OrderStatus.SUBMITTED: OrderStatus.SUBMITTED,
        ft.OrderStatus.FILLED_ALL: OrderStatus.FILLED,
        ft.OrderStatus.FILLED_PART: OrderStatus.PARTIAL_FILLED,
        ft.OrderStatus.CANCELLED_ALL: OrderStatus.CANCELLED,
        ft.OrderStatus.CANCELLED_PART: OrderStatus.CANCELLED,
        ft.OrderStatus.FAILED: OrderStatus.FAILED,
        ft.OrderStatus.DISABLED: OrderStatus.CANCELLED,
        ft.OrderStatus.DELETED: OrderStatus.CANCELLED,
    }
    return status_map.get(moomoo_status, OrderStatus.UNKNOWN)


def _map_order_type(atlas_type: OrderType):
    """Map Atlas OrderType to Moomoo OrderType."""
    if not MOOMOO_AVAILABLE:
        return None
    type_map = {
        OrderType.MARKET: ft.OrderType.MARKET,
        OrderType.LIMIT: ft.OrderType.NORMAL,
        OrderType.STOP: ft.OrderType.STOP,
        OrderType.STOP_LIMIT: ft.OrderType.STOP_LIMIT,
        OrderType.TRAILING_STOP: ft.OrderType.TRAILING_STOP,
    }
    return type_map.get(atlas_type, ft.OrderType.NORMAL)


def _map_side(side: OrderSide):
    """Map Atlas OrderSide to Moomoo TrdSide."""
    if not MOOMOO_AVAILABLE:
        return None
    return ft.TrdSide.BUY if side == OrderSide.BUY else ft.TrdSide.SELL


# ═══════════════════════════════════════════════════════════════
# Moomoo Broker
# ═══════════════════════════════════════════════════════════════

class MomooBroker(BrokerAdapter):
    """Live/simulated ASX trading via Moomoo OpenD gateway."""

    def __init__(self, config: dict, live: bool = False):
        super().__init__(config)
        self._live = live

        moomoo_cfg = config.get("moomoo", {})
        self._host = moomoo_cfg.get("opend_host", "127.0.0.1")
        self._port = moomoo_cfg.get("opend_port", 11111)
        self._security_firm = moomoo_cfg.get("security_firm", "FUTUAU")
        self._currency = moomoo_cfg.get("currency", "AUD")
        self._default_order_type = moomoo_cfg.get("order_type", "NORMAL")
        self._tif = moomoo_cfg.get("time_in_force", "DAY")
        # Secret key name is hardcoded — never stored in config
        self._pwd_env = "MOOMOO_TRADE_PWD"

        self._trd_ctx = None
        self._quote_ctx = None
        self._acc_id = 0

    @property
    def name(self) -> str:
        env = "LIVE" if self._live else "SIMULATE"
        return f"MomooBroker[{env}]"

    @property
    def is_live(self) -> bool:
        return self._live

    @property
    def trd_env(self):
        if not MOOMOO_AVAILABLE:
            return None
        return ft.TrdEnv.REAL if self._live else ft.TrdEnv.SIMULATE

    # ── Lifecycle ──────────────────────────────────────────────

    def connect(self) -> bool:
        if not MOOMOO_AVAILABLE:
            logger.error("moomoo-api not installed")
            return False

        try:
            sec_firm = getattr(ft.SecurityFirm, self._security_firm,
                               ft.SecurityFirm.FUTUAU)

            # Trade context — AU market
            self._trd_ctx = ft.OpenSecTradeContext(
                filter_trdmarket=ft.TrdMarket.AU,
                host=self._host,
                port=self._port,
                security_firm=sec_firm,
            )

            # Quote context — for real-time prices
            self._quote_ctx = ft.OpenQuoteContext(
                host=self._host,
                port=self._port,
            )

            # Discover account
            ret, data = self._trd_ctx.get_acc_list()
            if ret != ft.RET_OK:
                logger.error("Failed to get account list: %s", data)
                return False

            # Find AU account matching our environment
            for _, row in data.iterrows():
                if row.get("trd_env") == str(self.trd_env):
                    self._acc_id = int(row["acc_id"])
                    logger.info("Using account %s (env=%s, firm=%s)",
                                self._acc_id, self.trd_env, sec_firm)
                    break

            if self._acc_id == 0:
                logger.warning("No matching account found, using default")

            # Unlock trade (required for placing orders)
            # Load password securely: env var → secrets file → interactive
            pwd = get_secret(self._pwd_env, prompt=False)
            if pwd:
                ret, data = self._trd_ctx.unlock_trade(password=pwd)
                if ret != ft.RET_OK:
                    logger.error("Trade unlock failed (wrong password?)")
                    return False
                logger.info("Trade unlocked via secure credential")
            else:
                logger.warning(
                    "No trade password found. Set via: "
                    "1) env var %s, 2) ~/.atlas-secrets.json, "
                    "or 3) run 'atlas setup-secrets'", self._pwd_env
                )

            self._connected = True
            logger.info("MomooBroker connected: host=%s port=%s acc=%s live=%s",
                        self._host, self._port, self._acc_id, self._live)
            return True

        except Exception as e:
            logger.error("MomooBroker connect failed: %s", e, exc_info=True)
            return False

    def disconnect(self):
        if self._trd_ctx:
            self._trd_ctx.close()
            self._trd_ctx = None
        if self._quote_ctx:
            self._quote_ctx.close()
            self._quote_ctx = None
        self._connected = False
        logger.info("MomooBroker disconnected")

    # ── Account ────────────────────────────────────────────────

    def get_account_info(self) -> AccountInfo:
        self._require_connected()
        currency = getattr(ft.Currency, self._currency, ft.Currency.AUD)

        ret, data = self._trd_ctx.accinfo_query(
            trd_env=self.trd_env,
            acc_id=self._acc_id,
            refresh_cache=True,
            currency=currency,
        )
        if ret != ft.RET_OK:
            logger.error("accinfo_query failed: %s", data)
            return AccountInfo()

        row = data.iloc[0]
        equity = float(row.get("total_assets", 0))
        cash = float(row.get("cash", row.get("avl_withdrawal_cash", 0)))
        market_val = float(row.get("market_val", row.get("total_market_val", 0)))
        power = float(row.get("power", 0))

        starting = self.config.get("risk", {}).get("starting_equity", 5000)
        pnl = round(equity - starting, 2)
        pnl_pct = round(pnl / starting * 100, 2) if starting > 0 else 0

        return AccountInfo(
            equity=round(equity, 2),
            cash=round(cash, 2),
            market_value=round(market_val, 2),
            buying_power=round(power, 2),
            total_pnl=pnl,
            total_pnl_pct=pnl_pct,
            currency=self._currency,
        )

    def get_positions(self) -> list[PositionInfo]:
        self._require_connected()

        ret, data = self._trd_ctx.position_list_query(
            trd_env=self.trd_env,
            acc_id=self._acc_id,
            refresh_cache=True,
        )
        if ret != ft.RET_OK:
            logger.error("position_list_query failed: %s", data)
            return []

        positions = []
        for _, row in data.iterrows():
            moomoo_code = row.get("code", "")
            ticker = mapper.to_atlas(moomoo_code)
            qty = int(row.get("qty", 0))
            if qty == 0:
                continue

            cost_price = float(row.get("cost_price", 0))
            current = float(row.get("market_val", 0)) / qty if qty else 0
            upnl = float(row.get("pl_val", 0))
            upnl_pct = float(row.get("pl_ratio", 0)) * 100

            positions.append(PositionInfo(
                ticker=ticker,
                entry_price=cost_price,
                shares=qty,
                current_price=round(current, 4),
                market_value=float(row.get("market_val", 0)),
                unrealized_pnl=round(upnl, 2),
                unrealized_pnl_pct=round(upnl_pct, 2),
                cost_basis=round(cost_price * qty, 2),
            ))
        return positions

    # ── Orders ─────────────────────────────────────────────────

    def place_order(
        self,
        ticker: str,
        side: OrderSide,
        qty: int,
        price: float,
        order_type: OrderType = OrderType.LIMIT,
        stop_price: Optional[float] = None,
        remark: str = "",
        **kwargs,
    ) -> OrderResult:
        self._require_connected()

        moomoo_code = mapper.to_moomoo(ticker)
        moomoo_side = _map_side(side)
        moomoo_type = _map_order_type(order_type)
        tif = getattr(ft.TimeInForce, self._tif, ft.TimeInForce.DAY)

        # Safety check for live orders
        if self._live:
            safety = self.config.get("trading", {}).get("live_safety", {})
            max_value = safety.get("max_order_value", 2000)
            order_value = price * qty
            if order_value > max_value:
                return OrderResult(
                    success=False, ticker=ticker, side=side,
                    status=OrderStatus.FAILED,
                    message=f"Order value ${order_value:.2f} exceeds "
                            f"max_order_value ${max_value}",
                )

        logger.info("Placing order: %s %s %d x %.4f (%s) [%s]",
                     side.value, moomoo_code, qty, price,
                     order_type.value, remark)

        kwargs_order = dict(
            price=price,
            qty=qty,
            code=moomoo_code,
            trd_side=moomoo_side,
            order_type=moomoo_type,
            trd_env=self.trd_env,
            acc_id=self._acc_id,
            remark=remark or f"atlas_{uuid.uuid4().hex[:8]}",
            time_in_force=tif,
        )

        # Add stop price for stop orders
        if stop_price and order_type in (OrderType.STOP, OrderType.STOP_LIMIT):
            kwargs_order["aux_price"] = stop_price

        ret, data = self._trd_ctx.place_order(**kwargs_order)

        if ret != ft.RET_OK:
            logger.error("place_order failed: %s", data)
            return OrderResult(
                success=False, ticker=ticker, side=side,
                status=OrderStatus.FAILED, requested_qty=qty,
                requested_price=price, message=str(data),
            )

        row = data.iloc[0]
        order_id = str(row.get("order_id", ""))

        logger.info("Order placed: %s %s → order_id=%s",
                     side.value, moomoo_code, order_id)

        return OrderResult(
            success=True,
            order_id=order_id,
            ticker=ticker,
            side=side,
            status=OrderStatus.SUBMITTED,
            requested_qty=qty,
            requested_price=price,
            message="Order submitted",
            raw=row.to_dict() if hasattr(row, "to_dict") else {},
        )

    def cancel_order(self, order_id: str) -> OrderResult:
        self._require_connected()

        ret, data = self._trd_ctx.modify_order(
            modify_order_op=ft.ModifyOrderOp.CANCEL,
            order_id=order_id,
            qty=0,
            price=0,
            trd_env=self.trd_env,
            acc_id=self._acc_id,
        )

        if ret != ft.RET_OK:
            logger.error("cancel_order failed for %s: %s", order_id, data)
            return OrderResult(
                success=False, order_id=order_id,
                status=OrderStatus.FAILED, message=str(data),
            )

        logger.info("Order cancelled: %s", order_id)
        return OrderResult(
            success=True, order_id=order_id,
            status=OrderStatus.CANCELLED, message="Cancelled",
        )

    def cancel_all_orders(self) -> list[OrderResult]:
        self._require_connected()

        ret, data = self._trd_ctx.cancel_all_order(
            trd_env=self.trd_env,
            acc_id=self._acc_id,
            trdmarket=ft.TrdMarket.AU,
        )

        if ret != ft.RET_OK:
            logger.error("cancel_all_orders failed: %s", data)
            return [OrderResult(success=False, status=OrderStatus.FAILED,
                                message=str(data))]

        logger.warning("ALL ORDERS CANCELLED")
        return [OrderResult(success=True, status=OrderStatus.CANCELLED,
                            message="All orders cancelled")]

    def get_open_orders(self) -> list[OrderResult]:
        self._require_connected()

        ret, data = self._trd_ctx.order_list_query(
            trd_env=self.trd_env,
            acc_id=self._acc_id,
            refresh_cache=True,
        )
        if ret != ft.RET_OK:
            logger.error("order_list_query failed: %s", data)
            return []

        orders = []
        for _, row in data.iterrows():
            ticker = mapper.to_atlas(row.get("code", ""))
            status = _map_order_status(row.get("order_status"))
            side_str = row.get("trd_side", "BUY")
            side = OrderSide.BUY if "BUY" in str(side_str).upper() else OrderSide.SELL

            orders.append(OrderResult(
                success=True,
                order_id=str(row.get("order_id", "")),
                ticker=ticker,
                side=side,
                status=status,
                requested_qty=int(row.get("qty", 0)),
                filled_qty=int(row.get("dealt_qty", 0)),
                requested_price=float(row.get("price", 0)),
                fill_price=float(row.get("dealt_avg_price", 0)),
                raw=row.to_dict() if hasattr(row, "to_dict") else {},
            ))
        return orders

    def get_order_status(self, order_id: str) -> OrderResult:
        # Query today's orders and filter
        orders = self.get_open_orders()
        for o in orders:
            if o.order_id == order_id:
                return o
        return OrderResult(
            success=False, order_id=order_id,
            status=OrderStatus.UNKNOWN, message="Order not found in today's orders",
        )

    # ── Market Data ────────────────────────────────────────────

    def get_prices(self, tickers: list[str]) -> dict[str, float]:
        """Get real-time ASX prices via Moomoo snapshot.

        Args:
            tickers: List of .AX format tickers.

        Returns:
            Dict of .AX ticker -> latest price.
        """
        if not self._quote_ctx:
            return {}

        moomoo_codes = mapper.to_moomoo_list(tickers)

        # Moomoo supports up to 400 per request
        prices = {}
        batch_size = 400
        for i in range(0, len(moomoo_codes), batch_size):
            batch = moomoo_codes[i:i + batch_size]
            ret, data = self._quote_ctx.get_market_snapshot(batch)
            if ret != ft.RET_OK:
                logger.error("get_market_snapshot failed: %s", data)
                continue

            for _, row in data.iterrows():
                moomoo_code = row.get("code", "")
                atlas_ticker = mapper.to_atlas(moomoo_code)
                last_price = float(row.get("last_price", 0))
                if last_price > 0:
                    prices[atlas_ticker] = last_price

        return prices

    # ── Deals / History ────────────────────────────────────────

    def get_today_deals(self) -> list[DealInfo]:
        """Get today's executed fills."""
        self._require_connected()

        ret, data = self._trd_ctx.deal_list_query(
            trd_env=self.trd_env,
            acc_id=self._acc_id,
            refresh_cache=True,
        )
        if ret != ft.RET_OK:
            logger.error("deal_list_query failed: %s", data)
            return []

        deals = []
        for _, row in data.iterrows():
            ticker = mapper.to_atlas(row.get("code", ""))
            side_str = row.get("trd_side", "BUY")
            side = OrderSide.BUY if "BUY" in str(side_str).upper() else OrderSide.SELL

            deals.append(DealInfo(
                order_id=str(row.get("order_id", "")),
                ticker=ticker,
                side=side,
                qty=int(row.get("qty", 0)),
                price=float(row.get("price", 0)),
                deal_time=str(row.get("create_time", "")),
                raw=row.to_dict() if hasattr(row, "to_dict") else {},
            ))
        return deals

    # ── Internal ───────────────────────────────────────────────

    def _require_connected(self):
        if not self._connected:
            raise RuntimeError("MomooBroker not connected. Call connect() first.")
