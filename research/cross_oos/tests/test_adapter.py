"""Unit tests for the Atlas cross-OOS adapter (engine-free, synthetic inputs)."""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from research.cross_oos import adapter  # noqa: E402


def _equity_from_returns(returns, base=10000.0):
    idx = pd.date_range("2022-01-03", periods=len(returns) + 1, freq="B")
    eq = base * np.cumprod(np.concatenate([[1.0], 1.0 + np.asarray(returns)]))
    return pd.Series(eq, index=idx)


def test_daily_returns_roundtrip():
    rng = np.random.default_rng(0)
    r = rng.normal(0.0005, 0.01, 300)
    eq = _equity_from_returns(r)
    out = adapter.daily_returns(eq)
    assert len(out) == 300
    assert np.allclose(out.to_numpy(), r, atol=1e-9)


def test_cpcv_paths_count_and_finiteness():
    rng = np.random.default_rng(1)
    r = rng.normal(0.001, 0.01, 500)
    paths = adapter.cpcv_path_sharpes(r, n_groups=6, k_test=2)
    # C(6,2) = 15 splits, all finite
    assert len(paths) == 15
    assert all(np.isfinite(paths))


def test_group_attribution_and_concentration():
    trades = [
        {"ticker": "AAA", "pnl": 100.0, "exit_date": "2022-01-10", "entry_regime": "bull"},
        {"ticker": "AAA", "pnl": 50.0, "exit_date": "2022-01-11", "entry_regime": "bull"},
        {"ticker": "BBB", "pnl": -20.0, "exit_date": "2022-01-10", "entry_regime": "bear"},
        {"ticker": "CCC", "pnl": 10.0, "exit_date": "2022-01-12", "entry_regime": "chop"},
    ]
    piv = adapter.group_daily_pnl(trades, "ticker")
    assert set(piv.columns) == {"AAA", "BBB", "CCC"}
    assert piv["AAA"].sum() == 150.0
    # AAA dominates net abs PnL: 150 / (150+20+10) = 0.833
    tf = adapter.top_group_frac(piv)
    assert abs(tf - 150.0 / 180.0) < 1e-9


def test_regime_attribution_keys():
    trades = [
        {"ticker": "AAA", "pnl": 5.0, "exit_date": f"2022-01-{d:02d}", "entry_regime": "bull"}
        for d in range(3, 25)
    ] + [
        {"ticker": "BBB", "pnl": -1.0, "exit_date": f"2022-02-{d:02d}", "entry_regime": "bear"}
        for d in range(3, 25)
    ]
    reg = adapter.regime_attribution(trades)
    assert set(reg["regime_sharpe"].keys()) == {"bull", "bear"}
    assert reg["regime_net"]["bull"] > 0 > reg["regime_net"]["bear"]
    # two regimes, neither should be 100% of abs PnL
    assert 0.0 <= reg["max_regime_pnl_frac"] <= 1.0


def _make_grid(signal_mu, n=500, n_cfg=8, seed=10):
    """Grid of correlated daily-return series sharing a common signal + idiosyncratic noise."""
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2022-01-03", periods=n, freq="B")
    common = rng.normal(signal_mu, 0.008, n)
    out = {}
    for j in range(n_cfg):
        out[f"cfg{j}"] = pd.Series(common + rng.normal(0.0, 0.004, n), index=idx)
    return out


def test_battery_passes_on_real_edge():
    grid = _make_grid(signal_mu=0.0009, n=600, n_cfg=10, seed=2)  # genuine positive drift
    primary = grid["cfg0"]
    # Build trades that mirror a diversified, regime-spread book
    rng = np.random.default_rng(3)
    tickers = [f"T{i:02d}" for i in range(20)]
    regimes = ["bull", "bear", "chop", "neutral"]
    dates = pd.date_range("2022-01-03", periods=600, freq="B")
    trades = []
    for d in dates:
        for _ in range(2):
            trades.append({
                "ticker": rng.choice(tickers),
                "pnl": float(rng.normal(6.0, 25.0)),  # positive expectancy, diversified
                "exit_date": d, "entry_regime": rng.choice(regimes),
            })
    res = adapter.assemble_bundle(primary, trades, grid_returns=grid,
                                  forward_net=1234.0, oos_cagr_degradation_pct=-10.0)
    rep = adapter.evaluate(res["bundle"])
    # A genuine, diversified edge should clear PBO and have a positive CPCV median.
    assert res["bundle"]["pbo"] <= 0.5
    assert res["bundle"]["median_cpcv_sharpe"] > 0
    assert rep["n_missing"] == 0  # every enforced gate was measured


def test_battery_flags_noise_via_pbo():
    grid = _make_grid(signal_mu=0.0, n=600, n_cfg=12, seed=7)  # pure noise, no edge
    primary = grid["cfg0"]
    res = adapter.assemble_bundle(primary, trades=[], grid_returns=grid)
    # On pure noise PBO trends toward ~0.5 and the noise battery should not be a clean pass.
    assert res["bundle"]["pbo"] >= 0.3
    rep = adapter.evaluate(res["bundle"])
    assert rep["overall_pass"] is False


def test_evaluate_tiers_promote_screen_fail():
    base = {
        "median_cpcv_sharpe": 1.0, "frac_paths_positive": 0.8, "pbo": 0.1,
        "top_group_frac": 0.2, "loo_group_ok": True, "min_regime_sharpe": 0.3,
        "max_regime_pnl_frac": 0.4,
    }
    # DSR above promote bar -> PROMOTE
    r = adapter.evaluate_tiers({**base, "dsr": 0.95})
    assert r["tier"] == "PROMOTE" and r["promote"]["overall_pass"]
    # DSR between screen and promote -> SCREEN
    r = adapter.evaluate_tiers({**base, "dsr": 0.80})
    assert r["tier"] == "SCREEN" and not r["promote"]["overall_pass"] and r["screen"]["overall_pass"]
    # DSR below screen bar -> FAIL
    r = adapter.evaluate_tiers({**base, "dsr": 0.50})
    assert r["tier"] == "FAIL"
    # A failing NON-DSR gate -> FAIL even with perfect DSR
    r = adapter.evaluate_tiers({**base, "dsr": 0.99, "pbo": 0.9})
    assert r["tier"] == "FAIL"


def test_assemble_bundle_uses_search_burden_for_dsr():
    grid = _make_grid(signal_mu=0.0009, n=600, n_cfg=10, seed=2)
    primary = grid["cfg0"]
    # A heavy search burden (many distinct trials) must deflate DSR below the grid DSR.
    burden = {"n_trials": 471, "sr_variance_pp": (0.39 ** 2) / 252,
              "sr_variance_ann": 0.39 ** 2, "n_experiments": 5449,
              "strategies_found": ["x"], "source": "test"}
    res = adapter.assemble_bundle(primary, trades=[], grid_returns=grid, search_burden=burden)
    assert res["diagnostics"]["dsr_source"] == "search_history"
    assert res["diagnostics"]["search_burden"]["n_trials"] == 471
    # search-history DSR (authoritative) should be <= the grid proxy DSR here
    assert res["bundle"]["dsr"] <= res["diagnostics"]["dsr_grid"] + 1e-9


def test_evaluate_drops_unmeasured_timesplit_gates():
    # bundle without forward_net / oos_cagr_degradation_ok must not FAIL on them as 'missing'
    bundle = {
        "median_cpcv_sharpe": 1.0, "frac_paths_positive": 0.8, "pbo": 0.1, "dsr": 0.99,
        "top_group_frac": 0.2, "loo_group_ok": True, "min_regime_sharpe": 0.3,
        "max_regime_pnl_frac": 0.4,
    }
    rep = adapter.evaluate(bundle)
    names = {g.name for g in rep["gates"]}
    assert "forward_net" not in names
    assert "oos_cagr_degradation_ok" not in names
    assert rep["overall_pass"] is True


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
