"""Unit tests for Telegram regime context in plan notifications.

Tests cover:
  - Regime section formatting for all 6 regime states
  - Emoji mapping correctness
  - Transition detection (prev day change)
  - Omission when regime_enabled=False (no regime_state in plan)
  - Full format_plan_message integration
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch
import tempfile

import pytest

# Import the functions under test
from services.telegram_bot import (
    REGIME_EMOJI,
    _format_regime_section,
    _load_prev_regime_state,
    format_plan_message,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_plan(
    regime_state: str | None = None,
    active_universes: list | None = None,
    sizing_multiplier: float = 1.0,
    reasoning: str = "",
    trade_date: str = "2026-04-01",
    market_id: str = "sp500",
) -> dict:
    """Build a minimal plan dict for testing."""
    plan: dict = {
        "trade_date": trade_date,
        "market_id": market_id,
        "proposed_entries": [],
        "proposed_exits": [],
        "portfolio_snapshot": {
            "equity": 10_000.0,
            "cash": 5_000.0,
            "total_pnl": 100.0,
            "total_pnl_pct": 1.0,
            "open_positions": 0,
        },
        "risk_summary": {
            "total_proposed_cost": 0.0,
            "total_proposed_risk": 0.0,
            "portfolio_exposure_pct": 0.0,
        },
    }
    if regime_state is not None:
        plan["regime_state"] = regime_state
        plan["active_universes"] = active_universes or ["sp500"]
        plan["sizing_multiplier"] = sizing_multiplier
        plan["regime_reasoning"] = reasoning
    return plan


def _write_prev_plan(plans_dir: Path, trade_date: str, market_id: str, regime_state: str) -> None:
    """Write a minimal prior-day plan file for transition-detection tests."""
    fname = plans_dir / f"plan_{market_id}_{trade_date}.json"
    fname.write_text(json.dumps({"trade_date": trade_date, "regime_state": regime_state}))


# ---------------------------------------------------------------------------
# 1. Regime emoji mapping
# ---------------------------------------------------------------------------

class TestRegimeEmojiMapping:
    """REGIME_EMOJI covers all 6 known regime states."""

    ALL_STATES = [
        ("bull_risk_on",        "🟢"),
        ("bull_risk_off",       "🟡"),
        ("transition_uncertain","🟠"),
        ("bear_risk_off",       "🔴"),
        ("bear_capitulation",   "⛔"),
        ("recovery_early",      "🔵"),
    ]

    @pytest.mark.parametrize("state,expected_emoji", ALL_STATES)
    def test_emoji_correct(self, state: str, expected_emoji: str):
        assert REGIME_EMOJI[state] == expected_emoji

    def test_all_six_states_present(self):
        assert len(REGIME_EMOJI) == 6

    def test_unknown_state_returns_neutral_via_get(self):
        """REGIME_EMOJI.get() with a default should handle unknown states."""
        unknown_emoji = REGIME_EMOJI.get("some_future_state", "⚪")
        assert unknown_emoji == "⚪"


# ---------------------------------------------------------------------------
# 2. _format_regime_section formatting
# ---------------------------------------------------------------------------

class TestFormatRegimeSection:
    """_format_regime_section returns correct HTML for well-formed plan dicts."""

    def test_empty_when_no_regime_state(self):
        """No regime_state in plan → empty string (regime disabled)."""
        plan = _make_plan()  # no regime fields
        assert _format_regime_section(plan) == ""

    def test_empty_when_regime_state_none(self):
        plan = _make_plan()
        plan["regime_state"] = None
        assert _format_regime_section(plan) == ""

    def test_empty_when_regime_state_falsy(self):
        plan = _make_plan()
        plan["regime_state"] = ""
        assert _format_regime_section(plan) == ""

    @pytest.mark.parametrize("state,emoji", [
        ("bull_risk_on",        "🟢"),
        ("bull_risk_off",       "🟡"),
        ("transition_uncertain","🟠"),
        ("bear_risk_off",       "🔴"),
        ("bear_capitulation",   "⛔"),
        ("recovery_early",      "🔵"),
    ])
    def test_emoji_appears_for_each_state(self, state: str, emoji: str):
        plan = _make_plan(regime_state=state)
        section = _format_regime_section(plan)
        assert emoji in section, f"Expected {emoji!r} for state {state!r}"

    def test_state_name_in_output(self):
        plan = _make_plan(regime_state="bull_risk_on")
        section = _format_regime_section(plan)
        assert "bull_risk_on" in section

    def test_universes_listed(self):
        plan = _make_plan(
            regime_state="bull_risk_on",
            active_universes=["sp500", "sector_etfs", "commodity_etfs"],
        )
        section = _format_regime_section(plan)
        assert "sp500, sector_etfs, commodity_etfs" in section

    def test_sizing_multiplier_shown(self):
        plan = _make_plan(regime_state="bear_risk_off", sizing_multiplier=0.3)
        section = _format_regime_section(plan)
        assert "0.3x" in section

    def test_reasoning_included_when_present(self):
        plan = _make_plan(
            regime_state="bull_risk_on",
            reasoning="SPY above 200 DMA, VIX below 20",
        )
        section = _format_regime_section(plan)
        assert "SPY above 200 DMA, VIX below 20" in section

    def test_long_reasoning_truncated(self):
        long_reason = "A" * 200
        plan = _make_plan(regime_state="bull_risk_on", reasoning=long_reason)
        section = _format_regime_section(plan)
        # Should be truncated to ~120 chars + ellipsis
        assert "..." in section
        # The raw long string should not appear verbatim
        assert long_reason not in section

    def test_no_reasoning_field_ok(self):
        """Plan without regime_reasoning should not raise."""
        plan = _make_plan(regime_state="bull_risk_on")
        plan.pop("regime_reasoning", None)
        section = _format_regime_section(plan)
        assert "bull_risk_on" in section

    def test_empty_active_universes_defaults_to_sp500(self):
        """When active_universes is empty / missing, fall back to 'sp500'."""
        plan = _make_plan(regime_state="bull_risk_on", active_universes=[])
        section = _format_regime_section(plan)
        assert "sp500" in section

    def test_html_not_double_escaped(self):
        """State name contains no HTML-special chars — should not be mangled."""
        plan = _make_plan(regime_state="bull_risk_on")
        section = _format_regime_section(plan)
        assert "&lt;" not in section
        assert "&gt;" not in section


# ---------------------------------------------------------------------------
# 3. Transition detection
# ---------------------------------------------------------------------------

class TestTransitionDetection:
    """Regime change from previous day is highlighted with ⚡."""

    def _make_plans_dir(self, tmp_path: Path) -> Path:
        plans_dir = tmp_path / "plans"
        plans_dir.mkdir()
        return plans_dir

    def _patch_project_root(self, tmp_path: Path):
        """Return a context manager that patches PROJECT_ROOT in telegram_bot."""
        import services.telegram_bot as tb

        class FakeRoot:
            def __truediv__(self, other: str) -> Path:
                if other == "plans":
                    return tmp_path / "plans"
                # Fall through for everything else
                return tb.PROJECT_ROOT.__class__.__truediv__(tb.PROJECT_ROOT, other)  # type: ignore

        return patch.object(tb, "PROJECT_ROOT", FakeRoot())

    def test_transition_highlighted_when_state_changed(self, tmp_path: Path):
        plans_dir = self._make_plans_dir(tmp_path)
        _write_prev_plan(plans_dir, "2026-03-31", "sp500", "transition_uncertain")

        plan = _make_plan(
            regime_state="bull_risk_on",
            trade_date="2026-04-01",
            market_id="sp500",
        )

        with self._patch_project_root(tmp_path):
            section = _format_regime_section(plan, "sp500")

        assert "⚡" in section, "Expected transition indicator ⚡"
        assert "transition_uncertain" in section
        assert "🟠" in section  # prev state emoji

    def test_no_transition_when_state_unchanged(self, tmp_path: Path):
        plans_dir = self._make_plans_dir(tmp_path)
        _write_prev_plan(plans_dir, "2026-03-31", "sp500", "bull_risk_on")

        plan = _make_plan(
            regime_state="bull_risk_on",
            trade_date="2026-04-01",
            market_id="sp500",
        )

        with self._patch_project_root(tmp_path):
            section = _format_regime_section(plan, "sp500")

        assert "⚡" not in section, "Should not show transition for unchanged regime"

    def test_no_transition_when_no_prev_plan(self, tmp_path: Path):
        """No previous plan file → gracefully skip transition line."""
        self._make_plans_dir(tmp_path)  # empty plans dir

        plan = _make_plan(
            regime_state="bull_risk_on",
            trade_date="2026-04-01",
            market_id="sp500",
        )

        with self._patch_project_root(tmp_path):
            section = _format_regime_section(plan, "sp500")

        assert "⚡" not in section
        assert "bull_risk_on" in section  # state still shown

    def test_no_transition_when_prev_plan_has_no_regime(self, tmp_path: Path):
        """Previous plan with no regime_state → skip transition line."""
        plans_dir = self._make_plans_dir(tmp_path)
        (plans_dir / "plan_sp500_2026-03-31.json").write_text(
            json.dumps({"trade_date": "2026-03-31"})
        )

        plan = _make_plan(
            regime_state="bull_risk_on",
            trade_date="2026-04-01",
            market_id="sp500",
        )

        with self._patch_project_root(tmp_path):
            section = _format_regime_section(plan, "sp500")

        assert "⚡" not in section

    def test_all_transitions_show_correct_emoji_pairs(self, tmp_path: Path):
        """Each of the 6 prev states produces the right prev-state emoji."""
        plans_dir = self._make_plans_dir(tmp_path)

        for prev_state, prev_emoji in REGIME_EMOJI.items():
            if prev_state == "bull_risk_on":
                continue  # skip same-state case
            _write_prev_plan(plans_dir, "2026-03-31", "sp500", prev_state)

            plan = _make_plan(
                regime_state="bull_risk_on",
                trade_date="2026-04-01",
                market_id="sp500",
            )

            with self._patch_project_root(tmp_path):
                section = _format_regime_section(plan, "sp500")

            assert prev_emoji in section, (
                f"Expected {prev_emoji!r} (prev={prev_state!r}) in section:\n{section}"
            )
            # Clean up for next iteration
            (plans_dir / "plan_sp500_2026-03-31.json").unlink()


# ---------------------------------------------------------------------------
# 4. Integration: format_plan_message includes regime section
# ---------------------------------------------------------------------------

class TestFormatPlanMessageIntegration:
    """format_plan_message correctly embeds regime section."""

    def test_regime_section_present_when_enabled(self):
        plan = _make_plan(
            regime_state="bull_risk_on",
            active_universes=["sp500", "sector_etfs"],
            sizing_multiplier=1.0,
            reasoning="Strong trend signals",
        )
        msg = format_plan_message(plan, "sp500")
        assert "📊 Regime" in msg
        assert "bull_risk_on" in msg
        assert "🟢" in msg
        assert "sp500, sector_etfs" in msg
        assert "1.0x" in msg

    def test_regime_section_absent_when_not_enabled(self):
        """Plan without regime_state → no regime section in message."""
        plan = _make_plan()  # no regime fields
        msg = format_plan_message(plan, "sp500")
        assert "📊 Regime" not in msg

    def test_regime_section_absent_when_regime_state_none(self):
        plan = _make_plan()
        plan["regime_state"] = None
        msg = format_plan_message(plan, "sp500")
        assert "📊 Regime" not in msg

    def test_message_structure_unchanged_without_regime(self):
        """Existing plan message structure is preserved when regime is absent."""
        plan = _make_plan()
        plan["proposed_entries"] = [
            {
                "ticker": "AAPL",
                "strategy": "momentum",
                "entry_price": 150.0,
                "position_size": 5,
                "confidence": 0.75,
            }
        ]
        msg = format_plan_message(plan, "sp500")
        assert "Atlas Trade Plan" in msg
        assert "AAPL" in msg
        assert "Mode:" in msg

    def test_bear_capitulation_emoji_in_full_message(self):
        plan = _make_plan(
            regime_state="bear_capitulation",
            active_universes=["defensive_etfs"],
            sizing_multiplier=0.0,
        )
        msg = format_plan_message(plan, "sp500")
        assert "⛔" in msg
        assert "bear_capitulation" in msg

    def test_recovery_early_emoji_in_full_message(self):
        plan = _make_plan(
            regime_state="recovery_early",
            active_universes=["sp500"],
            sizing_multiplier=0.6,
        )
        msg = format_plan_message(plan, "sp500")
        assert "🔵" in msg
        assert "recovery_early" in msg


# ---------------------------------------------------------------------------
# 5. _load_prev_regime_state edge cases
# ---------------------------------------------------------------------------

class TestLoadPrevRegimeState:
    """_load_prev_regime_state handles edge cases gracefully."""

    def test_returns_none_for_missing_plans_dir(self, tmp_path: Path):
        import services.telegram_bot as tb
        with patch.object(tb, "PROJECT_ROOT", tmp_path):
            result = _load_prev_regime_state("2026-04-01", "sp500")
        assert result is None

    def test_returns_none_for_empty_market_id(self, tmp_path: Path):
        result = _load_prev_regime_state("2026-04-01", "")
        assert result is None

    def test_returns_none_for_empty_trade_date(self):
        result = _load_prev_regime_state("", "sp500")
        assert result is None

    def test_returns_regime_state_from_most_recent_prior_plan(self, tmp_path: Path):
        plans_dir = tmp_path / "plans"
        plans_dir.mkdir()
        # Older plan
        _write_prev_plan(plans_dir, "2026-03-30", "sp500", "bear_risk_off")
        # More recent plan (should be selected)
        _write_prev_plan(plans_dir, "2026-03-31", "sp500", "transition_uncertain")

        import services.telegram_bot as tb
        with patch.object(tb, "PROJECT_ROOT", tmp_path):
            result = _load_prev_regime_state("2026-04-01", "sp500")

        assert result == "transition_uncertain"

    def test_does_not_return_same_day_plan(self, tmp_path: Path):
        """Plans on the same date as trade_date should not be returned."""
        plans_dir = tmp_path / "plans"
        plans_dir.mkdir()
        _write_prev_plan(plans_dir, "2026-04-01", "sp500", "bull_risk_on")  # same day

        import services.telegram_bot as tb
        with patch.object(tb, "PROJECT_ROOT", tmp_path):
            result = _load_prev_regime_state("2026-04-01", "sp500")

        assert result is None

    def test_handles_corrupted_plan_file_gracefully(self, tmp_path: Path):
        """Corrupted plan JSON should not raise — return None instead."""
        plans_dir = tmp_path / "plans"
        plans_dir.mkdir()
        (plans_dir / "plan_sp500_2026-03-31.json").write_text("not valid json {{{{")

        import services.telegram_bot as tb
        with patch.object(tb, "PROJECT_ROOT", tmp_path):
            result = _load_prev_regime_state("2026-04-01", "sp500")

        assert result is None
