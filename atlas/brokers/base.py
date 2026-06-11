"""Abstract broker interface for Atlas.

All brokers implement this ABC.
Atlas internals use yfinance ticker format throughout — conversion
happens inside each broker implementation at the boundary.
For ASX: .AX format. For US: bare tickers. For LSE: .L format.
"""

from __future__ import annotations

import enum
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger("atlas.broker")


# ═══════════════════════════════════════════════════════════════
# Data classes — broker-agnostic return types
# ═══════════════════════════════════════════════════════════════

class OrderStatus(enum.Enum):
    """Unified order status across brokers."""
    PENDING = "PENDING"
    SUBMITTED = "SUBMITTED"
    PARTIAL_FILLED = "PARTIAL_FILLED"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"


class OrderSide(enum.Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(enum.Enum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    STOP = "STOP"
    STOP_LIMIT = "STOP_LIMIT"
    TRAILING_STOP = "TRAILING_STOP"


@dataclass
class OrderResult:
    """Returned after placing, modifying, or cancelling an order."""
    success: bool
    order_id: str = ""
    ticker: str = ""                # .AX format
    side: OrderSide = OrderSide.BUY
    status: OrderStatus = OrderStatus.UNKNOWN
    requested_qty: int = 0
    filled_qty: int = 0
    requested_price: float = 0.0
    fill_price: float = 0.0
    commission: float = 0.0
    message: str = ""
    raw: dict = field(default_factory=dict)   # broker-specific payload


@dataclass
class PositionInfo:
    """A single open position."""
    ticker: str                     # .AX format
    strategy: str = ""
    entry_date: str = ""
    entry_price: float = 0.0
    shares: int = 0
    current_price: float = 0.0
    market_value: float = 0.0
    unrealized_pnl: float = 0.0
    unrealized_pnl_pct: float = 0.0
    stop_price: float = 0.0
    take_profit: Optional[float] = None
    cost_basis: float = 0.0         # total cost including commission
    sector: str = "Unknown"
    today_pnl: float = 0.0         # today's P&L from broker (resets pre-session)
    currency: str = ""              # native currency of this position (USD, AUD, etc.)


@dataclass
class AccountInfo:
    """Account-level summary."""
    equity: float = 0.0             # net asset value
    cash: float = 0.0               # available cash
    market_value: float = 0.0       # total position value
    buying_power: float = 0.0
    total_pnl: float = 0.0
    total_pnl_pct: float = 0.0
    num_positions: int = 0
    currency: str = "AUD"
    market_id: str = ""             # market this account trades
    halted: bool = False
    halt_reason: str = ""


@dataclass
class DealInfo:
    """A single executed fill / deal."""
    order_id: str = ""
    ticker: str = ""                # .AX format
    side: OrderSide = OrderSide.BUY
    qty: int = 0
    price: float = 0.0
    commission: float = 0.0
    deal_time: str = ""
    raw: dict = field(default_factory=dict)


@dataclass
class OrderFeeInfo:
    """Fee breakdown for a single order."""
    order_id: str = ""
    total_fee: float = 0.0
    fee_details: list = field(default_factory=list)  # [(name, amount), ...]
    raw: dict = field(default_factory=dict)


@dataclass
class MarketStateInfo:
    """Market open/close status for a ticker."""
    ticker: str = ""
    market_state: str = ""          # MORNING, AFTERNOON, OVERNIGHT, REST, etc.
    raw: dict = field(default_factory=dict)


@dataclass
class TradingDayInfo:
    """A single trading day."""
    date: str = ""                  # YYYY-MM-DD
    trade_date_type: str = ""       # WHOLE, MORNING, AFTERNOON


@dataclass
class SlippageReport:
    """Slippage analysis for a single order/deal."""
    order_id: str = ""
    ticker: str = ""
    side: str = ""
    requested_price: float = 0.0
    fill_price: float = 0.0
    slippage_abs: float = 0.0       # fill - requested (positive = worse for buyer)
    slippage_pct: float = 0.0       # slippage as % of requested price
    qty: int = 0
    slippage_cost: float = 0.0      # slippage_abs * qty


# ═══════════════════════════════════════════════════════════════
# Abstract Broker
# ═══════════════════════════════════════════════════════════════

class BrokerAdapter(ABC):
    """Unified interface for all broker implementations.

    All tickers passed in and returned use .AX format.
    Broker implementations handle any format conversion internally.
    """

    def __init__(self, config: dict):
        self.config = config
        self._connected = False

    @property
    def name(self) -> str:
        """Human-readable broker name."""
        return self.__class__.__name__

    @property
    def is_live(self) -> bool:
        """Whether this broker executes real orders."""
        return False

    @property
    def is_connected(self) -> bool:
        return self._connected

    # ── Lifecycle ──────────────────────────────────────────────

    @abstractmethod
    def connect(self) -> bool:
        """Establish connection. Returns True on success."""
        ...

    @abstractmethod
    def disconnect(self):
        """Clean up connection resources."""
        ...

    # ── Account ────────────────────────────────────────────────

    @abstractmethod
    def get_account_info(self) -> AccountInfo:
        """Query account equity, cash, buying power."""
        ...

    @abstractmethod
    def get_positions(self) -> list[PositionInfo]:
        """Return all open positions."""
        ...

    # ── Orders ─────────────────────────────────────────────────

    @abstractmethod
    def place_order(
        self,
        ticker: str,            # .AX format
        side: OrderSide,
        qty: int,
        price: float,
        order_type: OrderType = OrderType.LIMIT,
        stop_price: Optional[float] = None,
        remark: str = "",
    ) -> OrderResult:
        """Place a new order. Returns result with order_id."""
        ...

    @abstractmethod
    def cancel_order(self, order_id: str) -> OrderResult:
        """Cancel a specific order."""
        ...

    @abstractmethod
    def cancel_all_orders(self) -> list[OrderResult]:
        """Emergency: cancel every open order. Returns list of results."""
        ...

    @abstractmethod
    def get_open_orders(self) -> list[OrderResult]:
        """Return all currently open/pending orders."""
        ...

    @abstractmethod
    def get_order_status(self, order_id: str) -> OrderResult:
        """Query status of a specific order."""
        ...

    # ── Market Data (optional — default falls back to yfinance) ──

    def get_prices(self, tickers: list[str]) -> dict[str, float]:
        """Get latest prices for tickers. Override for real-time data.

        Args:
            tickers: List of .AX format tickers.

        Returns:
            Dict of ticker -> latest price.
        """
        return {}  # Default: not implemented, caller falls back to cache

    # ── Extended queries (optional — broker-specific) ──────────

    def get_history_orders(self, days: int = 30) -> list[OrderResult]:
        """Get historical orders for the past N days."""
        return []

    def get_history_deals(self, days: int = 30) -> list[DealInfo]:
        """Get historical deal fills for the past N days."""
        return []

    def get_today_deals(self) -> list[DealInfo]:
        """Get today's executed fills. Override in broker implementations.

        # Audit C4: base default returns empty list so callers that wrap
        # in try/except AttributeError degrade gracefully without crashing.
        """
        return []

    def get_order_fees(self, order_ids: list[str]) -> list[OrderFeeInfo]:
        """Get fee breakdown for specific orders."""
        return []

    def get_market_states(self, tickers: list[str]) -> list[MarketStateInfo]:
        """Get current market state (open/closed/etc) for tickers."""
        return []

    def get_trading_days(self, market: str = "US", days: int = 30) -> list[TradingDayInfo]:
        """Get trading calendar for the past N days."""
        return []

    def get_max_trade_qty(self, ticker: str, price: float) -> Optional[int]:
        """Query max buyable/sellable quantity for a ticker at given price."""
        return None

    def get_slippage_report(self, days: int = 30) -> list[SlippageReport]:
        """Analyse slippage by comparing order prices to fill prices."""
        return []

    # ── Convenience ────────────────────────────────────────────

    def buy(self, ticker: str, qty: int, price: float,
            order_type: OrderType = OrderType.LIMIT, **kwargs) -> OrderResult:
        """Shorthand for place_order with BUY side."""
        return self.place_order(ticker, OrderSide.BUY, qty, price,
                                order_type=order_type, **kwargs)

    def sell(self, ticker: str, qty: int, price: float,
             order_type: OrderType = OrderType.LIMIT, **kwargs) -> OrderResult:
        """Shorthand for place_order with SELL side."""
        return self.place_order(ticker, OrderSide.SELL, qty, price,
                                order_type=order_type, **kwargs)

    def __repr__(self):
        status = "connected" if self._connected else "disconnected"
        live = "LIVE" if self.is_live else "DRY_RUN"
        return f"<{self.name} [{live}] {status}>"
