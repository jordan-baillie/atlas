#!/usr/bin/env python3
"""v9.3 Robust Blend Validation

Runs full-period and OOS backtests for v9.1, v9.3 (loads v9.2 from cache).
Also runs 5 perturbation trials for v9.3.
Saves comprehensive results to backtest/results/v93_robust_validation.json
"""
import sys, json, copy, time, random
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

DATA_DIR = Path('/a0/usr/projects/atlas-asx/data/cache')
RESULTS_DIR = Path('/a0/usr/projects/atlas-asx/backtest/results')
CONFIG_DIR = Path('/a0/usr/projects/atlas-asx/config')

def load_data(min_rows=100):
    data_dict = {}
    for pf in sorted(DATA_DIR.glob('*.parquet')):
        if pf.stem == 'IOZ_AX':
            continue
        ticker = pf.stem.replace('_AX', '.AX')
        df = pd.read_parquet(pf)
        df.columns = [c.lower() for c in df.columns]
        if 'date' in df.columns:
            df['date'] = pd.to_datetime(df['date'])
            df = df.set_index('date')
        df.index = pd.to_datetime(df.index)
        if len(df) >= min_rows:
            data_dict[ticker] = df
    return data_dict

def build_strategies(cfg):
    strategies = []
    if cfg['strategies'].get('mean_reversion', {}).get('enabled', True):
        strategies.append(MeanReversion(cfg))
    if cfg['strategies'].get('trend_following', {}).get('enabled', True):
        strategies.append(TrendFollowing(cfg))
    if cfg['strategies'].get('bb_squeeze', {}).get('enabled', True):
        strategies.append(BBSqueeze(cfg))
    if cfg['strategies'].get('opening_gap', {}).get('enabled', True):
        strategies.append(OpeningGap(cfg))
    return strategies

def run_backtest(cfg, data_dict):
    strategies = build_strategies(cfg)
    engine = BacktestEngine(cfg)
    result = engine.run_walkforward(data_dict, strategies)
    return result.metrics

def normalize_metrics(m):
    """Normalize metrics to consistent format"""
    cagr = m.get('cagr', 0)
    cagr_pct = cagr * 100 if abs(cagr) < 2 else cagr
    dd = m.get('max_drawdown', 0)
    dd_pct = dd * 100 if abs(dd) < 2 else dd
    wr = m.get('win_rate', 0)
    wr_pct = wr * 100 if abs(wr) < 2 else wr
    return {
        'total_trades': m.get('total_trades', 0),
        'total_pnl': round(m.get('total_pnl', 0), 2),
        'avg_trade': round(m.get('avg_trade', 0), 2),
        'win_rate_pct': round(wr_pct, 2),
        'profit_factor': round(m.get('profit_factor', 0), 4),
        'sharpe': round(m.get('sharpe', 0), 4),
        'cagr_pct': round(cagr_pct, 4),
        'max_drawdown_pct': round(dd_pct, 4),
    }

def perturb_config(cfg, seed, perturbation_pct=0.15):
    """Perturb all numeric strategy parameters by ±perturbation_pct"""
    rng = random.Random(seed)
    pcfg = copy.deepcopy(cfg)
    log = {}
    for strat_name, strat_cfg in pcfg['strategies'].items():
        if not isinstance(strat_cfg, dict):
            continue
        strat_log = {}
        for k, v in strat_cfg.items():
            if isinstance(v, (int, float)) and k not in ('enabled',):
                factor = rng.uniform(1 - perturbation_pct, 1 + perturbation_pct)
                if isinstance(v, int):
                    new_val = max(1, round(v * factor))
                else:
                    new_val = round(v * factor, 4)
                strat_cfg[k] = new_val
                strat_log[k] = {'original': v, 'perturbed': new_val, 'factor': round(factor, 4)}
        if strat_log:
            log[strat_name] = strat_log
    return pcfg, log

def split_data_oos(data_dict, split_date='2025-06-01', warmup_date='2025-03-01'):
    """Split data for OOS validation"""
    # In-sample: before split_date
    data_is = {k: v[v.index < split_date] for k, v in data_dict.items()}
    data_is = {k: v for k, v in data_is.items() if len(v) >= 100}
    
    # Out-of-sample: from warmup_date onward (3-month warmup overlap)
    data_oos = {k: v[v.index >= warmup_date] for k, v in data_dict.items()}
    data_oos = {k: v for k, v in data_oos.items() if len(v) >= 60}
    
    return data_is, data_oos

def fmt_pct(v):
    return f"{v:+.2f}%" if v is not None else "N/A"

def fmt_f(v, dec=4):
    return f"{v:+.{dec}f}" if v is not None else "N/A"

def print_comparison_table(results):
    """Print comprehensive comparison table"""
    print("\n" + "="*100)
    print("COMPREHENSIVE COMPARISON: v9.1 vs v9.2 vs v9.3")
    print("="*100)
    
    # Full period
    print("\n--- FULL PERIOD ---")
    header = f"{'Metric':<22} {'v9.1':>14} {'v9.2':>14} {'v9.3':>14} {'v91→v93':>14}"
    print(header)
    print("-" * len(header))
    
    v91f = results.get('v91_full', {})
    v92f = results.get('v92_full', {})
    v93f = results.get('v93_full', {})
    
    metrics_fmt = [
        ('CAGR %', 'cagr_pct', '.2f', '%'),
        ('Sharpe', 'sharpe', '.4f', ''),
        ('Profit Factor', 'profit_factor', '.4f', ''),
        ('Max Drawdown %', 'max_drawdown_pct', '.2f', '%'),
        ('Win Rate %', 'win_rate_pct', '.1f', '%'),
        ('Total Trades', 'total_trades', 'd', ''),
        ('Total PnL $', 'total_pnl', '.2f', ''),
        ('Avg Trade $', 'avg_trade', '.2f', ''),
    ]
    
    for label, key, fmt, suffix in metrics_fmt:
        v1 = v91f.get(key)
        v2 = v92f.get(key)
        v3 = v93f.get(key)
        delta = (v3 - v1) if v1 is not None and v3 is not None else None
        
        v1s = f"{v1:{fmt}}{suffix}" if v1 is not None else "N/A"
        v2s = f"{v2:{fmt}}{suffix}" if v2 is not None else "N/A"
        v3s = f"{v3:{fmt}}{suffix}" if v3 is not None else "N/A"
        ds = f"{delta:+{fmt}}{suffix}" if delta is not None else "N/A"
        print(f"{label:<22} {v1s:>14} {v2s:>14} {v3s:>14} {ds:>14}")
    
    # OOS period
    print("\n--- OUT-OF-SAMPLE (post 2025-06-01) ---")
    header = f"{'Metric':<22} {'v9.1':>14} {'v9.2':>14} {'v9.3':>14} {'v91→v93':>14}"
    print(header)
    print("-" * len(header))
    
    v91o = results.get('v91_oos', {})
    v92o = results.get('v92_oos', {})
    v93o = results.get('v93_oos', {})
    
    for label, key, fmt, suffix in metrics_fmt:
        v1 = v91o.get(key)
        v2 = v92o.get(key)
        v3 = v93o.get(key)
        delta = (v3 - v1) if v1 is not None and v3 is not None else None
        
        v1s = f"{v1:{fmt}}{suffix}" if v1 is not None else "N/A"
        v2s = f"{v2:{fmt}}{suffix}" if v2 is not None else "N/A"
        v3s = f"{v3:{fmt}}{suffix}" if v3 is not None else "N/A"
        ds = f"{delta:+{fmt}}{suffix}" if delta is not None else "N/A"
        print(f"{label:<22} {v1s:>14} {v2s:>14} {v3s:>14} {ds:>14}")
    
    # Degradation analysis
    print("\n--- DEGRADATION: Full → OOS ---")
    header = f"{'Metric':<22} {'v9.1 deg%':>14} {'v9.2 deg%':>14} {'v9.3 deg%':>14}"
    print(header)
    print("-" * len(header))
    
    for label, key, fmt, suffix in [('CAGR %', 'cagr_pct', '.1f', '%'), 
                                     ('Sharpe', 'sharpe', '.1f', '%'),
                                     ('Profit Factor', 'profit_factor', '.1f', '%'),
                                     ('Win Rate %', 'win_rate_pct', '.1f', '%')]:
        degs = []
        for vf, vo in [(v91f, v91o), (v92f, v92o), (v93f, v93o)]:
            full_v = vf.get(key)
            oos_v = vo.get(key)
            if full_v and abs(full_v) > 0.0001:
                deg = ((oos_v - full_v) / abs(full_v)) * 100
                degs.append(f"{deg:+.1f}%")
            else:
                degs.append("N/A")
        print(f"{label:<22} {degs[0]:>14} {degs[1]:>14} {degs[2]:>14}")
    
    # Perturbation results
    if 'v93_perturbation' in results:
        perturb = results['v93_perturbation']
        print("\n--- v9.3 PERTURBATION ROBUSTNESS (5 trials, ±15%) ---")
        print(f"{'Metric':<22} {'Baseline':>12} {'Mean':>12} {'Std':>12} {'Min':>12} {'Max':>12} {'Stability':>12}")
        print("-" * 94)
        
        baseline = v93f
        trials = perturb.get('trials', [])
        
        for label, key in [('CAGR %', 'cagr_pct'), ('Sharpe', 'sharpe'), 
                           ('Profit Factor', 'profit_factor'), ('Max DD %', 'max_drawdown_pct'),
                           ('Total Trades', 'total_trades')]:
            vals = [t.get(key, 0) for t in trials]
            base_v = baseline.get(key, 0)
            mean_v = np.mean(vals) if vals else 0
            std_v = np.std(vals) if vals else 0
            min_v = min(vals) if vals else 0
            max_v = max(vals) if vals else 0
            stability = mean_v / base_v if base_v and abs(base_v) > 0.001 else 0
            print(f"{label:<22} {base_v:>12.2f} {mean_v:>12.2f} {std_v:>12.2f} {min_v:>12.2f} {max_v:>12.2f} {stability:>12.3f}
