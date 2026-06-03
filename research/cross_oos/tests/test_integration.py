"""End-to-end integration: run a synthetic candidate through the full cross-OOS battery.

A genuinely-good strategy (a real, persistent, cross-asset/venue/regime edge) must PASS every
Plan §2 gate; pure noise must FAIL. This proves cpcv + overfitting + splitters + metrics +
gates wire together, not just that each unit works in isolation.
"""
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from research.cross_oos import cpcv, gates, metrics, overfitting, splitters  # noqa: E402

PPY = metrics.DAYS_PER_YEAR


def _build_bundle(per_bar, config_matrix, asset_net, venue_lab, venue_ret,
                  regime_lab, regime_ret, forward) -> dict:
    # Axis 1 — CPCV path distribution (annualized Sharpe per held-out block)
    path_sharpes = [
        metrics.annualized_sharpe(per_bar[s.test_idx], PPY)
        for s in cpcv.cpcv_splits(len(per_bar), n_groups=6, k_test=2)
    ]
    path_sharpes = [x for x in path_sharpes if x == x]

    # Multiple-testing controls
    pbo = overfitting.pbo_cscv(config_matrix, n_splits=10)["pbo"]
    cfg_sr = overfitting._col_sharpe(config_matrix)
    dsr = overfitting.deflated_sharpe_ratio(
        sr=metrics.sharpe(per_bar, periods=1), n_obs=len(per_bar),
        n_trials=config_matrix.shape[1], sr_variance=float(np.nanvar(cfg_sr)),
    )

    # Axis 2 — cross-asset concentration
    top_asset_frac = max(asset_net.values()) / sum(asset_net.values())

    # Axis 3 — cross-venue LOO: each held-out venue must be net-positive
    loo_venue_ok = all(
        venue_ret[s.test_idx].sum() > 0 for s in splitters.leave_one_venue_out(venue_lab)
    )

    # Axis 4 — regime robustness
    strat = splitters.regime_stratify(regime_lab)
    reg_sharpes = {k: metrics.annualized_sharpe(regime_ret[idx], PPY) for k, idx in strat.items()}
    reg_net = {k: float(regime_ret[idx].sum()) for k, idx in strat.items()}
    tot = sum(abs(v) for v in reg_net.values()) + 1e-12

    return {
        "median_cpcv_sharpe": float(np.median(path_sharpes)),
        "frac_paths_positive": float(np.mean([x > 0 for x in path_sharpes])),
        "pbo": pbo,
        "dsr": dsr,
        "top_asset_frac": top_asset_frac,
        "loo_venue_ok": loo_venue_ok,
        "min_regime_sharpe": min(reg_sharpes.values()),
        "max_regime_pnl_frac": max(abs(v) for v in reg_net.values()) / tot,
        "cost_stress_sharpe": metrics.annualized_sharpe(per_bar - 0.0004, PPY),
        "forward_net": float(forward.sum()),
    }


def test_good_strategy_passes_full_battery():
    rng = np.random.default_rng(42)
    n, ntrials = 3000, 12
    # per-period Sharpe ~0.18 → annualized ~3.4: a strong, real edge
    per_bar = rng.normal(0.0018, 0.01, n)
    cfg = rng.normal(0.0, 0.01, (n, ntrials))
    cfg[:, 7] = per_bar  # one genuinely-dominant config
    asset_net = {"BTC": 1.0, "ETH": 0.95, "SOL": 0.9, "BNB": 0.85}        # diversified
    venue_lab = np.array((["bybit"] * 1000) + (["binance"] * 1000) + (["okx"] * 1000))
    venue_ret = rng.normal(0.0018, 0.01, n)                                # all venues +ve
    reg = np.array(["bull"] * 1100 + ["chop"] * 1000 + ["bear"] * 900)
    reg_ret = rng.normal(0.0018, 0.01, n)
    forward = rng.normal(0.0018, 0.01, 400)

    bundle = _build_bundle(per_bar, cfg, asset_net, venue_lab, venue_ret, reg, reg_ret, forward)
    rep = gates.evaluate_gates(bundle)
    assert rep["overall_pass"] is True, "\n" + gates.format_report(rep)


def test_pure_noise_fails_battery():
    rng = np.random.default_rng(1)
    n, ntrials = 3000, 12
    per_bar = rng.normal(0.0, 0.01, n)             # zero edge
    cfg = rng.normal(0.0, 0.01, (n, ntrials))      # all noise → PBO ~0.5, DSR low
    asset_net = {"BTC": 1.0, "ETH": 0.02, "SOL": 0.01, "BNB": 0.01}  # concentrated
    venue_lab = np.array((["bybit"] * 1500) + (["binance"] * 1500))
    venue_ret = rng.normal(0.0, 0.01, n)
    reg = np.array(["bull"] * 1100 + ["chop"] * 1000 + ["bear"] * 900)
    reg_ret = rng.normal(0.0, 0.01, n)
    forward = rng.normal(0.0, 0.01, 400)

    bundle = _build_bundle(per_bar, cfg, asset_net, venue_lab, venue_ret, reg, reg_ret, forward)
    rep = gates.evaluate_gates(bundle)
    assert rep["overall_pass"] is False
