#!/usr/bin/env python3
"""Atlas Full-Universe Re-Optimization (Coordinate Descent)

Key change from original: optimizes on ALL 185 tickers (>=100 rows)
instead of just top 25 by volume.
"""
import sys, json, os, copy, time, logging, argparse, shutil
from pathlib import Path
from datetime import datetime

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))
os.chdir(str(PROJECT))

import pandas as pd
import numpy as np
from utils.logging_config import setup_logging
setup_logging("reoptimize_full", level=logging.WARNING)

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

RESULTS_FILE = PROJECT / 'backtest' / 'results' / 'reoptimization_full_universe.json'


def parse_args():
    parser = argparse.ArgumentParser(
        description="Run full-universe reoptimization and write a staged candidate config by default."
    )
    parser.add_argument(
        '--candidate-path',
        type=str,
        default=None,
        help='Path to write optimized candidate config JSON (default: config/config_candidate_reoptimized_<timestamp>.json)',
    )
    parser.add_argument(
        '--results-path',
        type=str,
        default=None,
        help='Path to write reoptimization results JSON (default: backtest/results/reoptimization_full_universe.json)',
    )
    parser.add_argument(
        '--backup-path',
        type=str,
        default=None,
        help='Optional explicit backup path for the current active config.',
    )
    parser.add_argument(
        '--promote-active',
        action='store_true',
        help='Also overwrite config/active/asx.json with the optimized config (default: false).',
    )
    return parser.parse_args()


def resolve_output_path(path_str, default_path):
    if not path_str:
        return Path(default_path)
    p = Path(path_str)
    if not p.is_absolute():
        p = PROJECT / p
    return p


def default_candidate_path():
    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    return PROJECT / 'config' / f'config_candidate_reoptimized_{ts}.json'


def default_backup_path():
    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    return PROJECT / 'config' / 'versions' / f'active_config_pre_reopt_{ts}.json'

def load_full_universe():
    cache_dir = PROJECT / 'data' / 'cache' / 'asx'
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
        except Exception as e:
            logging.getLogger(__name__).debug("Skip %s: %s", ticker, e)
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

PARAM_GRIDS = {
    'mean_reversion': {
        'rsi_oversold': [20, 25, 30, 35, 40, 45],
        'zscore_entry': [-3.0, -2.5, -2.0, -1.5, -1.0],
        'atr_stop_mult': [2.0, 2.5, 3.0, 3.5, 4.0],
        'profit_target_atr_mult': [1.0, 1.5, 2.0, 2.5, 3.0],
        'max_hold_days': [5, 7, 10, 15, 20],
    },
    'bb_squeeze': {
        'bb_std': [1.5, 2.0, 2.5, 3.0],
        'kc_atr_mult': [1.0, 1.5, 2.0, 2.5],
        'momentum_period': [10, 15, 20, 30, 40],
        'atr_stop_mult': [1.0, 1.5, 2.0, 2.5, 3.0],
        'trailing_stop_atr_mult': [1.5, 2.0, 2.5, 3.0],
        'max_hold_days': [5, 10, 15, 20, 25],
    },
    'trend_following': {
        'fast_ma': [5, 10, 15, 20],
        'slow_ma': [20, 30, 40, 50, 60],
        'pullback_pct': [0.01, 0.02, 0.03, 0.04, 0.05],
        'atr_stop_mult': [2.0, 2.5, 3.0, 3.5, 4.0],
        'max_hold_days': [10, 15, 20, 25, 30],
    },
    'opening_gap': {
        'gap_threshold': [-0.03, -0.025, -0.02, -0.015, -0.01],
        'ibs_confirm': [0.15, 0.2, 0.3, 0.4, 0.5],
        'rsi14_max': [30, 40, 50, 60, 70],
        'atr_stop_mult': [1.5, 2.0, 2.5, 3.0, 3.5],
        'max_hold_days': [3, 5, 7, 10, 15],
    },
    'dividend_capture': {
        'days_before_ex': [3, 5, 7, 10],
        'days_after_ex': [3, 5, 8, 12],
        'min_franking_pct': [0, 50, 75, 100],
        'min_grossed_up_yield': [0.5, 0.8, 1.0, 1.2, 1.5],
        'atr_stop_mult': [2.5, 3.0, 3.5, 4.0, 5.0],
    },
}

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
    args = parse_args()
    RESULTS_FILE = resolve_output_path(args.results_path, RESULTS_FILE)
    candidate_config_path = resolve_output_path(args.candidate_path, default_candidate_path())
    backup_config_path = resolve_output_path(args.backup_path, default_backup_path())

    print('=' * 70, flush=True)
    print('ATLAS-ASX FULL-UNIVERSE RE-OPTIMIZATION', flush=True)
    print(f'Started: {datetime.now().isoformat()}', flush=True)
    print(f'Results file: {RESULTS_FILE}', flush=True)
    print(f'Candidate config target: {candidate_config_path}', flush=True)
    print(f'Promote active config: {args.promote_active}', flush=True)
    print('=' * 70, flush=True)

    config = get_active_config()
    data = load_full_universe()
    print(f'Loaded {len(data)} tickers (full universe, >= 100 rows)', flush=True)

    # Save pre-optimization config backup
    backup_config_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(PROJECT / 'config' / 'active' / 'asx.json', backup_config_path)
    print(f'Saved pre-optimization config backup to {backup_config_path}', flush=True)

    # Run baseline combined
    print(f'\n{"="*70}', flush=True)
    print('BASELINE COMBINED (all strategies)', flush=True)
    print(f'{"="*70}', flush=True)
    t0 = time.time()
    bl = run_combined(config, data)
    print(f'Baseline ({time.time()-t0:.0f}s): trades={bl["trades"]} CAGR={bl["cagr"]*100:.2f}% Sharpe={bl["sharpe"]:.3f} PF={bl["pf"]:.3f} DD={bl["max_dd"]*100:.2f}%', flush=True)

    results_tracker = {
        'timestamp': datetime.now().isoformat(),
        'n_tickers': len(data),
        'baseline_combined': bl,
        'results_path': str(RESULTS_FILE),
        'backup_config_path': str(backup_config_path),
        'active_config_path': str(PROJECT / 'config' / 'active' / 'asx.json'),
        'candidate_config_path': str(candidate_config_path),
        'active_config_overwritten': False,
    }
    RESULTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(RESULTS_FILE, 'w') as f:
        json.dump(results_tracker, f, indent=2, default=str)

    # Optimize each strategy on full universe
    for sn in ACTIVE:
        print(f'\n{"="*70}', flush=True)
        print(f'OPTIMIZING: {sn.upper()}', flush=True)
        print(f'{"="*70}', flush=True)
        t0 = time.time()
        res, config = optimize_strategy(config, sn, data, results_tracker)
        elapsed = time.time() - t0
        print(
            f'  Done {elapsed:.0f}s | {res["iterations"]} iters | '
            f'score: {res["baseline_score"]} -> {res["optimized_score"]}',
            flush=True,
        )

    # Final combined run with optimized parameters
    print(f'\n{"="*70}', flush=True)
    print('FINAL COMBINED (optimized config)', flush=True)
    print(f'{"="*70}', flush=True)
    t0 = time.time()
    final_combined = run_combined(config, data)
    elapsed = time.time() - t0
    print(
        f'Final ({elapsed:.0f}s): trades={final_combined["trades"]} '
        f'CAGR={final_combined["cagr"]*100:.2f}% '
        f'Sharpe={final_combined["sharpe"]:.3f} PF={final_combined["pf"]:.3f} '
        f'DD={final_combined["max_dd"]*100:.2f}%',
        flush=True
    )

    # Persist artifacts and write optimized candidate config (and optionally promote active)
    results_tracker['final_combined'] = final_combined
    results_tracker['candidate_config_path'] = str(candidate_config_path)
    results_tracker['active_config_overwritten'] = bool(args.promote_active)

    candidate_config_path.parent.mkdir(parents=True, exist_ok=True)
    with open(candidate_config_path, 'w') as f:
        json.dump(config, f, indent=2)
    print(f'Saved optimized candidate config to {candidate_config_path}', flush=True)

    if args.promote_active:
        active_config_path = PROJECT / 'config' / 'active' / 'asx.json'
        with open(active_config_path, 'w') as f:
            json.dump(config, f, indent=2)
        print(f'Promoted optimized config to {active_config_path}', flush=True)
    else:
        print('Active config not modified (staged candidate only)', flush=True)

    results_tracker['finished_at'] = datetime.now().isoformat()
    with open(RESULTS_FILE, 'w') as f:
        json.dump(results_tracker, f, indent=2, default=str)

    print('\nReoptimization complete.', flush=True)
