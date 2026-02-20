#!/usr/bin/env python3
"""Parameter Stability Analyzer for Atlas-ASX.

Runs perturbation analysis on a config to measure robustness.
- 10 random perturbation trials (all params ±15%)
- Single-parameter sensitivity analysis
- Stability scoring

Usage: python3 scripts/param_stability_report.py [config_file]
  Default: config/active_config.json
"""
import sys, json, copy, time, random, argparse
import numpy as np
from pathlib import Path
from datetime import datetime

sys.path.insert(0, '/a0/usr/projects/atlas-asx')
import pandas as pd
from backtest.engine import BacktestEngine
from strategies.mean_reversion import MeanReversion
from strategies.trend_following import TrendFollowing
from strategies.bb_squeeze import BBSqueeze
from strategies.opening_gap import OpeningGap

PROJECT = Path('/a0/usr/projects/atlas-asx')
DATA_DIR = PROJECT / 'data' / 'cache'
RESULTS_DIR = PROJECT / 'backtest' / 'results'

def load_data(min_rows=100):
    dd = {}
    for pf in sorted(DATA_DIR.glob('*.parquet')):
        if pf.stem == 'IOZ_AX': continue
        ticker = pf.stem.replace('_AX', '.AX')
        df = pd.read_parquet(pf)
        df.columns = [c.lower() for c in df.columns]
        if 'date' in df.columns:
            df['date'] = pd.to_datetime(df['date'])
            df = df.set_index('date')
        df.index = pd.to_datetime(df.index)
        if len(df) >= min_rows:
            dd[ticker] = df
    return dd

def build_strats(cfg):
    s = []
    if cfg['strategies'].get('mean_reversion', {}).get('enabled', True):
        s.append(MeanReversion(cfg))
    if cfg['strategies'].get('trend_following', {}).get('enabled', True):
        s.append(TrendFollowing(cfg))
    if cfg['strategies'].get('bb_squeeze', {}).get('enabled', True):
        s.append(BBSqueeze(cfg))
    if cfg['strategies'].get('opening_gap', {}).get('enabled', True):
        s.append(OpeningGap(cfg))
    return s

def norm_m(m):
    cagr = m.get('cagr', 0)
    cp = cagr * 100 if abs(cagr) < 2 else cagr
    dd = m.get('max_drawdown', 0)
    dp = dd * 100 if abs(dd) < 2 else dd
    wr = m.get('win_rate', 0)
    wp = wr * 100 if abs(wr) < 2 else wr
    return {
        'total_trades': m.get('total_trades', 0),
        'profit_factor': round(m.get('profit_factor', 0), 4),
        'sharpe': round(m.get('sharpe', 0), 4),
        'cagr_pct': round(cp, 4),
        'max_drawdown_pct': round(dp, 4),
        'win_rate_pct': round(wp, 2),
    }

def run_bt(cfg, data, label=""):
    t0 = time.time()
    engine = BacktestEngine(cfg)
    result = engine.run_walkforward(data, build_strats(cfg))
    el = time.time() - t0
    n = norm_m(result.metrics)
    n['runtime_s'] = round(el, 1)
    print(f"  [{label}] {el:.0f}s CAGR={n['cagr_pct']:.2f}% Sh={n['sharpe']:.4f} PF={n['profit_factor']:.4f} T={n['total_trades']}")
    sys.stdout.flush()
    return n

def perturb_all(cfg, seed, pct=0.15):
    rng = random.Random(seed)
    pc = copy.deepcopy(cfg)
    log = {}
    for sn, sc in pc['strategies'].items():
        if not isinstance(sc, dict): continue
        sl = {}
        for k, v in list(sc.items()):
            if isinstance(v, bool): continue
            if isinstance(v, int):
                f = rng.uniform(1-pct, 1+pct)
                nv = max(1, round(v * f))
                sc[k] = nv
                sl[k] = {'original': v, 'perturbed': nv, 'factor': round(f, 4)}
            elif isinstance(v, float):
                f = rng.uniform(1-pct, 1+pct)
                nv = round(v * f, 4)
                sc[k] = nv
                sl[k] = {'original': v, 'perturbed': nv, 'factor': round(f, 4)}
        if sl: log[sn] = sl
    return pc, log

def perturb_single(cfg, strategy, param, pct=0.15, direction=1):
    """Perturb a single parameter by +pct (direction=1) or -pct (direction=-1)."""
    pc = copy.deepcopy(cfg)
    sc = pc['strategies'].get(strategy, {})
    v = sc.get(param)
    if v is None: return pc, None
    factor = 1 + (pct * direction)
    if isinstance(v, int):
        nv = max(1, round(v * factor))
    elif isinstance(v, float):
        nv = round(v * factor, 4)
    else:
        return pc, None
    sc[param] = nv
    return pc, {'strategy': strategy, 'param': param, 'original': v, 'perturbed': nv, 'factor': round(factor, 4)}

def get_numeric_params(cfg):
    """Extract all numeric (non-bool) strategy parameters."""
    params = []
    for sn, sc in cfg['strategies'].items():
        if not isinstance(sc, dict): continue
        if not sc.get('enabled', True): continue
        for k, v in sc.items():
            if isinstance(v, bool): continue
            if isinstance(v, (int, float)):
                params.append((sn, k, v))
    return params

def main():
    parser = argparse.ArgumentParser(description='Parameter Stability Report')
    parser.add_argument('config', nargs='?', default=str(PROJECT / 'config' / 'active_config.json'))
    parser.add_argument('--trials', type=int, default=10, help='Number of random perturbation trials')
    parser.add_argument('--pct', type=float, default=0.15, help='Perturbation percentage (default 0.15 = ±15%%)')
    parser.add_argument('--skip-sensitivity', action='store_true', help='Skip single-param sensitivity analysis')
    args = parser.parse_args()

    print("=" * 70)
    print("PARAMETER STABILITY REPORT")
    print(f"Config: {args.config}")
    print(f"Trials: {args.trials}, Perturbation: ±{args.pct*100:.0f}%")
    print(f"Started: {datetime.now().isoformat()}")
    print("=" * 70)
    sys.stdout.flush()

    with open(args.config) as f:
        cfg = json.load(f)

    print(f"Config version: {cfg.get('version', 'unknown')}")

    print("\n[1] Loading market data...")
    sys.stdout.flush()
    data = load_data(min_rows=100)
    print(f"  {len(data)} tickers loaded")
    sys.stdout.flush()

    # Baseline run
    print("\n[2] Running BASELINE backtest...")
    sys.stdout.flush()
    baseline = run_bt(cfg, data, "baseline")

    # Random perturbation trials
    print(f"\n[3] Running {args.trials} RANDOM perturbation trials (±{args.pct*100:.0f}%)...")
    sys.stdout.flush()
    trials = []
    seeds = list(range(42, 42 + args.trials))
    for i, seed in enumerate(seeds):
        print(f"  Trial {i+1}/{args.trials} (seed={seed})...")
        sys.stdout.flush()
        pc, pl = perturb_all(cfg, seed, args.pct)
        tm = run_bt(pc, data, f"pert-{i+1}")
        tm['seed'] = seed
        tm['perturbation_log'] = pl
        trials.append(tm)

    # Perturbation summary
    psummary = {}
    for key in ['cagr_pct', 'sharpe', 'profit_factor', 'max_drawdown_pct', 'total_trades']:
        vals = [t[key] for t in trials]
        psummary[key] = {
            'mean': round(float(np.mean(vals)), 4),
            'std': round(float(np.std(vals)), 4),
            'min': round(float(np.min(vals)), 4),
            'max': round(float(np.max(vals)), 4),
        }

    # Stability score
    base_cagr = baseline['cagr_pct']
    mean_pert_cagr = psummary['cagr_pct']['mean']
    if base_cagr != 0:
        stability_score = round(mean_pert_cagr / base_cagr, 4)
    else:
        stability_score = 0

    print(f"\n--- PERTURBATION SUMMARY ---")
    print(f"{'Metric':<22} {'Baseline':>10} {'Mean':>10} {'Std':>10} {'Min':>10} {'Max':>10}")
    print("-" * 75)
    for key in ['cagr_pct', 'sharpe', 'profit_factor', 'max_drawdown_pct', 'total_trades']:
        bv = baseline[key]
        s = psummary[key]
        print(f"{key:<22} {bv:>10.4f} {s['mean']:>10.4f} {s['std']:>10.4f} {s['min']:>10.4f} {s['max']:>10.4f}")
    print(f"\nStability Score: {stability_score} (1.0 = perfectly stable)")

    # Single-parameter sensitivity analysis
    sensitivity = []
    if not args.skip_sensitivity:
        params = get_numeric_params(cfg)
        print(f"\n[4] Running SINGLE-PARAM sensitivity ({len(params)} params, 2 runs each)...")
        sys.stdout.flush()
        for i, (sn, pk, pv) in enumerate(params):
            print(f"  [{i+1}/{len(params)}] {sn}.{pk} = {pv}")
            sys.stdout.flush()
            # Perturb up
            pc_up, info_up = perturb_single(cfg, sn, pk, args.pct, direction=1)
            if info_up is None: continue
            m_up = run_bt(pc_up, data, f"{sn}.{pk}+{args.pct*100:.0f}%")
            # Perturb down
            pc_dn, info_dn = perturb_single(cfg, sn, pk, args.pct, direction=-1)
            m_dn = run_bt(pc_dn, data, f"{sn}.{pk}-{args.pct*100:.0f}%")
            # Calculate sensitivity
            cagr_range = abs(m_up['cagr_pct'] - m_dn['cagr_pct'])
            sharpe_range = abs(m_up['sharpe'] - m_dn['sharpe'])
            sensitivity.append({
                'strategy': sn,
                'param': pk,
                'original_value': pv,
                'cagr_up': m_up['cagr_pct'],
                'cagr_down': m_dn['cagr_pct'],
                'cagr_range': round(cagr_range, 4),
                'sharpe_up': m_up['sharpe'],
                'sharpe_down': m_dn['sharpe'],
                'sharpe_range': round(sharpe_range, 4),
            })

        # Sort by CAGR range descending
        sensitivity.sort(key=lambda x: x['cagr_range'], reverse=True)
        print(f"\n--- TOP 10 MOST SENSITIVE PARAMETERS (by CAGR range) ---")
        print(f"{'Strategy':<20} {'Param':<25} {'Value':>8} {'CAGR+':>8} {'CAGR-':>8} {'Range':>8}")
        print("-" * 80)
        for s in sensitivity[:10]:
            print(f"{s['strategy']:<20} {s['param']:<25} {s['original_value']:>8} {s['cagr_up']:>8.2f} {s['cagr_down']:>8.2f} {s['cagr_range']:>8.2f}
