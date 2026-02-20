#!/usr/bin/env python3
"""Test 3-strategy config with PARALLEL perturbation stability using 8 cores."""
import json, os, sys, time, copy
import numpy as np
import pandas as pd
from pathlib import Path
from collections import Counter
from concurrent.futures import ProcessPoolExecutor, as_completed
import multiprocessing as mp

os.chdir('/a0/usr/projects/atlas-asx')
sys.path.insert(0, '.')

N_WORKERS = 7  # Leave 1 core for system
N_TRIALS = 20
PERTURB_PCT = 0.15

# Signal parameters to perturb (the 8 from Phase 1.1)
PARAM_MAP = {
    'mean_reversion': ['rsi_period', 'rsi_entry'],
    'trend_following': ['ema_fast', 'ema_slow', 'atr_period'],
    'opening_gap': ['gap_threshold_pct', 'atr_period', 'sma_exit_period'],
}

def load_data():
    """Load all ticker data from cache."""
    data_dict = {}
    cache = Path('data/cache')
    for pf in sorted(cache.glob('*.parquet')):
        if pf.stem == 'IOZ_AX':
            continue
        df = pd.read_parquet(pf)
        df.columns = [c.lower() for c in df.columns]
        if 'date' in df.columns:
            df['date'] = pd.to_datetime(df['date'])
            df = df.set_index('date')
        df.index = pd.to_datetime(df.index)
        if len(df) < 100:
            continue
        ticker = pf.stem.replace('_AX', '.AX')
        data_dict[ticker] = df
    return data_dict

def run_single_backtest(args):
    """Run a single backtest - designed for multiprocessing."""
    config, trial_id = args
    os.chdir('/a0/usr/projects/atlas-asx')
    sys.path.insert(0, '.')
    
    from backtest.engine import BacktestEngine
    from strategies.mean_reversion import MeanReversion
    from strategies.trend_following import TrendFollowing
    from strategies.opening_gap import OpeningGap
    
    data_dict = load_data()
    strategies = [MeanReversion(config), TrendFollowing(config), OpeningGap(config)]
    engine = BacktestEngine(config)
    result = engine.run_walkforward(data_dict, strategies)
    m = result.metrics
    
    cagr = m.get('cagr', 0)
    cagr = cagr * 100 if abs(cagr) < 2 else cagr
    max_dd = m.get('max_drawdown', 0)
    max_dd = max_dd * 100 if abs(max_dd) < 1 else max_dd
    wr = m.get('win_rate', 0)
    wr = wr * 100 if abs(wr) < 2 else wr
    
    return {
        'trial_id': trial_id,
        'trades': m.get('total_trades', 0),
        'cagr': cagr,
        'sharpe': m.get('sharpe', 0),
        'pf': m.get('profit_factor', 0),
        'max_dd': max_dd,
        'win_rate': wr,
        'final_equity': m.get('final_equity', 0),
    }

def make_perturbed_config(base_cfg, seed):
    """Create a perturbed config with given random seed."""
    rng = np.random.RandomState(seed)
    cfg = copy.deepcopy(base_cfg)
    for strat_name, params in PARAM_MAP.items():
        for param in params:
            orig = cfg['strategies'][strat_name].get(param)
            if orig is None or orig == 0:
                continue
            factor = 1.0 + rng.uniform(-PERTURB_PCT, PERTURB_PCT)
            new_val = orig * factor
            if isinstance(orig, int):
                new_val = max(2, int(round(new_val)))
            else:
                new_val = round(new_val, 4)
            cfg['strategies'][strat_name][param] = new_val
    return cfg

if __name__ == '__main__':
    mp.set_start_method('spawn', force=True)
    
    print("Loading config...")
    with open('config/config_phase1_1_reduced.json') as f:
        cfg = json.load(f)
    cfg['strategies']['bb_squeeze']['enabled'] = False
    print("BB Squeeze disabled. Using {} workers.".format(N_WORKERS))
    
    # === BASELINE ===
    print("\n=== 3-STRATEGY BASELINE ===")
    t0 = time.time()
    baseline = run_single_backtest((cfg, 'baseline'))
    elapsed = time.time() - t0
    print("Completed in {:.1f}s".format(elapsed))
    print("Trades: {}".format(baseline['trades']))
    print("CAGR: {:.2f}%".format(baseline['cagr']))
    print("Sharpe: {:.3f}".format(baseline['sharpe']))
    print("PF: {:.3f}".format(baseline['pf']))
    print("MaxDD: {:.2f}%".format(baseline['max_dd']))
    print("WinRate: {:.1f}%".format(baseline['win_rate']))
    print("Final equity: ${:.2f}".format(baseline['final_equity']))
    
    # === PARALLEL PERTURBATION ===
    print("\n=== PERTURBATION STABILITY TEST (+/-15%) - {} trials, {} workers ===".format(N_TRIALS, N_WORKERS))
    
    # Prepare all perturbed configs
    tasks = []
    for i in range(N_TRIALS):
        pcfg = make_perturbed_config(cfg, seed=42+i)
        tasks.append((pcfg, i))
    
    results = []
    t0 = time.time()
    with ProcessPoolExecutor(max_workers=N_WORKERS) as executor:
        futures = {executor.submit(run_single_backtest, task): task[1] for task in tasks}
        for future in as_completed(futures):
            trial_id = futures[future]
            try:
                r = future.result()
                results.append(r)
                print("  Trial {}/{}: CAGR={:.2f}%, Sharpe={:.3f}, PF={:.3f}, Trades={}".format(
                    len(results), N_TRIALS, r['cagr'], r['sharpe'], r['pf'], r['trades']))
            except Exception as e:
                print("  Trial {} FAILED: {}".format(trial_id, e))
    
    elapsed = time.time() - t0
    print("\nAll {} trials completed in {:.1f}s (avg {:.1f}s/trial, {:.1f}x speedup vs sequential)".format(
        len(results), elapsed, elapsed/len(results), (elapsed/len(results)*N_WORKERS)/max(1,elapsed)*len(results)))
    
    # === ANALYZE RESULTS ===
    results.sort(key=lambda x: x['trial_id'])
    cagrs = [r['cagr'] for r in results]
    sharpes = [r['sharpe'] for r in results]
    trades_list = [r['trades'] for r in results]
    
    mean_cagr = np.mean(cagrs)
    min_cagr = np.min(cagrs)
    max_cagr = np.max(cagrs)
    std_cagr = np.std(cagrs)
    retention = mean_cagr / baseline['cagr'] * 100 if baseline['cagr'] > 0 else 0
    worst_retention = min_cagr / baseline['cagr'] * 100 if baseline['cagr'] > 0 else 0
    
    print("\n" + "="*60)
    print("PERTURBATION RESULTS SUMMARY")
    print("="*60)
    print("Baseline CAGR: {:.2f}%".format(baseline['cagr']))
    print("Mean perturbed CAGR: {:.2f}% (retention: {:.1f}%)".format(mean_cagr, retention))
    print("Min perturbed CAGR: {:.2f}% (worst-case retention: {:.1f}%)".format(min_cagr, worst_retention))
    print("Max perturbed CAGR: {:.2f}%".format(max_cagr))
    print("Std CAGR: {:.2f}%".format(std_cagr))
    print("Positive CAGR trials: {}/{}".format(sum(1 for c in cagrs if c > 0), N_TRIALS))
    print("Mean Sharpe: {:.3f}".format(np.mean(sharpes)))
    print("Mean trades: {:.1f}".format(np.mean(trades_list)))
    
    # Compare to v9.2 baseline
    print("\nCOMPARISON TO ORIGINAL v9.2:")
    print("  v9.2:     CAGR=11.15%, perturbation retention=24%")
    print("  Phase1.1: CAGR=8.31%,  perturbation retention=76.4% (MR+TF only)")
    print("  Phase1.2: CAGR={:.2f}%, perturbation retention={:.1f}% (MR+TF+OG)".format(
        baseline['cagr'], retention))
    
    # === SAVE RESULTS ===
    os.makedirs('backtest/results', exist_ok=True)
    output = {
        'baseline': baseline,
        'perturbation': {
            'n_trials': N_TRIALS,
            'n_workers': N_WORKERS,
            'perturbation_pct': PERTURB_PCT,
            'elapsed_seconds': round(elapsed, 1),
            'mean_cagr': round(mean_cagr, 2),
            'min_cagr': round(min_cagr, 2),
            'max_cagr': round(max_cagr, 2),
            'std_cagr': round(std_cagr, 2),
            'retention_pct': round(retention, 1),
            'worst_retention_pct': round(worst_retention, 1),
            'positive_trials': sum(1 for c in cagrs if c > 0),
            'mean_sharpe': round(np.mean(sharpes), 3),
            'mean_trades': round(np.mean(trades_list), 1),
            'trial_results': results,
        }
    }
    
    with open('backtest/results/phase1_2_3strat_results.json', 'w') as f:
        json.dump(output, f, indent=2)
    
    with open('config/config_phase1_2_3strat.json', 'w') as f:
        json.dump(cfg, f, indent=2)
    
    print("\nResults saved to backtest/results/phase1_2_3strat_results.json")
    print("Config saved to config/config_phase1_2_3strat.json")
    print("Done.")
