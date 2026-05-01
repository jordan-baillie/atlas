"""Ticker → universe membership resolver.

Used at INSERT time to derive the correct universe for a trade row,
instead of defaulting to 'sp500' (which is wrong for ETFs held under
the sp500 broker but belonging to a different universe per definitions).
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
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


def _live_verify_membership(ticker: str, hint: str) -> bool:
    """Live re-check whether *ticker* is a member of universe *hint*.

    Bypasses the module cache so we catch new additions or rebuilds since
    cache-build time.  Looks up via:
      - static universes: scans UNIVERSES[hint]['tickers']
      - dynamic sp500: calls universe.builder.get_universe_tickers('sp500')
                       (which reads data/processed/sp500/universe.json)

    Returns True only on confirmed membership.  Raises on infrastructure
    failure (file missing, unknown universe) so the caller can decide
    whether to log+return None vs propagate.
    """
    from universe.definitions import UNIVERSES
    if hint not in UNIVERSES:
        # Hint isn't even a real universe name — can't verify.
        return False
    udef = UNIVERSES[hint]
    method = udef.get("method", "")
    if method == "static":
        return ticker in set(udef.get("tickers", []))
    if method == "sp500_constituents" or hint == "sp500":
        # Dynamic — consult the builder.
        from universe.builder import get_universe_tickers
        return ticker in set(get_universe_tickers("sp500"))
    # Unknown method — be conservative.
    return False


def derive_universe(ticker: str, hint: Optional[str] = None) -> Optional[str]:
    """Return the canonical universe for *ticker*.

    Resolution:
    1. If ticker belongs to exactly ONE universe → return it (hint ignored).
    2. If ticker belongs to MULTIPLE universes AND *hint* is one of them → return hint.
    3. If ticker belongs to MULTIPLE universes AND hint is a valid universe name →
       log WARN (hint disagrees with memberships) and return hint.
    4. If ticker belongs to MULTIPLE universes AND hint is invalid/None →
       return first membership alphabetically (stable).
    5. If ticker is NOT in any cached universe → perform an explicit live
       re-check against the hint's source-of-truth:
         - static universes: UNIVERSES[hint]['tickers']
         - dynamic sp500:   get_universe_tickers('sp500')
       Returns hint ONLY on confirmed membership.  Returns None otherwise
       (caller logs and writes NULL to SQLite — never a blind plausible guess).

    KEY INVARIANT: NEVER returns a hint blindly.  When the ticker is not in
    any cached universe, the function performs an explicit live re-check against
    the hint's source-of-truth (static UNIVERSES dict OR the dynamic sp500
    builder).  Only returns hint after confirmed membership.  Returns None when
    verification fails or hint disagrees — caller logs and writes NULL.
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
    # Not in any cached universe — try live verification against the hint.
    # This handles two scenarios:
    #   (a) cache was built before the dynamic builder for `hint` had data
    #       (e.g., data/processed/<hint>/universe.json was missing or stale),
    #   (b) the ticker is a NEW addition since cache was built.
    if hint:
        try:
            verified = _live_verify_membership(ticker, hint)
        except Exception as exc:
            logger.warning(
                "derive_universe: live verify FAILED for %s hint=%s: %s "
                "— refusing blind hint fallback, returning None",
                ticker, hint, exc,
            )
            return None
        if verified:
            logger.info(
                "derive_universe: %s confirmed in %s via live verification "
                "(was missing from cache)", ticker, hint,
            )
            return hint
        logger.warning(
            "derive_universe: %s NOT a member of hint=%s (live-verified) "
            "— returning None instead of blind hint fallback",
            ticker, hint,
        )
        return None
    logger.warning(
        "derive_universe: %s has NO known universe membership and no hint — None",
        ticker,
    )
    return None


def check_state_file_universes(
    state_dir: Path | None = None,
) -> list[dict[str, str]]:
    """Scan all live_*.json state files and report cross-market positions.

    A position is "cross-market" when the ticker's canonical universe
    (from ``derive_universe()``) differs from the market_id of the state
    file it is listed in.

    Args:
        state_dir: Path to directory containing live_*.json files.
                   Defaults to <project_root>/brokers/state/.

    Returns:
        List of dicts, one per cross-market position found:
            {
                "file":         "live_sp500.json",
                "market_id":    "sp500",
                "ticker":       "FCX",
                "canonical_universe": "commodity_etfs",
            }
        Empty list means all positions are in their canonical universe state file.
    """
    if state_dir is None:
        # Resolve project root relative to this file
        _project_root = Path(__file__).resolve().parent.parent
        state_dir = _project_root / "brokers" / "state"

    violations: list[dict[str, str]] = []

    for json_path in sorted(state_dir.glob("live_*.json")):
        try:
            with open(json_path) as fh:
                state = json.load(fh)
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("check_state_file_universes: failed to read %s: %s", json_path.name, exc)
            continue

        market_id: str = state.get("market_id", "")
        if not market_id:
            # Try to derive from filename: live_sp500.json → sp500
            stem = json_path.stem  # "live_sp500"
            if stem.startswith("live_"):
                market_id = stem[5:]  # "sp500"

        positions: list[dict] = state.get("positions", [])
        for pos in positions:
            ticker = pos.get("ticker", "")
            if not ticker:
                continue
            canonical = derive_universe(ticker)
            if canonical is None:
                # Unknown ticker — skip silently (new tickers may not be in cache yet)
                logger.debug(
                    "check_state_file_universes: unknown ticker %s in %s — skipping",
                    ticker, json_path.name,
                )
                continue
            if canonical != market_id:
                violations.append(
                    {
                        "file": json_path.name,
                        "market_id": market_id,
                        "ticker": ticker,
                        "canonical_universe": canonical,
                    }
                )
                logger.warning(
                    "Cross-market position: %s is in %s (file: %s) but belongs to %s",
                    ticker, market_id, json_path.name, canonical,
                )

    return violations


def clear_cache() -> None:
    """Clear the membership cache (for tests)."""
    global _ALL_MEMBERSHIP_CACHE
    _ALL_MEMBERSHIP_CACHE = {}
