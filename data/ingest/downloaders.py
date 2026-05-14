"""Download paths for Atlas OHLCV data.

Handles downloads via yfinance and Alpaca, plus routing logic (_fetch_ohlcv).
Depends on: data.ingest.cache (constants + _is_crypto_ticker),
            data.ingest.normalization (_clean_ohlcv, _apply_split_adjustments).
"""
import logging
from datetime import timedelta
from typing import Dict, List, Optional

import pandas as pd

from data.ingest.cache import _YFINANCE_ONLY, _is_crypto_ticker
from data.ingest.normalization import _clean_ohlcv, _apply_split_adjustments

logger = logging.getLogger(__name__)

try:
    import yfinance as yf
    YF_AVAILABLE = True
except ImportError:
    YF_AVAILABLE = False
    logger.warning("yfinance not installed -- yfinance fallback unavailable")


# ---------------------------------------------------------------------------
# Download via yfinance
# ---------------------------------------------------------------------------

def _download_via_yfinance(
    ticker: str,
    start_str: str,
    end_str: str,
) -> pd.DataFrame:
    """Download OHLCV for a single ticker via yfinance.

    Applies split adjustments (not dividend adjustments) to historical prices.

    Args:
        ticker:    Fully-formatted ticker symbol (e.g. 'AAPL', 'BHP.AX').
        start_str: Inclusive start date 'YYYY-MM-DD'.
        end_str:   Inclusive end date   'YYYY-MM-DD'.

    Returns:
        Cleaned split-adjusted DataFrame, or empty DataFrame on failure.
    """
    if not YF_AVAILABLE:
        logger.warning(f"{ticker}: yfinance not available")
        return pd.DataFrame()

    # yfinance end is exclusive -- add 1 day so end_str is included
    fetch_end = (pd.Timestamp(end_str) + timedelta(days=1)).strftime("%Y-%m-%d")
    try:
        df = yf.download(ticker, start=start_str, end=fetch_end,
                         progress=False, auto_adjust=False)
        if not df.empty:
            cleaned = _clean_ohlcv(df, ticker)
            # Fetch split history and backward-adjust prices to current scale.
            # Only split adjustments -- no dividend adjustments (prevents
            # retroactive price drift when dividends are paid).
            try:
                splits = yf.Ticker(ticker).splits
                if splits is not None and not splits.empty:
                    # Ensure tz-naive to match cleaned df index
                    if splits.index.tz is not None:
                        splits.index = splits.index.tz_localize(None)
                    cleaned = _apply_split_adjustments(cleaned, splits)
                    logger.debug(
                        f"{ticker}: applied {len(splits)} split adjustment(s)"
                    )
            except Exception as se:
                logger.warning(
                    f"{ticker}: split data fetch failed ({se}) "
                    "-- proceeding without split adjustment"
                )
            return cleaned
    except Exception as e:
        logger.error(f"{ticker}: yfinance download failed: {e}")

    return pd.DataFrame()


# ---------------------------------------------------------------------------
# Download via Alpaca
# ---------------------------------------------------------------------------

def _download_via_alpaca(
    tickers: List[str],
    start_date: str,
    end_date: str,
    config: Optional[dict] = None,  # reserved for future source_priority reads
) -> Dict[str, pd.DataFrame]:
    """Download OHLCV via Alpaca API.

    Uses the AlpacaMarketData singleton (reads credentials from
    ``~/.atlas-secrets.json``).  Returns an empty dict if Alpaca is
    unavailable or credentials are missing -- callers should fall back to
    yfinance in that case.

    Args:
        tickers:    List of Atlas-format US tickers.
        start_date: Start date 'YYYY-MM-DD'.
        end_date:   End date   'YYYY-MM-DD'.
        config:     Optional config dict (reserved; not used yet).

    Returns:
        Dict of ticker -> DataFrame (same schema as yfinance output).
        Empty dict on any failure.
    """
    try:
        from brokers.alpaca.market_data import get_alpaca_data_client
        client = get_alpaca_data_client()
        if client is None:
            logger.debug("_download_via_alpaca: no Alpaca client available")
            return {}
        result = client.download_universe_bars(tickers, start_date, end_date)
        logger.debug(
            "_download_via_alpaca: got %d/%d tickers",
            len(result), len(tickers),
        )
        return result
    except Exception as e:
        logger.warning("_download_via_alpaca failed: %s", e)
        return {}


# ---------------------------------------------------------------------------
# Routing: _fetch_ohlcv
# ---------------------------------------------------------------------------

def _fetch_ohlcv(
    ticker: str,
    start_str: str,
    end_str: str,
    market_id: Optional[str] = None,
) -> pd.DataFrame:
    """Download OHLCV from the best available source and return cleaned data.

    Routing rules:
    - Tickers in ``_YFINANCE_ONLY`` always go to yfinance (index tickers,
      futures, and ETFs not available on Alpaca).
    - SP500 market: Alpaca-primary with yfinance fallback.
    - All other markets (ASX, etc.): yfinance directly.

    Args:
        ticker:    Fully-formatted ticker (e.g. 'AAPL' for sp500, 'BHP.AX' for asx).
        start_str: Inclusive start date 'YYYY-MM-DD'.
        end_str:   Inclusive end date   'YYYY-MM-DD'.
        market_id: Market identifier (e.g. 'sp500', 'asx').

    Returns:
        Cleaned DataFrame (same format as ``_clean_ohlcv``), or empty DataFrame.
    """
    # Force yfinance for index/commodity tickers not available on Alpaca
    if ticker in _YFINANCE_ONLY:
        logger.debug(f"{ticker}: in _YFINANCE_ONLY -- using yfinance directly")
        return _download_via_yfinance(ticker, start_str, end_str)

    # Crypto: route to Alpaca CryptoHistoricalDataClient
    if (market_id or "").lower() == "crypto" or _is_crypto_ticker(ticker):
        try:
            from brokers.alpaca.market_data import get_historical_bars as _alpaca_bars
            result = _alpaca_bars(ticker, start=start_str, end=end_str)
            df = result.get(ticker, pd.DataFrame())
            if not df.empty:
                logger.debug(f"{ticker}: Alpaca crypto bars ({len(df)} rows)")
                if "adj_close" in df.columns:
                    df = df.drop(columns=["adj_close"])
                return df
            logger.debug(f"{ticker}: Alpaca crypto returned empty -- falling back to yfinance")
        except Exception as e:
            logger.debug(f"{ticker}: Alpaca crypto fetch failed ({e}) -- falling back to yfinance")

    if (market_id or "").lower() == "sp500":
        # Alpaca-primary path for US equities
        try:
            from brokers.alpaca.market_data import get_historical_bars as _alpaca_bars
            result = _alpaca_bars(ticker, start=start_str, end=end_str)
            df = result.get(ticker, pd.DataFrame())
            if not df.empty:
                logger.debug(f"{ticker}: Alpaca historical bars ({len(df)} rows)")
                # Drop adj_close -- canonical format is open, high, low, close, volume, ticker
                if "adj_close" in df.columns:
                    df = df.drop(columns=["adj_close"])
                return df  # already in Atlas format from get_historical_bars
            logger.debug(f"{ticker}: Alpaca returned empty -- falling back to yfinance")
        except Exception as e:
            logger.debug(f"{ticker}: Alpaca fetch failed ({e}) -- falling back to yfinance")

    # yfinance path (all non-sp500 markets, or sp500 fallback)
    return _download_via_yfinance(ticker, start_str, end_str)
