#!/usr/bin/env python3
"""Atlas Strategy Evaluator — Single Strategy Evaluation on Any Market

The atomic building block the research runner calls repeatedly.

Modes:
    --strategy {name} --market {id}                    Solo backtest
    --strategy {name} --market {id} --params '{json}'  Solo with param overrides
    --strategy {name} --market {id} --combined         Portfolio impact (add to active set)
    --market {id} --active-only                        Run current active config only (baseline)

Handles both promoted strategies (in strategies/) and sandbox strategies (in research/strategies/).

Output: Structured JSON to stdout and optionally to research/experiments/.

Usage:
    python3 scripts/strategy_evaluator.py --strategy momentum_breakout --market sp500
    python3 scripts/strategy_evaluator.py --strategy momentum_breakout --market sp500 --combined
    python3 scripts/strategy_evaluator.py --strategy momentum_breakout --market sp500 --params '{"breakout_period": 30}'
    python3 scripts/strategy_evaluator.py --market sp500 --active-only
    python3 scripts/strategy_evaluator.py --strategy momentum_breakout --market sp500 --experiment-id exp123
"""
import sys
import json
import shutil
import time
import copy
import logging
import argparse
import importlib
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

import pandas as pd
import numpy as np

from utils.config import get_active_config
from backtest.engine import BacktestEngine
from strategies.mean_reversion import MeanReversion
from strategies.trend_following import TrendFollowing
from strategies.bb_squeeze import BBSqueeze
from strategies.opening_gap import OpeningGap
from strategies.momentum_breakout import MomentumBreakout
from strategies.short_term_mr import ShortTermMR
from strategies.sector_rotation import SectorRotation
from strategies.mtf_momentum import MTFMomentum
from strategies.dividend_capture import DividendCapture
from strategies.connors_rsi2 import ConnorsRSI2

from utils.logging_config import setup_logging
setup_logging("strategy_evaluator", level=logging.WARNING)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Strategy registry — maps name to class
# ---------------------------------------------------------------------------
STRATEGY_REGISTRY = {
    'mean_reversion': MeanReversion,
    'trend_following': TrendFollowing,
    'bb_squeeze': BBSqueeze,
    'opening_gap': OpeningGap,
    'momentum_breakout': MomentumBreakout,
    'short_term_mr': ShortTermMR,
    'sector_rotation': SectorRotation,
    'mtf_momentum': MTFMomentum,
    'dividend_capture': DividendCapture,
    'connors_rsi2': ConnorsRSI2,
}


def load_sandbox_strategy(name: str):
    """Try to load a strategy from research/strategies/ if not in main registry."""
    sandbox_dir = PROJECT / 'research' / 'strategies'
    module_path = sandbox_dir / f'{name}.py'
    if not module_path.exists():
        return None
    spec = importlib.util.spec_from_file_location(f'research.strategies.{name}', module_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    # Look for a class that inherits from BaseStrategy
    for attr_name in dir(mod):
        attr = getattr(mod, attr_name)
        if isinstance(attr, type) and hasattr(attr, 'generate_signals') and attr_name != 'BaseStrategy':
            return attr
    return None


def get_strategy_class(name: str):
    """Get strategy class from registry or sandbox."""
    cls = STRATEGY_REGISTRY.get(name)
    if cls:
        return cls
    cls = load_sandbox_strategy(name)
    if cls:
        return cls
    raise ValueError(f"Unknown strategy: {name}. Available: {list(STRATEGY_REGISTRY.keys())}")


def load_market_data(market_id: str, snapshot_id: Optional[str] = None) -> dict:
    """Load all cached ticker data for a market.

    Args:
        market_id:   Market identifier (e.g. 'sp500', 'asx').
        snapshot_id: If provided, load from ``data/snapshots/{snapshot_id}/``
                     instead of ``data/cache/{market_id}/``.  Use this for
                     reproducible backtests pinned to a specific data state.

    Returns:
        Dict mapping ticker -> DataFrame with OHLCV data.
    """
    try:
        from markets import get_market
        market = get_market(market_id)
        benchmark = market.benchmark_ticker
        yf_suffix = market.yfinance_suffix
        valid_universe = set(market.get_formatted_tickers())
        valid_universe.add(benchmark)
    except (ImportError, KeyError):
        benchmark = 'IOZ.AX' if market_id == 'asx' else 'SPY'
        yf_suffix = '.AX' if market_id == 'asx' else ''
        valid_universe = None

    if snapshot_id is not None:
        cache_dir = PROJECT / 'data' / 'snapshots' / snapshot_id
    else:
        cache_dir = PROJECT / 'data' / 'cache' / market_id

    data_dict = {}
    for pf in sorted(cache_dir.glob('*.parquet')):
        stem = pf.stem
        if yf_suffix:
            suffix_under = yf_suffix.replace('.', '_')
            if not stem.endswith(suffix_under):
                continue
            ticker = stem.replace(suffix_under, yf_suffix)
        else:
            if '_AX' in stem:
                continue
            ticker = stem

        if ticker == benchmark:
            continue

        # Skip tickers not in the market's universe (stale cache files)
        if valid_universe is not None and ticker not in valid_universe:
            continue

        try:
            df = pd.read_parquet(pf)
            df.columns = [c.lower() for c in df.columns]
            if 'date' in df.columns:
                df['date'] = pd.to_datetime(df['date'])
                df = df.set_index('date')
            df.index = pd.to_datetime(df.index)
            if len(df) >= 100:
                data_dict[ticker] = df
        except Exception:
            pass
    return data_dict


def save_snapshot(market: str, snapshot_id: str) -> Path:
    """Copy cached market data to a named snapshot for reproducible backtests.

    Creates ``data/snapshots/{snapshot_id}/`` containing all parquet files from
    ``data/cache/{market}/`` plus a ``snapshot_meta.json`` with provenance info.
    Use this before a data refresh to pin the current state for later comparison.

    Args:
        market:      Market ID whose cache to snapshot (e.g. 'sp500').
        snapshot_id: Unique name for this snapshot (e.g. 'pre-refresh-2026-03').

    Returns:
        Path to the created snapshot directory.

    Raises:
        FileNotFoundError: If the market cache directory does not exist.
    """
    cache_dir = PROJECT / 'data' / 'cache' / market
    if not cache_dir.exists():
        raise FileNotFoundError(f"Cache directory not found: {cache_dir}")

    snapshot_dir = PROJECT / 'data' / 'snapshots' / snapshot_id
    snapshot_dir.mkdir(parents=True, exist_ok=True)

    count = 0
    for pf in cache_dir.glob('*.parquet'):
        shutil.copy2(pf, snapshot_dir / pf.name)
        count += 1

    meta = {
        'market': market,
        'snapshot_id': snapshot_id,
        'created_at': datetime.now(timezone.utc).isoformat(),
        'source_dir': str(cache_dir),
        'file_count': count,
    }
    with open(snapshot_dir / 'snapshot_meta.json', 'w') as f:
        json.dump(meta, f, indent=2)

    logger.info(f"Snapshot '{snapshot_id}' saved: {count} parquet files -> {snapshot_dir}")
    return snapshot_dir


def list_snapshots() -> list:
    """Return a list of all available snapshots.

    Each entry is a dict from ``snapshot_meta.json`` (keys: snapshot_id, market,
    created_at, file_count) or a minimal dict with just ``snapshot_id`` if the
    metadata file is missing.

    Returns:
        List of snapshot metadata dicts, sorted by snapshot directory name.
    """
    snapshots_root = PROJECT / 'data' / 'snapshots'
    if not snapshots_root.exists():
        return []

    snapshots = []
    for snap_dir in sorted(snapshots_root.iterdir()):
        if not snap_dir.is_dir():
            continue
        meta_path = snap_dir / 'snapshot_meta.json'
        if meta_path.exists():
            try:
                with open(meta_path) as f:
                    snapshots.append(json.load(f))
            except Exception:
                snapshots.append({'snapshot_id': snap_dir.name, 'market': 'unknown'})
        else:
            snapshots.append({'snapshot_id': snap_dir.name, 'market': 'unknown'})

    return snapshots


def make_config_with_strategy(base_config: dict, strategy_name: str,
                               params_override: dict = None,
                               solo: bool = False) -> dict:
    """Create a config with the target strategy enabled.

    If solo=True, disable all other strategies.
    If solo=False, enable target alongside existing active strategies.
    """
    cfg = copy.deepcopy(base_config)

    # Ensure strategy section exists
    if strategy_name not in cfg.get('strategies', {}):
        cfg.setdefault('strategies', {})[strategy_name] = {'enabled': True}
    else:
        cfg['strategies'][strategy_name]['enabled'] = True

    if solo:
        for s in cfg.get('strategies', {}):
            if s != strategy_name:
                cfg['strategies'][s]['enabled'] = False

    # Apply param overrides
    if params_override:
        for k, v in params_override.items():
            cfg['strategies'][strategy_name][k] = v

    return cfg


def run_backtest(cfg: dict, data: dict, strategy_names: list = None) -> dict:
    """Run walkforward backtest, return metrics dict."""
    strategies = []
    strats_cfg = cfg.get('strategies', {})
    for name, scfg in strats_cfg.items():
        if scfg.get('enabled', False):
            if strategy_names and name not in strategy_names:
                continue
            cls = get_strategy_class(name)
            strategies.append(cls(cfg))

    if not strategies:
        return {'error': 'No strategies enabled', 'total_trades': 0}

    engine = BacktestEngine(cfg)
    result = engine.run_walkforward(data, strategies)
    m = result.metrics

    cagr = m.get('cagr', 0)
    cagr_pct = cagr * 100 if abs(cagr) < 2 else cagr

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
        # R-multiple metrics (from calc_all_metrics)
        'expectancy_r': round(m.get('expectancy_r', 0), 4),
        'avg_r': round(m.get('avg_r', 0), 4),
        'r_count': m.get('r_count', 0),
        # Statistical edge
        'edge_p_value': m.get('edge_p_value', 1.0),
        'edge_significant': m.get('edge_significant', False),
        # Risk metrics (VaR, CVaR, Calmar)
        'var_95_pct': round(m.get('var_95', 0) * 100, 3),
        'cvar_95_pct': round(m.get('cvar_95', 0) * 100, 3),
        'calmar': round(m.get('calmar', 0), 4),
        # Monte Carlo drawdown
        'mc_p95_drawdown_pct': round(m.get('mc_p95_drawdown', 0) * 100, 2),
        'mc_fragile': m.get('mc_fragile', False),
    }

    # Per-strategy breakdown if available
    if hasattr(result, 'strategy_breakdown') and result.strategy_breakdown:
        metrics['strategy_breakdown'] = result.strategy_breakdown
    elif hasattr(result, 'trades') and result.trades:
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
        metrics['strategy_breakdown'] = breakdown

    return metrics


def evaluate_strategy(strategy_name: str, market_id: str,
                      params_override: dict = None,
                      combined: bool = False,
                      experiment_id: str = None) -> dict:
    """Main evaluation entry point. Returns structured result dict."""
    t0 = time.time()
    config = get_active_config(market_id)
    data = load_market_data(market_id)

    result = {
        'experiment_id': experiment_id,
        'strategy': strategy_name,
        'market': market_id,
        'mode': 'combined' if combined else 'solo',
        'params_override': params_override,
        'n_tickers': len(data),
        'config_version': config.get('version', 'unknown'),
        'timestamp': datetime.now(timezone.utc).isoformat(),
    }

    if combined:
        # Step 1: Baseline (current active config, no changes)
        baseline_cfg = copy.deepcopy(config)
        baseline_metrics = run_backtest(baseline_cfg, data)
        result['baseline'] = baseline_metrics

        # Step 2: Combined (add target strategy to active set)
        combined_cfg = make_config_with_strategy(config, strategy_name,
                                                  params_override, solo=False)
        combined_metrics = run_backtest(combined_cfg, data)
        result['combined'] = combined_metrics

        # Step 3: Compute deltas
        delta = {}
        for key in ('cagr_pct', 'sharpe', 'sortino', 'max_drawdown_pct',
                     'win_rate_pct', 'profit_factor', 'total_pnl', 'total_trades'):
            b = baseline_metrics.get(key, 0) or 0
            c = combined_metrics.get(key, 0) or 0
            delta[key] = round(c - b, 4)
        result['delta'] = delta

        # Step 4: Solo metrics for the strategy alone
        solo_cfg = make_config_with_strategy(config, strategy_name,
                                              params_override, solo=True)
        solo_metrics = run_backtest(solo_cfg, data)
        result['solo'] = solo_metrics

    else:
        # Solo evaluation
        solo_cfg = make_config_with_strategy(config, strategy_name,
                                              params_override, solo=True)
        solo_metrics = run_backtest(solo_cfg, data)
        result['solo'] = solo_metrics

    result['runtime_s'] = round(time.time() - t0, 1)

    # Save to experiment file if ID provided
    if experiment_id:
        from research.models import EXPERIMENTS_DIR
        EXPERIMENTS_DIR.mkdir(parents=True, exist_ok=True)
        out_path = EXPERIMENTS_DIR / f'eval-{experiment_id}.json'
        with open(out_path, 'w') as f:
            json.dump(result, f, indent=2, default=str)
        result['output_path'] = str(out_path)

    return result


def evaluate_active_baseline(market_id: str) -> dict:
    """Run the current active config as-is (baseline measurement)."""
    t0 = time.time()
    config = get_active_config(market_id)
    data = load_market_data(market_id)

    metrics = run_backtest(config, data)

    return {
        'market': market_id,
        'mode': 'active_baseline',
        'config_version': config.get('version', 'unknown'),
        'n_tickers': len(data),
        'metrics': metrics,
        'timestamp': datetime.now(timezone.utc).isoformat(),
        'runtime_s': round(time.time() - t0, 1),
    }


def parse_args():
    parser = argparse.ArgumentParser(description='Atlas Strategy Evaluator')
    parser.add_argument('--strategy', type=str, help='Strategy name to evaluate')
    parser.add_argument('--market', type=str, required=True, help='Market ID (asx, sp500)')
    parser.add_argument('--params', type=str, default=None,
                        help='JSON string of parameter overrides')
    parser.add_argument('--combined', action='store_true',
                        help='Test strategy added to current active portfolio')
    parser.add_argument('--active-only', action='store_true',
                        help='Run current active config baseline only')
    parser.add_argument('--experiment-id', type=str, default=None,
                        help='Experiment ID for output file naming')
    parser.add_argument('--output', type=str, default=None,
                        help='Output JSON path (default: stdout)')
    parser.add_argument('--quiet', action='store_true',
                        help='Suppress progress output')
    return parser.parse_args()


def main():
    args = parse_args()

    if args.active_only:
        result = evaluate_active_baseline(args.market)
    elif args.strategy:
        params = json.loads(args.params) if args.params else None
        result = evaluate_strategy(
            strategy_name=args.strategy,
            market_id=args.market,
            params_override=params,
            combined=args.combined,
            experiment_id=args.experiment_id,
        )
    else:
        print("Error: --strategy or --active-only required", file=sys.stderr)
        return 1

    # Output
    output_json = json.dumps(result, indent=2, default=str)
    if args.output:
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        with open(args.output, 'w') as f:
            f.write(output_json)
        if not args.quiet:
            _print_summary(result)
            print(f"\nSaved to: {args.output}")
    else:
        if not args.quiet:
            _print_summary(result)
        else:
            print(output_json)

    return 0


def _print_summary(result: dict):
    """Print a human-readable summary."""
    mode = result.get('mode', 'unknown')
    market = result.get('market', '?')
    strategy = result.get('strategy', 'active')

    print(f"\n{'='*60}")
    print(f"Strategy Evaluator — {strategy} on {market} ({mode})")
    print(f"{'='*60}")

    if mode == 'active_baseline':
        m = result.get('metrics', {})
        print(f"  Config: {result.get('config_version')}")
        print(f"  Tickers: {result.get('n_tickers')}")
        _print_metrics("Baseline", m)
    elif mode == 'solo':
        _print_metrics("Solo", result.get('solo', {}))
    elif mode == 'combined':
        _print_metrics("Baseline", result.get('baseline', {}))
        _print_metrics("Solo", result.get('solo', {}))
        _print_metrics("Combined", result.get('combined', {}))
        delta = result.get('delta', {})
        print(f"\n  Delta (combined - baseline):")
        for k, v in delta.items():
            print(f"    {k}: {v:+.4f}")

    print(f"\n  Runtime: {result.get('runtime_s', 0):.1f}s")


def _print_metrics(label: str, m: dict):
    print(f"\n  {label}:")
    print(f"    Trades: {m.get('total_trades', 0)}")
    print(f"    CAGR: {m.get('cagr_pct', 0):.2f}%")
    print(f"    Sharpe: {m.get('sharpe', 0):.4f}")
    print(f"    Sortino: {m.get('sortino', 0):.4f}")
    print(f"    Max DD: {m.get('max_drawdown_pct', 0):.2f}%")
    print(f"    Win Rate: {m.get('win_rate_pct', 0):.1f}%")
    print(f"    PF: {m.get('profit_factor', 0):.4f}")
    print(f"    PnL: ${m.get('total_pnl', 0):.2f}")


if __name__ == '__main__':
    sys.exit(main())
