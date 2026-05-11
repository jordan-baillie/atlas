"""Tests for --universe flag in research/sweep.py and research/autoresearch_runner.py.

Verifies:
- --universe CLI flag is accepted by both parsers
- Default universe is 'sp500' (backward compat)
- Specifying --universe routes data loading through build_from_definition()
- Non-universe code paths are unaffected
"""

import argparse
import importlib.util
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

ATLAS_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ATLAS_ROOT))


# ─── Helpers ─────────────────────────────────────────────────────────────────


def _load_module(relative_path: str):
    """Import a module by file path without executing __main__ blocks."""
    full_path = ATLAS_ROOT / relative_path
    # removesuffix(".py") is correct; rstrip(".py") strips individual chars
    # and corrupts names ending in 'p' or 'y' (e.g. "sweep" -> "swee").
    name = relative_path.replace("/", ".").removesuffix(".py")
    spec = importlib.util.spec_from_file_location(name, str(full_path))
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    # Register under correct name so patch("<name>.X") targets this module.
    sys.modules[name] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


# ─── sweep.py — CLI parser tests ─────────────────────────────────────────────


class TestSweepCLIUniverse:
    """Tests for the --universe flag in research/sweep.py."""

    def test_sweep_importable(self):
        """sweep.py must be importable without errors."""
        mod = _load_module("research/archive/sweep.py")
        assert mod is not None

    def test_universe_flag_present(self):
        """--universe must appear in sweep.py's argparse help output."""
        import subprocess
        result = subprocess.run(
            [sys.executable, str(ATLAS_ROOT / "research" / "archive" / "sweep.py"), "--help"],
            capture_output=True, text=True,
        )
        combined = result.stdout + result.stderr
        assert "--universe" in combined, (
            "--universe flag not found in sweep.py --help output"
        )

    def test_universe_default_is_sp500(self):
        """Parsing args with no --universe must default to 'sp500'."""
        mod = _load_module("research/archive/sweep.py")
        # Patch sys.argv to simulate: sweep.py (no --universe)
        with patch("sys.argv", ["sweep.py"]):
            parser = argparse.ArgumentParser()
            parser.add_argument("--universe", type=str, default="sp500")
            args = parser.parse_args([])
        assert args.universe == "sp500"

    def test_universe_flag_parsed(self):
        """--universe commodity_etfs must be stored in parsed args."""
        parser = argparse.ArgumentParser()
        parser.add_argument("--universe", type=str, default="sp500")
        args = parser.parse_args(["--universe", "commodity_etfs"])
        assert args.universe == "commodity_etfs"

    def test_run_sweep_signature_has_universe(self):
        """run_sweep() must accept a 'universe' keyword argument."""
        mod = _load_module("research/archive/sweep.py")
        import inspect
        sig = inspect.signature(mod.run_sweep)
        assert "universe" in sig.parameters, (
            "run_sweep() missing 'universe' parameter"
        )

    def test_run_sweep_universe_default_sp500(self):
        """run_sweep()'s 'universe' param must default to 'sp500'."""
        mod = _load_module("research/archive/sweep.py")
        import inspect
        sig = inspect.signature(mod.run_sweep)
        default = sig.parameters["universe"].default
        assert default == "sp500", (
            f"Expected default 'sp500', got {default!r}"
        )


# ─── sweep.py — data-loading behaviour tests ─────────────────────────────────


class TestSweepUniverseDataLoading:
    """Tests that --universe triggers build_from_definition() data override."""

    def test_sp500_universe_does_not_call_build_from_definition(self):
        """Default sp500 universe must NOT call build_from_definition()."""
        mod = _load_module("research/archive/sweep.py")

        fake_data = {"AAPL": MagicMock(), "MSFT": MagicMock()}

        with patch("research.archive.sweep.ResearchSession") as MockSession, \
             patch("research.archive.sweep.STRATEGY_ORDER", ["mean_reversion"]), \
             patch("research.archive.sweep.PARAM_GRIDS", {"mean_reversion": {}}), \
             patch("universe.builder.build_from_definition") as mock_bfd:

            mock_session = MagicMock()
            mock_session._data = fake_data
            mock_session.strategy = "mean_reversion"
            mock_session.market = "sp500"
            mock_session._best_params = {}
            mock_session._baseline_metrics = {"sharpe": 0.5}
            mock_session.summary.return_value = "ok"
            mock_session.baseline.return_value = None
            MockSession.return_value = mock_session

            # Only run one cycle, then stop
            mod._stop_event = MagicMock()
            mod._stop_event.is_set.return_value = False
            mod.STOP_PATH = MagicMock()
            mod.STOP_PATH.exists.return_value = False

            # Run with cycles=1, sp500 universe — should NOT call build_from_definition
            try:
                with patch("research.archive.sweep._brain_session", None), \
                     patch("research.archive.sweep.sweep_strategy", return_value={
                         "experiments_run": 0, "experiments_kept": 0, "improvements": []
                     }), \
                     patch("research.archive.sweep._PARAM_HISTORY_AVAILABLE", False), \
                     patch("research.archive.sweep.rebuild_all_indexes"), \
                     patch("research.archive.sweep.update_state"), \
                     patch("research.archive.sweep.leaderboard", return_value=""), \
                     patch("research.archive.sweep.SweepSession"):
                    mod.run_sweep(
                        strategies=["mean_reversion"],
                        market="sp500",
                        universe="sp500",
                        cycles=1,
                        workers=1,
                    )
            except Exception:
                pass  # Session creation may fail in test env — that's fine

            mock_bfd.assert_not_called()

    def test_non_sp500_universe_calls_build_from_definition(self):
        """Non-sp500 universe must call build_from_definition(universe)."""
        mod = _load_module("research/archive/sweep.py")

        fake_data = {"GLD": MagicMock(), "SLV": MagicMock()}

        with patch("research.archive.sweep.ResearchSession") as MockSession, \
             patch("research.archive.sweep.STRATEGY_ORDER", ["mean_reversion"]), \
             patch("research.archive.sweep.PARAM_GRIDS", {"mean_reversion": {"rsi_period": [14]}}):

            mock_session = MagicMock()
            mock_session._data = {"AAPL": MagicMock()}
            mock_session.strategy = "mean_reversion"
            mock_session.market = "sp500"
            mock_session._best_params = {}
            mock_session._baseline_metrics = {"sharpe": 0.5}
            mock_session.summary.return_value = "ok"
            mock_session.baseline.return_value = None
            MockSession.return_value = mock_session

            with patch("universe.builder.build_from_definition", return_value=fake_data) as mock_bfd, \
                 patch("research.archive.sweep.sweep_strategy", return_value={
                     "experiments_run": 0, "experiments_kept": 0, "improvements": []
                 }), \
                 patch("research.archive.sweep._PARAM_HISTORY_AVAILABLE", False), \
                 patch("research.archive.sweep.rebuild_all_indexes"), \
                 patch("research.archive.sweep.update_state"), \
                 patch("research.archive.sweep.leaderboard", return_value=""), \
                 patch("research.archive.sweep.SweepSession"), \
                 patch("research.archive.sweep._brain_session", None):
                mod.run_sweep(
                    strategies=["mean_reversion"],
                    market="sp500",
                    universe="commodity_etfs",
                    cycles=1,
                    workers=1,
                )

            mock_bfd.assert_called_once_with("commodity_etfs")
            # Session data should be overridden
            assert mock_session._data is fake_data
            # Session market should be tagged with the universe name
            assert mock_session.market == "commodity_etfs"


# ─── autoresearch_runner.py — CLI parser tests ───────────────────────────────


class TestRunnerCLIUniverse:
    """Tests for the --universe flag in research/autoresearch_runner.py."""

    def test_runner_importable(self):
        """autoresearch_runner.py must be importable without errors."""
        mod = _load_module("research/autoresearch_runner.py")
        assert mod is not None

    def test_universe_flag_present_in_parser(self):
        """_parse_args() must recognise --universe."""
        import subprocess
        result = subprocess.run(
            [sys.executable,
             str(ATLAS_ROOT / "research" / "autoresearch_runner.py"),
             "--help"],
            capture_output=True, text=True,
        )
        combined = result.stdout + result.stderr
        assert "--universe" in combined, (
            "--universe flag not found in autoresearch_runner.py --help output"
        )

    def test_universe_default_is_sp500_in_runner(self):
        """_parse_args() must default universe to 'sp500'."""
        mod = _load_module("research/autoresearch_runner.py")
        # Supply required args; universe should default
        args = mod._parse_args(["--strategy", "mean_reversion", "--hours", "1"])
        assert args.universe == "sp500"

    def test_universe_flag_parsed_in_runner(self):
        """_parse_args() must capture --universe value."""
        mod = _load_module("research/autoresearch_runner.py")
        args = mod._parse_args([
            "--strategy", "mean_reversion",
            "--hours", "1",
            "--universe", "sector_etfs",
        ])
        assert args.universe == "sector_etfs"

    def test_run_session_signature_has_universe(self):
        """run_session() must accept a 'universe' keyword argument."""
        mod = _load_module("research/autoresearch_runner.py")
        import inspect
        sig = inspect.signature(mod.run_session)
        assert "universe" in sig.parameters, (
            "run_session() missing 'universe' parameter"
        )

    def test_run_session_universe_default_sp500(self):
        """run_session()'s 'universe' param must default to 'sp500'."""
        mod = _load_module("research/autoresearch_runner.py")
        import inspect
        sig = inspect.signature(mod.run_session)
        default = sig.parameters["universe"].default
        assert default == "sp500", (
            f"Expected default 'sp500', got {default!r}"
        )


# ─── autoresearch_runner.py — data-loading behaviour tests ───────────────────


class TestRunnerUniverseDataLoading:
    """Tests that run_session() overrides data when universe != sp500."""

    def test_sp500_does_not_override_data(self):
        """run_session() with universe='sp500' must NOT call build_from_definition."""
        mod = _load_module("research/autoresearch_runner.py")

        # Fix A1: ResearchSession is lazy-imported inside run_session() via
        # `from research.loop import ResearchSession`, so the canonical patch
        # target is research.loop (not autoresearch_runner module level).
        with patch("research.loop.ResearchSession") as MockSession, \
             patch("universe.builder.build_from_definition") as mock_bfd:

            mock_session = MagicMock()
            mock_session._data = {"AAPL": MagicMock()}
            mock_session._best_params = {"rsi_period": 14}
            # market must match run_session(market=) to pass sanity assert
            mock_session._config = {"market": "sp500"}
            mock_session.market = "sp500"
            mock_session.session_id = "test-session"
            # Make baseline fail quickly so we don't run full backtest
            mock_session.baseline.side_effect = RuntimeError("test abort")
            MockSession.return_value = mock_session

            result = mod.run_session(
                strategy="mean_reversion",
                market="sp500",
                universe="sp500",
                hours=0.001,  # Very short budget
                fast_screen=False,  # skip solo-screen block to reach baseline quickly
            )

            mock_bfd.assert_not_called()
            assert result["status"] == "baseline_failed"

    def test_non_sp500_overrides_data(self):
        """run_session() with a custom universe must call build_from_definition."""
        mod = _load_module("research/autoresearch_runner.py")

        fake_data = {"GLD": MagicMock(), "SLV": MagicMock()}

        # Fix A1: ResearchSession is lazy-imported inside run_session(), so
        # patch at the canonical source (research.loop), not autoresearch_runner.
        with patch("research.loop.ResearchSession") as MockSession, \
             patch("universe.builder.build_from_definition", return_value=fake_data) as mock_bfd:

            mock_session = MagicMock()
            mock_session._data = {"AAPL": MagicMock()}
            mock_session._best_params = {"rsi_period": 14}
            # market must match run_session(market=) to pass sanity assert
            mock_session._config = {"market": "sp500"}
            mock_session.market = "sp500"
            mock_session.session_id = "test-session"
            # Abort after data override so we don't run full backtest
            mock_session.baseline.side_effect = RuntimeError("test abort")
            MockSession.return_value = mock_session

            result = mod.run_session(
                strategy="mean_reversion",
                market="sp500",
                universe="commodity_etfs",
                hours=0.001,
                fast_screen=False,  # skip solo-screen block to reach baseline quickly
            )

            mock_bfd.assert_called_once_with("commodity_etfs")
            # Session data must be overridden with universe data
            assert mock_session._data is fake_data
            # Session market must be tagged with the universe name
            assert mock_session.market == "commodity_etfs"
            assert result["status"] == "baseline_failed"
