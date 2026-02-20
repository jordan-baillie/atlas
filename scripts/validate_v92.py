import json, sys, time
from pathlib import Path
import pandas as pd

sys.path.insert(0, '/a0/usr/projects/atlas-asx')

from backtest.engine import BacktestEngine
from strategies.mean_reversion import MeanReversion
from strategies.trend_following import TrendFollowing
from strategies.bb_squeeze import BBSqueeze
from strategies.opening_gap import OpeningGap
from strategies.dividend_capture import DividendCapture

# Load data once
DATA_DIR = Path('/a0/usr/projects/atlas-asx/data/cache')
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

print(f"Loaded {len(data_dict)} tickers")

# ==========================================
# TEST 1: BASELINE (v9.1 defaults)
# ==========================================
print("\n" + "=" * 60)
print("TEST 1: BASELINE v9.1 (defaults)")
print("=" * 60)

with open('/a0/usr/projects/atlas-asx/config/config_v9.1_pre_reoptimization.json') as f:
    cfg_baseline = json.load(f)

t0 = time.time()
strategies_bl = [
    MeanReversion(cfg_baseline),
    TrendFollowing(cfg_baseline),
    BBSqueeze(cfg_baseline),
    OpeningGap(cfg_baseline),
    DividendCapture(cfg_baseline),
]
engine_bl = BacktestEngine(cfg_baseline)
result_bl = engine_bl.run_walkforward(data_dict, strategies_bl)
m_bl = result_bl.metrics
t1 = time.time()

cagr_bl = m_bl.get('cagr', 0)
if abs(cagr_bl) < 2:
    cagr_bl = cagr_bl * 100
print(f"  Trades: {m_bl.get('total_trades', 0)}")
print(f"  CAGR: {cagr_bl:.2f}%")
print(f"  Sharpe: {m_bl.get('sharpe', 0):.4f}")
print(f"  PF: {m_bl.get('profit_factor', 0):.4f}")
print(f"  MaxDD: {m_bl.get('max_drawdown', 0)*100:.2f}%")
print(f"  WinRate: {m_bl.get('win_rate', 0)*100:.1f}%")
print(f"  PnL: ${m_bl.get('total_pnl', 0):.2f}")
print(f"  Time: {t1-t0:.0f}s")

if hasattr(result_bl, 'strategy_metrics'):
    print("\n  Strategy Breakdown:")
    for sname, sm in result_bl.strategy_metrics.items():
        t_count = sm.get('total_trades', 0)
        wr = sm.get('win_rate', 0) * 100
        pnl = sm.get('total_pnl', 0)
        print(f"    {sname}: trades={t_count} wr={wr:.1f}% pnl=${pnl:.2f}")

# ==========================================
# TEST 2: OPTIMIZED v9.2
# ==========================================
print("\n" + "=" * 60)
print("TEST 2: OPTIMIZED v9.2")
print("=" * 60)

with open('/a0/usr/projects/atlas-asx/config/active_config.json') as f:
    cfg_opt = json.load(f)

t0 = time.time()
strategies_opt = []
if cfg_opt['strategies'].get('mean_reversion', {}).get('enabled', True):
    strategies_opt.append(MeanReversion(cfg_opt))
if cfg_opt['strategies'].get('trend_following', {}).get('enabled', True):
    strategies_opt.append(TrendFollowing(cfg_opt))
if cfg_opt['strategies'].get('bb_squeeze', {}).get('enabled', True):
    strategies_opt.append(BBSqueeze(cfg_opt))
if cfg_opt['strategies'].get('opening_gap', {}).get('enabled', True):
    strategies_opt.append(OpeningGap(cfg_opt))
if cfg_opt['strategies'].get('dividend_capture', {}).get('enabled', True):
    strategies_opt.append(DividendCapture(cfg_opt))

names = [type(s).__name__ for s in strategies_opt]
print(f"  Active strategies: {names}")

engine_opt = BacktestEngine(cfg_opt)
result_opt = engine_opt.run_walkforward(data_dict, strategies_opt)
m_opt = result_opt.metrics
t1 = time.time()

cagr_opt = m_opt.get('cagr', 0)
if abs(cagr_opt) < 2:
    cagr_opt = cagr_opt * 100
print(f"  Trades: {m_opt.get('total_trades', 0)}")
print(f"  CAGR: {cagr_opt:.2f}%")
print(f"  Sharpe: {m_opt.get('sharpe', 0):.4f}")
print(f"  PF: {m_opt.get('profit_factor', 0):.4f}")
print(f"  MaxDD: {m_opt.get('max_drawdown', 0)*100:.2f}%")
print(f"  WinRate: {m_opt.get('win_rate', 0)*100:.1f}%")
print(f"  PnL: ${m_opt.get('total_pnl', 0):.2f}")
print(f"  Time: {t1-t0:.0f}s")

if hasattr(result_opt, 'strategy_metrics'):
    print("\n  Strategy Breakdown:")
    for sname, sm in result_opt.strategy_metrics.items():
        t_count = sm.get('total_trades', 0)
        wr = sm.get('win_rate', 0) * 100
        pnl = sm.get('total_pnl', 0)
        print(f"    {sname}: trades={t_count} wr={wr:.1f}% pnl=${pnl:.2f}")

# ==========================================
# COMPARISON
# ==========================================
print("\n" + "=" * 60)
print("COMPARISON: v9.1 BASELINE vs v9.2 OPTIMIZED")
print("=" * 60)
print(f"  CAGR:   {cagr_bl:+.2f}% -> {cagr_opt:+.2f}% (delta: {cagr_opt-cagr_bl:+.2f}%)")
print(f"  Sharpe: {m_bl.get('sharpe',0):.4f} -> {m_opt.get('sharpe',0):.4f}")
print(f"  PF:     {m_bl.get('profit_factor',0):.4f} -> {m_opt.get('profit_factor',0):.4f}")
print(f"  MaxDD:  {m_bl.get('max_drawdown',0)*100:.2f}% -> {m_opt.get('max_drawdown',0)*100:.2f}%")
print(f"  Trades: {m_bl.get('total_trades',0)} -> {m_opt.get('total_trades',0)}")
print(f"  PnL:    ${m_bl.get('total_pnl',0):.2f} -> ${m_opt.get('total_pnl',0):.2f}")

results = {
    "timestamp": pd.Timestamp.now().isoformat(),
    "baseline_v91": {
        "version": "v9.1_maxpos10",
        "metrics": {k: float(v) if isinstance(v, (int, float)) else v for k, v in m_bl.items()}
    },
    "optimized_v92": {
        "version": "v9.2_reoptimized",
        "metrics": {k: float(v) if isinstance(v, (int, float)) else v for k, v in m_opt.items()}
    }
}
with open('/a0/usr/projects/atlas-asx/backtest/results/v92_validation.json', 'w') as f:
    json.dump(results, f, indent=2, default=str)
print("\nResults saved to backtest/results/v92_validation.json")
