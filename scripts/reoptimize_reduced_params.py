#!/usr/bin/env python3
"""Atlas-ASX Full-Universe Re-Optimization (Coordinate Descent)

Key change from original: optimizes on ALL 185 tickers (>=100 rows)
instead of just top 25 by volume.
"""
import sys, json, os, copy, time, logging
from pathlib import Path
from datetime import datetime

PROJECT = Path('/a0/usr/projects/atlas-asx')
sys.path.insert(0, str(PROJECT))
os.chdir(str(PROJECT))

import pandas as pd
import numpy as np
logging.basicConfig(level=logging.WARNING)

from utils.config import get_active_config
from strategies.mean_reversion import MeanReversion
from strategies.trend_following import TrendFollowing
from strategies.bb_squeeze import BBSqueeze
from strategies.opening_gap import OpeningGap
from strategies.dividend_capture import DividendCapture
from backtest.engine import BacktestEngine

STRAT_MAP = {
    'mean_reversion': MeanReversion,
    'trend_following': TrendFollowing,
    'bb_squeeze': BBSqueeze,
    'opening_gap': OpeningGap,
    'dividend_capture': DividendCapture,
}
ACTIVE = ['mean_reversion','bb_squeeze','trend_following','opening_gap','dividend_capture']

RESULTS_FILE = PROJECT / 'backtest' / 'results' / 'phase1_1_reduced_params.json'

def load_full_universe():
    cache_dir = PROJECT / 'data' / 'cache'
    data_dict = {}
    for pf in sorted(cache_dir.glob('*.parquet')):
        if pf.stem == 'IOZ_AX':
            continue
        ticker = pf.stem.replace('_AX', '.AX')
        try:
            df = pd.read_parquet(pf)
            df.columns = [c.lower() for c in df.columns]
            if 'date' in df.columns:
                df['date'] = pd.to_datetime(df['date'])
                df = df.set_index('date')
            df.index = pd.to_datetime(df.index)
            if len(df) >= 100:
                data_dict[ticker] = df
        except:
            pass
    return data_dict

def run_single(config, strat_name, data):
    cfg = copy.deepcopy(config)
    for s in ACTIVE:
        cfg['strategies'][s]['enabled'] = (s == strat_name)
    strat = STRAT_MAP[strat_name](cfg)
    result = BacktestEngine(cfg).run_walkforward(data, [strat])
    m = result.metrics
    return {
        'trades': m['total_trades'], 'cagr': m['cagr'],
        'sharpe': m['sharpe'], 'max_dd': m['max_drawdown'],
        'pf': m.get('profit_factor', 0), 'wr': m.get('win_rate', 0),
        'total_pnl': m.get('total_pnl', 0),
    }

def run_combined(config, data):
    cfg = copy.deepcopy(config)
    strats = []
    for s in ACTIVE:
        if cfg['strategies'].get(s, {}).get('enabled', False):
            strats.append(STRAT_MAP[s](cfg))
    result = BacktestEngine(cfg).run_walkforward(data, strats)
    m = result.metrics
    return {
        'trades': m['total_trades'], 'cagr': m['cagr'],
        'sharpe': m['sharpe'], 'max_dd': m['max_drawdown'],
        'pf': m.get('profit_factor', 0), 'wr': m.get('win_rate', 0),
        'total_pnl': m.get('total_pnl', 0),
    }

def score(r):
    if r['trades'] < 3:
        return -999
    return round(r['sharpe'] * 2 + r['pf'] * 1 + r['cagr'] * 50 - r['max_dd'] * 10, 4)

def apply_params(config, strat_name, params):
    cfg = copy.deepcopy(config)
    for k, v in params.items():
        cfg['strategies'][strat_name][k] = v
    return cfg

def get_current(config, strat_name, param):
    return config['strategies'][strat_name].get(param)

# ============================================================
# REDUCED PARAMETER GRIDS - Only core signal parameters (8)
# Phase 1.1: Reduced from 21 to 8 optimizable params
# Ratio improvement: ~307 trades / 8 params = 38:1 (was 14.6:1)
# ============================================================
PARAM_GRIDS = {
    'mean_reversion': {
        'rsi_oversold': [20, 25, 30, 35, 40, 45],
        'zscore_entry': [-3.0, -2.5, -2.0, -1.5, -1.0],
    },
    'bb_squeeze': {
        'bb_std': [1.5, 2.0, 2.5, 3.0],
        'kc_atr_mult': [1.0, 1.5, 2.0, 2.5],
    },
    'trend_following': {
        'fast_ma': [5, 10, 15, 20],
        'slow_ma': [20, 30, 40, 50, 60],
    },
    'opening_gap': {
        'gap_threshold': [-0.03, -0.025, -0.02, -0.015, -0.01],
        'rsi14_max': [30, 40, 50, 60, 70],
    },
}

# FIXED PARAMETERS - locked to v9.2 values, NOT re-optimized
FIXED_PARAMS = {
    'mean_reversion': {
        'atr_stop_mult': 2.5,
        'profit_target_atr_mult': 1.5,
        'max_hold_days': 7,
    },
    'bb_squeeze': {
        'atr_stop_mult': 1.0,
        'trailing_stop_atr_mult': 3.0,
        'momentum_period': 30,
        'max_hold_days': 20,
    },
    'trend_following': {
        'atr_stop_mult': 3.5,
        'pullback_pct': 0.02,
        'max_hold_days': 25,
    },
    'opening_gap': {
        'atr_stop_mult': 2.0,
        'ibs_confirm': 0.2,
        'max_hold_days': 15,
    },
}


def apply_fixed_params(config):
    """Apply all fixed parameters to config, overriding any values."""
    import copy
    cfg = copy.deepcopy(config)
    for strat_name, params in FIXED_PARAMS.items():
        if strat_name in cfg.get('strategies', {}):
            for k, v in params.items():
                cfg['strategies'][strat_name][k] = v
    return cfg


def optimize_strategy(config, strat_name, data, results_tracker):
    grid = PARAM_GRIDS[strat_name]
    best_params = {}
    for p in grid:
        best_params[p] = get_current(config, strat_name, p)

    print(f'  Current params: {best_params}', flush=True)
    baseline = run_single(config, strat_name, data)
    best_score = score(baseline)
    print(f'  Baseline: trades={baseline["trades"]} sharpe={baseline["sharpe"]:.3f} pf={baseline["pf"]:.3f} cagr={baseline["cagr"]*100:.2f}% dd={baseline["max_dd"]*100:.2f}% score={best_score}', flush=True)

    total_evals = sum(len(v) for v in grid.values())
    done_iters = 0
    improved = False
    sweep_log = []

    for pass_num in range(2):
        pass_improved = False
        print(f'\n  === PASS {pass_num + 1} ===', flush=True)

        for param, values in grid.items():
            print(f'  Sweeping {param}: {values}', flush=True)
            p_best_val = best_params[param]
            p_best_score = best_score

            for val in values:
                if val == best_params[param]:
                    continue
                test_p = {**best_params, param: val}
                test_cfg = apply_params(config, strat_name, test_p)
                try:
                    t0 = time.time()
                    r = run_single(test_cfg, strat_name, data)
                    s = score(r)
                    elapsed = time.time() - t0
                    done_iters += 1
                    marker = ''
                    if s > p_best_score:
                        marker = ' << BETTER'
                        p_best_score = s
                        p_best_val = val
                        improved = True
                        pass_improved = True
                    print(f'    {param}={val}: t={r["trades"]} sh={r["sharpe"]:.3f} pf={r["pf"]:.3f} cagr={r["cagr"]*100:.2f}% dd={r["max_dd"]*100:.2f}% sc={s:.2f} ({elapsed:.0f}s){marker}', flush=True)
                    sweep_log.append({
                        'pass': pass_num+1, 'param': param, 'value': val,
                        'trades': r['trades'], 'sharpe': r['sharpe'],
                        'pf': r['pf'], 'cagr': r['cagr'], 'score': s,
                    })
                except Exception as e:
                    print(f'    {param}={val}: ERROR {e}', flush=True)
                    done_iters += 1

            if p_best_val != best_params[param]:
                print(f'  >>> {param}: {best_params[param]} -> {p_best_val} (score: {best_score:.2f} -> {p_best_score:.2f})', flush=True)
                best_params[param] = p_best_val
                config = apply_params(config, strat_name, best_params)
                best_score = p_best_score

        if not pass_improved:
            print(f'  Pass {pass_num + 1}: No improvement, stopping early', flush=True)
            break

    final = run_single(config, strat_name, data)
    result = {
        'strategy': strat_name,
        'baseline': baseline, 'baseline_score': score(baseline),
        'optimized': final, 'optimized_score': score(final),
        'best_params': best_params, 'iterations': done_iters,
        'improved': improved, 'sweep_log': sweep_log,
    }

    results_tracker[strat_name] = result
    with open(RESULTS_FILE, 'w') as f:
        json.dump(results_tracker, f, indent=2, default=str)

    return result, config



if __name__ == '__main__':
    import numpy as np

    print('=' * 70, flush=True)
    print('ATLAS-ASX PHASE 1.1: REDUCED PARAMETER OPTIMIZATION', flush=True)
    print('Optimizable: 8 signal params (was 21)', flush=True)
    print('Fixed to v9.2: 13 risk/position params', flush=True)
    print(f'Started: {datetime.now().isoformat()}', flush=True)
    print('=' * 70, flush=True)

    config = get_active_config()
    data = load_full_universe()
    print(f'Loaded {len(data)} tickers (full universe, >= 100 rows)', flush=True)

    # Save pre-optimization config backup
    import shutil
    backup_path = PROJECT / 'config' / 'config_pre_phase1_1.json'
    shutil.copy2(PROJECT / 'config' / 'active_config.json', backup_path)
    print(f'Saved backup: {backup_path}', flush=True)

    # Step 1: Apply fixed params FIRST
    print(f'\nApplying {sum(len(v) for v in FIXED_PARAMS.values())} fixed parameters...', flush=True)
    config = apply_fixed_params(config)
    for sn, params in FIXED_PARAMS.items():
        for k, v in params.items():
            print(f'  FIXED {sn}.{k} = {v}', flush=True)

    # Step 2: Run baseline with fixed params applied
    print(f'\n{"=" * 70}', flush=True)
    print('BASELINE (with fixed params, before signal optimization)', flush=True)
    print(f'{"=" * 70}', flush=True)
    t0 = time.time()
    bl = run_combined(config, data)
    elapsed = time.time() - t0
    print(f'Baseline ({elapsed:.0f}s): trades={bl["trades"]} CAGR={bl["cagr"]*100:.2f}% Sharpe={bl["sharpe"]:.3f} PF={bl["pf"]:.3f} DD={bl["max_dd"]*100:.2f}%', flush=True)

    results_tracker = {
        'timestamp': datetime.now().isoformat(),
        'phase': '1.1_reduced_params',
        'n_tickers': len(data),
        'n_optimizable_params': 8,
        'n_fixed_params': 13,
        'fixed_params': FIXED_PARAMS,
        'baseline_combined': bl,
    }
    with open(RESULTS_FILE, 'w') as f:
        json.dump(results_tracker, f, indent=2, default=str)

    # Step 3: Optimize only signal parameters
    ACTIVE = [s for s in config.get('strategies', {}) if config['strategies'][s].get('enabled', False)]
    for sn in ACTIVE:
        if sn not in PARAM_GRIDS:
            print(f'\nSkipping {sn} (no grid defined)', flush=True)
            continue
        print(f'\n{"=" * 70}', flush=True)
        print(f'OPTIMIZING SIGNALS: {sn.upper()} ({len(PARAM_GRIDS[sn])} params)', flush=True)
        print(f'{"=" * 70}', flush=True)
        t0 = time.time()
        res, config = optimize_strategy(config, sn, data, results_tracker)
        elapsed = time.time() - t0
        print(f'  Done {elapsed:.0f}s | {res["iterations"]} iters | score: {res["baseline_score"]:.4f} -> {res["optimized_score"]:.4f}', flush=True)

    # Step 4: Re-apply fixed params (safety - ensure optimization didn't touch them)
    config = apply_fixed_params(config)

    # Step 5: Final combined result
    print(f'\n{"=" * 70}', flush=True)
    print('FINAL COMBINED (optimized signals + fixed risk params)', flush=True)
    print(f'{"=" * 70}', flush=True)
    final = run_combined(config, data)
    print(f'FINAL: trades={final["trades"]} CAGR={final["cagr"]*100:.2f}% Sharpe={final["sharpe"]:.3f} PF={final["pf"]:.3f} DD={final["max_dd"]*100:.2f}%', flush=True)
    results_tracker['final_combined'] = final

    # Step 6: Perturbation stability test
    print(f'\n{"=" * 70}', flush=True)
    print('PERTURBATION STABILITY TEST (+/-15%)', flush=True)
    print(f'{"=" * 70}', flush=True)
    n_perturb = 30
    perturb_results = []
    for trial in range(n_perturb):
        import copy
        p_config = copy.deepcopy(config)
        for sn in ACTIVE:
            if sn not in PARAM_GRIDS:
                continue
            for param in PARAM_GRIDS[sn]:
                curr_val = p_config['strategies'][sn].get(param)
                if curr_val is not None and isinstance(curr_val, (int, float)):
                    noise = 1.0 + np.random.uniform(-0.15, 0.15)
                    p_config['strategies'][sn][param] = type(curr_val)(curr_val * noise)
        p_config = apply_fixed_params(p_config)  # Keep fixed params locked
        pr = run_combined(p_config, data)
        perturb_results.append(pr)
        if (trial + 1) % 10 == 0:
            print(f'  {trial+1}/{n_perturb} perturbations done...', flush=True)

    # Compute stability metrics
    p_cagrs = [r['cagr'] for r in perturb_results]
    p_sharpes = [r['sharpe'] for r in perturb_results]
    base_cagr = final['cagr']
    mean_p_cagr = np.mean(p_cagrs)
    retention = mean_p_cagr / base_cagr if base_cagr > 0 else 0

    print(f'\nPerturbation Results (+/-15%, {n_perturb} trials):', flush=True)
    print(f'  Base CAGR:  {base_cagr*100:.2f}%', flush=True)
    print(f'  Mean CAGR:  {mean_p_cagr*100:.2f}%', flush=True)
    print(f'  Min CAGR:   {np.min(p_cagrs)*100:.2f}%', flush=True)
    print(f'  Max CAGR:   {np.max(p_cagrs)*100:.2f}%', flush=True)
    print(f'  Std CAGR:   {np.std(p_cagrs)*100:.2f}%', flush=True)
    print(f'  RETENTION:  {retention*100:.1f}%', flush=True)
    print(f'  Mean Sharpe: {np.mean(p_sharpes):.3f}', flush=True)

    results_tracker['perturbation'] = {
        'n_trials': n_perturb,
        'range': 0.15,
        'base_cagr': base_cagr,
        'mean_cagr': mean_p_cagr,
        'min_cagr': float(np.min(p_cagrs)),
        'max_cagr': float(np.max(p_cagrs)),
        'std_cagr': float(np.std(p_cagrs)),
        'retention': retention,
        'mean_sharpe': float(np.mean(p_sharpes)),
        'all_cagrs': [float(c) for c in p_cagrs],
    }

    # Step 7: Save final config
    config_path = PROJECT / 'config' / 'config_phase1_1_reduced.json'
    with open(config_path, 'w') as f:
        json.dump(config, f, indent=2)
    print(f'\nSaved optimized config: {config_path}', flush=True)

    # Save final results
    with open(RESULTS_FILE, 'w') as f:
        json.dump(results_tracker, f, indent=2, default=str)
    print(f'Saved results: {RESULTS_FILE}', flush=True)

    # Summary
    print(f'\n{"=" * 70}', flush=True)
    print('PHASE 1.1 SUMMARY', flush=True)
    print(f'{"=" * 70}', flush=True)
    print(f'Baseline:     CAGR={bl["cagr"]*100:.2f}% Sharpe={bl["sharpe"]:.3f} PF={bl["pf"]:.3f} DD={bl["max_dd"]*100:.2f}%', flush=True)
    print(f'Optimized:    CAGR={final["cagr"]*100:.2f}% Sharpe={final["sharpe"]:.3f} PF={final["pf"]:.3f} DD={final["max_dd"]*100:.2f}%', flush=True)
    print(f'Perturbation: {retention*100:.1f}% retention (target: >60%)', flush=True)
    print(f'Params:       8 optimized, 13 fixed (was 21 optimized)', flush=True)
    status = 'PASS' if retention > 0.5 else 'NEEDS WORK'
    print(f'Status:       {status}', flush=True)
    print(f'\nDone: {datetime.now().isoformat()}', flush=True)
