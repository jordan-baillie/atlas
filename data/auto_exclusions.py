"""
Auto-exclusion manager for stale/delisted tickers.

Maintains a JSON file at config/auto_excluded_tickers.json that tracks
tickers automatically excluded from the pipeline when they fail to return
fresh data. Separate from manual config exclusions.
"""
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Set

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
AUTO_EXCLUSION_FILE = PROJECT_ROOT / "config" / "auto_excluded_tickers.json"


def _load_exclusions() -> Dict:
    """Load auto-exclusion data from disk."""
    if AUTO_EXCLUSION_FILE.exists():
        try:
            with open(AUTO_EXCLUSION_FILE, "r") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("Failed to load auto-exclusions: %s", e)
    return {"excluded": {}, "version": 1}


def _save_exclusions(data: Dict) -> None:
    """Atomically save auto-exclusion data to disk."""
    AUTO_EXCLUSION_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = AUTO_EXCLUSION_FILE.with_suffix(".json.tmp")
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2, default=str)
    import os
    os.replace(str(tmp), str(AUTO_EXCLUSION_FILE))


def add_exclusion(ticker: str, market_id: str, reason: str, last_data_date: Optional[str] = None) -> None:
    """Add a ticker to the auto-exclusion list."""
    data = _load_exclusions()
    data["excluded"][ticker.upper()] = {
        "market_id": market_id,
        "reason": reason,
        "excluded_at": datetime.now().isoformat(),
        "last_data_date": last_data_date,
        "recovery_attempts": 0,
        "last_recovery_attempt": None,
    }
    _save_exclusions(data)
    logger.info("Auto-excluded %s from %s: %s", ticker, market_id, reason)


def remove_exclusion(ticker: str) -> bool:
    """Remove a ticker from the auto-exclusion list. Returns True if it was found."""
    data = _load_exclusions()
    ticker_upper = ticker.upper()
    if ticker_upper in data["excluded"]:
        del data["excluded"][ticker_upper]
        _save_exclusions(data)
        logger.info("Removed auto-exclusion for %s", ticker)
        return True
    return False


def get_excluded_tickers(market_id: Optional[str] = None) -> Set[str]:
    """Return set of auto-excluded ticker symbols, optionally filtered by market."""
    data = _load_exclusions()
    if market_id:
        return {t for t, info in data["excluded"].items() if info.get("market_id") == market_id}
    return set(data["excluded"].keys())


def get_exclusion_details() -> Dict:
    """Return full exclusion data including metadata."""
    return _load_exclusions()


def update_recovery_attempt(ticker: str) -> None:
    """Increment recovery attempt counter for a ticker."""
    data = _load_exclusions()
    ticker_upper = ticker.upper()
    if ticker_upper in data["excluded"]:
        data["excluded"][ticker_upper]["recovery_attempts"] = data["excluded"][ticker_upper].get("recovery_attempts", 0) + 1
        data["excluded"][ticker_upper]["last_recovery_attempt"] = datetime.now().isoformat()
        _save_exclusions(data)


def quarantine_cache(ticker: str, market_id: str) -> Optional[Path]:
    """Move a stale ticker's cache file to a quarantine directory. Returns new path or None."""
    from data.ingest import _cache_path
    cache_file = _cache_path(ticker, market_id)
    if not cache_file.exists():
        return None
    quarantine_dir = cache_file.parent / "quarantine"
    quarantine_dir.mkdir(exist_ok=True)
    dest = quarantine_dir / cache_file.name
    cache_file.rename(dest)
    logger.info("Quarantined cache for %s: %s -> %s", ticker, cache_file, dest)
    return dest
