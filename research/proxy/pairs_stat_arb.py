#!/usr/bin/env python3
"""#422 Stage-0(b) FREE PROBE — market-neutral pairs / statistical-arbitrage.

A DIFFERENT signal class from momentum (which we've now tested twice). Dollar-neutral spread
mean-reversion is regime-balanced by construction and gives many independent bets -> a natural
complement to the bull/trend-leaning csm + shmr basket (#423), and the candidate 3rd leg that
could push the ensemble's regime_concentration under 2.0.

The Atlas engine is LONG-ONLY, so (like the #421 gap-fade proxy) this is a standalone,
look-ahead-free WALK-FORWARD simulator using only daily closes:
  - Gatev et al. (2006) distance method: on a 252d FORMATION window, normalise each price to a
    cumulative-return index and pick the top-K pairs by smallest sum-of-squared-distance.
  - Trade them OUT-OF-SAMPLE on the next 63d window: enter when the normalised spread diverges
    > entry_z formation-sigmas (long the loser / short the winner), exit on reversion (|z|<exit_z)
    or at window end. Pair selection & sigma come ONLY from the formation window (no look-ahead).
  - Dollar-neutral pair return = sign * (ret_long_leg - ret_short_leg); pessimistic costs.

Judged through the SAME assemble_bundle + evaluate_tiers panel as the battery, then combined
with the cached csm + shmr returns into a proper 3-way ensemble. No gate loosening.
"""
from __future__ import annotations

import pickle
import sys
from pathlib import Path

import numpy as np
import pandas as pd

PROJECT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT))

import scripts.validate_oos as vo  # noqa: E402
from research.cross_oos import adapter  # noqa: E402
import research.cross_oos.metrics as cm  # noqa: E402
from research.proxy.gap_fade_proxy import build_panels, regime_series  # noqa: E402

ENS_CACHE = PROJECT / "backtest" / "results" / "_ensemble_cache.pkl"

# Pre-registered defaults + small grid (committed before seeing results).
DEFAULT = {"formation": 252, "trading": 63, "top_pairs": 20, "entry_z": 2.0, "exit_z": 0.5}
GRID = [{"formation": 252, "trading": 63, "top_pairs": k, "entry_z": z, "exit_z": 0.5}
        for k in (10, 20, 30) for z in (1.5, 2.0, 2.5)]


def simulate(C: pd.DataFrame, reg: pd.Series, *, formation, trading, top_pairs,
             entry_z, exit_z, cost_bps_leg=10.0):
    """Walk-forward Gatev pairs. Returns (daily_return Series, trades list)."""
    idx = list(C.index)
    rets = C.pct_change()
    daily = pd.Series(0.0, index=C.index)
    trades = []
    cost = (2.0 * cost_bps_leg) / 1e4  # both legs, round trip, charged at entry (conservative)

    start = formation
    while start < len(idx):
        form = C.iloc[start - formation:start]
        tr_dates = idx[start:start + trading]
        if not tr_dates:
            break
        valid = form.columns[form.notna().all()]
        valid = [t for t in valid if (form[t] > 0).all()]
        if len(valid) >= 4:
            norm = form[valid] / form[valid].iloc[0]
            M = norm.to_numpy()
            sq = (M ** 2).sum(0)
            gram = M.T @ M
            ssd = sq[:, None] + sq[None, :] - 2.0 * gram
            n = len(valid)
            iu = np.triu_indices(n, 1)
            order = np.argsort(ssd[iu])[:top_pairs]
            base = form[valid].iloc[0]
            for oi in order:
                a, b = valid[iu[0][oi]], valid[iu[1][oi]]
                sform = norm[a] - norm[b]
                mu, sd = float(sform.mean()), float(sform.std() or 1e-9)
                pos, entry_d = 0, None
                for d in tr_dates:
                    za, zb = C[a].loc[d] / base[a], C[b].loc[d] / base[b]
                    z = ((za - zb) - mu) / sd
                    if pos != 0:  # accrue daily pnl, then check exit
                        pr = pos * (rets[a].loc[d] - rets[b].loc[d])
                        if np.isfinite(pr):
                            daily.loc[d] += pr / top_pairs
                        if abs(z) < exit_z or d == tr_dates[-1]:
                            trades.append({"ticker": f"{a}/{b}", "pnl": float(pos * ((za - zb) - 0) * 1e3),
                                           "exit_date": d, "entry_regime": reg.loc[entry_d]})
                            pos = 0
                    elif z > entry_z:
                        pos, entry_d = -1, d  # spread rich -> short A long B
                        daily.loc[d] -= cost / top_pairs
                    elif z < -entry_z:
                        pos, entry_d = +1, d  # spread cheap -> long A short B
                        daily.loc[d] -= cost / top_pairs
        start += trading
    return daily, trades


def _panel(b, diag=None):
    s = (f"CPCV {b['median_cpcv_sharpe']:.3f} | frac+ {b['frac_paths_positive']:.2f} "
         f"| PBO {b['pbo']:.3f} | DSR {b['dsr']:.3f} | regime_conc {b['regime_concentration_ratio']:.2f} "
         f"| per_regime_ok {b['per_regime_expectancy_ok']} | min_regime {b['min_regime_sharpe']:.2f} "
         f"| top_grp {b['top_group_frac']:.2f} | loo_ok {b['loo_group_ok']} | fwd {b.get('forward_net', float('nan')):.0f}")
    return s


def main(market="sp500", top_liquid=120, cost_bps_leg=10.0):
    print(f"=== #422 pairs/stat-arb ({market}) ===")
    data = vo.load_data(market=market)
    O, C = build_panels(data, top_liquid=top_liquid)
    reg = regime_series(C)
    print(f"universe: {C.shape[1]} most-liquid | {C.shape[0]} days | cost {cost_bps_leg:.0f}bps/leg RT")

    pr, trades = simulate(C, reg, **DEFAULT, cost_bps_leg=cost_bps_leg)
    print(f"DEFAULT {DEFAULT}: net Sharpe {cm.annualized_sharpe(pr.to_numpy()):+.2f} "
          f"| cum {pr.sum()*100:+.0f}% | trades {len(trades)}")

    grid = {}
    for cfg in GRID:
        s, _ = simulate(C, reg, **cfg, cost_bps_leg=cost_bps_leg)
        grid[f"k{cfg['top_pairs']}_z{cfg['entry_z']}"] = s
    split = int(len(pr) * 0.80)
    fnet = float(pr.iloc[split:].sum() * 1e4)
    out = adapter.assemble_bundle(pr, trades, grid, forward_net=fnet)
    t = adapter.evaluate_tiers(out["bundle"])
    print("\n--- PAIRS standalone (gate panel) ---")
    print(_panel(out["bundle"]))
    print(f"TIER: {t.get('tier')}")

    # ---- 3-way ensemble: csm + shmr + pairs ----
    if ENS_CACHE.exists():
        comp = pickle.loads(ENS_CACHE.read_bytes())
        rc = comp["cross_sectional_momentum"]["returns"]
        rs = comp["short_horizon_mr"]["returns"]
        tr_csm = comp["cross_sectional_momentum"]["trades"]
        tr_shmr = comp["short_horizon_mr"]["trades"]
        idx = rc.index.union(rs.index).union(pr.index)
        rc, rs, rp = (x.reindex(idx).fillna(0.0) for x in (rc, rs, pr))
        cm3 = pd.concat([rc, rs, rp], axis=1)
        cm3.columns = ["csm", "shmr", "pairs"]
        print("\ncorrelation matrix (csm/shmr/pairs):")
        print(cm3.corr().round(2).to_string())
        ens = (rc + rs + rp) / 3.0
        alltr = list(tr_csm) + list(tr_shmr) + list(trades)
        grid3 = {"csm": rc, "shmr": rs, "pairs": rp, "ens3": ens}
        split = int(len(ens) * 0.80)
        fnet3 = float(ens.iloc[split:].sum() * 1e4)
        out3 = adapter.assemble_bundle(ens.dropna(), alltr, grid3, forward_net=fnet3)
        t3 = adapter.evaluate_tiers(out3["bundle"])
        print(f"\n===== 3-WAY ENSEMBLE (csm+shmr+pairs, equal-weight) net Sharpe "
              f"{cm.annualized_sharpe(ens.to_numpy()):+.2f} | cum {ens.sum()*100:+.0f}% =====")
        print(_panel(out3["bundle"]))
        print(f"regime_net: { {k: round(v,1) for k,v in out3['diagnostics']['regime']['regime_net'].items()} }")
        print(f"GATES: {t3.get('gates')}")
        print(f"TIER: {t3.get('tier')}")
    else:
        print("\n(no ensemble cache; run ensemble_eval.py first for the 3-way)")


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--market", default="sp500")
    ap.add_argument("--top-liquid", type=int, default=120)
    ap.add_argument("--cost-bps-leg", type=float, default=10.0)
    a = ap.parse_args()
    main(a.market, a.top_liquid, a.cost_bps_leg)
