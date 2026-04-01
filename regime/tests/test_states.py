"""
regime/tests/test_states.py — Unit tests for RegimeState enum and REGIME_CONFIGS.

Run with:
    cd /root/atlas && python -m pytest regime/tests/test_states.py -v

Coverage:
  - All 6 enum members exist with correct string values
  - Enum round-trips: value → member → value
  - REGIME_CONFIGS covers exactly all 6 states
  - Every config entry has the 4 required keys
  - Value ranges are sane (sizing_multiplier in (0,1], max_positions >= 1)
  - String-keyed REGIME_CONFIGS_BY_VALUE mirrors the enum-keyed dict
  - regime.json is valid JSON and can be loaded
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

# Ensure project root on path when running from the worktree root.
PROJECT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT))

from regime.states import (
    REGIME_CONFIGS,
    REGIME_CONFIGS_BY_VALUE,
    REQUIRED_CONFIG_KEYS,
    RegimeState,
)

# ──────────────────────────────────────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────────────────────────────────────

EXPECTED_STATE_VALUES = {
    "bull_risk_on",
    "bull_risk_off",
    "transition_uncertain",
    "bear_risk_off",
    "bear_capitulation",
    "recovery_early",
}

EXPECTED_MEMBER_NAMES = {
    "BULL_RISK_ON",
    "BULL_RISK_OFF",
    "TRANSITION_UNCERTAIN",
    "BEAR_RISK_OFF",
    "BEAR_CAPITULATION",
    "RECOVERY_EARLY",
}

# ──────────────────────────────────────────────────────────────────────────────
# 1. RegimeState enum
# ──────────────────────────────────────────────────────────────────────────────


class TestRegimeStateEnum:
    def test_exactly_six_states(self):
        assert len(RegimeState) == 6, (
            f"Expected 6 regime states, got {len(RegimeState)}: "
            f"{[s.value for s in RegimeState]}"
        )

    def test_all_expected_values_present(self):
        actual_values = {s.value for s in RegimeState}
        assert actual_values == EXPECTED_STATE_VALUES

    def test_all_expected_member_names_present(self):
        actual_names = set(RegimeState.__members__)
        assert actual_names == EXPECTED_MEMBER_NAMES

    def test_specific_values(self):
        assert RegimeState.BULL_RISK_ON.value         == "bull_risk_on"
        assert RegimeState.BULL_RISK_OFF.value        == "bull_risk_off"
        assert RegimeState.TRANSITION_UNCERTAIN.value == "transition_uncertain"
        assert RegimeState.BEAR_RISK_OFF.value        == "bear_risk_off"
        assert RegimeState.BEAR_CAPITULATION.value    == "bear_capitulation"
        assert RegimeState.RECOVERY_EARLY.value       == "recovery_early"

    def test_enum_is_str_subclass(self):
        """RegimeState inherits str so it can be used as a SQLite value directly."""
        for state in RegimeState:
            assert isinstance(state, str), f"{state!r} is not a str instance"

    # ── Round-trip tests ────────────────────────────────────────────────────

    @pytest.mark.parametrize("state", list(RegimeState))
    def test_roundtrip_value_to_member(self, state: RegimeState):
        """RegimeState(state.value) returns the same member."""
        recovered = RegimeState(state.value)
        assert recovered is state

    @pytest.mark.parametrize("state", list(RegimeState))
    def test_roundtrip_name_to_member(self, state: RegimeState):
        """RegimeState[state.name] returns the same member."""
        recovered = RegimeState[state.name]
        assert recovered is state

    @pytest.mark.parametrize("state", list(RegimeState))
    def test_roundtrip_str_comparison(self, state: RegimeState):
        """Enum member compares equal to its raw string value."""
        assert state == state.value

    def test_invalid_value_raises(self):
        with pytest.raises(ValueError):
            RegimeState("not_a_regime")


# ──────────────────────────────────────────────────────────────────────────────
# 2. REGIME_CONFIGS dict
# ──────────────────────────────────────────────────────────────────────────────


class TestRegimeConfigs:
    def test_covers_all_states(self):
        assert set(REGIME_CONFIGS.keys()) == set(RegimeState)

    def test_exactly_six_entries(self):
        assert len(REGIME_CONFIGS) == 6

    @pytest.mark.parametrize("state", list(RegimeState))
    def test_required_keys_present(self, state: RegimeState):
        cfg = REGIME_CONFIGS[state]
        missing = REQUIRED_CONFIG_KEYS - set(cfg.keys())
        assert not missing, (
            f"State {state.value!r} config is missing keys: {missing}"
        )

    @pytest.mark.parametrize("state", list(RegimeState))
    def test_active_universes_is_non_empty_list(self, state: RegimeState):
        universes = REGIME_CONFIGS[state]["active_universes"]
        assert isinstance(universes, list)
        assert len(universes) >= 1, f"{state.value!r} has no active universes"

    @pytest.mark.parametrize("state", list(RegimeState))
    def test_strategy_types_is_non_empty_list(self, state: RegimeState):
        strats = REGIME_CONFIGS[state]["strategy_types"]
        assert isinstance(strats, list)
        assert len(strats) >= 1, f"{state.value!r} has no strategy types"

    @pytest.mark.parametrize("state", list(RegimeState))
    def test_sizing_multiplier_range(self, state: RegimeState):
        mult = REGIME_CONFIGS[state]["sizing_multiplier"]
        assert isinstance(mult, (int, float))
        assert 0 < mult <= 1.0, (
            f"{state.value!r} sizing_multiplier {mult} not in (0, 1]"
        )

    @pytest.mark.parametrize("state", list(RegimeState))
    def test_max_positions_positive_integer(self, state: RegimeState):
        mp = REGIME_CONFIGS[state]["max_positions"]
        assert isinstance(mp, int)
        assert mp >= 1, f"{state.value!r} max_positions must be >= 1"

    # ── Cross-state sanity checks ───────────────────────────────────────────

    def test_capitulation_has_lowest_sizing(self):
        """BEAR_CAPITULATION should have the smallest sizing multiplier."""
        cap_mult = REGIME_CONFIGS[RegimeState.BEAR_CAPITULATION]["sizing_multiplier"]
        for state, cfg in REGIME_CONFIGS.items():
            if state is not RegimeState.BEAR_CAPITULATION:
                assert cfg["sizing_multiplier"] >= cap_mult, (
                    f"{state.value!r} sizing {cfg['sizing_multiplier']} < "
                    f"BEAR_CAPITULATION sizing {cap_mult}"
                )

    def test_bull_risk_on_has_full_sizing(self):
        assert REGIME_CONFIGS[RegimeState.BULL_RISK_ON]["sizing_multiplier"] == 1.0

    def test_capitulation_has_fewest_universes(self):
        cap_universes = REGIME_CONFIGS[RegimeState.BEAR_CAPITULATION]["active_universes"]
        assert len(cap_universes) == 2

    def test_capitulation_has_fewest_positions(self):
        cap_mp = REGIME_CONFIGS[RegimeState.BEAR_CAPITULATION]["max_positions"]
        for state, cfg in REGIME_CONFIGS.items():
            if state is not RegimeState.BEAR_CAPITULATION:
                assert cfg["max_positions"] >= cap_mp

    def test_specific_config_values_bull_risk_on(self):
        cfg = REGIME_CONFIGS[RegimeState.BULL_RISK_ON]
        assert "sp500" in cfg["active_universes"]
        assert "sector_etfs" in cfg["active_universes"]
        assert "commodity_etfs" in cfg["active_universes"]
        assert "all" in cfg["strategy_types"]
        assert cfg["sizing_multiplier"] == 1.0
        assert cfg["max_positions"] == 5

    def test_specific_config_values_bear_capitulation(self):
        cfg = REGIME_CONFIGS[RegimeState.BEAR_CAPITULATION]
        assert "treasury_etfs" in cfg["active_universes"]
        assert "gold_etfs" in cfg["active_universes"]
        assert cfg["sizing_multiplier"] == 0.3
        assert cfg["max_positions"] == 2

    def test_specific_config_values_transition_uncertain(self):
        cfg = REGIME_CONFIGS[RegimeState.TRANSITION_UNCERTAIN]
        assert cfg["sizing_multiplier"] == 0.5
        assert cfg["max_positions"] == 3
        assert "mean_reversion" in cfg["strategy_types"]

    def test_specific_config_values_bear_risk_off(self):
        cfg = REGIME_CONFIGS[RegimeState.BEAR_RISK_OFF]
        assert "treasury_etfs" in cfg["active_universes"]
        assert "gold_etfs" in cfg["active_universes"]
        assert "defensive_etfs" in cfg["active_universes"]
        assert cfg["sizing_multiplier"] == 0.5
        assert cfg["max_positions"] == 3

    def test_specific_config_values_bull_risk_off(self):
        cfg = REGIME_CONFIGS[RegimeState.BULL_RISK_OFF]
        assert "sp500" in cfg["active_universes"]
        assert "treasury_etfs" in cfg["active_universes"]
        assert cfg["sizing_multiplier"] == 0.7
        assert cfg["max_positions"] == 4

    def test_specific_config_values_recovery_early(self):
        cfg = REGIME_CONFIGS[RegimeState.RECOVERY_EARLY]
        assert "sp500" in cfg["active_universes"]
        assert "momentum_breakout" in cfg["strategy_types"]
        assert cfg["sizing_multiplier"] == 0.7
        assert cfg["max_positions"] == 4


# ──────────────────────────────────────────────────────────────────────────────
# 3. REGIME_CONFIGS_BY_VALUE (string-keyed mirror)
# ──────────────────────────────────────────────────────────────────────────────


class TestRegimeConfigsByValue:
    def test_same_number_of_entries(self):
        assert len(REGIME_CONFIGS_BY_VALUE) == len(REGIME_CONFIGS)

    def test_keys_match_enum_values(self):
        assert set(REGIME_CONFIGS_BY_VALUE.keys()) == EXPECTED_STATE_VALUES

    @pytest.mark.parametrize("state", list(RegimeState))
    def test_config_matches_enum_keyed_dict(self, state: RegimeState):
        assert REGIME_CONFIGS_BY_VALUE[state.value] is REGIME_CONFIGS[state]

    def test_lookup_by_raw_string(self):
        cfg = REGIME_CONFIGS_BY_VALUE["bull_risk_on"]
        assert cfg["sizing_multiplier"] == 1.0


# ──────────────────────────────────────────────────────────────────────────────
# 4. regime.json config file
# ──────────────────────────────────────────────────────────────────────────────


class TestRegimeJson:
    @pytest.fixture(scope="class")
    def regime_cfg(self) -> dict:
        path = PROJECT / "config" / "active" / "regime.json"
        assert path.exists(), f"regime.json not found at {path}"
        with path.open() as f:
            return json.load(f)

    def test_valid_json_loads(self, regime_cfg):
        assert isinstance(regime_cfg, dict)

    def test_has_model_version(self, regime_cfg):
        assert regime_cfg.get("model_version") == "v1"

    def test_has_required_top_level_keys(self, regime_cfg):
        required = {
            "trend_thresholds",
            "risk_thresholds",
            "credit_thresholds",
            "yield_curve_thresholds",
            "dollar_thresholds",
            "commodity_thresholds",
            "weights",
        }
        missing = required - set(regime_cfg.keys())
        assert not missing, f"regime.json missing keys: {missing}"

    def test_weights_sum_to_one(self, regime_cfg):
        weights = {k: v for k, v in regime_cfg["weights"].items() if not k.startswith("_")}
        total = sum(weights.values())
        assert abs(total - 1.0) < 1e-9, (
            f"regime.json weights sum to {total:.6f}, expected 1.0"
        )

    def test_risk_thresholds_ordered(self, regime_cfg):
        rt = regime_cfg["risk_thresholds"]
        assert rt["vix_low"] < rt["vix_high"] < rt["vix_extreme"]

    def test_credit_thresholds_ordered(self, regime_cfg):
        ct = regime_cfg["credit_thresholds"]
        assert ct["oas_normal"] < ct["oas_elevated"] < ct["oas_crisis"]

    def test_dollar_thresholds_ordered(self, regime_cfg):
        dt = regime_cfg["dollar_thresholds"]
        assert dt["dxy_weak"] < dt["dxy_strong"]

    def test_commodity_thresholds_present(self, regime_cfg):
        ct = regime_cfg["commodity_thresholds"]
        assert "gold_copper_ratio_risk_on_below" in ct
        assert "gold_copper_ratio_risk_off_above" in ct
        assert ct["gold_copper_ratio_risk_on_below"] < ct["gold_copper_ratio_risk_off_above"]

    def test_yield_curve_thresholds_present(self, regime_cfg):
        yc = regime_cfg["yield_curve_thresholds"]
        assert "inversion_10y2y" in yc
        assert "inversion_10y3m" in yc
        assert "steep_threshold" in yc
