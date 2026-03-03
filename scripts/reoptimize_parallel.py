#!/usr/bin/env python3
"""Atlas Full-Universe Re-Optimization — Parallel (8-core)

Parallelises:
  1. Strategy-level: all 5 strategies optimized concurrently via ProcessPoolExecutor
  2. Param-value-level: within each strategy sweep, all candidate values evaluated in parallel

Produces identical artifacts to the sequential script (candidate config + results JSON).
"""
import sys, json, os, copy, time, logging, argparse, shutil
from pathlib import Path
from datetime import datetime
from concurrent.futures import ProcessPoolExecutor, as_completed

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))
os.chdir(str(PROJECT))

import pandas as pd
import numpy as np

from utils.logging_config import setup_logging
setup_logging("reoptimize_parallel", level=logging.WARNING)

from utils.config import get_active_config
from strategies.mean_reversion import MeanReversion
from strategies.trend_following import TrendFollowing
from strategies.bb_squeeze import BBSqueeze
from strategies.opening_gap import OpeningGap
from strategies.dividend_capture import DividendCapture
from backtest.engine import BacktestEngine

STRAT_MAP = {
    'mean_reversion': MeanReversion,
    'trend_following': TrendFollowing,
    'bb_squeeze': BBSqueeze,
    'opening_gap': OpeningGap,
    'dividend_capture': DividendCapture,
}

# Default active strategies per market (overridable via config)
DEFAULT_ACTIVE = {
    'asx': ['mean_reversion', 'bb_squeeze', 'trend_following', 'opening_gap'],
    'sp500': ['mean_reversion', 'trend_following', 'opening_gap'],
}

DEFAULT_MARKET = 'asx'

# Per-market parameter grids — keeps sweeps market-appropriate
PARAM_GRIDS = {
    'asx': {
        'mean_reversion': {
            'rsi_oversold': [20, 25, 30, 35, 40, 45],
            'zscore_entry': [-3.0, -2.5, -2.0, -1.5, -1.0],
            'atr_stop_mult': [2.0, 2.5, 3.0, 3.5, 4.0],
            'profit_target_atr_mult': [1.0, 1.5, 2.0, 2.5, 3.0],
            'max_hold_days': [5, 7, 10, 15, 20],
        },
        'bb_squeeze': {
            'bb_std': [1.5, 2.0, 2.5, 3.0],
            'kc_atr_mult': [1.0, 1.5, 2.0, 2.5],
            'momentum_period': [10, 15, 20, 30, 40],
            'atr_stop_mult': [1.0, 1.5, 2.0, 2.5, 3.0],
            'trailing_stop_atr_mult': [1.5, 2.0, 2.5, 3.0],
            'max_hold_days': [5, 10, 15, 20, 25],
        },
        'trend_following': {
            'fast_ma': [5, 10, 15, 20],
            'slow_ma': [20, 30, 40, 50, 60],
            'pullback_pct': [0.01, 0.02, 0.03, 0.04, 0.05],
            'atr_stop_mult': [2.0, 2.5, 3.0, 3.5, 4.0],
            'max_hold_days': [10, 15, 20, 25, 30],
        },
        'opening_gap': {
            'gap_threshold': [-0.03, -0.025, -0.02, -0.015, -0.01],
            'ibs_confirm': [0.15, 0.2, 0.3, 0.4, 0.5],
            'rsi14_max': [30, 40, 50, 60, 70],
            'atr_stop_mult': [1.5, 2.0, 2.5, 3.0, 3.5],
            'max_hold_days': [3, 5, 7, 10, 15],
        },
        'dividend_capture': {
            'days_before_ex': [3, 5, 7, 10],
            'days_after_ex': [3, 5, 8, 12],
            'min_franking_pct': [0, 50, 75, 100],
            'min_grossed_up_yield': [0.5, 0.8, 1.0, 1.2, 1.5],
            'atr_stop_mult': [2.5, 3.0, 3.5, 4.0, 5.0],
        },
    },
    'sp500': {
        'mean_reversion': {
            'rsi_period': [2, 3, 5, 14],
            'rsi_oversold': [5, 10, 15, 20, 25, 30, 35, 40],
            'zscore_entry': [-3.0, -2.5, -2.0, -1.5, -1.0],
            'atr_stop_mult': [2.5, 3.0, 3.5, 4.0],
            'profit_target_atr_mult': [1.0, 1.5, 2.0, 2.5],
            'max_hold_days': [3, 5, 7, 10],
            'sma200_filter': [True, False],
            'ibs_max': [0.2, 0.25, 0.3, 1.0],
        },
        'trend_following': {
            'fast_ma': [5, 10, 15, 20],
            'slow_ma': [20, 30, 40, 50, 60],
            'pullback_pct': [0.01, 0.02, 0.03, 0.04, 0.05],
            'atr_stop_mult': [2.0, 2.5, 3.0, 3.5],
            'trailing_stop_atr_mult': [2.5, 3.0, 3.5, 4.0],
            'max_hold_days': [10, 15, 20, 25],
        },
        'opening_gap': {
            'gap_threshold': [-0.01, -0.015, -0.02, -0.025, -0.03],
            'ibs_confirm': [0.2, 0.25, 0.35, 0.5],
            'rsi14_max': [25, 30, 35, 40, 50],
            'vol_surge_threshold': [1.0, 1.2, 1.5],
            'atr_stop_mult': [1.5, 2.0, 2.5, 3.0],
            'max_hold_days': [2, 3, 5, 7],
            'sma_exit_period': [3, 5],
            'sma200_filter': [True, False],
        },
    },
}

def get_param_grids(market_id: str) -> dict:
    """Get parameter grids for a market, falling back to ASX defaults."""
    return PARAM_GRIDS.get(market_id, PARAM_GRIDS['asx'])

def get_active_strategies(market_id: str, config: dict) -> list:
    """Get list of active (enabled) strategy names from config."""
    strats = config.get('strategies', {})
    enabled = [s for s in STRAT_MAP if strats.get(s, {}).get('enabled', False)]
    if not enabled:
        # Fallback to market defaults
        enabled = DEFAULT_ACTIVE.get(market_id, DEFAULT_ACTIVE['asx'])
    return enabled

# ---------------------------------------------------------------------------
# Helpers (must be picklable / top-level for multiprocessing)
# ---------------------------------------------------------------------------

def load_full_universe(market_id: str = 'asx'):
    """Load all cached ticker data for a market.

    Reads from per-market cache dir: data/cache/{market_id}/
    Falls back to legacy flat cache for backward compatibility.
    Handles ticker naming conventions per market:
      - ASX: file stem 'BHP_AX' -> ticker 'BHP.AX'
      - US/SP500: file stem 'AAPL' -> ticker 'AAPL' (no suffix)
    """
    # Determine benchmark and valid universe to filter against
    try:
        from markets import get_market
        market = get_market(market_id)
        benchmark = market.benchmark_ticker
        yf_suffix = market.yfinance_suffix
        valid_universe = set(market.get_formatted_tickers())
        valid_universe.add(benchmark)  # benchmark loaded separately, but don't warn on it
    except (ImportError, KeyError):
        benchmark = 'IOZ.AX' if market_id == 'asx' else 'SPY'
        yf_suffix = '.AX' if market_id == 'asx' else ''
        valid_universe = None  # can't filter without market profile

    cache_dir = PROJECT / 'data' / 'cache' / market_id

    data_dict = {}
    for pf in sorted(cache_dir.glob('*.parquet')):
        # Convert filename to ticker based on market
        stem = pf.stem
        if yf_suffix:
            # ASX: BHP_AX.parquet -> BHP.AX
            suffix_under = yf_suffix.replace('.', '_')  # '.AX' -> '_AX'
            if not stem.endswith(suffix_under):
                continue  # Skip non-matching files (e.g. US files in flat cache)
            ticker = stem.replace(suffix_under, yf_suffix)
        else:
            # US: AAPL.parquet -> AAPL (no suffix)
            if '_AX' in stem:
                continue  # Skip ASX files in flat cache
            ticker = stem

        # Skip benchmark
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


def run_single(config, strat_name, data, active_strategies=None):
    cfg = copy.deepcopy(config)
    active = active_strategies or list(STRAT_MAP.keys())
    for s in active:
        if s in cfg.get('strategies', {}):
            cfg['strategies'][s]['enabled'] = (s == strat_name)
    strat = STRAT_MAP[strat_name](cfg)
    result = BacktestEngine(cfg).run_walkforward(data, [strat])
    m = result.metrics
    return {
        'trades': m['total_trades'], 'cagr': m['cagr'],
        'sharpe': m['sharpe'], 'max_dd': m['max_drawdown'],
        'pf': m.get('profit_factor', 0), 'wr': m.get('win_rate', 0),
        'total_pnl': m.get('total_pnl', 0),
    }


def run_combined(config, data, active_strategies=None):
    cfg = copy.deepcopy(config)
    active = active_strategies or list(STRAT_MAP.keys())
    strats = []
    for s in active:
        if cfg['strategies'].get(s, {}).get('enabled', False):
            strats.append(STRAT_MAP[s](cfg))
    result = BacktestEngine(cfg).run_walkforward(data, strats)
    m = result.metrics
    return {
        'trades': m['total_trades'], 'cagr': m['cagr'],
        'sharpe': m['sharpe'], 'max_dd': m['max_drawdown'],
        'pf': m.get('profit_factor', 0), 'wr': m.get('win_rate', 0),
        'total_pnl': m.get('total_pnl', 0),
    }


def score(r):
    """Robust scoring that penalises degenerate low-trade solutions.

    Key fixes over original:
    - Minimum 15 trades (was 3) — below this is statistically meaningless
    - Trade count scaling ramp: full credit at 50+ trades, linear ramp 15→50
    - PF capped at 4.0 to prevent inf from distorting the score
    - CAGR capped at ±50% to prevent outlier distortion
    """
    MIN_TRADES = 15
    FULL_TRADES = 50  # full credit threshold

    if r['trades'] < MIN_TRADES:
        return -999

    # Cap components to prevent inf/outlier distortion
    pf = min(r['pf'], 4.0) if r['pf'] != float('inf') else 4.0
    cagr = max(min(r['cagr'], 0.50), -0.50)
    sharpe = max(min(r['sharpe'], 3.0), -3.0)
    max_dd = r['max_dd']

    raw = sharpe * 2 + pf * 1 + cagr * 50 - max_dd * 10

    # Trade count scaling: linear ramp from MIN_TRADES to FULL_TRADES
    if r['trades'] < FULL_TRADES:
        trade_scale = (r['trades'] - MIN_TRADES) / (FULL_TRADES - MIN_TRADES)
        trade_scale = max(0.3, min(1.0, trade_scale))  # floor at 0.3
        raw *= trade_scale

    return round(raw, 4)


def apply_params(config, strat_name, params):
    cfg = copy.deepcopy(config)
    for k, v in params.items():
        cfg['strategies'][strat_name][k] = v
    return cfg


def get_current(config, strat_name, param):
    return config['strategies'][strat_name].get(param)


# ---------------------------------------------------------------------------
# Worker: evaluate a single (param, value) — called from ProcessPoolExecutor
# ---------------------------------------------------------------------------

def _eval_param_value(config, strat_name, best_params, param, val, data, active_strategies=None):
    """Evaluate one parameter value. Returns (param, val, metrics, score)."""
    test_p = {**best_params, param: val}
    test_cfg = apply_params(config, strat_name, test_p)
    try:
        r = run_single(test_cfg, strat_name, data, active_strategies=active_strategies)
        s = score(r)
        return (param, val, r, s, None)
    except Exception as e:
        return (param, val, None, -999, str(e))


# ---------------------------------------------------------------------------
# Optimise one strategy (runs in its own process)
# ---------------------------------------------------------------------------

def optimize_strategy_parallel(config, strat_name, data, n_workers=2,
                               market_id='asx', active_strategies=None):
    """Coordinate descent for one strategy, parallelising value evaluations."""
    grids = get_param_grids(market_id)
    grid = grids.get(strat_name, {})
    if not grid:
        print(f'[{strat_name.upper()}] No param grid for market {market_id}, skipping', flush=True)
        return {'strategy': strat_name, 'improved': False}, config
    best_params = {}
    for p in grid:
        best_params[p] = get_current(config, strat_name, p)

    tag = strat_name.upper()
    print(f'[{tag}] Starting optimisation | current params: {best_params}', flush=True)

    baseline = run_single(config, strat_name, data, active_strategies=active_strategies)
    best_score = score(baseline)
    print(f'[{tag}] Baseline: trades={baseline["trades"]} sharpe={baseline["sharpe"]:.3f} '
          f'pf={baseline["pf"]:.3f} cagr={baseline["cagr"]*100:.2f}% dd={baseline["max_dd"]*100:.2f}% '
          f'score={best_score}', flush=True)

    total_evals = 0
    improved = False
    sweep_log = []

    for pass_num in range(2):
        pass_improved = False
        print(f'[{tag}] === PASS {pass_num + 1} ===', flush=True)

        for param, values in grid.items():
            candidates = [v for v in values if v != best_params[param]]
            if not candidates:
                continue

            # Parallel evaluation of all candidate values for this param
            results_map = {}
            with ProcessPoolExecutor(max_workers=n_workers) as pool:
                futures = {
                    pool.submit(
                        _eval_param_value, config, strat_name,
                        best_params, param, val, data, active_strategies
                    ): val
                    for val in candidates
                }
                for fut in as_completed(futures):
                    p_name, val, r, s, err = fut.result()
                    results_map[val] = (r, s, err)

            # Pick the best from this sweep
            p_best_val = best_params[param]
            p_best_score = best_score

            for val in values:
                if val == best_params[param]:
                    continue
                r, s, err = results_map[val]
                total_evals += 1
                if err:
                    print(f'[{tag}]   {param}={val}: ERROR {err}', flush=True)
                    continue
                marker = ''
                if s > p_best_score:
                    marker = ' << BETTER'
                    p_best_score = s
                    p_best_val = val
                    improved = True
                    pass_improved = True
                print(f'[{tag}]   {param}={val}: t={r["trades"]} sh={r["sharpe"]:.3f} '
                      f'pf={r["pf"]:.3f} cagr={r["cagr"]*100:.2f}% dd={r["max_dd"]*100:.2f}% '
                      f'sc={s:.2f}{marker}', flush=True)
                sweep_log.append({
                    'pass': pass_num + 1, 'param': param, 'value': val,
                    'trades': r['trades'], 'sharpe': r['sharpe'],
                    'pf': r['pf'], 'cagr': r['cagr'], 'score': s,
                })

            if p_best_val != best_params[param]:
                print(f'[{tag}] >>> {param}: {best_params[param]} -> {p_best_val} '
                      f'(score: {best_score:.2f} -> {p_best_score:.2f})', flush=True)
                best_params[param] = p_best_val
                config = apply_params(config, strat_name, best_params)
                best_score = p_best_score

        if not pass_improved:
            print(f'[{tag}] Pass {pass_num + 1}: No improvement, stopping early', flush=True)
            break

    final = run_single(config, strat_name, data, active_strategies=active_strategies)
    result = {
        'strategy': strat_name,
        'baseline': baseline, 'baseline_score': score(baseline),
        'optimized': final, 'optimized_score': score(final),
        'best_params': best_params, 'iterations': total_evals,
        'improved': improved, 'sweep_log': sweep_log,
    }
    print(f'[{tag}] DONE | score: {result["baseline_score"]} -> {result["optimized_score"]} '
          f'| {total_evals} evals', flush=True)
    return result, config


# ---------------------------------------------------------------------------
# Top-level worker for strategy-parallel optimisation
# ---------------------------------------------------------------------------

def _optimize_one_strategy(args):
    """Top-level worker: optimise a single strategy (used by strategy-level parallelism)."""
    config, strat_name, data, market_id, active_strategies, inner_workers = args
    result, updated_config = optimize_strategy_parallel(
        config, strat_name, data, n_workers=inner_workers,
        market_id=market_id, active_strategies=active_strategies,
    )
    # Extract only this strategy's params from the updated config
    best_params = result.get('best_params', {})
    return strat_name, result, best_params


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def parse_args():
    parser = argparse.ArgumentParser(description="Parallel full-universe reoptimization")
    parser.add_argument('--market', type=str, default=DEFAULT_MARKET,
                        help='Market to optimize (asx, sp500, etc.)')
    parser.add_argument('--candidate-path', type=str, default=None)
    parser.add_argument('--results-path', type=str, default=None)
    parser.add_argument('--backup-path', type=str, default=None)
    parser.add_argument('--promote-active', action='store_true')
    parser.add_argument('--workers', type=int, default=5,
                        help='Number of strategies to optimise in parallel (default: 5)')
    parser.add_argument('--inner-workers', type=int, default=None,
                        help='Param-sweep workers per strategy (default: auto = cpu_count // n_strategy_workers). '
                             'Override for manual core tuning.')
    return parser.parse_args()


def resolve_output_path(path_str, default_path):
    if not path_str:
        return Path(default_path)
    p = Path(path_str)
    if not p.is_absolute():
        p = PROJECT / p
    return p


def default_candidate_path():
    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    return PROJECT / 'config' / f'config_candidate_reoptimized_{ts}.json'


def default_backup_path():
    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    return PROJECT / 'config' / 'versions' / f'active_config_pre_reopt_{ts}.json'


if __name__ == '__main__':
    args = parse_args()
    market_id = args.market.lower().strip()
    config = get_active_config(market_id)
    ACTIVE = get_active_strategies(market_id, config)

    n_strategy_workers = min(args.workers, len(ACTIVE))

    # Auto-calculate inner (param-sweep) workers per strategy
    cpu_cores = os.cpu_count() or 8
    if args.inner_workers is not None:
        inner_workers = max(1, args.inner_workers)
    else:
        inner_workers = max(1, cpu_cores // max(1, n_strategy_workers))
    RESULTS_FILE = PROJECT / 'backtest' / 'results' / f'reoptimization_{market_id}.json'
    results_path = resolve_output_path(args.results_path, RESULTS_FILE)

    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    default_cand = PROJECT / 'config' / 'candidates' / f'config_candidate_{market_id}_{ts}.json'
    default_bak = PROJECT / 'config' / 'versions' / f'active_config_pre_reopt_{market_id}_{ts}.json'
    candidate_config_path = resolve_output_path(args.candidate_path, default_cand)
    backup_config_path = resolve_output_path(args.backup_path, default_bak)

    active_config_file = f'{market_id}.json'

    print('=' * 70, flush=True)
    print(f'ATLAS FULL-UNIVERSE RE-OPTIMIZATION — {market_id.upper()} (PARALLEL)', flush=True)
    print(f'Started: {datetime.now().isoformat()}', flush=True)
    print(f'Market: {market_id}', flush=True)
    print(f'Active strategies: {ACTIVE}', flush=True)
    print(f'Workers: {n_strategy_workers} strategy-level × {inner_workers} inner = '
          f'{n_strategy_workers * inner_workers} total cores', flush=True)
    print(f'Results file: {results_path}', flush=True)
    print(f'Candidate config target: {candidate_config_path}', flush=True)
    print(f'Promote active config: {args.promote_active}', flush=True)
    print('=' * 70, flush=True)

    t_global = time.time()
    data = load_full_universe(market_id)
    print(f'Loaded {len(data)} tickers for {market_id} (full universe, >= 100 rows)', flush=True)

    # Backup current active config
    backup_config_path.parent.mkdir(parents=True, exist_ok=True)
    active_path = PROJECT / 'config' / 'active' / active_config_file
    if active_path.exists():
        shutil.copy2(active_path, backup_config_path)
        print(f'Saved pre-optimization config backup to {backup_config_path}', flush=True)

    # Baseline combined
    print(f'\n{"=" * 70}', flush=True)
    print('BASELINE COMBINED (all strategies)', flush=True)
    print(f'{"=" * 70}', flush=True)
    t0 = time.time()
    bl = run_combined(config, data, active_strategies=ACTIVE)
    print(f'Baseline ({time.time() - t0:.0f}s): trades={bl["trades"]} '
          f'CAGR={bl["cagr"]*100:.2f}% Sharpe={bl["sharpe"]:.3f} '
          f'PF={bl["pf"]:.3f} DD={bl["max_dd"]*100:.2f}%', flush=True)

    results_tracker = {
        'timestamp': datetime.now().isoformat(),
        'market_id': market_id,
        'n_tickers': len(data),
        'active_strategies': ACTIVE,
        'baseline_combined': bl,
        'results_path': str(results_path),
        'backup_config_path': str(backup_config_path),
        'active_config_path': str(active_path),
        'candidate_config_path': str(candidate_config_path),
        'active_config_overwritten': False,
        'parallel_workers': n_strategy_workers,
        'inner_workers': inner_workers,
        'cpu_cores': cpu_cores,
    }
    results_path.parent.mkdir(parents=True, exist_ok=True)
    with open(results_path, 'w') as f:
        json.dump(results_tracker, f, indent=2, default=str)

    # ---------- Parallel strategy optimisation ----------
    print(f'\n{"=" * 70}', flush=True)
    print(f'OPTIMISING {len(ACTIVE)} STRATEGIES IN PARALLEL ({n_strategy_workers} workers)', flush=True)
    print(f'{"=" * 70}', flush=True)

    strategy_args = [
        (copy.deepcopy(config), sn, data, market_id, ACTIVE, inner_workers)
        for sn in ACTIVE
    ]

    with ProcessPoolExecutor(max_workers=n_strategy_workers) as pool:
        futures = {pool.submit(_optimize_one_strategy, sa): sa[1] for sa in strategy_args}
        strategy_results = {}
        for fut in as_completed(futures):
            sn = futures[fut]
            try:
                strat_name, result, best_params = fut.result()
                strategy_results[strat_name] = (result, best_params)
                results_tracker[strat_name] = result
                print(f'\n✅ {strat_name.upper()} complete — '
                      f'score: {result.get("baseline_score", "?")} -> {result.get("optimized_score", "?")}', flush=True)
            except Exception as e:
                print(f'\n❌ {sn.upper()} FAILED: {e}', flush=True)
                import traceback; traceback.print_exc()

    # Merge optimised params into config
    for sn in ACTIVE:
        if sn in strategy_results:
            _, best_params = strategy_results[sn]
            for k, v in best_params.items():
                config['strategies'][sn][k] = v

    # Final combined with merged optimised params
    print(f'\n{"=" * 70}', flush=True)
    print('FINAL COMBINED (optimised config)', flush=True)
    print(f'{"=" * 70}', flush=True)
    t0 = time.time()
    final_combined = run_combined(config, data, active_strategies=ACTIVE)
    elapsed = time.time() - t0
    print(
        f'Final ({elapsed:.0f}s): trades={final_combined["trades"]} '
        f'CAGR={final_combined["cagr"]*100:.2f}% '
        f'Sharpe={final_combined["sharpe"]:.3f} PF={final_combined["pf"]:.3f} '
        f'DD={final_combined["max_dd"]*100:.2f}%',
        flush=True,
    )

    # Save artifacts
    results_tracker['final_combined'] = final_combined
    results_tracker['candidate_config_path'] = str(candidate_config_path)
    results_tracker['active_config_overwritten'] = bool(args.promote_active)
    results_tracker['finished_at'] = datetime.now().isoformat()
    results_tracker['total_runtime_s'] = round(time.time() - t_global, 1)

    candidate_config_path.parent.mkdir(parents=True, exist_ok=True)
    with open(candidate_config_path, 'w') as f:
        json.dump(config, f, indent=2)
    print(f'Saved optimised candidate config to {candidate_config_path}', flush=True)

    if args.promote_active:
        with open(active_path, 'w') as f:
            json.dump(config, f, indent=2)
        print(f'Promoted optimised config to {active_path}', flush=True)
    else:
        print('Active config not modified (staged candidate only)', flush=True)

    with open(results_path, 'w') as f:
        json.dump(results_tracker, f, indent=2, default=str)

    total_elapsed = time.time() - t_global
    print(f'\n{"=" * 70}', flush=True)
    print(f'REOPTIMISATION COMPLETE [{market_id.upper()}] — {total_elapsed:.0f}s total ({total_elapsed/60:.1f} min)', flush=True)
    print(f'{"=" * 70}', flush=True)

    # Summary comparison
    print(f'\n  Baseline:  CAGR={bl["cagr"]*100:.2f}%  Sharpe={bl["sharpe"]:.3f}  PF={bl["pf"]:.3f}  DD={bl["max_dd"]*100:.2f}%')
    print(f'  Optimised: CAGR={final_combined["cagr"]*100:.2f}%  Sharpe={final_combined["sharpe"]:.3f}  PF={final_combined["pf"]:.3f}  DD={final_combined["max_dd"]*100:.2f}%')
    delta_cagr = (final_combined["cagr"] - bl["cagr"]) * 100
    print(f'  Delta:     CAGR {delta_cagr:+.2f}pp  Sharpe {final_combined["sharpe"]-bl["sharpe"]:+.3f}  PF {final_combined["pf"]-bl["pf"]:+.3f}')
