#!/usr/bin/env python3
"""Atlas-ASX v9.2 Out-of-Sample Validation Script

Three validation tests:
  1. Time-Period Split (IS vs OOS at 2025-06-01)
  2. Parameter Perturbation / Robustness (10 trials, ±10-20%)
  3. Walk-Forward Window Consistency Analysis

Expected runtime: ~60-90 minutes (each backtest ~280-360s)
"""
import json, sys, time, copy, random, datetime
from pathlib import Path
import numpy as np
import pandas as pd

sys.path.insert(0, '/a0/usr/projects/atlas-asx')

from backtest.engine import BacktestEngine
from strategies.mean_reversion import MeanReversion
from strategies.trend_following import TrendFollowing
from strategies.bb_squeeze import BBSqueeze
from strategies.opening_gap import OpeningGap

# ============================================================
# CONSTANTS
# ============================================================
DATA_DIR = Path('/a0/usr/projects/atlas-asx/data/cache')
CONFIG_PATH = Path('/a0/usr/projects/atlas-asx/config/active_config.json')
OUTPUT_PATH = Path('/a0/usr/projects/atlas-asx/backtest/results/v92_oos_validation.json')
SPLIT_DATE = '2025-06-01'
WARMUP_DATE = '2025-03-01'  # 3-month overlap for indicator warmup
MIN_ROWS = 60
N_PERTURBATION_TRIALS = 10
PERTURB_MIN = 0.8
PERTURB_MAX = 1.2
RANDOM_SEED = 42

# v9.2 optimized params (the params we are validating)
OPTIMIZED_PARAMS = {
    'mean_reversion': {
        'rsi_oversold': 35,
        'zscore_entry': -2.0,
        'atr_stop_mult': 2.5,
        'profit_target_atr_mult': 1.5,
        'max_hold_days': 7,
    },
    'bb_squeeze': {
        'bb_std': 3.0,
        'kc_atr_mult': 2.0,
        'momentum_period': 30,
        'atr_stop_mult': 1.0,
        'trailing_stop_atr_mult': 3.0,
        'max_hold_days': 20,
    },
    'trend_following': {
        'fast_ma': 20,
        'slow_ma': 50,
        'pullback_pct': 0.02,
        'atr_stop_mult': 3.5,
        'max_hold_days': 25,
    },
    'opening_gap': {
        'gap_threshold': -0.01,
        'ibs_confirm': 0.2,
        'rsi14_max': 40,
        'atr_stop_mult': 2.0,
        'max_hold_days': 15,
    },
}


def load_data():
    """Load all parquet data files."""
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
        data_dict[ticker] = df
    return data_dict


def load_config():
    """Load active config."""
    with open(CONFIG_PATH) as f:
        return json.load(f)


def make_strategies(cfg):
    """Instantiate only enabled strategies from config."""
    strategies = []
    strats = cfg.get('strategies', {})
    if strats.get('mean_reversion', {}).get('enabled', True):
        strategies.append(MeanReversion(cfg))
    if strats.get('trend_following', {}).get('enabled', True):
        strategies.append(TrendFollowing(cfg))
    if strats.get('bb_squeeze', {}).get('enabled', True):
        strategies.append(BBSqueeze(cfg))
    if strats.get('opening_gap', {}).get('enabled', True):
        strategies.append(OpeningGap(cfg))
    return strategies


def run_backtest(cfg, data, label=''):
    """Run walk-forward backtest, return result and elapsed time."""
    t0 = time.time()
    strategies = make_strategies(cfg)
    engine = BacktestEngine(cfg)
    result = engine.run_walkforward(data, strategies)
    elapsed = time.time() - t0
    m = result.metrics
    # Normalize CAGR
    cagr = m.get('cagr', 0)
    cagr_pct = cagr * 100 if abs(cagr) < 2 else cagr
    print(f"  [{label}] Trades={m.get('total_trades',0)} "
          f"CAGR={cagr_pct:.2f}% Sharpe={m.get('sharpe',0):.4f} "
          f"PF={m.get('profit_factor',0):.4f} MaxDD={m.get('max_drawdown',0)*100:.2f}% "
          f"WR={m.get('win_rate',0)*100:.1f}% PnL=${m.get('total_pnl',0):.2f} "
          f"({elapsed:.0f}s)")
    return result, elapsed


def extract_metrics(result):
    """Extract key metrics dict from BacktestResult."""
    m = result.metrics
    cagr = m.get('cagr', 0)
    cagr_pct = cagr * 100 if abs(cagr) < 2 else cagr
    return {
        'total_trades': m.get('total_trades', 0),
        'cagr_pct': round(cagr_pct, 4),
        'sharpe': round(m.get('sharpe', 0), 4),
        'profit_factor': round(m.get('profit_factor', 0), 4),
        'max_drawdown_pct': round(m.get('max_drawdown', 0) * 100, 4),
        'win_rate_pct': round(m.get('win_rate', 0) * 100, 2),
        'total_pnl': round(m.get('total_pnl', 0), 2),
        'sortino': round(m.get('sortino', 0), 4),
        'avg_trade': round(m.get('avg_trade', 0), 2),
        'final_equity': round(m.get('final_equity', 0), 2),
    }


def perturb_params(cfg, seed):
    """Create a copy of cfg with randomly perturbed strategy parameters."""
    rng = random.Random(seed)
    cfg_new = copy.deepcopy(cfg)
    perturbed_log = {}

    for strat_name, params in OPTIMIZED_PARAMS.items():
        strat_cfg = cfg_new.get('strategies', {}).get(strat_name, {})
        perturbed_log[strat_name] = {}
        for param_name, orig_val in params.items():
            factor = rng.uniform(PERTURB_MIN, PERTURB_MAX)
            new_val = orig_val * factor
            # Preserve type: round integers
            if isinstance(orig_val, int):
                new_val = max(1, round(new_val))
            else:
                new_val = round(new_val, 4)
            strat_cfg[param_name] = new_val
            perturbed_log[strat_name][param_name] = {
                'original': orig_val,
                'factor': round(factor, 4),
                'perturbed': new_val,
            }
    return cfg_new, perturbed_log


def analyze_walk_forward_windows(result):
    """Analyze per-window metrics for consistency."""
    windows = result.walk_forward_windows
    if not windows:
        return {'error': 'No walk-forward windows found'}

    window_returns = []
    for w in windows:
        eq_start = w.get('equity_start', 0)
        eq_end = w.get('equity_end', 0)
        if eq_start > 0:
            ret = (eq_end - eq_start) / eq_start
        else:
            ret = 0.0
        window_returns.append(ret)

    window_pnls = [w.get('pnl', 0) for w in windows]
    window_trades = [w.get('trades', 0) for w in windows]

    n_positive = sum(1 for r in window_returns if r > 0)
    n_negative = sum(1 for r in window_returns if r <= 0)

    analysis = {
        'n_windows': len(windows),
        'n_positive_windows': n_positive,
        'n_negative_windows': n_negative,
        'win_rate_windows_pct': round(n_positive / len(windows) * 100, 1) if windows else 0,
        'mean_window_return_pct': round(np.mean(window_returns) * 100, 4),
        'std_window_return_pct': round(np.std(window_returns) * 100, 4),
        'min_window_return_pct': round(min(window_returns) * 100, 4),
        'max_window_return_pct': round(max(window_returns) * 100, 4),
        'median_window_return_pct': round(np.median(window_returns) * 100, 4),
        'mean_window_pnl': round(np.mean(window_pnls), 2),
        'std_window_pnl': round(np.std(window_pnls), 2),
        'mean_trades_per_window': round(np.mean(window_trades), 1),
        'total_trades_across_windows': sum(window_trades),
        'per_window_detail': [
            {
                'window': w.get('window', i),
                'test_start': str(w.get('test_start', ''))[:10],
                'test_end': str(w.get('test_end', ''))[:10],
                'trades': w.get('trades', 0),
                'pnl': round(w.get('pnl', 0), 2),
                'return_pct': round(window_returns[i] * 100, 4),
                'equity_start': round(w.get('equity_start', 0), 2),
                'equity_end': round(w.get('equity_end', 0), 2),
            }
            for i, w in enumerate(windows)
        ],
    }
    return analysis


def main():
    overall_start = time.time()
    results = {
        'validation_type': 'v9.2_out_of_sample_validation',
        'timestamp': datetime.datetime.now().isoformat(),
        'config_version': 'v9.2_reoptimized',
        'split_date': SPLIT_DATE,
        'warmup_date': WARMUP_DATE,
        'n_perturbation_trials': N_PERTURBATION_TRIALS,
        'perturbation_range': [PERTURB_MIN, PERTURB_MAX],
    }

    # ----------------------------------------------------------
    # Load data
    # ----------------------------------------------------------
    print("=" * 70)
    print("ATLAS-ASX v9.2 OUT-OF-SAMPLE VALIDATION")
    print("=" * 70)
    print(f"\nLoading data from {DATA_DIR}...")
    data_all = load_data()
    print(f"Loaded {len(data_all)} tickers")

    cfg = load_config()
    print(f"Config: {cfg.get('version', 'unknown')}")
    print(f"Split date: {SPLIT_DATE}")
    print(f"Warmup date for OOS: {WARMUP_DATE}")
    print(f"Perturbation trials: {N_PERTURBATION_TRIALS}
