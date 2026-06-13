"""Alpaca tradable asset cache.

Fetches and caches the list of active, tradable US equities from Alpaca.
Used to filter the Atlas universe and validate plan entries before execution.

Cache is stored on disk and refreshed at most once per day (asset lists
don't change intra-day). Thread-safe for concurrent dashboard/executor use.

Usage:
    from atlas.brokers.alpaca.tradable_assets import get_tradable_set, is_tradable

    tradable = get_tradable_set()        # set of Alpaca symbols (BRK.B format)
    ok = is_tradable("AAPL")             # True
    ok = is_tradable("PXD")              # False (delisted)
    ok = is_tradable("BRK-B")            # True (auto-converts BRK-B → BRK.B)
"""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from atlas.brokers.alpaca.mapper import to_alpaca
from atlas.kernel.paths import DATA_DIR

logger = logging.getLogger("atlas.broker.alpaca.tradable_assets")

# Cache on disk — survives restarts
_CACHE_DIR = DATA_DIR / "cache"
_CACHE_FILE = _CACHE_DIR / "alpaca_tradable_assets.json"
_CACHE_TTL_HOURS = 20  # refresh at most once per ~day

# In-memory cache
_tradable_set: Optional[set[str]] = None
_shortable_set: Optional[set[str]] = None   # subset of tradable that Alpaca will let you SHORT
_cache_lock = threading.Lock()
_last_fetch_ts: float = 0.0


def _fetch_from_alpaca() -> tuple[set[str], set[str]]:
    """Fetch all active US equities from Alpaca API.

    Returns (tradable, shortable) sets of Alpaca-format symbols (e.g. 'AAPL', 'BRK.B').
    shortable is a subset of tradable: names Alpaca currently has borrow for (a.shortable).
    Submitting a short on a non-shortable name returns 42210000 'cannot be sold short' (task #37).
    """
    try:
        from alpaca.trading.client import TradingClient
        from alpaca.trading.requests import GetAssetsRequest
        from alpaca.trading.enums import AssetClass, AssetStatus
        from atlas.kernel.secrets import get_secret

        # Paper keys serve the asset list identically (verified 2026-06-11); live keys
        # are parked off the automation path until the live go-live gate (security audit).
        api_key = get_secret("ALPACA_PAPER_API_KEY", prompt=False) or get_secret("ALPACA_API_KEY", prompt=False)
        api_secret = get_secret("ALPACA_PAPER_SECRET_KEY", prompt=False) or get_secret("ALPACA_SECRET_KEY", prompt=False)
        if not api_key or not api_secret:
            logger.warning("Alpaca credentials not available — cannot fetch asset list")
            return set()

        client = TradingClient(
            api_key=api_key,
            secret_key=api_secret,
            paper=True,
        )
        req = GetAssetsRequest(
            asset_class=AssetClass.US_EQUITY,
            status=AssetStatus.ACTIVE,
        )
        assets = client.get_all_assets(req)
        tradable = {a.symbol for a in assets if a.tradable}
        shortable = {a.symbol for a in assets if a.tradable and getattr(a, "shortable", False)}
        logger.info("Fetched %d tradable (%d shortable) US equities from Alpaca", len(tradable), len(shortable))
        return tradable, shortable

    except Exception as e:
        logger.error("Failed to fetch Alpaca asset list: %s", e)
        return set(), set()


def _save_cache(symbols: set[str], shortable: set[str]) -> None:
    """Persist tradable + shortable sets to disk (atomic)."""
    try:
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        data = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "count": len(symbols),
            "symbols": sorted(symbols),
            "shortable": sorted(shortable),
        }
        import os as _os
        tmp = str(_CACHE_FILE) + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f)
        _os.replace(tmp, _CACHE_FILE)
        logger.debug("Saved %d tradable (%d shortable) assets to cache", len(symbols), len(shortable))
    except Exception as e:
        logger.warning("Failed to save tradable assets cache: %s", e)


def _load_cache() -> Optional[tuple[set[str], set[str]]]:
    """Load (tradable, shortable) from disk cache. Returns None if stale or missing.
    Back-compat: an old cache without a 'shortable' key yields an empty shortable set
    (is_shortable then fails open until the next ~daily refresh repopulates it)."""
    try:
        if not _CACHE_FILE.exists():
            return None
        with open(_CACHE_FILE) as f:
            data = json.load(f)
        ts = datetime.fromisoformat(data["timestamp"])
        age_hours = (datetime.now(timezone.utc) - ts).total_seconds() / 3600
        if age_hours > _CACHE_TTL_HOURS:
            logger.debug("Tradable assets cache is %.1f hours old — stale", age_hours)
            return None
        symbols = set(data["symbols"])
        shortable = set(data.get("shortable", []))
        logger.debug("Loaded %d tradable (%d shortable) from cache (%.1fh old)",
                     len(symbols), len(shortable), age_hours)
        return symbols, shortable
    except Exception as e:
        logger.warning("Failed to load tradable assets cache: %s", e)
        return None


def get_tradable_set(force_refresh: bool = False) -> set[str]:
    """Get the set of tradable Alpaca symbols.

    Returns Alpaca-format symbols (e.g. 'BRK.B', not 'BRK-B').
    Cached in memory and on disk. Refreshed at most once per day.

    Args:
        force_refresh: Skip cache and fetch fresh from Alpaca API.

    Returns:
        Set of tradable symbols. Empty set on failure (never blocks).
    """
    global _tradable_set, _shortable_set, _last_fetch_ts

    with _cache_lock:
        now = time.time()

        # Return in-memory cache if fresh
        if (not force_refresh
                and _tradable_set is not None
                and (now - _last_fetch_ts) < _CACHE_TTL_HOURS * 3600):
            return _tradable_set

        # Try disk cache
        if not force_refresh:
            cached = _load_cache()
            if cached:
                _tradable_set, _shortable_set = cached
                _last_fetch_ts = now
                return _tradable_set

        # Fetch from API
        symbols, shortable = _fetch_from_alpaca()
        if symbols:
            _tradable_set, _shortable_set = symbols, shortable
            _last_fetch_ts = now
            _save_cache(symbols, shortable)
            return _tradable_set

        # Fall back to stale disk cache (better than nothing)
        if _tradable_set is not None:
            logger.warning("Using stale in-memory tradable set (%d symbols)", len(_tradable_set))
            return _tradable_set

        # Last resort: try loading stale disk cache ignoring TTL
        try:
            if _CACHE_FILE.exists():
                with open(_CACHE_FILE) as f:
                    data = json.load(f)
                _tradable_set = set(data["symbols"])
                _shortable_set = set(data.get("shortable", []))
                _last_fetch_ts = now
                logger.warning("Using stale disk cache (%d symbols)", len(_tradable_set))
                return _tradable_set
        except Exception:
            pass

        logger.error("No tradable asset data available — returning empty set")
        return set()


def get_shortable_set(force_refresh: bool = False) -> set[str]:
    """Set of Alpaca symbols that are currently SHORTABLE (Alpaca has borrow).
    Shares the same cache/refresh as get_tradable_set (one fetch populates both)."""
    get_tradable_set(force_refresh=force_refresh)  # ensures both sets are loaded
    return _shortable_set if _shortable_set is not None else set()


def is_shortable(ticker: str) -> bool:
    """True if `ticker` can be sold short on Alpaca. FAILS OPEN: if shortable data is
    unavailable (empty set, e.g. an old cache), returns True so we degrade to the broker
    rejecting the order rather than silently refusing a legitimately-shortable name."""
    shortable = get_shortable_set()
    if not shortable:
        return True
    return to_alpaca(ticker) in shortable


def is_tradable(ticker: str) -> bool:
    """Check if a ticker is tradable on Alpaca.

    Accepts both Atlas format (BRK-B) and Alpaca format (BRK.B).
    Returns True if the asset is active and tradable, False otherwise.
    Returns True if the tradable set is unavailable (fail-open).
    """
    tradable = get_tradable_set()
    if not tradable:
        # Fail open — don't block trades if we can't verify
        return True
    alpaca_sym = to_alpaca(ticker)
    return alpaca_sym in tradable


def filter_tradable(tickers: list[str]) -> tuple[list[str], list[str]]:
    """Split tickers into (tradable, untradable) lists.

    Args:
        tickers: List of Atlas-format tickers.

    Returns:
        (tradable_tickers, untradable_tickers) — both in Atlas format.
    """
    tradable_set = get_tradable_set()
    if not tradable_set:
        # Fail open
        return tickers, []

    ok, bad = [], []
    for t in tickers:
        alpaca_sym = to_alpaca(t)
        if alpaca_sym in tradable_set:
            ok.append(t)
        else:
            bad.append(t)
    return ok, bad
