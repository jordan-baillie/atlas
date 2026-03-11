#!/usr/bin/env python3
"""Atlas Portfolio-Level Research — Task #92

Three sweeps:
    1. Risk-per-trade sweep: 0.25%, 0.5%, 1.0%, 2.0% (current: 0.5%)
    2. Max positions sweep: 5, 8, 10, 15, 20 (current: 15, revalidation)
    3. Allocation pools: TF:5/MR:5/OG:3 hard_pool vs disabled (current: disabled)

All run against 7yr unadjusted snapshot for deterministic reproducibility.
Active strategies only: mean_reversion, trend_following, opening_gap.
"""
import sys
import json
import copy
import time
import logging
import multiprocessing as mp
from pathlib import Path
from datetime import datetime, timezone

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

logging.basicConfig(level=logging.WARNING)

MARKET = "sp500"
SNAPSHOT = "sp500_v3_unadj_20260310_7yr"
ACTIVE_STRATEGIES = ["mean_reversion", "trend_following", "opening_gap"]

# ── Experiment definitions ──────────────────────────────────────────────────

# (id, label, config_overrides)
EXPERIMENTS = []

# Current baseline (after bug fix, engine reads risk.max_risk_per_trade_pct directly)
EXPERIMENTS.append(("baseline", "BASELINE: current config (risk=0.5%, max_pos=15, no pools)", {}))

# --- Sweep 1: Risk-per-trade ---
# After bug fix: DynamicSizer now reads risk.max_risk_per_trade_pct as fallback
# when dynamic_sizing.base_risk_pct is not explicitly set. Only need to set risk.
for rpt in [0.0015, 0.0025, 0.003, 0.0035, 0.004, 0.0045, 0.006, 0.0075, 0.01, 0.015, 0.02, 0.03]:
    label = f"risk_per_trade={rpt*100:.2f}%"
    bps = int(rpt * 10000)
    EXPERIMENTS.append((
        f"risk_{bps}bps",
        label,
        {"risk": {"max_risk_per_trade_pct": rpt}}
    ))

# --- Sweep 2: Max positions ---
for mp_val in [5, 8, 10, 20]:
    EXPERIMENTS.append((
        f"maxpos_{mp_val}",
        f"max_positions={mp_val}",
        {"risk": {"max_open_positions": mp_val}}
    ))

# --- Sweep 3: Allocation pools ---
EXPERIMENTS.append((
    "pool_hard",
    "allocation pools hard_pool (TF:5, MR:5, OG:3)",
    {"allocation": {
        "enabled": True,
        "mode": "hard_pool",
        "overflow_enabled": False,
        "pools": {
            "trend_following": {"max_positions": 5},
            "mean_reversion": {"max_positions": 5},
            "opening_gap": {"max_positions": 3},
            "_other": {"max_positions": 2},
        }
    }}
))

EXPERIMENTS.append((
    "pool_soft",
    "allocation pools soft_pool (TF:5, MR:5, OG:3, overflow)",
    {"allocation": {
        "enabled": True,
        "mode": "soft_pool",
        "overflow_enabled": True,
        "pools": {
            "trend_following": {"max_positions": 5},
            "mean_reversion": {"max_positions": 5},
            "opening_gap": {"max_positions": 3},
            "_other": {"max_positions": 2},
        }
    }}
))

EXPERIMENTS.append((
    "pool_balanced",
    "allocation pools balanced (TF:5, MR:7, OG:3)",
    {"allocation": {
        "enabled": True,
        "mode": "hard_pool",
        "overflow_enabled": False,
        "pools": {
            "trend_following": {"max_positions": 5},
            "mean_reversion": {"max_positions": 7},
            "opening_gap": {"max_positions": 3},
            "_other": {"max_positions": 0},
        }
    }}
))

# --- Combined: Best risk + best positions (will decide after initial sweep) ---
EXPERIMENTS.append((
    "risk_1pct_maxpos_20",
    "risk=1.0% + max_pos=20 (wider deployment)",
    {"risk": {"max_risk_per_trade_pct": 0.01, "max_open_positions": 20}}
))

EXPERIMENTS.append((
    "risk_2pct_maxpos_10",
    "risk=2.0% + max_pos=10 (concentrated, bigger bets)",
    {"risk": {"max_risk_per_trade_pct": 0.02, "max_open_positions": 10}}
))

EXPERIMENTS.append((
    "risk_035_maxpos_15",
    "risk=0.35% + max_pos=15 (best from initial sweep)",
    {"risk": {"max_risk_per_trade_pct": 0.0035, "max_open_positions": 15}}
))

EXPERIMENTS.append((
    "risk_035_maxpos_20",
    "risk=0.35% + max_pos=20 (best risk + more room)",
    {"risk": {"max_risk_per_trade_pct": 0.0035, "max_open_positions": 20}}
))


def load_data():
    """Load market data from snapshot."""
    import pandas as pd
    from markets import get_market
    market = get_market(MARKET)
    valid = set(market.get_formatted_tickers())
    valid.add(market.benchmark_ticker)
    cache = PROJECT / 'data' / 'snapshots' / SNAPSHOT
    if not cache.exists():
        cache = PROJECT / 'data' / 'cache' / MARKET
        print(f"⚠ Snapshot {SNAPSHOT} not found, using live cache")
    data = {}
    for pf in sorted(cache.glob('*.parquet')):
        stem = pf.stem
        if '_AX' in stem:
            continue
        ticker = stem
        if ticker == market.benchmark_ticker.replace('.', '_'):
            continue
        if ticker not in valid and ticker.replace('_', '.') not in valid:
            continue
        try:
            df = pd.read_parquet(pf)
            df.columns = [c.lower() for c in df.columns]
            if 'date' in df.columns:
                df['date'] = pd.to_datetime(df['date'])
                df = df.set_index('date')
            df.index = pd.to_datetime(df.index)
            if len(df) >= 100:
                data[ticker] = df
        except Exception:
            pass
    return data


def apply_overrides(base_cfg, overrides):
    """Deep merge overrides into base config."""
    cfg = copy.deepcopy(base_cfg)
    for key, val in overrides.items():
        if isinstance(val, dict) and key in cfg and isinstance(cfg[key], dict):
            for k2, v2 in val.items():
                if isinstance(v2, dict) and k2 in cfg[key] and isinstance(cfg[key][k2], dict):
                    cfg[key][k2].update(v2)
                else:
                    cfg[key][k2] = v2
        else:
            cfg[key] = val
    return cfg


def run_experiment(args):
    """Worker: run one experiment."""
    exp_id, label, overrides, base_cfg, data = args

    from backtest.engine import BacktestEngine
    from strategies.mean_reversion import MeanReversion
    from strategies.trend_following import TrendFollowing
    from strategies.opening_gap import OpeningGap

    STRAT_CLASSES = {
        'mean_reversion': MeanReversion,
        'trend_following': TrendFollowing,
        'opening_gap': OpeningGap,
    }

    cfg = apply_overrides(base_cfg, overrides)

    # Only use active strategies
    strats = []
    for sname in ACTIVE_STRATEGIES:
        scfg = None
        if isinstance(cfg.get('strategies'), list):
            for s in cfg['strategies']:
                if isinstance(s, dict) and s.get('name') == sname:
                    scfg = s
                    break
        elif isinstance(cfg.get('strategies'), dict):
            scfg = cfg['strategies'].get(sname)

        if scfg and scfg.get('enabled', True) and sname in STRAT_CLASSES:
            strats.append(STRAT_CLASSES[sname](cfg))

    if not strats:
        return exp_id, label, {'error': 'No strategies', 'total_trades': 0}, 0.0

    t0 = time.time()
    engine = BacktestEngine(cfg)
    result = engine.run_walkforward(data, strats)
    dt = time.time() - t0

    m = result.metrics
    cagr = m.get('cagr', 0)
    cagr_pct = cagr * 100 if abs(cagr) < 2 else cagr

    # Per-strategy breakdown
    strat_trades = {}
    for t in result.trades:
        s = t.get('strategy', 'unknown')
        strat_trades.setdefault(s, []).append(t)
    breakdown = {}
    for s, trades in strat_trades.items():
        pnls = [t.get('pnl', 0) for t in trades]
        wins = sum(1 for p in pnls if p > 0)
        breakdown[s] = {
            'trades': len(trades),
            'total_pnl': round(sum(pnls), 2),
            'win_rate_pct': round(wins / len(trades) * 100, 1) if trades else 0,
        }

    metrics = {
        'total_trades': m.get('total_trades', 0),
        'cagr_pct': round(cagr_pct, 4),
        'sharpe': round(m.get('sharpe', 0), 4),
        'sortino': round(m.get('sortino', 0), 4),
        'max_drawdown_pct': round(m.get('max_drawdown', 0) * 100, 4),
        'win_rate_pct': round(m.get('win_rate', 0) * 100, 2),
        'profit_factor': round(m.get('profit_factor', 0), 4),
        'total_pnl': round(m.get('total_pnl', 0), 2),
        'avg_trade': round(m.get('avg_trade', 0), 2),
        'final_equity': round(m.get('final_equity', 0), 2),
        'expectancy_r': round(m.get('expectancy_r', 0), 4),
        'edge_p_value': m.get('edge_p_value', 1.0),
        'edge_significant': m.get('edge_significant', False),
        'mc_p95_drawdown': round(m.get('mc_p95_drawdown', 0) * 100, 2) if m.get('mc_p95_drawdown') else None,
        'mc_fragile': m.get('mc_fragile', False),
        'calmar': round(m.get('calmar', 0), 4),
        'var_95': round(m.get('var_95', 0) * 100, 4) if m.get('var_95') else None,
        'strategy_breakdown': breakdown,
        'config_overrides': overrides,
    }

    risk_pct = cfg.get('risk', {}).get('max_risk_per_trade_pct', 0.005)
    max_pos = cfg.get('risk', {}).get('max_open_positions', 15)
    alloc = cfg.get('allocation', {}).get('enabled', False)
    print(f"  ✓ [{exp_id}] risk={risk_pct*100:.2f}% maxpos={max_pos} alloc={alloc} "
          f"Sharpe={metrics['sharpe']:+.4f} CAGR={metrics['cagr_pct']:.1f}% "
          f"Trades={metrics['total_trades']} DD={metrics['max_drawdown_pct']:.1f}% "
          f"PnL=${metrics['total_pnl']:.0f} ({dt:.0f}s)", flush=True)

    return exp_id, label, metrics, round(dt, 1)


def main():
    n_workers = min(len(EXPERIMENTS), max(1, mp.cpu_count() - 2))
    print(f"╔══════════════════════════════════════════════════════════════════╗")
    print(f"║  Portfolio-Level Research — Task #92                            ║")
    print(f"║  {len(EXPERIMENTS)} experiments × {n_workers} workers                                  ║")
    print(f"║  Snapshot: {SNAPSHOT}                     ║")
    print(f"╚══════════════════════════════════════════════════════════════════╝")

    t_start = time.time()
    from utils.config import get_active_config
    base = get_active_config(MARKET)

    # Ensure only active strategies are enabled
    if isinstance(base.get('strategies'), list):
        for s in base['strategies']:
            if isinstance(s, dict):
                s['enabled'] = s.get('name', '') in ACTIVE_STRATEGIES
    elif isinstance(base.get('strategies'), dict):
        for name, scfg in base['strategies'].items():
            scfg['enabled'] = name in ACTIVE_STRATEGIES

    data = load_data()
    load_time = time.time() - t_start
    print(f"\nLoaded {len(data)} tickers in {load_time:.0f}s\n")

    worker_args = [
        (exp_id, label, overrides, base, data)
        for exp_id, label, overrides in EXPERIMENTS
    ]

    mp.set_start_method('fork', force=True)
    results = {}
    with mp.Pool(processes=n_workers) as pool:
        for result in pool.imap_unordered(run_experiment, worker_args):
            exp_id, label, metrics, dt = result
            results[exp_id] = {'label': label, 'metrics': metrics, 'runtime_s': dt}

    total_time = time.time() - t_start

    # ── Print results table ──
    print(f"\n{'='*120}")
    print(f"RESULTS — Portfolio-Level Research (Task #92)")
    print(f"{'='*120}")

    # Header
    hdr = f"{'Experiment':<45} {'Sharpe':>8} {'CAGR%':>8} {'DD%':>6} {'Trades':>7} {'PF':>6} {'WR%':>6} {'PnL':>9} {'Calmar':>7} {'p-val':>6}"
    print(hdr)
    print("-" * 120)

    # Get baseline for delta calculation
    bl = results.get('baseline', {}).get('metrics', {})
    bl_sharpe = bl.get('sharpe', 0)

    # Print in experiment order
    for exp_id, label, _ in EXPERIMENTS:
        r = results.get(exp_id, {})
        m = r.get('metrics', {})
        if 'error' in m:
            print(f"  {label:<43} ERROR: {m['error']}")
            continue

        sharpe = m.get('sharpe', 0)
        delta = sharpe - bl_sharpe if exp_id != 'baseline' else 0
        delta_str = f" ({delta:+.3f})" if exp_id != 'baseline' else " (BASE)"

        print(f"  {label:<43} {sharpe:>+8.4f}{delta_str:>9} "
              f"{m.get('cagr_pct',0):>7.1f}% {m.get('max_drawdown_pct',0):>5.1f}% "
              f"{m.get('total_trades',0):>7} {m.get('profit_factor',0):>6.2f} "
              f"{m.get('win_rate_pct',0):>5.1f}% ${m.get('total_pnl',0):>8.0f} "
              f"{m.get('calmar',0):>7.2f} {m.get('edge_p_value',1):>6.3f}")

    # ── Sweep summaries ──
    print(f"\n{'='*80}")
    print("SWEEP 1: Risk Per Trade")
    print(f"{'='*80}")
    risk_experiments = ['baseline'] + [e[0] for e in EXPERIMENTS if e[0].startswith('risk_') and 'maxpos' not in e[0]]
    for exp_id in risk_experiments:
        r = results.get(exp_id, {}).get('metrics', {})
        cfg_risk = 0.005  # default
        if exp_id != 'baseline':
            ovr = [e[2] for e in EXPERIMENTS if e[0] == exp_id][0]
            cfg_risk = ovr.get('risk', {}).get('max_risk_per_trade_pct', 0.005)
        s_delta = r.get('sharpe', 0) - bl_sharpe
        print(f"  risk={cfg_risk*100:>5.2f}% → Sharpe {r.get('sharpe',0):>+.4f} (Δ{s_delta:>+.4f}), "
              f"CAGR {r.get('cagr_pct',0):>6.1f}%, DD {r.get('max_drawdown_pct',0):>5.1f}%, "
              f"Trades {r.get('total_trades',0):>4}, PnL ${r.get('total_pnl',0):>7.0f}")

    print(f"\n{'='*80}")
    print("SWEEP 2: Max Open Positions")
    print(f"{'='*80}")
    pos_experiments = ['baseline'] + [e[0] for e in EXPERIMENTS if e[0].startswith('maxpos_')]
    for exp_id in pos_experiments:
        r = results.get(exp_id, {}).get('metrics', {})
        max_pos = 15
        if exp_id != 'baseline':
            ovr = [e[2] for e in EXPERIMENTS if e[0] == exp_id][0]
            max_pos = ovr.get('risk', {}).get('max_open_positions', 15)
        s_delta = r.get('sharpe', 0) - bl_sharpe
        bkdn = r.get('strategy_breakdown', {})
        strat_str = ", ".join(f"{k}={v['trades']}" for k, v in sorted(bkdn.items()))
        print(f"  max_pos={max_pos:>2} → Sharpe {r.get('sharpe',0):>+.4f} (Δ{s_delta:>+.4f}), "
              f"CAGR {r.get('cagr_pct',0):>6.1f}%, DD {r.get('max_drawdown_pct',0):>5.1f}%, "
              f"Trades {r.get('total_trades',0):>4} [{strat_str}]")

    print(f"\n{'='*80}")
    print("SWEEP 3: Allocation Pools")
    print(f"{'='*80}")
    pool_experiments = ['baseline'] + [e[0] for e in EXPERIMENTS if e[0].startswith('pool_')]
    for exp_id in pool_experiments:
        r = results.get(exp_id, {}).get('metrics', {})
        s_delta = r.get('sharpe', 0) - bl_sharpe
        bkdn = r.get('strategy_breakdown', {})
        strat_str = ", ".join(f"{k}={v['trades']}" for k, v in sorted(bkdn.items()))
        print(f"  {exp_id:<15} → Sharpe {r.get('sharpe',0):>+.4f} (Δ{s_delta:>+.4f}), "
              f"CAGR {r.get('cagr_pct',0):>6.1f}%, DD {r.get('max_drawdown_pct',0):>5.1f}%, "
              f"Trades {r.get('total_trades',0):>4} [{strat_str}]")

    print(f"\n{'='*80}")
    print("COMBINED EXPERIMENTS")
    print(f"{'='*80}")
    for exp_id in ['risk_1pct_maxpos_20', 'risk_2pct_maxpos_10']:
        r = results.get(exp_id, {}).get('metrics', {})
        s_delta = r.get('sharpe', 0) - bl_sharpe
        bkdn = r.get('strategy_breakdown', {})
        strat_str = ", ".join(f"{k}={v['trades']}" for k, v in sorted(bkdn.items()))
        print(f"  {exp_id:<25} → Sharpe {r.get('sharpe',0):>+.4f} (Δ{s_delta:>+.4f}), "
              f"CAGR {r.get('cagr_pct',0):>6.1f}%, DD {r.get('max_drawdown_pct',0):>5.1f}%, "
              f"Trades {r.get('total_trades',0):>4} [{strat_str}]")

    # ── Find best configuration ──
    print(f"\n{'='*80}")
    print("RANKING (by Sharpe)")
    print(f"{'='*80}")
    ranked = sorted(results.items(), key=lambda x: x[1].get('metrics', {}).get('sharpe', -99), reverse=True)
    for i, (exp_id, r) in enumerate(ranked):
        m = r.get('metrics', {})
        marker = " ★" if i == 0 else "  "
        base = " (CURRENT)" if exp_id == 'baseline' else ""
        print(f"  {i+1:>2}. {marker} {r.get('label',''):<50} Sharpe {m.get('sharpe',0):>+.4f} "
              f"CAGR {m.get('cagr_pct',0):>6.1f}% DD {m.get('max_drawdown_pct',0):>5.1f}%{base}")

    # Save results
    output = {
        'task': 'task_92_portfolio_research',
        'timestamp': datetime.now(timezone.utc).isoformat(),
        'snapshot': SNAPSHOT,
        'market': MARKET,
        'n_tickers': len(data),
        'total_runtime_s': round(total_time, 1),
        'baseline': results.get('baseline', {}),
        'experiments': results,
        'sweeps': {
            'risk_per_trade': {exp_id: results.get(exp_id, {}) for exp_id in risk_experiments},
            'max_positions': {exp_id: results.get(exp_id, {}) for exp_id in pos_experiments},
            'allocation_pools': {exp_id: results.get(exp_id, {}) for exp_id in pool_experiments},
        }
    }
    out_path = PROJECT / 'research' / 'results' / 'task92_portfolio_research.json'
    with open(out_path, 'w') as f:
        json.dump(output, f, indent=2, default=str)
    print(f"\nResults saved to {out_path}")
    print(f"Total time: {total_time:.0f}s ({total_time/60:.1f} min)")


if __name__ == '__main__':
    main()
