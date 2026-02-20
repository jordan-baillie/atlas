#!/usr/bin/env python3
"""Atlas-ASX 5-Strategy Optimization (Coordinate Descent)"""
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
ACTIVE = ['mean_reversion','trend_following','bb_squeeze','opening_gap','dividend_capture']

def load_data(n=25):
    all_data = {}
    for f in (PROJECT / 'data' / 'cache').glob('*.parquet'):
        ticker = f.stem.replace('_', '.')
        df = pd.read_parquet(f)
        df.columns = [c.lower() for c in df.columns]
        if len(df) >= 252:
            all_data[ticker] = df
    vols = {}
    for t, df in all_data.items():
        if 'volume' in df.columns:
            vols[t] = df['volume'].tail(252).mean()
    topN = sorted(vols, key=vols.get, reverse=True)[:n]
    return {t: all_data[t] for t in topN}

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

PARAM_GRIDS = {
    'mean_reversion': {
        'rsi_oversold': [25, 30, 35, 40],
        'zscore_entry': [-2.5, -2.0, -1.5],
        'atr_stop_mult': [2.0, 2.5, 3.0, 3.5],
        'profit_target_atr_mult': [1.0, 1.5, 2.0, 2.5],
        'max_hold_days': [7, 10, 15],
    },
    'trend_following': {
        'fast_ma': [5, 10, 15, 20],
        'slow_ma': [20, 30, 40, 50],
        'pullback_pct': [0.01, 0.02, 0.03, 0.04],
        'atr_stop_mult': [2.5, 3.0, 3.5, 4.0],
        'max_hold_days': [15, 20, 25, 30],
    },
    'bb_squeeze': {
        'bb_std': [2.0, 2.5, 3.0],
        'kc_atr_mult': [1.5, 2.0, 2.5],
        'momentum_period': [20, 30, 40],
        'atr_stop_mult': [1.0, 1.5, 2.0],
        'trailing_stop_atr_mult': [1.5, 2.0, 2.5],
        'max_hold_days': [10, 15, 20],
    },
    'opening_gap': {
        'gap_threshold': [-0.02, -0.015, -0.01],
        'ibs_confirm': [0.2, 0.3, 0.4],
        'rsi14_max': [40, 50, 60],
        'atr_stop_mult': [2.0, 2.5, 3.0],
        'max_hold_days': [5, 7, 10],
    },
    'dividend_capture': {
        'days_before_ex': [5, 7, 10],
        'days_after_ex': [5, 8, 12],
        'min_franking_pct': [50, 75, 100],
        'min_grossed_up_yield': [0.8, 1.2, 1.5],
        'atr_stop_mult': [3.0, 3.5, 4.0],
    },
}

def optimize_strategy(config, strat_name, data):
    grid = PARAM_GRIDS[strat_name]
    best_params = {}
    for p in grid:
        best_params[p] = get_current(config, strat_name, p)
    print(f'  Current: {best_params}')
    baseline = run_single(config, strat_name, data)
    best_score = score(baseline)
    print(f'  Baseline: trades={baseline["trades"]} sharpe={baseline["sharpe"]:.3f} pf={baseline["pf"]:.3f} cagr={baseline["cagr"]*100:.2f}% score={best_score}')
    iters = 0
    improved = False
    for param, values in grid.items():
        print(f'  Sweeping {param}: {values}')
        p_best_val = best_params[param]
        p_best_score = best_score
        for val in values:
            if val == best_params[param]:
                continue
            test_p = {**best_params, param: val}
            test_cfg = apply_params(config, strat_name, test_p)
            try:
                r = run_single(test_cfg, strat_name, data)
                s = score(r)
                iters += 1
                marker = ''
                if s > p_best_score:
                    marker = ' << BETTER'
                    p_best_score = s
                    p_best_val = val
                    improved = True
                print(f'    {param}={val}: t={r["trades"]} sh={r["sharpe"]:.3f} pf={r["pf"]:.3f} cagr={r["cagr"]*100:.2f}% sc={s}{marker}')
            except Exception as e:
                print(f'    {param}={val}: ERROR {e}')
                iters += 1
        if p_best_val != best_params[param]:
            print(f'  >>> {param}: {best_params[param]} -> {p_best_val}')
            best_params[param] = p_best_val
            config = apply_params(config, strat_name, best_params)
            best_score = p_best_score
    final = run_single(config, strat_name, data)
    return {
        'strategy': strat_name,
        'baseline': baseline, 'baseline_score': score(baseline),
        'optimized': final, 'optimized_score': score(final),
        'best_params': best_params, 'iterations': iters,
        'improved': improved,
    }, config

if __name__ == '__main__':
    print('=' * 60)
    print('ATLAS-ASX 5-STRATEGY OPTIMIZATION')
    print('=' * 60)
    config = get_active_config()
    data = load_data(25)
    print(f'Loaded {len(data)} tickers')
    print('\n--- BASELINE COMBINED ---')
    t0 = time.time()
    bl = run_combined(config, data)
    print(f'Baseline ({time.time()-t0:.0f}s): trades={bl["trades"]} CAGR={bl["cagr"]*100:.2f}% Sharpe={bl["sharpe"]:.3f} PF={bl["pf"]:.3f} DD={bl["max_dd"]*100:.2f}%')
    results = {'timestamp': datetime.now().isoformat(), 'n_tickers': len(data), 'baseline_combined': bl}
    for sn in ACTIVE:
        print(f'\n{"="*60}')
        print(f'OPTIMIZING: {sn.upper()}')
        print(f'{"="*60}')
        t0 = time.time()
        res, config = optimize_strategy(config, sn, data)
        print(f'  Done {time.time()-t0:.0f}s | {res["iterations"]} iters | score: {res["baseline_score"]} -> {res["optimized_score"]}')
        results[sn] = res
    print(f'\n{"="*60}')
    print('OPTIMIZED COMBINED')
    print(f'{"="*60}')
    t0 = time.time()
    opt = run_combined(config, data)
    print(f'Optimized ({time.time()-t0:.0f}s): trades={opt["trades"]} CAGR={opt["cagr"]*100:.2f}% Sharpe={opt["sharpe"]:.3f} PF={opt["pf"]:.3f} DD={opt["max_dd"]*100:.2f}%')
    results['optimized_combined'] = opt
    results['improvement'] = {
        'cagr_delta': round((opt['cagr'] - bl['cagr']) * 100, 3),
        'sharpe_delta': round(opt['sharpe'] - bl['sharpe'], 3),
        'dd_delta': round((opt['max_dd'] - bl['max_dd']) * 100, 3),
    }
    print(f'\n=== IMPROVEMENT ===')
    print(f'CAGR:   {bl["cagr"]*100:+.2f}% -> {opt["cagr"]*100:+.2f}% ({results["improvement"]["cagr_delta"]:+.3f}%)')
    print(f'Sharpe: {bl["sharpe"]:.3f} -> {opt["sharpe"]:.3f} ({results["improvement"]["sharpe_delta"]:+.3f})')
    print(f'MaxDD:  {bl["max_dd"]*100:.2f}% -> {opt["max_dd"]*100:.2f}% ({results["improvement"]["dd_delta"]:+.3f}%)')
    # Save optimized config
    config_out = PROJECT / 'config' / 'config_optimized.json'
    with open(config_out, 'w') as f:
        json.dump(config, f, indent=2)
    print(f'\nOptimized config saved: {config_out}')
    results_out = PROJECT / 'backtest' / 'results' / 'optimization_results.json'
    with open(results_out, 'w') as f:
        json.dump(results, f, indent=2, default=str)
    print(f'Results saved: {results_out}')
    # Print param changes
    print(f'\n=== PARAMETER CHANGES ===')
    for sn in ACTIVE:
        r = results[sn]
        orig = get_active_config()['strategies'][sn]
        changed = []
        for p, v in r['best_params'].items():
            ov = orig.get(p)
            if ov != v:
                changed.append(f'{p}: {ov} -> {v}')
        if changed:
            print(f'{sn}: {", ".join(changed)}')
        else:
            print(f'{sn}: no changes')
