"""Alpaca Markets broker implementation.

Connects to Alpaca Markets REST API via the alpaca-py SDK.
Supports both paper trading and live (real-money) trading.

Alpaca offers commission-free US equity trading with fractional shares.
Paper trading uses a separate endpoint but otherwise identical API.

Dependencies:
    pip install alpaca-py

Credentials (loaded in order: env → ~/.atlas-secrets.json):
    ALPACA_API_KEY    — Alpaca API key ID (starts with PK for paper)
    ALPACA_SECRET_KEY — Alpaca API secret key

Config section in market config JSON:
    "alpaca": {
        "paper": true,
        "data_feed": "iex",
        "_credentials": "Managed via ~/.atlas-secrets.json"
    }

Usage:
    broker = AlpacaBroker(config, live=False)
    broker.connect()
    info = broker.get_account_info()
    broker.disconnect()
"""

from __future__ import annotations

import logging
import uuid
from typing import Optional

from brokers.base import (
    BrokerAdapter,
    AccountInfo,
    PositionInfo,
    OrderResult,
    OrderSide,
    OrderStatus,
    OrderType,
)
from brokers.alpaca.mapper import to_alpaca, to_atlas
from brokers.secrets import get_secret

logger = logging.getLogger("atlas.broker.alpaca")

# ── Optional alpaca-py imports — graceful if SDK not installed ────────────────

try:
    from alpaca.trading.client import TradingClient
    from alpaca.trading.requests import (
        MarketOrderRequest,
        LimitOrderRequest,
        StopOrderRequest,
        StopLimitOrderRequest,
        GetOrdersRequest,
    )
    from alpaca.trading.enums import (
        OrderSide as AlpacaSide,
        TimeInForce,
        QueryOrderStatus,
    )
    _ALPACA_AVAILABLE = True
except ImportError:
    TradingClient = None          # type: ignore[assignment,misc]
    MarketOrderRequest = None     # type: ignore[assignment,misc]
    LimitOrderRequest = None      # type: ignore[assignment,misc]
    StopOrderRequest = None       # type: ignore[assignment,misc]
    StopLimitOrderRequest = None  # type: ignore[assignment,misc]
    GetOrdersRequest = None       # type: ignore[assignment,misc]
    AlpacaSide = None             # type: ignore[assignment,misc]
    TimeInForce = None            # type: ignore[assignment,misc]
    QueryOrderStatus = None       # type: ignore[assignment,misc]
    _ALPACA_AVAILABLE = False
    logger.warning("alpaca-py not installed — run: pip install alpaca-py")


# ── Alpaca order status → Atlas OrderStatus ─────────────────────────────────

_STATUS_MAP: dict[str, OrderStatus] = {
    "new":                  OrderStatus.SUBMITTED,
    "accepted":             OrderStatus.SUBMITTED,
    "accepted_for_bidding": OrderStatus.SUBMITTED,
    "pending_new":          OrderStatus.PENDING,
    "pending_cancel":       OrderStatus.PENDING,
    "pending_replace":      OrderStatus.PENDING,
    "pending_review":       OrderStatus.PENDING,
    "held":                 OrderStatus.PENDING,
    "partially_filled":     OrderStatus.PARTIAL_FILLED,
    "filled":               OrderStatus.FILLED,
    "calculated":           OrderStatus.FILLED,   # post-fill calc state
    "done_for_day":         OrderStatus.CANCELLED,
    "canceled":             OrderStatus.CANCELLED,
    "expired":              OrderStatus.CANCELLED,
    "replaced":             OrderStatus.CANCELLED,
    "rejected":             OrderStatus.FAILED,
    "stopped":              OrderStatus.FAILED,
    "suspended":            OrderStatus.FAILED,
}


def _map_status(alpaca_status) -> OrderStatus:
    """Map Alpaca order status enum/string to Atlas OrderStatus."""
    val = str(alpaca_status.value if hasattr(alpaca_status, "value") else alpaca_status).lower()
    return _STATUS_MAP.get(val, OrderStatus.UNKNOWN)


def _map_side(alpaca_side) -> OrderSide:
    """Map Alpaca order side to Atlas OrderSide."""
    val = str(alpaca_side.value if hasattr(alpaca_side, "value") else alpaca_side).lower()
    return OrderSide.BUY if val == "buy" else OrderSide.SELL


# ── AlpacaBroker ─────────────────────────────────────────────────────────────


class AlpacaBroker(BrokerAdapter):
    """Alpaca Markets broker implementing the Atlas BrokerAdapter interface.

    Supports paper and live US equity trading via the alpaca-py REST SDK.
    No moomoo or IBKR dependencies — self-contained.
    """

    def __init__(self, config: dict, live: bool = False):
        super().__init__(config)
        self._live = live
        self._alpaca_cfg = config.get("alpaca", {})
        self._market_id = config.get("market", "sp500")

        # Paper mode: overridden by alpaca config section; live flag is a
        # secondary gate (live=True AND paper=False → real money)
        paper_cfg = self._alpaca_cfg.get("paper", True)
        self._paper = not live or paper_cfg  # Default: paper unless explicitly live

        self._trading_client = None   # alpaca.trading.client.TradingClient
        self._data_feed = self._alpaca_cfg.get("data_feed", "iex")

    # ── Properties ──────────────────────────────────────────────────────────

    @property
    def name(self) -> str:
        return "alpaca"

    @property
    def is_live(self) -> bool:
        return self._live and not self._paper

    # ── Lifecycle ────────────────────────────────────────────────────────────

    def connect(self) -> bool:
        """Connect to Alpaca Markets API.

        Loads credentials from environment / ~/.atlas-secrets.json.
        Returns True on success, False if credentials missing or auth fails.
        """
        if not _ALPACA_AVAILABLE or TradingClient is None:
            logger.error("alpaca-py not installed — run: pip install alpaca-py")
            return False

        try:
            api_key = get_secret("ALPACA_API_KEY")
            secret_key = get_secret("ALPACA_SECRET_KEY")

            if not api_key or not secret_key:
                logger.error(
                    "Alpaca credentials missing — set ALPACA_API_KEY and "
                    "ALPACA_SECRET_KEY in env or ~/.atlas-secrets.json"
                )
                return False

            mode = "paper" if self._paper else "LIVE"
            logger.info("Connecting to Alpaca (%s)...", mode)

            self._trading_client = TradingClient(
                api_key=api_key,
                secret_key=secret_key,
                paper=self._paper,
            )

            # Validate credentials by fetching account
            account = self._trading_client.get_account()
            logger.info(
                "Alpaca connected — account %s, equity $%.2f (%s)",
                account.account_number,
                float(account.equity or 0),
                mode,
            )
            self._connected = True
            return True

        except Exception as e:
            logger.error("Alpaca connection failed: %s", e)
            self._connected = False
            return False

    def disconnect(self):
        """Clean up — alpaca-py is stateless HTTP so just clear the client."""
        if self._trading_client:
            self._trading_client = None
        self._connected = False
        logger.debug("Alpaca disconnected")

    # ── Account ─────────────────────────────────────────────────────────────

    def get_account_info(self) -> AccountInfo:
        """Query account equity, cash, and buying power from Alpaca."""
        if not self._connected or not self._trading_client:
            logger.warning("get_account_info called while disconnected")
            return AccountInfo(market_id=self._market_id)

        try:
            acct = self._trading_client.get_account()

            equity = float(acct.equity or 0)
            last_equity = float(acct.last_equity or 0)
            pnl = equity - last_equity
            pnl_pct = (pnl / last_equity) if last_equity else 0.0

            return AccountInfo(
                equity=equity,
                cash=float(acct.cash or 0),
                market_value=float(acct.long_market_value or 0),
                buying_power=float(acct.buying_power or 0),
                total_pnl=pnl,
                total_pnl_pct=pnl_pct,
                num_positions=0,   # filled separately by get_positions
                currency=acct.currency or "USD",
                market_id=self._market_id,
                halted=bool(acct.trading_blocked or acct.account_blocked),
                halt_reason=(
                    "account_blocked" if acct.account_blocked
                    else "trading_blocked" if acct.trading_blocked
                    else ""
                ),
            )

        except Exception as e:
            logger.error("get_account_info failed: %s", e)
            return AccountInfo(market_id=self._market_id)

    def get_positions(self) -> list[PositionInfo]:
        """Return all open positions from Alpaca."""
        if not self._connected or not self._trading_client:
            logger.warning("get_positions called while disconnected")
            return []

        try:
            raw_positions = self._trading_client.get_all_positions()
            result = []

            for pos in raw_positions:
                ticker = to_atlas(pos.symbol, self._market_id)
                shares = int(float(pos.qty or 0))
                if shares == 0:
                    continue

                entry_price = float(pos.avg_entry_price or 0)
                current_price = float(pos.current_price or 0)
                market_value = float(pos.market_value or 0)
                cost_basis = float(pos.cost_basis or 0)
                unrealized_pnl = float(pos.unrealized_pl or 0)
                unrealized_pnl_pct = float(pos.unrealized_plpc or 0)

                result.append(PositionInfo(
                    ticker=ticker,
                    strategy="",
                    entry_price=entry_price,
                    shares=shares,
                    current_price=current_price,
                    market_value=market_value,
                    cost_basis=cost_basis,
                    unrealized_pnl=unrealized_pnl,
                    unrealized_pnl_pct=unrealized_pnl_pct,
                ))

            logger.debug("get_positions: %d positions", len(result))
            return result

        except Exception as e:
            logger.error("get_positions failed: %s", e)
            return []

    # ── Orders ───────────────────────────────────────────────────────────────

    def place_order(
        self,
        ticker: str,
        side: OrderSide,
        qty: int,
        price: float,
        order_type: OrderType = OrderType.LIMIT,
        stop_price: Optional[float] = None,
        remark: str = "",
    ) -> OrderResult:
        """Place an order via Alpaca REST API.

        Maps Atlas OrderType/OrderSide to alpaca-py request objects.
        Returns OrderResult with order_id and status.
        """
        if not self._connected or not self._trading_client:
            return OrderResult(success=False, message="Not connected", ticker=ticker)

        if not _ALPACA_AVAILABLE:
            return OrderResult(
                success=False, message="alpaca-py not installed", ticker=ticker
            )

        try:
            symbol = to_alpaca(ticker, self._market_id)
            alpaca_side = AlpacaSide.BUY if side == OrderSide.BUY else AlpacaSide.SELL

            # Build the appropriate request object
            if order_type == OrderType.MARKET:
                req = MarketOrderRequest(
                    symbol=symbol,
                    qty=qty,
                    side=alpaca_side,
                    time_in_force=TimeInForce.DAY,
                )
            elif order_type == OrderType.LIMIT:
                req = LimitOrderRequest(
                    symbol=symbol,
                    qty=qty,
                    side=alpaca_side,
                    limit_price=round(price, 2),
                    time_in_force=TimeInForce.DAY,
                )
            elif order_type == OrderType.STOP:
                req = StopOrderRequest(
                    symbol=symbol,
                    qty=qty,
                    side=alpaca_side,
                    stop_price=round(stop_price or price, 2),
                    time_in_force=TimeInForce.DAY,
                )
            elif order_type == OrderType.STOP_LIMIT:
                req = StopLimitOrderRequest(
                    symbol=symbol,
                    qty=qty,
                    side=alpaca_side,
                    stop_price=round(stop_price or price, 2),
                    limit_price=round(price, 2),
                    time_in_force=TimeInForce.DAY,
                )
            else:
                # Fallback to limit for unsupported types
                logger.warning(
                    "Unsupported order type %s — falling back to LIMIT", order_type
                )
                req = LimitOrderRequest(
                    symbol=symbol,
                    qty=qty,
                    side=alpaca_side,
                    limit_price=round(price, 2),
                    time_in_force=TimeInForce.DAY,
                )

            order = self._trading_client.submit_order(order_data=req)

            order_status = _map_status(order.status)
            filled_qty = int(float(order.filled_qty or 0))
            fill_price = float(order.filled_avg_price or 0)

            logger.info(
                "Order placed: %s %s %d @ %.2f → id=%s status=%s",
                side.value, symbol, qty, price, order.id, order_status.value,
            )

            return OrderResult(
                success=True,
                order_id=str(order.id),
                ticker=ticker,
                side=side,
                status=order_status,
                requested_qty=qty,
                filled_qty=filled_qty,
                requested_price=price,
                fill_price=fill_price,
                commission=0.0,   # Alpaca: commission-free
                message=remark,
                raw={"alpaca_order_id": str(order.id), "symbol": symbol},
            )

        except Exception as e:
            logger.error("place_order failed for %s: %s", ticker, e)
            return OrderResult(
                success=False,
                ticker=ticker,
                side=side,
                requested_qty=qty,
                requested_price=price,
                message=str(e),
            )

    def cancel_order(self, order_id: str) -> OrderResult:
        """Cancel a specific order by ID."""
        if not self._connected or not self._trading_client:
            return OrderResult(success=False, message="Not connected", order_id=order_id)

        try:
            self._trading_client.cancel_order_by_id(uuid.UUID(order_id))
            logger.info("Cancelled order %s", order_id)
            return OrderResult(
                success=True,
                order_id=order_id,
                status=OrderStatus.CANCELLED,
                message="Cancelled",
            )

        except Exception as e:
            logger.error("cancel_order %s failed: %s", order_id, e)
            return OrderResult(
                success=False,
                order_id=order_id,
                message=str(e),
            )

    def cancel_all_orders(self) -> list[OrderResult]:
        """Cancel all open orders (emergency use)."""
        if not self._connected or not self._trading_client:
            return []

        try:
            cancelled = self._trading_client.cancel_orders()
            results = []

            for order in cancelled:
                results.append(OrderResult(
                    success=True,
                    order_id=str(order.id),
                    ticker=to_atlas(order.symbol, self._market_id),
                    status=OrderStatus.CANCELLED,
                    message="Bulk cancel",
                    raw={"symbol": order.symbol},
                ))

            logger.info("cancel_all_orders: cancelled %d orders", len(results))
            return results

        except Exception as e:
            logger.error("cancel_all_orders failed: %s", e)
            return []

    def get_open_orders(self) -> list[OrderResult]:
        """Return all currently open/pending orders."""
        if not self._connected or not self._trading_client:
            return []

        try:
            req = GetOrdersRequest(status=QueryOrderStatus.OPEN)
            orders = self._trading_client.get_orders(filter=req)
            return [self._order_to_result(o) for o in orders]

        except Exception as e:
            logger.error("get_open_orders failed: %s", e)
            return []

    def get_order_status(self, order_id: str) -> OrderResult:
        """Query status of a specific order by ID."""
        if not self._connected or not self._trading_client:
            return OrderResult(
                success=False,
                order_id=order_id,
                message="Not connected",
            )

        try:
            order = self._trading_client.get_order_by_id(uuid.UUID(order_id))
            return self._order_to_result(order)

        except Exception as e:
            logger.error("get_order_status %s failed: %s", order_id, e)
            return OrderResult(
                success=False,
                order_id=order_id,
                message=str(e),
            )

    # ── Internal helpers ─────────────────────────────────────────────────────

    def _order_to_result(self, order) -> OrderResult:
        """Convert an Alpaca Order object to Atlas OrderResult."""
        ticker = to_atlas(order.symbol or "", self._market_id)

        filled_qty = int(float(order.filled_qty or 0))
        fill_price = float(order.filled_avg_price or 0)
        requested_qty = int(float(order.qty or 0))
        limit_price = float(order.limit_price or 0)
        stop_price_val = float(order.stop_price or 0)
        # Use limit_price as the requested price if set, else stop_price
        requested_price = limit_price or stop_price_val

        return OrderResult(
            success=True,
            order_id=str(order.id),
            ticker=ticker,
            side=_map_side(order.side),
            status=_map_status(order.status),
            requested_qty=requested_qty,
            filled_qty=filled_qty,
            requested_price=requested_price,
            fill_price=fill_price,
            commission=0.0,
            raw={
                "symbol": order.symbol,
                "alpaca_status": str(order.status),
                "alpaca_type": str(order.order_type or order.type),
            },
        )

    def __repr__(self):
        mode = "LIVE" if self.is_live else "PAPER"
        status = "connected" if self._connected else "disconnected"
        return f"<AlpacaBroker [{mode}] {status}>"
