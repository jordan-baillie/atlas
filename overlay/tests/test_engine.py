"""
overlay/tests/test_engine.py — Unit tests for overlay/engine.py.

Run with:
    cd /root/atlas && python -m pytest overlay/tests/test_engine.py -v

Coverage
--------
- OverlayDecision.no_change() factory
- _try_parse_json: valid JSON, invalid input, markdown fences
- _validate_response: no_change, tighten, sizing clamp, missing keys,
  confidence clamping, invalid sizing type
- _call_pi: mocked subprocess.run — success, timeout, non-zero exit,
  bad JSON, empty output, OS error, pi envelope unwrapping
- run_overlay: returns OverlayDecision in all paths, log_only mode,
  active mode, regime failure, pi failure, sizing violation, DB write
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import List, Optional
from unittest.mock import MagicMock, patch

import pytest

# ── Project root on path ──────────────────────────────────────────────────────
PROJECT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT))

from db.atlas_db import get_overlay_decisions, init_db
from overlay.engine import (
    OverlayDecision,
    _call_pi,
    _try_parse_json,
    _validate_response,
    run_overlay,
)


# ──────────────────────────────────────────────────────────────────────────────
# Test helpers
# ──────────────────────────────────────────────────────────────────────────────


def _make_regime(sizing: float = 0.7, state: str = "bull_risk_on") -> MagicMock:
    """
    Build a MagicMock that mimics RegimeClassification.

    Using MagicMock avoids importing RegimeModel (which requires a DB) in unit
    tests that only need to exercise overlay logic.
    """
    regime = MagicMock()
    regime.sizing_multiplier = sizing
    regime.state = MagicMock()
    regime.state.value = state
    regime.active_universes = ["sp500", "sector_etfs"]
    regime.enabled_strategies = ["momentum_breakout", "mean_reversion"]
    regime.scores = {"trend": 0.5, "risk": 0.3, "composite": 0.4}
    regime.reasoning = "Bull trend intact, VIX low"
    regime.date = "2026-04-02"
    regime.max_positions = 5
    return regime


def _ok_response(
    adjust: bool = False,
    sizing: Optional[float] = None,
    universes: Optional[List[str]] = None,
    tickers: Optional[List[str]] = None,
    confidence: float = 0.8,
    reasoning: str = "test reasoning",
) -> str:
    """Build a well-formed LLM JSON response string."""
    return json.dumps({
        "adjust": adjust,
        "sizing_multiplier_override": sizing,
        "universes_to_deactivate": universes or [],
        "tickers_to_avoid": tickers or [],
        "reasoning": reasoning,
        "confidence": confidence,
    })


class _FakeProcess:
    """Minimal subprocess.CompletedProcess substitute."""

    def __init__(
        self,
        stdout: str = "",
        returncode: int = 0,
        stderr: str = "",
    ) -> None:
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = stderr


@pytest.fixture()
def tmp_db(tmp_path):
    """
    Point atlas_db at a fresh temp SQLite file for each test.

    Mirrors the fixture pattern from regime/tests/test_model.py.
    """
    db_path = str(tmp_path / "test_atlas.db")
    init_db(db_path)
    yield db_path
    import db.atlas_db as _adb

    _adb._db_path_override = None


# ──────────────────────────────────────────────────────────────────────────────
# OverlayDecision
# ──────────────────────────────────────────────────────────────────────────────


class TestOverlayDecision:
    def test_no_change_factory_defaults(self):
        d = OverlayDecision.no_change()
        assert d.adjust is False
        assert d.sizing_multiplier_override is None
        assert d.universes_to_deactivate == []
        assert d.tickers_to_avoid == []
        assert d.confidence == 0.0

    def test_no_change_custom_reasoning(self):
        d = OverlayDecision.no_change("pi timed out")
        assert "timed out" in d.reasoning

    def test_tighten_decision_fields(self):
        d = OverlayDecision(
            adjust=True,
            sizing_multiplier_override=0.4,
            universes_to_deactivate=["sp500"],
            tickers_to_avoid=["AAPL"],
            reasoning="elevated risk",
            confidence=0.85,
        )
        assert d.adjust is True
        assert d.sizing_multiplier_override == 0.4
        assert "sp500" in d.universes_to_deactivate
        assert "AAPL" in d.tickers_to_avoid
        assert d.confidence == 0.85


# ──────────────────────────────────────────────────────────────────────────────
# _try_parse_json
# ──────────────────────────────────────────────────────────────────────────────


class TestTryParseJson:
    def test_valid_object(self):
        assert _try_parse_json('{"a": 1}') == {"a": 1}

    def test_invalid_returns_none(self):
        assert _try_parse_json("not json") is None

    def test_empty_string_returns_none(self):
        assert _try_parse_json("") is None

    def test_none_returns_none(self):
        assert _try_parse_json(None) is None  # type: ignore[arg-type]

    def test_strips_markdown_fences_backtick_json(self):
        text = "```json\n{\"a\": 1}\n```"
        result = _try_parse_json(text)
        assert result == {"a": 1}

    def test_strips_bare_backtick_fences(self):
        text = "```\n{\"a\": 1}\n```"
        result = _try_parse_json(text)
        assert result == {"a": 1}

    def test_json_array_returns_none(self):
        assert _try_parse_json("[1, 2, 3]") is None

    def test_json_string_returns_none(self):
        assert _try_parse_json('"just a string"') is None

    def test_nested_json_not_unwrapped(self):
        """_try_parse_json does NOT recursively unwrap — that's _call_pi's job."""
        outer = json.dumps({"text": '{"adjust": false}'})
        result = _try_parse_json(outer)
        assert result == {"text": '{"adjust": false}'}


# ──────────────────────────────────────────────────────────────────────────────
# _validate_response
# ──────────────────────────────────────────────────────────────────────────────


class TestValidateResponse:
    def test_no_change_response_pass_through(self):
        regime = _make_regime(sizing=0.7)
        raw = {"adjust": False, "reasoning": "all clear", "confidence": 0.9}
        d = _validate_response(raw, regime)
        assert d.adjust is False
        assert d.sizing_multiplier_override is None
        assert d.universes_to_deactivate == []
        assert d.tickers_to_avoid == []
        assert d.confidence == 0.9

    def test_tighten_valid_sizing_below_regime_cap(self):
        regime = _make_regime(sizing=0.7)
        raw = {
            "adjust": True,
            "sizing_multiplier_override": 0.4,
            "universes_to_deactivate": ["sp500"],
            "tickers_to_avoid": ["AAPL", "TSLA"],
            "reasoning": "tariff risk",
            "confidence": 0.75,
        }
        d = _validate_response(raw, regime)
        assert d.adjust is True
        assert d.sizing_multiplier_override == 0.4
        assert "sp500" in d.universes_to_deactivate
        assert "AAPL" in d.tickers_to_avoid
        assert "TSLA" in d.tickers_to_avoid
        assert d.confidence == 0.75

    def test_asymmetric_constraint_sizing_clamped(self):
        """ASYMMETRIC CONSTRAINT: sizing > regime cap is clamped to cap."""
        regime = _make_regime(sizing=0.7)
        raw = {
            "adjust": True,
            "sizing_multiplier_override": 1.0,  # violation — trying to loosen
            "universes_to_deactivate": [],
            "tickers_to_avoid": [],
            "reasoning": "trying to loosen",
            "confidence": 0.5,
        }
        d = _validate_response(raw, regime)
        assert d.adjust is True
        assert d.sizing_multiplier_override == 0.7  # clamped to regime cap

    def test_asymmetric_constraint_exact_regime_cap_allowed(self):
        """sizing == regime cap is valid (clamp is strictly > not >=)."""
        regime = _make_regime(sizing=0.7)
        raw = {
            "adjust": True,
            "sizing_multiplier_override": 0.7,  # exactly the cap — allowed
            "universes_to_deactivate": [],
            "tickers_to_avoid": [],
            "reasoning": "holding at regime cap",
            "confidence": 0.5,
        }
        d = _validate_response(raw, regime)
        assert d.sizing_multiplier_override == 0.7

    def test_missing_adjust_key_defaults_no_change(self):
        regime = _make_regime()
        raw = {"reasoning": "something happened"}  # missing 'adjust'
        d = _validate_response(raw, regime)
        assert d.adjust is False
        assert "error" in d.reasoning.lower()

    def test_missing_reasoning_key_defaults_no_change(self):
        regime = _make_regime()
        raw = {"adjust": True}  # missing 'reasoning'
        d = _validate_response(raw, regime)
        assert d.adjust is False

    def test_confidence_clamped_above_1(self):
        regime = _make_regime()
        raw = {"adjust": False, "reasoning": "ok", "confidence": 99.9}
        d = _validate_response(raw, regime)
        assert d.confidence == 1.0

    def test_confidence_clamped_below_0(self):
        regime = _make_regime()
        raw = {"adjust": False, "reasoning": "ok", "confidence": -5.0}
        d = _validate_response(raw, regime)
        assert d.confidence == 0.0

    def test_invalid_sizing_type_ignored(self):
        regime = _make_regime(sizing=0.7)
        raw = {
            "adjust": True,
            "sizing_multiplier_override": "not-a-number",
            "universes_to_deactivate": [],
            "tickers_to_avoid": [],
            "reasoning": "test",
            "confidence": 0.5,
        }
        d = _validate_response(raw, regime)
        assert d.sizing_multiplier_override is None

    def test_null_sizing_allowed_when_adjust_true(self):
        regime = _make_regime(sizing=0.7)
        raw = {
            "adjust": True,
            "sizing_multiplier_override": None,
            "universes_to_deactivate": ["sp500"],
            "tickers_to_avoid": [],
            "reasoning": "deactivate universe only",
            "confidence": 0.6,
        }
        d = _validate_response(raw, regime)
        assert d.adjust is True
        assert d.sizing_multiplier_override is None
        assert "sp500" in d.universes_to_deactivate

    def test_empty_lists_normalised(self):
        regime = _make_regime()
        raw = {
            "adjust": True,
            "sizing_multiplier_override": 0.5,
            "universes_to_deactivate": None,
            "tickers_to_avoid": None,
            "reasoning": "test",
            "confidence": 0.5,
        }
        d = _validate_response(raw, regime)
        assert d.universes_to_deactivate == []
        assert d.tickers_to_avoid == []


# ──────────────────────────────────────────────────────────────────────────────
# _call_pi (subprocess mocked)
# ──────────────────────────────────────────────────────────────────────────────


class TestCallPi:
    def test_successful_no_change_response(self):
        payload = _ok_response(adjust=False)
        with patch("subprocess.run", return_value=_FakeProcess(stdout=payload)):
            result = _call_pi("test prompt")
        assert result is not None
        assert result["adjust"] is False

    def test_successful_tighten_response(self):
        payload = _ok_response(adjust=True, sizing=0.4)
        with patch("subprocess.run", return_value=_FakeProcess(stdout=payload)):
            result = _call_pi("test prompt")
        assert result is not None
        assert result["adjust"] is True
        assert result["sizing_multiplier_override"] == 0.4

    def test_timeout_returns_none(self):
        with patch("subprocess.run",
                   side_effect=subprocess.TimeoutExpired("pi", 120)):
            result = _call_pi("test prompt")
        assert result is None

    def test_non_zero_exit_returns_none(self):
        with patch("subprocess.run",
                   return_value=_FakeProcess(returncode=1, stderr="fatal error")):
            result = _call_pi("test prompt")
        assert result is None

    def test_invalid_json_output_returns_none(self):
        with patch("subprocess.run",
                   return_value=_FakeProcess(stdout="I am not JSON")):
            result = _call_pi("test prompt")
        assert result is None

    def test_empty_stdout_returns_none(self):
        with patch("subprocess.run", return_value=_FakeProcess(stdout="")):
            result = _call_pi("test prompt")
        assert result is None

    def test_os_error_returns_none(self):
        with patch("subprocess.run", side_effect=OSError("pi not found")):
            result = _call_pi("test prompt")
        assert result is None

    def test_pi_envelope_text_field_unwrapped(self):
        """pi --mode json may wrap model output in {"type":..., "text":"..."}."""
        inner = _ok_response(adjust=True, sizing=0.3)
        envelope = json.dumps({"type": "result", "text": inner})
        with patch("subprocess.run", return_value=_FakeProcess(stdout=envelope)):
            result = _call_pi("test prompt")
        assert result is not None
        assert result["adjust"] is True
        assert result["sizing_multiplier_override"] == 0.3

    def test_pi_envelope_content_field_unwrapped(self):
        """pi may use 'content' instead of 'text' in the envelope."""
        inner = _ok_response(adjust=False)
        envelope = json.dumps({"type": "result", "content": inner})
        with patch("subprocess.run", return_value=_FakeProcess(stdout=envelope)):
            result = _call_pi("test prompt")
        assert result is not None
        assert result["adjust"] is False

    def test_markdown_fenced_json_parsed(self):
        inner = "```json\n" + _ok_response(adjust=True, sizing=0.5) + "\n```"
        with patch("subprocess.run", return_value=_FakeProcess(stdout=inner)):
            result = _call_pi("test prompt")
        assert result is not None
        assert result["adjust"] is True


# ──────────────────────────────────────────────────────────────────────────────
# run_overlay — end-to-end with mocked subprocess + tmp DB
# ──────────────────────────────────────────────────────────────────────────────


class TestRunOverlay:
    """
    Integration-style tests for run_overlay().

    All external I/O is mocked:
    - RegimeModel.classify_and_record via patch("overlay.engine.RegimeModel")
    - subprocess.run for pi CLI
    - _load_news / _load_charts for data sources
    """

    def _patch_overlay(self, regime, subprocess_result, news="", charts=""):
        """Return a context-manager stack for standard overlay mocking."""
        from contextlib import ExitStack

        stack = ExitStack()
        stack.enter_context(
            patch("overlay.engine.RegimeModel",
                  **{"return_value.classify_and_record.return_value": regime})
        )
        stack.enter_context(
            patch("subprocess.run", return_value=subprocess_result)
        )
        stack.enter_context(
            patch("overlay.engine._load_news", return_value=news)
        )
        stack.enter_context(
            patch("overlay.engine._load_charts", return_value=charts)
        )
        return stack

    def test_returns_overlay_decision_type(self, tmp_db):
        regime = _make_regime()
        proc = _FakeProcess(stdout=_ok_response(adjust=False))
        with self._patch_overlay(regime, proc):
            decision = run_overlay(mode="log_only")
        assert isinstance(decision, OverlayDecision)

    def test_no_change_response_propagated(self, tmp_db):
        regime = _make_regime()
        proc = _FakeProcess(stdout=_ok_response(adjust=False, reasoning="all clear"))
        with self._patch_overlay(regime, proc):
            decision = run_overlay(mode="log_only")
        assert decision.adjust is False
        assert decision.reasoning == "all clear"

    def test_tighten_response_propagated(self, tmp_db):
        regime = _make_regime(sizing=0.7)
        proc = _FakeProcess(
            stdout=_ok_response(adjust=True, sizing=0.4,
                                universes=["sp500"], tickers=["AAPL"])
        )
        with self._patch_overlay(regime, proc):
            decision = run_overlay(mode="log_only")
        assert decision.adjust is True
        assert decision.sizing_multiplier_override == 0.4
        assert "sp500" in decision.universes_to_deactivate
        assert "AAPL" in decision.tickers_to_avoid

    def test_log_only_mode_still_returns_decision(self, tmp_db):
        """log_only mode does not block; decision is returned for inspection."""
        regime = _make_regime()
        proc = _FakeProcess(stdout=_ok_response(adjust=True, sizing=0.3))
        with self._patch_overlay(regime, proc):
            decision = run_overlay(mode="log_only")
        assert isinstance(decision, OverlayDecision)
        assert decision.adjust is True

    def test_active_mode_returns_decision_for_plan(self, tmp_db):
        regime = _make_regime()
        proc = _FakeProcess(stdout=_ok_response(adjust=True, sizing=0.5))
        with self._patch_overlay(regime, proc):
            decision = run_overlay(mode="active")
        assert isinstance(decision, OverlayDecision)
        assert decision.adjust is True

    def test_regime_failure_returns_no_change(self, tmp_db):
        """Regime model crash → safe no_change, pipeline not blocked."""
        with patch("overlay.engine.RegimeModel") as MockModel:
            MockModel.return_value.classify_and_record.side_effect = (
                RuntimeError("macro_indicators table is empty")
            )
            decision = run_overlay(mode="log_only")
        assert isinstance(decision, OverlayDecision)
        assert decision.adjust is False
        assert "error" in decision.reasoning.lower()

    def test_pi_timeout_returns_no_change(self, tmp_db):
        """pi CLI timeout → no_change, pipeline not blocked."""
        regime = _make_regime()
        with self._patch_overlay(
            regime,
            subprocess_result=None,  # won't be used — side_effect below
        ):
            with patch("subprocess.run",
                       side_effect=subprocess.TimeoutExpired("pi", 120)):
                decision = run_overlay(mode="log_only")
        assert decision.adjust is False
        assert "error" in decision.reasoning.lower()

    def test_pi_bad_json_returns_no_change(self, tmp_db):
        """Garbage pi response → no_change."""
        regime = _make_regime()
        proc = _FakeProcess(stdout="here is some prose, not JSON at all")
        with self._patch_overlay(regime, proc):
            decision = run_overlay(mode="log_only")
        assert decision.adjust is False

    def test_sizing_violation_clamped(self, tmp_db):
        """LLM tries to loosen sizing → clamped to regime cap."""
        regime = _make_regime(sizing=0.7)
        proc = _FakeProcess(
            stdout=_ok_response(adjust=True, sizing=2.0)  # massive violation
        )
        with self._patch_overlay(regime, proc):
            decision = run_overlay(mode="log_only")
        assert decision.sizing_multiplier_override == 0.7  # clamped

    def test_decision_written_to_db(self, tmp_db):
        """A decision is persisted to overlay_decisions after run_overlay."""
        regime = _make_regime()
        proc = _FakeProcess(
            stdout=_ok_response(adjust=True, sizing=0.3, tickers=["AAPL"])
        )
        with self._patch_overlay(regime, proc, news="market news", charts="RSI=30"):
            run_overlay(mode="log_only")

        rows = get_overlay_decisions(days=1)
        assert len(rows) >= 1
        latest = rows[0]
        assert latest["action"] == "tighten"
        assert latest["sizing_override"] == 0.3
        assert "AAPL" in (latest.get("tickers_avoided") or [])

    def test_no_change_decision_written_to_db(self, tmp_db):
        """no_change decisions are also persisted."""
        regime = _make_regime()
        proc = _FakeProcess(stdout=_ok_response(adjust=False))
        with self._patch_overlay(regime, proc):
            run_overlay(mode="log_only")

        rows = get_overlay_decisions(days=1)
        assert len(rows) >= 1
        assert rows[0]["action"] == "no_change"

    def test_db_failure_does_not_crash(self, tmp_db):
        """DB write failure is logged but does not propagate — pipeline safe."""
        regime = _make_regime()
        proc = _FakeProcess(stdout=_ok_response(adjust=False))
        with self._patch_overlay(regime, proc), \
             patch("overlay.engine._record_decision",
                   side_effect=Exception("DB locked")):
            decision = run_overlay(mode="log_only")
        # Should still return a valid decision despite DB failure
        assert isinstance(decision, OverlayDecision)

    def test_news_and_charts_included_in_data_sources(self, tmp_db):
        """data_sources metadata reflects which sources were available."""
        regime = _make_regime()
        proc = _FakeProcess(stdout=_ok_response(adjust=False))
        with self._patch_overlay(regime, proc, news="headline news", charts=""):
            run_overlay(mode="log_only")

        rows = get_overlay_decisions(days=1)
        assert rows
        ds = rows[0].get("data_sources") or {}
        assert ds.get("news_available") is True
        assert ds.get("charts_available") is False
