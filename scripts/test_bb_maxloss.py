#!/usr/bin/env python3
"""Phase 9A: BB Squeeze Stop Tightening + Max Loss Cap Testing

Tests:
  1. Baseline (current config)
  2. BB Squeeze trailing_stop_atr_mult: 2.5, 2.0, 1.5
  3. Portfolio max_loss_per_trade: $45, $40, $35, $30
  4. Combined best BB trailing + best max loss cap

Each run analyzes BB Squeeze trades specifically.
"""
import sys, json, copy, time
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
CONFIG_PATH = Path('/a0/usr/projects/atlas-asx/config/active_config.json')

def load_data(min_rows=100):
    dd = {}
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
        'total_pnl': round(m.get('total_pnl', 0), 2),
        'avg_trade': round(m.get('avg_trade', 0), 2),
        'win_rate_pct': round(wp, 2),
        'profit_factor': round(m.get('profit_factor', 0), 4),
        'sharpe': round(m.get('sharpe', 0), 4),
        'sortino': round(m.get('sortino', 0), 4),
        'cagr_pct': round(cp, 4),
        'max_drawdown_pct': round(dp, 4),
        'final_equity': round(m.get('final_equity', 0), 2),
        'exposure': round(m.get('exposure', 0) * 100 if abs(m.get('exposure', 0)) < 2 else m.get('exposure', 0), 2),
    }

def analyze_bb_trades(trades):
    """Analyze BB Squeeze trades specifically."""
    bb = [t for t in trades if t.get('strategy') == 'bb_squeeze']
    if not bb:
        return {'count': 0}
    
    pnls = [t['pnl'] for t in bb]
    losses = [p for p in pnls if p < 0]
    big_losses = [p for p in pnls if p < -35]
    
    exit_reasons = {}
    for t in bb:
        r = t.get('exit_reason', 'unknown')
        exit_reasons[r] = exit_reasons.get(r, 0) + 1
    
    return {
        'count': len(bb),
        'total_pnl': round(sum(pnls), 2),
        'avg_pnl': round(np.mean(pnls), 2),
        'win_rate': round(len([p for p in pnls if p > 0]) / len(pnls) * 100, 1),
        'losses_count': len(losses),
        'avg_loss': round(np.mean(losses), 2) if losses else 0,
        'worst_loss': round(min(pnls), 2) if losses else 0,
        'big_losses_gt35': len(big_losses),
        'big_losses_total': round(sum(big_losses), 2) if big_losses else 0,
        'exit_reasons': exit_reasons,
    }

def analyze_maxloss_exits(trades):
    """Count max_loss_cap exits across all strategies."""
    mlc = [t for t in trades if t.get('exit_reason') == 'max_loss_cap']
    if not mlc:
        return {'count': 0, 'strategies': {}}
    
    by_strat = {}
    for t in mlc:
        s = t.get('strategy', 'unknown')
        if s not in by_strat:
            by_strat[s] = {'count': 0, 'total_loss': 0}
        by_strat[s]['count'] += 1
        by_strat[s]['total_loss'] = round(by_strat[s]['total_loss'] + t['pnl'], 2)
    
    return {
        'count': len(mlc),
        'total_loss': round(sum(t['pnl'] for t in mlc), 2),
        'avg_loss': round(np.mean([t['pnl'] for t in mlc]), 2),
        'strategies': by_strat,
    }

def run_test(cfg, data, label):
    """Run a single backtest and return results."""
    t0 = time.time()
    print(f"\n{'='*70}")
    print(f"  Running: {label}")
    print(f"{'='*70}")
    sys.stdout.flush()
    
    import importlib
    import strategies.bb_squeeze as bbs_mod
    import strategies.mean_reversion as mr_mod
    import strategies.trend_following as tf_mod
    import strategies.opening_gap as og_mod
    import backtest.engine as eng_mod
    importlib.reload(bbs_mod)
    importlib.reload(mr_mod)
    importlib.reload(tf_mod)
    importlib.reload(og_mod)
    importlib.reload(eng_mod)
    from backtest.engine import BacktestEngine as BE
    from strategies.bb_squeeze import BBSqueeze as BBS
    from strategies.mean_reversion import MeanReversion as MR
    from strategies.trend_following import TrendFollowing as TF
    from strategies.opening_gap import OpeningGap as OG
    
    strats = []
    if cfg['strategies'].get('mean_reversion', {}).get('enabled', True):
        strats.append(MR(cfg))
    if cfg['strategies'].get('trend_following', {}).get('enabled', True):
        strats.append(TF(cfg))
    if cfg['strategies'].get('bb_squeeze', {}).get('enabled', True):
        strats.append(BBS(cfg))
    if cfg['strategies'].get('opening_gap', {}).get('enabled', True):
        strats.append(OG(cfg))
    
    engine = BE(cfg)
    result = engine.run_walkforward(data, strats)
    elapsed = time.time() - t0
    
    metrics = norm_m(result.metrics)
    metrics['runtime_s'] = round(elapsed, 1)
    
    bb_analysis = analyze_bb_trades(result.trades)
    mlc_analysis = analyze_maxloss_exits(result.trades)
    
    # Per-strategy summary
    strat_pnl = {}
    for t in result.trades:
        s = t.get('strategy', 'unknown')
        if s not in strat_pnl:
            strat_pnl[s] = {'count': 0, 'pnl': 0}
        strat_pnl[s]['count'] += 1
        strat_pnl[s]['pnl'] = round(strat_pnl[s]['pnl'] + t['pnl'], 2)
    
    print(f"  Runtime: {elapsed:.0f}s")
    print(f"  CAGR: {metrics['cagr_pct']:.2f}%  Sharpe: {metrics['sharpe']:.4f}  PF: {metrics['profit_factor']:.4f}")
    print(f"  MaxDD: {metrics['max_drawdown_pct']:.2f}%  WR: {metrics['win_rate_pct']:.1f}%  Trades: {metrics['total_trades']}")
    print(f"  Final Equity: ${metrics['final_equity']:.2f}  Total P&L: ${metrics['total_pnl']:.2f}")
    print(f"  Strategy P&L: {json.dumps(strat_pnl)}")
    print(f"  BB Squeeze: {bb_analysis['count']} trades, ${bb_analysis.get('total_pnl', 0):.2f} P&L, {bb_analysis.get('big_losses_gt35', 0)} big losses (>${bb_analysis.get('big_losses_total', 0):.2f})")
    if mlc_analysis['count'] > 0:
        print(f"  Max Loss Cap exits: {mlc_analysis['count']} trades, ${mlc_analysis['total_loss']:.2f} total")
    sys.stdout.flush()
    
    return {
        'label': label,
        'metrics': metrics,
        'bb_analysis': bb_analysis,
        'maxloss_exits': mlc_analysis,
        'strategy_pnl': strat_pnl,
    }

def main():
    print("="*70)
    print("PHASE 9A: BB SQUEEZE STOP TIGHTENING + MAX LOSS CAP TESTING")
    print(f"Started: {datetime.now().isoformat()}")
    print("="*70)
    
    # Load data once
    print("\nLoading data...")
    data = load_data()
    print(f"Loaded {len(data)} tickers")
    
    # Load base config
    with open(CONFIG_PATH) as f:
        base_cfg = json.load(f)
    
    all_results = []
    
    # ===== TEST 1: BASELINE =====
    cfg = copy.deepcopy(base_cfg)
    r = run_test(cfg, data, "BASELINE (current config)")
    r['config_changes'] = 'none'
    all_results.append(r)
    
    # ===== TEST 2: BB Squeeze trailing stop tightening =====
    for trail_mult in [2.5, 2.0, 1.5]:
        cfg = copy.deepcopy(base_cfg)
        cfg['strategies']['bb_squeeze']['trailing_stop_atr_mult'] = trail_mult
        r = run_test(cfg, data, f"BB trail={trail_mult}x ATR")
        r['config_changes'] = f'bb_squeeze.trailing_stop_atr_mult={trail_mult}'
        all_results.append(r)
    
    # ===== TEST 3: Portfolio max loss cap =====
    for max_loss in [45.0, 40.0, 35.0, 30.0]:
        cfg = copy.deepcopy(base_cfg)
        cfg['risk']['max_loss_per_trade'] = max_loss
        r = run_test(cfg, data, f"Max loss cap=${max_loss:.0f}")
        r['config_changes'] = f'risk.max_loss_per_trade={max_loss}'
        all_results.append(r)
    
    # ===== TEST 4: Combined - find best from each category =====
    # Find best BB trail setting (highest sharpe)
    bb_tests = [r for r in all_results if r['label'].startswith('BB trail=')]
    best_bb = max(bb_tests, key=lambda x: x['metrics']['sharpe'])
    best_trail = float(best_bb['config_changes'].split('=')[1])
    
    # Find best max loss cap (highest sharpe)
    mlc_tests = [r for r in all_results if r['label'].startswith('Max loss cap')]
    best_mlc = max(mlc_tests, key=lambda x: x['metrics']['sharpe'])
    best_cap = float(best_mlc['config_changes'].split('=')[1])
    
    print(f"\n>>> Best BB trail: {best_trail}x ATR (Sharpe={best_bb['metrics']['sharpe']:.4f})")
    print(f">>> Best max loss cap: ${best_cap:.0f} (Sharpe={best_mlc['metrics']['sharpe']:.4f}
