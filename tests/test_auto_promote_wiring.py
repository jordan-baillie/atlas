"""Tests for auto_promote wiring in autoresearch_runner.py.

Tests:
    1. kept=0 → returns None (unconditional skip)
    2. delta_sharpe < 0.05 → returns {promoted: False, reason: "delta_sharpe..."}
    3. kept=5, delta ≥ 0.05, params available → calls auto_promote with correct args
    4. final_sharpe <= 0 → refuses with "negative final sharpe" reason
    5. --no-auto-promote CLI flag → parses auto_promote_enabled=False
    6. run_session() calls promotion when flag on, skips when off
"""

import sys
import types
from pathlib import Path
from unittest.mock import MagicMock, patch, call
import pytest

ATLAS_ROOT = Path(__file__).resolve().parent.parent
if str(ATLAS_ROOT) not in sys.path:
    sys.path.insert(0, str(ATLAS_ROOT))

from research.autoresearch_runner import _promote_session_result, _parse_args


# ─── Test 1: kept=0 returns None ─────────────────────────────────────────────

class TestPromoteSessionSkipOnKeptZero:
    def test_kept_zero_returns_none(self):
        result = _promote_session_result(
            strategy="mean_reversion",
            market="sp500",
            universe="sp500",
            kept=0,
            starting_sharpe=1.0,
            final_sharpe=1.2,
        )
        assert result is None, "kept=0 must return None (unconditional skip)"

    def test_kept_negative_returns_none(self):
        result = _promote_session_result(
            strategy="momentum_breakout",
            market="sp500",
            universe="sp500",
            kept=-1,
            starting_sharpe=0.5,
            final_sharpe=0.8,
        )
        assert result is None, "kept<0 must also return None"


# ─── Test 2: delta_sharpe < 0.05 gate ────────────────────────────────────────

class TestPromoteSessionDeltaGate:
    def test_tiny_improvement_fails_client_gate(self):
        result = _promote_session_result(
            strategy="mean_reversion",
            market="sp500",
            universe="sp500",
            kept=3,
            starting_sharpe=1.0,
            final_sharpe=1.04,  # delta = 0.04 < 0.05
        )
        assert result is not None
        assert result["promoted"] is False
        assert "0.05" in result["reason"] or "delta_sharpe" in result["reason"], (
            f"Expected delta_sharpe gate reason, got: {result['reason']}"
        )

    def test_exact_zero_delta_fails_gate(self):
        result = _promote_session_result(
            strategy="mean_reversion",
            market="sp500",
            universe="sp500",
            kept=2,
            starting_sharpe=1.5,
            final_sharpe=1.5,  # delta = 0.0
        )
        assert result is not None
        assert result["promoted"] is False

    def test_negative_delta_fails_gate(self):
        # Regression: final_sharpe > 0 but delta < 0
        result = _promote_session_result(
            strategy="mean_reversion",
            market="sp500",
            universe="sp500",
            kept=1,
            starting_sharpe=1.5,
            final_sharpe=1.0,  # delta = -0.5
        )
        assert result is not None
        assert result["promoted"] is False


# ─── Test 3: delta ≥ 0.05, params available → auto_promote called ────────────

class TestPromoteSessionCallsAutoPromote:
    def test_calls_auto_promote_with_correct_args(self):
        fake_params = {"rsi_period": 14, "bb_window": 20}
        fake_outcome = {"promoted": True, "reason": "all gates passed", "version": "v2.1.0"}

        with (
            patch("research.autoresearch_runner._promote_session_result.__module__"),
            patch("db.atlas_db.get_research_best") as mock_best,
            patch("research.promoter.auto_promote") as mock_promote,
        ):
            # Simulate SQLite returning a best row with params
            mock_best.return_value = [
                {"params": fake_params, "sharpe": 1.12, "trades": 50, "max_dd_pct": 15.0}
            ]
            mock_promote.return_value = fake_outcome

            result = _promote_session_result(
                strategy="mean_reversion",
                market="sp500",
                universe="sp500",
                kept=5,
                starting_sharpe=1.0,
                final_sharpe=1.10,  # delta = 0.10 ≥ 0.05
            )

        assert result is not None
        assert result.get("promoted") is True

        mock_promote.assert_called_once()
        call_kwargs = mock_promote.call_args[1]
        assert call_kwargs["strategy"] == "mean_reversion"
        assert call_kwargs["market"] == "sp500"
        assert call_kwargs["improved_params"] == fake_params
        assert abs(call_kwargs["initial_sharpe"] - 1.0) < 1e-6
        # final_sharpe passed to auto_promote should come from research_best sharpe (1.12)
        assert abs(call_kwargs["final_sharpe"] - 1.12) < 1e-6

    def test_outcome_includes_strategy_key(self):
        fake_params = {"rsi_period": 14}
        fake_outcome = {"promoted": False, "reason": "cooldown active", "version": None}

        with (
            patch("db.atlas_db.get_research_best") as mock_best,
            patch("research.promoter.auto_promote") as mock_promote,
        ):
            mock_best.return_value = [{"params": fake_params, "sharpe": 1.1, "trades": 30, "max_dd_pct": 12.0}]
            mock_promote.return_value = fake_outcome

            result = _promote_session_result(
                strategy="momentum_breakout",
                market="sp500",
                universe="sp500",
                kept=3,
                starting_sharpe=1.0,
                final_sharpe=1.10,
            )

        assert result is not None
        assert result.get("strategy") == "momentum_breakout"


# ─── Test 4: final_sharpe <= 0 refusal ───────────────────────────────────────

class TestPromoteSessionNegativeSharpe:
    def test_negative_final_sharpe_refused(self):
        result = _promote_session_result(
            strategy="mean_reversion",
            market="sp500",
            universe="sp500",
            kept=3,
            starting_sharpe=-1.0,
            final_sharpe=-0.5,  # "improved" but still negative
        )
        assert result is not None
        assert result["promoted"] is False
        assert "negative" in result["reason"].lower() or "sharpe" in result["reason"].lower(), (
            f"Expected negative-sharpe reason, got: {result['reason']}"
        )

    def test_zero_final_sharpe_refused(self):
        result = _promote_session_result(
            strategy="trend_following",
            market="sp500",
            universe="sp500",
            kept=2,
            starting_sharpe=-0.5,
            final_sharpe=0.0,  # exactly zero — edge case
        )
        assert result is not None
        assert result["promoted"] is False

    def test_none_sharpe_refused(self):
        result = _promote_session_result(
            strategy="mean_reversion",
            market="sp500",
            universe="sp500",
            kept=3,
            starting_sharpe=None,
            final_sharpe=1.2,
        )
        assert result is not None
        assert result["promoted"] is False
        assert "missing" in result["reason"].lower() or "none" in result["reason"].lower() or "sharpe" in result["reason"].lower()


# ─── Test 5: --no-auto-promote CLI flag ──────────────────────────────────────

class TestCliFlag:
    def test_default_auto_promote_enabled(self):
        args = _parse_args(["--strategy", "mean_reversion", "--hours", "1"])
        assert args.auto_promote_enabled is True

    def test_no_auto_promote_flag_disables(self):
        args = _parse_args(["--strategy", "mean_reversion", "--hours", "1", "--no-auto-promote"])
        assert args.auto_promote_enabled is False

    def test_all_other_args_still_parse(self):
        args = _parse_args([
            "--strategy", "momentum_breakout",
            "--market", "sp500",
            "--universe", "sp500",
            "--hours", "2.5",
            "--notify",
            "--no-fast-screen",
            "--no-auto-promote",
        ])
        assert args.strategy == "momentum_breakout"
        assert args.hours == 2.5
        assert args.notify is True
        assert args.fast_screen is False
        assert args.auto_promote_enabled is False


# ─── Test 6: run_session() integration ───────────────────────────────────────

class TestRunSessionPromotion:
    """End-to-end test via run_session() with all heavy components mocked."""

    def _build_mock_session(self):
        """Build a minimal mock ResearchSession that returns baseline + one kept."""
        mock_session = MagicMock()
        mock_session.session_id = "test-session-001"
        mock_session._config = {"market": "sp500"}
        mock_session._data = {"AAPL": MagicMock(), "MSFT": MagicMock()}
        mock_session._best_params = {"rsi_period": 14}
        mock_session.market = "sp500"
        mock_session.baseline.return_value = {"sharpe": 1.0, "total_trades": 100, "cagr_pct": 15.0}
        mock_session.experiment.return_value = {
            "recommendation": "keep",
            "metrics": {"sharpe": 1.15, "total_trades": 95, "cagr_pct": 14.0},
            "delta": {"sharpe": 0.15},
            "rationale": "improved",
        }
        return mock_session

    def test_promotion_called_when_flag_on(self):
        """When auto_promote_enabled=True and kept>0 with sufficient delta, _promote_session_result is called.

        ResearchSession is imported lazily inside run_session(), so we patch
        research.loop.ResearchSession rather than research.autoresearch_runner.ResearchSession.
        """
        from research.autoresearch_runner import run_session

        promo_outcome = {"promoted": False, "reason": "cooldown active", "strategy": "mean_reversion"}

        with (
            patch("research.loop.ResearchSession") as MockSession,
            patch("research.autoresearch_runner.build_sweep_plan") as mock_plan,
            patch("research.autoresearch_runner._vectorised_presort", side_effect=lambda s, p, d, b: p),
            patch("research.autoresearch_runner._run_solo_screen") as mock_solo,
            patch("research.autoresearch_runner._promote_session_result") as mock_promote,
            patch("research.autoresearch_runner._try_send_telegram"),
        ):
            mock_session = self._build_mock_session()
            MockSession.return_value = mock_session
            # One candidate in the plan
            mock_plan.return_value = [("rsi_period=12", "rsi_period", 12)]
            # Solo screen passes — candidate will be promoted to combined verify
            mock_solo.return_value = (
                {"sharpe": 1.1, "total_trades": 90, "runtime_s": 1.0},
                {"decision": "keep", "delta_sharpe": 0.1, "rationale": "improved"},
            )
            mock_promote.return_value = promo_outcome

            summary = run_session(
                strategy="mean_reversion",
                market="sp500",
                universe="sp500",
                hours=0.001,  # tiny budget — ends after baseline+one experiment
                notify=False,
                auto_promote_enabled=True,
            )

        # Promotion should have been attempted
        mock_promote.assert_called_once()
        call_kwargs = mock_promote.call_args[1]
        assert call_kwargs["strategy"] == "mean_reversion"
        assert call_kwargs["market"] == "sp500"
        # Summary should contain the promotion key when promo ran
        assert "promotion" in summary

    def test_promotion_skipped_when_flag_off(self):
        """When auto_promote_enabled=False, _promote_session_result is NOT called."""
        from research.autoresearch_runner import run_session

        with (
            patch("research.loop.ResearchSession") as MockSession,
            patch("research.autoresearch_runner.build_sweep_plan") as mock_plan,
            patch("research.autoresearch_runner._vectorised_presort", side_effect=lambda s, p, d, b: p),
            patch("research.autoresearch_runner._promote_session_result") as mock_promote,
            patch("research.autoresearch_runner._try_send_telegram"),
        ):
            mock_session = self._build_mock_session()
            MockSession.return_value = mock_session
            mock_plan.return_value = []  # empty plan — session completes immediately

            summary = run_session(
                strategy="mean_reversion",
                market="sp500",
                universe="sp500",
                hours=0.001,
                notify=False,
                auto_promote_enabled=False,
            )

        # Promotion should NOT have been called
        mock_promote.assert_not_called()
        assert "promotion" not in summary
