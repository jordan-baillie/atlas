"""Regression tests for momentum_breakout same-bar stop mitigation.

Covers:
  - Config value flows through to MomentumBreakout.atr_stop_mult correctly
  - Sentinel: currently shipped atr_stop_mult value (0.61 — no variant shipped)

Backtest comparison run 2026-05-20:
  No variant met all 3 decision criteria:
    B-2.5/B-3.0 did not achieve ≥50% proxy SB-rate reduction at the daily-bar
    target (≤6.1%); B-3.5 achieved reduction but destroyed Sharpe (−0.27 vs
    baseline 0.65). Config unchanged. See:
    docs/project-notes/same-bar-mitigation-decision-2026-05-20.md
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

PROJECT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_sp500_config() -> dict:
    cfg_path = PROJECT / "config" / "active" / "sp500.json"
    return json.loads(cfg_path.read_text())


def _make_mb_instance(atr_stop_mult: float | None = None):
    """Instantiate MomentumBreakout from a minimal config."""
    from strategies.momentum_breakout import MomentumBreakout

    cfg = _load_sp500_config()
    if atr_stop_mult is not None:
        cfg["strategies"]["momentum_breakout"]["atr_stop_mult"] = atr_stop_mult
    return MomentumBreakout(cfg)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestAtrStopMultFlowsToInstance:
    """Verify the config param propagates into the strategy instance."""

    def test_momentum_breakout_atr_stop_mult_honored(self):
        """Regression: atr_stop_mult in sp500.json flows into MomentumBreakout."""
        cfg = _load_sp500_config()
        config_val = cfg["strategies"]["momentum_breakout"]["atr_stop_mult"]

        from strategies.momentum_breakout import MomentumBreakout
        mb = MomentumBreakout(cfg)

        assert mb.atr_stop_mult == config_val, (
            f"MomentumBreakout.atr_stop_mult ({mb.atr_stop_mult}) "
            f"does not match config value ({config_val}). "
            "Check strategies/momentum_breakout.py __init__ param wiring."
        )

    def test_atr_stop_mult_custom_value_honored(self):
        """Patching atr_stop_mult in config must propagate to instance."""
        custom_val = 0.99
        mb = _make_mb_instance(atr_stop_mult=custom_val)
        assert mb.atr_stop_mult == custom_val, (
            f"Expected atr_stop_mult={custom_val}, got {mb.atr_stop_mult}"
        )

    def test_atr_stop_mult_is_numeric(self):
        """atr_stop_mult must be a positive float."""
        mb = _make_mb_instance()
        assert isinstance(mb.atr_stop_mult, (int, float)), (
            f"atr_stop_mult must be numeric, got {type(mb.atr_stop_mult)}"
        )
        assert mb.atr_stop_mult > 0, (
            f"atr_stop_mult must be positive, got {mb.atr_stop_mult}"
        )


class TestAtrStopMultSentinel:
    """Sentinel: assert the currently shipped atr_stop_mult value.

    If a future variant ships, update the expected value here AND update the
    config version + description in sp500.json.

    Current shipped value: 0.61
      Reason: No variant met all 3 decision criteria in the 2026-05-20
      backtest comparison. See docs/project-notes/same-bar-mitigation-
      decision-2026-05-20.md for the full decision rationale.
    """

    # -----------------------------------------------------------------------
    # UPDATE THIS if a variant ships in the future:
    EXPECTED_ATR_STOP_MULT = 0.61
    # -----------------------------------------------------------------------

    def test_momentum_breakout_atr_stop_mult_target_value(self):
        """Sentinel: confirm currently shipped atr_stop_mult value.

        This test is a DELIBERATE no-ship sentinel. It will fail if someone
        changes atr_stop_mult without updating this test — forcing a conscious
        review of the backtest comparison decision.
        """
        cfg = _load_sp500_config()
        actual = cfg["strategies"]["momentum_breakout"]["atr_stop_mult"]
        assert actual == self.EXPECTED_ATR_STOP_MULT, (
            f"momentum_breakout atr_stop_mult is {actual!r}, "
            f"expected {self.EXPECTED_ATR_STOP_MULT!r}.\n"
            "If shipping a new value: update EXPECTED_ATR_STOP_MULT here, "
            "update sp500.json version+description, and reference the "
            "supporting backtest results in data/same_bar_mitigation_comparison_*.json"
        )

    def test_sp500_config_version_unchanged(self):
        """Sentinel: config version must stay at v3.2.4 (no-ship = no version bump)."""
        cfg = _load_sp500_config()
        version = cfg.get("version", "")
        assert version == "v3.2.4", (
            f"sp500.json version is {version!r}, expected 'v3.2.4'. "
            "Config was not supposed to change in the 2026-05-20 same-bar "
            "mitigation decision (no variant met criteria). "
            "If a version bump is intentional, update this sentinel."
        )

    def test_momentum_breakout_enabled(self):
        """momentum_breakout must remain enabled after the no-ship decision."""
        cfg = _load_sp500_config()
        enabled = cfg["strategies"]["momentum_breakout"].get("enabled", False)
        assert enabled is True, (
            "momentum_breakout has been disabled in sp500.json unexpectedly."
        )
