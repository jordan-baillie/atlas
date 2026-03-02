"""Ticker mapping between Atlas and Moomoo formats.

Atlas uses yfinance format:
  ASX:   BHP.AX, CBA.AX, IOZ.AX
  SP500: AAPL, MSFT, ON  (plain symbols, no suffix)
  HK:    0700.HK, 0005.HK, 2800.HK  (leading zeros preserved)

Moomoo uses market prefix:
  ASX:   AU.BHP, AU.CBA, AU.IOZ
  US:    US.AAPL, US.MSFT, US.ON
  HK:    HK.0700, HK.0005, HK.2800  (leading zeros preserved)

All conversion happens at the broker boundary — Atlas internals
never see AU./US./HK. format.
"""

# Market ID → Moomoo prefix
_MARKET_PREFIX = {
    "asx": "AU",
    "sp500": "US",
    "hk": "HK",
}

# Moomoo prefix → yfinance suffix (empty string = no suffix)
_PREFIX_SUFFIX = {
    "AU": ".AX",
    "US": "",
    "HK": ".HK",
}


def to_moomoo(ticker: str, market_id: str = "asx") -> str:
    """Convert Atlas ticker to Moomoo format.

    >>> to_moomoo('BHP.AX', 'asx')
    'AU.BHP'
    >>> to_moomoo('AAPL', 'sp500')
    'US.AAPL'
    >>> to_moomoo('US.AAPL', 'sp500')
    'US.AAPL'
    >>> to_moomoo('0700.HK', 'hk')
    'HK.0700'
    >>> to_moomoo('0005', 'hk')
    'HK.0005'
    """
    prefix = _MARKET_PREFIX.get(market_id, "AU")

    # Already in Moomoo format
    if ticker.startswith(f"{prefix}."):
        return ticker

    # Strip yfinance suffix if present
    suffix = _PREFIX_SUFFIX.get(prefix, "")
    if suffix and ticker.endswith(suffix):
        code = ticker[: -len(suffix)].upper()
    else:
        code = ticker.upper()

    return f"{prefix}.{code}"


def to_atlas(moomoo_code: str) -> str:
    """Convert Moomoo ticker to Atlas/yfinance format.

    >>> to_atlas('AU.BHP')
    'BHP.AX'
    >>> to_atlas('US.AAPL')
    'AAPL'
    >>> to_atlas('BHP.AX')
    'BHP.AX'
    >>> to_atlas('HK.0700')
    '0700.HK'
    >>> to_atlas('HK.0005')
    '0005.HK'
    """
    for prefix, suffix in _PREFIX_SUFFIX.items():
        if moomoo_code.startswith(f"{prefix}."):
            code = moomoo_code[len(prefix) + 1:].upper()
            return f"{code}{suffix}"

    # Already in Atlas format or unknown — pass through
    return moomoo_code


def to_moomoo_list(tickers: list[str], market_id: str = "asx") -> list[str]:
    """Convert list of Atlas tickers to Moomoo format."""
    return [to_moomoo(t, market_id) for t in tickers]


def to_atlas_list(moomoo_codes: list[str]) -> list[str]:
    """Convert list of Moomoo codes to Atlas format."""
    return [to_atlas(c) for c in moomoo_codes]
