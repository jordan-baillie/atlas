"""Atlas Market Profile Base Classes.

Defines the abstract MarketProfile that each exchange must implement,
plus shared data structures for fees, trading hours, etc.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass
class FeeStructure:
    """Broker fee structure for a market.

    Attributes:
        commission_per_trade: Flat fee per trade in local currency.
        commission_pct: Percentage commission (e.g., 0.0003 = 0.03%).
        slippage_pct: Estimated slippage as fraction (e.g., 0.001 = 0.1%).
        flat_fee_threshold: Position value above which pct fee applies.
        min_position_value: Minimum position value allowed.
    """
    commission_per_trade: float = 5.0
    commission_pct: float = 0.0003
    slippage_pct: float = 0.001
    flat_fee_threshold: float = 10000.0
    min_position_value: float = 500.0


@dataclass
class TradingHours:
    """Trading session times (in local exchange timezone).

    Attributes:
        timezone: IANA timezone string.
        market_open: Opening time as "HH:MM".
        market_close: Closing time as "HH:MM".
        pre_market_open: Pre-market start (None if no pre-market).
        post_market_close: Post-market end (None if no post-market).
    """
    timezone: str = "UTC"
    market_open: str = "09:30"
    market_close: str = "16:00"
    pre_market_open: Optional[str] = None
    post_market_close: Optional[str] = None


class MarketProfile(ABC):
    """Abstract base class for exchange/index market profiles.

    Each market (ASX, S&P 500, FTSE, etc.) implements this to provide
    market-specific configuration. Strategies and the backtest engine
    remain market-agnostic — they consume data through this interface.
    """

    # --- Required class attributes (override in subclass) ---

    @property
    @abstractmethod
    def market_id(self) -> str:
        """Short lowercase identifier, e.g. 'asx', 'sp500'."""
        ...

    @property
    @abstractmethod
    def display_name(self) -> str:
        """Human-readable name, e.g. 'ASX 200', 'S&P 500'."""
        ...

    @property
    @abstractmethod
    def country(self) -> str:
        """ISO 3166-1 alpha-2 country code, e.g. 'AU', 'US'."""
        ...

    @property
    @abstractmethod
    def currency(self) -> str:
        """ISO 4217 currency code, e.g. 'AUD', 'USD'."""
        ...

    @property
    @abstractmethod
    def yfinance_suffix(self) -> str:
        """Ticker suffix for yfinance downloads, e.g. '.AX', '', '.L'."""
        ...

    @property
    @abstractmethod
    def benchmark_ticker(self) -> str:
        """Benchmark ETF ticker (fully qualified), e.g. 'IOZ.AX', 'SPY'."""
        ...

    @property
    @abstractmethod
    def risk_free_rate(self) -> float:
        """Annual risk-free rate for Sharpe calculations."""
        ...

    @property
    @abstractmethod
    def trading_hours(self) -> TradingHours:
        """Trading session times for this market."""
        ...

    @property
    @abstractmethod
    def default_fees(self) -> FeeStructure:
        """Default fee structure for this market."""
        ...

    # --- Abstract methods ---

    @abstractmethod
    def get_universe_tickers(self) -> List[str]:
        """Return the full candidate universe of ticker codes (without suffix).

        These are raw codes like 'BHP', 'AAPL'. The suffix is added
        by format_ticker().

        Returns:
            List of ticker code strings.
        """
        ...

    @abstractmethod
    def get_sector_map(self) -> Dict[str, str]:
        """Return a mapping of ticker code -> GICS sector name.

        Returns:
            Dict mapping ticker codes to sector strings.
            Empty dict if not available (will be fetched dynamically).
        """
        ...

    # --- Concrete helpers ---

    def format_ticker(self, code: str) -> str:
        """Add the market suffix to a raw ticker code.

        >>> asx.format_ticker('BHP')
        'BHP.AX'
        >>> sp500.format_ticker('AAPL')
        'AAPL'
        """
        code = code.upper().strip()
        # Already has the suffix
        if self.yfinance_suffix and code.endswith(self.yfinance_suffix):
            return code
        # Strip any existing suffix first
        if "." in code:
            code = code.split(".")[0]
        return f"{code}{self.yfinance_suffix}"

    def strip_suffix(self, ticker: str) -> str:
        """Remove the market suffix from a fully-qualified ticker.

        >>> asx.strip_suffix('BHP.AX')
        'BHP'
        >>> sp500.strip_suffix('AAPL')
        'AAPL'
        """
        if self.yfinance_suffix and ticker.endswith(self.yfinance_suffix):
            return ticker[: -len(self.yfinance_suffix)]
        return ticker

    def get_formatted_tickers(self) -> List[str]:
        """Return universe tickers with the market suffix applied.

        Returns:
            List of fully-qualified ticker strings for yfinance.
        """
        return [self.format_ticker(t) for t in self.get_universe_tickers()]

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__} market_id={self.market_id!r} tickers={len(self.get_universe_tickers())}>"
