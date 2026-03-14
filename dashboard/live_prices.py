"""Live price fetcher for Atlas dashboard.

Provides near-real-time quotes with in-memory caching. Designed to be
called frequently (every 10–30 s) from the dashboard server's
/api/prices endpoint.

Price sources (priority order per ticker):
  1. In-memory cache (if fresh, < CACHE_TTL seconds old)
  2. Alpaca snapshot API — US equities only (no 15-min delay, real-time)
  3. Yahoo Finance v8 chart API — fallback for non-US tickers, indices,
     FX pairs (^GSPC, ^AXJO, AUDUSD=X) that Alpaca does not support.

Alpaca is skipped automatically when:
  - The ticker has a non-US suffix (.AX, .HK, =X, ^)
  - Alpaca credentials are not configured
  - Alpaca returns no data for the ticker
"""

import json
import logging
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Optional

logger = logging.getLogger("atlas.live_prices")

# ── Configuration ─────────────────────────────────────────────

CACHE_TTL = 12          # seconds — cache each ticker's quote
BATCH_TIMEOUT = 8       # seconds — max time for a full batch fetch
PER_TICKER_TIMEOUT = 5  # seconds — per-ticker HTTP timeout
MAX_WORKERS = 6         # parallel fetches
USER_AGENT = "Mozilla/5.0 (compatible; Atlas/1.0)"

# ── In-memory price cache ─────────────────────────────────────

_cache: dict[str, dict] = {}     # ticker -> {price, prev_close, change_pct, ...}
_cache_lock = threading.Lock()


def _is_fresh(ticker: str) -> bool:
    """Check if cached quote is still fresh."""
    entry = _cache.get(ticker)
    if not entry:
        return False
    return (time.time() - entry.get("_fetched_at", 0)) < CACHE_TTL


def get_cached(ticker: str) -> Optional[dict]:
    """Get cached quote if fresh enough."""
    with _cache_lock:
        if _is_fresh(ticker):
            return _cache[ticker]
    return None


# ── Alpaca ticker classification ────────────────────────────

# Patterns that Alpaca US equity API cannot handle.
# These always fall back to Yahoo Finance.
_YAHOO_ONLY_PREFIXES = ("^",)          # indices: ^GSPC, ^AXJO, ^HSI
_YAHOO_ONLY_SUFFIXES = (".AX", ".HK", ".L", ".T", "=X")  # non-US / FX


def _is_alpaca_supported(ticker: str) -> bool:
    """Return True if this ticker can be fetched via Alpaca (US equity)."""
    t = ticker.upper().strip()
    if any(t.startswith(p) for p in _YAHOO_ONLY_PREFIXES):
        return False
    if any(t.endswith(s) for s in _YAHOO_ONLY_SUFFIXES):
        return False
    return True


def _classify_freshness(ticker: str, market_time) -> str:
    """Classify quote freshness based on market timestamp age.

    Returns: 'live' (<60s), 'delayed' (<15min), 'stale' (<1h), 'closed' (>1h), 'unknown'.
    """
    if not market_time:
        return "unknown"
    try:
        if isinstance(market_time, (int, float)):
            ts = datetime.fromtimestamp(market_time)
        else:
            ts = datetime.fromisoformat(str(market_time))
        age_s = (datetime.now() - ts).total_seconds()
        if age_s < 60:
            return "live"
        elif age_s < 900:  # 15 min
            return "delayed"
        elif age_s < 3600:  # 1 hour
            return "stale"
        else:
            return "closed"
    except Exception:
        return "unknown"


def _fetch_alpaca_quote(ticker: str) -> Optional[dict]:
    """Fetch a live quote from Alpaca snapshot API for a US equity.

    Returns the same dict shape as ``_fetch_yf_quote()`` so callers are
    source-agnostic.  Returns None on failure or if Alpaca unavailable.
    """
    try:
        from brokers.alpaca.market_data import get_alpaca_data_client
        client = get_alpaca_data_client()
        if not client or not client.is_available:
            return None

        snap = client.get_snapshot(ticker)
        if not snap:
            return None

        price = snap.get("price", 0.0)
        if not price or price <= 0:
            return None

        daily = snap.get("daily_bar", {})
        prev = snap.get("prev_daily_bar", {})
        trade = snap.get("latest_trade", {})

        prev_close = prev.get("close", 0.0)
        change = round(price - prev_close, 4) if prev_close else 0
        change_pct = round(change / prev_close * 100, 2) if prev_close else 0

        market_time = snap.get("latest_trade", {}).get("timestamp")
        return {
            "ticker": ticker,
            "price": round(price, 4),
            "prev_close": round(prev_close, 4) if prev_close else None,
            "change": round(change, 4),
            "change_pct": round(change_pct, 2),
            "day_high": daily.get("high", price),
            "day_low": daily.get("low", price),
            "volume": daily.get("volume", trade.get("size", 0)),
            "currency": "USD",
            "exchange": "NASDAQ/NYSE",
            "market_time": market_time,
            "freshness": _classify_freshness(ticker, market_time),
            "source": "alpaca",
            "_fetched_at": time.time(),
        }
    except Exception as e:
        logger.debug("Alpaca quote failed for %s: %s", ticker, e)
        return None


# ── Yahoo Finance v8 fetcher ─────────────────────────────────

def _fetch_yf_quote(ticker: str) -> Optional[dict]:
    """Fetch a single quote from Yahoo Finance v8 chart API.

    Returns dict with: price, prev_close, change, change_pct, volume,
    day_high, day_low, currency, exchange, market_time, source.
    """
    try:
        url = (
            f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
            f"?interval=1m&range=1d&includePrePost=false"
        )
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=PER_TICKER_TIMEOUT) as resp:
            data = json.loads(resp.read())

        result = data.get("chart", {}).get("result")
        if not result:
            return None
        meta = result[0].get("meta", {})

        price = meta.get("regularMarketPrice")
        if not price or price <= 0:
            return None

        prev_close = meta.get("chartPreviousClose") or meta.get("previousClose") or 0
        change = round(price - prev_close, 4) if prev_close else 0
        change_pct = round(change / prev_close * 100, 2) if prev_close else 0

        # Extract day high/low from indicators if available
        indicators = result[0].get("indicators", {})
        quotes = indicators.get("quote", [{}])[0]
        highs = [h for h in (quotes.get("high") or []) if h is not None]
        lows = [l for l in (quotes.get("low") or []) if l is not None]
        volumes = quotes.get("volume") or []

        day_high = max(highs) if highs else price
        day_low = min(lows) if lows else price
        volume = sum(v for v in volumes if v is not None) if volumes else 0

        market_time_val = meta.get("regularMarketTime")
        quote = {
            "ticker": ticker,
            "price": round(price, 4),
            "prev_close": round(prev_close, 4) if prev_close else None,
            "change": round(change, 4),
            "change_pct": round(change_pct, 2),
            "day_high": round(day_high, 4),
            "day_low": round(day_low, 4),
            "volume": volume,
            "currency": meta.get("currency", ""),
            "exchange": meta.get("exchangeName", ""),
            "market_time": market_time_val,
            "freshness": _classify_freshness(ticker, market_time_val),
            "source": "yahoo",
            "_fetched_at": time.time(),
        }
        return quote

    except Exception as e:
        logger.debug("YF quote failed for %s: %s", ticker, e)
        return None


# ── Batch fetcher ─────────────────────────────────────────────

def _fetch_best_quote(ticker: str) -> Optional[dict]:
    """Fetch a quote from the best available source for this ticker.

    For US equities: tries Alpaca first (real-time), falls back to Yahoo.
    For indices / FX / non-US tickers: uses Yahoo directly.
    """
    if _is_alpaca_supported(ticker):
        quote = _fetch_alpaca_quote(ticker)
        if quote:
            return quote
        logger.debug("Alpaca miss for %s — falling back to Yahoo", ticker)

    return _fetch_yf_quote(ticker)


def fetch_prices(tickers: list[str]) -> dict[str, dict]:
    """Fetch live prices for a list of tickers.

    Uses cache for fresh quotes, fetches stale ones in parallel.
    Source priority per ticker:
      - Alpaca snapshot (US equities, real-time)
      - Yahoo Finance v8 (indices, FX, ASX, HK, or Alpaca fallback)

    Returns {ticker: quote_dict}.
    """
    if not tickers:
        return {}

    result = {}
    stale = []

    # Check cache first
    with _cache_lock:
        for t in tickers:
            if _is_fresh(t):
                result[t] = _cache[t]
            else:
                stale.append(t)

    if not stale:
        return result

    # Fetch stale tickers in parallel using best source per ticker
    with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, len(stale))) as pool:
        futures = {pool.submit(_fetch_best_quote, t): t for t in stale}
        for future in as_completed(futures, timeout=BATCH_TIMEOUT):
            ticker = futures[future]
            try:
                quote = future.result()
                if quote:
                    with _cache_lock:
                        _cache[ticker] = quote
                    result[ticker] = quote
                else:
                    # Return stale cache if available
                    with _cache_lock:
                        if ticker in _cache:
                            result[ticker] = _cache[ticker]
            except Exception as e:
                logger.debug("Fetch failed for %s: %s", ticker, e)
                with _cache_lock:
                    if ticker in _cache:
                        result[ticker] = _cache[ticker]

    return result


def fetch_index_prices() -> dict[str, dict]:
    """Fetch major index quotes (benchmarks + FX)."""
    indices = ["^GSPC", "^AXJO", "^HSI", "AUDUSD=X"]
    return fetch_prices(indices)


def get_all_tickers_from_dashboard(dashboard_path: str) -> list[str]:
    """Extract all position + plan tickers from the dashboard data file.

    Searches multiple locations where positions/plans may be stored:
    - Top-level: portfolio.open_positions, plan.entries
    - Per-market: markets.{mid}.open_positions, markets.{mid}.plan
    """
    try:
        with open(dashboard_path) as f:
            data = json.load(f)
    except Exception:
        return []

    tickers = set()

    # Top-level portfolio positions
    portfolio = data.get("portfolio", {})
    for pos in portfolio.get("open_positions", []):
        t = pos.get("ticker", "")
        if t:
            tickers.add(t)

    # Top-level plan entries
    plan = data.get("plan", {})
    for entry in plan.get("proposed_entries", plan.get("entries", [])):
        t = entry.get("ticker", "")
        if t:
            tickers.add(t)

    # Per-market positions and plans
    for mid, mkt in data.get("markets", {}).items():
        for pos in mkt.get("open_positions", mkt.get("positions", [])):
            t = pos.get("ticker", "")
            if t:
                tickers.add(t)
        mplan = mkt.get("plan", {})
        if mplan:
            for entry in mplan.get("proposed_entries", mplan.get("entries", [])):
                t = entry.get("ticker", "")
                if t:
                    tickers.add(t)

    # Manual portfolio positions (Moomoo)
    for pos in data.get("manual_portfolio", {}).get("positions", []):
        t = pos.get("ticker", "")
        if t:
            tickers.add(t)

    return list(tickers)


def get_cache_stats() -> dict:
    """Return cache statistics for monitoring."""
    with _cache_lock:
        now = time.time()
        total = len(_cache)
        fresh = sum(1 for v in _cache.values()
                    if (now - v.get("_fetched_at", 0)) < CACHE_TTL)
        return {
            "total_cached": total,
            "fresh": fresh,
            "stale": total - fresh,
            "cache_ttl_s": CACHE_TTL,
        }
