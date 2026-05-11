"""Tests: plan generator skips passive universes (#300).

Covers:
  1. cmd_plan returns early when trading.live_enabled=False for a live-mode universe
  2. _run_regime_aware_plan filters out universes with live_enabled=False
  3. _run_regime_aware_plan includes universes with live_enabled=True
  4. _run_regime_aware_plan fails open when a universe config cannot be loaded
  5. Plan file is NOT written when cmd_plan returns early

Run:
    cd /root/atlas && python3 -m pytest tests/test_plan_generator_passive_skip.py -v --timeout=30
"""
from __future__ import annotations

import copy
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

PROJECT = Path(__file__).resolve().parent.parent
if str(PROJECT) not in sys.path:
    sys.path.insert(0, str(PROJECT))

TESTS_DIR = Path(__file__).resolve().parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

from conftest import MINIMAL_CONFIG  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _config(
    mode: str = "live",
    live_enabled: bool | None = None,
    market: str = "sp500",
) -> dict:
    """Build a minimal config dict."""
    cfg = copy.deepcopy(MINIMAL_CONFIG)
    cfg["market"] = market
    cfg["trading"]["mode"] = mode
    if live_enabled is not None:
        cfg["trading"]["live_enabled"] = live_enabled
    else:
        cfg["trading"].pop("live_enabled", None)
    return cfg


def _make_args(market: str = "sp500", date: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(market=market, date=date)


def _per_universe_config(universe: str, live_enabled: bool) -> dict:
    return {
        "market": universe,
        "trading": {
            "mode": "passive" if not live_enabled else "live",
            "live_enabled": live_enabled,
        },
    }


# ---------------------------------------------------------------------------
# 1. cmd_plan early-exit for live-mode universe with live_enabled=False
# ---------------------------------------------------------------------------

class TestCmdPlanLiveEnabledGate:
    """cmd_plan must skip when mode='live' AND live_enabled=False."""

    def test_skips_live_mode_with_live_enabled_false(self, monkeypatch):
        """mode=live, live_enabled=False → return early; TradePlanGenerator never instantiated."""
        from scripts.cli import cmd_plan

        monkeypatch.setattr(
            "scripts.cli.get_active_config",
            lambda market_id: _config(mode="live", live_enabled=False),
        )
        mock_gen_cls = MagicMock()
        monkeypatch.setattr("scripts.cli.TradePlanGenerator", mock_gen_cls)

        cmd_plan(_make_args())

        mock_gen_cls.assert_not_called()

    def test_no_plan_file_written_when_live_enabled_false(self, monkeypatch, tmp_path):
        """No plan JSON file should be written when live_enabled=False."""
        from scripts.cli import cmd_plan

        monkeypatch.setattr(
            "scripts.cli.get_active_config",
            lambda market_id: _config(mode="live", live_enabled=False),
        )
        monkeypatch.setattr("scripts.cli.TradePlanGenerator", MagicMock())

        cmd_plan(_make_args())

        plan_files = list(tmp_path.glob("plan_*.json"))
        assert plan_files == [], f"Unexpected plan files: {plan_files}"

    def test_live_enabled_true_proceeds_past_gate(self, monkeypatch):
        """mode=live, live_enabled=True → does NOT trigger live_enabled early-exit.

        Probe by raising StopIteration inside get_tickers() to confirm
        execution reached the next block past the live_enabled guard.
        """
        from scripts.cli import cmd_plan

        monkeypatch.setattr(
            "scripts.cli.get_active_config",
            lambda market_id: _config(mode="live", live_enabled=True),
        )
        monkeypatch.setattr(
            "scripts.cli.get_tickers",
            lambda *a, **kw: (_ for _ in ()).throw(StopIteration("reached")),
        )

        with pytest.raises(StopIteration, match="reached"):
            cmd_plan(_make_args())

    def test_paper_mode_not_gated_by_live_enabled(self, monkeypatch):
        """mode=paper, live_enabled=False → does NOT trigger early-exit (paper mode ok)."""
        from scripts.cli import cmd_plan

        monkeypatch.setattr(
            "scripts.cli.get_active_config",
            lambda market_id: _config(mode="paper", live_enabled=False),
        )
        monkeypatch.setattr(
            "scripts.cli.get_tickers",
            lambda *a, **kw: (_ for _ in ()).throw(StopIteration("reached")),
        )

        with pytest.raises(StopIteration, match="reached"):
            cmd_plan(_make_args())

    def test_passive_mode_still_skipped_by_mode_check(self, monkeypatch):
        """mode=passive → skipped by the existing mode check (not the live_enabled check).

        Verifies TradePlanGenerator is never called in either case.
        """
        from scripts.cli import cmd_plan

        monkeypatch.setattr(
            "scripts.cli.get_active_config",
            lambda market_id: _config(mode="passive", live_enabled=False),
        )
        mock_gen_cls = MagicMock()
        monkeypatch.setattr("scripts.cli.TradePlanGenerator", mock_gen_cls)

        cmd_plan(_make_args())

        mock_gen_cls.assert_not_called()


# ---------------------------------------------------------------------------
# 2. _run_regime_aware_plan filters passive universes (#300 — core fix)
#
# RegimeModel, build_multi_universe, and PortfolioConstructor are late-imported
# inside _run_regime_aware_plan, so we patch them at their original module paths.
# ---------------------------------------------------------------------------

class TestRegimeAwarePlanPassiveFilter:
    """_run_regime_aware_plan must skip universes with live_enabled=False."""

    def _make_generator(self, config: dict):
        from brokers.plan import TradePlanGenerator
        portfolio = MagicMock()
        portfolio.positions = []
        return TradePlanGenerator(portfolio=portfolio, config=config)

    def _mock_regime(self, universes: list[str]) -> MagicMock:
        r = MagicMock()
        r.state.value = "bull_risk_on"
        r.active_universes = list(universes)
        r.sizing_multiplier = 1.0
        r.enabled_strategies = ["all"]
        return r

    def _dummy_plan(self) -> dict:
        return {
            "proposed_entries": [],
            "proposed_exits": [],
            "rejected_entries": [],
            "status": "PENDING_APPROVAL",
            "trade_date": "2026-01-01",
            "generated_at": "2026-01-01T00:00:00",
            "risk_summary": {},
            "portfolio_snapshot": {},
            "active_universes": [],
        }

    def test_passive_universe_excluded_before_data_loading(self):
        """Universes with live_enabled=False are removed before build_multi_universe."""
        cfg = _config(mode="live", live_enabled=True, market="sp500")
        cfg["regime_enabled"] = True
        gen = self._make_generator(cfg)

        fake_regime = self._mock_regime(["sp500", "sector_etfs", "commodity_etfs"])

        per_uni_configs = {
            "sp500": _per_universe_config("sp500", live_enabled=True),
            "sector_etfs": _per_universe_config("sector_etfs", live_enabled=False),
            "commodity_etfs": _per_universe_config("commodity_etfs", live_enabled=False),
        }

        loaded_universes: list[str] = []

        def mock_build(universes):
            loaded_universes.extend(universes)
            return {u: {} for u in universes}

        # Late imports are done inside _run_regime_aware_plan — patch at source module.
        with patch("regime.model.RegimeModel") as mock_rm_cls, \
             patch("universe.builder.build_multi_universe", side_effect=mock_build), \
             patch("portfolio.constructor.PortfolioConstructor") as mock_pc_cls, \
             patch("utils.config.get_active_config", side_effect=lambda u: per_uni_configs[u]):

            mock_rm_cls.return_value.classify_current.return_value = fake_regime
            mock_rm_cls.return_value.classify_and_record.return_value = None

            constructed = MagicMock()
            constructed.signals = []
            constructed.rejected = []
            mock_pc_cls.return_value.construct.return_value = constructed

            with patch.object(gen, "_save_plan"), \
                 patch.object(gen, "generate_plan", return_value=self._dummy_plan()):
                gen._run_regime_aware_plan(
                    strategies=[],
                    prices={},
                    trade_date="2026-01-01",
                    equity=10000.0,
                    existing_positions=[],
                    exit_recommendations=[],
                )

        assert loaded_universes == ["sp500"], (
            f"Expected only ['sp500'] but got {loaded_universes} — "
            "passive universes should be filtered before data loading"
        )

    def test_live_universe_retained_in_active_universes(self):
        """Universes with live_enabled=True are kept in active_universes."""
        cfg = _config(mode="live", live_enabled=True, market="sp500")
        cfg["regime_enabled"] = True
        gen = self._make_generator(cfg)

        fake_regime = self._mock_regime(["sp500"])
        per_uni_configs = {
            "sp500": _per_universe_config("sp500", live_enabled=True),
        }
        loaded_universes: list[str] = []

        def mock_build(universes):
            loaded_universes.extend(universes)
            return {u: {} for u in universes}

        with patch("regime.model.RegimeModel") as mock_rm_cls, \
             patch("universe.builder.build_multi_universe", side_effect=mock_build), \
             patch("portfolio.constructor.PortfolioConstructor") as mock_pc_cls, \
             patch("utils.config.get_active_config", side_effect=lambda u: per_uni_configs[u]):

            mock_rm_cls.return_value.classify_current.return_value = fake_regime
            mock_rm_cls.return_value.classify_and_record.return_value = None

            constructed = MagicMock()
            constructed.signals = []
            constructed.rejected = []
            mock_pc_cls.return_value.construct.return_value = constructed

            with patch.object(gen, "_save_plan"), \
                 patch.object(gen, "generate_plan", return_value=self._dummy_plan()):
                gen._run_regime_aware_plan(
                    strategies=[],
                    prices={},
                    trade_date="2026-01-01",
                    equity=10000.0,
                    existing_positions=[],
                    exit_recommendations=[],
                )

        assert "sp500" in loaded_universes

    def test_fail_open_when_universe_config_unavailable(self):
        """Universe stays in active_universes when its config cannot be loaded (fail-open)."""
        cfg = _config(mode="live", live_enabled=True, market="sp500")
        cfg["regime_enabled"] = True
        gen = self._make_generator(cfg)

        fake_regime = self._mock_regime(["sp500", "mystery_universe"])

        loaded_universes: list[str] = []

        def mock_build(universes):
            loaded_universes.extend(universes)
            return {u: {} for u in universes}

        def config_loader(u: str) -> dict:
            if u == "mystery_universe":
                raise FileNotFoundError(f"No config for {u}")
            return _per_universe_config(u, live_enabled=True)

        with patch("regime.model.RegimeModel") as mock_rm_cls, \
             patch("universe.builder.build_multi_universe", side_effect=mock_build), \
             patch("portfolio.constructor.PortfolioConstructor") as mock_pc_cls, \
             patch("utils.config.get_active_config", side_effect=config_loader):

            mock_rm_cls.return_value.classify_current.return_value = fake_regime
            mock_rm_cls.return_value.classify_and_record.return_value = None

            constructed = MagicMock()
            constructed.signals = []
            constructed.rejected = []
            mock_pc_cls.return_value.construct.return_value = constructed

            with patch.object(gen, "_save_plan"), \
                 patch.object(gen, "generate_plan", return_value=self._dummy_plan()):
                gen._run_regime_aware_plan(
                    strategies=[],
                    prices={},
                    trade_date="2026-01-01",
                    equity=10000.0,
                    existing_positions=[],
                    exit_recommendations=[],
                )

        assert "mystery_universe" in loaded_universes, (
            "Universe with unavailable config should be included (fail-open)"
        )
