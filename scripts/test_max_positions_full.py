#!/usr/bin/env python3
"""
Max Positions Full Universe Test
Tests max_positions = 5, 6, 8, 10, 12, 15 on full universe with all 5 strategies.
"""
import sys, os, json, time, copy
from pathlib import Path
sys.path.insert(0, '/a0/usr/projects/atlas-asx')
os.chdir('/a0/usr/projects/atlas-asx')

import pandas as pd
from backtest.engine import BacktestEngine
from strategies.mean_reversion import MeanReversion
from strategies.trend_following import TrendFollowing
from strategies.bb_squeeze import BBSqueeze
from strategies.opening_gap import OpeningGap
from strategies.dividend_capture import DividendCapture

# Load base config
with open('config/active_config.json') as f:
    base_config = json.load(f)
print(f"Config: {base_config.get('version','?')}")
print(f"Base max_positions: {base_config['risk']['max_open_positions']}")

# Load full universe
cache_dir = Path('data/cache')
parquet_files = sorted([f for f in cache_dir.glob('*.parquet') if f.stem != 'IOZ_AX'])
print(f"Loading {len(parquet_files)} tickers...", flush=True)

data_dict = {}
for pf in parquet_files:
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
        print(f"  Skip {ticker}: {e}")

print(f"Loaded {len(data_dict)} valid tickers", flush=True)

# Strategy factory
def make_strategies(cfg):
    return [
        MeanReversion(cfg),
        TrendFollowing(cfg),
        BBSqueeze(cfg),
        OpeningGap(cfg),
        DividendCapture(cfg),
    ]

# Test configurations
max_positions_to_test = [5, 6, 8, 10, 12, 15]
results = []
output_file = 'backtest/results/max_positions_full_universe.json'

print(f"\n{'='*65}")
print(f"FULL UNIVERSE MAX POSITIONS TEST ({len(data_dict)} tickers, 5 strategies)")
print(f"{'='*65}")
print(f"Testing: {max_positions_to_test}\n", flush=True)

for max_pos in max_positions_to_test:
    cfg = copy.deepcopy(base_config)
    cfg['risk']['max_open_positions'] = max_pos

    strategies = make_strategies(cfg)
    engine = BacktestEngine(cfg)

    print(f"Running max_positions={max_pos}...", flush=True)
    t0 = time.time()

    try:
        result = engine.run_walkforward(data_dict, strategies)
        elapsed = time.time() - t0
        m = result.metrics

        # cagr/win_rate/max_drawdown/exposure stored as fractions in some versions
        cagr_val   = m.get('cagr', 0)
        wr_val     = m.get('win_rate', 0)
        dd_val     = m.get('max_drawdown', 0)
        exp_val    = m.get('exposure', 0)
        # Convert fractions to percent if < 2 (heuristic)
        if abs(cagr_val) < 2:  cagr_val *= 100
        if wr_val <= 1:        wr_val   *= 100
        if dd_val <= 1:        dd_val   *= 100
        if exp_val <= 1:       exp_val  *= 100

        row = {
            'max_positions': max_pos,
            'time_s': round(elapsed, 1),
            'total_trades': m.get('total_trades', 0),
            'win_rate': round(wr_val, 2),
            'cagr': round(cagr_val, 4),
            'max_dd': round(dd_val, 4),
            'profit_factor': round(m.get('profit_factor', 0), 4),
            'total_pnl': round(m.get('total_pnl', 0), 2),
            'sharpe': round(m.get('sharpe', 0), 4),
            'exposure': round(exp_val, 2),
            'avg_trade': round(m.get('avg_trade', 0), 2),
            'final_equity': round(m.get('final_equity', 0), 2),
        }

        # Per-strategy breakdown
        strat_details = {}
        if hasattr(result, 'trades'):
            for trade in result.trades:
                s = trade.get('strategy', 'unknown')
                if s not in strat_details:
                    strat_details[s] = {'total': 0, 'wins': 0, 'pnl': 0.0}
                strat_details[s]['total'] += 1
                if trade.get('pnl', 0) > 0:
                    strat_details[s]['wins'] += 1
                strat_details[s]['pnl'] += trade.get('pnl', 0)

        row['strategies'] = {
            s: {
                'trades': v['total'],
                'win_rate': round(v['wins']/v['total']*100, 1) if v['total'] > 0 else 0,
                'pnl': round(v['pnl'], 2)
            }
            for s, v in strat_details.items()
        }

        results.append(row)
        with open(output_file, 'w') as f:
            json.dump(results, f, indent=2)

        print(f"  Done in {elapsed:.0f}s | trades={row['total_trades']} "
              f"CAGR={row['cagr']:.2f}% Sharpe={row['sharpe']:.3f} "
              f"PF={row['profit_factor']:.3f} MaxDD={row['max_dd']:.2f}% "
              f"PnL=${row['total_pnl']:.0f}", flush=True)

    except Exception as e:
        elapsed = time.time() - t0
        print(f"  FAILED after {elapsed:.0f}s: {e}")
        import traceback; traceback.print_exc()

# Summary table
print(f"\n{'='*70}")
print("RESULTS SUMMARY")
print(f"{'='*70}")
print(f"{'MaxPos':>6} {'Trades':>7} {'WR%':>6} {'CAGR%':>8} {'Sharpe':>8} {'PF':>6} {'MaxDD%':>8} {'PnL$':>8} {'FinalEq$':>10}")
print('-'*70)
baseline = next((r for r in results if r['max_positions'] == 5), None)
for r in results:
    mark = ' <-- baseline' if r['max_positions'] == 5 else ''
    print(f"{r['max_positions']:>6} {r['total_trades']:>7} {r['win_rate']:>6.1f} "
          f"{r['cagr']:>8.2f} {r['sharpe']:>8.3f} {r['profit_factor']:>6.3f} "
          f"{r['max_dd']:>8.2f} {r['total_pnl']:>8.0f} {r['final_equity']:>10.0f}{mark}")

if baseline and len(results) > 1:
    print(f"\n{'='*70}")
    print("DELTA vs BASELINE (max_positions=5)")
    print(f"{'='*70}")
    print(f"{'MaxPos':>6} {'ΔTrades':>8} {'ΔCAGR%':>8} {'ΔSharpe':>9} {'ΔPF':>7} {'ΔMaxDD%':>9} {'ΔPnL$':>8}")
    print('-'*70)
    for r in results:
        if r['max_positions'] == 5:
            continue
        print(f"{r['max_positions']:>6} "
              f"{r['total_trades']-baseline['total_trades']:>+8} "
              f"{r['cagr']-baseline['cagr']:>+8.2f} "
              f"{r['sharpe']-baseline['sharpe']:>+9.3f} "
              f"{r['profit_factor']-baseline['profit_factor']:>+7.3f} "
              f"{r['max_dd']-baseline['max_dd']:>+9.2f} "
              f"{r['total_pnl']-baseline['total_pnl']:>+8.0f}")

print(f"\nResults saved to: {output_file}")
print("DONE")
