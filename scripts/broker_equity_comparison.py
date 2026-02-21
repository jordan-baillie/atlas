"""
Broker & Equity Comparison Backtest
====================================
Compares 4 scenarios:
  A: $5K equity  + Moomoo ($3/order min)  — current baseline
  B: $10K equity + Moomoo ($3/order min)  — equity increase only
  C: $5K equity  + Webull ($1/order min)  — broker switch only
  D: $10K equity + Webull ($1/order min)  — both changes combined

Commission model:
  flat_fee_threshold=0 forces max(flat_fee, pct) on ALL positions,
  correctly modelling real-world minimum fees per order.

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

# ── Load data once from parquet cache ────────────────────────────────────────
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

# ── Scenario definitions ──────────────────────────────────────────────────────
# flat_fee_threshold=0 → ALL positions use max(flat_fee, pct_commission)
# which correctly applies the minimum fee per order
SCENARIOS = [
    ("A: $5K  + Moomoo $3/order [BASELINE]", {
        "risk.starting_equity":      5000,
        "fees.commission_per_trade": 3.0,
        "fees.commission_pct":       0.0003,
        "fees.flat_fee_threshold":   0.0,
    }),
    ("B: $10K + Moomoo $3/order", {
        "risk.starting_equity":      10000,
        "fees.commission_per_trade": 3.0,
        "fees.commission_pct":       0.0003,
        "fees.flat_fee_threshold":   0.0,
    }),
    ("C: $5K  + Webull $1/order", {
        "risk.starting_equity":      5000,
        "fees.commission_per_trade": 1.0,
        "fees.commission_pct":       0.0003,
        "fees.flat_fee_threshold":   0.0,
    }),
    ("D: $10K + Webull $1/order [TARGET]", {
        "risk.starting_equity":      10000,
        "fees.commission_per_trade": 1.0,
        "fees.commission_pct":       0.0003,
        "fees.flat_fee_threshold":   0.0,
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
    """Normalise fraction-stored metrics to percentages."""
    if key in ('cagr', 'max_drawdown', 'win_rate', 'exposure'):
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

    equity = overrides['risk.starting_equity']
    cagr   = normalise(m.get('cagr', 0), 'cagr')
    sharpe = m.get('sharpe', m.get('sharpe_ratio', 0))
    pf     = m.get('profit_factor', 0)
    dd     = normalise(m.get('max_drawdown', 0), 'max_drawdown')
    wr     = normalise(m.get('win_rate', 0), 'win_rate')
    ntrades = m.get('total_trades', 0)

    # Commissions: try to get from trades or from metrics
    total_comm = 0
    avg_pos = 0
    if hasattr(result, 'trades') and result.trades:
        trades = result.trades
        total_comm = sum(t.get('commission', 0) for t in trades)
        pos_vals = [t.get('entry_price', 0) * t.get('shares', 0) for t in trades]
        avg_pos = sum(pos_vals) / len(pos_vals) if pos_vals else 0
    elif 'total_commission' in m:
        total_comm = m['total_commission']
    else:
        # Estimate: trades * commission_per_trade * 2 sides
        total_comm = ntrades * overrides['fees.commission_per_trade'] * 2
        avg_pos = -1  # unknown

    comm_drag = total_comm / equity * 100
    net_profit = equity * cagr / 100

    print(f"  [{int(elapsed)}s] trades={ntrades} CAGR={cagr:.2f}% Sharpe={sharpe:.3f} "
          f"PF={pf:.3f} DD={dd:.2f}% WR={wr:.1f}%")
    print(f"         comm_total=${total_comm:.0f} ({comm_drag:.1f}% of equity) "
          f"avg_pos=${avg_pos:.0f}" if avg_pos >= 0 else
          f"         comm_estimated=${total_comm:.0f} ({comm_drag:.1f}% of equity)")

    return {
        'n_trades': ntrades,
        'cagr': cagr,
        'sharpe': sharpe,
        'profit_factor': pf,
        'max_drawdown': dd,
        'win_rate': wr,
        'total_commission': total_comm,
        'commission_pct_equity': comm_drag,
        'avg_position_value': avg_pos,
        'net_profit_annual': net_profit,
        'equity': equity,
        'broker_fee': overrides['fees.commission_per_trade'],
    }


# ── Run all scenarios ─────────────────────────────────────────────────────────
results = []
for name, overrides in SCENARIOS:
    print(f"\nRunning: {name}")
    r = run_scenario(name, overrides)
    results.append((name, r))

# ── Summary table ─────────────────────────────────────────────────────────────
print("\n" + "="*105)
print(f"{'Scenario':<42} {'Trd':>4} {'CAGR%':>7} {'Sharpe':>7} {'PF':>6} "
      f"{'DD%':>6} {'WR%':>5} {'Comm$':>7} {'Drag%':>6} {'NetProfit$':>11}")
print("="*105)
for name, r in results:
    marker = " <<<" if "TARGET" in name else (" ***" if "BASELINE" in name else "")
    print(f"{name[:41]:<42} {r['n_trades']:>4} {r['cagr']:>7.2f} {r['sharpe']:>7.3f} "
          f"{r['profit_factor']:>6.3f} {r['max_drawdown']:>6.2f} {r['win_rate']:>5.1f} "
          f"${r['total_commission']:>6.0f} {r['commission_pct_equity']:>6.1f}% "
          f"${r['net_profit_annual']:>10.0f}{marker}")
print("="*105)

# ── Save results ──────────────────────────────────────────────────────────────
out = ROOT / 'backtest' / 'results' / 'broker_equity_comparison.json'
out_data = {
    'run_date': '2026-02-21',
    'description': 'Broker vs equity level comparison: Moomoo $3/order vs Webull $1/order at $5K and $10K',
    'scenarios': {name: r for name, r in results},
}
with open(out, 'w') as f:
    json.dump(out_data, f, indent=2)
print(f"\nResults saved to {out}")
