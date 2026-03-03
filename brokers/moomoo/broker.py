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
    OrderStatus, OrderSide, OrderType, OrderFeeInfo, MarketStateInfo,
    TradingDayInfo, SlippageReport,
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
        self._trd_market = moomoo_cfg.get("trd_market", "AU")
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
    def market_id(self) -> str:
        """Atlas market_id derived from moomoo trd_market config."""
        return {"AU": "asx", "US": "sp500", "HK": "hk"}.get(self._trd_market, "asx")

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

            # Trade context — must use FUTUAU firm to see AU real account.
            # Filter by HK market for queries (AU market filter returns
            # "environment param wrong" for real accounts server-side).
            self._trd_ctx = ft.OpenSecTradeContext(
                filter_trdmarket=ft.TrdMarket.HK,
                host=self._host,
                port=self._port,
                security_firm=sec_firm,
            )

            # Quote context — for real-time prices (HK works, AU unsupported)
            self._quote_ctx = ft.OpenQuoteContext(
                host=self._host,
                port=self._port,
            )

            # Discover account
            ret, data = self._trd_ctx.get_acc_list()
            if ret != ft.RET_OK:
                logger.error("Failed to get account list: %s", data)
                return False

            # Find account: prefer REAL with AU market auth, fall back to env match
            for _, row in data.iterrows():
                trd_env = str(row.get("trd_env", ""))
                market_auth = str(row.get("trdmarket_auth", ""))
                if self._live and trd_env == "REAL" and "AU" in market_auth:
                    self._acc_id = int(row["acc_id"])
                    logger.info("Using REAL AU account %s (firm=%s, markets=%s)",
                                self._acc_id, sec_firm, market_auth)
                    break
                elif not self._live and trd_env == "SIMULATE":
                    self._acc_id = int(row["acc_id"])
                    logger.info("Using SIMULATE account %s (firm=%s)",
                                self._acc_id, sec_firm)
                    break

            if self._acc_id == 0:
                # Fall back to configured acc_id from secrets
                configured_id = self.config.get("moomoo", {}).get("acc_id", 0)
                if configured_id:
                    self._acc_id = int(configured_id)
                    logger.info("Using configured account %s", self._acc_id)
                else:
                    logger.warning("No matching account found")

            # Unlock trade (required for placing orders on real accounts)
            if self._live:
                pwd = get_secret(self._pwd_env, prompt=False)
                if pwd:
                    ret, data = self._trd_ctx.unlock_trade(password=pwd)
                    if ret != ft.RET_OK:
                        if self._live:
                            # Audit H10: trade unlock failure is fatal for live accounts.
                            # Without unlock, orders will be silently rejected by the broker.
                            logger.error(
                                "Trade unlock FAILED for live account — cannot place orders safely. "
                                "Check MOOMOO_TRADE_PWD secret. Error: %s", data
                            )
                            return False
                        # For simulated accounts, unlock failure is non-fatal
                        logger.warning("Trade unlock result (non-fatal for simulate): %s", data)
                    else:
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
            upnl_pct = float(row.get("pl_ratio", 0))  # Already in %

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

        moomoo_code = mapper.to_moomoo(ticker, self.market_id)
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

        # Round price to 2 decimals — Moomoo rejects excess precision
        price = round(price, 2)

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

        # Add stop/trigger price for stop orders
        if stop_price and order_type in (OrderType.STOP, OrderType.STOP_LIMIT):
            kwargs_order["aux_price"] = stop_price

        # Trailing stop parameters
        if order_type in (OrderType.TRAILING_STOP,):
            trail_type = kwargs.get("trail_type")
            trail_value = kwargs.get("trail_value")
            trail_spread = kwargs.get("trail_spread")
            if trail_type:
                kwargs_order["trail_type"] = trail_type
            if trail_value is not None:
                kwargs_order["trail_value"] = trail_value
            if trail_spread is not None:
                kwargs_order["trail_spread"] = trail_spread

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

            # Skip fully filled, cancelled, or failed orders — they're not "open"
            raw_status = str(row.get("order_status", "")).upper()
            if raw_status in ("FILLED_ALL", "CANCELLED_ALL", "CANCELLED_PART",
                              "FAILED", "DELETED", "DISABLED"):
                continue

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

    def get_all_today_orders(self) -> list[OrderResult]:
        """Return ALL orders from today including filled/cancelled (for status checks)."""
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
        # Query all today's orders (including filled) and filter
        orders = self.get_all_today_orders()
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

        moomoo_codes = mapper.to_moomoo_list(tickers, self.market_id)

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

    # ── History & Analytics ───────────────────────────────────

    def get_history_orders(self, days: int = 30) -> list[OrderResult]:
        """Get historical orders for the past N days."""
        self._require_connected()
        from datetime import datetime, timedelta

        start = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
        end = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        ret, data = self._trd_ctx.history_order_list_query(
            trd_env=self.trd_env,
            acc_id=self._acc_id,
            start=start, end=end,
        )
        if ret != ft.RET_OK:
            logger.error("history_order_list_query failed: %s", data)
            return []

        orders = []
        for _, row in data.iterrows():
            ticker = mapper.to_atlas(row.get("code", ""))
            status = _map_order_status(row.get("order_status"))
            side_str = row.get("trd_side", "BUY")
            side = OrderSide.BUY if "BUY" in str(side_str).upper() else OrderSide.SELL

            orders.append(OrderResult(
                success=status in (OrderStatus.FILLED, OrderStatus.SUBMITTED),
                order_id=str(row.get("order_id", "")),
                ticker=ticker,
                side=side,
                status=status,
                requested_qty=int(row.get("qty", 0)),
                filled_qty=int(row.get("dealt_qty", 0)),
                requested_price=float(row.get("price", 0)),
                fill_price=float(row.get("dealt_avg_price", 0)),
                message=str(row.get("last_err_msg", "")),
                raw=row.to_dict() if hasattr(row, "to_dict") else {},
            ))
        return orders

    def get_history_deals(self, days: int = 30) -> list[DealInfo]:
        """Get historical deal fills for the past N days."""
        self._require_connected()
        from datetime import datetime, timedelta

        start = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
        end = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        ret, data = self._trd_ctx.history_deal_list_query(
            trd_env=self.trd_env,
            acc_id=self._acc_id,
            start=start, end=end,
        )
        if ret != ft.RET_OK:
            logger.error("history_deal_list_query failed: %s", data)
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

    def get_order_fees(self, order_ids: list[str]) -> list[OrderFeeInfo]:
        """Get fee breakdown for specific orders."""
        self._require_connected()

        if not order_ids:
            return []

        ret, data = self._trd_ctx.order_fee_query(
            order_id_list=order_ids,
        )
        if ret != ft.RET_OK:
            logger.error("order_fee_query failed: %s", data)
            return []

        fees = []
        for _, row in data.iterrows():
            fee_details = row.get("fee_details", [])
            # Parse fee_details — comes as list of (name, amount) tuples
            parsed = []
            if isinstance(fee_details, list):
                for item in fee_details:
                    if isinstance(item, (list, tuple)) and len(item) >= 2:
                        parsed.append((str(item[0]), float(item[1])))

            fees.append(OrderFeeInfo(
                order_id=str(row.get("order_id", "")),
                total_fee=float(row.get("fee_amount", 0)),
                fee_details=parsed,
                raw=row.to_dict() if hasattr(row, "to_dict") else {},
            ))
        return fees

    def get_market_states(self, tickers: list[str]) -> list[MarketStateInfo]:
        """Get current market state for tickers.

        Accepts tickers in Atlas format (.AX) or Moomoo format (US./HK.).
        AU quotes are unsupported — uses global state to infer.
        """
        if not self._quote_ctx:
            return []

        # Classify tickers
        au_tickers = [t for t in tickers if t.endswith(".AX") or t.startswith("AU.")]
        us_tickers = [t for t in tickers if t.startswith("US.")]
        hk_tickers = [t for t in tickers if t.startswith("HK.")]
        query_tickers = us_tickers + hk_tickers  # These support get_market_state

        states = []

        # Query US/HK market state directly (already in moomoo format)
        if query_tickers:
            ret, data = self._quote_ctx.get_market_state(query_tickers)
            if ret == ft.RET_OK:
                for _, row in data.iterrows():
                    code = row.get("code", "")
                    states.append(MarketStateInfo(
                        ticker=code,
                        market_state=str(row.get("market_state", "UNKNOWN")),
                        raw=row.to_dict() if hasattr(row, "to_dict") else {},
                    ))
            else:
                logger.debug("get_market_state failed for %s: %s", query_tickers, data)

        # For AU tickers, infer from global state
        if au_tickers:
            ret, gstate = self._quote_ctx.get_global_state()
            if ret == ft.RET_OK:
                for ticker in au_tickers:
                    states.append(MarketStateInfo(
                        ticker=ticker,
                        market_state="AU_UNSUPPORTED",
                        raw=gstate,
                    ))

        return states

    def get_trading_days(self, market: str = "US", days: int = 30) -> list[TradingDayInfo]:
        """Get trading calendar. Supports US and HK markets (not AU)."""
        if not self._quote_ctx:
            return []

        from datetime import datetime, timedelta

        market_map = {
            "US": ft.TradeDateMarket.US,
            "HK": ft.TradeDateMarket.HK,
            "CN": ft.TradeDateMarket.CN,
        }
        mkt = market_map.get(market.upper())
        if not mkt:
            logger.warning("Trading days not available for market: %s", market)
            return []

        start = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
        end = datetime.now().strftime("%Y-%m-%d")

        ret, data = self._quote_ctx.request_trading_days(
            market=mkt, start=start, end=end,
        )
        if ret != ft.RET_OK:
            logger.error("request_trading_days failed: %s", data)
            return []

        result = []
        for item in data:
            if isinstance(item, dict):
                result.append(TradingDayInfo(
                    date=item.get("time", ""),
                    trade_date_type=item.get("trade_date_type", ""),
                ))
            else:
                result.append(TradingDayInfo(date=str(item)))
        return result

    def get_max_trade_qty(self, ticker: str, price: float) -> Optional[int]:
        """Query max buyable quantity for a ticker at given price.

        Note: Only works for US market. AU returns 'not supported'.
        """
        self._require_connected()

        moomoo_code = mapper.to_moomoo(ticker, self.market_id)
        try:
            ret, data = self._trd_ctx.acctradinginfo_query(
                order_type=ft.OrderType.NORMAL,
                code=moomoo_code,
                price=price,
                trd_env=self.trd_env,
                acc_id=self._acc_id,
            )
            if ret == ft.RET_OK and len(data) > 0:
                row = data.iloc[0]
                max_buy = int(row.get("max_cash_buy", 0))
                logger.info("Max buy qty for %s @ %.2f: %d", ticker, price, max_buy)
                return max_buy
            else:
                logger.debug("acctradinginfo_query for %s: %s", ticker, data)
                return None
        except Exception as e:
            logger.debug("acctradinginfo_query failed for %s: %s", ticker, e)
            return None

    def get_slippage_report(self, days: int = 30) -> list[SlippageReport]:
        """Analyse slippage by comparing order prices to actual fills.

        Matches history_order prices (what we requested) against
        history_deal prices (what we actually got).
        """
        orders = self.get_history_orders(days=days)
        deals = self.get_history_deals(days=days)

        if not orders or not deals:
            return []

        # Group deals by order_id
        deal_map: dict[str, list[DealInfo]] = {}
        for d in deals:
            deal_map.setdefault(d.order_id, []).append(d)

        reports = []
        for order in orders:
            if order.status not in (OrderStatus.FILLED, OrderStatus.PARTIAL_FILLED):
                continue
            if not order.order_id or order.order_id not in deal_map:
                continue

            order_deals = deal_map[order.order_id]
            total_qty = sum(d.qty for d in order_deals)
            if total_qty == 0:
                continue

            # Volume-weighted average fill price
            vwap_fill = sum(d.price * d.qty for d in order_deals) / total_qty
            requested = order.requested_price

            if requested <= 0:
                continue

            # Slippage: positive = worse for buyer, negative = better
            if order.side == OrderSide.BUY:
                slip = vwap_fill - requested
            else:
                slip = requested - vwap_fill

            slip_pct = (slip / requested) * 100

            reports.append(SlippageReport(
                order_id=order.order_id,
                ticker=order.ticker,
                side=order.side.value,
                requested_price=requested,
                fill_price=round(vwap_fill, 4),
                slippage_abs=round(slip, 4),
                slippage_pct=round(slip_pct, 4),
                qty=total_qty,
                slippage_cost=round(slip * total_qty, 2),
            ))

        return reports

    # ── Protective orders ──────────────────────────────────────

    def place_protective_orders(
        self,
        ticker: str,
        qty: int,
        stop_price: float,
        take_profit: Optional[float],
        strategy: str = "",
        trade_date: str = "",
        *,
        dry_run: bool = False,
    ):
        """Place SL (stop) + TP (limit) protective orders for a filled position.

        Note: Moomoo does NOT support OCA groups. SL and TP are placed as
        separate independent orders. If one fills, the intraday monitor must
        cancel the other.

        Args:
            ticker:       Atlas-format ticker (e.g. 'BHP.AX').
            qty:          Number of shares to protect.
            stop_price:   Stop-loss trigger price.
            take_profit:  Take-profit limit price (None → skip TP).
            strategy:     Strategy name — used in order remark.
            trade_date:   YYYY-MM-DD string — used in order remark.
            dry_run:      Log intent but do NOT send orders.

        Returns:
            ProtectiveOrderResult with sl_order_id, tp_order_id, and placement flags.
        """
        from brokers.moomoo.protective_orders import place_protective_orders
        self._require_connected()
        return place_protective_orders(
            broker=self,
            ticker=ticker,
            qty=qty,
            stop_price=stop_price,
            take_profit=take_profit,
            strategy=strategy,
            trade_date=trade_date,
            dry_run=dry_run,
            config=self.config,
        )

    def sync_all_protective_orders(
        self,
        positions: list,
        plan: Optional[dict] = None,
        *,
        trade_date: str = "",
        dry_run: bool = False,
    ) -> dict:
        """Sync protective orders for all live positions.

        Idempotent — existing orders are detected and skipped.
        Only missing SL/TP orders are placed.

        Args:
            positions:   List of Position objects from live_portfolio.
            plan:        Today's trade plan dict (for stop/TP lookups).
            trade_date:  YYYY-MM-DD (defaults to today if empty).
            dry_run:     Log intent but do NOT send orders.

        Returns:
            Summary dict with counts and per-ticker results.
        """
        from brokers.moomoo.protective_orders import sync_protective_orders
        self._require_connected()

        # Fetch current open orders once (avoids repeated API calls per position)
        open_orders = self.get_open_orders()

        return sync_protective_orders(
            broker=self,
            positions=positions,
            open_orders=open_orders,
            plan=plan,
            config=self.config,
            trade_date=trade_date,
            dry_run=dry_run,
        )

    def get_protective_order_status(
        self,
        stop_order_id: str,
        tp_order_id: str = "",
    ) -> dict:
        """Query live status of SL and TP orders.

        Args:
            stop_order_id:  SL order ID (empty → skip).
            tp_order_id:    TP order ID (empty → skip).

        Returns:
            Dict with 'sl' and 'tp' keys, each containing status and fill info.
        """
        from brokers.moomoo.protective_orders import get_protective_order_status
        self._require_connected()
        return get_protective_order_status(
            broker=self,
            stop_order_id=stop_order_id,
            tp_order_id=tp_order_id,
        )

    # ── Internal ───────────────────────────────────────────────

    def _require_connected(self):
        if not self._connected:
            raise RuntimeError("MomooBroker not connected. Call connect() first.")
