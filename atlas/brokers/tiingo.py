"""
Tiingo Data Client
==================
Fetch real-time and EOD prices from Tiingo's IEX endpoint for US equities.

Used by:
  - eod_settlement.py  (post-close OHLC prices)
  - dashboard/generate_data.py  (live portfolio prices)

Credentials loaded from ~/.atlas-secrets.json  (key: TIINGO_API_TOKEN).
"""

import json
import logging
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional

import requests

logger = logging.getLogger(__name__)

_SECRETS_PATH = Path.home() / ".atlas-secrets.json"
_IEX_URL = "https://api.tiingo.com/iex/"

# Singleton
_client: Optional["TiingoClient"] = None
_client_lock = threading.Lock()

# Price cache — avoids redundant API calls within the same minute
_price_cache: Dict[tuple, tuple] = {}
_CACHE_TTL = 60  # seconds


class TiingoClient:
    """Lightweight Tiingo IEX client for US equity quotes."""

    def __init__(self, token: str):
        self.token = token
        self.session = requests.Session()
        self.session.headers.update({
            "Content-Type": "application/json",
            "Authorization": f"Token {token}",
        })

    def get_quotes(self, tickers: List[str]) -> Dict[str, dict]:
        """Fetch latest IEX quotes for a list of US tickers.

        Returns dict of ticker -> {
            "price": float,        # tngoLast (composite last)
            "prev_close": float,
            "open": float,
            "high": float,
            "low": float,
            "volume": int,
            "timestamp": str,
        }

        Tickers with no data are silently omitted.
        """
        if not tickers:
            return {}

        # Check time-based cache
        cache_key = tuple(sorted(tickers))
        now = time.time()
        if cache_key in _price_cache:
            cached_time, cached_result = _price_cache[cache_key]
            if (now - cached_time) < _CACHE_TTL:
                logger.debug("Tiingo IEX: returning cached quotes for %d tickers", len(tickers))
                return cached_result

        # Tiingo IEX accepts comma-separated tickers (max ~100 per request)
        results: Dict[str, dict] = {}
        # Batch in chunks of 50 to stay well within limits
        for i in range(0, len(tickers), 50):
            batch = tickers[i : i + 50]
            ticker_str = ",".join(batch)
            try:
                resp = self.session.get(
                    _IEX_URL,
                    params={"tickers": ticker_str, "token": self.token},
                    timeout=15,
                )
                resp.raise_for_status()
                data = resp.json()
                for item in data:
                    t = item.get("ticker", "").upper()
                    if not t:
                        continue
                    price = item.get("tngoLast") or item.get("last") or item.get("mid") or 0
                    results[t] = {
                        "price": float(price) if price else 0,
                        "prev_close": float(item.get("prevClose", 0) or 0),
                        "open": float(item.get("open", 0) or 0),
                        "high": float(item.get("high", 0) or 0),
                        "low": float(item.get("low", 0) or 0),
                        "volume": int(item.get("volume", 0) or 0),
                        "timestamp": item.get("timestamp", ""),
                    }
            except requests.RequestException as e:
                logger.error("Tiingo IEX request failed for batch %d: %s", i, e)
            except (ValueError, KeyError) as e:
                logger.error("Tiingo IEX parse error for batch %d: %s", i, e)

        logger.info("Tiingo IEX: got quotes for %d/%d tickers", len(results), len(tickers))
        # Update cache
        _price_cache[cache_key] = (now, results)
        return results


    def get_daily_prices(self, ticker: str, start_date: str,
                         end_date: str = "") -> List[dict]:
        """Fetch daily EOD prices from Tiingo for a date range.

        Args:
            ticker: US equity ticker (e.g. 'SPY')
            start_date: YYYY-MM-DD
            end_date: YYYY-MM-DD (optional, defaults to today)

        Returns:
            [{"date": "YYYY-MM-DD", "close": float, "open": float,
              "high": float, "low": float, "volume": int}, ...]
            Sorted ascending by date. Trading days only.
        """
        params = {"startDate": start_date, "token": self.token}
        if end_date:
            params["endDate"] = end_date
        try:
            resp = self.session.get(
                f"https://api.tiingo.com/tiingo/daily/{ticker}/prices",
                params=params,
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
            result = []
            for item in data:
                result.append({
                    "date": item["date"][:10],
                    "close": float(item.get("close", 0) or 0),
                    "open": float(item.get("open", 0) or 0),
                    "high": float(item.get("high", 0) or 0),
                    "low": float(item.get("low", 0) or 0),
                    "volume": int(item.get("volume", 0) or 0),
                })
            logger.info("Tiingo daily: %s returned %d bars (%s → %s)",
                        ticker, len(result), start_date, end_date or "now")
            return result
        except requests.RequestException as e:
            logger.error("Tiingo daily request failed for %s: %s", ticker, e)
            return []
        except (ValueError, KeyError) as e:
            logger.error("Tiingo daily parse error for %s: %s", ticker, e)
            return []


def get_tiingo_client() -> Optional[TiingoClient]:
    """Return a singleton TiingoClient, or None if credentials missing."""
    global _client
    if _client is not None:
        return _client

    with _client_lock:
        # Double-check after acquiring lock
        if _client is not None:
            return _client
        try:
            secrets = json.loads(_SECRETS_PATH.read_text())
            token = secrets.get("TIINGO_API_TOKEN", "")
            if not token:
                logger.warning("TIINGO_API_TOKEN not set in %s", _SECRETS_PATH)
                return None
            _client = TiingoClient(token)
            return _client
        except FileNotFoundError:
            logger.warning("Secrets file not found: %s", _SECRETS_PATH)
            return None
        except (json.JSONDecodeError, KeyError) as e:
            logger.warning("Error reading Tiingo token: %s", e)
            return None
