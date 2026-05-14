"""Standalone cache helpers for Atlas ingest.

Contains only stateless helpers, constants, and ticker-list functions.
NOT included here: CACHE_DIR, _market_cache_dir, _cache_path, _cache_is_fresh,
_load_cache, _save_cache, clear_cache, cache_stats -- those live in
data/ingest/__init__.py so tests that patch data.ingest.* continue to work.
"""
import logging
from typing import List, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants re-used by __init__.py and downloaders.py
# ---------------------------------------------------------------------------

CACHE_MAX_AGE_HOURS = 24
DEFAULT_MARKET = "asx"

# Tickers that MUST use yfinance -- not available or unreliable on Alpaca.
# Includes index tickers (^VIX, ^GSPC), futures (GC=F, HG=F), and broad ETFs
# that Alpaca's IEX feed doesn't carry consistently.
_YFINANCE_ONLY = {"^VIX", "^TNX", "^IRX", "^AXJO", "GC=F", "HG=F", "SPY", "^GSPC", "^SKEW", "RSP"}


# ---------------------------------------------------------------------------
# Stateless helpers
# ---------------------------------------------------------------------------

def _is_crypto_ticker(symbol: str) -> bool:
    """Return True if symbol looks like a crypto pair (e.g. BTC-USD, BTC/USD)."""
    crypto_suffixes = ('/USD', '/USDT', '-USD', '-USDT')
    return any(symbol.upper().endswith(s) for s in crypto_suffixes)


# ---------------------------------------------------------------------------
# Backward-compatible ticker list (delegates to market profile)
# ---------------------------------------------------------------------------

def get_asx200_tickers() -> List[str]:
    """Return ASX 200 tickers with .AX suffix.

    Backward-compatible wrapper -- delegates to the ASX market profile.
    """
    from markets import get_market
    market = get_market("asx")
    tickers = market.get_formatted_tickers()
    logger.info(f"ASX ticker universe: {len(tickers)} tickers")
    return tickers


def get_market_tickers(market_id: str) -> List[str]:
    """Return formatted tickers for any registered market.

    Args:
        market_id: Market identifier (e.g., 'asx', 'sp500').

    Returns:
        List of yfinance-ready ticker strings.
    """
    from markets import get_market
    market = get_market(market_id)
    tickers = market.get_formatted_tickers()
    logger.info(f"{market.display_name} ticker universe: {len(tickers)} tickers")
    return tickers
