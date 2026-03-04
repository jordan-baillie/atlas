"""Ticker mapping between Atlas and Alpaca Markets formats.

Atlas uses yfinance format:
  SP500: AAPL, MSFT, TSLA  (plain symbols, no suffix)

Alpaca uses the same bare symbol format for US equities:
  AAPL, MSFT, TSLA

Conversion for US stocks is essentially a passthrough. This module
exists for consistency with other broker implementations and to handle
any edge cases (e.g. accidental .AX suffix, case normalisation).

All conversion happens at the broker boundary — Atlas internals
never see Alpaca-specific formatting.
"""

from __future__ import annotations


def to_alpaca(ticker: str, market_id: str = "sp500") -> str:
    """Convert Atlas ticker to Alpaca symbol format.

    For US stocks (SP500): passthrough — both use bare symbols like AAPL.
    Strips any accidental suffixes and uppercases.

    >>> to_alpaca("AAPL")
    'AAPL'
    >>> to_alpaca("aapl")
    'AAPL'
    >>> to_alpaca("BHP.AX", "asx")
    'BHP'
    >>> to_alpaca("0700.HK", "hk")
    '0700'
    """
    t = ticker.strip().upper()

    # Strip yfinance suffixes if accidentally present
    for suffix in (".AX", ".HK", ".L", ".T"):
        if t.endswith(suffix):
            return t[: -len(suffix)]

    return t


def to_atlas(alpaca_symbol: str, market_id: str = "sp500") -> str:
    """Convert Alpaca symbol to Atlas/yfinance format.

    For US stocks: passthrough — same bare symbol like AAPL.

    >>> to_atlas("AAPL")
    'AAPL'
    >>> to_atlas("msft")
    'MSFT'
    """
    return alpaca_symbol.strip().upper()


def to_alpaca_list(tickers: list[str], market_id: str = "sp500") -> list[str]:
    """Convert list of Atlas tickers to Alpaca format."""
    return [to_alpaca(t, market_id) for t in tickers]


def to_atlas_list(alpaca_symbols: list[str], market_id: str = "sp500") -> list[str]:
    """Convert list of Alpaca symbols to Atlas format."""
    return [to_atlas(s, market_id) for s in alpaca_symbols]
