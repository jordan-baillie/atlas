"""Alpaca broker implementation for Atlas.

Connects to Alpaca Markets via the alpaca-py SDK to execute
real or paper trades on US equities (SP500).

Requirements:
    pip install alpaca-py

Credentials:
    ALPACA_API_KEY and ALPACA_SECRET_KEY loaded from:
        1. Environment variables (preferred)
        2. ~/.atlas-secrets.json

Config keys (under 'alpaca' section):
    paper:  true  → paper-api.alpaca.markets (default)
            false → api.alpaca.markets (live real-money orders)
    feed:   "iex" (free, default) | "sip" (paid, full market data)
    tif:    "day" | "gtc" | "ioc" | "fok" (time in force, default "day")

All tickers at the Atlas boundary use plain US symbols (AAPL, MSFT).
Alpaca uses the same format — conversion is a no-op for SP500.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone, timedelta
from typing import Optional

from brokers.base import (
    BrokerAdapter, OrderResult, PositionInfo, AccountInfo, DealInfo,
    OrderStatus, OrderSide, OrderType,
)
from brokers.alpaca import mapper
from brokers.alpaca.market_data import AlpacaMarketData
from brokers.secrets import get_secret

logger = logging.getLogger("atlas.broker.alpaca")

try:
    from alpaca.trading.client import TradingClient
    from alpaca.trading.requests import (
        MarketOrderRequest,
        LimitOrderRequest,
        StopOrderRequest,
        StopLimitOrderRequest,
        TrailingStopOrderRequest,
        GetOrdersRequest,
    )
    from alpaca.trading.enums import (
        OrderSide as AlpacaSide,
        OrderType as AlpacaOrderType,
        TimeInForce,
        QueryOrderStatus,
        OrderStatus as AlpacaOrderStatus,
    )
    ALPACA_AVAILABLE = True
except ImportError:
    ALPACA_AVAILABLE = False
    logger.warning("alpaca-py not installed. Run: pip install alpaca-py")


# ═══════════════════════════════════════════════════════════════
# Status / enum mapping
# ═══════════════════════════════════════════════════════════════

# Alpaca order status string values → Atlas OrderStatus
_STATUS_MAP = {
    "new":                  OrderStatus.SUBMITTED,
    "partially_filled":     OrderStatus.PARTIAL_FILLED,
    "filled":               OrderStatus.FILLED,
    "done_for_day":         OrderStatus.CANCELLED,
    "canceled":             OrderStatus.CANCELLED,
    "expired":              OrderStatus.CANCELLED,
    "replaced":             OrderStatus.CANCELLED,
    "pending_cancel":       OrderStatus.SUBMITTED,
    "pending_replace":      OrderStatus.SUBMITTED,
    "pending_review":       OrderStatus.PENDING,
    "accepted":             OrderStatus.SUBMITTED,
    "pending_new":          OrderStatus.PENDING,
    "accepted_for_bidding": OrderStatus.PENDING,
    "stopped":              OrderStatus.CANCELLED,
    "rejected":             OrderStatus.FAILED,
    "suspended":            OrderStatus.FAILED,
    "calculated":           OrderStatus.SUBMITTED,
    "held":                 OrderStatus.PENDING,
}

# Alpaca "open" statuses — orders that are still working
_OPEN_STATUSES = {
    "new", "partially_filled", "pending_cancel", "pending_replace",
    "pending_review", "accepted", "pending_new", "accepted_for_bidding",
    "calculated", "held",
}


def _map_order_status(status_value: str) -> OrderStatus:
    """Map Alpaca order status string to Atlas OrderStatus."""
    return _STATUS_MAP.get(str(status_value).lower(), OrderStatus.UNKNOWN)


def _map_side(side: OrderSide) -> "AlpacaSide":
    """Map Atlas OrderSide to Alpaca OrderSide."""
    if not ALPACA_AVAILABLE:
        return None
    return AlpacaSide.BUY if side == OrderSide.BUY else AlpacaSide.SELL


def _map_tif(tif_str: str) -> "TimeInForce":
    """Map time-in-force string to Alpaca TimeInForce enum."""
    if not ALPACA_AVAILABLE:
        return None
    tif_map = {
        "day": TimeInForce.DAY,
        "gtc": TimeInForce.GTC,
        "ioc": TimeInForce.IOC,
        "fok": TimeInForce.FOK,
        "opg": TimeInForce.OPG,
        "cls": TimeInForce.CLS,
    }
    return tif_map.get(tif_str.lower(), TimeInForce.DAY)


def _order_to_result(order, atlas_ticker: str, side: OrderSide) -> OrderResult:
    """Convert an Alpaca Order object to Atlas OrderResult."""
    status_val = str(getattr(order, "status", "")).lower()
    # Alpaca status may be an enum — extract .value if so
    if hasattr(order.status, "value"):
        status_val = str(order.status.value).lower()

    filled_qty_raw = getattr(order, "filled_qty", None) or 0
    filled_avg = getattr(order, "filled_avg_price", None) or 0.0
    requested_qty_raw = getattr(order, "qty", None) or 0
    limit_price = float(getattr(order, "limit_price", None) or 0)
    stop_price = float(getattr(order, "stop_price", None) or 0)
    requested_price = limit_price or stop_price or 0.0

    return OrderResult(
        success=True,
        order_id=str(getattr(order, "id", "")),
        ticker=atlas_ticker,
        side=side,
        status=_map_order_status(status_val),
        requested_qty=int(float(requested_qty_raw)) if requested_qty_raw else 0,
        filled_qty=int(float(filled_qty_raw)) if filled_qty_raw else 0,
        requested_price=float(requested_price),
        fill_price=float(filled_avg),
        message=f"status={status_val}",
        raw={
            "id": str(getattr(order, "id", "")),
            "client_order_id": str(getattr(order, "client_order_id", "")),
            "status": status_val,
            "symbol": str(getattr(order, "symbol", "")),
            "filled_at": str(getattr(order, "filled_at", "")),
            "submitted_at": str(getattr(order, "submitted_at", "")),
        },
    )


# ═══════════════════════════════════════════════════════════════
# Alpaca Broker
# ═══════════════════════════════════════════════════════════════

class AlpacaBroker(BrokerAdapter):
    """Live or paper US equity trading via Alpaca Markets.

    Supports SP500 (and any US equity) via Alpaca's REST API.
    Uses the official alpaca-py SDK — no raw HTTP calls.

    Paper trading is the default (paper=True in config or unset).
    Set 'alpaca.paper: false' in config for live real-money trading.

    Credentials are loaded from environment variables or
    ~/.atlas-secrets.json using keys ALPACA_API_KEY and
    ALPACA_SECRET_KEY.
    """

    def __init__(self, config: dict, live: bool = False):
        super().__init__(config)
        self._live = live

        alpaca_cfg = config.get("alpaca", {})

        # 'paper' in config takes precedence over the live flag.
        # paper=True → simulated orders (safe default)
        # paper=False → real money orders
        self._paper = alpaca_cfg.get("paper", not live)

        # Data feed: "iex" (free) or "sip" (paid subscription)
        self._feed = alpaca_cfg.get("feed", "iex")

        # Default time in force
        self._tif = alpaca_cfg.get("tif", "day")

        self._trade_client: Optional["TradingClient"] = None
        self._market_data: Optional[AlpacaMarketData] = None

        # Track Atlas order_id → Alpaca order ID mapping (both are the same
        # UUID returned by Alpaca — we use Alpaca's ID as our order_id)
        # Kept for explicit clarity and future local state if needed.
        self._order_map: dict[str, str] = {}

    # ── Properties ─────────────────────────────────────────────

    @property
    def name(self) -> str:
        mode = "PAPER" if self._paper else "LIVE"
        return f"AlpacaBroker[{mode}]"

    @property
    def is_live(self) -> bool:
        """True only if we are NOT in paper mode."""
        return not self._paper

    @property
    def market_id(self) -> str:
        """Atlas market ID served by this broker."""
        return "sp500"

    # ── Lifecycle ──────────────────────────────────────────────

    def connect(self) -> bool:
        """Authenticate with Alpaca API and verify account access.

        Loads API credentials from environment or ~/.atlas-secrets.json.
        Validates connectivity by fetching account info.

        Returns:
            True on success, False on failure.
        """
        if not ALPACA_AVAILABLE:
            logger.error("alpaca-py not installed — cannot connect")
            return False

        # Load credentials
        api_key = get_secret("ALPACA_API_KEY", prompt=False)
        api_secret = get_secret("ALPACA_SECRET_KEY", prompt=False)

        if not api_key or not api_secret:
            logger.error(
                "Alpaca credentials not found. Set ALPACA_API_KEY and "
                "ALPACA_SECRET_KEY in environment or ~/.atlas-secrets.json"
            )
            return False

        try:
            self._trade_client = TradingClient(
                api_key=api_key,
                secret_key=api_secret,
                paper=self._paper,
            )

            # Initialise market data client (uses same credentials)
            self._market_data = AlpacaMarketData(
                api_key=api_key,
                api_secret=api_secret,
                feed=self._feed,
            )

            # Validate connectivity — fetch account (raises on auth failure)
            account = self._trade_client.get_account()
            equity = float(getattr(account, "equity", 0) or 0)
            status = str(getattr(account, "status", "UNKNOWN"))

            logger.info(
                "AlpacaBroker connected: paper=%s feed=%s equity=$%.2f status=%s",
                self._paper, self._feed, equity, status,
            )

            # Warn if account is not active
            if status.upper() not in ("ACTIVE",):
                logger.warning("Alpaca account status is '%s' (expected ACTIVE)", status)

            self._connected = True
            return True

        except Exception as e:
            logger.error("AlpacaBroker connect failed: %s", e, exc_info=True)
            self._trade_client = None
            self._market_data = None
            return False

    def disconnect(self):
        """Clean up Alpaca client references."""
        self._trade_client = None
        self._market_data = None
        self._connected = False
        logger.info("AlpacaBroker disconnected")

    # ── Account ────────────────────────────────────────────────

    def get_account_info(self) -> AccountInfo:
        """Query account equity, cash, and buying power from Alpaca.

        Returns:
            AccountInfo populated from Alpaca's /v2/account endpoint.
        """
        self._require_connected()

        try:
            account = self._trade_client.get_account()
        except Exception as e:
            logger.error("get_account failed: %s", e, exc_info=True)
            return AccountInfo()

        equity = float(getattr(account, "equity", 0) or 0)
        cash = float(getattr(account, "cash", 0) or 0)
        buying_power = float(getattr(account, "buying_power", 0) or 0)
        portfolio_value = float(getattr(account, "portfolio_value", 0) or 0)
        long_market_value = float(getattr(account, "long_market_value", 0) or 0)

        # P&L vs configured starting equity
        starting = self.config.get("risk", {}).get("starting_equity", 10000)
        pnl = round(equity - starting, 2)
        pnl_pct = round(pnl / starting * 100, 2) if starting > 0 else 0.0

        # Account halted flags
        trading_blocked = bool(getattr(account, "trading_blocked", False))
        account_blocked = bool(getattr(account, "account_blocked", False))
        halted = trading_blocked or account_blocked
        halt_reason = ""
        if halted:
            halt_reason = (
                "trading_blocked" if trading_blocked else "account_blocked"
            )

        return AccountInfo(
            equity=round(equity, 2),
            cash=round(cash, 2),
            market_value=round(long_market_value, 2),
            buying_power=round(buying_power, 2),
            total_pnl=pnl,
            total_pnl_pct=pnl_pct,
            num_positions=0,        # filled by get_positions() call in callers
            currency="USD",
            market_id=self.market_id,
            halted=halted,
            halt_reason=halt_reason,
        )

    def get_positions(self) -> list[PositionInfo]:
        """Return all open positions from Alpaca.

        Returns:
            List of PositionInfo in Atlas format.
        """
        self._require_connected()

        try:
            raw_positions = self._trade_client.get_all_positions()
        except Exception as e:
            logger.error("get_all_positions failed: %s", e, exc_info=True)
            return []

        positions = []
        for pos in (raw_positions or []):
            symbol = str(getattr(pos, "symbol", ""))
            atlas_ticker = mapper.to_atlas(symbol)

            qty_raw = getattr(pos, "qty", 0) or 0
            qty = int(float(qty_raw))
            if qty == 0:
                continue

            avg_entry = float(getattr(pos, "avg_entry_price", 0) or 0)
            current_price = float(getattr(pos, "current_price", 0) or 0)
            market_value = float(getattr(pos, "market_value", 0) or 0)
            cost_basis = float(getattr(pos, "cost_basis", 0) or 0)
            unrealized_pl = float(getattr(pos, "unrealized_pl", 0) or 0)
            unrealized_plpc = float(getattr(pos, "unrealized_plpc", 0) or 0)
            # Alpaca gives unrealized_plpc as a decimal (e.g. 0.05 = 5%)
            unrealized_plpc_pct = round(unrealized_plpc * 100, 2)

            positions.append(PositionInfo(
                ticker=atlas_ticker,
                entry_price=round(avg_entry, 4),
                shares=qty,
                current_price=round(current_price, 4),
                market_value=round(market_value, 2),
                unrealized_pnl=round(unrealized_pl, 2),
                unrealized_pnl_pct=unrealized_plpc_pct,
                cost_basis=round(cost_basis, 2),
            ))

        logger.debug("get_positions: %d open positions", len(positions))
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
        """Place an order on Alpaca.

        Supports market, limit, stop, stop_limit, and trailing_stop orders.
        Maps Atlas OrderType to the corresponding alpaca-py request class.

        Args:
            ticker:      Atlas-format US ticker (e.g. 'AAPL').
            side:        BUY or SELL.
            qty:         Number of whole shares. Use kwargs['notional'] for
                         fractional dollar-based orders.
            price:       Limit price (used for LIMIT and STOP_LIMIT orders).
            order_type:  Atlas OrderType enum.
            stop_price:  Stop trigger price (STOP, STOP_LIMIT, TRAILING_STOP).
            remark:      Free-text label stored as client_order_id prefix.

        Kwargs:
            notional (float): Dollar amount for fractional share orders.
                              When provided, qty is ignored.
            trail_percent (float): Trailing stop as percentage of price.
            trail_price (float):   Trailing stop as dollar amount.
            extended_hours (bool): Allow extended-hours execution.

        Returns:
            OrderResult with success flag and order_id on success.
        """
        self._require_connected()
        alpaca_symbol = mapper.to_alpaca(ticker)
        alpaca_side = _map_side(side)
        tif = _map_tif(self._tif)
        client_id = f"atlas_{remark[:8] if remark else uuid.uuid4().hex[:8]}"

        # Dollar-amount order (fractional shares)
        notional = kwargs.get("notional")
        extended_hours = kwargs.get("extended_hours", False)

        logger.info(
            "Placing order: %s %s %s qty=%d price=%.4f type=%s%s",
            alpaca_symbol, side.value, order_type.value, qty, price,
            " notional=$%.2f" % notional if notional else "",
            f" [{'LIVE' if self.is_live else 'PAPER'}]",
        )

        try:
            order_data = _build_order_request(
                symbol=alpaca_symbol,
                side=alpaca_side,
                qty=qty,
                price=price,
                order_type=order_type,
                stop_price=stop_price,
                tif=tif,
                client_id=client_id,
                notional=notional,
                extended_hours=extended_hours,
                trail_percent=kwargs.get("trail_percent"),
                trail_price=kwargs.get("trail_price"),
            )
        except ValueError as e:
            logger.error("build_order_request failed for %s: %s", ticker, e)
            return OrderResult(
                success=False, ticker=ticker, side=side,
                status=OrderStatus.FAILED, requested_qty=qty,
                requested_price=price, message=str(e),
            )

        try:
            order = self._trade_client.submit_order(order_data)
        except Exception as e:
            logger.error("submit_order failed for %s: %s", ticker, e, exc_info=True)
            return OrderResult(
                success=False, ticker=ticker, side=side,
                status=OrderStatus.FAILED, requested_qty=qty,
                requested_price=price, message=str(e),
            )

        result = _order_to_result(order, ticker, side)
        logger.info(
            "Order submitted: %s %s → id=%s status=%s",
            alpaca_symbol, side.value, result.order_id, result.status.value,
        )
        return result

    def cancel_order(self, order_id: str) -> OrderResult:
        """Cancel a specific order by its Alpaca order ID.

        Args:
            order_id: Alpaca order UUID (returned as order_id by place_order).

        Returns:
            OrderResult indicating success or failure.
        """
        self._require_connected()

        try:
            self._trade_client.cancel_order_by_id(order_id)
            logger.info("Order cancelled: %s", order_id)
            return OrderResult(
                success=True, order_id=order_id,
                status=OrderStatus.CANCELLED, message="Cancelled",
            )
        except Exception as e:
            logger.error("cancel_order failed for %s: %s", order_id, e, exc_info=True)
            return OrderResult(
                success=False, order_id=order_id,
                status=OrderStatus.FAILED, message=str(e),
            )

    def cancel_all_orders(self) -> list[OrderResult]:
        """Cancel all open orders.

        Alpaca returns a list of CancelOrderResponse with status per order.
        We map each to an OrderResult.

        Returns:
            List of OrderResult (one per cancelled order).
        """
        self._require_connected()

        try:
            responses = self._trade_client.cancel_orders()
        except Exception as e:
            logger.error("cancel_orders failed: %s", e, exc_info=True)
            return [OrderResult(
                success=False, status=OrderStatus.FAILED, message=str(e),
            )]

        if not responses:
            logger.info("cancel_all_orders: no open orders to cancel")
            return []

        results = []
        for resp in responses:
            order_id = str(getattr(resp, "id", ""))
            # HTTP status 200 means successfully cancelled
            status_code = getattr(resp, "status", None)
            success = (status_code == 200) if status_code is not None else True
            results.append(OrderResult(
                success=success,
                order_id=order_id,
                status=OrderStatus.CANCELLED if success else OrderStatus.FAILED,
                message=f"cancel status={status_code}",
            ))

        logger.warning("cancel_all_orders: cancelled %d orders", len(results))
        return results

    def get_open_orders(self) -> list[OrderResult]:
        """Return all currently open/working orders.

        Queries Alpaca with status=open, which returns orders in:
        new, partially_filled, pending states.

        Returns:
            List of OrderResult for open orders.
        """
        self._require_connected()

        try:
            req = GetOrdersRequest(status=QueryOrderStatus.OPEN)
            orders = self._trade_client.get_orders(req)
        except Exception as e:
            logger.error("get_orders(open) failed: %s", e, exc_info=True)
            return []

        results = []
        for order in (orders or []):
            symbol = str(getattr(order, "symbol", ""))
            atlas_ticker = mapper.to_atlas(symbol)
            side_raw = getattr(order, "side", None)
            side_str = str(side_raw.value if hasattr(side_raw, "value") else side_raw).lower()
            side = OrderSide.BUY if side_str == "buy" else OrderSide.SELL
            results.append(_order_to_result(order, atlas_ticker, side))

        logger.debug("get_open_orders: %d open orders", len(results))
        return results

    def sync_all_protective_orders(
        self,
        positions: list,
        plan: Optional[dict] = None,
        *,
        trade_date: str = "",
        dry_run: bool = False,
    ) -> dict:
        """Sync protective stop orders for all live positions.

        Idempotent — existing stop/stop_limit sell orders are detected and skipped.
        Only missing SL orders are placed.

        Args:
            positions:   List of PositionInfo objects (fetched from Alpaca if empty).
            plan:        Today's trade plan dict (for stop price lookups).
                         Accepted shapes: {ticker: {stop_price: X}},
                         {'entries': [{ticker, stop_price}, ...]}, or a plain list.
            trade_date:  YYYY-MM-DD (informational; defaults to today if empty).
            dry_run:     Log intent but do NOT send orders.

        Returns:
            Summary dict: {"sl_placed": N, "sl_already_exists": M, "errors": E,
                           "per_ticker": {ticker: {action, ...}}}
        """
        self._require_connected()

        sl_placed = 0
        sl_already_exists = 0
        errors = 0
        per_ticker: dict = {}

        # Fetch positions from Alpaca if caller did not supply them
        if not positions:
            positions = self.get_positions()

        if not positions:
            logger.info("sync_all_protective_orders: no open positions — nothing to sync")
            return {"sl_placed": 0, "sl_already_exists": 0, "errors": 0, "per_ticker": {}}

        # Fetch all open orders directly so we can inspect order_type
        try:
            req = GetOrdersRequest(status=QueryOrderStatus.OPEN)
            open_orders_raw = self._trade_client.get_orders(req)
        except Exception as e:
            logger.error("sync_all_protective_orders: get_orders failed: %s", e, exc_info=True)
            return {
                "sl_placed": 0, "sl_already_exists": 0,
                "errors": len(positions), "per_ticker": {},
            }

        # Build set of tickers that already have a stop or stop_limit SELL order
        tickers_with_stop: set = set()
        for order in (open_orders_raw or []):
            order_type_raw = getattr(order, "order_type", None)
            order_type_str = str(
                order_type_raw.value if hasattr(order_type_raw, "value") else order_type_raw
            ).lower()
            side_raw = getattr(order, "side", None)
            side_str = str(
                side_raw.value if hasattr(side_raw, "value") else side_raw
            ).lower()
            if side_str == "sell" and order_type_str in ("stop", "stop_limit"):
                symbol = str(getattr(order, "symbol", ""))
                atlas_ticker = mapper.to_atlas(symbol)
                tickers_with_stop.add(atlas_ticker)
                logger.debug(
                    "sync_protective: found existing stop SELL for %s (type=%s)",
                    atlas_ticker, order_type_str,
                )

        # Normalise plan into {ticker: entry_dict} for stop-price lookup
        plan_by_ticker: dict = {}
        if plan:
            if isinstance(plan, list):
                for e in plan:
                    t = e.get("ticker", "")
                    if t:
                        plan_by_ticker[t] = e
            elif isinstance(plan, dict):
                entries = plan.get("entries") or plan.get("plan_entries") or []
                if entries:
                    for e in entries:
                        t = e.get("ticker", "")
                        if t:
                            plan_by_ticker[t] = e
                else:
                    # Direct {ticker: {stop_price: X}} mapping
                    for k, v in plan.items():
                        if isinstance(v, dict):
                            plan_by_ticker[k] = v

        trade_date_label = trade_date or "today"

        for pos in positions:
            ticker = pos.ticker
            try:
                if ticker in tickers_with_stop:
                    sl_already_exists += 1
                    per_ticker[ticker] = {"action": "skipped", "reason": "stop_exists"}
                    logger.debug("sync_protective: %s already has stop order — skipping", ticker)
                    continue

                # Resolve stop price: plan entry → fallback 5 % below entry price
                plan_entry = plan_by_ticker.get(ticker, {})
                stop_price = (
                    plan_entry.get("stop_price")
                    or plan_entry.get("sl_price")
                    or plan_entry.get("stop")
                )
                if not stop_price:
                    stop_price = round(pos.entry_price * 0.95, 2)
                    logger.warning(
                        "sync_protective: no stop_price in plan for %s (%s) — "
                        "using 5%% fallback: %.2f",
                        ticker, trade_date_label, stop_price,
                    )
                else:
                    stop_price = round(float(stop_price), 2)

                if dry_run:
                    logger.info(
                        "sync_protective [DRY RUN]: would place STOP SELL %s "
                        "qty=%d stop=%.2f",
                        ticker, pos.shares, stop_price,
                    )
                    sl_placed += 1
                    per_ticker[ticker] = {
                        "action": "dry_run_placed",
                        "stop_price": stop_price,
                        "qty": pos.shares,
                    }
                    continue

                result = self.place_order(
                    ticker=ticker,
                    side=OrderSide.SELL,
                    qty=pos.shares,
                    price=0.0,
                    order_type=OrderType.STOP,
                    stop_price=stop_price,
                    remark="sync_sl",
                )

                if result.success:
                    sl_placed += 1
                    per_ticker[ticker] = {
                        "action": "placed",
                        "order_id": result.order_id,
                        "stop_price": stop_price,
                        "qty": pos.shares,
                    }
                    logger.info(
                        "sync_protective: placed STOP SELL %s qty=%d stop=%.2f → id=%s",
                        ticker, pos.shares, stop_price, result.order_id,
                    )
                else:
                    errors += 1
                    per_ticker[ticker] = {"action": "error", "message": result.message}
                    logger.error(
                        "sync_protective: place_order failed for %s: %s",
                        ticker, result.message,
                    )

            except Exception as e:
                errors += 1
                per_ticker[ticker] = {"action": "error", "message": str(e)}
                logger.error(
                    "sync_protective: unexpected error for %s: %s", ticker, e, exc_info=True
                )

        logger.info(
            "sync_all_protective_orders complete: sl_placed=%d sl_already_exists=%d errors=%d",
            sl_placed, sl_already_exists, errors,
        )
        return {
            "sl_placed": sl_placed,
            "sl_already_exists": sl_already_exists,
            "errors": errors,
            "per_ticker": per_ticker,
        }

    def get_order_status(self, order_id: str) -> OrderResult:
        """Query the current status of a specific order.

        Args:
            order_id: Alpaca order UUID.

        Returns:
            OrderResult with current status, or failed result if not found.
        """
        self._require_connected()

        try:
            order = self._trade_client.get_order_by_id(order_id)
        except Exception as e:
            logger.error("get_order_by_id(%s) failed: %s", order_id, e, exc_info=True)
            return OrderResult(
                success=False, order_id=order_id,
                status=OrderStatus.UNKNOWN, message=str(e),
            )

        symbol = str(getattr(order, "symbol", ""))
        atlas_ticker = mapper.to_atlas(symbol)
        side_raw = getattr(order, "side", None)
        side_str = str(side_raw.value if hasattr(side_raw, "value") else side_raw).lower()
        side = OrderSide.BUY if side_str == "buy" else OrderSide.SELL

        return _order_to_result(order, atlas_ticker, side)

    # ── Market Data (real-time) ────────────────────────────────

    def get_prices(self, tickers: list[str]) -> dict[str, float]:
        """Get latest prices for US tickers via Alpaca data API.

        Uses snapshot (quote + bar) for best-effort real-time price.
        Falls back gracefully if market data is unavailable.

        Args:
            tickers: List of Atlas-format tickers (AAPL, MSFT, ...).

        Returns:
            Dict of ticker → price. Missing tickers not in dict.
        """
        if not self._market_data:
            return {}

        try:
            return self._market_data.get_prices(tickers)
        except Exception as e:
            logger.error("get_prices failed: %s", e, exc_info=True)
            return {}

    def get_market_snapshot(self, ticker: str) -> Optional[dict]:
        """Get a full market snapshot for a single ticker.

        Returns rich dict with latest_trade, latest_quote, minute_bar,
        daily_bar, prev_daily_bar, and a best-effort 'price'.

        Args:
            ticker: Atlas-format US ticker (e.g. 'AAPL').

        Returns:
            Snapshot dict or None if unavailable.
        """
        if not self._market_data:
            return None
        try:
            return self._market_data.get_snapshot(ticker)
        except Exception as e:
            logger.error("get_market_snapshot(%s) failed: %s", ticker, e, exc_info=True)
            return None

    # ── Deals / History ────────────────────────────────────────

    def get_today_deals(self) -> list[DealInfo]:
        """Get today's executed fills.

        Queries closed orders placed today and extracts filled ones.
        Alpaca does not have a separate 'deals' endpoint in the Trading API —
        fills are embedded in the order object.

        Returns:
            List of DealInfo for today's fills.
        """
        self._require_connected()

        now_utc = datetime.now(timezone.utc)
        start_of_day = now_utc.replace(hour=0, minute=0, second=0, microsecond=0)

        try:
            req = GetOrdersRequest(
                status=QueryOrderStatus.CLOSED,
                after=start_of_day,
                limit=500,
            )
            orders = self._trade_client.get_orders(req)
        except Exception as e:
            logger.error("get_today_deals query failed: %s", e, exc_info=True)
            return []

        return _orders_to_deals(orders or [])

    def get_history_deals(self, days: int = 30) -> list[DealInfo]:
        """Get historical fills for the past N days.

        Args:
            days: Number of calendar days to look back.

        Returns:
            List of DealInfo for fills in the period.
        """
        self._require_connected()

        now_utc = datetime.now(timezone.utc)
        start = now_utc - timedelta(days=days)

        try:
            req = GetOrdersRequest(
                status=QueryOrderStatus.CLOSED,
                after=start,
                limit=500,
            )
            orders = self._trade_client.get_orders(req)
        except Exception as e:
            logger.error("get_history_deals query failed: %s", e, exc_info=True)
            return []

        return _orders_to_deals(orders or [])

    def get_history_orders(self, days: int = 30) -> list[OrderResult]:
        """Get historical orders for the past N days (all statuses).

        Args:
            days: Number of calendar days to look back.

        Returns:
            List of OrderResult covering all closed orders in the period.
        """
        self._require_connected()

        now_utc = datetime.now(timezone.utc)
        start = now_utc - timedelta(days=days)

        try:
            req = GetOrdersRequest(
                status=QueryOrderStatus.ALL,
                after=start,
                limit=500,
            )
            orders = self._trade_client.get_orders(req)
        except Exception as e:
            logger.error("get_history_orders query failed: %s", e, exc_info=True)
            return []

        results = []
        for order in (orders or []):
            symbol = str(getattr(order, "symbol", ""))
            atlas_ticker = mapper.to_atlas(symbol)
            side_raw = getattr(order, "side", None)
            side_str = str(
                side_raw.value if hasattr(side_raw, "value") else side_raw
            ).lower()
            side = OrderSide.BUY if side_str == "buy" else OrderSide.SELL
            results.append(_order_to_result(order, atlas_ticker, side))

        return results

    # ── Internal ───────────────────────────────────────────────

    def _require_connected(self):
        if not self._connected or self._trade_client is None:
            raise RuntimeError(
                "AlpacaBroker not connected. Call connect() first."
            )


# ═══════════════════════════════════════════════════════════════
# Internal helpers
# ═══════════════════════════════════════════════════════════════

def _build_order_request(
    symbol: str,
    side: "AlpacaSide",
    qty: int,
    price: float,
    order_type: OrderType,
    stop_price: Optional[float],
    tif: "TimeInForce",
    client_id: str,
    notional: Optional[float] = None,
    extended_hours: bool = False,
    trail_percent: Optional[float] = None,
    trail_price: Optional[float] = None,
) -> object:
    """Build the appropriate alpaca-py order request object.

    Each order type requires a specific request class; mixing params
    across classes raises validation errors from Pydantic.

    Args:
        symbol:       Alpaca-format symbol (e.g. 'AAPL').
        side:         AlpacaSide enum value.
        qty:          Whole share quantity (ignored if notional is set).
        price:        Limit price (LIMIT, STOP_LIMIT).
        order_type:   Atlas OrderType enum.
        stop_price:   Stop trigger price (STOP, STOP_LIMIT).
        tif:          TimeInForce enum value.
        client_id:    Unique client order ID string.
        notional:     Dollar amount for fractional orders.
        extended_hours: Allow extended-hours execution.
        trail_percent: Trailing stop as % of market price.
        trail_price:  Trailing stop as dollar offset.

    Returns:
        An alpaca-py OrderRequest subclass instance.

    Raises:
        ValueError: If required parameters are missing for the order type.
    """
    # Determine qty or notional
    qty_param = None if notional else qty
    notional_param = round(notional, 2) if notional else None

    if order_type == OrderType.MARKET:
        return MarketOrderRequest(
            symbol=symbol,
            qty=qty_param,
            notional=notional_param,
            side=side,
            time_in_force=tif,
            client_order_id=client_id,
            extended_hours=extended_hours,
        )

    elif order_type == OrderType.LIMIT:
        if not price:
            raise ValueError("Limit price required for LIMIT order")
        return LimitOrderRequest(
            symbol=symbol,
            qty=qty_param,
            notional=notional_param,
            side=side,
            time_in_force=tif,
            limit_price=round(price, 2),
            client_order_id=client_id,
            extended_hours=extended_hours,
        )

    elif order_type == OrderType.STOP:
        if not stop_price:
            raise ValueError("stop_price required for STOP order")
        return StopOrderRequest(
            symbol=symbol,
            qty=qty_param,
            side=side,
            time_in_force=tif,
            stop_price=round(stop_price, 2),
            client_order_id=client_id,
        )

    elif order_type == OrderType.STOP_LIMIT:
        if not price:
            raise ValueError("price (limit_price) required for STOP_LIMIT order")
        if not stop_price:
            raise ValueError("stop_price required for STOP_LIMIT order")
        return StopLimitOrderRequest(
            symbol=symbol,
            qty=qty_param,
            side=side,
            time_in_force=tif,
            limit_price=round(price, 2),
            stop_price=round(stop_price, 2),
            client_order_id=client_id,
        )

    elif order_type == OrderType.TRAILING_STOP:
        if trail_percent is None and trail_price is None:
            # Default: 2% trailing stop if no trail params given
            trail_percent = 2.0
            logger.warning(
                "TrailingStop placed without trail_percent or trail_price — "
                "defaulting to 2%% trail"
            )
        req_kwargs: dict = dict(
            symbol=symbol,
            qty=qty_param,
            side=side,
            time_in_force=tif,
            client_order_id=client_id,
        )
        if trail_percent is not None:
            req_kwargs["trail_percent"] = float(trail_percent)
        elif trail_price is not None:
            req_kwargs["trail_price"] = round(float(trail_price), 2)
        return TrailingStopOrderRequest(**req_kwargs)

    else:
        raise ValueError(f"Unsupported order type: {order_type}")


def _orders_to_deals(orders: list) -> list[DealInfo]:
    """Convert a list of Alpaca filled orders to DealInfo objects.

    Only orders with status=filled (or partially_filled with filled_qty>0)
    are included. Each order generates one DealInfo at the VWAP fill price.
    """
    deals = []
    for order in orders:
        status_raw = getattr(order, "status", None)
        status_str = str(
            status_raw.value if hasattr(status_raw, "value") else status_raw
        ).lower()

        filled_qty_raw = getattr(order, "filled_qty", None) or 0
        filled_qty = int(float(filled_qty_raw)) if filled_qty_raw else 0
        if filled_qty == 0:
            continue

        symbol = str(getattr(order, "symbol", ""))
        atlas_ticker = mapper.to_atlas(symbol)
        fill_price = float(getattr(order, "filled_avg_price", None) or 0)
        filled_at = str(getattr(order, "filled_at", "") or "")

        side_raw = getattr(order, "side", None)
        side_str = str(
            side_raw.value if hasattr(side_raw, "value") else side_raw
        ).lower()
        side = OrderSide.BUY if side_str == "buy" else OrderSide.SELL

        deals.append(DealInfo(
            order_id=str(getattr(order, "id", "")),
            ticker=atlas_ticker,
            side=side,
            qty=filled_qty,
            price=fill_price,
            deal_time=filled_at,
            raw={
                "symbol": symbol,
                "status": status_str,
                "client_order_id": str(getattr(order, "client_order_id", "")),
            },
        ))

    return deals
