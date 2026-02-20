
import json, sys, logging
from pathlib import Path
from datetime import datetime
logging.basicConfig(level=logging.WARNING)
sys.path.insert(0, '/a0/usr/projects/atlas-asx')

import pandas as pd
import numpy as np
from utils.config import get_active_config
from strategies.trend_following import TrendFollowing
from strategies.mean_reversion import MeanReversion
from backtest.engine import BacktestEngine

# Load config and data
config = get_active_config()
cache_dir = Path('/a0/usr/projects/atlas-asx/data/cache')
with open('/a0/usr/projects/atlas-asx/data/processed/universe.json') as f:
    tickers = json.load(f).get('tickers', [])
data = {}
for t in tickers:
    p = cache_dir / (t.replace('.', '_') + '.parquet')
    if p.exists():
        data[t] = pd.read_parquet(p)
print(f'Loaded {len(data)} tickers', flush=True)

# Run full backtest
strategies = [TrendFollowing(config), MeanReversion(config)]
engine = BacktestEngine(config)
print('Running full backtest with v6.1d config...', flush=True)
result = engine.run_walkforward(data, strategies)
m = result.metrics
print(f'Full: CAGR={m["cagr"]*100:.2f}% DD={m["max_drawdown"]*100:.2f}% WR={m["win_rate"]*100:.1f}% PF={m["profit_factor"]:.2f} T={m["total_trades"]}', flush=True)

# Split trades at 70/30 boundary
split_date = pd.Timestamp('2025-03-22')
is_trades = [t for t in result.trades if pd.Timestamp(t['entry_date']) < split_date]
oos_trades = [t for t in result.trades if pd.Timestamp(t['entry_date']) >= split_date]

def calc_metrics(trades, label):
    if not trades:
        print(f'{label}: No trades')
        return {}
    wins = [t for t in trades if t.get('pnl', 0) > 0]
    losses = [t for t in trades if t.get('pnl', 0) <= 0]
    total_pnl = sum(t.get('pnl', 0) for t in trades)
    gross_profit = sum(t.get('pnl', 0) for t in wins)
    gross_loss = abs(sum(t.get('pnl', 0) for t in losses))
    wr = len(wins) / len(trades) * 100 if trades else 0
    pf = gross_profit / gross_loss if gross_loss > 0 else float('inf')
    avg_win = gross_profit / len(wins) if wins else 0
    avg_loss = gross_loss / len(losses) if losses else 0

    tf = [t for t in trades if t.get('strategy') == 'trend_following']
    mr = [t for t in trades if t.get('strategy') == 'mean_reversion']
    tf_wr = len([t for t in tf if t.get('pnl',0)>0])/len(tf)*100 if tf else 0
    mr_wr = len([t for t in mr if t.get('pnl',0)>0])/len(mr)*100 if mr else 0

    boosted = [t for t in trades if t.get('features',{}).get('breadth_confidence_adj',0) > 0]
    penalized = [t for t in trades if t.get('features',{}).get('breadth_confidence_adj',0) < 0]
    neutral = [t for t in trades if t.get('features',{}).get('breadth_confidence_adj',0) == 0]

    print(f'\n=== {label} ===')
    print(f'Trades: {len(trades)} (TF={len(tf)}, MR={len(mr)})')
    print(f'Win Rate: {wr:.1f}% (TF={tf_wr:.1f}%, MR={mr_wr:.1f}%)')
    print(f'Total PnL: ${total_pnl:.2f}')
    print(f'Profit Factor: {pf:.2f}')
    print(f'Avg Win: ${avg_win:.2f} | Avg Loss: ${avg_loss:.2f}')
    print(f'Breadth: B={len(boosted)}(${sum(t.get("pnl",0) for t in boosted):+.2f}) '
          f'P={len(penalized)}(${sum(t.get("pnl",0) for t in penalized):+.2f}) '
          f'N={len(neutral)}(${sum(t.get("pnl",0) for t in neutral):+.2f})')
    return {'trades': len(trades), 'wr': wr, 'pnl': total_pnl, 'pf': pf,
            'avg_win': avg_win, 'avg_loss': avg_loss, 'tf': len(tf), 'mr': len(mr),
            'tf_wr': tf_wr, 'mr_wr': mr_wr}

print(f'\nSplit date: 2025-03-22 (70/30)')
is_m = calc_metrics(is_trades, 'IN-SAMPLE (2023-02 to 2025-03)')
oos_m = calc_metrics(oos_trades, 'OUT-OF-SAMPLE (2025-03 to 2026-02)')
full_m = calc_metrics(result.trades, 'FULL PERIOD')

# Degradation analysis
if is_m and oos_m:
    print(f'\n=== OOS VALIDATION SUMMARY ===')
    print(f'Win Rate:  IS={is_m["wr"]:.1f}% -> OOS={oos_m["wr"]:.1f}% (delta={oos_m["wr"]-is_m["wr"]:+.1f}%)')
    print(f'PF:        IS={is_m["pf"]:.2f} -> OOS={oos_m["pf"]:.2f} (delta={oos_m["pf"]-is_m["pf"]:+.2f})')
    print(f'Avg Win:   IS=${is_m["avg_win"]:.2f} -> OOS=${oos_m["avg_win"]:.2f}')
    print(f'Avg Loss:  IS=${is_m["avg_loss"]:.2f} -> OOS=${oos_m["avg_loss"]:.2f}')
    print(f'PnL/Trade: IS=${is_m["pnl"]/is_m["trades"]:.2f} -> OOS=${oos_m["pnl"]/oos_m["trades"]:.2f}')

    wr_deg = abs(oos_m['wr'] - is_m['wr']) / is_m['wr'] * 100 if is_m['wr'] > 0 else 0
    pf_deg = abs(oos_m['pf'] - is_m['pf']) / is_m['pf'] * 100 if is_m['pf'] > 0 else 0
    print(f'\nDegradation: WR={wr_deg:.1f}%, PF={pf_deg:.1f}%')

    passed = oos_m['wr'] > 50 and oos_m['pf'] > 1.0 and oos_m['pnl'] > 0
    print(f'OOS PASS: {'YES - SYSTEM VALIDATED' if passed else 'NO - NEEDS REVIEW'}')

# Save results
oos_report = {
    'config': 'v6.1d_phase7c_asymmetric',
    'split_date': '2025-03-22',
    'full': m,
    'in_sample': is_m,
    'out_of_sample': oos_m,
    'trades_is': len(is_trades),
    'trades_oos': len(oos_trades),
}
with open('/a0/usr/projects/atlas-asx/backtest/results/oos_validation_v6.1d.json', 'w') as f:
    json.dump(oos_report, f, indent=2, default=str)
print('\nOOS report saved.')
