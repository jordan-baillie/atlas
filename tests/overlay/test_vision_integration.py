"""
tests/overlay/test_vision_integration.py
Vision integration tests for the Atlas chart-vision overlay feature.

Covers:
  - call_pi_vision path validation
  - call_pi_vision command-line construction
  - overlay_vision flag=OFF gate (render_reference_set never called)
  - overlay_vision flag=ON populates decision.chart_vision_signals

All subprocess calls, LLM calls, and chart-render calls are mocked —
no network traffic, no disk writes beyond tmp_path.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Ensure project root is on sys.path
PROJECT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT))

# ---------------------------------------------------------------------------
# Minimal PNG bytes (1x1 transparent PNG — valid file but tiny)
# ---------------------------------------------------------------------------
_MINIMAL_PNG = bytes([
    0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A,  # PNG signature
    0x00, 0x00, 0x00, 0x0D, 0x49, 0x48, 0x44, 0x52,  # IHDR chunk length + type
    0x00, 0x00, 0x00, 0x01, 0x00, 0x00, 0x00, 0x01,  # 1x1
    0x08, 0x06, 0x00, 0x00, 0x00, 0x1F, 0x15, 0xC4,  # bit depth, color type, ...
    0x89, 0x00, 0x00, 0x00, 0x0A, 0x49, 0x44, 0x41,  # IDAT chunk
    0x54, 0x78, 0x9C, 0x62, 0x00, 0x01, 0x00, 0x00,
    0x05, 0x00, 0x01, 0x0D, 0x0A, 0x2D, 0xB4, 0x00,  # IDAT data (zlib)
    0x00, 0x00, 0x00, 0x00, 0x49, 0x45, 0x4E, 0x44,  # IEND chunk
    0xAE, 0x42, 0x60, 0x82,
])


# ═══════════════════════════════════════════════════════════════════════════════
# Test 1: call_pi_vision — path validation
# ═══════════════════════════════════════════════════════════════════════════════

class TestCallPiVisionPathValidation:
    """call_pi_vision raises FileNotFoundError for missing image paths."""

    def test_missing_path_raises(self, tmp_path):
        """Nonexistent image raises FileNotFoundError before subprocess is called."""
        from utils.pi_subprocess import call_pi_vision

        missing = tmp_path / "nonexistent.png"
        with pytest.raises(FileNotFoundError, match="Image not found"):
            call_pi_vision("test prompt", [missing])

    def test_mix_existing_and_missing_raises(self, tmp_path):
        """Even one missing path among existing files raises FileNotFoundError."""
        from utils.pi_subprocess import call_pi_vision

        existing = tmp_path / "real.png"
        existing.write_bytes(_MINIMAL_PNG)
        missing = tmp_path / "ghost.png"

        with pytest.raises(FileNotFoundError, match="Image not found"):
            call_pi_vision("test prompt", [existing, missing])

    def test_empty_image_list_proceeds_to_subprocess(self, tmp_path, monkeypatch):
        """Empty image list is valid (no validation error); subprocess is called."""
        from utils.pi_subprocess import call_pi_vision

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = '{"trend": "up"}'
        mock_result.stderr = ""

        captured = {}

        def fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            return mock_result

        monkeypatch.setattr(subprocess, "run", fake_run)

        result = call_pi_vision("prompt", [], model="claude-opus-4-7")
        assert result == '{"trend": "up"}'
        assert "--model" in captured["cmd"]
        assert "claude-opus-4-7" in captured["cmd"]


# ═══════════════════════════════════════════════════════════════════════════════
# Test 2: call_pi_vision — command construction
# ═══════════════════════════════════════════════════════════════════════════════

class TestCallPiVisionCommandConstruction:
    """call_pi_vision builds the pi CLI command correctly."""

    def test_cmd_contains_at_refs_model_systemp_mode(self, tmp_path, monkeypatch):
        """Verify @path refs, --model, --system-prompt, --mode appear in cmd."""
        from utils.pi_subprocess import call_pi_vision

        # Create two real PNG files
        img1 = tmp_path / "spy_daily.png"
        img2 = tmp_path / "qqq_daily.png"
        img1.write_bytes(_MINIMAL_PNG)
        img2.write_bytes(_MINIMAL_PNG)

        captured = {}

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "{}"
        mock_result.stderr = ""

        def fake_run(cmd, **kwargs):
            captured["cmd"] = list(cmd)
            captured["kwargs"] = kwargs
            return mock_result

        monkeypatch.setattr(subprocess, "run", fake_run)

        call_pi_vision(
            "analyse these charts",
            [img1, img2],
            model="claude-opus-4-7",
            mode="json",
            system_prompt="test-system-prompt",
        )

        cmd = captured["cmd"]

        # @-path references must be present
        assert f"@{img1}" in cmd, f"@img1 not in cmd: {cmd}"
        assert f"@{img2}" in cmd, f"@img2 not in cmd: {cmd}"

        # Model flag
        assert "--model" in cmd
        assert "claude-opus-4-7" in cmd

        # System prompt flag
        assert "--system-prompt" in cmd
        assert "test-system-prompt" in cmd

        # Mode flag
        assert "--mode" in cmd
        assert "json" in cmd

        # Prompt appears somewhere (as last positional arg)
        assert "analyse these charts" in cmd

    def test_mode_none_omits_mode_flag(self, tmp_path, monkeypatch):
        """mode=None must omit --mode from the command."""
        from utils.pi_subprocess import call_pi_vision

        img = tmp_path / "chart.png"
        img.write_bytes(_MINIMAL_PNG)

        captured_cmd = []

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "{}"
        mock_result.stderr = ""

        monkeypatch.setattr(
            subprocess, "run",
            lambda cmd, **kw: (captured_cmd.__setitem__(slice(None), cmd), mock_result)[1],
        )

        call_pi_vision("prompt", [img], mode=None)
        assert "--mode" not in captured_cmd

    def test_extra_args_appear_in_cmd(self, tmp_path, monkeypatch):
        """extra_args are included in the command."""
        from utils.pi_subprocess import call_pi_vision

        img = tmp_path / "chart.png"
        img.write_bytes(_MINIMAL_PNG)

        captured_cmd = []

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "{}"
        mock_result.stderr = ""

        monkeypatch.setattr(
            subprocess, "run",
            lambda cmd, **kw: (captured_cmd.__setitem__(slice(None), cmd), mock_result)[1],
        )

        call_pi_vision("prompt", [img], extra_args=["--no-tools"])
        assert "--no-tools" in captured_cmd

    def test_nonzero_exit_raises_pi_subprocess_error(self, tmp_path, monkeypatch):
        """Non-zero return code raises PiSubprocessError."""
        from utils.pi_subprocess import call_pi_vision, PiSubprocessError

        img = tmp_path / "chart.png"
        img.write_bytes(_MINIMAL_PNG)

        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stdout = ""
        mock_result.stderr = "something went wrong"

        monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: mock_result)

        with pytest.raises(PiSubprocessError, match="pi CLI failed"):
            call_pi_vision("prompt", [img])

    def test_timeout_raises_pi_subprocess_error(self, tmp_path, monkeypatch):
        """TimeoutExpired wraps as PiSubprocessError."""
        from utils.pi_subprocess import call_pi_vision, PiSubprocessError

        img = tmp_path / "chart.png"
        img.write_bytes(_MINIMAL_PNG)

        def fake_run(cmd, **kwargs):
            raise subprocess.TimeoutExpired(cmd, 10)

        monkeypatch.setattr(subprocess, "run", fake_run)

        with pytest.raises(PiSubprocessError, match="timed out"):
            call_pi_vision("prompt", [img], timeout=10)


# ═══════════════════════════════════════════════════════════════════════════════
# Shared regime mock fixture
# ═══════════════════════════════════════════════════════════════════════════════

def _make_regime_mock():
    """Build a minimal fake RegimeClassification that satisfies _validate_response."""
    from regime.states import RegimeState

    regime = MagicMock()
    regime.state = RegimeState.BULL_RISK_ON
    regime.sizing_multiplier = 1.0
    regime.max_positions = 10
    regime.active_universes = ["sp500"]
    regime.enabled_strategies = ["momentum_breakout"]
    regime.reasoning = "test regime"
    regime.scores = {}
    regime.date = "2026-04-17"
    return regime


def _make_engine_patches(monkeypatch, fake_pi_response, cfg_vision_dict):
    """Apply the common set of monkeypatches needed to run run_overlay in tests."""
    import overlay.engine as engine_mod
    import db.atlas_db as atlas_db_mod

    # Mock RegimeModel
    fake_regime = _make_regime_mock()
    mock_model = MagicMock()
    mock_model.classify_and_record.return_value = fake_regime
    monkeypatch.setattr(engine_mod, "RegimeModel", MagicMock(return_value=mock_model))

    # Mock _call_pi to return the base text-only response
    monkeypatch.setattr(engine_mod, "_call_pi", lambda prompt: fake_pi_response)

    # Mock config loader
    monkeypatch.setattr(
        "utils.config.load_config",
        lambda path=None: cfg_vision_dict,
    )

    # Mock DB write (avoid touching SQLite)
    monkeypatch.setattr(engine_mod, "_record_decision", lambda *a, **kw: 1)

    # Mock all data source loaders (avoid network / file I/O)
    for loader in ("_load_news", "_load_charts", "_load_sector_rotation",
                   "_load_aaii_sentiment", "_load_etf_flows", "_load_macro_surprise"):
        monkeypatch.setattr(engine_mod, loader, lambda: "")

    return fake_regime


# ═══════════════════════════════════════════════════════════════════════════════
# Test 3: Flag OFF — render_reference_set is never called
# ═══════════════════════════════════════════════════════════════════════════════

class TestOverlayFlagOff:
    """When overlay_vision.enabled=false, chart_renders are never invoked."""

    def test_flag_off_does_not_call_render(self, monkeypatch):
        """render_reference_set must NOT be called when enabled=false."""
        import overlay.engine as engine_mod

        cfg = {"overlay_vision": {"enabled": False}}
        fake_pi_response = {
            "adjust": False,
            "reasoning": "test — no tightening",
            "confidence": 0.0,
        }

        _make_engine_patches(monkeypatch, fake_pi_response, cfg)

        # If render_reference_set is somehow called, raise to fail the test
        render_called = {"flag": False}

        def forbidden_render(*args, **kwargs):
            render_called["flag"] = True
            raise AssertionError("render_reference_set called despite flag=off")

        monkeypatch.setattr(
            "overlay.sources.chart_renders.render_reference_set",
            forbidden_render,
        )

        from overlay.engine import run_overlay
        decision = run_overlay(mode="log_only")

        assert not render_called["flag"], "render_reference_set was invoked with flag OFF"
        assert decision.chart_vision_signals == [], (
            "chart_vision_signals should be empty when flag is off"
        )
        assert isinstance(decision.adjust, bool)

    def test_flag_off_returns_baseline_decision(self, monkeypatch):
        """Flag=off path returns the text-only decision unchanged."""
        import overlay.engine as engine_mod

        cfg = {"overlay_vision": {"enabled": False}}
        fake_pi_response = {
            "adjust": True,
            "sizing_multiplier_override": 0.7,
            "reasoning": "elevated risk",
            "confidence": 0.8,
            "universes_to_deactivate": [],
            "tickers_to_avoid": ["NVDA"],
        }

        _make_engine_patches(monkeypatch, fake_pi_response, cfg)

        from overlay.engine import run_overlay
        decision = run_overlay(mode="log_only")

        assert decision.adjust is True
        assert decision.sizing_multiplier_override == pytest.approx(0.7)
        assert "NVDA" in decision.tickers_to_avoid
        assert decision.chart_vision_signals == []


# ═══════════════════════════════════════════════════════════════════════════════
# Test 4: Flag ON — chart_vision_signals populated
# ═══════════════════════════════════════════════════════════════════════════════

class TestOverlayFlagOn:
    """When overlay_vision.enabled=true, chart_vision_signals is populated."""

    def test_flag_on_populates_vision_signals(self, monkeypatch, tmp_path):
        """Vision branch runs and populates decision.chart_vision_signals."""
        import overlay.engine as engine_mod

        cfg = {
            "overlay_vision": {
                "enabled": True,
                "model": "claude-opus-4-7",
                "max_images": 3,
                "timeout_seconds": 30,
            }
        }
        fake_pi_response = {
            "adjust": False,
            "reasoning": "no tightening",
            "confidence": 0.0,
        }
        vision_signals = [
            {
                "ticker": "SPY",
                "pattern": "ascending triangle",
                "support": 500.0,
                "resistance": 520.0,
                "tighten_rec": False,
                "confidence": 0.7,
            }
        ]
        vision_response = {
            "adjust": False,
            "reasoning": "ok",
            "chart_vision_signals": vision_signals,
        }

        _make_engine_patches(monkeypatch, fake_pi_response, cfg)

        # Create a fake PNG in tmp_path
        fake_png = tmp_path / "SPY_daily_1y.png"
        fake_png.write_bytes(_MINIMAL_PNG)

        # Mock render_reference_set to return the fake image
        monkeypatch.setattr(
            "overlay.sources.chart_renders.render_reference_set",
            lambda positions=None, out_dir=None, max_images=10: {"SPY_daily_1y": fake_png},
        )

        # Mock _call_pi_with_vision to return vision_response
        monkeypatch.setattr(
            engine_mod,
            "_call_pi_with_vision",
            lambda prompt, labels_and_paths, model=None, timeout=None: vision_response,
        )

        from overlay.engine import run_overlay
        decision = run_overlay(mode="log_only")

        assert len(decision.chart_vision_signals) == 1, (
            f"Expected 1 vision signal, got {len(decision.chart_vision_signals)}"
        )
        sig = decision.chart_vision_signals[0]
        assert sig["ticker"] == "SPY"
        assert sig["pattern"] == "ascending triangle"
        assert sig["support"] == pytest.approx(500.0)
        assert sig["resistance"] == pytest.approx(520.0)
        assert sig["confidence"] == pytest.approx(0.7)

    def test_flag_on_vision_error_is_nonfatal(self, monkeypatch, tmp_path):
        """An exception in the vision path does NOT prevent a valid decision."""
        import overlay.engine as engine_mod

        cfg = {
            "overlay_vision": {
                "enabled": True,
                "model": "claude-opus-4-7",
                "max_images": 3,
                "timeout_seconds": 30,
            }
        }
        fake_pi_response = {
            "adjust": False,
            "reasoning": "no tightening",
            "confidence": 0.0,
        }

        _make_engine_patches(monkeypatch, fake_pi_response, cfg)

        # Render raises an unexpected exception
        monkeypatch.setattr(
            "overlay.sources.chart_renders.render_reference_set",
            lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("render failed")),
        )

        from overlay.engine import run_overlay
        decision = run_overlay(mode="log_only")

        # Decision still valid, vision signals empty
        assert decision.chart_vision_signals == []
        assert isinstance(decision.adjust, bool)


# ═══════════════════════════════════════════════════════════════════════════════
# Test 5: _call_pi_with_vision unit tests
# ═══════════════════════════════════════════════════════════════════════════════

class TestCallPiWithVision:
    """Unit tests for the _call_pi_with_vision engine function directly."""

    def test_returns_none_when_circuit_breaker_tripped(self, monkeypatch):
        """If the circuit breaker is tripped, _call_pi_with_vision returns None."""
        from overlay.engine import _call_pi_with_vision

        monkeypatch.setattr(
            "utils.claude_circuit_breaker.is_tripped",
            lambda: True,
        )

        result = _call_pi_with_vision("prompt", [], model="claude-opus-4-7", timeout=10)
        assert result is None

    def test_returns_none_on_subprocess_error(self, monkeypatch, tmp_path):
        """PiSubprocessError from call_pi_vision is caught and returns None."""
        from overlay.engine import _call_pi_with_vision
        from utils.pi_subprocess import PiSubprocessError

        fake_png = tmp_path / "test.png"
        fake_png.write_bytes(_MINIMAL_PNG)

        monkeypatch.setattr(
            "utils.claude_circuit_breaker.is_tripped",
            lambda: False,
        )
        monkeypatch.setattr(
            "utils.pi_subprocess.call_pi_vision",
            lambda *a, **kw: (_ for _ in ()).throw(PiSubprocessError("timeout")),
        )

        result = _call_pi_with_vision(
            "prompt",
            [("SPY_daily", fake_png)],
            model="claude-opus-4-7",
            timeout=10,
        )
        assert result is None

    def test_augmented_prompt_contains_vision_block(self, monkeypatch, tmp_path):
        """The prompt sent to call_pi_vision contains the CHART IMAGES ATTACHED block."""
        from overlay.engine import _call_pi_with_vision

        fake_png = tmp_path / "spy.png"
        fake_png.write_bytes(_MINIMAL_PNG)

        captured_prompt = []

        monkeypatch.setattr(
            "utils.claude_circuit_breaker.is_tripped",
            lambda: False,
        )

        def fake_vision(prompt, paths, **kwargs):
            captured_prompt.append(prompt)
            return '{"adjust": false, "reasoning": "ok", "chart_vision_signals": []}'

        monkeypatch.setattr("utils.pi_subprocess.call_pi_vision", fake_vision)

        result = _call_pi_with_vision(
            "BASE PROMPT",
            [("SPY_daily_1y", fake_png)],
            model="claude-opus-4-7",
            timeout=10,
        )

        assert len(captured_prompt) == 1
        prompt_sent = captured_prompt[0]
        assert "BASE PROMPT" in prompt_sent
        assert "CHART IMAGES ATTACHED" in prompt_sent
        assert "chart_vision_signals" in prompt_sent
        assert "SPY_daily_1y" in prompt_sent
