"""
universe/tests/test_definitions.py — Unit tests for universe/definitions.py.

Run with:
    cd /root/atlas && python -m pytest universe/tests/test_definitions.py -v

Coverage:
  - All 6 universes are defined
  - sp500 universe has correct method ("sp500_constituents")
  - All static universes have non-empty ticker lists
  - get_universe() returns correct definitions and raises for unknowns
  - get_universe_tickers() returns correct lists and raises for sp500
  - get_all_etf_tickers() returns deduplicated tickers
  - list_universes() returns all 6 names
  - Universe names align with REGIME_CONFIGS active_universes references
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Ensure project root on path when running from any working directory.
PROJECT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT))

from universe.definitions import (
    UNIVERSES,
    get_all_etf_tickers,
    get_universe,
    get_universe_tickers,
    list_universes,
)

# ──────────────────────────────────────────────────────────────────────────────
# Expected constants
# ──────────────────────────────────────────────────────────────────────────────

EXPECTED_UNIVERSE_NAMES = {
    "sp500",
    "sector_etfs",
    "treasury_etfs",
    "commodity_etfs",
    "gold_etfs",
    "defensive_etfs",
}

# All universe names referenced in regime/states.py REGIME_CONFIGS
REGIME_REFERENCED_UNIVERSES = {
    "sp500",
    "sector_etfs",
    "treasury_etfs",
    "commodity_etfs",
    "gold_etfs",
    "defensive_etfs",
}

# Tickers that intentionally appear in multiple universes
CROSS_UNIVERSE_TICKERS = {
    "GLD": {"commodity_etfs", "gold_etfs"},
    "XLU": {"sector_etfs", "defensive_etfs"},
    "XLP": {"sector_etfs", "defensive_etfs"},
}


# ──────────────────────────────────────────────────────────────────────────────
# 1. UNIVERSES dict structure
# ──────────────────────────────────────────────────────────────────────────────

class TestUniversesDict:
    def test_exactly_6_universes(self):
        """UNIVERSES must contain exactly 6 entries."""
        assert len(UNIVERSES) == 6

    def test_all_expected_names_present(self):
        """All 6 expected universe names must be present."""
        assert set(UNIVERSES.keys()) == EXPECTED_UNIVERSE_NAMES

    def test_each_entry_has_method_key(self):
        """Every universe definition must have a 'method' key."""
        for name, defn in UNIVERSES.items():
            assert "method" in defn, f"Universe {name!r} missing 'method' key"

    def test_sp500_method(self):
        """sp500 universe must use method 'sp500_constituents'."""
        assert UNIVERSES["sp500"]["method"] == "sp500_constituents"

    def test_sp500_has_top_n(self):
        """sp500 universe must declare a top_n value."""
        assert "top_n" in UNIVERSES["sp500"]
        assert isinstance(UNIVERSES["sp500"]["top_n"], int)
        assert UNIVERSES["sp500"]["top_n"] > 0

    def test_static_universes_have_tickers_key(self):
        """All static universes must have a non-empty 'tickers' list."""
        for name, defn in UNIVERSES.items():
            if defn["method"] == "static":
                assert "tickers" in defn, f"{name!r} missing 'tickers'"
                assert isinstance(defn["tickers"], list), f"{name!r} 'tickers' must be a list"
                assert len(defn["tickers"]) > 0, f"{name!r} 'tickers' must be non-empty"

    def test_all_ticker_strings_are_uppercase(self):
        """All tickers in static universes must be uppercase strings."""
        for name, defn in UNIVERSES.items():
            if defn["method"] == "static":
                for t in defn["tickers"]:
                    assert isinstance(t, str), f"{name!r}: ticker {t!r} is not a string"
                    assert t == t.upper(), f"{name!r}: ticker {t!r} is not uppercase"
                    assert t.strip() == t, f"{name!r}: ticker {t!r} has whitespace"

    def test_no_duplicate_tickers_within_single_universe(self):
        """Each static universe must not have internal duplicates."""
        for name, defn in UNIVERSES.items():
            if defn["method"] == "static":
                tickers = defn["tickers"]
                assert len(tickers) == len(set(tickers)), (
                    f"Universe {name!r} has duplicate tickers: "
                    f"{[t for t in tickers if tickers.count(t) > 1]}"
                )


# ──────────────────────────────────────────────────────────────────────────────
# 2. Per-universe content checks
# ──────────────────────────────────────────────────────────────────────────────

class TestSectorEtfs:
    def test_has_11_tickers(self):
        assert len(UNIVERSES["sector_etfs"]["tickers"]) == 11

    def test_contains_xlk(self):
        assert "XLK" in UNIVERSES["sector_etfs"]["tickers"]

    def test_contains_xlf(self):
        assert "XLF" in UNIVERSES["sector_etfs"]["tickers"]

    def test_contains_xlp(self):
        """XLP is shared with defensive_etfs."""
        assert "XLP" in UNIVERSES["sector_etfs"]["tickers"]

    def test_contains_xlu(self):
        """XLU is shared with defensive_etfs."""
        assert "XLU" in UNIVERSES["sector_etfs"]["tickers"]


class TestTreasuryEtfs:
    def test_has_5_tickers(self):
        assert len(UNIVERSES["treasury_etfs"]["tickers"]) == 5

    def test_contains_tlt(self):
        assert "TLT" in UNIVERSES["treasury_etfs"]["tickers"]

    def test_contains_bnd(self):
        assert "BND" in UNIVERSES["treasury_etfs"]["tickers"]


class TestCommodityEtfs:
    def test_has_10_tickers(self):
        assert len(UNIVERSES["commodity_etfs"]["tickers"]) == 10

    def test_contains_gld(self):
        """GLD is shared with gold_etfs."""
        assert "GLD" in UNIVERSES["commodity_etfs"]["tickers"]

    def test_contains_uso(self):
        assert "USO" in UNIVERSES["commodity_etfs"]["tickers"]


class TestGoldEtfs:
    def test_has_4_tickers(self):
        assert len(UNIVERSES["gold_etfs"]["tickers"]) == 4

    def test_contains_gld(self):
        """GLD is shared with commodity_etfs."""
        assert "GLD" in UNIVERSES["gold_etfs"]["tickers"]

    def test_contains_gdx(self):
        assert "GDX" in UNIVERSES["gold_etfs"]["tickers"]

    def test_contains_gdxj(self):
        assert "GDXJ" in UNIVERSES["gold_etfs"]["tickers"]

    def test_contains_iau(self):
        assert "IAU" in UNIVERSES["gold_etfs"]["tickers"]


class TestDefensiveEtfs:
    def test_has_6_tickers(self):
        assert len(UNIVERSES["defensive_etfs"]["tickers"]) == 6

    def test_contains_sh(self):
        """SH is the inverse S&P 500 ETF."""
        assert "SH" in UNIVERSES["defensive_etfs"]["tickers"]

    def test_contains_psq(self):
        assert "PSQ" in UNIVERSES["defensive_etfs"]["tickers"]

    def test_contains_xlu(self):
        """XLU is shared with sector_etfs."""
        assert "XLU" in UNIVERSES["defensive_etfs"]["tickers"]

    def test_contains_xlp(self):
        """XLP is shared with sector_etfs."""
        assert "XLP" in UNIVERSES["defensive_etfs"]["tickers"]


# ──────────────────────────────────────────────────────────────────────────────
# 3. get_universe() helper
# ──────────────────────────────────────────────────────────────────────────────

class TestGetUniverse:
    def test_returns_correct_definition_for_each_universe(self):
        for name in EXPECTED_UNIVERSE_NAMES:
            defn = get_universe(name)
            assert defn is UNIVERSES[name], (
                f"get_universe({name!r}) should return the same dict object"
            )

    def test_raises_keyerror_for_unknown_universe(self):
        with pytest.raises(KeyError, match="nonexistent"):
            get_universe("nonexistent")

    def test_raises_keyerror_with_helpful_message(self):
        with pytest.raises(KeyError) as exc_info:
            get_universe("bad_name")
        # Error message should list known universes
        assert "sp500" in str(exc_info.value)

    def test_sp500_definition_has_expected_keys(self):
        defn = get_universe("sp500")
        assert defn["method"] == "sp500_constituents"
        assert "top_n" in defn

    def test_static_universe_definition_has_tickers(self):
        defn = get_universe("gold_etfs")
        assert defn["method"] == "static"
        assert "tickers" in defn
        assert len(defn["tickers"]) > 0


# ──────────────────────────────────────────────────────────────────────────────
# 4. get_universe_tickers() helper
# ──────────────────────────────────────────────────────────────────────────────

class TestGetUniverseTickers:
    def test_sector_etfs_returns_11_tickers(self):
        tickers = get_universe_tickers("sector_etfs")
        assert len(tickers) == 11

    def test_treasury_etfs_returns_5_tickers(self):
        tickers = get_universe_tickers("treasury_etfs")
        assert len(tickers) == 5

    def test_commodity_etfs_returns_10_tickers(self):
        tickers = get_universe_tickers("commodity_etfs")
        assert len(tickers) == 10

    def test_gold_etfs_returns_4_tickers(self):
        tickers = get_universe_tickers("gold_etfs")
        assert len(tickers) == 4

    def test_defensive_etfs_returns_6_tickers(self):
        tickers = get_universe_tickers("defensive_etfs")
        assert len(tickers) == 6

    def test_returns_a_copy_not_the_original(self):
        """Mutating the returned list must not affect UNIVERSES."""
        tickers = get_universe_tickers("gold_etfs")
        original_len = len(UNIVERSES["gold_etfs"]["tickers"])
        tickers.append("FAKE")
        assert len(UNIVERSES["gold_etfs"]["tickers"]) == original_len

    def test_sp500_raises_value_error(self):
        """sp500 is dynamic — get_universe_tickers must raise ValueError."""
        with pytest.raises(ValueError, match="sp500"):
            get_universe_tickers("sp500")

    def test_sp500_error_mentions_builder(self):
        """Error message should guide users to build_universe()."""
        with pytest.raises(ValueError) as exc_info:
            get_universe_tickers("sp500")
        assert "build_universe" in str(exc_info.value)

    def test_raises_keyerror_for_unknown_universe(self):
        with pytest.raises(KeyError):
            get_universe_tickers("totally_unknown")

    def test_gold_etfs_contains_gld(self):
        assert "GLD" in get_universe_tickers("gold_etfs")

    def test_treasury_etfs_contains_tlt(self):
        assert "TLT" in get_universe_tickers("treasury_etfs")


# ──────────────────────────────────────────────────────────────────────────────
# 5. get_all_etf_tickers() helper
# ──────────────────────────────────────────────────────────────────────────────

class TestGetAllEtfTickers:
    def test_returns_a_list(self):
        assert isinstance(get_all_etf_tickers(), list)

    def test_no_duplicates(self):
        tickers = get_all_etf_tickers()
        assert len(tickers) == len(set(tickers)), (
            f"get_all_etf_tickers() returned duplicates: "
            f"{[t for t in tickers if tickers.count(t) > 1]}"
        )

    def test_gld_appears_exactly_once(self):
        """GLD is in commodity_etfs AND gold_etfs but must appear once."""
        tickers = get_all_etf_tickers()
        assert tickers.count("GLD") == 1

    def test_xlu_appears_exactly_once(self):
        """XLU is in sector_etfs AND defensive_etfs but must appear once."""
        tickers = get_all_etf_tickers()
        assert tickers.count("XLU") == 1

    def test_xlp_appears_exactly_once(self):
        """XLP is in sector_etfs AND defensive_etfs but must appear once."""
        tickers = get_all_etf_tickers()
        assert tickers.count("XLP") == 1

    def test_sp500_tickers_not_included(self):
        """sp500 is dynamic — its tickers should NOT be in the ETF list."""
        tickers = get_all_etf_tickers()
        # The sp500 universe has no pre-defined tickers to include
        # (method != "static"), so it contributes nothing.
        # We verify by checking the count equals only static tickers.
        static_count = sum(
            len(defn["tickers"])
            for defn in UNIVERSES.values()
            if defn["method"] == "static"
        )
        # Deduplication means result <= static_count
        assert len(tickers) <= static_count

    def test_contains_all_unique_static_tickers(self):
        """Result must contain every ticker that appears in any static universe."""
        expected = set()
        for defn in UNIVERSES.values():
            if defn["method"] == "static":
                expected.update(defn["tickers"])
        assert set(get_all_etf_tickers()) == expected

    def test_result_is_non_empty(self):
        assert len(get_all_etf_tickers()) > 0

    def test_total_unique_count_is_correct(self):
        """Manually compute expected deduplicated count."""
        expected = set()
        for defn in UNIVERSES.values():
            if defn["method"] == "static":
                expected.update(defn["tickers"])
        assert len(get_all_etf_tickers()) == len(expected)

    def test_all_results_are_uppercase_strings(self):
        for t in get_all_etf_tickers():
            assert isinstance(t, str)
            assert t == t.upper()


# ──────────────────────────────────────────────────────────────────────────────
# 6. list_universes() helper
# ──────────────────────────────────────────────────────────────────────────────

class TestListUniverses:
    def test_returns_6_names(self):
        assert len(list_universes()) == 6

    def test_returns_all_expected_names(self):
        assert set(list_universes()) == EXPECTED_UNIVERSE_NAMES

    def test_returns_a_list(self):
        assert isinstance(list_universes(), list)

    def test_all_items_are_strings(self):
        for name in list_universes():
            assert isinstance(name, str)


# ──────────────────────────────────────────────────────────────────────────────
# 7. Regime alignment — names match REGIME_CONFIGS references
# ──────────────────────────────────────────────────────────────────────────────

class TestRegimeAlignment:
    def test_all_regime_referenced_universes_are_defined(self):
        """Every universe referenced in REGIME_CONFIGS must exist in UNIVERSES."""
        defined = set(list_universes())
        for name in REGIME_REFERENCED_UNIVERSES:
            assert name in defined, (
                f"Universe {name!r} is referenced in REGIME_CONFIGS "
                f"but not defined in UNIVERSES"
            )

    def test_regime_configs_active_universes_all_valid(self):
        """All active_universes in REGIME_CONFIGS must be defined."""
        from regime.states import REGIME_CONFIGS
        defined = set(list_universes())
        for state, cfg in REGIME_CONFIGS.items():
            for u_name in cfg["active_universes"]:
                assert u_name in defined, (
                    f"Regime {state.value!r} references universe {u_name!r} "
                    f"which is not defined in UNIVERSES"
                )

    def test_all_6_universe_names_covered_by_at_least_one_regime(self):
        """Every universe must be used by at least one regime state."""
        from regime.states import REGIME_CONFIGS
        used = set()
        for cfg in REGIME_CONFIGS.values():
            used.update(cfg["active_universes"])
        defined = set(list_universes()) - {"sp500"}  # sp500 may be excluded from some
        # All static universe names appear in at least one regime
        for name in ["sector_etfs", "treasury_etfs", "commodity_etfs", "gold_etfs", "defensive_etfs"]:
            assert name in used, (
                f"Universe {name!r} is defined but never referenced in REGIME_CONFIGS"
            )
