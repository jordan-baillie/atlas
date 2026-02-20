#!/usr/bin/env python3
"""Test 3-strategy config (BB Squeeze disabled) + perturbation stability."""
import json, os, sys, time, copy
import numpy as np
import pandas as pd
from pathlib import Path
from collections import Counter
os.chdir('/a0/usr/projects/atlas-asx')
sys.path.insert(0, '.')

# === LOAD DATA ===
print("Loading data...")
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
print("Loaded {} tickers".format(len(data_dict)))

from backtest.engine import BacktestEngine
from strategies.mean_reversion import MeanReversion
from strategies.trend_following import TrendFollowing
from strategies.opening_gap import OpeningGap

# === LOAD CONFIG ===
with open('config/config_phase1_1_reduced.json') as f:
    cfg = json.load(f)

# Disable BB Squeeze
cfg['strategies']['bb_squeeze']['enabled'] = False
print("BB Squeeze disabled.")

# === RUN 3-STRATEGY BACKTEST ===
def run_backtest(config, label=""):
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

print("\n=== 3-STRATEGY BASELINE ===")
t0 = time.time()
baseline = run_backtest(cfg, "3-strat baseline")
print("Completed in {:.1f}s".format(time.time() - t0))
print("Trades: {}".format(baseline['trades']))
print("CAGR: {:.2f}%".format(baseline['cagr']))
print("Sharpe: {:.3f}".format(baseline['sharpe']))
print("PF: {:.3f}".format(baseline['pf']))
print("MaxDD: {:.2f}%".format(baseline['max_dd']))
print("WinRate: {:.1f}%".format(baseline['win_rate']))
print("Final equity: ${:.2f}".format(baseline['final_equity']))

# Per-strategy breakdown
if hasattr(baseline['result'], 'trades') and baseline['result'].trades:
    strat_counts = Counter(t.get('strategy', 'unknown') for t in baseline['result'].trades)
    print("\nPer-Strategy:")
    for strat, count in sorted(strat_counts.items()):
        strat_trades = [t for t in baseline['result'].trades if t.get('strategy') == strat]
        strat_pnl = sum(t.get('pnl', 0) for t in strat_trades)
        strat_wins = sum(1 for t in strat_trades if t.get('pnl', 0) > 0)
        strat_wr = strat_wins / count * 100 if count > 0 else 0
        print("  {}: {} trades, PnL=${:.2f}, WR={:.1f}%".format(strat, count, strat_pnl, strat_wr))

# === PERTURBATION STABILITY TEST ===
print("\n=== PERTURBATION STABILITY TEST (+/-15%) ===")

# Signal parameters to perturb (the 8 from Phase 1.1)
PARAM_MAP = {
    'mean_reversion': ['rsi_period', 'rsi_entry'],
    'trend_following': ['ema_fast', 'ema_slow', 'atr_period'],
    'opening_gap': ['gap_threshold_pct', 'atr_period', 'sma_exit_period'],
}

np.random.seed(42)
N_TRIALS = 20
PERTURB_PCT = 0.15
results = []

for trial in range(N_TRIALS):
    perturbed_cfg = copy.deepcopy(cfg)
    for strat_name, params in PARAM_MAP.items():
        for param in params:
            orig = perturbed_cfg['strategies'][strat_name].get(param)
            if orig is None or orig == 0:
                continue
            # Random perturbation within +/- 15%
            factor = 1.0 + np.random.uniform(-PERTURB_PCT, PERTURB_PCT)
            new_val = orig * factor
            # Keep integers as integers
            if isinstance(orig, int):
                new_val = max(2, int(round(new_val)))
            else:
                new_val = round(new_val, 4)
            perturbed_cfg['strategies'][strat_name][param] = new_val
    
    r = run_backtest(perturbed_cfg, "perturb_{}".format(trial))
    results.append(r)
    print("  Trial {}/{}: CAGR={:.2f}%, Sharpe={:.3f}, PF={:.3f}".format(
        trial+1, N_TRIALS, r['cagr'], r['sharpe'], r['pf']))

cagrs = [r['cagr'] for r in results]
mean_cagr = np.mean(cagrs)
min_cagr = np.min(cagrs)
max_cagr = np.max(cagrs)
std_cagr = np.std(cagrs)
retention = mean_cagr / baseline['cagr'] * 100 if baseline['cagr'] > 0 else 0
worst_retention = min_cagr / baseline['cagr'] * 100 if baseline['cagr'] > 0 else 0

print("\n=== PERTURBATION RESULTS ===")
print("Baseline CAGR: {:.2f}%".format(baseline['cagr']))
print("Mean perturbed CAGR: {:.2f}% (retention: {:.1f}%)".format(mean_cagr, retention))
print("Min perturbed CAGR: {:.2f}% (worst-case retention: {:.1f}%)".format(min_cagr, worst_retention))
print("Max perturbed CAGR: {:.2f}%".format(max_cagr))
print("Std CAGR: {:.2f}%".format(std_cagr))
print("Positive CAGR trials: {}/{}".format(sum(1 for c in cagrs if c > 0), N_TRIALS))

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
        'retention_pct': round(retention, 1),
        'worst_retention_pct': round(worst_retention, 1),
        'positive_trials': sum(1 for c in cagrs if c > 0),
        'trial_cagrs': [round(c, 2) for c in cagrs],
    }
}

with open('backtest/results/phase1_2_3strat_results.json', 'w') as f:
    json.dump(output, f, indent=2)

# Save config
with open('config/config_phase1_2_3strat.json', 'w') as f:
    json.dump(cfg, f, indent=2)

print("\nResults saved to backtest/results/phase1_2_3strat_results.json")
print("Config saved to config/config_phase1_2_3strat.json")
print("Done.")
