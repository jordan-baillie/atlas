"""
tests/test_rca_phase3a_overlay_enforce_gate.py

RCA Phase 3A — Config-promotion gate for overlay enforce-mode flips.

Tests that:
- shadow_mode=true always passes (gate is silent)
- shadow_mode=false + overlay_enforce_validated=true passes (proven gate)
- shadow_mode=false + missing overlay_enforce_validated fails with [OVERLAY_ENFORCE]
- shadow_mode=false + overlay_enforce_validated=false fails with [OVERLAY_ENFORCE]
- The backtest script returns the correct structure
- The sp500 active config passes validation after the flip
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

# Ensure project root is importable
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from config.schema import validate_config, validate_config_file


# ── Fixtures ─────────────────────────────────────────────────────────────────

def _minimal_config(**overlay_fields) -> dict:
    """Return a minimal valid config with the given overlay fields injected."""
    cfg: dict = {
        "version": "v1.0",
        "market": "sp500",
        "risk": {
            "starting_equity": 10_000,
            "max_risk_per_trade_pct": 0.02,
            "max_open_positions": 10,
        },
        "trading": {
            "mode": "live",
            "broker": "alpaca",
        },
        "data": {
            "source": "yfinance",
            "history_years": 5,
        },
    }
    if overlay_fields:
        cfg["overlay"] = overlay_fields
    return cfg


def _overlay_errors(config: dict) -> list[str]:
    """Return only [OVERLAY_ENFORCE] errors from validate_config."""
    return [e for e in validate_config(config) if "OVERLAY_ENFORCE" in e]


# ── Gate tests ────────────────────────────────────────────────────────────────

class TestOverlayEnforceGate:
    """Core gate behavior tests (4 cases from spec)."""

    def test_config_with_shadow_true_passes_validation(self):
        """shadow_mode=true should never trigger [OVERLAY_ENFORCE]."""
        cfg = _minimal_config(shadow_mode=True)
        errs = _overlay_errors(cfg)
        assert errs == [], f"Unexpected OVERLAY_ENFORCE errors: {errs}"

    def test_config_with_shadow_true_and_no_validated_field_passes(self):
        """shadow_mode=true with no overlay_enforce_validated field → OK."""
        cfg = _minimal_config(shadow_mode=True)
        assert "overlay_enforce_validated" not in cfg.get("overlay", {})
        errs = _overlay_errors(cfg)
        assert errs == []

    def test_config_with_shadow_false_and_validated_true_passes(self):
        """shadow_mode=false + overlay_enforce_validated=true → gate passes."""
        cfg = _minimal_config(shadow_mode=False, overlay_enforce_validated=True)
        errs = _overlay_errors(cfg)
        assert errs == [], f"Should pass with validated=True but got: {errs}"

    def test_config_with_shadow_false_and_no_validated_flag_fails(self):
        """shadow_mode=false with no overlay_enforce_validated → [OVERLAY_ENFORCE] raised."""
        cfg = _minimal_config(shadow_mode=False)
        assert "overlay_enforce_validated" not in cfg.get("overlay", {})
        errs = _overlay_errors(cfg)
        assert len(errs) == 1, f"Expected exactly 1 error, got: {errs}"
        assert "[OVERLAY_ENFORCE]" in errs[0]
        assert "overlay_enforce_validated=true" in errs[0]

    def test_config_with_shadow_false_and_validated_false_fails(self):
        """shadow_mode=false + overlay_enforce_validated=false → gate fires."""
        cfg = _minimal_config(shadow_mode=False, overlay_enforce_validated=False)
        errs = _overlay_errors(cfg)
        assert len(errs) == 1, f"Expected exactly 1 error, got: {errs}"
        assert "[OVERLAY_ENFORCE]" in errs[0]


class TestOverlayEnforceGateEdgeCases:
    """Edge cases for the gate."""

    def test_no_overlay_section_passes(self):
        """Config with no overlay section at all → gate is silent."""
        cfg = _minimal_config()  # no overlay fields
        assert "overlay" not in cfg
        errs = _overlay_errors(cfg)
        assert errs == []

    def test_shadow_mode_absent_passes(self):
        """overlay present but shadow_mode absent → gate is silent."""
        cfg = _minimal_config(enabled=True, mode="log_only")
        assert "shadow_mode" not in cfg["overlay"]
        errs = _overlay_errors(cfg)
        assert errs == []

    def test_shadow_mode_none_does_not_trigger_gate(self):
        """shadow_mode=None (not False) → gate is silent."""
        cfg = _minimal_config(shadow_mode=None)
        errs = _overlay_errors(cfg)
        assert errs == []

    def test_validated_true_string_is_not_accepted(self):
        """overlay_enforce_validated='true' (string) is not the bool True → gate fires."""
        cfg = _minimal_config(shadow_mode=False, overlay_enforce_validated="true")
        errs = _overlay_errors(cfg)
        # 'true' string is not `is True` in Python — gate should fire
        assert len(errs) == 1
        assert "[OVERLAY_ENFORCE]" in errs[0]

    def test_error_message_contains_backtest_hint(self):
        """Error message should reference the backtest script."""
        cfg = _minimal_config(shadow_mode=False)
        errs = _overlay_errors(cfg)
        assert len(errs) == 1
        assert "backtest_overlay_phase3a" in errs[0]


class TestSp500ConfigAfterFlip:
    """Verify the sp500 config passes validation after the Phase 3A flip."""

    def test_sp500_overlay_block_has_shadow_mode_false(self):
        """sp500.json overlay.shadow_mode should now be false."""
        cfg_path = _ROOT / "config" / "active" / "sp500.json"
        cfg = json.loads(cfg_path.read_text())
        overlay = cfg.get("overlay", {})
        assert overlay.get("shadow_mode") is False, (
            f"Expected shadow_mode=False, got: {overlay.get('shadow_mode')}"
        )

    def test_sp500_overlay_block_has_enforce_validated_true(self):
        """sp500.json overlay.overlay_enforce_validated should be true."""
        cfg_path = _ROOT / "config" / "active" / "sp500.json"
        cfg = json.loads(cfg_path.read_text())
        overlay = cfg.get("overlay", {})
        assert overlay.get("overlay_enforce_validated") is True, (
            f"Expected overlay_enforce_validated=True, got: {overlay.get('overlay_enforce_validated')}"
        )

    def test_sp500_config_passes_full_validation(self):
        """sp500.json should have zero validation errors after the flip."""
        cfg_path = _ROOT / "config" / "active" / "sp500.json"
        errs = validate_config_file(cfg_path)
        assert errs == [], f"sp500.json validation failed:\n" + "\n".join(errs)

    def test_commodity_etfs_still_in_shadow_mode(self):
        """commodity_etfs.json should still have shadow_mode=true."""
        cfg_path = _ROOT / "config" / "active" / "commodity_etfs.json"
        if not cfg_path.exists():
            pytest.skip("commodity_etfs.json not found")
        cfg = json.loads(cfg_path.read_text())
        overlay = cfg.get("overlay", {})
        assert overlay.get("shadow_mode") is True, (
            f"commodity_etfs should stay in shadow mode, got: {overlay.get('shadow_mode')}"
        )

    def test_sector_etfs_still_in_shadow_mode(self):
        """sector_etfs.json should still have shadow_mode=true."""
        cfg_path = _ROOT / "config" / "active" / "sector_etfs.json"
        if not cfg_path.exists():
            pytest.skip("sector_etfs.json not found")
        cfg = json.loads(cfg_path.read_text())
        overlay = cfg.get("overlay", {})
        assert overlay.get("shadow_mode") is True, (
            f"sector_etfs should stay in shadow mode, got: {overlay.get('shadow_mode')}"
        )


class TestBacktestScript:
    """Verify the backtest script returns the correct structure."""

    def test_backtest_returns_flip_decision(self, tmp_path):
        """run_backtest() on the real DB should return FLIP for the 7-day window."""
        sys.path.insert(0, str(_ROOT))
        from scripts.backtest_overlay_phase3a import run_backtest

        result = run_backtest(window_days=7)

        # Structure checks
        assert "decision" in result
        assert "actual_cumulative_pnl" in result
        assert "hypo_cumulative_pnl" in result
        assert "cumulative_delta" in result
        assert "rows" in result
        assert "trading_days" in result
        assert len(result["trading_days"]) == 7

    def test_backtest_cumulative_delta_positive(self):
        """The 7-day window delta should be positive (enforce saved money)."""
        from scripts.backtest_overlay_phase3a import run_backtest
        result = run_backtest(window_days=7)
        assert result["cumulative_delta"] > 0, (
            f"Expected positive delta but got {result['cumulative_delta']}"
        )

    def test_backtest_decision_is_flip(self):
        """The decision for the 7-day window should be FLIP."""
        from scripts.backtest_overlay_phase3a import run_backtest
        result = run_backtest(window_days=7)
        assert result["decision"] == "FLIP", (
            f"Expected FLIP but got {result['decision']}"
        )

    def test_backtest_n_tighten_affected_trades(self):
        """Should report exactly 3 tighten-affected closed sp500 trades."""
        from scripts.backtest_overlay_phase3a import run_backtest
        result = run_backtest(window_days=7)
        assert result["n_trades_tighten_affected"] == 3, (
            f"Expected 3 tighten-affected trades, got {result['n_trades_tighten_affected']}"
        )

    def test_backtest_flip_criteria_both_pass(self):
        """Both flip criteria should pass for the 7-day window."""
        from scripts.backtest_overlay_phase3a import run_backtest
        result = run_backtest(window_days=7)
        crit = result["flip_criteria"]
        assert crit["cumulative_delta_ge_001"] is True
        assert crit["median_delta_ge_0"] is True
