"""Canonical SPDR sector ETF constants — single source of truth.

Resolves a previously-duplicated set of hardcoded tuples across signal files
(audit D2, 2026-05-14).

Design notes
------------
* ``SECTOR_ETFS`` is a tuple of the 11 SPDR ticker symbols (ordered
  alphabetically by ticker for stable iteration).
* ``SECTOR_ETF_NAMES`` is the companion dict mapping ticker → sector name,
  used by callers that need the human-readable label.
* ``DEFENSIVE_ETFS_PURE`` vs ``DEFENSIVE_ETFS_INCLUSIVE`` are intentionally
  different; see the inline docstrings for the thesis distinction.
"""
from __future__ import annotations

# ---------------------------------------------------------------------------
# All 11 SPDR Select Sector ETFs (S&P 500 GICS sectors)
# ---------------------------------------------------------------------------

SECTOR_ETF_NAMES: dict[str, str] = {
    "XLB": "Materials",
    "XLC": "Communication Services",
    "XLE": "Energy",
    "XLF": "Financials",
    "XLI": "Industrials",
    "XLK": "Technology",
    "XLP": "Consumer Staples",
    "XLRE": "Real Estate",
    "XLU": "Utilities",
    "XLV": "Health Care",
    "XLY": "Consumer Discretionary",
}

#: Tuple of the 11 SPDR ticker symbols (ordered, canonical).
SECTOR_ETFS: tuple[str, ...] = tuple(SECTOR_ETF_NAMES)

# ---------------------------------------------------------------------------
# Defensive subsets — two intentionally divergent definitions
# ---------------------------------------------------------------------------

#: **Pure defensive** — Utilities (XLU) + Consumer Staples (XLP) only.
#:
#: Used by ``signals/sector_rotation.py`` (price momentum thesis).
#:
#: Rationale: These two sectors exhibit clear bond-proxy, low-beta
#: behaviour whose *price leadership* is an unambiguous risk-off signal.
#: Healthcare (XLV) is deliberately excluded: its biotech/pharma exposure
#: means XLV can lead on ROC in bull markets (growth-driven), creating
#: false-positive risk-off flags if included here.
DEFENSIVE_ETFS_PURE: frozenset[str] = frozenset({"XLU", "XLP"})

#: **Inclusive defensive** — Utilities + Consumer Staples + Health Care.
#:
#: Used by ``signals/etf_flows.py`` (volume flow thesis).
#:
#: Rationale: When measuring *institutional volume flows* during risk-off
#: rotations, healthcare (XLV) is a legitimate destination — pension funds
#: and conservative allocators routinely increase XLV exposure alongside
#: XLU/XLP.  Volume surges in all three together constitute a genuine
#: institutional defensive-rotation signal.
DEFENSIVE_ETFS_INCLUSIVE: frozenset[str] = frozenset({"XLU", "XLP", "XLV"})

# ---------------------------------------------------------------------------
# Cyclical subset (volume flow thesis — etf_flows.py)
# ---------------------------------------------------------------------------

#: High-beta cyclical sectors used to detect risk-on institutional flows.
CYCLICAL_ETFS: frozenset[str] = frozenset({"XLK", "XLF", "XLI", "XLY"})
