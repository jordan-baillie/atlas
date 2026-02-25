"""Atlas Market Profile Registry.

Provides market-specific configurations for different stock exchanges.
Each market profile encapsulates ticker formats, currencies, benchmarks,
fee structures, and universe ticker lists.

Usage:
    from markets import get_market, list_markets

    asx = get_market("asx")
    sp500 = get_market("sp500")
    print(list_markets())  # ["asx", "sp500"]
"""

from markets.base import MarketProfile, FeeStructure, TradingHours
from markets.registry import MarketRegistry, get_market, list_markets

__all__ = [
    "MarketProfile",
    "FeeStructure",
    "TradingHours",
    "MarketRegistry",
    "get_market",
    "list_markets",
]
