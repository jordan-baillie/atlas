"""Regression test: no NEW ticker appears in more than one market universe.

Pre-existing intentional overlaps are documented in KNOWN_OVERLAPS below.
The test fails if a NEW overlap is introduced (e.g., duplicate ADDED without reason), guarding against state-pollution
bug-class "CROSS-MARKET HWM INCOMPATIBILITY" (see 2026-05-01 incident).
protective-order sync.

Run with: python -m pytest tests/test_universe_disjointness.py -v
"""
from __future__ import annotations

import sys
from itertools import combinations
from pathlib import Path
from typing import Dict, FrozenSet, Set, Tuple

import pytest

ATLAS_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ATLAS_ROOT))

from markets.registry import MarketRegistry  # noqa: E402


# ---------------------------------------------------------------------------
# Known intentional overlaps (pre-existing by design).
# Format: frozenset({market_a, market_b}) → frozenset({ticker, ...})
# ---------------------------------------------------------------------------
# These overlaps are INTENTIONAL:
#   asx ∩ sp500           — cross-listed companies (CCL, DOW, PRU, RMD, ALL)
#   commodity_etfs ∩ gold_etfs  — GLD is both a commodity and a gold ETF
#   defensive_etfs ∩ sector_etfs — XLP/XLU sit in both sector and defensive buckets
#   commodity_etfs ∩ sp500  — FCX (Freeport-McMoRan) is an S&P 500 constituent AND a
#     commodity equity proxy for copper exposure (universe/definitions.py commodity_etfs).
#     The FIX-PMEQ-001 per-market equity formula requires FCX to appear in
#     markets/etf_markets.py CommodityETFsMarket so that _refresh_from_broker() loads
#     it into LivePortfolio.positions for commodity_etfs — consistent with how
#     market_equity_history snapshots attribute FCX via derive_universe().
#     (Prior Task #282 removed FCX from commodity_etfs causing a phantom HALT on
#     2026-05-01 when the snapshot-HWM included FCX but the live formula did not.)
KNOWN_OVERLAPS: dict[FrozenSet[str], FrozenSet[str]] = {
    frozenset({"asx", "sp500"}): frozenset({"ALL", "CCL", "DOW", "PRU", "RMD"}),
    frozenset({"commodity_etfs", "gold_etfs"}): frozenset({"GLD"}),
    frozenset({"defensive_etfs", "sector_etfs"}): frozenset({"XLP", "XLU"}),
    frozenset({"commodity_etfs", "sp500"}): frozenset({"FCX"}),
}


def _get_all_universe_tickers() -> Dict[str, Set[str]]:
    """Return {market_id: set(tickers)} for every registered market."""
    result: Dict[str, Set[str]] = {}
    for market_id in MarketRegistry.list_ids():
        market = MarketRegistry.get(market_id)
        try:
            tickers = market.get_universe_tickers()
        except NotImplementedError:
            continue
        if tickers:
            result[market_id] = set(tickers)
    return result


class TestUniverseDisjointness:
    """Every market pair must not have unexpected shared tickers."""

    def test_no_unexpected_cross_market_duplicates(self) -> None:
        """For every (mkt_a, mkt_b) pair, assert only known overlaps exist.

        Fails immediately if a NEW ticker appears in more than one market.
        Helpful failure message names the pair and the unexpected tickers.
        """
        universes = _get_all_universe_tickers()
        market_ids = sorted(universes.keys())

        failures = []
        for mkt_a, mkt_b in combinations(market_ids, 2):
            overlap = universes[mkt_a] & universes[mkt_b]
            if not overlap:
                continue
            key = frozenset({mkt_a, mkt_b})
            allowed = KNOWN_OVERLAPS.get(key, frozenset())
            unexpected = overlap - allowed
            if unexpected:
                failures.append(
                    f"Tickers {sorted(unexpected)} appear in both {mkt_a} and {mkt_b} "
                    f"(not in KNOWN_OVERLAPS — add intentionally or remove the duplicate)"
                )

        if failures:
            failure_msg = "\n".join(failures)
            pytest.fail(
                f"Unexpected universe overlap(s) detected:\n{failure_msg}\n\n"
                "Fix: assign each ticker to exactly one market, or add it to "
                "KNOWN_OVERLAPS in tests/test_universe_disjointness.py with a comment."
            )

    def test_fcx_is_in_commodity_etfs(self) -> None:
        """Regression: FCX must be in commodity_etfs (copper equity proxy).

        FCX (Freeport-McMoRan) is listed in universe/definitions.py under commodity_etfs
        because it's a copper/commodity proxy.  It is also an S&P 500 constituent (sp500),
        making it an intentional overlap documented in KNOWN_OVERLAPS.

        HISTORY: Task #282 (2026-04-29) removed FCX from commodity_etfs, believing
        "sp500 owns it".  This caused a phantom HALT on 2026-05-01 because
        market_equity_history snapshots attributed FCX MV to commodity_etfs via
        derive_universe() but _refresh_from_broker() did not load FCX for commodity_etfs.
        The fix (2026-05-01) restored FCX to CommodityETFsMarket.get_universe_tickers()
        so the live formula is consistent with the snapshot attribution.
        """
        commodity_etfs = MarketRegistry.get("commodity_etfs")
        tickers = set(commodity_etfs.get_universe_tickers())
        assert "FCX" in tickers, (
            "FCX must be in CommodityETFsMarket.get_universe_tickers() — it is a commodity "
            "equity proxy listed in universe/definitions.py and the FIX-PMEQ-001 formula "
            "requires this for snapshot-live consistency.  See incident 2026-05-01."
        )

    def test_fcx_commodity_etfs_overlap_is_documented(self) -> None:
        """FCX appearing in both sp500 and commodity_etfs must be in KNOWN_OVERLAPS."""
        sp500 = set(MarketRegistry.get("sp500").get_universe_tickers())
        commodity = set(MarketRegistry.get("commodity_etfs").get_universe_tickers())
        overlap = sp500 & commodity
        key = frozenset({"commodity_etfs", "sp500"})
        if "FCX" in overlap:
            assert key in KNOWN_OVERLAPS, (
                "FCX found in both sp500 and commodity_etfs but not in KNOWN_OVERLAPS. "
                "Add it with rationale or remove the duplicate."
            )
            assert "FCX" in KNOWN_OVERLAPS[key], (
                "FCX in commodity_etfs∩sp500 but not listed in KNOWN_OVERLAPS entry."
            )

    def test_registered_markets_are_non_empty(self) -> None:
        """Sanity: every market must have at least one ticker."""
        universes = _get_all_universe_tickers()
        empty = [mid for mid, tickers in universes.items() if len(tickers) == 0]
        assert not empty, f"Markets with zero tickers: {empty}"

    def test_known_overlaps_still_exist(self) -> None:
        """Guard: if a known overlap is resolved, remove it from KNOWN_OVERLAPS.

        This prevents KNOWN_OVERLAPS from silently accumulating dead entries
        that could hide real bugs.
        """
        universes = _get_all_universe_tickers()
        stale_entries = []
        for pair_key, expected_overlap in KNOWN_OVERLAPS.items():
            mkt_a, mkt_b = sorted(pair_key)
            if mkt_a not in universes or mkt_b not in universes:
                stale_entries.append(f"Market {pair_key} no longer registered")
                continue
            actual_overlap = universes[mkt_a] & universes[mkt_b]
            missing = expected_overlap - actual_overlap
            if missing:
                stale_entries.append(
                    f"KNOWN_OVERLAPS entry {pair_key} lists {sorted(missing)} "
                    f"but those tickers no longer overlap — remove stale entry"
                )
        if stale_entries:
            pytest.fail(
                "Stale KNOWN_OVERLAPS entries:\n" + "\n".join(stale_entries) + "\n\n"
                "Update KNOWN_OVERLAPS in tests/test_universe_disjointness.py."
            )
