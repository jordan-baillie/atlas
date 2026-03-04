"""Ticker mapping between Atlas and Alpaca formats.

Atlas uses yfinance format for US/SP500:
    AAPL, MSFT, GOOGL, BRK.B  (plain symbols, no suffix)

Alpaca uses the same format for US stocks:
    AAPL, MSFT, GOOGL, BRK.B  (plain symbols, no suffix)

This means conversion is mostly pass-through for SP500.
Edge cases handled:
    - Strip .AX suffix if somehow an ASX ticker gets passed in
    - Strip .HK suffix if somehow an HK ticker gets passed in
    - Uppercase normalisation

All conversion happens at the broker boundary — Atlas internals
never see broker-specific formatting.
"""


# Suffixes that indicate non-US markets — not supported via Alpaca US equity
_NON_US_SUFFIXES = (".AX", ".HK", ".L", ".T", ".PA", ".DE")


def to_alpaca(atlas_ticker: str) -> str:
    """Convert Atlas ticker to Alpaca symbol.

    For US equities (SP500), this is a no-op since both formats
    use plain symbols (AAPL, MSFT, etc.).

    Edge cases:
        - Already in Alpaca format → returned as-is (uppercased)
        - Has non-US suffix (e.g. BHP.AX) → strip suffix, warn
        - Handles BRK.B style class suffixes correctly (not stripped)

    >>> to_alpaca('AAPL')
    'AAPL'
    >>> to_alpaca('aapl')
    'AAPL'
    >>> to_alpaca('BRK.B')
    'BRK.B'
    """
    ticker = atlas_ticker.strip().upper()

    # Strip known non-US suffixes
    for suffix in _NON_US_SUFFIXES:
        if ticker.endswith(suffix):
            # Non-US ticker — strip suffix for best-effort lookup
            return ticker[: -len(suffix)]

    return ticker


def to_atlas(alpaca_symbol: str) -> str:
    """Convert Alpaca symbol to Atlas/yfinance format.

    For US equities, this is a no-op since Alpaca uses the same
    bare format as Atlas for SP500 symbols.

    >>> to_atlas('AAPL')
    'AAPL'
    >>> to_atlas('BRK/B')
    'BRK-B'
    """
    symbol = alpaca_symbol.strip().upper()

    # Alpaca sometimes uses '/' for class separators (BRK/B) while
    # yfinance uses '-'. Normalise to yfinance format.
    if "/" in symbol:
        symbol = symbol.replace("/", "-")

    return symbol


def to_alpaca_list(tickers: list[str]) -> list[str]:
    """Convert list of Atlas tickers to Alpaca format."""
    return [to_alpaca(t) for t in tickers]


def to_atlas_list(alpaca_symbols: list[str]) -> list[str]:
    """Convert list of Alpaca symbols to Atlas format."""
    return [to_atlas(s) for s in alpaca_symbols]
