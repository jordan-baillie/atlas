#!/usr/bin/env python3
"""Performance Health Check for Atlas-ASX Trading System.

Runs a quick backtest on last 6 months of data, compares to stored baseline,
and flags degradation. Exits 0 (healthy) or 1 (degraded).

Usage: python3 scripts/health_check.py
"""
import sys, json, time
from pathlib import Path
from datetime import datetime, timedelta

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
import pandas as pd
from backtest.engine import BacktestEngine
from strategies.mean_reversion import MeanReversion
from strategies.trend_following import TrendFollowing
from strategies.bb_squeeze import BBSqueeze
from strategies.opening_gap import OpeningGap

DATA_DIR = PROJECT_ROOT / 'data' / 'cache'
CONFIG_DIR = PROJECT_ROOT / 'config'
LOGS_DIR = PROJECT_ROOT / 'logs'

# Baseline metrics from v9.3 robust blend (full-period)
# Update these when a new config is promoted to active
BASELINE = {
    'cagr': 11.15,
    'sharpe': 0.6806,
    'profit_factor': 1.4059,
    'max_drawdown': 7.07,
}

# Degradation thresholds
THRESHOLDS = {
    'cagr_drop_pct': 50,       # Flag if CAGR drops >50% from baseline
    'sharpe_floor': 0.0,       # Flag if Sharpe goes negative
    'pf_floor': 1.0,           # Flag if Profit Factor drops below 1.0
}

def load_data_recent(months=6, min_rows=60):
    """Load only the last ~6 months of data for quick health check."""
    dd = {}
    cutoff = pd.Timestamp.now() - pd.DateOffset(months=months)
    for pf in sorted(DATA_DIR.glob('*.parquet')):
        if pf.stem == 'IOZ_AX': continue
        ticker = pf.stem.replace('_AX', '.AX')
        df = pd.read_parquet(pf)
        df.columns = [c.lower() for c in df.columns]
        if 'date' in df.columns:
            df['date'] = pd.to_datetime(df['date'])
            df = df.set_index('date')
        df.index = pd.to_datetime(df.index)
        df = df[df.index >= cutoff]
        if len(df) >= min_rows:
            dd[ticker] = df
    return dd

def build_strategies(cfg):
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

def norm_metric(val):
    """Normalize fractional metric to percentage if needed."""
    if val is not None and abs(val) < 2:
        return val * 100
    return val

def main():
    t0 = time.time()
    today = datetime.now().strftime('%Y-%m-%d')
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    report_path = LOGS_DIR / f'health_check_{today}.json'

    print(f"=== Atlas-ASX Health Check ({today}) ===")
    print(f"Baseline: CAGR={BASELINE['cagr']:.2f}% Sh={BASELINE['sharpe']:.4f} PF={BASELINE['profit_factor']:.4f}")

    # Load active config
    cfg_path = CONFIG_DIR / 'active_config.json'
    with open(cfg_path) as f:
        cfg = json.load(f)
    print(f"Config: {cfg.get('version', 'unknown')}")

    # Load recent data
    print("Loading last 18 months of data...")
    data = load_data_recent(months=18, min_rows=60)
    print(f"  {len(data)} tickers loaded")

    if len(data) < 10:
        report = {
            'date': today,
            'status': 'ERROR',
            'message': f'Insufficient tickers: {len(data)} < 10',
            'runtime_s': round(time.time() - t0, 1),
        }
        with open(report_path, 'w') as f:
            json.dump(report, f, indent=2)
        print(f"ERROR: {report['message']}")
        sys.exit(1)

    # Run backtest
    print("Running backtest on recent data...")
    engine = BacktestEngine(cfg)
    strategies = build_strategies(cfg)
    result = engine.run_walkforward(data, strategies)
    m = result.metrics
    elapsed = time.time() - t0

    # Normalize metrics
    cagr = norm_metric(m.get('cagr', 0))
    sharpe = m.get('sharpe', 0)
    pf = m.get('profit_factor', 0)
    maxdd = norm_metric(m.get('max_drawdown', 0))
    trades = m.get('total_trades', 0)

    print(f"  CAGR={cagr:.2f}% Sharpe={sharpe:.4f} PF={pf:.4f} MaxDD={maxdd:.2f}% Trades={trades}")

    # Check degradation
    flags = []
    if BASELINE['cagr'] > 0:
        cagr_drop = ((BASELINE['cagr'] - cagr) / BASELINE['cagr']) * 100
        if cagr_drop > THRESHOLDS['cagr_drop_pct']:
            flags.append(f"CAGR degraded {cagr_drop:.1f}% from baseline ({cagr:.2f}% vs {BASELINE['cagr']:.2f}%)")

    if sharpe < THRESHOLDS['sharpe_floor']:
        flags.append(f"Sharpe negative: {sharpe:.4f}")

    if pf < THRESHOLDS['pf_floor']:
        flags.append(f"Profit Factor below 1.0: {pf:.4f}")

    status = 'DEGRADED' if flags else 'HEALTHY'

    report = {
        'date': today,
        'config_version': cfg.get('version', 'unknown'),
        'status': status,
        'metrics': {
            'cagr_pct': round(cagr, 4),
            'sharpe': round(sharpe, 4),
            'profit_factor': round(pf, 4),
            'max_drawdown_pct': round(maxdd, 4),
            'total_trades': trades,
        },
        'baseline': BASELINE,
        'thresholds': THRESHOLDS,
        'flags': flags,
        'tickers_tested': len(data),
        'data_window_months': 18,
        'runtime_s': round(elapsed, 1),
    }

    with open(report_path, 'w') as f:
        json.dump(report, f, indent=2)

    print(f"\nStatus: {status}")
    if flags:
        for flag in flags:
            print(f"  ⚠ {flag}")
    print(f"Report: {report_path}")
    print(f"Runtime: {elapsed:.1f}s")

    sys.exit(0 if status == 'HEALTHY' else 1)

if __name__ == '__main__':
    main()
