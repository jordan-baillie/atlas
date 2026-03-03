"""Interactive Brokers broker implementation for Atlas — ib_insync (socket API).

Connects to IB Gateway / TWS via the native TWS API protocol (port 4001 live,
4002 demo). Uses ib_insync for async-to-sync bridging.

Prerequisites:
    - IB Gateway running (Docker: ghcr.io/gnzsnz/ib-gateway)
    - pip install ib_insync

Key differences from the REST approach:
    - Socket-based, not HTTP — no session cookies, no SSL cert issues
    - ib_insync handles reconnection, heartbeat, and message parsing
    - Contract resolution via qualifyContracts() — no manual conid lookup
    - Market data via reqMktData() — real-time streaming available
"""

from __future__ import annotations

import logging
import time
from typing import Any, Optional

from brokers.base import (
    AccountInfo,
    BrokerAdapter,
    DealInfo,
    OrderFeeInfo,
    OrderResult,
    OrderSide,
    OrderStatus,
    OrderType,
    PositionInfo,
    SlippageReport,
)
from brokers.ibkr import mapper

logger = logging.getLogger("atlas.broker.ibkr")


def _map_order_status(status_str: str) -> OrderStatus:
    """Map IB order status string to Atlas OrderStatus."""
    s = (status_str or "").lower().strip()
    mapping = {
        "presubmitted": OrderStatus.SUBMITTED,
        "submitted": OrderStatus.SUBMITTED,
        "filled": OrderStatus.FILLED,
        "cancelled": OrderStatus.CANCELLED,
        "inactive": OrderStatus.FAILED,
        "pendingsubmit": OrderStatus.PENDING,
        "pendingcancel": OrderStatus.PENDING,
        "apicancelled": OrderStatus.CANCELLED,
    }
    return mapping.get(s, OrderStatus.UNKNOWN)


class IBKRBroker(BrokerAdapter):
    """Live trading via IB Gateway socket API (ib_insync)."""

    def __init__(self, config: dict, live: bool = False):
        super().__init__(config)
        self._live = live
        self._ib = None  # ib_insync.IB instance

        ibkr_cfg = config.get("ibkr", {})
        self._host = ibkr_cfg.get("host", "127.0.0.1")
        # Port: 4001=live, 4002=demo (IB Gateway standard)
        self._port = ibkr_cfg.get("port", 4001 if live else 4002)
        self._account_id: str = ibkr_cfg.get("account_id", "")
        self._currency: str = ibkr_cfg.get("currency", "AUD")
        self._client_id: int = ibkr_cfg.get("client_id", 10)
        self._contract_cache: dict = {}  # Audit H6: cache qualified contracts to avoid repeated API calls

    @property
    def name(self) -> str:
        env = "LIVE" if self._live else "PAPER"
        return f"IBKRBroker[{env}]"

    @property
    def is_live(self) -> bool:
        return self._live

    @property
    def market_id(self) -> str:
        return {"AUD": "asx", "USD": "sp500", "HKD": "hk"}.get(
            self._currency, "asx"
        )

    # ── Lifecycle ──────────────────────────────────────────────

    def connect(self) -> bool:
        """Connect to IB Gateway via socket."""
        try:
            # ── Pre-flight checks (fast-fail before slow ib_insync connect) ──

            # 1. TCP probe — is the gateway port even accepting connections?
            import socket as _sock
            try:
                probe = _sock.create_connection(
                    (self._host, self._port), timeout=3,
                )
                probe.close()
            except (ConnectionRefusedError, TimeoutError, OSError) as e:
                logger.warning(
                    "IBKRBroker pre-flight: gateway not reachable at %s:%d (%s)",
                    self._host, self._port, e,
                )
                return False

            # 2. Docker health — skip connect if container is genuinely unhealthy.
            #    Only trust the check when exit code is 0 (health check itself works).
            #    Many gateway images have broken health checks (missing nc/curl).
            try:
                import subprocess
                result = subprocess.run(
                    ["docker", "inspect", "atlas-ibgateway",
                     "--format={{.State.Health.Status}}"],
                    capture_output=True, text=True, timeout=5,
                )
                health = result.stdout.strip()
                if health == "unhealthy" and result.returncode == 0:
                    # Double-check: is the health check itself failing (broken probe)?
                    # If the last health log shows exec/OCI errors, ignore the status.
                    log_result = subprocess.run(
                        ["docker", "inspect", "atlas-ibgateway",
                         "--format={{(index .State.Health.Log 0).Output}}"],
                        capture_output=True, text=True, timeout=5,
                    )
                    log_out = log_result.stdout.strip()
                    if "exec failed" in log_out or "not found" in log_out:
                        logger.debug(
                            "IBKRBroker pre-flight: ignoring 'unhealthy' — "
                            "Docker health check is broken (missing probe tool)"
                        )
                    else:
                        logger.warning(
                            "IBKRBroker pre-flight: gateway container is unhealthy — skipping connect"
                        )
                        return False
            except Exception:
                pass  # Docker not available or check failed — proceed anyway

            # ── ib_insync connect ──────────────────────────────────

            # ib_insync needs an asyncio event loop — create one if
            # running inside a thread pool (e.g. Telegram bot callback)
            import asyncio
            try:
                asyncio.get_event_loop()
            except RuntimeError:
                asyncio.set_event_loop(asyncio.new_event_loop())

            from ib_insync import IB
            self._ib = IB()
            self._ib.connect(
                self._host, self._port,
                clientId=self._client_id,
                timeout=20,
            )

            # Discover account
            if not self._account_id:
                accounts = self._ib.managedAccounts()
                if accounts:
                    self._account_id = accounts[0]
                    logger.info("Auto-selected account: %s", self._account_id)

            if not self._account_id:
                logger.error("No account ID found")
                return False

            self._connected = True
            logger.info(
                "IBKRBroker connected: %s:%d account=%s live=%s",
                self._host, self._port, self._account_id, self._live,
            )
            return True

        except Exception as e:
            logger.error("IBKRBroker connect failed: %s", e, exc_info=True)
            return False

    def disconnect(self):
        """Disconnect from IB Gateway."""
        if self._ib:
            try:
                self._ib.disconnect()
            except Exception:
                pass
            self._ib = None
        self._connected = False
        logger.info("IBKRBroker disconnected")

    def keepalive(self) -> bool:
        """Check connection is still alive."""
        if not self._ib:
            return False
        return self._ib.isConnected()

    # ── Account ────────────────────────────────────────────────

    def get_account_info(self) -> AccountInfo:
        self._require_connected()

        summary = self._ib.accountSummary(self._account_id)

        vals = {}
        for item in summary:
            if item.currency == self._currency or item.currency == "":
                try:
                    vals[item.tag] = float(item.value) if item.value else 0.0
                except (ValueError, TypeError):
                    pass  # skip non-numeric fields like AccountType

        equity = vals.get("NetLiquidation", 0)
        cash = vals.get("TotalCashValue", vals.get("AvailableFunds", 0))
        market_val = vals.get("GrossPositionValue", 0)
        buying_power = vals.get("BuyingPower", 0)

        starting = self.config.get("risk", {}).get("starting_equity", 3999)
        pnl = round(equity - starting, 2)
        pnl_pct = round(pnl / starting * 100, 2) if starting > 0 else 0

        return AccountInfo(
            equity=round(equity, 2),
            cash=round(cash, 2),
            market_value=round(market_val, 2),
            buying_power=round(buying_power, 2),
            total_pnl=pnl,
            total_pnl_pct=pnl_pct,
            currency=self._currency,
            market_id=self.market_id,
        )

    def get_positions(self) -> list[PositionInfo]:
        self._require_connected()

        # Use portfolio() instead of positions() — it includes marketPrice
        # and unrealizedPNL from IBKR's own valuation, without requiring
        # reqMktData snapshots (which fail without a market data subscription
        # and spam Error 354 to logs and Telegram).
        portfolio_items = self._ib.portfolio(self._account_id)
        positions = []

        # Build a lookup from positions() for avgCost (portfolio uses
        # averageCost which is the same but let's be safe)
        for item in portfolio_items:
            qty = int(item.position)
            if qty == 0:
                continue

            contract = item.contract
            symbol = contract.symbol
            exchange = contract.exchange or contract.primaryExchange or ""
            ticker = mapper.to_atlas(symbol, exchange)

            avg_cost = float(item.averageCost)
            mkt_price = float(item.marketPrice)
            mkt_value = float(item.marketValue)
            upnl = float(item.unrealizedPNL)

            # Fallback if marketPrice is 0 or stale
            if mkt_price <= 0:
                mkt_price = avg_cost
                mkt_value = mkt_price * abs(qty)
                upnl = 0

            upnl_pct = round((mkt_price - avg_cost) / avg_cost * 100, 2) if avg_cost > 0 else 0

            positions.append(PositionInfo(
                ticker=ticker,
                entry_price=round(avg_cost, 4),
                shares=abs(qty),
                current_price=round(mkt_price, 4),
                market_value=round(abs(mkt_value), 2),
                unrealized_pnl=round(upnl, 2),
                unrealized_pnl_pct=upnl_pct,
                cost_basis=round(avg_cost * abs(qty), 2),
            ))

        return positions

    # ── Contract resolution ────────────────────────────────────

    def _make_contract(self, ticker: str):
        """Create an IB Contract from Atlas ticker.

        Always uses SMART routing to avoid IBKR error 10311
        (direct-routed order precautionary rejection). SMART routing
        still routes to the correct exchange (e.g. ASX for .AX tickers).
        """
        from ib_insync import Stock

        symbol = mapper.strip_suffix(ticker, self.market_id)
        exchange = mapper.get_exchange(self.market_id)
        currency = mapper.get_currency(self.market_id)

        # SMART routing with primaryExchange hint
        return Stock(symbol, "SMART", currency, primaryExchange=exchange)

    def _qualify_contract(self, ticker: str):
        """Resolve and qualify a contract with IBKR.

        # Audit H6: check cache first to avoid repeated qualifyContracts() API calls.
        """
        if ticker in self._contract_cache:
            return self._contract_cache[ticker]

        contract = self._make_contract(ticker)
        qualified = self._ib.qualifyContracts(contract)
        if qualified:
            self._contract_cache[ticker] = qualified[0]
            return qualified[0]
        logger.warning("Could not qualify contract for %s", ticker)
        return contract

    def _get_last_price(self, contract) -> Optional[float]:
        """Get last/close price for a contract."""
        # Request a snapshot
        ticker_data = self._ib.reqMktData(contract, "", True, False)
        self._ib.sleep(0.5)  # Audit H6: reduced from 2s to 0.5s for snapshot requests
        self._ib.cancelMktData(contract)

        price = ticker_data.last
        if price and price > 0 and price != float("inf"):
            return float(price)

        price = ticker_data.close
        if price and price > 0 and price != float("inf"):
            return float(price)

        return None

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
        from ib_insync import LimitOrder, MarketOrder, StopOrder, Order

        contract = self._qualify_contract(ticker)

        # Safety check
        if self._live:
            safety = self.config.get("trading", {}).get("live_safety", {})
            max_value = safety.get("max_order_value", 2000)
            order_value = price * qty
            if order_value > max_value:
                return OrderResult(
                    success=False, ticker=ticker, side=side,
                    status=OrderStatus.FAILED,
                    message=f"Order value ${order_value:.2f} exceeds max ${max_value}",
                )

        action = "BUY" if side == OrderSide.BUY else "SELL"

        if order_type == OrderType.MARKET:
            ib_order = MarketOrder(action, qty)
        elif order_type == OrderType.STOP:
            ib_order = StopOrder(action, qty, round(stop_price or price, 2))
        elif order_type == OrderType.TRAILING_STOP:
            trail_amount = kwargs.get("trail_value", 0)
            ib_order = Order(
                action=action,
                totalQuantity=qty,
                orderType="TRAIL",
                auxPrice=round(trail_amount, 2) if trail_amount else 0,
            )
        else:  # LIMIT (default)
            ib_order = LimitOrder(action, qty, round(price, 2))

        ib_order.tif = self.config.get("ibkr", {}).get("time_in_force", "DAY")
        # Bypass precautionary settings (error 10311 etc.) — we do our
        # own safety checks in LiveExecutor preflight
        ib_order.overridePercentageConstraints = True
        if remark:
            ib_order.orderRef = remark[:32]

        logger.info(
            "Placing order: %s %s %d x %.4f (%s) [%s]",
            action, ticker, qty, price, order_type.value, remark,
        )

        try:
            trade = self._ib.placeOrder(contract, ib_order)
            self._ib.sleep(2)  # wait for order acknowledgement

            order_id = str(trade.order.orderId)
            status = trade.orderStatus.status if trade.orderStatus else ""

            logger.info(
                "Order placed: %s %s → order_id=%s status=%s",
                action, ticker, order_id, status,
            )

            mapped_status = _map_order_status(status) if status else OrderStatus.SUBMITTED
            success = mapped_status not in (OrderStatus.CANCELLED, OrderStatus.FAILED)

            return OrderResult(
                success=success,
                order_id=order_id,
                ticker=ticker,
                side=side,
                status=mapped_status,
                requested_qty=qty,
                requested_price=price,
                fill_price=trade.orderStatus.avgFillPrice if trade.orderStatus else 0,
                filled_qty=int(trade.orderStatus.filled) if trade.orderStatus else 0,
                message=status or "Order submitted",
            )

        except Exception as e:
            logger.error("Order failed for %s: %s", ticker, e, exc_info=True)
            return OrderResult(
                success=False, ticker=ticker, side=side,
                status=OrderStatus.FAILED, requested_qty=qty,
                requested_price=price, message=str(e)[:200],
            )

    def cancel_order(self, order_id: str) -> OrderResult:
        self._require_connected()

        # Find the trade by order ID
        for trade in self._ib.openTrades():
            if str(trade.order.orderId) == order_id:
                self._ib.cancelOrder(trade.order)
                self._ib.sleep(2)
                logger.info("Order cancelled: %s", order_id)
                return OrderResult(
                    success=True, order_id=order_id,
                    status=OrderStatus.CANCELLED, message="Cancelled",
                )

        logger.warning("Order %s not found in open trades", order_id)
        return OrderResult(
            success=False, order_id=order_id,
            status=OrderStatus.FAILED, message="Order not found",
        )

    def cancel_all_orders(self) -> list[OrderResult]:
        self._require_connected()
        self._ib.reqGlobalCancel()
        self._ib.sleep(2)
        logger.warning("Global cancel requested")
        return [OrderResult(
            success=True, status=OrderStatus.CANCELLED,
            message="Global cancel sent",
        )]

    def get_open_orders(self) -> list[OrderResult]:
        self._require_connected()
        results = []

        for trade in self._ib.openTrades():
            o = trade.order
            s = trade.orderStatus
            contract = trade.contract

            side = OrderSide.BUY if o.action == "BUY" else OrderSide.SELL
            ticker = mapper.to_atlas(
                contract.symbol,
                contract.exchange or contract.primaryExchange or "",
            )

            results.append(OrderResult(
                success=True,
                order_id=str(o.orderId),
                ticker=ticker,
                side=side,
                status=_map_order_status(s.status) if s else OrderStatus.UNKNOWN,
                requested_qty=int(o.totalQuantity),
                filled_qty=int(s.filled) if s else 0,
                requested_price=float(o.lmtPrice) if o.lmtPrice else 0,
                fill_price=float(s.avgFillPrice) if s else 0,
                message=s.status if s else "",
            ))

        return results

    def get_order_status(self, order_id: str) -> OrderResult:
        """Get status of a specific order."""
        for trade in self._ib.trades():
            if str(trade.order.orderId) == order_id:
                s = trade.orderStatus
                o = trade.order
                side = OrderSide.BUY if o.action == "BUY" else OrderSide.SELL
                return OrderResult(
                    success=True,
                    order_id=order_id,
                    ticker=trade.contract.symbol,
                    side=side,
                    status=_map_order_status(s.status) if s else OrderStatus.UNKNOWN,
                    requested_qty=int(o.totalQuantity),
                    filled_qty=int(s.filled) if s else 0,
                    requested_price=float(o.lmtPrice) if o.lmtPrice else 0,
                    fill_price=float(s.avgFillPrice) if s else 0,
                )

        return OrderResult(
            success=False, order_id=order_id,
            status=OrderStatus.UNKNOWN, message="Order not found",
        )

    # ── Market Data ────────────────────────────────────────────

    def get_prices(self, tickers: list[str]) -> dict[str, float]:
        """Get latest prices for a list of tickers."""
        if not self._connected:
            return {}

        prices = {}
        for ticker in tickers:
            try:
                contract = self._qualify_contract(ticker)
                price = self._get_last_price(contract)
                if price and price > 0:
                    prices[ticker] = round(price, 4)
            except Exception as e:
                logger.debug("Price fetch failed for %s: %s", ticker, e)

        return prices

    # ── History ────────────────────────────────────────────────

    def get_history_orders(self, days: int = 30) -> list[OrderResult]:
        """Get recent orders."""
        if not self._connected:
            return []
        results = []
        for trade in self._ib.trades():
            o = trade.order
            s = trade.orderStatus
            side = OrderSide.BUY if o.action == "BUY" else OrderSide.SELL
            results.append(OrderResult(
                success=s.status == "Filled" if s else False,
                order_id=str(o.orderId),
                ticker=trade.contract.symbol,
                side=side,
                status=_map_order_status(s.status) if s else OrderStatus.UNKNOWN,
                requested_qty=int(o.totalQuantity),
                filled_qty=int(s.filled) if s else 0,
                requested_price=float(o.lmtPrice) if o.lmtPrice else 0,
                fill_price=float(s.avgFillPrice) if s else 0,
            ))
        return results

    def get_history_deals(self, days: int = 30) -> list[DealInfo]:
        """Get recent fills via ib_insync fills().

        # Audit H5: ib_insync fills() returns all execution fills from the
        # current IB Gateway session. Convert each Fill to a DealInfo.
        """
        if not self._connected or not self._ib:
            return []
        try:
            fills = self._ib.fills()
            deals = []
            for fill in fills:
                contract = fill.contract
                execution = fill.execution
                symbol = contract.symbol
                exchange = contract.exchange or contract.primaryExchange or ""
                ticker = mapper.to_atlas(symbol, exchange)
                # IB side: "BOT" = bought, "SLD" = sold
                side = OrderSide.BUY if execution.side.upper() == "BOT" else OrderSide.SELL
                deals.append(DealInfo(
                    order_id=str(execution.orderId),
                    ticker=ticker,
                    side=side,
                    qty=int(execution.shares),
                    price=float(execution.price),
                    deal_time=str(execution.time),
                    raw={"execId": execution.execId},
                ))
            return deals
        except Exception as e:
            logger.warning("get_history_deals failed: %s", e)
            return []

    def get_today_deals(self) -> list[DealInfo]:
        """Get today's executed fills (session fills from IB Gateway).

        # Audit C4: ib_insync fills() is session-based so this returns
        # fills since the gateway last connected — close enough to "today".
        """
        return self.get_history_deals()

    # ── Protective orders ──────────────────────────────────────

    def place_protective_orders(
        self,
        ticker: str,
        qty: int,
        stop_price: float,
        take_profit: Optional[float] = None,
    ) -> dict:
        """Place SL + TP protective orders for an existing position.

        Places a stop-loss and optional take-profit as an OCA (One-Cancels-All)
        group. When SL fills, TP is automatically cancelled (and vice versa).
        When take_profit is None, only a GTC stop-loss is placed — common for
        trailing-stop strategies that manage exits outside the broker.

        Args:
            ticker:      Atlas ticker (e.g. 'BHP.AX', 'AAPL').
            qty:         Number of shares to protect.
            stop_price:  Stop-loss trigger price.
            take_profit: Take-profit limit price (None = SL-only).

        Returns:
            dict with keys: success, sl_order_id, tp_order_id, oca_group, message.
        """
        self._require_connected()
        from brokers.ibkr.protective_orders import place_protective_orders as _place

        contract = self._qualify_contract(ticker)
        result = _place(
            self._ib, contract, qty,
            stop_price=stop_price,
            take_profit_price=take_profit,
            account_id=self._account_id,
        )

        logger.info(
            "place_protective_orders %s qty=%d sl=%.4f tp=%s → success=%s SL=%s TP=%s",
            ticker, qty, stop_price,
            f"{take_profit:.4f}" if take_profit else "none",
            result["success"],
            result.get("sl_order_id"),
            result.get("tp_order_id"),
        )
        return result

    def sync_all_protective_orders(self, plan_entries: list[dict]) -> dict:
        """Ensure all current long positions have broker-side protective orders.

        Fetches live positions from IBKR via portfolio(), then for each long
        position checks if an Atlas SL order exists. Positions missing a
        stop-loss get protective orders placed (SL + TP if plan provides a target).

        This is safe to call multiple times — positions already protected are
        skipped. Useful as a daily reconciliation step to recover from missed
        stop placements after server restarts.

        Args:
            plan_entries: List of plan entry dicts (from TradePlanGenerator).
                          Expected keys: ticker, stop_price, optionally
                          take_profit or take_profit_price.

        Returns:
            sync summary dict — see protective_orders.sync_protective_orders().
        """
        self._require_connected()
        from brokers.ibkr import protective_orders as po

        # Build plan lookup by ticker for stop/tp data
        plan_by_ticker: dict = {}
        for entry in (plan_entries or []):
            t = entry.get("ticker", "")
            if t:
                plan_by_ticker[t] = entry

        # Fetch live portfolio to find long positions
        portfolio_items = self._ib.portfolio(self._account_id)
        positions_for_sync: list[dict] = []

        for item in portfolio_items:
            qty = int(item.position)
            if qty <= 0:
                continue  # skip shorts and flat positions

            contract = item.contract
            symbol = contract.symbol
            exchange = contract.exchange or contract.primaryExchange or ""
            ticker = mapper.to_atlas(symbol, exchange)

            plan = plan_by_ticker.get(ticker, {})
            stop_price = plan.get("stop_price", 0)
            raw_tp = plan.get("take_profit_price") or plan.get("take_profit", 0)
            tp_price = float(raw_tp) if raw_tp else None

            positions_for_sync.append({
                "contract": contract,
                "qty": qty,
                "ticker": ticker,
                "stop_price": stop_price,
                "take_profit_price": tp_price,
            })

        if not positions_for_sync:
            logger.info("sync_all_protective_orders: no long positions found")
            return {
                "positions_checked": 0,
                "already_protected": 0,
                "orders_placed": 0,
                "no_stop_price": 0,
                "failed": 0,
                "details": [],
            }

        logger.info(
            "sync_all_protective_orders: %d long position(s), %d plan entries",
            len(positions_for_sync), len(plan_entries or []),
        )

        return po.sync_protective_orders(
            self._ib,
            positions_for_sync,
            plan_entries=plan_entries,
            account_id=self._account_id,
        )

    def get_protective_order_status(self, ticker: str) -> dict:
        """Return the current SL/TP order status for a ticker.

        Queries open trades for Atlas-managed stop-loss and take-profit orders
        on this contract. Useful for reconciliation and dashboard display.

        Args:
            ticker: Atlas ticker string (e.g. 'BHP.AX').

        Returns:
            dict with keys:
                ticker:      str
                protected:   bool — True if at least one SL order exists
                has_sl:      bool
                has_tp:      bool
                sl_orders:   list[dict] — each with order_id, stop_price, status, etc.
                tp_orders:   list[dict] — each with order_id, limit_price, status, etc.
                oca_groups:  list[str]
        """
        self._require_connected()
        from brokers.ibkr.protective_orders import get_existing_protective_orders

        contract = self._qualify_contract(ticker)
        existing = get_existing_protective_orders(
            self._ib, contract, self._account_id,
        )

        def _trade_to_dict(trade) -> dict:
            o = trade.order
            s = trade.orderStatus
            return {
                "order_id": str(o.orderId),
                "order_type": o.orderType or "",
                "action": o.action or "",
                "qty": int(o.totalQuantity),
                "stop_price": float(o.auxPrice or 0),
                "limit_price": float(o.lmtPrice or 0),
                "status": s.status if s else "unknown",
                "oca_group": o.ocaGroup or "",
                "order_ref": o.orderRef or "",
                "tif": o.tif or "",
            }

        return {
            "ticker": ticker,
            "protected": existing["protected"],
            "has_sl": existing["has_sl"],
            "has_tp": existing["has_tp"],
            "sl_orders": [_trade_to_dict(t) for t in existing["sl_orders"]],
            "tp_orders": [_trade_to_dict(t) for t in existing["tp_orders"]],
            "oca_groups": list(existing["oca_groups"]),
        }

    # ── Internal ───────────────────────────────────────────────

    def _require_connected(self):
        if not self._connected or not self._ib or not self._ib.isConnected():
            raise RuntimeError(
                "IBKRBroker not connected. Call connect() first."
            )
