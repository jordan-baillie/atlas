"""Ticker → universe membership resolver.

Used at INSERT time to derive the correct universe for a trade row,
instead of defaulting to 'sp500' (which is wrong for ETFs held under
the sp500 broker but belonging to a different universe per definitions).
"""
from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)

_UNAMBIGUOUS_CACHE: dict[str, str] = {}
_ALL_MEMBERSHIP_CACHE: dict[str, set[str]] = {}


def _build_membership() -> dict[str, set[str]]:
    """Ticker → set of universes it belongs to. Cached."""
    global _ALL_MEMBERSHIP_CACHE
    if _ALL_MEMBERSHIP_CACHE:
        return _ALL_MEMBERSHIP_CACHE
    mp: dict[str, set[str]] = {}
    from universe.definitions import UNIVERSES
    for uname, udef in UNIVERSES.items():
        if udef.get("method") == "static":
            for t in udef.get("tickers", []):
                mp.setdefault(t, set()).add(uname)
    # sp500 is dynamic — resolve via builder
    try:
        from universe.builder import get_universe_tickers
        for t in get_universe_tickers("sp500"):
            mp.setdefault(t, set()).add("sp500")
    except Exception as exc:
        logger.debug("sp500 dynamic fetch failed in membership cache: %s", exc)
    _ALL_MEMBERSHIP_CACHE = mp
    return mp


def derive_universe(ticker: str, hint: Optional[str] = None) -> Optional[str]:
    """Return the canonical universe for *ticker*.

    Resolution:
    1. If ticker belongs to exactly ONE universe → return it.
    2. If ticker belongs to MULTIPLE universes AND *hint* is one of them → return hint.
    3. If ticker belongs to MULTIPLE universes AND hint is not a match → return hint if it's
       non-empty and looks valid, else the first membership alphabetically (stable).
    4. If ticker is NOT in any known universe → return hint if provided (to preserve legacy
       market_id), else None. Logs a WARN.

    Returns None when the caller should leave `universe` NULL in SQLite and log.
    NEVER silently returns 'sp500' as a fallback.
    """
    if not ticker:
        return hint or None
    mp = _build_membership()
    memberships = mp.get(ticker, set())
    if len(memberships) == 1:
        (only,) = memberships
        return only
    if len(memberships) > 1:
        if hint and hint in memberships:
            return hint
        # Prefer hint if it's a real universe name; else deterministic first.
        from universe.definitions import UNIVERSES
        if hint and hint in UNIVERSES:
            # hint is valid but ticker isn't in it — log and still return hint
            logger.warning(
                "derive_universe: %s is in %s but hint=%s disagrees — using hint",
                ticker, sorted(memberships), hint,
            )
            return hint
        return sorted(memberships)[0]
    # Not in any known universe
    logger.warning(
        "derive_universe: %s has NO known universe membership (hint=%s) — using hint or None",
        ticker, hint,
    )
    return hint or None


def clear_cache() -> None:
    """Clear the membership cache (for tests)."""
    global _ALL_MEMBERSHIP_CACHE
    _ALL_MEMBERSHIP_CACHE = {}
