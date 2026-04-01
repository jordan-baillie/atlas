"""
regime/tests/test_model.py — Unit tests for regime/model.py (RegimeModel).

Run with:
    cd /root/atlas && python -m pytest regime/tests/test_model.py -v

Coverage
--------
- Classification with synthetic indicators for each of the 6 states
- Bull-market indicators → bull_risk_on
- Crash indicators (VIX=50, wide spreads, SPY below 200 DMA) → bear_capitulation
- Mixed / uncertain signals → transition_uncertain
- classify_date reads from SQLite (via test DB)
- classify_and_record writes to regime_history
- Reasoning string is populated and human-readable
- Config from regime.json is properly loaded
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import pytest

# ── Project root on path ───────────────────────────────────────────────────────
PROJECT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT))

from db.atlas_db import (
    get_macro_indicators,
    get_regime_history,
    init_db,
    upsert_macro_indicators,
)
from regime.model import RegimeClassification, RegimeModel
from regime.states import RegimeState

# ──────────────────────────────────────────────────────────────────────────────
# Synthetic indicator sets
# ──────────────────────────────────────────────────────────────────────────────

BULL_RISK_ON_INDICATORS = {
    "spy_close": 500,
    "spy_200dma": 450,
    "spy_above_200dma": 1,
    "spy_200dma_slope": 0.05,
    "vix": 15,
    "vix3m": 17,
    "vix_term_ratio": 0.88,
    "credit_oas": 0.8,
    "yield_curve_10y2y": 1.5,
    "yield_curve_10y3m": 2.0,
    "dxy": 100,
    "gold_copper_ratio": 16,
}

CRASH_INDICATORS = {
    "spy_close": 250,
    "spy_200dma": 350,
    "spy_above_200dma": 0,
    "spy_200dma_slope": -0.08,
    "vix": 55,
    "vix3m": 35,
    "vix_term_ratio": 1.57,
    "credit_oas": 3.5,
    "yield_curve_10y2y": -0.5,
    "yield_curve_10y3m": -1.0,
    "dxy": 108,
    "gold_copper_ratio": 28,
}

BEAR_RISK_OFF_INDICATORS = {
    "spy_above_200dma": 0,
    "spy_200dma_slope": -0.04,
    "vix": 32,
    "vix3m": 28,
    "vix_term_ratio": 1.14,
    "credit_oas": 2.0,
    "yield_curve_10y2y": -0.3,
    "yield_curve_10y3m": -0.8,
    "dxy": 104,
    "gold_copper_ratio": 24,
}

BULL_RISK_OFF_INDICATORS = {
    # SPY above 200 DMA but VIX starting to climb (elevated risk/credit).
    # NOTE: In no-history mode classify() will return recovery_early for
    # these indicators (mixed signals heuristic). Test via classify_date()
    # with an empty-history DB so _check_recent_bear() returns False and
    # the bull_risk_off rule fires correctly.
    "spy_above_200dma": 1,
    "spy_200dma_slope": 0.02,
    "vix": 27,
    "vix3m": 22,
    "vix_term_ratio": 1.23,
    "credit_oas": 1.8,
    "yield_curve_10y2y": 0.8,
    "yield_curve_10y3m": 0.9,
    "dxy": 101,
    "gold_copper_ratio": 19,
}

TRANSITION_UNCERTAIN_INDICATORS = {
    # SPY below 200 DMA (broken trend) but macro risk is mostly calm —
    # opposing signals that cancel to abs(composite) < 0.15.
    # spy_above_200dma=0 ensures recovery_early (trend>0 heuristic) doesn't fire.
    "spy_above_200dma": 0,
    "spy_200dma_slope": 0.0,
    "vix": 21,
    "vix3m": 22,
    "vix_term_ratio": 0.95,
    "credit_oas": 1.7,
    "yield_curve_10y2y": 0.3,
    "yield_curve_10y3m": 0.3,
    "dxy": 100,
    "gold_copper_ratio": 20,
}

RECOVERY_EARLY_INDICATORS = {
    # Trend just turned positive, but risk/credit still slightly stressed.
    "spy_above_200dma": 1,
    "spy_200dma_slope": 0.01,
    "vix": 28,
    "vix3m": 22,
    "vix_term_ratio": 1.27,
    "credit_oas": 1.9,
    "yield_curve_10y2y": 0.3,
    "yield_curve_10y3m": 0.5,
    "dxy": 103,
    "gold_copper_ratio": 21,
}


# ──────────────────────────────────────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────────────────────────────────────


@pytest.fixture(scope="session")
def model() -> RegimeModel:
    """Shared RegimeModel loaded once per test session."""
    return RegimeModel()


@pytest.fixture()
def tmp_db(tmp_path):
    """
    Point atlas_db at a fresh temp SQLite file for each test.

    The fixture yields the db path and resets the override after the test.
    """
    db_path = str(tmp_path / "test_atlas.db")
    init_db(db_path)
    yield db_path
    # Reset to production path.
    import db.atlas_db as _adb

    _adb._db_path_override = None


# ──────────────────────────────────────────────────────────────────────────────
# Config loading
# ──────────────────────────────────────────────────────────────────────────────


class TestConfigLoading:
    def test_default_config_loads(self, model):
        """RegimeModel loads regime.json without error."""
        assert model._config is not None
        assert "weights" in model._config
        assert "model_version" in model._config

    def test_model_version_set(self, model):
        """model_version is read from config."""
        assert model._model_version == "v1"

    def test_missing_config_raises(self, tmp_path):
        """FileNotFoundError on non-existent config path."""
        with pytest.raises(FileNotFoundError):
            RegimeModel(config_path=str(tmp_path / "nonexistent.json"))


# ──────────────────────────────────────────────────────────────────────────────
# Classification — each of the 6 states
# ──────────────────────────────────────────────────────────────────────────────


class TestClassificationStates:
    def test_bull_risk_on(self, model):
        result = model.classify(BULL_RISK_ON_INDICATORS)
        assert result.state == RegimeState.BULL_RISK_ON, (
            f"Expected bull_risk_on, got {result.state.value} "
            f"(composite={result.scores['composite']:.3f})"
        )

    def test_bear_capitulation(self, model):
        result = model.classify(CRASH_INDICATORS)
        assert result.state == RegimeState.BEAR_CAPITULATION, (
            f"Expected bear_capitulation, got {result.state.value} "
            f"(composite={result.scores['composite']:.3f})"
        )

    def test_bear_risk_off(self, model):
        result = model.classify(BEAR_RISK_OFF_INDICATORS)
        assert result.state == RegimeState.BEAR_RISK_OFF, (
            f"Expected bear_risk_off, got {result.state.value} "
            f"(composite={result.scores['composite']:.3f})"
        )

    def test_bull_risk_off(self, model, tmp_db):
        """
        Bull-risk-off requires DB path: in no-history mode the mixed-signal
        recovery_early heuristic fires first.  Use classify_date() so
        _check_recent_bear() returns False (accessible DB, no bear history)
        and the bull_risk_off rule fires correctly.
        """
        date = "2024-04-01"
        upsert_macro_indicators(date, **BULL_RISK_OFF_INDICATORS)

        result = model.classify_date(date)
        assert result.state == RegimeState.BULL_RISK_OFF, (
            f"Expected bull_risk_off, got {result.state.value} "
            f"(composite={result.scores['composite']:.3f})"
        )

    def test_transition_uncertain(self, model):
        result = model.classify(TRANSITION_UNCERTAIN_INDICATORS)
        assert result.state == RegimeState.TRANSITION_UNCERTAIN, (
            f"Expected transition_uncertain, got {result.state.value} "
            f"(composite={result.scores['composite']:.3f})"
        )

    def test_recovery_early_no_history(self, model):
        """
        Without regime history the recovery_early rule falls back to
        mixed-signal detection: trend > 0 AND (risk < 0 OR credit < 0).
        """
        result = model.classify(RECOVERY_EARLY_INDICATORS)
        assert result.state == RegimeState.RECOVERY_EARLY, (
            f"Expected recovery_early, got {result.state.value} "
            f"(trend={result.scores['trend']:.3f}, "
            f"risk={result.scores['risk']:.3f}, "
            f"credit={result.scores['credit']:.3f})"
        )


# ──────────────────────────────────────────────────────────────────────────────
# Result fields
# ──────────────────────────────────────────────────────────────────────────────


class TestResultFields:
    def test_scores_keys_present(self, model):
        result = model.classify(BULL_RISK_ON_INDICATORS)
        expected_keys = {"trend", "risk", "credit", "yield_curve", "dollar", "commodity", "composite"}
        assert expected_keys.issubset(result.scores.keys())

    def test_scores_in_range(self, model):
        for indicators in [
            BULL_RISK_ON_INDICATORS,
            CRASH_INDICATORS,
            BEAR_RISK_OFF_INDICATORS,
            TRANSITION_UNCERTAIN_INDICATORS,
        ]:
            result = model.classify(indicators)
            for k, v in result.scores.items():
                assert -1.0 <= v <= 1.0, f"Score '{k}' = {v} out of range"

    def test_active_universes_nonempty(self, model):
        for indicators in [BULL_RISK_ON_INDICATORS, CRASH_INDICATORS]:
            result = model.classify(indicators)
            assert len(result.active_universes) > 0

    def test_sizing_multiplier_positive(self, model):
        for indicators in [BULL_RISK_ON_INDICATORS, CRASH_INDICATORS, TRANSITION_UNCERTAIN_INDICATORS]:
            result = model.classify(indicators)
            assert 0 < result.sizing_multiplier <= 1.0

    def test_max_positions_positive_int(self, model):
        result = model.classify(BULL_RISK_ON_INDICATORS)
        assert isinstance(result.max_positions, int)
        assert result.max_positions > 0

    def test_model_version_in_result(self, model):
        result = model.classify(BULL_RISK_ON_INDICATORS)
        assert result.model_version == "v1"

    def test_enabled_strategies_nonempty(self, model):
        result = model.classify(BULL_RISK_ON_INDICATORS)
        assert len(result.enabled_strategies) > 0

    def test_result_is_dataclass(self, model):
        result = model.classify(BULL_RISK_ON_INDICATORS)
        assert isinstance(result, RegimeClassification)


# ──────────────────────────────────────────────────────────────────────────────
# Reasoning string
# ──────────────────────────────────────────────────────────────────────────────


class TestReasoning:
    def test_reasoning_populated(self, model):
        result = model.classify(BULL_RISK_ON_INDICATORS)
        assert isinstance(result.reasoning, str)
        assert len(result.reasoning) > 10

    def test_reasoning_contains_state(self, model):
        result = model.classify(BULL_RISK_ON_INDICATORS)
        assert "bull_risk_on" in result.reasoning

    def test_reasoning_contains_composite(self, model):
        result = model.classify(BULL_RISK_ON_INDICATORS)
        assert "Composite:" in result.reasoning

    def test_reasoning_crash_state(self, model):
        result = model.classify(CRASH_INDICATORS)
        assert "bear_capitulation" in result.reasoning

    def test_reasoning_human_readable(self, model):
        """Reasoning should mention trend/risk/credit in plain English."""
        result = model.classify(BULL_RISK_ON_INDICATORS)
        # Should contain at least one human-readable description word.
        descriptive_words = {"above", "below", "low", "high", "elevated", "tight", "blowing", "normal", "inverted", "flat", "moderate", "calm", "near"}
        found = any(word in result.reasoning.lower() for word in descriptive_words)
        assert found, f"Reasoning lacks descriptive language: {result.reasoning}"


# ──────────────────────────────────────────────────────────────────────────────
# Database integration — classify_date
# ──────────────────────────────────────────────────────────────────────────────


class TestClassifyDate:
    def test_classify_date_reads_from_db(self, model, tmp_db):
        """classify_date returns correct state for data inserted into test DB."""
        date = "2024-06-15"
        upsert_macro_indicators(date, **BULL_RISK_ON_INDICATORS)

        result = model.classify_date(date)
        # State should be bull_risk_on or similar positive state
        assert result.state in {RegimeState.BULL_RISK_ON, RegimeState.BULL_RISK_OFF, RegimeState.RECOVERY_EARLY}
        assert result.date == date

    def test_classify_date_missing_raises(self, model, tmp_db):
        """classify_date raises ValueError when no row exists for the date."""
        with pytest.raises(ValueError, match="No macro indicators found"):
            model.classify_date("1900-01-01")

    def test_classify_date_crash_scenario(self, model, tmp_db):
        """Crash indicators inserted into DB are classified as bear_capitulation."""
        date = "2020-03-16"
        upsert_macro_indicators(date, **CRASH_INDICATORS)

        result = model.classify_date(date)
        assert result.state == RegimeState.BEAR_CAPITULATION
        assert result.date == date

    def test_classify_date_returns_correct_date_field(self, model, tmp_db):
        date = "2024-09-30"
        upsert_macro_indicators(date, **TRANSITION_UNCERTAIN_INDICATORS)

        result = model.classify_date(date)
        assert result.date == date


# ──────────────────────────────────────────────────────────────────────────────
# Database integration — classify_and_record
# ──────────────────────────────────────────────────────────────────────────────


class TestClassifyAndRecord:
    def test_record_writes_to_regime_history(self, model, tmp_db):
        """classify_and_record persists a row to regime_history."""
        date = "2024-05-10"
        upsert_macro_indicators(date, **BULL_RISK_ON_INDICATORS)

        result = model.classify_and_record(date=date)

        history = get_regime_history()
        assert len(history) == 1
        row = history[0]
        assert row["date"] == date
        assert row["regime_state"] == result.state.value

    def test_record_stores_correct_state(self, model, tmp_db):
        date = "2020-03-20"
        upsert_macro_indicators(date, **CRASH_INDICATORS)

        result = model.classify_and_record(date=date)
        assert result.state == RegimeState.BEAR_CAPITULATION

        history = get_regime_history()
        assert history[0]["regime_state"] == "bear_capitulation"

    def test_record_stores_active_universes(self, model, tmp_db):
        date = "2024-07-04"
        upsert_macro_indicators(date, **BULL_RISK_ON_INDICATORS)

        model.classify_and_record(date=date)
        history = get_regime_history()
        assert isinstance(history[0]["active_universes"], list)
        assert len(history[0]["active_universes"]) > 0

    def test_record_stores_reasoning(self, model, tmp_db):
        date = "2024-08-01"
        upsert_macro_indicators(date, **TRANSITION_UNCERTAIN_INDICATORS)

        model.classify_and_record(date=date)
        history = get_regime_history()
        assert history[0]["reasoning"] != ""

    def test_record_no_date_uses_most_recent(self, model, tmp_db):
        """classify_and_record(date=None) picks up the most recent DB row."""
        upsert_macro_indicators("2024-10-01", **BULL_RISK_ON_INDICATORS)

        result = model.classify_and_record()
        history = get_regime_history()
        assert len(history) == 1
        assert history[0]["date"] == "2024-10-01"

    def test_record_idempotent(self, model, tmp_db):
        """Calling classify_and_record twice for the same date upserts, not duplicates."""
        date = "2024-11-15"
        upsert_macro_indicators(date, **BULL_RISK_ON_INDICATORS)

        model.classify_and_record(date=date)
        model.classify_and_record(date=date)

        history = get_regime_history()
        assert len(history) == 1


# ──────────────────────────────────────────────────────────────────────────────
# Threshold boundary tests (verify relaxed thresholds work as intended)
# ──────────────────────────────────────────────────────────────────────────────


class TestThresholds:
    """
    Directly exercise _apply_rules() with synthetic score dicts to verify that
    the relaxed classification thresholds behave correctly.
    """

    def test_bear_risk_off_boundary_now_classifies(self, model):
        """
        Old threshold: composite < -0.3 AND trend < -0.3 (strictly).
        New threshold: composite <= -0.25 AND trend <= -0.25 (inclusive).

        composite=-0.27 / trend=-0.27 would fall through under the old rules
        and land at transition_uncertain.  With the relaxed threshold it must
        be classified as bear_risk_off.
        """
        scores = {
            "composite": -0.27,
            "trend": -0.27,
            "risk": -0.3,
            "credit": -0.2,
            "yield_curve": 0.0,
            "dollar": 0.0,
            "commodity": 0.0,
        }
        state = model._apply_rules(scores, recent_was_bear=False)
        assert state == RegimeState.BEAR_RISK_OFF, (
            f"Expected bear_risk_off at boundary, got {state.value}"
        )

    def test_bear_capitulation_boundary_now_classifies(self, model):
        """
        Old threshold: composite < -0.6 (strictly).
        New threshold: composite <= -0.5 (inclusive).

        composite=-0.55 with high-risk scores would be missed by the old rule.
        """
        scores = {
            "composite": -0.55,
            "trend": -0.50,
            "risk": -0.80,   # satisfies risk < -0.7
            "credit": -0.40,
            "yield_curve": -0.30,
            "dollar": -0.20,
            "commodity": 0.0,
        }
        state = model._apply_rules(scores, recent_was_bear=None)
        assert state == RegimeState.BEAR_CAPITULATION, (
            f"Expected bear_capitulation at boundary, got {state.value}"
        )

    def test_bear_capitulation_requires_risk_or_credit_extreme(self, model):
        """
        bear_capitulation needs composite <= -0.5 AND (risk < -0.7 OR credit < -0.7).
        Without the extreme risk/credit signal it should fall through to bear_risk_off.
        """
        scores = {
            "composite": -0.55,
            "trend": -0.50,
            "risk": -0.50,   # NOT below -0.7
            "credit": -0.40,  # NOT below -0.7
            "yield_curve": -0.30,
            "dollar": 0.0,
            "commodity": 0.0,
        }
        state = model._apply_rules(scores, recent_was_bear=False)
        assert state == RegimeState.BEAR_RISK_OFF, (
            f"Expected bear_risk_off without extreme risk/credit, got {state.value}"
        )

    def test_boundary_exactly_at_025_classifies_bear_risk_off(self, model):
        """composite == trend == -0.25 is exactly at the inclusive boundary."""
        scores = {
            "composite": -0.25,
            "trend": -0.25,
            "risk": -0.20,
            "credit": -0.10,
            "yield_curve": 0.0,
            "dollar": 0.0,
            "commodity": 0.0,
        }
        state = model._apply_rules(scores, recent_was_bear=False)
        assert state == RegimeState.BEAR_RISK_OFF

    def test_boundary_exactly_at_050_classifies_capitulation(self, model):
        """composite == -0.50 with extreme risk is exactly at the inclusive boundary."""
        scores = {
            "composite": -0.50,
            "trend": -0.45,
            "risk": -0.75,   # < -0.7
            "credit": -0.30,
            "yield_curve": 0.0,
            "dollar": 0.0,
            "commodity": 0.0,
        }
        state = model._apply_rules(scores, recent_was_bear=None)
        assert state == RegimeState.BEAR_CAPITULATION


# ──────────────────────────────────────────────────────────────────────────────
# Recovery detection with history
# ──────────────────────────────────────────────────────────────────────────────


class TestRecoveryEarlyWithHistory:
    def test_recovery_detected_after_bear_period(self, model, tmp_db):
        """
        If regime_history has a recent BEAR_RISK_OFF entry and current trend
        turns positive, classify_date should return recovery_early.
        """
        from db.atlas_db import record_regime

        # Insert a bear period in history.
        record_regime(
            date="2024-06-01",
            state=RegimeState.BEAR_RISK_OFF.value,
            trend_score=-0.5,
            risk_score=-0.4,
            active_universes=["treasury_etfs"],
            sizing_multiplier=0.5,
            reasoning="Bear period",
        )

        # Insert recovery indicators.
        upsert_macro_indicators("2024-06-15", **RECOVERY_EARLY_INDICATORS)

        result = model.classify_date("2024-06-15")
        assert result.state == RegimeState.RECOVERY_EARLY, (
            f"Expected recovery_early after bear period, got {result.state.value} "
            f"(trend={result.scores['trend']:.3f}, risk={result.scores['risk']:.3f})"
        )
