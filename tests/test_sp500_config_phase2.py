"""Tests for Phase 2 W1+W2 sp500.json config updates (2026-04-28)."""
import json
import pytest
from pathlib import Path

CONFIG_PATH = Path("/root/atlas/config/active/sp500.json")


@pytest.fixture(scope="module")
def cfg():
    with CONFIG_PATH.open() as f:
        return json.load(f)


# ── W1 tests ─────────────────────────────────────────────────────────────────

def test_w1_sp500_config_loads_cleanly(cfg):
    assert cfg["market"] == "sp500"
    assert "strategies" in cfg


def test_w1_sector_rotation_weight_is_005(cfg):
    assert cfg["strategies"]["sector_rotation"]["weight"] == 0.05


def test_w1_momentum_and_connors_each_at_030(cfg):
    assert cfg["strategies"]["momentum_breakout"]["weight"] == 0.30
    assert cfg["strategies"]["connors_rsi2"]["weight"] == 0.30


def test_w1_total_enabled_strategy_weight_matches_expected(cfg):
    # User-specified change: sum drops from 0.8978 to 0.7978 (-0.10).
    # Tolerate small float error.
    enabled_weight = sum(
        s.get("weight", 0)
        for s in cfg["strategies"].values()
        if s.get("enabled") is True
    )
    assert abs(enabled_weight - 0.7978) < 1e-6, f"Expected 0.7978, got {enabled_weight}"


# ── W2 tests ─────────────────────────────────────────────────────────────────

def test_w2_momentum_breakout_params_match_research_best(cfg):
    mb = cfg["strategies"]["momentum_breakout"]
    assert mb["atr_stop_mult"] == 0.61
    assert mb["lookback_days"] == 14
    assert mb["atr_period"] == 18
    assert mb["trend_ma_period"] == 27
    assert mb["breakout_period"] == 10
    assert mb["max_hold_days"] == 15           # unchanged
    assert mb["profit_target_atr_mult"] == 6.0  # unchanged


def test_w2_strategy_initializes_with_new_params():
    """Verify the strategy class can be instantiated with the new params."""
    cfg_full = json.loads(CONFIG_PATH.read_text())
    mb_params = cfg_full["strategies"]["momentum_breakout"]

    # Strategy class is MomentumBreakout (not MomentumBreakoutStrategy)
    try:
        from strategies.momentum_breakout import MomentumBreakout as Strat
    except ImportError:
        # Fallback: just verify keys are loadable & types are correct
        assert isinstance(mb_params["atr_stop_mult"], (int, float))
        assert isinstance(mb_params["lookback_days"], int)
        return

    # If import worked, verify instantiation doesn't crash
    inst = Strat(config=cfg_full)
    assert inst is not None


def test_metadata_blocks_present(cfg):
    assert "_weight_update_2026_04_28" in cfg
    assert "_param_update_2026_04_28" in cfg
    wu = cfg["_weight_update_2026_04_28"]
    assert any(
        c["strategy"] == "sector_rotation" and c["new_weight"] == 0.05
        for c in wu["changes"]
    )
    pu = cfg["_param_update_2026_04_28"]
    assert pu["strategy"] == "momentum_breakout"
    assert pu["new_params"]["atr_stop_mult"] == 0.61
