"""Tests for cli.py plan generator: passive-universe skip and PAPER lifecycle inclusion.

Covers:
  1. cmd_plan skips passive universe (Task #300)
  2. cmd_plan proceeds for live mode
  3. cmd_plan proceeds for paper mode
  4. cmd_plan skips disabled mode
  5. get_strategies includes PAPER lifecycle strategies with research_best params
  6. get_strategies excludes RESEARCH lifecycle strategies
  7. get_strategies deduplicates live-and-paper strategies (live params win)
  8. get_strategies falls back gracefully on strategy_lifecycle DB failure

Run:
    cd /root/atlas && python3 -m pytest tests/test_plan_generator_lifecycle.py -v --timeout=30
"""
from __future__ import annotations

import copy
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

PROJECT = Path(__file__).resolve().parent.parent
if str(PROJECT) not in sys.path:
    sys.path.insert(0, str(PROJECT))

import db.atlas_db as _adb
from db.atlas_db import init_db, upsert_research_best
from monitor.strategy_lifecycle import PromotionState, transition
from scripts.cli import DEFAULT_MARKET, _STRATEGY_REGISTRY, get_strategies

TESTS_DIR = Path(__file__).resolve().parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))
from conftest import MINIMAL_CONFIG  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _config(mode: str = "live", market: str = "sp500") -> dict:
    """Return a minimal config dict with a specific trading.mode.

    For mode="live", live_enabled is set to True so the #300 live_enabled
    gate does not block the test.  MINIMAL_CONFIG has live_enabled=False
    (paper/test default); live mode tests need it True.
    """
    cfg = copy.deepcopy(MINIMAL_CONFIG)
    cfg["market"] = market
    cfg["trading"]["mode"] = mode
    if mode == "live":
        cfg["trading"]["live_enabled"] = True
    return cfg


def _make_args(market: str = "sp500", date: str | None = None):
    """Fake argparse namespace for cmd_plan."""
    return SimpleNamespace(market=market, date=date)


# ---------------------------------------------------------------------------
# Fix 1: cmd_plan early-exit for passive/non-live-or-paper universes
# ---------------------------------------------------------------------------

class TestCmdPlanModeGate:
    """cmd_plan must skip plan generation when trading.mode is not live or paper."""

    def test_cmd_plan_skips_passive_universe(self, monkeypatch):
        """Passive mode → return early, TradePlanGenerator never instantiated."""
        from scripts.cli import cmd_plan

        monkeypatch.setattr(
            "scripts.cli.get_active_config",
            lambda market_id: _config("passive"),
        )

        mock_gen_cls = MagicMock()
        monkeypatch.setattr("scripts.cli.TradePlanGenerator", mock_gen_cls)

        args = _make_args()
        cmd_plan(args)

        mock_gen_cls.assert_not_called()

    def test_cmd_plan_proceeds_for_live_mode(self, monkeypatch):
        """Live mode → does NOT trigger early-exit (gets past the mode check).

        We stub out everything after the mode guard with a side-effect that
        raises StopIteration so we can confirm execution reached the next block.
        """
        from scripts.cli import cmd_plan

        monkeypatch.setattr(
            "scripts.cli.get_active_config",
            lambda market_id: _config("live"),
        )

        # Raise immediately after the mode guard, inside get_tickers() —
        # confirms the early-exit was NOT taken.
        monkeypatch.setattr(
            "scripts.cli.get_tickers",
            lambda *a, **kw: (_ for _ in ()).throw(StopIteration("reached")),
        )

        with pytest.raises(StopIteration, match="reached"):
            cmd_plan(_make_args())

    def test_cmd_plan_proceeds_for_paper_mode(self, monkeypatch):
        """Paper mode → does NOT trigger early-exit."""
        from scripts.cli import cmd_plan

        monkeypatch.setattr(
            "scripts.cli.get_active_config",
            lambda market_id: _config("paper"),
        )
        monkeypatch.setattr(
            "scripts.cli.get_tickers",
            lambda *a, **kw: (_ for _ in ()).throw(StopIteration("reached")),
        )

        with pytest.raises(StopIteration, match="reached"):
            cmd_plan(_make_args())

    def test_cmd_plan_skips_disabled_mode(self, monkeypatch):
        """Any non live/paper mode (e.g. 'disabled') → early-exit."""
        from scripts.cli import cmd_plan

        monkeypatch.setattr(
            "scripts.cli.get_active_config",
            lambda market_id: _config("disabled"),
        )
        mock_gen_cls = MagicMock()
        monkeypatch.setattr("scripts.cli.TradePlanGenerator", mock_gen_cls)

        args = _make_args()
        cmd_plan(args)

        mock_gen_cls.assert_not_called()


# ---------------------------------------------------------------------------
# Fix 2: get_strategies includes PAPER lifecycle strategies
# ---------------------------------------------------------------------------

class TestGetStrategiesLifecycleInclusion:
    """get_strategies must include PAPER lifecycle strategies with research_best params."""

    def _seed_paper_strategy(self, strategy: str, universe: str, params: dict) -> None:
        """Seed strategy_lifecycle + research_best for a PAPER strategy."""
        # Transition from None → RESEARCH → PAPER (using operator='manual' to bypass graph
        # from None directly to PAPER, since RESEARCH→PAPER is the normal path).
        transition(strategy, universe, PromotionState.RESEARCH, reason="test seed", operator="manual")
        transition(strategy, universe, PromotionState.PAPER, reason="test seed paper", operator="system")
        # Seed research_best params
        upsert_research_best(
            strategy=strategy,
            universe=universe,
            params=params,
            sharpe=1.27,
            trades=294,
            max_dd_pct=24.88,
        )

    def test_get_strategies_includes_paper_lifecycle(self):
        """PAPER lifecycle strategy is instantiated alongside live-config strategies."""
        self._seed_paper_strategy(
            "short_term_mr",
            "sp500",
            {"rsi_oversold": 15, "atr_stop_mult": 2.5, "ibs_oversold": 0.4},
        )

        # Config: only momentum_breakout is enabled in live config.
        cfg = copy.deepcopy(MINIMAL_CONFIG)
        cfg["market"] = "sp500"
        # Disable everything except momentum_breakout
        for k in cfg["strategies"]:
            cfg["strategies"][k]["enabled"] = False
        cfg["strategies"]["momentum_breakout"]["enabled"] = True

        strats = get_strategies(cfg)
        names = [s.__class__.__name__ for s in strats]

        # Both live and paper strategies must be present
        assert "MomentumBreakout" in names, f"Expected MomentumBreakout in {names}"
        assert "ShortTermMR" in names, f"Expected ShortTermMR (PAPER) in {names}"

        # ShortTermMR must use research_best params
        stmr = next(s for s in strats if s.__class__.__name__ == "ShortTermMR")
        strat_cfg = stmr.config["strategies"]["short_term_mr"]
        assert strat_cfg.get("rsi_oversold") == 15, (
            f"Expected rsi_oversold=15, got {strat_cfg.get('rsi_oversold')}"
        )
        assert strat_cfg.get("atr_stop_mult") == 2.5, (
            f"Expected atr_stop_mult=2.5, got {strat_cfg.get('atr_stop_mult')}"
        )
        assert strat_cfg.get("ibs_oversold") == 0.4, (
            f"Expected ibs_oversold=0.4, got {strat_cfg.get('ibs_oversold')}"
        )

    def test_get_strategies_excludes_research_lifecycle(self):
        """RESEARCH lifecycle strategy must NOT be instantiated unless live-config-enabled."""
        # Seed bb_squeeze as RESEARCH only (not PAPER)
        transition("bb_squeeze", "sp500", PromotionState.RESEARCH, reason="test seed", operator="manual")

        # Config: bb_squeeze NOT in live config as enabled
        cfg = copy.deepcopy(MINIMAL_CONFIG)
        cfg["market"] = "sp500"
        for k in cfg["strategies"]:
            cfg["strategies"][k]["enabled"] = False
        cfg["strategies"]["momentum_breakout"]["enabled"] = True
        # bb_squeeze not in strategies dict at all (or explicitly disabled)
        # bb_squeeze may or may not be in MINIMAL_CONFIG; ensure it's disabled either way
        cfg["strategies"].setdefault("bb_squeeze", {})["enabled"] = False

        strats = get_strategies(cfg)
        names = [s.__class__.__name__ for s in strats]

        assert "BBSqueeze" not in names, (
            f"RESEARCH strategy BBSqueeze should NOT be instantiated; got {names}"
        )

    def test_get_strategies_dedup_live_and_paper(self):
        """Strategy enabled in both live config AND PAPER lifecycle → instantiated ONCE with live params."""
        # Seed short_term_mr as PAPER with different params than live config
        self._seed_paper_strategy(
            "short_term_mr",
            "sp500",
            {"rsi_oversold": 99, "atr_stop_mult": 9.9, "ibs_oversold": 0.9},
        )

        # Config: short_term_mr is ALSO enabled in live config with known params
        cfg = copy.deepcopy(MINIMAL_CONFIG)
        cfg["market"] = "sp500"
        for k in cfg["strategies"]:
            cfg["strategies"][k]["enabled"] = False
        cfg["strategies"]["momentum_breakout"]["enabled"] = True
        cfg["strategies"]["short_term_mr"]["enabled"] = True
        cfg["strategies"]["short_term_mr"]["rsi_oversold"] = 25   # live-config value
        cfg["strategies"]["short_term_mr"]["atr_stop_mult"] = 1.5

        strats = get_strategies(cfg)
        names = [s.__class__.__name__ for s in strats]

        # Exactly one ShortTermMR (no duplicate)
        stmr_instances = [s for s in strats if s.__class__.__name__ == "ShortTermMR"]
        assert len(stmr_instances) == 1, (
            f"Expected exactly one ShortTermMR instance; got {len(stmr_instances)}"
        )

        # Must use live-config params (25), NOT research_best params (99)
        strat_cfg = stmr_instances[0].config["strategies"]["short_term_mr"]
        assert strat_cfg.get("rsi_oversold") == 25, (
            f"Expected live-config rsi_oversold=25, got {strat_cfg.get('rsi_oversold')}  "
            f"(research_best had 99 — live config should win on dedup)"
        )

    def test_get_strategies_lifecycle_db_failure_fallback(self, monkeypatch):
        """DB failure in lifecycle lookup → safe fallback to live-config-only set, no crash."""
        # Patch list_state to raise an exception
        monkeypatch.setattr(
            "monitor.strategy_lifecycle.list_state",
            lambda state: (_ for _ in ()).throw(RuntimeError("DB connection failed")),
        )

        cfg = copy.deepcopy(MINIMAL_CONFIG)
        cfg["market"] = "sp500"
        for k in cfg["strategies"]:
            cfg["strategies"][k]["enabled"] = False
        cfg["strategies"]["momentum_breakout"]["enabled"] = True
        cfg["strategies"]["mean_reversion"]["enabled"] = True

        # Must not raise; must return live-config set only
        strats = get_strategies(cfg)
        names = [s.__class__.__name__ for s in strats]

        assert "MomentumBreakout" in names, "Live-config strategy must still be returned"
        assert "MeanReversion" in names, "Live-config strategy must still be returned"
        # No PAPER-only strategy should appear
        assert "ShortTermMR" not in names, (
            "PAPER-only strategy must not appear when lifecycle DB fails"
        )
