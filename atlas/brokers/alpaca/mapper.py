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

# Quote-currency suffixes used by crypto pairs in yfinance/Atlas format (BTC-USD)
_CRYPTO_QUOTE_SUFFIXES = ("-USD", "-USDT", "-BTC", "-ETH")


def to_alpaca(atlas_ticker: str) -> str:
    """Convert Atlas/yfinance ticker to Alpaca symbol.

    Key mappings:
        - BTC-USD → BTC/USD  (crypto: yfinance uses '-USD', Alpaca uses '/USD')
        - BRK-B → BRK.B     (share class: yfinance uses '-', Alpaca uses '.')
        - .AX / .HK suffixes stripped (non-US, best-effort)
        - Uppercase normalisation

    >>> to_alpaca('AAPL')
    'AAPL'
    >>> to_alpaca('aapl')
    'AAPL'
    >>> to_alpaca('BRK-B')
    'BRK.B'
    >>> to_alpaca('BRK.B')
    'BRK.B'
    """
    ticker = atlas_ticker.strip().upper()

    # Strip known non-US suffixes
    for suffix in _NON_US_SUFFIXES:
        if ticker.endswith(suffix):
            return ticker[: -len(suffix)]

    # Crypto pairs: BTC-USD → BTC/USD (Alpaca uses slash for crypto)
    if any(ticker.endswith(s) for s in _CRYPTO_QUOTE_SUFFIXES):
        return ticker.replace("-", "/")

    # yfinance uses '-' for share class (BRK-B), Alpaca uses '.' (BRK.B)
    if "-" in ticker:
        ticker = ticker.replace("-", ".")

    return ticker


def to_atlas(alpaca_symbol: str) -> str:
    """Convert Alpaca symbol to Atlas/yfinance format.

    Key mappings:
        - BRK.B → BRK-B  (Alpaca uses '.', yfinance uses '-')
        - BRK/B → BRK-B  (Alpaca alt format)

    >>> to_atlas('AAPL')
    'AAPL'
    >>> to_atlas('BRK.B')
    'BRK-B'
    >>> to_atlas('BRK/B')
    'BRK-B'
    """
    symbol = alpaca_symbol.strip().upper()

    # Alpaca uses '/' for crypto (BTC/USD → BTC-USD) and occasionally
    # for share class (BRK/B → BRK-B); yfinance/Atlas always uses '-'
    if "/" in symbol:
        symbol = symbol.replace("/", "-")

    # Share class dots: BRK.B → BRK-B (but not suffixes like .AX)
    # Heuristic: if the part after '.' is 1-2 chars and all alpha, it's a
    # share class (B, A, WS) not a market suffix.
    if "." in symbol:
        parts = symbol.rsplit(".", 1)
        if len(parts) == 2 and len(parts[1]) <= 2 and parts[1].isalpha():
            symbol = parts[0] + "-" + parts[1]

    return symbol


def to_alpaca_list(tickers: list[str]) -> list[str]:
    """Convert list of Atlas tickers to Alpaca format."""
    return [to_alpaca(t) for t in tickers]


def to_atlas_list(alpaca_symbols: list[str]) -> list[str]:
    """Convert list of Alpaca symbols to Atlas format."""
    return [to_atlas(s) for s in alpaca_symbols]
