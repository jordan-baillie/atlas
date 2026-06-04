#!/usr/bin/env python3
"""#423 Stage-0(c) FREE PROBE — ensemble short_horizon_mr x cross_sectional_momentum.

The board's thesis (memo 2026-06-04, Moonshot+Risk): shmr is a REAL high-turnover edge whose
only material defect was regime_concentration 5.16 (it earns in mean-reverting/choppy regimes),
while csm earns in trend regimes. If the two are uncorrelated, a portfolio of them should have
LOWER regime concentration and a higher, more stable cross-validated Sharpe -> possibly clearing
SCREEN at the PORTFOLIO level with zero new data. Evaluated through the SAME assemble_bundle +
evaluate_tiers gate panel as the battery. No gate loosening.

Runs each strategy once on the full universe (cached), then combines daily returns (50/50 and
inverse-vol) and concatenates trades (each already regime-tagged by the engine).
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
from backtest.engine import BacktestEngine  # noqa: E402
from scripts.strategy_evaluator import load_sandbox_strategy  # noqa: E402
from utils.config import get_active_config  # noqa: E402

CACHE = PROJECT / "backtest" / "results" / "_ensemble_cache.pkl"


def run_strategy(name: str, data, max_positions: int):
    cfg = get_active_config("sp500")
    cfg.setdefault("strategies", {})[name] = {"enabled": True}
    cfg.setdefault("risk", {})["max_open_positions"] = max_positions
    res = BacktestEngine(cfg).run_walkforward(data, [load_sandbox_strategy(name)(cfg)])
    return adapter.daily_returns(res.equity_curve), res.trades


def main(market="sp500", max_positions=15, use_cache=True):
    if use_cache and CACHE.exists():
        comp = pickle.loads(CACHE.read_bytes())
        print("loaded cached component backtests")
    else:
        data = vo.load_data(market=market)
        data = {k: v for k, v in data.items() if len(v) >= 260}
        comp = {}
        for nm in ("cross_sectional_momentum", "short_horizon_mr"):
            print(f"running {nm} (full universe)...", flush=True)
            r, tr = run_strategy(nm, data, max_positions)
            comp[nm] = {"returns": r, "trades": tr}
        CACHE.write_bytes(pickle.dumps(comp))

    csm, shmr = comp["cross_sectional_momentum"], comp["short_horizon_mr"]
    rc, rs = csm["returns"], shmr["returns"]
    idx = rc.index.union(rs.index)
    rc = rc.reindex(idx).fillna(0.0)
    rs = rs.reindex(idx).fillna(0.0)
    corr = float(pd.concat([rc, rs], axis=1).corr().iloc[0, 1])

    # inverse-vol weights (full-sample; a fixed pre-registered rule, not optimized)
    vc, vs = rc.std() or 1e-9, rs.std() or 1e-9
    wc, ws = (1 / vc) / (1 / vc + 1 / vs), (1 / vs) / (1 / vc + 1 / vs)
    ens5050 = 0.5 * rc + 0.5 * rs
    ensIV = wc * rc + ws * rs
    alltrades = list(csm["trades"]) + list(shmr["trades"])

    print(f"\ndaily-return correlation csm vs shmr: {corr:+.3f}  (low/negative => strong diversification)")
    for nm, r in (("csm only", rc), ("shmr only", rs), ("ens 50/50", ens5050),
                  (f"ens inv-vol ({wc:.2f}/{ws:.2f})", ensIV)):
        paths = adapter.cpcv_path_sharpes(pd.Series(r).dropna())
        print(f"  {nm:26s} net Sharpe {cm.annualized_sharpe(r.to_numpy()):+.2f} "
              f"| CPCV med {np.median(paths) if paths else float('nan'):+.3f} "
              f"| cum {r.sum()*100:+.0f}%")

    # Full gate panel on each ensemble variant (trades combined for regime/ticker gates).
    grid = {"csm": rc, "shmr": rs, "ens5050": ens5050, "ensIV": ensIV}
    for label, r in (("ENSEMBLE 50/50", ens5050), ("ENSEMBLE inv-vol", ensIV)):
        split = int(len(r) * 0.80)
        fnet = float(pd.Series(r).iloc[split:].sum() * 1e4)
        out = adapter.assemble_bundle(pd.Series(r).dropna(), alltrades, grid, forward_net=fnet)
        b = out["bundle"]
        t = adapter.evaluate_tiers(out["bundle"])
        print(f"\n===== {label} (gate panel, same as battery) =====")
        print(f"CPCV {b['median_cpcv_sharpe']:.3f} | frac+ {b['frac_paths_positive']:.2f} "
              f"| PBO {b['pbo']:.3f} | DSR {b['dsr']:.3f}")
        print(f"regime_conc {b['regime_concentration_ratio']:.2f} (shmr-alone was 5.16) "
              f"| per_regime_ok {b['per_regime_expectancy_ok']} | min_regime {b['min_regime_sharpe']:.2f} "
              f"| top_ticker {b['top_group_frac']:.2f} | loo_ok {b['loo_group_ok']} "
              f"| fwd {b.get('forward_net', float('nan')):.0f}")
        print(f"regime_net: { {k: round(v,1) for k,v in out['diagnostics']['regime']['regime_net'].items()} }")
        print(f"GATES: {t.get('gates')}")
        print(f"TIER: {t.get('tier')}")


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-cache", action="store_true")
    ap.add_argument("--max-positions", type=int, default=15)
    a = ap.parse_args()
    main(max_positions=a.max_positions, use_cache=not a.no_cache)
