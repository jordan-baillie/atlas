#!/usr/bin/env python3
"""Test 3-strategy config with fixed OG exits + perturbation stability.
Uses 7-core parallelism for perturbation trials."""
import json, os, sys, time, copy
import numpy as np
import pandas as pd
from pathlib import Path
from collections import Counter
from concurrent.futures import ProcessPoolExecutor, as_completed
os.chdir('/a0/usr/projects/atlas-asx')
sys.path.insert(0, '.')

def load_data():
    data_dict = {}
    cache = Path('data/cache')
    for pf in sorted(cache.glob('*.parquet')):
        if pf.stem == 'IOZ_AX': continue
        df = pd.read_parquet(pf)
        df.columns = [c.lower() for c in df.columns]
        if 'date' in df.columns:
            df['date'] = pd.to_datetime(df['date'])
            df = df.set_index('date')
        df.index = pd.to_datetime(df.index)
        if len(df) < 100: continue
        ticker = pf.stem.replace('_AX', '.AX')
        data_dict[ticker] = df
    return data_dict

def run_backtest(cfg, data_dict, label=""):
    from backtest.engine import BacktestEngine
    from strategies.mean_reversion import MeanReversion
    from strategies.trend_following import TrendFollowing
    from strategies.opening_gap import OpeningGap
    
    strategies = [MeanReversion(cfg), TrendFollowing(cfg), OpeningGap(cfg)]
    engine = BacktestEngine(cfg)
    result = engine.run_walkforward(data_dict, strategies)
    m = result.metrics
    cagr = m.get('cagr', 0)
    cagr = cagr * 100 if abs(cagr) < 2 else cagr
    max_dd = m.get('max_drawdown', 0)
    max_dd = max_dd * 100 if abs(max_dd) < 1 else max_dd
    wr = m.get('win_rate', 0)
    wr = wr * 100 if abs(wr) < 2 else wr
    return {
        'label': label,
        'trades': m.get('total_trades', 0),
        'cagr': cagr,
        'sharpe': m.get('sharpe', 0),
        'pf': m.get('profit_factor', 0),
        'max_dd': max_dd,
        'win_rate': wr,
        'final_equity': m.get('final_equity', 0),
        'total_pnl': m.get('total_pnl', 0),
        'result': result,
    }

def run_perturb_trial(args):
    """Run single perturbation trial (for parallel execution)."""
    trial_num, base_cfg, perturb_pct, seed = args
    os.chdir('/a0/usr/projects/atlas-asx')
    sys.path.insert(0, '.')
    
    data_dict = load_data()
    rng = np.random.RandomState(seed)
    
    PARAM_MAP = {
        'mean_reversion': ['rsi_period', 'rsi_entry'],
        'trend_following': ['ema_fast', 'ema_slow', 'atr_period'],
        'opening_gap': ['gap_threshold_pct', 'atr_period', 'sma_exit_period'],
    }
    
    perturbed_cfg = copy.deepcopy(base_cfg)
    for strat_name, params in PARAM_MAP.items():
        for param in params:
            orig = perturbed_cfg['strategies'][strat_name].get(param)
            if orig is None or orig == 0: continue
            factor = 1.0 + rng.uniform(-perturb_pct, perturb_pct)
            new_val = orig * factor
            if isinstance(orig, int):
                new_val = max(2, int(round(new_val)))
            else:
                new_val = round(new_val, 4)
            perturbed_cfg['strategies'][strat_name][param] = new_val
    
    r = run_backtest(perturbed_cfg, data_dict, f"perturb_{trial_num}")
    return {
        'trial': trial_num,
        'cagr': r['cagr'],
        'sharpe': r['sharpe'],
        'pf': r['pf'],
        'trades': r['trades'],
        'max_dd': r['max_dd'],
        'win_rate': r['win_rate'],
    }

if __name__ == '__main__':
    data_dict = load_data()
    print(f"Loaded {len(data_dict)} tickers")
    
    with open('config/config_phase1_1_reduced.json') as f:
        cfg = json.load(f)
    
    # Disable BB Squeeze, enable others
    cfg['strategies']['bb_squeeze']['enabled'] = False
    cfg['strategies']['opening_gap']['enabled'] = True
    cfg['strategies']['mean_reversion']['enabled'] = True
    cfg['strategies']['trend_following']['enabled'] = True
    
    # === BASELINE: 3-strategy with fixed OG ===
    print("\n=== 3-STRATEGY BASELINE (OG exits fixed) ===")
    t0 = time.time()
    baseline = run_backtest(cfg, data_dict, "3-strat fixed")
    elapsed = time.time() - t0
    print(f"Completed in {elapsed:.1f}s")
    print(f"Trades: {baseline['trades']}")
    print(f"CAGR: {baseline['cagr']:.2f}%")
    print(f"Sharpe: {baseline['sharpe']:.3f}")
    print(f"PF: {baseline['pf']:.3f}")
    print(f"MaxDD: {baseline['max_dd']:.2f}%")
    print(f"WinRate: {baseline['win_rate']:.1f}%")
    print(f"Final equity: ${baseline['final_equity']:.2f}")
    
    # Per-strategy breakdown
    if hasattr(baseline['result'], 'trades') and baseline['result'].trades:
        strat_counts = Counter(t.get('strategy', 'unknown') for t in baseline['result'].trades)
        print("\nPer-Strategy:")
        for strat, count in sorted(strat_counts.items()):
            strat_trades = [t for t in baseline['result'].trades if t.get('strategy') == strat]
            strat_pnl = sum(t.get('pnl', 0) for t in strat_trades)
            strat_wins = sum(1 for t in strat_trades if t.get('pnl', 0) > 0)
            strat_wr = strat_wins / count * 100 if count > 0 else 0
            avg_hold = np.mean([t.get('hold_days', 0) for t in strat_trades])
            print(f"  {strat}: {count} trades, PnL=${strat_pnl:.2f}, WR={strat_wr:.1f}%, AvgHold={avg_hold:.1f}d")
    
    # === PERTURBATION STABILITY TEST ===
    N_TRIALS = 20
    PERTURB_PCT = 0.15
    print(f"\n=== PERTURBATION STABILITY TEST (+/-{PERTURB_PCT*100:.0f}%, {N_TRIALS} trials, 7 cores) ===")
    
    args_list = [
        (i, cfg, PERTURB_PCT, 42 + i) for i in range(N_TRIALS)
    ]
    
    results = []
    t0 = time.time()
    with ProcessPoolExecutor(max_workers=7) as executor:
        futures = {executor.submit(run_perturb_trial, args): args[0] for args in args_list}
        for future in as_completed(futures):
            trial_num = futures[future]
            try:
                r = future.result()
                results.append(r)
                print(f"  Trial {r['trial']+1}/{N_TRIALS}: CAGR={r['cagr']:.2f}%, Sharpe={r['sharpe']:.3f}, "
                      f"PF={r['pf']:.3f}, Trades={r['trades']}")
            except Exception as e:
                print(f"  Trial {trial_num+1} FAILED: {e}")
    
    elapsed = time.time() - t0
    print(f"\nAll perturbation trials completed in {elapsed:.1f}s")
    
    # Sort results by trial number
    results.sort(key=lambda r: r['trial'])
    cagrs = [r['cagr'] for r in results]
    sharpes = [r['sharpe'] for r in results]
    
    mean_cagr = np.mean(cagrs)
    min_cagr = np.min(cagrs)
    max_cagr = np.max(cagrs)
    std_cagr = np.std(cagrs)
    retention = mean_cagr / baseline['cagr'] * 100 if baseline['cagr'] > 0 else 0
    worst_retention = min_cagr / baseline['cagr'] * 100 if baseline['cagr'] > 0 else 0
    
    print(f"\n=== PERTURBATION RESULTS ===")
    print(f"Baseline CAGR: {baseline['cagr']:.2f}%")
    print(f"Mean perturbed CAGR: {mean_cagr:.2f}% (retention: {retention:.1f}%)")
    print(f"Min perturbed CAGR: {min_cagr:.2f}% (worst-case retention: {worst_retention:.1f}%)")
    print(f"Max perturbed CAGR: {max_cagr:.2f}%")
    print(f"Std CAGR: {std_cagr:.2f}%")
    print(f"Mean Sharpe: {np.mean(sharpes):.3f}")
    print(f"Positive CAGR trials: {sum(1 for c in cagrs if c > 0)}/{N_TRIALS}")
    
    # === COMPARE TO MR+TF ONLY ===
    print(f"\n=== COMPARISON ===")
    print(f"Phase 1.1 (MR+TF only):  CAGR=8.31%, Sharpe=0.515, PF=1.608, DD=7.46%, Trades=196")
    print(f"Phase 1.2 (MR+TF+OG):   CAGR={baseline['cagr']:.2f}%, Sharpe={baseline['sharpe']:.3f}, "
          f"PF={baseline['pf']:.3f}, DD={baseline['max_dd']:.2f}%, Trades={baseline['trades']}")
    print(f"Perturbation retention:  Phase 1.1=76.4% → Phase 1.2={retention:.1f}%")
    
    # === SAVE RESULTS ===
    output = {
        'baseline': {k: v for k, v in baseline.items() if k != 'result'},
        'perturbation': {
            'n_trials': N_TRIALS,
            'perturbation_pct': PERTURB_PCT,
            'mean_cagr': round(mean_cagr, 2),
            'min_cagr': round(min_cagr, 2),
            'max_cagr': round(max_cagr, 2),
            'std_cagr': round(std_cagr, 2),
            'mean_sharpe': round(np.mean(sharpes), 3),
            'retention_pct': round(retention, 1),
            'worst_retention_pct': round(worst_retention, 1),
            'positive_trials': sum(1 for c in cagrs if c > 0),
            'trials': results,
        }
    }
    
    with open('backtest/results/phase1_2_3strat_fixed_results.json', 'w') as f:
        json.dump(output, f, indent=2)
    
    # Save config
    with open('config/config_phase1_2_3strat_fixed.json', 'w') as f:
        json.dump(cfg, f, indent=2)
    
    print(f"\nResults saved to backtest/results/phase1_2_3strat_fixed_results.json")
    print(f"Config saved to config/config_phase1_2_3strat_fixed.json")
    print("Done.")
