"""Run Phase 8BCD backtest with full diagnostics."""
import json, sys, logging, time
from pathlib import Path
from datetime import datetime

logging.basicConfig(level=logging.WARNING)
sys.path.insert(0, '/a0/usr/projects/atlas-asx')

import pandas as pd
from utils.config import load_config
from strategies.trend_following import TrendFollowing
from strategies.mean_reversion import MeanReversion
from strategies.sector_rotation import SectorRotation
from backtest.engine import BacktestEngine

config = load_config()
print(f'Config: {config["version"]}', flush=True)
print(f'Max positions: {config["risk"]["max_open_positions"]}', flush=True)
print(f'Min confidence: {config["risk"]["min_confidence"]}', flush=True)

# Load data
cache_dir = Path('data/cache')
with open('data/processed/universe.json') as f:
    tickers = json.load(f)['tickers']

data = {}
for t in tickers:
    p = cache_dir / (t.replace('.', '_') + '.parquet')
    if p.exists():
        df = pd.read_parquet(p)
        if len(df) > 100:
            data[t] = df
print(f'Loaded {len(data)} tickers', flush=True)

# Setup strategies
strategies = [
    TrendFollowing(config),
    MeanReversion(config),
    SectorRotation(config),
]
print(f'Strategies: {[s.name for s in strategies]}', flush=True)

# Run backtest
print('Running walkforward backtest...', flush=True)
t0 = time.time()
engine = BacktestEngine(config)
result = engine.run_walkforward(data, strategies)
dt = time.time() - t0
print(f'\nCompleted in {dt:.1f}s', flush=True)

# Print summary
print(f'\n{"="*60}')
print(f'  PHASE 8BCD RESULTS')
print(f'{"="*60}')
for k, v in result.metrics.items():
    if isinstance(v, float):
        print(f'  {k:25s}: {v:.4f}')
    else:
        print(f'  {k:25s}: {v}')

# Strategy breakdown
strat_trades = {}
for t in result.trades:
    s = t.get('strategy', 'unknown')
    strat_trades.setdefault(s, []).append(t)

print(f'\n{"="*60}')
print(f'  STRATEGY BREAKDOWN')
print(f'{"="*60}')
for strat, trades in sorted(strat_trades.items()):
    wins = sum(1 for t in trades if t.get('pnl', 0) > 0)
    losses = len(trades) - wins
    pnl = sum(t.get('pnl', 0) for t in trades)
    wr = wins/len(trades)*100 if trades else 0
    avg_pnl = pnl/len(trades) if trades else 0
    print(f'  {strat}: {len(trades)} trades (W={wins} L={losses}), WR={wr:.1f}%, PnL=${pnl:.2f}, AvgPnL=${avg_pnl:.2f}')

# IS/OOS breakdown  
is_trades = [t for t in result.trades if t.get('fold_type') == 'IS' or t.get('is_in_sample', True)]
oos_trades = [t for t in result.trades if t.get('fold_type') == 'OOS' or not t.get('is_in_sample', True)]
print(f'\nIS trades: {len(is_trades)}, OOS trades: {len(oos_trades)}')

# Save detailed results
output = {
    'config_version': config['version'],
    'timestamp': datetime.now().isoformat(),
    'num_tickers': len(data),
    'elapsed_seconds': round(dt, 1),
    'metrics': result.metrics,
    'strategy_breakdown': {},
    'trades': result.trades,
}
for strat, trades in strat_trades.items():
    wins = sum(1 for t in trades if t.get('pnl', 0) > 0)
    pnl = sum(t.get('pnl', 0) for t in trades)
    output['strategy_breakdown'][strat] = {
        'trades': len(trades),
        'wins': wins,
        'losses': len(trades) - wins,
        'win_rate': round(wins/len(trades)*100, 1) if trades else 0,
        'pnl': round(pnl, 2),
    }

out_path = f'backtest/results/phase8bcd_detailed_{datetime.now().strftime("%Y%m%d_%H%M%S")}.json'
with open(out_path, 'w') as f:
    json.dump(output, f, indent=2, default=str)
print(f'\nSaved: {out_path}')
