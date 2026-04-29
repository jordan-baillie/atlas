"""Sector concentration cap filter.

Enforces ``max_sector_concentration``: rejects candidates that would push any
single sector over the configured cap. Used as a pre-pass in plan generation
to prevent batches of same-sector signals all passing when 0 existing positions
are open in that sector.

Design mirrors ``risk/gross_exposure_guard.py``:
  - Fail-open on bad input (log warning, return all candidates)
  - Sort by confidence DESC before applying cap so best signals survive
  - Bucket None/empty sector as "unknown" (its own cap bucket)
  - Case-insensitive sector comparison
  - Log at WARNING on every rejection with ticker, sector, count, cap
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def _normalise_sector(raw: Any) -> str:
    """Return lowercase sector string; None/empty → 'unknown'."""
    s = (raw or "").strip()
    return s.lower() if s else "unknown"


def apply_sector_cap(
    candidates: list[dict[str, Any]],
    existing_positions: list[dict[str, Any]],
    cap: int,
    *,
    _logger: logging.Logger | None = None,
) -> list[dict[str, Any]]:
    """Filter candidates, rejecting those that would breach max_sector_concentration."""
    log = _logger or logger

    sector_count: dict[str, int] = {}
    for p in existing_positions:
        s = _normalise_sector(p.get("sector"))
        sector_count[s] = sector_count.get(s, 0) + 1

    accepted: list[dict[str, Any]] = []

    for c in sorted(candidates, key=lambda x: x.get("confidence", 0.0), reverse=True):
        raw_sector = c.get("sector")
        s = _normalise_sector(raw_sector)
        cur = sector_count.get(s, 0)

        if cur >= cap:
            log.warning(
                "SECTOR_CAP REJECTED: ticker=%s sector=%s current_count=%d cap=%d "
                "(%d already in this sector — signal dropped)",
                c.get("ticker"),
                raw_sector if raw_sector else "unknown",
                cur,
                cap,
                cur,
            )
            continue

        accepted.append(c)
        sector_count[s] = cur + 1

    return accepted
