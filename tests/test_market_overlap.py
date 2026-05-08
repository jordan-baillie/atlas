"""Regression test: no NEW cross-market ticker overlap beyond known exceptions.

This file is the task-#282 regression guard.  A companion file
``tests/test_universe_disjointness.py`` provides deeper invariant checks
(stale-entry detection, FCX-in-commodity-etfs guard, etc.); this file
focuses on the KNOWN_OVERLAPS whitelist pattern so the two are complementary.

KNOWN_OVERLAPS — intentional, documented, must not grow silently:
  asx ∩ sp500               — ASX/NYSE cross-listed companies (ALL/CCL/DOW/PRU/RMD)
  commodity_etfs ∩ gold_etfs — GLD is both a commodity proxy and a gold ETF
  defensive_etfs ∩ sector_etfs — XLP/XLU appear in both sector and defensive buckets
  commodity_etfs ∩ sp500    — FCX (Freeport-McMoRan, copper equity) is an S&P 500
                               constituent AND required in commodity_etfs for the
                               per-market equity formula.  Removing it (Task #282,
                               2026-04-29) caused a phantom HALT on 2026-05-01 because
                               market_equity_history snapshots attributed FCX to
                               commodity_etfs but _refresh_from_broker() did not load
                               it for that market.  FCX MUST remain in both markets.
                               See test_universe_disjointness.py::test_fcx_is_in_commodity_etfs.

NOTE on "crypto" market: the task spec referenced a crypto market but no such market
is registered in markets/registry.py (no CryptoMarket class exists).  This test
iterates all registered markets dynamically and will include crypto if/when added.

Run: pytest tests/test_market_overlap.py -v
"""
from __future__ import annotations

import sys
from itertools import combinations
from pathlib import Path
from typing import Dict, FrozenSet, Set

import pytest

ATLAS_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ATLAS_ROOT))

from markets.registry import MarketRegistry  # noqa: E402


# ---------------------------------------------------------------------------
# Known intentional overlaps — every entry requires a written rationale.
# frozenset({market_a, market_b}) → frozenset({ticker, ...})
# ---------------------------------------------------------------------------
KNOWN_OVERLAPS: dict[FrozenSet[str], FrozenSet[str]] = {
    # Cross-listed companies: ALL, CCL, DOW, PRU, RMD trade on both ASX and NYSE.
    frozenset({"asx", "sp500"}): frozenset({"ALL", "CCL", "DOW", "PRU", "RMD"}),

    # GLD is the canonical gold ETF AND a commodity proxy — legitimately in both.
    frozenset({"commodity_etfs", "gold_etfs"}): frozenset({"GLD"}),

    # XLP (Consumer Staples) and XLU (Utilities) are classified as both sector
    # and defensive ETFs; they sit in sector_etfs for completeness and in
    # defensive_etfs for regime-conditional reweighting.
    frozenset({"defensive_etfs", "sector_etfs"}): frozenset({"XLP", "XLU"}),

    # FCX (Freeport-McMoRan) is an S&P 500 constituent AND the primary copper
    # equity proxy in commodity_etfs.  Per-market equity formula (FIX-PMEQ-001)
    # requires FCX in CommodityETFsMarket.get_universe_tickers() so that
    # _refresh_from_broker() loads it when computing commodity_etfs equity.
    # Removing FCX from commodity_etfs caused phantom HALT on 2026-05-01.
    # DO NOT remove FCX from etf_markets.py without updating the equity formula.
    frozenset({"commodity_etfs", "sp500"}): frozenset({"FCX"}),
}


def _load_all_universes() -> Dict[str, Set[str]]:
    """Return {market_id: set(tickers)} for every registered market.

    Markets that raise NotImplementedError for get_universe_tickers() are skipped.
    """
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


class TestMarketOverlap:
    """Cross-market ticker overlap regression tests.

    Guards against accidental duplicate ticker assignments which cause:
    - Cross-market state pollution (positions tracked in wrong market)
    - Per-market equity double-counting (phantom HALT)
    - EOD settlement running stop checks on wrong market's positions
    """

    def test_no_unexpected_overlap_across_all_pairs(self) -> None:
        """Assert every (market_a, market_b) pair has only KNOWN_OVERLAPS tickers.

        Fails loudly with the pair and unexpected tickers so the fix is obvious:
        either remove the duplicate from one market, or add it to KNOWN_OVERLAPS
        with a written rationale comment.
        """
        universes = _load_all_universes()
        market_ids = sorted(universes.keys())

        failures: list[str] = []
        for mkt_a, mkt_b in combinations(market_ids, 2):
            overlap = universes[mkt_a] & universes[mkt_b]
            if not overlap:
                continue
            key = frozenset({mkt_a, mkt_b})
            allowed = KNOWN_OVERLAPS.get(key, frozenset())
            unexpected = overlap - allowed
            if unexpected:
                failures.append(
                    f"  {mkt_a} ∩ {mkt_b} = {sorted(unexpected)}  "
                    f"(add to KNOWN_OVERLAPS with rationale, or remove the duplicate)"
                )

        if failures:
            pytest.fail(
                "Unexpected cross-market ticker overlap(s):\n"
                + "\n".join(failures)
                + "\n\n"
                "Fix: assign each ticker to exactly one market, OR document the "
                "intentional overlap in KNOWN_OVERLAPS (tests/test_market_overlap.py)."
            )

    def test_known_overlaps_all_markets_still_registered(self) -> None:
        """Every market referenced in KNOWN_OVERLAPS must still be registered.

        Catches stale KNOWN_OVERLAPS entries left over after a market is renamed
        or removed (which would silently hide a real gap).
        """
        registered = set(MarketRegistry.list_ids())
        stale: list[str] = []
        for pair_key in KNOWN_OVERLAPS:
            for market_id in pair_key:
                if market_id not in registered:
                    stale.append(
                        f"  KNOWN_OVERLAPS references market {market_id!r} which is not "
                        f"registered. Remove or update the entry."
                    )
        if stale:
            pytest.fail(
                "Stale KNOWN_OVERLAPS market references:\n"
                + "\n".join(stale)
            )

    def test_known_overlaps_tickers_still_overlap(self) -> None:
        """Every KNOWN_OVERLAPS ticker must still actually appear in both markets.

        If a ticker is removed from one market (e.g. GLD delisted), the
        KNOWN_OVERLAPS entry becomes a dead whitelist that could hide future
        regressions.  This test forces explicit cleanup.
        """
        universes = _load_all_universes()
        stale: list[str] = []
        for pair_key, expected_tickers in KNOWN_OVERLAPS.items():
            mkt_a, mkt_b = sorted(pair_key)
            if mkt_a not in universes or mkt_b not in universes:
                continue  # stale market ref caught by test_known_overlaps_all_markets_still_registered
            actual_overlap = universes[mkt_a] & universes[mkt_b]
            missing = expected_tickers - actual_overlap
            if missing:
                stale.append(
                    f"  {mkt_a} ∩ {mkt_b}: {sorted(missing)} listed in KNOWN_OVERLAPS "
                    f"but no longer overlap — remove stale entry"
                )
        if stale:
            pytest.fail(
                "Stale KNOWN_OVERLAPS ticker entries:\n"
                + "\n".join(stale)
                + "\n\nUpdate KNOWN_OVERLAPS in tests/test_market_overlap.py."
            )

    def test_all_markets_have_at_least_one_ticker(self) -> None:
        """Sanity: every registered market must have a non-empty ticker list."""
        universes = _load_all_universes()
        empty = [mid for mid, tickers in universes.items() if len(tickers) == 0]
        assert not empty, f"Markets with zero tickers: {empty}"

    def test_fcx_in_commodity_etfs(self) -> None:
        """FCX must remain in commodity_etfs for the per-market equity formula.

        FCX is listed in universe/definitions.py as a commodity_etfs member
        (copper equity proxy) AND is an S&P 500 constituent.  The per-market
        equity formula (FIX-PMEQ-001) requires FCX in commodity_etfs so that
        _refresh_from_broker() tracks it for commodity_etfs equity calculations.

        HISTORY: Removing FCX from commodity_etfs (2026-04-29) caused a phantom
        HALT on 2026-05-01.  This test guards against a repeat.
        """
        commodity = MarketRegistry.get("commodity_etfs")
        tickers = set(commodity.get_universe_tickers())
        assert "FCX" in tickers, (
            "FCX must be in CommodityETFsMarket.get_universe_tickers(). "
            "See FIX-PMEQ-001 and the 2026-05-01 phantom-HALT incident. "
            "DO NOT remove FCX from commodity_etfs without updating the equity formula."
        )

    def test_sp500_contains_fcx(self) -> None:
        """FCX must also be in sp500 (it is an S&P 500 constituent)."""
        sp500 = MarketRegistry.get("sp500")
        tickers = set(sp500.get_universe_tickers())
        assert "FCX" in tickers, (
            "FCX expected in sp500 universe (S&P 500 constituent, Materials sector). "
            "If FCX was removed from sp500, update this test and KNOWN_OVERLAPS."
        )

    @pytest.mark.parametrize("market_id", ["sp500", "sector_etfs", "commodity_etfs",
                                            "defensive_etfs", "gold_etfs", "treasury_etfs", "asx"])
    def test_each_market_loadable(self, market_id: str) -> None:
        """Each market must be registered and return a non-empty ticker list."""
        market = MarketRegistry.get(market_id)
        tickers = market.get_universe_tickers()
        assert tickers, f"{market_id} returned an empty ticker list"
        assert all(isinstance(t, str) and t == t.upper() for t in tickers), (
            f"{market_id} has non-uppercase or non-string tickers: "
            f"{[t for t in tickers if not isinstance(t, str) or t != t.upper()][:5]}"
        )
