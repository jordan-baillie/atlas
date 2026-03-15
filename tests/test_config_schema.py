"""Tests for config/schema.py

All tests run offline in <5 s — no network, no broker, no file system writes.

Run with:
    pytest tests/test_config_schema.py -v --tb=short
"""
from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

import pytest

# Ensure project root is on path
PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

from config.schema import (  # noqa: E402
    SCHEMA,
    _MISSING,
    _get_nested,
    print_validation_report,
    validate_config,
    validate_config_file,
)

# ---------------------------------------------------------------------------
# Minimal valid config (satisfies all required fields, nothing more)
# ---------------------------------------------------------------------------

MINIMAL_VALID: dict = {
    "version": "v1.0",
    "market": "sp500",
    "risk": {
        "starting_equity": 10_000.0,
        "max_risk_per_trade_pct": 0.01,
        "max_open_positions": 5,
    },
    "trading": {
        "mode": "paper",
        "broker": "alpaca",
    },
    "data": {
        "source": "yfinance",
        "history_years": 5,
    },
}


def _cfg(**overrides) -> dict:
    """Return a deep copy of MINIMAL_VALID with nested key overrides applied.

    Keys use dot-notation; e.g. _cfg(**{"risk.max_open_positions": 3}).
    """
    cfg = copy.deepcopy(MINIMAL_VALID)
    for dotkey, val in overrides.items():
        parts = dotkey.split(".")
        node = cfg
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = val
    return cfg


def _del(cfg: dict, dotkey: str) -> dict:
    """Delete a nested key from *cfg* (in-place, returns cfg)."""
    parts = dotkey.split(".")
    node = cfg
    for part in parts[:-1]:
        node = node[part]
    del node[parts[-1]]
    return cfg


# ===========================================================================
# _get_nested
# ===========================================================================

class TestGetNested:
    def test_top_level_key_present(self):
        assert _get_nested({"version": "v1.0"}, "version") == "v1.0"

    def test_nested_two_levels(self):
        cfg = {"risk": {"max_open_positions": 10}}
        assert _get_nested(cfg, "risk.max_open_positions") == 10

    def test_nested_three_levels(self):
        cfg = {"risk": {"trailing_stop": {"enabled": True}}}
        assert _get_nested(cfg, "risk.trailing_stop.enabled") is True

    def test_missing_top_level_returns_sentinel(self):
        assert _get_nested({}, "version") is _MISSING

    def test_missing_leaf_returns_sentinel(self):
        assert _get_nested({"risk": {}}, "risk.max_open_positions") is _MISSING

    def test_missing_intermediate_returns_sentinel(self):
        assert _get_nested({}, "risk.max_open_positions") is _MISSING

    def test_false_value_is_not_missing(self):
        cfg = {"trading": {"live_enabled": False}}
        result = _get_nested(cfg, "trading.live_enabled")
        assert result is False
        assert result is not _MISSING

    def test_zero_value_is_not_missing(self):
        cfg = {"fees": {"commission_per_trade": 0}}
        result = _get_nested(cfg, "fees.commission_per_trade")
        assert result == 0
        assert result is not _MISSING

    def test_empty_string_is_not_missing(self):
        cfg = {"description": ""}
        result = _get_nested(cfg, "description")
        assert result == ""
        assert result is not _MISSING

    def test_non_dict_intermediate_returns_sentinel(self):
        # If an intermediate node is not a dict, treat as missing
        cfg = {"risk": "not_a_dict"}
        assert _get_nested(cfg, "risk.max_open_positions") is _MISSING


# ===========================================================================
# Happy path — valid configs
# ===========================================================================

class TestValidConfigPasses:
    def test_minimal_valid_config_passes(self):
        errors = validate_config(copy.deepcopy(MINIMAL_VALID))
        assert errors == [], f"Unexpected errors: {errors}"

    def test_sp500_json_passes_with_zero_errors(self):
        """The live SP500 config MUST pass with zero validation errors."""
        path = PROJECT / "config" / "active" / "sp500.json"
        errors = validate_config_file(path)
        assert errors == [], (
            f"sp500.json failed validation:\n" + "\n".join(f"  {e}" for e in errors)
        )

    def test_all_valid_trading_modes(self):
        for mode in ["live", "paper", "passive", "backtest"]:
            cfg = _cfg(**{"trading.mode": mode})
            errs = [e for e in validate_config(cfg) if "trading.mode" in e]
            assert not errs, f"Mode {mode!r} should be valid; got: {errs}"

    def test_all_valid_brokers(self):
        for broker in ["alpaca", "moomoo", "none"]:
            cfg = _cfg(**{"trading.broker": broker})
            errs = [e for e in validate_config(cfg) if "trading.broker" in e]
            assert not errs, f"Broker {broker!r} should be valid; got: {errs}"

    def test_all_valid_markets(self):
        for market in ["sp500", "asx"]:
            cfg = _cfg(**{"market": market})
            errs = [e for e in validate_config(cfg) if "market" in e]
            assert not errs, f"Market {market!r} should be valid; got: {errs}"

    def test_optional_keys_absent_causes_no_errors(self):
        """Minimal config with no optional fields must produce zero errors."""
        errors = validate_config(copy.deepcopy(MINIMAL_VALID))
        assert errors == []

    def test_optional_numeric_fields_accepted(self):
        cfg = _cfg(**{
            "risk.min_confidence": 0.70,
            "risk.max_sector_concentration": 3,
            "risk.max_daily_drawdown_pct": 0.03,
            "backtest.train_window_days": 252,
            "backtest.test_window_days": 63,
        })
        errors = validate_config(cfg)
        assert errors == [], f"Unexpected errors: {errors}"

    def test_optional_bool_fields_accepted(self):
        cfg = _cfg(**{
            "trading.live_enabled": False,
            "trading.approval_required": True,
            "allocation.enabled": True,
            "allocation.overflow_enabled": False,
            "dynamic_sizing.enabled": True,
        })
        errors = validate_config(cfg)
        assert errors == [], f"Unexpected errors: {errors}"

    def test_integer_accepted_for_int_float_field(self):
        """An integer value should satisfy an (int, float) type spec."""
        cfg = _cfg(**{"risk.starting_equity": 10_000})  # int, not float
        errors = [e for e in validate_config(cfg) if "starting_equity" in e]
        assert errors == []

    def test_float_accepted_for_int_float_field(self):
        cfg = _cfg(**{"risk.starting_equity": 9_999.99})  # float
        errors = [e for e in validate_config(cfg) if "starting_equity" in e]
        assert errors == []


# ===========================================================================
# Missing required fields
# ===========================================================================

class TestMissingRequiredFields:
    def test_missing_version(self):
        cfg = _del(copy.deepcopy(MINIMAL_VALID), "version")
        errs = validate_config(cfg)
        assert any("version" in e and "[MISSING]" in e for e in errs)

    def test_missing_market(self):
        cfg = _del(copy.deepcopy(MINIMAL_VALID), "market")
        errs = validate_config(cfg)
        assert any("market" in e and "[MISSING]" in e for e in errs)

    def test_missing_risk_starting_equity(self):
        cfg = _del(copy.deepcopy(MINIMAL_VALID), "risk.starting_equity")
        errs = validate_config(cfg)
        assert any("risk.starting_equity" in e and "[MISSING]" in e for e in errs)

    def test_missing_risk_max_risk_per_trade_pct(self):
        cfg = _del(copy.deepcopy(MINIMAL_VALID), "risk.max_risk_per_trade_pct")
        errs = validate_config(cfg)
        assert any("risk.max_risk_per_trade_pct" in e and "[MISSING]" in e for e in errs)

    def test_missing_risk_max_open_positions(self):
        cfg = _del(copy.deepcopy(MINIMAL_VALID), "risk.max_open_positions")
        errs = validate_config(cfg)
        assert any("risk.max_open_positions" in e and "[MISSING]" in e for e in errs)

    def test_missing_trading_mode(self):
        cfg = _del(copy.deepcopy(MINIMAL_VALID), "trading.mode")
        errs = validate_config(cfg)
        assert any("trading.mode" in e and "[MISSING]" in e for e in errs)

    def test_missing_trading_broker(self):
        cfg = _del(copy.deepcopy(MINIMAL_VALID), "trading.broker")
        errs = validate_config(cfg)
        assert any("trading.broker" in e and "[MISSING]" in e for e in errs)

    def test_missing_data_source(self):
        cfg = _del(copy.deepcopy(MINIMAL_VALID), "data.source")
        errs = validate_config(cfg)
        assert any("data.source" in e and "[MISSING]" in e for e in errs)

    def test_missing_data_history_years(self):
        cfg = _del(copy.deepcopy(MINIMAL_VALID), "data.history_years")
        errs = validate_config(cfg)
        assert any("data.history_years" in e and "[MISSING]" in e for e in errs)

    def test_all_errors_collected_on_empty_config(self):
        """Empty config must raise one MISSING error per required field."""
        required_count = sum(1 for entry in SCHEMA if entry[2])  # entry[2] = required
        errs = validate_config({})
        missing_errs = [e for e in errs if "[MISSING]" in e]
        assert len(missing_errs) == required_count, (
            f"Expected {required_count} MISSING errors, got {len(missing_errs)}: {missing_errs}"
        )

    def test_multiple_missing_fields_all_reported(self):
        """Removing two required fields must produce two MISSING errors."""
        cfg = copy.deepcopy(MINIMAL_VALID)
        del cfg["version"]
        del cfg["market"]
        errs = validate_config(cfg)
        missing = [e for e in errs if "[MISSING]" in e]
        assert len(missing) >= 2


# ===========================================================================
# Wrong type
# ===========================================================================

class TestWrongType:
    def test_string_for_int_field(self):
        cfg = _cfg(**{"risk.max_open_positions": "ten"})
        errs = validate_config(cfg)
        assert any("risk.max_open_positions" in e and "[TYPE]" in e for e in errs)

    def test_string_for_float_field(self):
        cfg = _cfg(**{"risk.max_risk_per_trade_pct": "0.01"})
        errs = validate_config(cfg)
        assert any("risk.max_risk_per_trade_pct" in e and "[TYPE]" in e for e in errs)

    def test_int_for_bool_field(self):
        """Integer 1 is NOT a valid bool — must raise a type error."""
        cfg = _cfg(**{"trading.live_enabled": 1})
        errs = validate_config(cfg)
        assert any("trading.live_enabled" in e and "[TYPE]" in e for e in errs)

    def test_bool_for_int_field(self):
        """True/False must NOT satisfy an int field (bool is subclass of int)."""
        cfg = _cfg(**{"risk.max_open_positions": True})
        errs = validate_config(cfg)
        assert any("risk.max_open_positions" in e and "[TYPE]" in e for e in errs)

    def test_bool_for_numeric_field(self):
        """False must NOT satisfy an (int, float) field."""
        cfg = _cfg(**{"risk.starting_equity": False})
        errs = validate_config(cfg)
        assert any("risk.starting_equity" in e and "[TYPE]" in e for e in errs)

    def test_string_for_bool_field(self):
        cfg = _cfg(**{"trading.live_enabled": "true"})
        errs = validate_config(cfg)
        assert any("trading.live_enabled" in e and "[TYPE]" in e for e in errs)

    def test_list_for_string_field(self):
        cfg = _cfg(**{"version": ["v1", "v2"]})
        errs = validate_config(cfg)
        assert any("version" in e and "[TYPE]" in e for e in errs)

    def test_dict_for_int_field(self):
        cfg = _cfg(**{"risk.max_open_positions": {"value": 5}})
        errs = validate_config(cfg)
        assert any("risk.max_open_positions" in e and "[TYPE]" in e for e in errs)

    def test_float_for_pure_int_field(self):
        """5.0 (float) is not acceptable for a pure-int field."""
        cfg = _cfg(**{"risk.max_open_positions": 5.0})
        errs = validate_config(cfg)
        assert any("risk.max_open_positions" in e and "[TYPE]" in e for e in errs)

    def test_type_error_does_not_also_produce_range_error(self):
        """When type is wrong, no spurious RANGE/ENUM error should appear."""
        cfg = _cfg(**{"risk.max_open_positions": "many"})
        errs = validate_config(cfg)
        range_errs = [e for e in errs if "[RANGE]" in e and "max_open_positions" in e]
        assert range_errs == [], f"Should not have RANGE errors after TYPE error: {range_errs}"


# ===========================================================================
# Out of range
# ===========================================================================

class TestOutOfRange:
    def test_max_open_positions_below_minimum(self):
        cfg = _cfg(**{"risk.max_open_positions": 0})
        errs = validate_config(cfg)
        assert any("risk.max_open_positions" in e and "[RANGE]" in e for e in errs)

    def test_max_open_positions_above_maximum(self):
        cfg = _cfg(**{"risk.max_open_positions": 100})
        errs = validate_config(cfg)
        assert any("risk.max_open_positions" in e and "[RANGE]" in e for e in errs)

    def test_max_open_positions_at_lower_bound_passes(self):
        cfg = _cfg(**{"risk.max_open_positions": 1})
        errs = [e for e in validate_config(cfg) if "max_open_positions" in e]
        assert errs == []

    def test_max_open_positions_at_upper_bound_passes(self):
        cfg = _cfg(**{"risk.max_open_positions": 50})
        errs = [e for e in validate_config(cfg) if "max_open_positions" in e]
        assert errs == []

    def test_risk_pct_negative(self):
        cfg = _cfg(**{"risk.max_risk_per_trade_pct": -0.01})
        errs = validate_config(cfg)
        assert any("risk.max_risk_per_trade_pct" in e and "[RANGE]" in e for e in errs)

    def test_risk_pct_above_one(self):
        cfg = _cfg(**{"risk.max_risk_per_trade_pct": 1.5})
        errs = validate_config(cfg)
        assert any("risk.max_risk_per_trade_pct" in e and "[RANGE]" in e for e in errs)

    def test_risk_pct_at_zero_passes(self):
        cfg = _cfg(**{"risk.max_risk_per_trade_pct": 0.0})
        errs = [e for e in validate_config(cfg) if "max_risk_per_trade_pct" in e]
        assert errs == []

    def test_risk_pct_at_one_passes(self):
        cfg = _cfg(**{"risk.max_risk_per_trade_pct": 1.0})
        errs = [e for e in validate_config(cfg) if "max_risk_per_trade_pct" in e]
        assert errs == []

    def test_starting_equity_negative(self):
        cfg = _cfg(**{"risk.starting_equity": -1000.0})
        errs = validate_config(cfg)
        assert any("risk.starting_equity" in e and "[RANGE]" in e for e in errs)

    def test_history_years_zero(self):
        cfg = _cfg(**{"data.history_years": 0})
        errs = validate_config(cfg)
        assert any("data.history_years" in e and "[RANGE]" in e for e in errs)

    def test_history_years_excessive(self):
        cfg = _cfg(**{"data.history_years": 100})
        errs = validate_config(cfg)
        assert any("data.history_years" in e and "[RANGE]" in e for e in errs)

    def test_slippage_pct_above_max(self):
        cfg = _cfg(**{"fees.slippage_pct": 0.5})  # 50% — clearly wrong
        errs = validate_config(cfg)
        assert any("fees.slippage_pct" in e and "[RANGE]" in e for e in errs)

    def test_min_confidence_above_one(self):
        cfg = _cfg(**{"risk.min_confidence": 1.5})
        errs = validate_config(cfg)
        assert any("risk.min_confidence" in e and "[RANGE]" in e for e in errs)

    def test_min_confidence_negative(self):
        cfg = _cfg(**{"risk.min_confidence": -0.1})
        errs = validate_config(cfg)
        assert any("risk.min_confidence" in e and "[RANGE]" in e for e in errs)

    def test_backtest_train_window_too_small(self):
        cfg = _cfg(**{"backtest.train_window_days": 5})
        errs = validate_config(cfg)
        assert any("backtest.train_window_days" in e and "[RANGE]" in e for e in errs)


# ===========================================================================
# Invalid enum values
# ===========================================================================

class TestInvalidEnum:
    def test_invalid_market(self):
        cfg = _cfg(**{"market": "nyse"})
        errs = validate_config(cfg)
        assert any("market" in e and "[ENUM]" in e for e in errs)

    def test_invalid_trading_mode(self):
        cfg = _cfg(**{"trading.mode": "simulation"})
        errs = validate_config(cfg)
        assert any("trading.mode" in e and "[ENUM]" in e for e in errs)

    def test_invalid_broker(self):
        cfg = _cfg(**{"trading.broker": "interactive_brokers"})
        errs = validate_config(cfg)
        assert any("trading.broker" in e and "[ENUM]" in e for e in errs)

    def test_invalid_data_source(self):
        cfg = _cfg(**{"data.source": "bloomberg"})
        errs = validate_config(cfg)
        assert any("data.source" in e and "[ENUM]" in e for e in errs)

    def test_invalid_allocation_mode(self):
        cfg = _cfg(**{
            "allocation.enabled": True,
            "allocation.mode": "round_robin",
        })
        errs = validate_config(cfg)
        assert any("allocation.mode" in e and "[ENUM]" in e for e in errs)

    def test_invalid_portfolio_optimizer_method(self):
        cfg = _cfg(**{"portfolio_optimizer.method": "genetic"})
        errs = validate_config(cfg)
        assert any("portfolio_optimizer.method" in e and "[ENUM]" in e for e in errs)

    def test_invalid_macro_regime_mode(self):
        cfg = _cfg(**{"macro_regime.mode": "override"})
        errs = validate_config(cfg)
        assert any("macro_regime.mode" in e and "[ENUM]" in e for e in errs)

    def test_enum_case_sensitive(self):
        """Enum values are case-sensitive: 'LIVE' must not match 'live'."""
        cfg = _cfg(**{"trading.mode": "LIVE"})
        errs = validate_config(cfg)
        assert any("trading.mode" in e and "[ENUM]" in e for e in errs)

    def test_enum_error_does_not_also_produce_type_error(self):
        """A wrong enum string should only produce [ENUM], not [TYPE]."""
        cfg = _cfg(**{"market": "nasdaq"})
        errs = validate_config(cfg)
        type_errs = [e for e in errs if "[TYPE]" in e and "market" in e]
        assert type_errs == [], f"Should not have TYPE error for wrong enum: {type_errs}"


# ===========================================================================
# validate_config_file
# ===========================================================================

class TestValidateConfigFile:
    def test_sp500_file_passes(self):
        path = PROJECT / "config" / "active" / "sp500.json"
        errors = validate_config_file(path)
        assert errors == []

    def test_sp500_file_passes_as_string_path(self):
        path = str(PROJECT / "config" / "active" / "sp500.json")
        errors = validate_config_file(path)
        assert errors == []

    def test_missing_file_returns_file_error(self):
        errors = validate_config_file("/nonexistent/path/config.json")
        assert len(errors) == 1
        assert "[FILE]" in errors[0]

    def test_invalid_json_returns_json_error(self, tmp_path):
        bad = tmp_path / "bad.json"
        bad.write_text("{not: valid json}")
        errors = validate_config_file(bad)
        assert len(errors) == 1
        assert "[JSON]" in errors[0]

    def test_json_array_returns_structure_error(self, tmp_path):
        arr = tmp_path / "arr.json"
        arr.write_text("[1, 2, 3]")
        errors = validate_config_file(arr)
        assert len(errors) == 1
        assert "[STRUCTURE]" in errors[0]

    def test_valid_minimal_file_passes(self, tmp_path):
        cfg_file = tmp_path / "minimal.json"
        cfg_file.write_text(json.dumps(MINIMAL_VALID))
        errors = validate_config_file(cfg_file)
        assert errors == []

    def test_invalid_config_file_reports_errors(self, tmp_path):
        bad_cfg = copy.deepcopy(MINIMAL_VALID)
        bad_cfg["market"] = "invalid"
        cfg_file = tmp_path / "bad_config.json"
        cfg_file.write_text(json.dumps(bad_cfg))
        errors = validate_config_file(cfg_file)
        assert any("[ENUM]" in e and "market" in e for e in errors)


# ===========================================================================
# print_validation_report
# ===========================================================================

class TestPrintValidationReport:
    def test_prints_pass_for_valid_dict(self, capsys):
        print_validation_report(copy.deepcopy(MINIMAL_VALID))
        out = capsys.readouterr().out
        assert "PASS" in out

    def test_prints_fail_for_invalid_dict(self, capsys):
        cfg = _cfg(**{"market": "invalid_market"})
        print_validation_report(cfg)
        out = capsys.readouterr().out
        assert "FAIL" in out

    def test_prints_error_count_for_invalid(self, capsys):
        cfg = _cfg(**{"market": "bad", "trading.mode": "bad"})
        print_validation_report(cfg)
        out = capsys.readouterr().out
        # Should mention error count
        assert "error" in out.lower()

    def test_prints_pass_for_sp500_file(self, capsys):
        path = PROJECT / "config" / "active" / "sp500.json"
        print_validation_report(path)
        out = capsys.readouterr().out
        assert "PASS" in out

    def test_prints_pass_for_string_path(self, capsys):
        path = str(PROJECT / "config" / "active" / "sp500.json")
        print_validation_report(path)
        out = capsys.readouterr().out
        assert "PASS" in out

    def test_includes_label_in_output(self, capsys):
        print_validation_report(copy.deepcopy(MINIMAL_VALID))
        out = capsys.readouterr().out
        assert "dict" in out  # label shows "<dict>"


# ===========================================================================
# SCHEMA metadata integrity
# ===========================================================================

class TestSchemaIntegrity:
    def test_schema_is_non_empty(self):
        assert len(SCHEMA) > 10, "SCHEMA should have at least 10 entries"

    def test_each_entry_has_six_fields(self):
        for entry in SCHEMA:
            assert len(entry) == 6, f"Schema entry {entry[0]!r} should have 6 fields, got {len(entry)}"

    def test_key_paths_are_strings(self):
        for entry in SCHEMA:
            assert isinstance(entry[0], str), f"key_path must be str: {entry[0]!r}"

    def test_required_field_is_bool(self):
        for entry in SCHEMA:
            assert isinstance(entry[2], bool), (
                f"required flag for {entry[0]!r} must be bool, got {type(entry[2])}"
            )

    def test_no_duplicate_key_paths(self):
        paths = [entry[0] for entry in SCHEMA]
        assert len(paths) == len(set(paths)), (
            "SCHEMA contains duplicate key_paths: "
            + str([p for p in paths if paths.count(p) > 1])
        )

    def test_required_fields_exist_in_sp500(self):
        """Every required field in SCHEMA must actually exist in sp500.json."""
        path = PROJECT / "config" / "active" / "sp500.json"
        with path.open() as fh:
            sp500 = json.load(fh)

        missing_required = []
        for key_path, _, required, *_ in SCHEMA:
            if required:
                if _get_nested(sp500, key_path) is _MISSING:
                    missing_required.append(key_path)

        assert missing_required == [], (
            f"Required SCHEMA fields not found in sp500.json: {missing_required}"
        )
