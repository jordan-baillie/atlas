"""
universe/definitions.py — Multi-universe asset definitions for Atlas.

Defines the 6 tradeable universes used by the regime-aware portfolio engine.
Each universe can be static (fixed ticker list) or dynamic (requires a builder).

Universe roles in the regime model (see regime/states.py):
    sp500           — S&P 500 top constituents; growth/momentum plays.
    sector_etfs     — SPDR sector ETFs; rotation and mean-reversion signals.
    treasury_etfs   — Treasury/bond ETFs; safe-haven in bear regimes.
    commodity_etfs  — Commodity exposure; inflation hedge, risk-on plays.
    gold_etfs       — Gold miners and bullion ETFs; crisis hedge.
    defensive_etfs  — Inverse/low-vol/dividend ETFs; capital preservation.

Usage
-----
    from universe.definitions import (
        UNIVERSES,
        get_universe,
        get_universe_tickers,
        get_all_etf_tickers,
        list_universes,
    )

    names = list_universes()
    defn  = get_universe("sector_etfs")
    tickers = get_universe_tickers("sector_etfs")
    all_etfs = get_all_etf_tickers()

Note on duplicate tickers
-------------------------
Some tickers appear in multiple universes intentionally — the same asset can
serve different strategic roles:
    XLU   — sector rotation (sector_etfs) AND defensive hedge (defensive_etfs)
    XLP   — sector rotation (sector_etfs) AND defensive hedge (defensive_etfs)
    GLD   — commodity exposure (commodity_etfs) AND gold hedge (gold_etfs)

``get_all_etf_tickers()`` deduplicates these for data-ingestion purposes;
per-universe membership intentionally retains the duplicates.
"""
from __future__ import annotations

from typing import Any

# ──────────────────────────────────────────────────────────────────────────────
# Universe definitions
# ──────────────────────────────────────────────────────────────────────────────

#: Type alias for a single universe definition block.
UniverseDefinition = dict[str, Any]

UNIVERSES: dict[str, UniverseDefinition] = {
    # ── Growth / equity ───────────────────────────────────────────────────────
    "sp500": {
        # Dynamic — top N S&P 500 constituents by liquidity.
        # Requires universe.builder.build_universe() to resolve tickers.
        "method": "sp500_constituents",
        "top_n": 100,
        "description": "Top 100 S&P 500 constituents by daily traded value.",
    },
    # ── Sector rotation ───────────────────────────────────────────────────────
    "sector_etfs": {
        # 11 SPDR Select Sector ETFs covering the full S&P 500 by GICS sector.
        "method": "static",
        "tickers": [
            "XLF",   # Financials
            "XLE",   # Energy
            "XLK",   # Technology
            "XLV",   # Health Care
            "XLI",   # Industrials
            "XLC",   # Communication Services
            "XLY",   # Consumer Discretionary
            "XLP",   # Consumer Staples  ← also in defensive_etfs
            "XLU",   # Utilities          ← also in defensive_etfs
            "XLB",   # Materials
            "XLRE",  # Real Estate
        ],
        "description": "SPDR Select Sector ETFs (all 11 GICS sectors).",
    },
    # ── Safe-haven fixed income ───────────────────────────────────────────────
    "treasury_etfs": {
        # Core US Treasury and investment-grade bond ETFs.
        "method": "static",
        "tickers": [
            "TLT",   # iShares 20+ Year Treasury Bond
            "IEF",   # iShares 7-10 Year Treasury Bond
            "SHY",   # iShares 1-3 Year Treasury Bond
            "TIP",   # iShares TIPS Bond (inflation-protected)
            "BND",   # Vanguard Total Bond Market
        ],
        "description": "US Treasury and investment-grade bond ETFs for safe-haven exposure.",
    },
    # ── Commodities ───────────────────────────────────────────────────────────
    "commodity_etfs": {
        # Broad commodity + sector commodity ETFs; inflation & risk-on plays.
        "method": "static",
        "tickers": [
            "GLD",   # SPDR Gold Shares          ← also in gold_etfs
            "SLV",   # iShares Silver Trust
            "USO",   # United States Oil Fund
            "XOP",   # SPDR S&P Oil & Gas E&P
            "CORN",  # Teucrium Corn Fund
            "DBA",   # Invesco DB Agriculture
            "DBB",   # Invesco DB Base Metals
            "UNG",   # United States Natural Gas
            "CCJ",   # Cameco (uranium, equity proxy)
            "FCX",   # Freeport-McMoRan (copper, equity proxy)
        ],
        "description": "Commodity ETFs and equity proxies for energy, metals, and agriculture.",
    },
    # ── Gold / crisis hedge ───────────────────────────────────────────────────
    "gold_etfs": {
        # Gold bullion and gold-miner ETFs; safe-haven crisis hedge.
        "method": "static",
        "tickers": [
            "GLD",   # SPDR Gold Shares (physical)  ← also in commodity_etfs
            "IAU",   # iShares Gold Trust (physical, lower cost)
            "GDX",   # VanEck Gold Miners ETF
            "GDXJ",  # VanEck Junior Gold Miners ETF
        ],
        "description": "Gold bullion and gold miner ETFs for crisis hedging.",
    },
    # ── Defensive / capital preservation ─────────────────────────────────────
    "defensive_etfs": {
        # Inverse/low-vol/quality ETFs; capital preservation in bear regimes.
        "method": "static",
        "tickers": [
            "SH",    # ProShares Short S&P 500 (inverse)
            "PSQ",   # ProShares Short QQQ (inverse)
            "XLU",   # Utilities SPDR        ← also in sector_etfs
            "XLP",   # Consumer Staples SPDR ← also in sector_etfs
            "VIG",   # Vanguard Dividend Appreciation
            "USMV",  # iShares MSCI USA Min Vol Factor
        ],
        "description": "Inverse, low-volatility, and dividend ETFs for capital preservation.",
    },
}

# ──────────────────────────────────────────────────────────────────────────────
# Helper functions
# ──────────────────────────────────────────────────────────────────────────────


def list_universes() -> list[str]:
    """Return the names of all defined universes.

    Returns:
        List of universe name strings in definition order.

    Example:
        >>> list_universes()
        ['sp500', 'sector_etfs', 'treasury_etfs', 'commodity_etfs', 'gold_etfs', 'defensive_etfs']
    """
    return list(UNIVERSES.keys())


def get_universe(name: str) -> UniverseDefinition:
    """Return the definition dict for a named universe.

    Args:
        name: Universe name (e.g. ``"sector_etfs"``).

    Returns:
        The universe definition dict containing at minimum ``"method"`` and
        either ``"tickers"`` (static) or ``"top_n"`` (sp500_constituents).

    Raises:
        KeyError: If ``name`` is not a recognised universe.

    Example:
        >>> get_universe("gold_etfs")["method"]
        'static'
    """
    if name not in UNIVERSES:
        known = ", ".join(UNIVERSES.keys())
        raise KeyError(
            f"Unknown universe {name!r}. Known universes: {known}"
        )
    return UNIVERSES[name]


def get_universe_tickers(name: str) -> list[str]:
    """Return the ticker list for a static universe.

    For ``"sp500"`` (method ``"sp500_constituents"``) this function raises
    ``ValueError`` because the ticker list is dynamic and requires
    ``universe.builder.build_universe()`` to resolve.

    Args:
        name: Universe name.

    Returns:
        List of ticker strings for static universes.

    Raises:
        KeyError: If ``name`` is not a recognised universe.
        ValueError: If the universe method is not ``"static"`` (i.e. sp500).

    Example:
        >>> "GLD" in get_universe_tickers("gold_etfs")
        True
    """
    defn = get_universe(name)  # raises KeyError if unknown
    if defn["method"] != "static":
        raise ValueError(
            f"Universe {name!r} uses method {defn['method']!r} — tickers are "
            f"dynamic and must be resolved via universe.builder.build_universe()."
        )
    return list(defn["tickers"])


def get_all_etf_tickers() -> list[str]:
    """Return a deduplicated list of ALL tickers across every static universe.

    The ``sp500`` universe is excluded because it is dynamic.  Deduplication
    preserves first-seen order (i.e. the ticker is kept from whichever static
    universe lists it first).

    Returns:
        Deduplicated list of ETF ticker strings in first-seen order.

    Example:
        >>> tickers = get_all_etf_tickers()
        >>> "GLD" in tickers    # appears in both commodity_etfs and gold_etfs
        True
        >>> tickers.count("GLD")
        1
    """
    seen: set[str] = set()
    result: list[str] = []
    for name, defn in UNIVERSES.items():
        if defn["method"] != "static":
            continue
        for ticker in defn["tickers"]:
            if ticker not in seen:
                seen.add(ticker)
                result.append(ticker)
    return result
