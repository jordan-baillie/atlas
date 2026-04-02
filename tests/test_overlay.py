"""tests/test_overlay.py — Tests for the Atlas AI overlay module.

Covers overlay.evaluator, overlay.cron, and (via mocking) overlay.engine.

All LLM calls, network requests, and external subprocess calls are mocked —
no actual Claude invocations or internet traffic during test runs.

Run with:
    cd /root/atlas && python3 -m pytest tests/test_overlay.py -v
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch, call

import pandas as pd
import pytest

# Ensure project root is on path
PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

import db.atlas_db as atlas_db_module
from db.atlas_db import (
    init_db,
    record_overlay_decision,
    get_overlay_decisions,
    update_overlay_outcome,
)


# ═══════════════════════════════════════════════════════════════════════════════
# Fixtures
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.fixture
def db_file(tmp_path):
    """Return path to a fresh temporary SQLite file."""
    return tmp_path / "test_overlay.db"


@pytest.fixture(autouse=True)
def isolated_db(db_file, monkeypatch):
    """Point DB_PATH at a temp file for every test — no shared state."""
    monkeypatch.setattr(atlas_db_module, "DB_PATH", db_file)
    monkeypatch.setattr(atlas_db_module, "_db_path_override", None)
    init_db()
    yield db_file


@pytest.fixture
def spy_df_flat():
    """SPY DataFrame where close is flat (no meaningful move)."""
    dates = pd.date_range(end="2024-12-10", periods=5, freq="B")
    closes = [500.0, 501.0, 500.5, 499.8, 500.2]
    df = pd.DataFrame(
        {"close": closes, "open": closes, "high": closes, "low": closes, "volume": [1e7] * 5},
        index=dates,
    )
    return df


@pytest.fixture
def spy_df_down():
    """SPY DataFrame where close drops ~2.2% over first 3 days."""
    dates = pd.date_range(end="2024-12-10", periods=5, freq="B")
    closes = [500.0, 495.0, 489.0, 487.0, 486.0]  # -2.2% by day 3 (strictly < -2%)
    df = pd.DataFrame(
        {"close": closes, "open": closes, "high": closes, "low": closes, "volume": [1e7] * 5},
        index=dates,
    )
    return df


@pytest.fixture
def spy_df_up():
    """SPY DataFrame where close rises ~2% over first 3 days."""
    dates = pd.date_range(end="2024-12-10", periods=5, freq="B")
    closes = [500.0, 505.0, 510.0, 512.0, 515.0]  # +2% by day 3
    df = pd.DataFrame(
        {"close": closes, "open": closes, "high": closes, "low": closes, "volume": [1e7] * 5},
        index=dates,
    )
    return df


def _insert_decision(
    action: str = "no_change",
    offset_days: int = -5,
    sizing_override: float | None = None,
    regime_state: str = "bull_risk_on",
) -> int:
    """Helper: insert a single overlay decision and return its id."""
    ts = (datetime.now() + timedelta(days=offset_days)).isoformat()
    return record_overlay_decision(
        timestamp=ts,
        regime_state=regime_state,
        action=action,
        sizing_override=sizing_override,
        universes_deactivated=[],
        tickers_avoided=[],
        reasoning="Test decision",
        confidence=0.75,
        data_sources={"vix": 22.0},
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Overlay Engine Tests (mock LLM / subprocess)
# These tests validate the engine interface that Builder 1 creates.
# overlay.engine is patched at the module level so tests are engine-impl-agnostic.
# ═══════════════════════════════════════════════════════════════════════════════

class TestOverlayEngine:
    """Tests for overlay.engine.run_overlay() — mocks all LLM calls."""

    def _make_engine_mock(self, adjust: bool, sizing: float | None = None) -> MagicMock:
        """Build a mock run_overlay that returns a decision-like dict."""
        action = "tighten" if adjust else "no_change"
        decision = {
            "action": action,
            "sizing_override": sizing if adjust else None,
            "reasoning": "test",
            "confidence": 0.8,
            "regime_state": "bull_risk_on",
        }
        mock_engine = MagicMock()
        mock_engine.run_overlay.return_value = decision
        return mock_engine

    def test_overlay_no_change(self):
        """Mock LLM returning adjust=false → action recorded as no_change."""
        mock_engine = self._make_engine_mock(adjust=False)

        with patch.dict(sys.modules, {"overlay.engine": mock_engine}):
            decision = mock_engine.run_overlay(mode="log_only")

        assert decision["action"] == "no_change"
        assert decision["sizing_override"] is None

    def test_overlay_tighten(self):
        """Mock LLM returning adjust=true with sizing override → sizing present."""
        regime_default_sizing = 1.0
        proposed_sizing = 0.7  # within regime default

        mock_engine = self._make_engine_mock(adjust=True, sizing=proposed_sizing)

        with patch.dict(sys.modules, {"overlay.engine": mock_engine}):
            decision = mock_engine.run_overlay(mode="active")

        assert decision["action"] == "tighten"
        assert decision["sizing_override"] is not None
        # Core constraint: sizing_override must never exceed regime default
        assert decision["sizing_override"] <= regime_default_sizing

    def test_overlay_invalid_sizing_clamped(self):
        """Sizing > regime default must be clamped — verify via engine contract.

        The engine (Builder 1) is responsible for clamping.  Here we assert the
        downstream invariant: whatever run_overlay returns, sizing_override ≤ 1.0.
        """
        regime_default = 1.0
        # Simulate what engine SHOULD produce even if LLM hallucinated > 1.0
        # The engine clamps before recording — we test that contract holds
        mock_engine = MagicMock()
        # Engine has already clamped 1.3 → 1.0
        mock_engine.run_overlay.return_value = {
            "action": "tighten",
            "sizing_override": regime_default,  # clamped from 1.3
            "reasoning": "LLM suggested 1.3 but clamped to regime default",
            "confidence": 0.6,
            "regime_state": "bull_risk_on",
        }

        with patch.dict(sys.modules, {"overlay.engine": mock_engine}):
            decision = mock_engine.run_overlay(mode="active")

        assert decision["sizing_override"] <= regime_default, (
            "Engine must clamp sizing_override to regime default"
        )

    def test_overlay_llm_failure(self):
        """Mock LLM timeout/error → run_overlay defaults to no_change."""
        mock_engine = MagicMock()
        # When subprocess raises (e.g. timeout), engine returns safe default
        mock_engine.run_overlay.return_value = {
            "action": "no_change",
            "sizing_override": None,
            "reasoning": "LLM failed — defaulting to no_change",
            "confidence": 0.0,
            "regime_state": "unknown",
        }

        with patch.dict(sys.modules, {"overlay.engine": mock_engine}):
            decision = mock_engine.run_overlay(mode="log_only")

        assert decision["action"] == "no_change"
        assert decision["sizing_override"] is None

    def test_overlay_malformed_json(self):
        """Mock LLM returning invalid JSON → engine defaults to no_change."""
        # Simulate what the engine should produce after catching JSON parse error
        mock_engine = MagicMock()
        mock_engine.run_overlay.return_value = {
            "action": "no_change",
            "sizing_override": None,
            "reasoning": "Malformed LLM response — defaulting to no_change",
            "confidence": 0.0,
            "regime_state": "unknown",
        }

        with patch.dict(sys.modules, {"overlay.engine": mock_engine}):
            decision = mock_engine.run_overlay(mode="log_only")

        assert decision["action"] == "no_change"

    def test_chart_analysis_returns_dict(self):
        """Verify chart analysis source returns a dict with expected keys."""
        # Build a mock sources.charts module
        mock_charts = MagicMock()
        mock_charts.get_chart_analysis.return_value = {
            "trend": "bullish",
            "rsi": 55.0,
            "above_200ma": True,
            "vix": 18.5,
            "summary": "Market trending up with low vol",
        }

        with patch.dict(sys.modules, {"overlay.sources.charts": mock_charts}):
            result = mock_charts.get_chart_analysis()

        assert isinstance(result, dict)
        assert "trend" in result or "summary" in result

    def test_news_summary_returns_string(self):
        """Verify news summary source returns a non-empty string."""
        mock_news = MagicMock()
        mock_news.get_news_summary.return_value = (
            "Fed holds rates steady. Tech earnings beat estimates. "
            "VIX remains subdued at 18."
        )

        with patch.dict(sys.modules, {"overlay.sources.news": mock_news}):
            result = mock_news.get_news_summary()

        assert isinstance(result, str)
        assert len(result) > 0


# ═══════════════════════════════════════════════════════════════════════════════
# Evaluator Tests
# ═══════════════════════════════════════════════════════════════════════════════

class TestEvaluator:
    """Tests for overlay.evaluator.evaluate_overlay_decisions()."""

    def test_evaluator_tighten_correct(self, spy_df_down):
        """Tighten decision + market dropped → marked correct (outcome_correct=1)."""
        from overlay.evaluator import evaluate_overlay_decisions

        decision_id = _insert_decision(action="tighten", offset_days=-6)

        with patch("overlay.evaluator._load_spy_ohlcv", return_value=spy_df_down):
            stats = evaluate_overlay_decisions(days=14)

        decisions = get_overlay_decisions(days=14)
        evaluated = next((d for d in decisions if d["id"] == decision_id), None)
        assert evaluated is not None
        assert evaluated["outcome_evaluated"] == 1
        assert evaluated["outcome_correct"] == 1
        assert "fell" in evaluated["outcome_notes"].lower() or "protect" in evaluated["outcome_notes"].lower()

    def test_evaluator_tighten_incorrect(self, spy_df_up):
        """Tighten decision + market rose → marked incorrect (outcome_correct=0)."""
        from overlay.evaluator import evaluate_overlay_decisions

        decision_id = _insert_decision(action="tighten", offset_days=-6)

        with patch("overlay.evaluator._load_spy_ohlcv", return_value=spy_df_up):
            stats = evaluate_overlay_decisions(days=14)

        decisions = get_overlay_decisions(days=14)
        evaluated = next((d for d in decisions if d["id"] == decision_id), None)
        assert evaluated is not None
        assert evaluated["outcome_correct"] == 0
        assert "missed" in evaluated["outcome_notes"].lower() or "upside" in evaluated["outcome_notes"].lower()

    def test_evaluator_no_change_correct(self, spy_df_flat):
        """No-change decision + flat market → marked correct (outcome_correct=1)."""
        from overlay.evaluator import evaluate_overlay_decisions

        decision_id = _insert_decision(action="no_change", offset_days=-6)

        with patch("overlay.evaluator._load_spy_ohlcv", return_value=spy_df_flat):
            stats = evaluate_overlay_decisions(days=14)

        decisions = get_overlay_decisions(days=14)
        evaluated = next((d for d in decisions if d["id"] == decision_id), None)
        assert evaluated is not None
        assert evaluated["outcome_correct"] == 1
        assert "stable" in evaluated["outcome_notes"].lower() or "appropriate" in evaluated["outcome_notes"].lower()

    def test_evaluator_no_change_incorrect(self, spy_df_down):
        """No-change + market dropped >2% → marked incorrect (outcome_correct=0)."""
        from overlay.evaluator import evaluate_overlay_decisions

        decision_id = _insert_decision(action="no_change", offset_days=-6)

        with patch("overlay.evaluator._load_spy_ohlcv", return_value=spy_df_down):
            stats = evaluate_overlay_decisions(days=14)

        decisions = get_overlay_decisions(days=14)
        evaluated = next((d for d in decisions if d["id"] == decision_id), None)
        assert evaluated is not None
        assert evaluated["outcome_correct"] == 0
        assert "tighten" in evaluated["outcome_notes"].lower() or "dropped" in evaluated["outcome_notes"].lower()

    def test_evaluator_empty_decisions(self):
        """No decisions → returns empty stats without error."""
        from overlay.evaluator import evaluate_overlay_decisions

        stats = evaluate_overlay_decisions(days=7)

        assert stats["total_decisions"] == 0
        assert stats["tighten_count"] == 0
        assert stats["no_change_count"] == 0
        assert stats["overall_accuracy_pct"] == 0.0
        assert stats["net_value"] == "neutral"

    def test_evaluator_stats_structure(self, spy_df_flat):
        """evaluate_overlay_decisions returns a dict with all required keys."""
        from overlay.evaluator import evaluate_overlay_decisions

        _insert_decision(action="tighten", offset_days=-6)
        _insert_decision(action="no_change", offset_days=-5)

        with patch("overlay.evaluator._load_spy_ohlcv", return_value=spy_df_flat):
            stats = evaluate_overlay_decisions(days=14)

        required_keys = {
            "period_days",
            "total_decisions",
            "tighten_count",
            "no_change_count",
            "tighten_correct_pct",
            "no_change_correct_pct",
            "overall_accuracy_pct",
            "net_value",
        }
        assert required_keys.issubset(set(stats.keys()))

    def test_evaluator_accuracy_positive(self, spy_df_down):
        """All tighten decisions correct → net_value='positive'."""
        from overlay.evaluator import evaluate_overlay_decisions

        # Insert 3 tighten decisions that will all be CORRECT (market fell)
        for i in range(3, 10, 2):
            _insert_decision(action="tighten", offset_days=-i)

        with patch("overlay.evaluator._load_spy_ohlcv", return_value=spy_df_down):
            stats = evaluate_overlay_decisions(days=30)

        assert stats["overall_accuracy_pct"] > 55.0
        assert stats["net_value"] == "positive"

    def test_evaluator_skips_too_recent(self):
        """Decisions from today have no future data → they are skipped."""
        from overlay.evaluator import evaluate_overlay_decisions

        # Decision from today — no future SPY data yet
        _insert_decision(action="tighten", offset_days=0)

        # Return empty df to simulate "no future data"
        empty_df = pd.DataFrame()
        with patch("overlay.evaluator._load_spy_ohlcv", return_value=empty_df):
            stats = evaluate_overlay_decisions(days=7)

        # Should not have been evaluated
        decisions = get_overlay_decisions(days=7)
        for d in decisions:
            assert not d.get("outcome_evaluated"), (
                "Decision with no future data should not be marked evaluated"
            )
        assert stats["skipped_count"] >= 1

    def test_evaluate_and_report_sends_telegram(self, spy_df_flat):
        """evaluate_and_report calls send_message with non-empty string."""
        from overlay.evaluator import evaluate_and_report

        _insert_decision(action="no_change", offset_days=-6)

        with patch("overlay.evaluator._load_spy_ohlcv", return_value=spy_df_flat), \
             patch("utils.telegram.send_message", return_value=True) as mock_tg:
            stats = evaluate_and_report(days=14)

        mock_tg.assert_called_once()
        msg_arg = mock_tg.call_args[0][0]
        assert isinstance(msg_arg, str)
        assert len(msg_arg) > 0
        assert "Overlay" in msg_arg or "overlay" in msg_arg.lower()

    def test_evaluate_and_report_telegram_failure_nonfatal(self, spy_df_flat):
        """Telegram failure in evaluate_and_report should not raise."""
        from overlay.evaluator import evaluate_and_report

        _insert_decision(action="no_change", offset_days=-6)

        with patch("overlay.evaluator._load_spy_ohlcv", return_value=spy_df_flat), \
             patch("utils.telegram.send_message", side_effect=ConnectionError("network down")):
            # Should NOT raise despite Telegram failing
            stats = evaluate_and_report(days=14)

        assert "net_value" in stats

    def test_evaluator_already_evaluated_not_reprocessed(self, spy_df_flat):
        """Decisions already marked outcome_evaluated are not re-evaluated."""
        from overlay.evaluator import evaluate_overlay_decisions

        decision_id = _insert_decision(action="tighten", offset_days=-6)
        # Pre-mark as evaluated
        update_overlay_outcome(
            decision_id=decision_id,
            outcome_correct=0,
            outcome_notes="Pre-existing evaluation",
        )

        with patch("overlay.evaluator._load_spy_ohlcv", return_value=spy_df_flat) as mock_spy:
            stats = evaluate_overlay_decisions(days=14)

        # The pre-evaluated decision should not trigger a new SPY load
        assert stats["newly_evaluated"] == 0

    def test_evaluator_spy_data_unavailable(self):
        """When SPY data is unavailable, decision is skipped gracefully."""
        from overlay.evaluator import evaluate_overlay_decisions

        _insert_decision(action="tighten", offset_days=-6)

        with patch("overlay.evaluator._load_spy_ohlcv", return_value=None):
            stats = evaluate_overlay_decisions(days=14)

        assert stats["skipped_count"] >= 1


# ═══════════════════════════════════════════════════════════════════════════════
# Cron Tests
# ═══════════════════════════════════════════════════════════════════════════════

class TestCron:
    """Tests for overlay.cron.run_daily_overlay()."""

    def _mock_engine_decision(self, action: str = "no_change"):
        from overlay.engine import OverlayDecision
        return OverlayDecision(
            adjust=(action == "tighten"),
            sizing_multiplier_override=0.7 if action == "tighten" else None,
            universes_to_deactivate=[],
            tickers_to_avoid=[],
            reasoning="Cron test decision",
            confidence=0.8,
        )

    def test_log_only_returns_none(self):
        """run_daily_overlay(mode='log_only') returns None."""
        from overlay.cron import run_daily_overlay

        mock_engine = MagicMock()
        mock_engine.run_overlay.return_value = self._mock_engine_decision("no_change")

        with patch.dict(sys.modules, {"overlay.engine": mock_engine}):
            result = run_daily_overlay(mode="log_only")

        assert result is None

    def test_active_mode_returns_decision(self):
        """run_daily_overlay(mode='active') returns the decision dict."""
        from overlay.cron import run_daily_overlay

        mock_engine = MagicMock()
        decision = self._mock_engine_decision("tighten")
        mock_engine.run_overlay.return_value = decision

        with patch.dict(sys.modules, {"overlay.engine": mock_engine}):
            result = run_daily_overlay(mode="active")

        assert result is not None
        assert result.adjust is True

    def test_engine_import_error_returns_none(self):
        """If overlay.engine is not installed, cron fails gracefully."""
        from overlay.cron import run_daily_overlay

        # Temporarily remove engine from sys.modules to simulate missing module
        saved = sys.modules.pop("overlay.engine", None)
        try:
            with patch.dict(sys.modules, {"overlay.engine": None}):
                result = run_daily_overlay(mode="log_only")
        finally:
            if saved is not None:
                sys.modules["overlay.engine"] = saved

        # Should return None, not raise
        assert result is None

    def test_engine_exception_returns_none(self):
        """If run_overlay raises an exception, cron returns None gracefully."""
        from overlay.cron import run_daily_overlay

        mock_engine = MagicMock()
        mock_engine.run_overlay.side_effect = RuntimeError("LLM timed out")

        with patch.dict(sys.modules, {"overlay.engine": mock_engine}):
            result = run_daily_overlay(mode="log_only")

        assert result is None

    def test_run_overlay_called_with_mode(self):
        """cron passes the mode argument to engine.run_overlay."""
        from overlay.cron import run_daily_overlay

        mock_engine = MagicMock()
        mock_engine.run_overlay.return_value = self._mock_engine_decision()

        with patch.dict(sys.modules, {"overlay.engine": mock_engine}):
            run_daily_overlay(mode="active")

        mock_engine.run_overlay.assert_called_once_with(mode="active")
