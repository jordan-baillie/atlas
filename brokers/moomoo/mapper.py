"""Ticker mapping between Atlas (.AX) and Moomoo (AU.) formats.

Atlas uses yfinance format:  BHP.AX, CBA.AX, IOZ.AX
Moomoo uses market prefix:   AU.BHP, AU.CBA, AU.IOZ

All conversion happens at the broker boundary — Atlas internals
never see AU. format.
"""


def to_moomoo(ticker: str) -> str:
    """Convert Atlas .AX ticker to Moomoo AU. format.

    >>> to_moomoo('BHP.AX')
    'AU.BHP'
    >>> to_moomoo('AU.BHP')
    'AU.BHP'
    """
    if ticker.startswith("AU."):
        return ticker
    code = ticker.replace(".AX", "").upper()
    return f"AU.{code}"


def to_atlas(moomoo_code: str) -> str:
    """Convert Moomoo AU. ticker to Atlas .AX format.

    >>> to_atlas('AU.BHP')
    'BHP.AX'
    >>> to_atlas('BHP.AX')
    'BHP.AX'
    """
    if moomoo_code.endswith(".AX"):
        return moomoo_code
    code = moomoo_code.replace("AU.", "").upper()
    return f"{code}.AX"


def to_moomoo_list(tickers: list[str]) -> list[str]:
    """Convert list of Atlas tickers to Moomoo format."""
    return [to_moomoo(t) for t in tickers]


def to_atlas_list(moomoo_codes: list[str]) -> list[str]:
    """Convert list of Moomoo codes to Atlas format."""
    return [to_atlas(c) for c in moomoo_codes]
