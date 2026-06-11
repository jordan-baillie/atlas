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
# TODO: Refactor — 1855 lines. Split into: AlpacaOrders, AlpacaPositions, AlpacaAccount modules.

from __future__ import annotations

import logging
import os
import time
import uuid
from datetime import datetime, timezone, timedelta
from typing import Optional

from atlas.brokers.base import (
    BrokerAdapter, OrderResult, PositionInfo, AccountInfo, DealInfo,
    OrderStatus, OrderSide, OrderType,
)
from atlas.brokers.alpaca import mapper
from atlas.brokers.alpaca.market_data import AlpacaMarketData
from atlas.brokers.retry import with_retry
from atlas.kernel.secrets import get_secret
from atlas.brokers.pdt_state import (
    is_pdt_deferred as _is_pdt_deferred_new,
    set_pdt_deferred as _set_pdt_deferred_new,
    _rth_close_today as _pdt_rth_close,
)

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
        StopLossRequest,
        TakeProfitRequest,
    )
    from alpaca.trading.enums import (
        OrderSide as AlpacaSide,
        OrderType as AlpacaOrderType,
        TimeInForce,
        QueryOrderStatus,
        OrderStatus as AlpacaOrderStatus,
        OrderClass,
    )
    ALPACA_AVAILABLE = True
except ImportError:
    ALPACA_AVAILABLE = False
    logger.warning("alpaca-py not installed. Run: pip install alpaca-py")


# ═══════════════════════════════════════════════════════════════
# PDT error detection
# ═══════════════════════════════════════════════════════════════

# Alpaca error code for Pattern Day Trading protection
_PDT_ERROR_CODE = "40310100"


def _is_pdt_error(message: str) -> bool:
    """Return True if an Alpaca rejection is due to Pattern Day Trading protection.

    PDT (Pattern Day Trade) protection triggers on accounts < $25k equity when
    a sell order is placed on a position opened the same trading day — the order
    could constitute a same-session round-trip (day trade) if it fills today.
    These are regulatory deferrals, not operational errors.  The stop will be
    placed successfully by the next pre-market sync (≥ next trading session).
    """
    return _PDT_ERROR_CODE in str(message)


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
    """Map Atlas OrderSide to Alpaca OrderSide.

    For short selling: SELL opens a short position (Alpaca handles automatically),
    BUY closes/covers a short position. No special handling needed.
    """
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
            # Dashboard-compatible aliases
            "order_status": status_val,
            "order_type": str(getattr(getattr(order, "order_type", None), "value", "limit")).lower(),
            "create_time": str(getattr(order, "submitted_at", "")),
            "symbol": str(getattr(order, "symbol", "")),
            "filled_at": str(getattr(order, "filled_at", "")),
            "submitted_at": str(getattr(order, "submitted_at", "")),
            "order_market": "US",
            # Price levels for stop/trailing stop/limit orders
            "stop_price": str(getattr(order, "stop_price", "") or ""),
            "limit_price": str(getattr(order, "limit_price", "") or ""),
            "trail_price": str(getattr(order, "trail_price", "") or ""),
            "trail_percent": str(getattr(order, "trail_percent", "") or ""),
            "qty": str(requested_qty_raw or ""),
            "side": str(getattr(getattr(order, "side", None), "value", "")).lower(),
            "order_class": str(getattr(getattr(order, "order_class", None), "value", "") or "").lower(),
        },
    )


# ═══════════════════════════════════════════════════════════════
# Alpaca Broker
# ═══════════════════════════════════════════════════════════════

class AlpacaBroker(BrokerAdapter):
    """Live or paper US equity trading via Alpaca Markets.

    Supports SP500 (and any US equity) via Alpaca's REST API.
    Uses the official alpaca-py SDK — no raw HTTP calls.

    Three trading modes (set via trading.mode in config):
      "live"  — real-money Alpaca account (ALPACA_API_KEY / ALPACA_SECRET_KEY)
      "paper" — Alpaca paper account (ALPACA_PAPER_API_KEY / ALPACA_PAPER_SECRET_KEY)
      "passive" — monitoring only, no orders placed

    The legacy 'alpaca.paper' config key is still respected for backward compatibility
    when mode is not explicitly set.
    """

    def __init__(self, config: dict, live: bool = False, mode: str = "live"):
        super().__init__(config)
        self._live = live
        self._mode = mode  # "live", "paper", or "passive"

        alpaca_cfg = config.get("alpaca", {})

        # mode="paper" always forces paper endpoint.
        # Legacy alpaca_cfg["paper"] key still respected for backward compat when
        # mode is not "paper" (e.g. mode="live" with alpaca.paper=true still works).
        self._paper = (mode == "paper") or alpaca_cfg.get("paper", not live)

        # Data feed: "iex" (free) or "sip" (paid subscription)
        self._feed = alpaca_cfg.get("feed", "iex")

        # Default time in force
        self._tif = alpaca_cfg.get("tif", "day")

        self._trade_client: Optional["TradingClient"] = None
        self._market_data: Optional[AlpacaMarketData] = None

        # Cached account number (set in connect()) for paper_account_id logging
        self._account_number: str = ""

        # Track Atlas order_id → Alpaca order ID mapping (both are the same
        # UUID returned by Alpaca — we use Alpaca's ID as our order_id)
        # Kept for explicit clarity and future local state if needed.
        self._order_map: dict[str, str] = {}

    # ── Properties ─────────────────────────────────────────────

    @property
    def name(self) -> str:
        """Human-readable broker name for operator log clarity.

        When mode is explicitly set to "paper" or "passive", uses the mode string.
        For mode="live", falls back to self._paper for backward compatibility with
        the legacy ``alpaca.paper: true`` config key (no explicit mode set).
        """
        if self._mode != "live":
            return f"AlpacaBroker[{self._mode.upper()}]"
        # mode="live": respect self._paper (includes legacy alpaca.paper=True config)
        return f"AlpacaBroker[{'PAPER' if self._paper else 'LIVE'}]"

    @property
    def mode(self) -> str:
        """Trading mode: 'live', 'paper', or 'passive'."""
        return self._mode

    @property
    def account_number(self) -> str:
        """Alpaca account number (populated after connect())."""
        return self._account_number

    @property
    def is_live(self) -> bool:
        """True only if we are NOT in paper mode."""
        return not self._paper

    @property
    def market_id(self) -> str:
        """Atlas market ID served by this broker."""
        return "sp500"

    # ── Retry helper ──────────────────────────────────────────

    def _broker_call(self, func, *args, **kwargs):
        """Execute an Alpaca SDK call with exponential backoff retry.

        Retries on transient errors (429, 502, 503, ConnectionError, TimeoutError).
        Raises immediately on non-retryable errors (400, 401, 403, 422).
        Raises the original exception after all retries are exhausted.

        Args:
            func: Callable (e.g. self._trade_client.get_account)
            *args, **kwargs: Forwarded to *func*.

        Returns:
            Whatever *func* returns.
        """
        _retried = with_retry(label=getattr(func, "__name__", str(func)))(
            lambda: func(*args, **kwargs)
        )
        return _retried()

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

        # Load credentials based on trading mode.
        # mode="paper" → ALPACA_PAPER_* keys (Alpaca paper account, virtual $)
        # mode="live"  → ALPACA_* keys (Alpaca live account, real money)
        if self._mode == "paper":
            api_key = get_secret("ALPACA_PAPER_API_KEY", prompt=False)
            api_secret = get_secret("ALPACA_PAPER_SECRET_KEY", prompt=False)
            if not api_key or not api_secret:
                logger.error(
                    "Alpaca paper credentials not found. Set ALPACA_PAPER_API_KEY and "
                    "ALPACA_PAPER_SECRET_KEY in environment or ~/.atlas-secrets.json"
                )
                return False
        else:
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
            raw_status = getattr(account, "status", "UNKNOWN")
            # Handle both enum (AccountStatus.ACTIVE) and string
            status = raw_status.name if hasattr(raw_status, 'name') else str(raw_status)

            # Cache account number for paper_account_id on trade records
            self._account_number = str(getattr(account, "account_number", "") or "")

            logger.info(
                "AlpacaBroker[%s] connected: paper=%s feed=%s equity=$%.2f status=%s",
                self._mode.upper(), self._paper, self._feed, equity, status,
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

    def verify_shorting_enabled(self) -> bool:
        """Check if the Alpaca account has shorting enabled.

        Returns:
            True if the account allows short selling, False otherwise.
            Also returns False if the check fails (e.g. not connected).
        """
        try:
            account = self._trade_client.get_account()
            enabled = getattr(account, 'shorting_enabled', False)
            if not enabled:
                logger.warning("Alpaca account does not have shorting enabled")
            return bool(enabled)
        except Exception as e:
            logger.error(f"Failed to check shorting status: {e}")
            return False

    # ── Account ────────────────────────────────────────────────

    def get_account_info(self) -> AccountInfo:
        """Query account equity, cash, and buying power from Alpaca.

        Returns:
            AccountInfo populated from Alpaca's /v2/account endpoint.
        """
        self._require_connected()

        try:
            account = self._broker_call(self._trade_client.get_account)
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

    def get_pdt_status(self) -> dict:
        """Query Alpaca account for pattern-day-trading status.

        Returns dict with:
          - daytrade_count: int  — number of round-trip day-trades in last 5 business days
          - pattern_day_trader: bool — Alpaca-flagged PDT account
          - equity: float  — account equity (used to determine $25k threshold)
          - blocked: bool  — True if a new BUY would likely hit 40310100
          - reason: str    — human-readable reason if blocked

        A new opening BUY in a sub-$25k account that already has 3+ day-trades
        in the rolling 5-business-day window is at high risk of broker-level
        PDT rejection because the next same-day exit would complete a 4th
        day-trade.  Returning blocked=True lets callers skip the submit and
        avoid 40310100 error noise.
        """
        try:
            account = self._broker_call(self._trade_client.get_account)
        except Exception as exc:
            logger.warning("get_pdt_status: account fetch failed (%s) — fail-open", exc)
            return {
                "daytrade_count": 0,
                "pattern_day_trader": False,
                "equity": 0.0,
                "blocked": False,
                "reason": f"account_fetch_failed: {exc}",
            }

        daytrade_count = int(getattr(account, "daytrade_count", 0) or 0)
        pattern_day_trader = bool(getattr(account, "pattern_day_trader", False))
        equity = float(getattr(account, "equity", 0) or 0)

        # PDT threshold: <$25k equity AND daytrade_count >= 3 means the NEXT
        # round-trip would trigger the rule.  Block new opening BUYs preemptively.
        blocked = equity < 25_000.0 and daytrade_count >= 3
        reason = ""
        if blocked:
            reason = (
                f"pdt_preempt: equity=${equity:.0f} < $25k AND "
                f"daytrade_count={daytrade_count} >= 3"
            )

        return {
            "daytrade_count": daytrade_count,
            "pattern_day_trader": pattern_day_trader,
            "equity": equity,
            "blocked": blocked,
            "reason": reason,
        }

    def get_positions(self) -> list[PositionInfo]:
        """Return all open positions from Alpaca, enriched with Tiingo prices.

        Alpaca's position ``current_price`` can be stale or incorrect
        (observed 8%+ deviations).  We fetch authoritative prices from
        Tiingo IEX and recalculate PnL fields.  Falls back to Alpaca
        prices if Tiingo is unavailable.

        Returns:
            List of PositionInfo in Atlas format.
        """
        self._require_connected()

        try:
            raw_positions = self._broker_call(self._trade_client.get_all_positions)
        except Exception as e:
            logger.error("get_all_positions failed: %s", e, exc_info=True)
            return []

        # ── Fetch authoritative prices from Tiingo ───────────────
        tiingo_prices: dict[str, float] = {}
        tickers_for_tiingo = []
        for pos in (raw_positions or []):
            symbol = str(getattr(pos, "symbol", ""))
            if symbol and not symbol.endswith(".AX"):
                tickers_for_tiingo.append(symbol)
        if tickers_for_tiingo:
            try:
                from atlas.brokers.tiingo import get_tiingo_client
                tiingo = get_tiingo_client()
                if tiingo is not None:
                    quotes = tiingo.get_quotes(tickers_for_tiingo)
                    for t, q in quotes.items():
                        price = q.get("price", 0)
                        if price and float(price) > 0:
                            tiingo_prices[t.upper()] = float(price)
                    logger.debug(
                        "get_positions: Tiingo enrichment for %d/%d tickers",
                        len(tiingo_prices), len(tickers_for_tiingo),
                    )
            except Exception as e:
                logger.warning("get_positions: Tiingo price fetch failed (using Alpaca): %s", e)

        positions = []
        for pos in (raw_positions or []):
            symbol = str(getattr(pos, "symbol", ""))
            atlas_ticker = mapper.to_atlas(symbol)

            qty_raw = getattr(pos, "qty", 0) or 0
            qty = int(float(qty_raw))
            if qty == 0:
                continue

            avg_entry = float(getattr(pos, "avg_entry_price", 0) or 0)
            alpaca_price = float(getattr(pos, "current_price", 0) or 0)
            cost_basis = float(getattr(pos, "cost_basis", 0) or 0)

            # Price arbiter: Alpaca is authority; halts ticker on >halt_pct spread
            from atlas.brokers.price_arbiter import arbitrate
            tiingo_price = tiingo_prices.get(symbol.upper(), 0)
            current_price = arbitrate(atlas_ticker, tiingo_price, alpaca_price)

            # Recalculate PnL from authoritative price
            market_value = round(current_price * qty, 2)
            unrealized_pl = round(market_value - cost_basis, 2)
            unrealized_plpc_pct = round(
                (unrealized_pl / cost_basis * 100) if cost_basis > 0 else 0, 2
            )

            positions.append(PositionInfo(
                ticker=atlas_ticker,
                entry_price=round(avg_entry, 4),
                shares=qty,
                current_price=round(current_price, 4),
                market_value=market_value,
                unrealized_pnl=unrealized_pl,
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
        tif_str = kwargs.pop("tif", "") or self._tif
        tif = _map_tif(tif_str)
        # Always append a UUID suffix to guarantee uniqueness across multiple
        # orders with the same strategy/remark on the same day.
        _remark_slug = remark[:10] if remark else "ord"
        client_id = f"atlas_{_remark_slug}_{uuid.uuid4().hex[:8]}"

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
                stop_loss_price=kwargs.get("stop_loss_price"),
                take_profit_price=kwargs.get("take_profit_price"),
            )
        except ValueError as e:
            logger.error("build_order_request failed for %s: %s", ticker, e)
            return OrderResult(
                success=False, ticker=ticker, side=side,
                status=OrderStatus.FAILED, requested_qty=qty,
                requested_price=price, message=str(e),
            )

        # ── PDT backoff: pre-check (BUY and SELL) ────────────────────────────
        # If the ticker was denied earlier today (40310100), block the submit
        # immediately rather than burning another API round-trip and error log.
        # Applies to BOTH sides: a same-day BUY after a PDT-denied SELL (or
        # vice versa) will hit the same broker-level rejection.
        if _is_pdt_deferred_new(ticker):
            logger.info(
                "pdt_skip: %s %s — pre-check, PDT deferred until RTH close "
                "(skipping submit_order)", ticker, side.value,
            )
            return OrderResult(
                success=False, ticker=ticker, side=side,
                status=OrderStatus.FAILED, requested_qty=qty,
                requested_price=price,
                message=f"pdt_deferred: {_PDT_ERROR_CODE}",
            )

        # ── PDT account-level pre-check (BUY only) ────────────────────────────
        # Sub-$25k accounts with daytrade_count >= 3 are at high risk of broker-
        # level rejection on the NEXT opening BUY because a same-day exit would
        # complete a 4th day-trade in the rolling 5-business-day window.
        # Skip the submit and record the ticker as deferred so subsequent
        # cycles (sync_protective_orders, intraday_monitor) also skip it.
        if side == OrderSide.BUY:
            pdt_status = self.get_pdt_status()
            if pdt_status["blocked"]:
                logger.warning(
                    "pdt_preempt: %s BUY blocked — %s (skipping submit_order)",
                    ticker, pdt_status["reason"],
                )
                _set_pdt_deferred_new(ticker, _pdt_rth_close())
                return OrderResult(
                    success=False, ticker=ticker, side=side,
                    status=OrderStatus.FAILED, requested_qty=qty,
                    requested_price=price,
                    message=f"pdt_preempt: {pdt_status['reason']}",
                )

        try:
            order = self._broker_call(self._trade_client.submit_order, order_data)
        except Exception as e:
            error_msg = str(e)
            if _is_pdt_error(error_msg):
                # Record deferral so next cycle skips the submit entirely.
                _set_pdt_deferred_new(ticker, _pdt_rth_close())
                logger.warning(
                    "pdt_deferred: ticker=%s until=21:00 UTC (auto-recorded)", ticker,
                )
            else:
                logger.error("submit_order failed for %s: %s", ticker, e, exc_info=True)
            return OrderResult(
                success=False, ticker=ticker, side=side,
                status=OrderStatus.FAILED, requested_qty=qty,
                requested_price=price, message=error_msg,
            )

        result = _order_to_result(order, ticker, side)
        logger.info(
            "Order submitted: %s %s → id=%s status=%s",
            alpaca_symbol, side.value, result.order_id, result.status.value,
        )
        return result

    def cancel_order(self, order_id: str) -> OrderResult:
        """Cancel a specific order by its Alpaca order ID.

        Idempotent: treats Alpaca code 42210000 ("order pending cancel") as
        success — the cancel WILL settle, we just hit a race where a prior
        cancel call is still in flight at Alpaca's side. Logging at INFO,
        not ERROR, to avoid filling errors table with benign races
        (see errors table ids 12-17, 23, db).

        Args:
            order_id: Alpaca order UUID (returned as order_id by place_order).

        Returns:
            OrderResult indicating success or failure.
        """
        self._require_connected()

        try:
            self._broker_call(self._trade_client.cancel_order_by_id, order_id)
            logger.info("Order cancelled: %s", order_id)
            return OrderResult(
                success=True, order_id=order_id,
                status=OrderStatus.CANCELLED, message="Cancelled",
            )
        except Exception as e:
            err_str = str(e)
            # Idempotency: 42210000 = "order pending cancel" — benign race
            # where a prior cancel call is still being processed. The cancel
            # will settle; treat as success and downgrade log severity.
            if "42210000" in err_str or "order pending cancel" in err_str.lower():
                logger.info(
                    "cancel_order: %s already pending cancel (benign race, treating as success)",
                    order_id,
                )
                return OrderResult(
                    success=True, order_id=order_id,
                    status=OrderStatus.CANCELLED, message="Already pending cancel (idempotent)",
                )
            logger.error("cancel_order failed for %s: %s", order_id, e, exc_info=True)
            return OrderResult(
                success=False, order_id=order_id,
                status=OrderStatus.FAILED, message=err_str,
            )


    def _wait_for_cancel_confirmed(
        self,
        order_id: str,
        timeout_s: float | None = None,
        poll_interval_s: float = 0.25,
    ) -> bool:
        """Poll until Alpaca confirms the cancel is terminal.

        Phase 2C: mirrors _wait_for_cancel_confirm in sync_protective_orders.py
        for the five broker.py internal cancel-then-place sites.  Prevents the
        40310000 insufficient-qty race where a replacement order is placed before
        Alpaca has fully settled the cancellation.

        Args:
            order_id:         Order ID that was just cancelled.
            timeout_s:        Max seconds to wait.  Defaults to env var
                              ATLAS_BROKER_CANCEL_CONFIRM_TIMEOUT_SEC (default 10.0).
            poll_interval_s:  Seconds between polls (default 0.25 s).

        Returns:
            True  -- cancel confirmed: status is CANCELLED or FAILED.
            False -- order FILLED (race lost) OR timeout elapsed.
        """
        if timeout_s is None:
            timeout_s = float(
                os.environ.get("ATLAS_BROKER_CANCEL_CONFIRM_TIMEOUT_SEC", "10.0")
            )

        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            try:
                result = self.get_order_status(order_id)
            except Exception as poll_exc:
                logger.warning(
                    "_wait_for_cancel_confirmed: get_order_status(%s) failed: %s -- retrying",
                    order_id, poll_exc,
                )
                time.sleep(poll_interval_s)
                continue

            status = result.status
            if status in (OrderStatus.CANCELLED, OrderStatus.FAILED):
                logger.debug(
                    "_wait_for_cancel_confirmed: order %s confirmed terminal (status=%s)",
                    order_id, status.value,
                )
                return True
            if status == OrderStatus.FILLED:
                logger.warning(
                    "_wait_for_cancel_confirmed: order %s FILLED before cancel confirmed"
                    " -- race lost (position may have exited)",
                    order_id,
                )
                return False

            # PENDING / SUBMITTED / UNKNOWN -- still settling; wait and retry
            time.sleep(poll_interval_s)

        logger.error(
            "_wait_for_cancel_confirmed: order %s did not reach terminal status within"
            " %.1fs -- refusing to place replacement (would risk duplicate stops)",
            order_id, timeout_s,
        )
        return False

    def cancel_all_orders(self) -> list[OrderResult]:
        """Cancel all open orders.

        Alpaca returns a list of CancelOrderResponse with status per order.
        We map each to an OrderResult.

        Returns:
            List of OrderResult (one per cancelled order).
        """
        self._require_connected()

        try:
            responses = self._broker_call(self._trade_client.cancel_orders)
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
        """Return all currently open/working orders, including OCO legs.

        Queries Alpaca with status=open and nested=True so that OCO/OTO
        child legs (which have status=HELD) are included.  Without this,
        the stop leg of an OCO pair is invisible because Alpaca only
        returns the active limit leg at the top level.

        Returns:
            List of OrderResult for open orders (including HELD legs).
        """
        self._require_connected()

        try:
            req = GetOrdersRequest(status=QueryOrderStatus.OPEN, nested=True)
            orders = self._broker_call(self._trade_client.get_orders, req)
        except Exception as e:
            logger.error("get_orders(open) failed: %s", e, exc_info=True)
            return []

        # Flatten: include OCO/OTO child legs that sit inside .legs
        # (e.g. a HELD stop sell inside an OCO limit sell parent).
        all_orders: list = []
        for order in (orders or []):
            all_orders.append(order)
            if hasattr(order, "legs") and order.legs:
                all_orders.extend(order.legs)

        results = []
        for order in all_orders:
            symbol = str(getattr(order, "symbol", ""))
            atlas_ticker = mapper.to_atlas(symbol)
            side_raw = getattr(order, "side", None)
            side_str = str(side_raw.value if hasattr(side_raw, "value") else side_raw).lower()
            side = OrderSide.BUY if side_str == "buy" else OrderSide.SELL
            results.append(_order_to_result(order, atlas_ticker, side))

        logger.debug("get_open_orders: %d open orders (incl. OCO legs)", len(results))
        return results

    # TODO: Extract to protective_orders.py (~400 lines)
    def sync_all_protective_orders(
        self,
        positions: list,
        plan: Optional[dict] = None,
        *,
        trade_date: str = "",
        dry_run: bool = False,
    ) -> dict:
        """Sync protective SL and TP orders for all live positions.

        Idempotent — existing stop/stop_limit/trailing_stop and limit sell
        orders are detected and skipped.  Only missing orders are placed.
        All protective orders use GTC (Good Till Cancelled) time-in-force.

        For each position:
          If strategy provides take_profit:
            - SL: STOP SELL GTC at stop_price (from plan or 5% fallback)
            - TP: LIMIT SELL GTC at take_profit price
          If no take_profit (or current price already past TP):
            - TRAILING_STOP SELL GTC with trail = entry_price - stop_price
              (acts as combined SL + profit capture; ratchets up with price)

        Args:
            positions:   List of PositionInfo objects (fetched from Alpaca if empty).
            plan:        Today's trade plan dict (for stop_price / take_profit lookups).
                         Accepted shapes: {ticker: {stop_price: X, take_profit: Y}},
                         {'entries': [{ticker, stop_price, take_profit}, ...]}, or a plain list.
            trade_date:  YYYY-MM-DD (informational; defaults to today if empty).
            dry_run:     Log intent but do NOT send orders.

        Returns:
            Summary dict: {"sl_placed": N, "sl_already_exists": M,
                           "tp_placed": N, "tp_already_exists": M,
                           "errors": E, "pdt_deferred": P,
                           "per_ticker": {ticker: {action, ...}}}
        """
        self._require_connected()

        sl_placed = 0
        sl_already_exists = 0
        tp_placed = 0
        tp_already_exists = 0
        errors = 0
        pdt_deferred = 0
        per_ticker: dict = {}

        # Fetch positions from Alpaca if caller did not supply them
        if not positions:
            positions = self.get_positions()

        if not positions:
            logger.info("sync_all_protective_orders: no open positions — nothing to sync")
            return {
                "sl_placed": 0, "sl_already_exists": 0,
                "tp_placed": 0, "tp_already_exists": 0,
                "errors": 0, "per_ticker": {},
            }

        # Fetch all open orders directly so we can inspect order_type.
        # nested=True includes OCO/OTO child legs that are otherwise invisible.
        try:
            req = GetOrdersRequest(status=QueryOrderStatus.OPEN, nested=True)
            open_orders_raw = self._broker_call(self._trade_client.get_orders, req)
        except Exception as e:
            logger.error("sync_all_protective_orders: get_orders failed: %s", e, exc_info=True)
            return {
                "sl_placed": 0, "sl_already_exists": 0,
                "tp_placed": 0, "tp_already_exists": 0,
                "errors": len(positions), "per_ticker": {},
            }

        # Flatten order list to include OCO/OTO child legs.
        # E.g. an OCO LIMIT SELL (TP) may have a child STOP SELL (SL)
        # with status=HELD that only appears in the parent's .legs list.
        all_scan_orders: list = []
        for order in (open_orders_raw or []):
            all_scan_orders.append(order)
            if hasattr(order, "legs") and order.legs:
                all_scan_orders.extend(order.legs)

        # Strategy trailing stop ATR multipliers for Path A stop tightening
        _TRAILING_MULTS = {
            "trend_following": 2.5,
            "momentum_breakout": 4.0,
            "sector_rotation": 3.5,
        }

        # Build dicts of tickers with existing SL and TP orders
        # tickers_with_stop: {ticker: {"price": X, "order_id": Y}}
        # tickers_with_tp: {ticker: {"price": X, "order_id": Y}}
        tickers_with_stop: dict[str, dict] = {}
        tickers_with_tp: dict[str, dict] = {}
        for order in all_scan_orders:
            order_type_raw = getattr(order, "order_type", None)
            order_type_str = str(
                order_type_raw.value if hasattr(order_type_raw, "value") else order_type_raw
            ).lower()
            side_raw = getattr(order, "side", None)
            side_str = str(
                side_raw.value if hasattr(side_raw, "value") else side_raw
            ).lower()
            symbol = str(getattr(order, "symbol", ""))
            atlas_ticker = mapper.to_atlas(symbol)

            if side_str == "sell":
                if order_type_str in ("stop", "stop_limit", "trailing_stop"):
                    stop_price = float(getattr(order, "stop_price", 0) or 0)
                    order_id = str(getattr(order, "id", ""))
                    tickers_with_stop[atlas_ticker] = {"price": stop_price, "order_id": order_id, "type": order_type_str}
                    logger.debug(
                        "sync_protective: found existing stop SELL for %s (type=%s, price=%.2f, id=%s)",
                        atlas_ticker, order_type_str, stop_price, order_id,
                    )
                elif order_type_str == "limit":
                    limit_price = float(getattr(order, "limit_price", 0) or 0)
                    order_id = str(getattr(order, "id", ""))
                    tickers_with_tp[atlas_ticker] = {"price": limit_price, "order_id": order_id}
                    logger.debug(
                        "sync_protective: found existing LIMIT SELL for %s @ %.2f (TP candidate, id=%s)",
                        atlas_ticker, limit_price, order_id,
                    )

        # Normalise plan into {ticker: entry_dict} for stop/tp-price lookup
        plan_by_ticker: dict = {}
        if plan:
            if isinstance(plan, list):
                for e in plan:
                    t = e.get("ticker", "")
                    if t:
                        plan_by_ticker[t] = e
            elif isinstance(plan, dict):
                entries = (plan.get("entries")
                          or plan.get("plan_entries")
                          or plan.get("proposed_entries")
                          or [])
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

        # Sort by position value descending so highest-risk positions
        # consume the limited PDT day-trade slots first.
        positions = sorted(
            positions,
            key=lambda p: p.entry_price * p.shares,
            reverse=True,
        )

        for pos in positions:
            ticker = pos.ticker
            ticker_result: dict = {}
            try:
                # ── PDT backoff: pre-check ────────────────────────────────────────
                # Skip any ticker that was PDT-denied earlier today so we don't
                # hammer Alpaca with repeated 40310100 rejections every 15 min.
                if _is_pdt_deferred_new(ticker):
                    logger.info(
                        "pdt_skip: %s — pre-check in sync_all_protective_orders, "
                        "PDT deferred until RTH close", ticker,
                    )
                    pdt_deferred += 1
                    ticker_result["sl_action"] = "pdt_deferred"
                    ticker_result["tp_action"] = "pdt_deferred"
                    ticker_result["action"] = "pdt_deferred"
                    ticker_result["qty"] = pos.shares
                    per_ticker[ticker] = ticker_result
                    continue  # skip to next position

                plan_entry = plan_by_ticker.get(ticker, {})

                # ── Resolve stop price (needed for all paths) ──
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
                ticker_result["stop_price"] = stop_price

                # ── Resolve take-profit ────────────────────────
                take_profit = (
                    plan_entry.get("take_profit")
                    or plan_entry.get("tp_price")
                    or getattr(pos, "take_profit", None)
                )
                has_tp = take_profit is not None and float(take_profit) > 0

                # If current price already exceeds TP, TP is stale → use trailing stop
                if has_tp and pos.current_price > float(take_profit) * 1.005:
                    logger.info(
                        "sync_protective: %s current $%.2f already past TP $%.2f — "
                        "using trailing stop instead",
                        ticker, pos.current_price, float(take_profit),
                    )
                    has_tp = False

                # ── Honor broker's existing TP when plan lacks one ───
                # If the plan has no TP but the broker still has an active TP limit
                # (typically the live half of a prior OCO), use the broker's TP
                # price as the source of truth.  This prevents Path B from canceling
                # the broker's stop leg and accidentally dropping the TP leg via
                # Alpaca's OCO one-cancels-other behaviour.  See FIX-OCO-TPDROP-001.
                if not has_tp and ticker in tickers_with_tp:
                    broker_tp_price = float(tickers_with_tp[ticker].get("price") or 0)
                    if broker_tp_price > 0 and pos.current_price < broker_tp_price * 1.005:
                        take_profit = broker_tp_price
                        has_tp = True
                        logger.info(
                            "sync_protective: %s plan lacks TP but broker has active "
                            "limit @ $%.2f — honoring broker TP to preserve OCO bracket",
                            ticker, broker_tp_price,
                        )
                    elif broker_tp_price > 0:
                        # Broker's TP is already past current price → stale, leave to Path B
                        logger.info(
                            "sync_protective: %s broker TP $%.2f is stale "
                            "(current $%.2f past it) — falling through to Path B",
                            ticker, broker_tp_price, pos.current_price,
                        )

                has_existing_stop = ticker in tickers_with_stop

                if has_tp:
                    # ═══ Path A: Strategy has TP → OCO order (SL + TP, one-cancels-other) ═══
                    take_profit = round(float(take_profit), 2)
                    ticker_result["take_profit"] = take_profit

                    # Check if both SL and TP already exist
                    has_existing_tp = ticker in tickers_with_tp and _prices_match(
                        tickers_with_tp[ticker]["price"], take_profit
                    )

                    if has_existing_stop and has_existing_tp:
                        # Both already exist → check if we should tighten the stop
                        existing_stop_price = tickers_with_stop[ticker]["price"]
                        existing_stop_order_id = tickers_with_stop[ticker]["order_id"]
                        existing_tp_price = tickers_with_tp[ticker]["price"]
                        existing_tp_order_id = tickers_with_tp[ticker]["order_id"]
                        
                        # Get strategy's trailing stop multiplier
                        strategy_name = plan_entry.get("strategy", "")
                        trailing_mult = _TRAILING_MULTS.get(strategy_name, 0)
                        
                        should_tighten = False
                        ideal_stop = existing_stop_price
                        
                        if trailing_mult > 0 and pos.current_price > pos.entry_price * 1.005:
                            # Position is profitable — compute trailing stop
                            # Derive ATR from original stop distance (conservative estimate)
                            atr_stop_mult = plan_entry.get("atr_stop_mult", 2.0)
                            if atr_stop_mult <= 0:
                                atr_stop_mult = 2.0
                            estimated_atr = (pos.entry_price - stop_price) / atr_stop_mult
                            
                            # Compute ideal trailing stop from current price
                            # (In production, would track highest_price; here we use current_price conservatively)
                            ideal_stop = pos.current_price - (trailing_mult * estimated_atr)
                            
                            # Only tighten if:
                            # 1. New stop is meaningfully higher (1% or $0.50, whichever is larger)
                            # 2. New stop doesn't exceed TP (must stay below)
                            min_improvement = max(existing_stop_price * 0.01, 0.50)
                            if (ideal_stop > existing_stop_price + min_improvement 
                                and ideal_stop < take_profit * 0.995):
                                should_tighten = True
                        
                        if should_tighten:
                            # Tighten the stop by canceling existing OCO and placing new one
                            if dry_run:
                                logger.info(
                                    "sync_protective [DRY RUN]: would tighten %s stop from $%.2f to $%.2f "
                                    "(trailing stop ratchet, strategy=%s)",
                                    ticker, existing_stop_price, ideal_stop, strategy_name,
                                )
                                ticker_result["sl_action"] = "dry_run_tightened"
                                ticker_result["sl_tightened_from"] = existing_stop_price
                                ticker_result["sl_tightened_to"] = ideal_stop
                            else:
                                # Cancel existing OCO (both legs)
                                cancel_success = True
                                try:
                                    # Site 1 of 5 -- Phase 2C: cancel stop + confirm settled
                                    cancel_result = self.cancel_order(existing_stop_order_id)
                                    if not cancel_result.success:
                                        logger.warning(
                                            "sync_protective: failed to cancel stop order %s for %s: %s",
                                            existing_stop_order_id, ticker, cancel_result.error,
                                        )
                                        cancel_success = False
                                    else:
                                        if not self._wait_for_cancel_confirmed(
                                            existing_stop_order_id, timeout_s=10.0
                                        ):
                                            logger.warning(
                                                "sync_protective: cancel-confirm timeout for stop order %s"
                                                " on %s -- aborting tightened OCO to avoid duplicate stops",
                                                existing_stop_order_id, ticker,
                                            )
                                            cancel_success = False
                                        else:
                                            logger.info(
                                                "sync_protective: canceled existing stop order %s for %s (tightening)",
                                                existing_stop_order_id, ticker,
                                            )

                                    # Site 2 of 5 -- Phase 2C: cancel TP + confirm settled
                                    if cancel_success:
                                        cancel_result = self.cancel_order(existing_tp_order_id)
                                        if not cancel_result.success:
                                            logger.warning(
                                                "sync_protective: failed to cancel TP order %s for %s: %s",
                                                existing_tp_order_id, ticker, cancel_result.error,
                                            )
                                            cancel_success = False
                                        else:
                                            if not self._wait_for_cancel_confirmed(
                                                existing_tp_order_id, timeout_s=10.0
                                            ):
                                                logger.warning(
                                                    "sync_protective: cancel-confirm timeout for TP order %s"
                                                    " on %s -- aborting tightened OCO to avoid duplicate stops",
                                                    existing_tp_order_id, ticker,
                                                )
                                                cancel_success = False
                                            else:
                                                logger.info(
                                                    "sync_protective: canceled existing TP order %s for %s (tightening)",
                                                    existing_tp_order_id, ticker,
                                                )
                                except Exception as cancel_err:
                                    logger.error(
                                        "sync_protective: error canceling orders for %s: %s",
                                        ticker, cancel_err, exc_info=True,
                                    )
                                    cancel_success = False
                                
                                if cancel_success:
                                    # Place new OCO with tightened stop
                                    # Alpaca OCO requires a LIMIT order as parent (TP leg),
                                    # with both take_profit and stop_loss parameters.
                                    # PDT pre-check for tightened OCO
                                    if _is_pdt_deferred_new(ticker):
                                        logger.info(
                                            "pdt_skip: %s — pre-check, skipping tightened OCO",
                                            ticker,
                                        )
                                        pdt_deferred += 1
                                        ticker_result["sl_action"] = "pdt_deferred"
                                        ticker_result["tp_action"] = "pdt_deferred"
                                    else:
                                        try:
                                            request = LimitOrderRequest(
                                                symbol=ticker,
                                                qty=pos.shares,
                                                side=AlpacaSide.SELL,
                                                limit_price=take_profit,
                                                order_class=OrderClass.OCO,
                                                take_profit=TakeProfitRequest(limit_price=take_profit),
                                                stop_loss=StopLossRequest(stop_price=round(ideal_stop, 2)),
                                                time_in_force=TimeInForce.GTC,
                                            )
                                            order = self._broker_call(self._trade_client.submit_order, request)

                                            logger.info(
                                                "sync_protective: tightened %s stop from $%.2f to $%.2f "
                                                "(trailing stop ratchet, strategy=%s) → OCO id=%s",
                                                ticker, existing_stop_price, ideal_stop, strategy_name, order.id,
                                            )
                                            ticker_result["sl_action"] = "tightened"
                                            ticker_result["sl_tightened_from"] = existing_stop_price
                                            ticker_result["sl_tightened_to"] = round(ideal_stop, 2)
                                            ticker_result["oco_order_id"] = str(order.id)
                                            sl_placed += 1
                                            tp_placed += 1
                                        except Exception as oco_err:
                                            _oco_err_msg = str(oco_err)
                                            if _is_pdt_error(_oco_err_msg):
                                                _set_pdt_deferred_new(ticker, _pdt_rth_close())
                                                logger.warning(
                                                    "sync_protective: tightened OCO deferred for %s — PDT",
                                                    ticker,
                                                )
                                                pdt_deferred += 1
                                                ticker_result["sl_action"] = "pdt_deferred"
                                                ticker_result["tp_action"] = "pdt_deferred"
                                            else:
                                                logger.error(
                                                    "sync_protective: failed to place tightened OCO for %s: %s",
                                                    ticker, oco_err, exc_info=True,
                                                )
                                                ticker_result["sl_action"] = "error_tightening"
                                                ticker_result["sl_error"] = _oco_err_msg
                                                errors += 1
                                else:
                                    # Cancel failed — don't place new OCO
                                    logger.warning(
                                        "sync_protective: skipping tightening for %s — cancel failed",
                                        ticker,
                                    )
                                    ticker_result["sl_action"] = "cancel_failed"
                                    errors += 1
                        else:
                            # Both already exist and no tightening needed → skip
                            sl_already_exists += 1
                            tp_already_exists += 1
                            ticker_result["sl_action"] = "skipped"
                            ticker_result["sl_reason"] = "stop_exists"
                            ticker_result["tp_action"] = "skipped"
                            ticker_result["tp_reason"] = "tp_exists"
                            logger.debug(
                                "sync_protective: %s already has both SL @ %.2f and TP @ %.2f — skipping",
                                ticker, existing_stop_price, take_profit,
                            )
                    elif dry_run:
                        # Dry run: log what would happen
                        logger.info(
                            "sync_protective [DRY RUN]: would place OCO SELL %s "
                            "qty=%d stop=%.2f tp=%.2f (GTC)",
                            ticker, pos.shares, stop_price, take_profit,
                        )
                        sl_placed += 1
                        tp_placed += 1
                        ticker_result["sl_action"] = "dry_run_oco"
                        ticker_result["tp_action"] = "dry_run_oco"
                    else:
                        # Need to place OCO. First cancel any existing individual orders.
                        # Phase 2C: track ALL cancels confirmed before placing OCO
                        cancel_confirmed_all = True
                        canceled_orders = []
                        if has_existing_stop:
                            # Cancel existing stop orders for this ticker
                            # (includes trailing_stop which also holds shares)
                            stop_orders = [
                                o for o in all_scan_orders
                                if o.symbol == ticker and o.order_type in ("stop", "stop_limit", "trailing_stop")
                            ]
                            for order in stop_orders:
                                cancel_result = self.cancel_order(order.id)
                                if cancel_result.success:
                                    # Site 3 of 5 -- Phase 2C: confirm cancel settled before placing OCO
                                    if not self._wait_for_cancel_confirmed(
                                        order.id, timeout_s=10.0
                                    ):
                                        logger.warning(
                                            "sync_protective: cancel-confirm timeout for stop order %s"
                                            " on %s -- aborting OCO to avoid duplicate stops",
                                            order.id, ticker,
                                        )
                                        cancel_confirmed_all = False
                                    else:
                                        canceled_orders.append(f"stop:{order.id}")
                                        logger.info(
                                            "sync_protective: canceled existing stop order %s for %s",
                                            order.id, ticker,
                                        )

                        if ticker in tickers_with_tp:
                            # Cancel existing TP (limit sell) orders for this ticker
                            tp_orders = [
                                o for o in all_scan_orders
                                if o.symbol == ticker
                                and o.order_type == "limit"
                                and o.side == "sell"
                            ]
                            for order in tp_orders:
                                cancel_result = self.cancel_order(order.id)
                                if cancel_result.success:
                                    # Site 4 of 5 -- Phase 2C: confirm cancel settled before placing OCO
                                    if not self._wait_for_cancel_confirmed(
                                        order.id, timeout_s=10.0
                                    ):
                                        logger.warning(
                                            "sync_protective: cancel-confirm timeout for TP order %s"
                                            " on %s -- aborting OCO to avoid duplicate stops",
                                            order.id, ticker,
                                        )
                                        cancel_confirmed_all = False
                                    else:
                                        canceled_orders.append(f"tp:{order.id}")
                                        logger.info(
                                            "sync_protective: canceled existing TP order %s for %s",
                                            order.id, ticker,
                                        )

                        if not cancel_confirmed_all:
                            # cancel-confirm timeout -- skip OCO this cycle, retry next pass
                            logger.warning(
                                "sync_protective: skipping OCO for %s -- one or more cancel"
                                " confirmations timed out (will retry next cycle)",
                                ticker,
                            )
                            ticker_result["sl_action"] = "cancel_confirm_timeout"
                            ticker_result["tp_action"] = "cancel_confirm_timeout"
                            errors += 1

                        if cancel_confirmed_all:
                            # Now place OCO order with both legs.
                            # Alpaca OCO requires a LIMIT order as parent (TP leg),
                            # with both take_profit and stop_loss parameters.
                            try:
                                request = LimitOrderRequest(
                                    symbol=ticker,
                                    qty=pos.shares,
                                    side=AlpacaSide.SELL,
                                    limit_price=take_profit,
                                    order_class=OrderClass.OCO,
                                    take_profit=TakeProfitRequest(limit_price=take_profit),
                                    stop_loss=StopLossRequest(stop_price=stop_price),
                                    time_in_force=TimeInForce.GTC,
                                )
                                order = self._broker_call(self._trade_client.submit_order, request)

                                # Success
                                sl_placed += 1
                                tp_placed += 1
                                ticker_result["sl_action"] = "oco_placed"
                                ticker_result["tp_action"] = "oco_placed"
                                ticker_result["oco_order_id"] = str(order.id)
                                if canceled_orders:
                                    ticker_result["canceled_orders"] = canceled_orders
                                logger.info(
                                    "sync_protective: placed OCO SELL GTC %s qty=%d stop=%.2f tp=%.2f → id=%s",
                                    ticker, pos.shares, stop_price, take_profit, order.id,
                                )
                            except Exception as oco_err:
                                oco_error_msg = str(oco_err)

                                # Check if it's a PDT error
                                if _is_pdt_error(oco_error_msg):
                                    # Record deferral so next cycle's pre-check skips submit.
                                    _set_pdt_deferred_new(ticker, _pdt_rth_close())
                                    # OCO rejected due to PDT. The TP leg makes the
                                    # entire OCO look like a potential day trade.
                                    # Try placing JUST the SL as a fallback — a stop
                                    # well below market is less likely to trigger PDT
                                    # than the full OCO, and at minimum protects downside.
                                    logger.warning(
                                        "sync_protective: OCO deferred for %s — PDT. "
                                        "Attempting SL-only fallback.",
                                        ticker,
                                    )
                                    try:
                                        sl_result = self.place_order(
                                            ticker=ticker,
                                            side=OrderSide.SELL,
                                            qty=pos.shares,
                                            price=0.0,
                                            order_type=OrderType.STOP,
                                            stop_price=stop_price,
                                            remark="sync_sl_pdt_fallback",
                                            tif="gtc",
                                        )
                                        if sl_result.success:
                                            sl_placed += 1
                                            ticker_result["sl_action"] = "placed_pdt_fallback"
                                            ticker_result["sl_order_id"] = sl_result.order_id
                                            ticker_result["tp_action"] = "pdt_deferred"
                                            ticker_result["tp_message"] = "OCO PDT-rejected, SL placed alone"
                                            logger.info(
                                                "sync_protective: SL-only placed for %s stop=%.2f "
                                                "(TP deferred to next sync) → id=%s",
                                                ticker, stop_price, sl_result.order_id,
                                            )
                                        elif _is_pdt_error(sl_result.message):
                                            # Both OCO and standalone SL rejected by PDT
                                            pdt_deferred += 1
                                            ticker_result["sl_action"] = "pdt_deferred"
                                            ticker_result["tp_action"] = "pdt_deferred"
                                            ticker_result["sl_message"] = oco_error_msg
                                            logger.warning(
                                                "sync_protective: both OCO and SL deferred for %s "
                                                "— PDT (account < $25k, same-day entry). "
                                                "Orders will be placed at next pre-market sync.",
                                                ticker,
                                            )
                                        else:
                                            errors += 1
                                            ticker_result["sl_action"] = "error"
                                            ticker_result["tp_action"] = "pdt_deferred"
                                            ticker_result["sl_message"] = sl_result.message
                                            logger.error(
                                                "sync_protective: SL fallback also failed for %s: %s",
                                                ticker, sl_result.message,
                                            )
                                    except Exception as sl_err:
                                        pdt_deferred += 1
                                        ticker_result["sl_action"] = "pdt_deferred"
                                        ticker_result["tp_action"] = "pdt_deferred"
                                        ticker_result["sl_message"] = str(sl_err)
                                        logger.warning(
                                            "sync_protective: SL fallback exception for %s: %s — "
                                            "full deferral to next sync",
                                            ticker, sl_err,
                                        )
                                else:
                                    # OCO failed with non-PDT error. Fallback: try placing just SL
                                    logger.warning(
                                        "sync_protective: OCO order failed for %s: %s — falling back to SL-only",
                                        ticker, oco_error_msg,
                                    )

                                    try:
                                        sl_result = self.place_order(
                                            ticker=ticker,
                                            side=OrderSide.SELL,
                                            qty=pos.shares,
                                            price=0.0,
                                            order_type=OrderType.STOP,
                                            stop_price=stop_price,
                                            remark="sync_sl_fallback",
                                            tif="gtc",
                                        )
                                        if sl_result.success:
                                            sl_placed += 1
                                            ticker_result["sl_action"] = "placed_fallback"
                                            ticker_result["sl_order_id"] = sl_result.order_id
                                            ticker_result["tp_action"] = "skipped"
                                            ticker_result["tp_reason"] = "oco_failed_sl_fallback"
                                            ticker_result["oco_error"] = oco_error_msg
                                            logger.info(
                                                "sync_protective: fallback SL placed for %s stop=%.2f → id=%s",
                                                ticker, stop_price, sl_result.order_id,
                                            )
                                        else:
                                            errors += 1
                                            ticker_result["sl_action"] = "error"
                                            ticker_result["tp_action"] = "error"
                                            ticker_result["sl_message"] = sl_result.message
                                            ticker_result["oco_error"] = oco_error_msg
                                            logger.error(
                                                "sync_protective: OCO and fallback SL both failed for %s. "
                                                "OCO: %s, SL: %s",
                                                ticker, oco_error_msg, sl_result.message,
                                            )
                                    except Exception as fallback_err:
                                        errors += 1
                                        ticker_result["sl_action"] = "error"
                                        ticker_result["tp_action"] = "error"
                                        ticker_result["sl_message"] = str(fallback_err)
                                        ticker_result["oco_error"] = oco_error_msg
                                        logger.error(
                                            "sync_protective: OCO and fallback SL both failed for %s. "
                                            "OCO: %s, Fallback: %s",
                                            ticker, oco_error_msg, str(fallback_err),
                                        )

                else:
                    # ═══ Path B: No TP → trailing stop GTC (combined SL + profit capture) ═══
                    # Trail distance = entry - stop (same initial risk distance).
                    # As price rises, the trailing stop ratchets up automatically.
                    trail_distance = round(pos.entry_price - stop_price, 2)
                    if trail_distance <= 0:
                        trail_distance = round(pos.entry_price * 0.05, 2)
                    ticker_result["trail_distance"] = trail_distance

                    if has_existing_stop:
                        # Check if we should upgrade a stale static stop to
                        # a tighter trailing stop.  This handles positions that
                        # blew through TP while the OCO path was broken (the
                        # fallback SL is far too loose once price has moved).
                        existing_stop_price = tickers_with_stop[ticker]["price"]
                        existing_stop_type = tickers_with_stop[ticker].get("type", "stop")
                        trailing_would_be = round(pos.current_price - trail_distance, 2)
                        should_upgrade = (
                            existing_stop_type != "trailing_stop"  # don't replace trailing with trailing
                            and pos.current_price > pos.entry_price * 1.01   # profitable
                            and trailing_would_be > existing_stop_price * 1.02  # meaningfully tighter
                        )

                        if should_upgrade and not dry_run:
                            # Cancel existing static stop, place trailing stop
                            # Site 5 of 5 -- Phase 2C: cancel + confirm settled before placing
                            cancel_ok = False
                            try:
                                existing_order_id = tickers_with_stop[ticker]["order_id"]
                                cr = self.cancel_order(existing_order_id)
                                if cr.success:
                                    if self._wait_for_cancel_confirmed(
                                        existing_order_id, timeout_s=10.0
                                    ):
                                        cancel_ok = True
                                        logger.info(
                                            "sync_protective: canceled static stop %s for %s "
                                            "(upgrading to trailing stop, static=$%.2f"
                                            " trailing~$%.2f)",
                                            existing_order_id, ticker,
                                            existing_stop_price, trailing_would_be,
                                        )
                                    else:
                                        logger.warning(
                                            "sync_protective: cancel-confirm timeout for static stop %s"
                                            " on %s -- aborting trailing upgrade to avoid duplicate stops",
                                            existing_order_id, ticker,
                                        )
                            except Exception as ce:
                                logger.warning(
                                    "sync_protective: cancel failed for %s static stop: %s",
                                    ticker, ce,
                                )

                            if cancel_ok:
                                trail_result = self.place_order(
                                    ticker=ticker,
                                    side=OrderSide.SELL,
                                    qty=pos.shares,
                                    price=0.0,
                                    order_type=OrderType.TRAILING_STOP,
                                    remark="sync_trail_upgrade",
                                    tif="gtc",
                                    trail_price=trail_distance,
                                )
                                if trail_result.success:
                                    sl_placed += 1
                                    ticker_result["sl_action"] = "trailing_upgraded"
                                    ticker_result["sl_order_id"] = trail_result.order_id
                                    ticker_result["sl_upgraded_from"] = existing_stop_price
                                    ticker_result["sl_upgraded_to_trail"] = trail_distance
                                    ticker_result["tp_action"] = "trailing"
                                    ticker_result["tp_reason"] = "upgraded_to_trailing"
                                    logger.info(
                                        "sync_protective: upgraded %s from static stop $%.2f "
                                        "to trailing stop trail=$%.2f (≈$%.2f) → id=%s",
                                        ticker, existing_stop_price, trail_distance,
                                        trailing_would_be, trail_result.order_id,
                                    )
                                else:
                                    # Trailing failed — re-place static stop as safety net
                                    errors += 1
                                    ticker_result["sl_action"] = "error"
                                    ticker_result["sl_message"] = trail_result.message
                                    logger.error(
                                        "sync_protective: trailing upgrade failed for %s: %s "
                                        "— re-placing static stop",
                                        ticker, trail_result.message,
                                    )
                                    self.place_order(
                                        ticker=ticker,
                                        side=OrderSide.SELL,
                                        qty=pos.shares,
                                        price=0.0,
                                        order_type=OrderType.STOP,
                                        stop_price=existing_stop_price,
                                        remark="sync_sl_restore",
                                        tif="gtc",
                                    )
                            else:
                                sl_already_exists += 1
                                ticker_result["sl_action"] = "skipped"
                                ticker_result["sl_reason"] = "upgrade_cancel_failed"
                                ticker_result["tp_action"] = "skipped"
                                ticker_result["tp_reason"] = "trailing_stop_covers"
                        elif should_upgrade and dry_run:
                            logger.info(
                                "sync_protective [DRY RUN]: would upgrade %s from "
                                "static stop $%.2f to trailing stop trail=$%.2f (≈$%.2f)",
                                ticker, existing_stop_price, trail_distance, trailing_would_be,
                            )
                            sl_placed += 1
                            ticker_result["sl_action"] = "dry_run_trailing_upgrade"
                            ticker_result["tp_action"] = "trailing"
                            ticker_result["tp_reason"] = "would_upgrade_to_trailing"
                        else:
                            sl_already_exists += 1
                            ticker_result["sl_action"] = "skipped"
                            ticker_result["sl_reason"] = "stop_exists"
                            ticker_result["tp_action"] = "skipped"
                            ticker_result["tp_reason"] = "trailing_stop_covers"
                            logger.debug(
                                "sync_protective: %s already has stop order — "
                                "skipping trailing stop", ticker,
                            )
                    elif dry_run:
                        logger.info(
                            "sync_protective [DRY RUN]: would place TRAILING_STOP SELL %s "
                            "qty=%d trail=$%.2f (GTC, no TP → trailing)",
                            ticker, pos.shares, trail_distance,
                        )
                        sl_placed += 1
                        ticker_result["sl_action"] = "dry_run_trailing"
                        ticker_result["tp_action"] = "trailing"
                        ticker_result["tp_reason"] = "no_tp_using_trailing"
                    else:
                        trail_result = self.place_order(
                            ticker=ticker,
                            side=OrderSide.SELL,
                            qty=pos.shares,
                            price=0.0,
                            order_type=OrderType.TRAILING_STOP,
                            remark="sync_trail",
                            tif="gtc",
                            trail_price=trail_distance,
                        )
                        if trail_result.success:
                            sl_placed += 1
                            ticker_result["sl_action"] = "trailing_placed"
                            ticker_result["sl_order_id"] = trail_result.order_id
                            ticker_result["tp_action"] = "trailing"
                            ticker_result["tp_reason"] = "no_tp_using_trailing"
                            logger.info(
                                "sync_protective: placed TRAILING_STOP SELL GTC %s "
                                "qty=%d trail=$%.2f → id=%s",
                                ticker, pos.shares, trail_distance,
                                trail_result.order_id,
                            )
                        elif _is_pdt_error(trail_result.message):
                            pdt_deferred += 1
                            ticker_result["sl_action"] = "pdt_deferred"
                            ticker_result["sl_message"] = trail_result.message
                            ticker_result["tp_action"] = "pdt_deferred"
                            logger.warning(
                                "sync_protective: trailing stop deferred for %s — "
                                "PDT protection (same-day entry, account < $25k). "
                                "Stop will be placed at next pre-market sync.",
                                ticker,
                            )
                        else:
                            errors += 1
                            ticker_result["sl_action"] = "error"
                            ticker_result["sl_message"] = trail_result.message
                            ticker_result["tp_action"] = "error"
                            logger.error(
                                "sync_protective: trailing stop failed for %s: %s",
                                ticker, trail_result.message,
                            )

                # Build combined action summary
                ticker_result["action"] = _summarise_ticker_action(ticker_result)
                ticker_result["qty"] = pos.shares
                per_ticker[ticker] = ticker_result

            except Exception as e:
                errors += 1
                per_ticker[ticker] = {"action": "error", "message": str(e)}
                logger.error(
                    "sync_protective: unexpected error for %s: %s", ticker, e, exc_info=True
                )

        logger.info(
            "sync_all_protective_orders complete: sl_placed=%d sl_exists=%d "
            "tp_placed=%d tp_exists=%d errors=%d pdt_deferred=%d",
            sl_placed, sl_already_exists, tp_placed, tp_already_exists,
            errors, pdt_deferred,
        )
        return {
            "sl_placed": sl_placed,
            "sl_already_exists": sl_already_exists,
            "tp_placed": tp_placed,
            "tp_already_exists": tp_already_exists,
            "errors": errors,
            "pdt_deferred": pdt_deferred,
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
            order = self._broker_call(self._trade_client.get_order_by_id, order_id)
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

def _prices_match(price_a: float, price_b: float, tolerance: float = 0.005) -> bool:
    """Check if two prices match within a relative tolerance (default 0.5%).

    Used to detect if an existing LIMIT SELL order is effectively
    the same as the desired take-profit price, avoiding duplicate TP orders.
    """
    if price_a <= 0 or price_b <= 0:
        return False
    return abs(price_a - price_b) / max(price_a, price_b) <= tolerance


def _summarise_ticker_action(ticker_result: dict) -> str:
    """Build a combined action string from SL and TP sub-actions.

    Returns a human-readable summary like 'sl_placed+tp_placed',
    'trailing_placed', 'sl_skipped+tp_skipped', 'pdt_deferred', etc.
    """
    sl = ticker_result.get("sl_action", "unknown")
    tp = ticker_result.get("tp_action", "unknown")

    # PDT protection — regulatory deferral, not an error
    if sl == "pdt_deferred" or tp == "pdt_deferred":
        return "pdt_deferred"
    # If both errored, overall is 'error'
    if sl == "error" and tp == "error":
        return "error"
    # Trailing stop covers both SL and TP
    if "trailing" in sl:
        return "trailing_placed" if "placed" in sl else "trailing_exists"
    # If either placed something, reflect that
    if "placed" in sl or "placed" in tp:
        parts = []
        if "placed" in sl:
            parts.append("sl_placed")
        elif sl == "skipped":
            parts.append("sl_exists")
        if "placed" in tp:
            parts.append("tp_placed")
        elif tp == "skipped" and ticker_result.get("tp_reason") == "tp_exists":
            parts.append("tp_exists")
        return "+".join(parts) if parts else "placed"
    # Both skipped
    return "skipped"


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
    stop_loss_price: Optional[float] = None,
    take_profit_price: Optional[float] = None,
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
        limit_kwargs: dict = dict(
            symbol=symbol,
            qty=qty_param,
            notional=notional_param,
            side=side,
            time_in_force=tif,
            limit_price=round(price, 2),
            client_order_id=client_id,
            extended_hours=extended_hours,
        )
        # Native BRACKET order: stop_loss + take_profit attached atomically.
        # Activates child legs on parent fill in a single API call (no race window).
        if stop_loss_price and stop_loss_price > 0 and take_profit_price and take_profit_price > 0:
            limit_kwargs["order_class"] = OrderClass.BRACKET
            limit_kwargs["stop_loss"] = StopLossRequest(stop_price=round(stop_loss_price, 2))
            limit_kwargs["take_profit"] = TakeProfitRequest(limit_price=round(take_profit_price, 2))
            logger.info(
                "BRACKET: %s entry=$%.2f stop=$%.2f tp=$%.2f",
                symbol, price, stop_loss_price, take_profit_price,
            )
        elif stop_loss_price and stop_loss_price > 0:
            limit_kwargs["order_class"] = OrderClass.OTO
            limit_kwargs["stop_loss"] = StopLossRequest(stop_price=round(stop_loss_price, 2))
            logger.info("OTO: %s entry=$%.2f stop=$%.2f", symbol, price, stop_loss_price)
        return LimitOrderRequest(**limit_kwargs)

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
