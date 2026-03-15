"""Point-in-Time S&P 500 membership reconstruction.

Tracks historical adds/removes to eliminate survivorship bias in backtesting.
Uses current membership as a base and walks backwards through known changes
to reconstruct the index at any historical date.
"""
import csv
import logging
from datetime import date, datetime
from pathlib import Path
from typing import Dict, List, Optional, Set, Union

logger = logging.getLogger(__name__)

CHANGES_FILE = Path(__file__).parent / "sp500_changes.csv"


def load_changes() -> List[Dict]:
    """Load S&P 500 membership changes from CSV.

    Returns:
        List of dicts with keys: date, ticker, action, replaced, notes.
        Sorted by date descending (most recent first).
    """
    if not CHANGES_FILE.exists():
        logger.warning(f"S&P 500 changes file not found: {CHANGES_FILE}")
        return []

    changes = []
    with open(CHANGES_FILE, "r") as f:
        reader = csv.DictReader(f)
        for row in reader:
            row["date"] = datetime.strptime(row["date"], "%Y-%m-%d").date()
            changes.append(row)

    # Sort most recent first (for backward walk)
    changes.sort(key=lambda x: x["date"], reverse=True)
    logger.debug(f"Loaded {len(changes)} S&P 500 membership changes")
    return changes


def get_current_members() -> Set[str]:
    """Get current S&P 500 members from universe cache.

    Falls back to a hardcoded snapshot if the cache doesn't exist.
    """
    cache_path = Path(__file__).parent.parent / "data" / "processed" / "sp500" / "universe.json"
    if cache_path.exists():
        import json
        with open(cache_path) as f:
            data = json.load(f)
        return set(data.get("tickers", []))

    # Fallback: load from top_liquid tickers if available
    try:
        from universe.builder import get_universe_tickers
        return set(get_universe_tickers("sp500"))
    except Exception:
        logger.warning("Could not load current S&P 500 members from cache or builder")
        return set()


def _parse_date(d: Union[str, date, datetime]) -> date:
    """Parse various date formats to date object."""
    if isinstance(d, datetime):
        return d.date()
    if isinstance(d, date):
        return d
    if isinstance(d, str):
        return datetime.strptime(d, "%Y-%m-%d").date()
    raise TypeError(f"Cannot parse date: {d} (type {type(d)})")


def get_members_at_date(
    target_date: Union[str, date],
    current_members: Optional[Set[str]] = None,
) -> Set[str]:
    """Reconstruct S&P 500 membership at a historical date.

    Algorithm:
        1. Start with current members
        2. Walk changes backwards from today to target_date
        3. For each ADD after target_date: remove ticker (wasn't in index yet)
        4. For each REMOVE after target_date: add ticker back (was still in)

    Args:
        target_date: Date to reconstruct membership for (str or date).
        current_members: Override current membership set (for testing).

    Returns:
        Set of tickers that were in the S&P 500 on target_date.
    """
    target = _parse_date(target_date)

    if current_members is None:
        current_members = get_current_members()

    if not current_members:
        logger.warning("No current members available, returning empty set")
        return set()

    members = set(current_members)
    changes = load_changes()

    applied = 0
    for change in changes:
        change_date = change["date"]
        if change_date <= target:
            break  # Only process changes AFTER target_date

        action = change["action"].upper()
        ticker = change["ticker"].upper()

        if action == "ADD":
            # This ticker was added after our target date, so remove it
            members.discard(ticker)
        elif action == "REMOVE":
            # This ticker was removed after our target date, so add it back
            members.add(ticker)
        applied += 1

    logger.info(
        f"PIT reconstruction: {len(members)} members at {target} "
        f"({applied} changes applied from {len(changes)} total)"
    )
    return members


def get_change_count_between(
    start_date: Union[str, date],
    end_date: Union[str, date],
) -> int:
    """Count membership changes between two dates (inclusive)."""
    start = _parse_date(start_date)
    end = _parse_date(end_date)
    changes = load_changes()
    return sum(1 for c in changes if start <= c["date"] <= end)
