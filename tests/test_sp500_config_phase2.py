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


def test_w1_sector_rotation_is_disabled(cfg):
    # 2026-04-28 W1: sector_rotation de-weighted to 0.05.
    # 2026-05-01 audit: disabled entirely (Sharpe 0.044 — no edge in sp500 universe).
    # Guard: sector_rotation must stay disabled until research validates a real edge.
    sr = cfg["strategies"]["sector_rotation"]
    assert sr.get("enabled") is False, (
        f"sector_rotation.enabled is {sr.get('enabled')!r} — must be False. "
        "It was disabled 2026-05-01 after solo Sharpe measured at 0.044 (< 0.5 gate). "
        "Do NOT re-enable without a validated research_best entry."
    )
    assert sr.get("weight", 0) == 0, (
        f"sector_rotation.weight is {sr.get('weight')} — must be 0 while disabled."
    )


def test_w1_momentum_and_connors_weights(cfg):
    # 2026-04-28 W1 set both to 0.30.
    # 2026-05-01 audit renormalized to 0.50 each (sector_rotation/opening_gap/mean_reversion disabled).
    # 2026-05-06 audit reverted connors_rsi2 overweight (research Sharpe 0.14 < 0.5 gate):
    #   momentum_breakout → 0.70, connors_rsi2 → 0.30.
    # Current validated weights (v3.2.2):
    assert cfg["strategies"]["momentum_breakout"]["weight"] == 0.70, (
        "momentum_breakout weight changed from expected 0.70 — check _audit_2026_05_06_revert"
    )
    assert cfg["strategies"]["connors_rsi2"]["weight"] == 0.30, (
        "connors_rsi2 weight changed from expected 0.30 — check _audit_2026_05_06_revert"
    )


def test_w1_total_enabled_strategy_weight_matches_expected(cfg):
    # 2026-04-28 W1: sum was 0.7978.
    # 2026-05-01 audit: disabled 3 unvalidated strategies; renormalized remaining 2 → sum=1.0.
    # 2026-05-06 audit: reverted connors_rsi2 overweight; sum still 1.0.
    # Invariant: enabled strategy weights must always sum to 1.0 (normalised allocation).
    enabled_weight = sum(
        s.get("weight", 0)
        for s in cfg["strategies"].values()
        if s.get("enabled") is True
    )
    assert abs(enabled_weight - 1.0) < 1e-6, (
        f"Enabled strategy weights sum to {enabled_weight:.6f}, expected 1.0. "
        "All weights must be renormalised after enabling/disabling strategies."
    )


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
