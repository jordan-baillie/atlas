"""
Scenario E: $10K equity + Webull $1/order + min_position_value=$1,000
=====================================================================
Tests whether raising min_position_value from $500 to $1,000 controls
the trade count explosion seen at $10K equity (437 trades in Scenario D).

For comparison, reprints Scenario C and D results from memory.

Run date: 2026-02-21
"""
import sys, copy, json, time
from pathlib import Path
import pandas as pd

ROOT = Path('/a0/usr/projects/atlas-asx')
sys.path.insert(0, str(ROOT))

from backtest.engine import BacktestEngine
from strategies.mean_reversion import MeanReversion
from strategies.trend_following import TrendFollowing
from strategies.opening_gap import OpeningGap

# ── Load data ────────────────────────────────────────────────────────────────
print("Loading price data from cache...")
data_dict = {}
for pf in sorted(Path(ROOT / 'data/cache').glob('*.parquet')):
    if pf.stem == 'IOZ_AX':
        continue
    df = pd.read_parquet(pf)
    df.columns = [c.lower() for c in df.columns]
    if 'date' in df.columns:
        df['date'] = pd.to_datetime(df['date'])
        df = df.set_index('date')
    df.index = pd.to_datetime(df.index)
    ticker = pf.stem.replace('_AX', '.AX')
    data_dict[ticker] = df
print(f"  Loaded {len(data_dict)} tickers")

# ── Load base config ──────────────────────────────────────────────────────────
with open(ROOT / 'config' / 'active_config.json') as f:
    BASE_CFG = json.load(f)

# ── Scenarios ─────────────────────────────────────────────────────────────────
SCENARIOS = [
    ("C: $5K  + Webull $1 + minpos $500  [PREV BEST]", {
        "risk.starting_equity":      5000,
        "fees.commission_per_trade": 1.0,
        "fees.commission_pct":       0.0003,
        "fees.flat_fee_threshold":   0.0,
        "fees.min_position_value":   500.0,
    }),
    ("D: $10K + Webull $1 + minpos $500", {
        "risk.starting_equity":      10000,
        "fees.commission_per_trade": 1.0,
        "fees.commission_pct":       0.0003,
        "fees.flat_fee_threshold":   0.0,
        "fees.min_position_value":   500.0,
    }),
    ("E: $10K + Webull $1 + minpos $1000 [TARGET]", {
        "risk.starting_equity":      10000,
        "fees.commission_per_trade": 1.0,
        "fees.commission_pct":       0.0003,
        "fees.flat_fee_threshold":   0.0,
        "fees.min_position_value":   1000.0,
    }),
    ("F: $10K + Webull $1 + minpos $1500", {
        "risk.starting_equity":      10000,
        "fees.commission_per_trade": 1.0,
        "fees.commission_pct":       0.0003,
        "fees.flat_fee_threshold":   0.0,
        "fees.min_position_value":   1500.0,
    }),
]

def apply_overrides(base_cfg, overrides):
    cfg = copy.deepcopy(base_cfg)
    for dotkey, val in overrides.items():
        section, key = dotkey.split('.', 1)
        if section not in cfg:
            cfg[section] = {}
        cfg[section][key] = val
    return cfg

def normalise(v, key=''):
    if key in ('cagr', 'max_drawdown', 'win_rate'):
        return v * 100 if abs(v) < 2 else v
    return v

def run_scenario(name, overrides):
    cfg = apply_overrides(BASE_CFG, overrides)
    strategies = [MeanReversion(cfg), TrendFollowing(cfg), OpeningGap(cfg)]
    eng = BacktestEngine(cfg)
    t0 = time.time()
    result = eng.run_walkforward(data_dict, strategies)
    elapsed = time.time() - t0
    m = result.metrics

    equity  = overrides['risk.starting_equity']
    minpos  = overrides['fees.min_position_value']
    cagr    = normalise(m.get('cagr', 0), 'cagr')
    sharpe  = m.get('sharpe', m.get('sharpe_ratio', 0))
    pf      = m.get('profit_factor', 0)
    dd      = normalise(m.get('max_drawdown', 0), 'max_drawdown')
    wr      = normalise(m.get('win_rate', 0), 'win_rate')
    ntrades = m.get('total_trades', 0)

    # Estimate commissions (round trips × 2 sides × fee)
    fee = overrides['fees.commission_per_trade']
    total_comm = ntrades * fee * 2
    comm_drag  = total_comm / equity * 100
    net_profit = equity * cagr / 100

    print(f"  [{int(elapsed)}s] trades={ntrades}  CAGR={cagr:.2f}%  Sharpe={sharpe:.3f}  "
          f"PF={pf:.3f}  DD={dd:.2f}%  WR={wr:.1f}%")
    print(f"         est.comm=${total_comm:.0f} ({comm_drag:.1f}% of equity)  "
          f"minpos=${minpos:.0f}  net_profit=${net_profit:.0f}/yr")

    return {
        'n_trades': ntrades, 'cagr': cagr, 'sharpe': sharpe,
        'profit_factor': pf, 'max_drawdown': dd, 'win_rate': wr,
        'total_commission': total_comm, 'commission_pct_equity': comm_drag,
        'net_profit_annual': net_profit, 'equity': equity,
        'min_position_value': minpos,
    }

results = []
for name, overrides in SCENARIOS:
    print(f"\nRunning: {name}")
    r = run_scenario(name, overrides)
    results.append((name, r))

# ── Summary table ──────────────────────────────────────────────────────────────
print("\n" + "="*108)
print(f"{'Scenario':<45} {'Trd':>4} {'CAGR%':>7} {'Sharpe':>7} {'PF':>6} "
      f"{'DD%':>6} {'WR%':>5} {'Comm$':>7} {'Drag%':>6} {'NetProfit$':>11}")
print("="*108)
for name, r in results.items() if isinstance(results, dict) else results:
    marker = " <<<" if "TARGET" in name else (" ***" if "PREV BEST" in name else "")
    print(f"{name[:44]:<45} {r['n_trades']:>4} {r['cagr']:>7.2f} {r['sharpe']:>7.3f} "
          f"{r['profit_factor']:>6.3f} {r['max_drawdown']:>6.2f} {r['win_rate']:>5.1f} "
          f"${r['total_commission']:>6.0f} {r['commission_pct_equity']:>6.1f}% "
          f"${r['net_profit_annual']:>10.0f}{marker}")
print("="*108)

# ── Save ──────────────────────────────────────────────────────────────────────
out = ROOT / 'backtest' / 'results' / 'scenario_e_minpos_sweep.json'
with open(out, 'w') as f:
    json.dump({'run_date': '2026-02-21', 'scenarios': {n: r for n, r in results}}, f, indent=2)
print(f"\nSaved to {out}")
