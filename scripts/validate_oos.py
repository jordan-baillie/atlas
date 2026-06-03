#!/usr/bin/env python3
"""Atlas Out-of-Sample Validation — Cross-OOS battery (replaces the legacy 3-test suite).

The AUTHORITATIVE verdict is now the strategy-agnostic cross-OOS battery
(``research.cross_oos``): CPCV path distribution, PBO (CSCV), Deflated Sharpe,
leave-one-ticker-group-out + concentration, and regime stratification, scored against
the equities-tuned ``adapter.ATLAS_DEFAULT_GATES`` (missing measurement == FAIL).

The battery reuses the existing BacktestEngine output — it does not replace the
backtester. The full-period run supplies the daily return series + trade attribution;
a grid of config variants supplies the PBO/DSR matrix; an IS/OOS time split supplies
the forward-holdout + degradation gates.

Back-compat: the legacy JSON keys (``test1_time_period_split``, ``test2_perturbation``,
``test3_walkforward_consistency``, ``summary.overall_verdict``) are still emitted as
derived projections so existing consumers (research/promoter.py, scripts/auto_reoptimize.py,
and the compiled TS risk-gate + artifact-summarizer extensions) keep working unchanged.
  - test1 (time split) and test3 (walk-forward windows) are REAL measurements.
  - test2.robust is projected from the modern overfitting controls (PBO + DSR).
  - summary.overall_verdict == 'PASS' iff the cross-OOS gate battery passes.

Expected runtime: ~60-90 minutes (full + IS + OOS + grid backtests, ~280-360s each).
"""
import json, sys, time, copy, random, datetime, argparse
from pathlib import Path
import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from backtest.engine import BacktestEngine
# Strategies are instantiated via scripts.strategy_evaluator.STRATEGY_REGISTRY inside
# make_strategies() so validation covers any enabled strategy in the config.
from research.cross_oos import adapter

# ============================================================
# CONSTANTS
# ============================================================
DATA_DIR = PROJECT_ROOT / 'data' / 'cache'
DEFAULT_CONFIG_PATH = PROJECT_ROOT / 'config' / 'active' / 'sp500.json'
DEFAULT_OUTPUT_PATH = PROJECT_ROOT / 'backtest' / 'results' / 'v92_oos_validation.json'
# SPLIT_DATE and WARMUP_DATE are now computed dynamically in main() from data.
# Kept as fallback defaults only.
_FALLBACK_SPLIT_DATE = '2025-06-01'
MIN_ROWS = 60
N_PERTURBATION_TRIALS = 10
PERTURB_MIN = 0.8
PERTURB_MAX = 1.2
RANDOM_SEED = 42

# OPTIMIZED_PARAMS is no longer hardcoded — it is extracted from the validated
# config file at runtime by extract_perturbable_params().  The old ASX-specific
# dict has been removed to make the script market-agnostic.


def load_data(market='sp500'):
    """Load all parquet data files for the given market."""
    data_dir = DATA_DIR / market if market else DATA_DIR
    if not data_dir.exists():
        data_dir = DATA_DIR  # fallback to legacy flat layout
    data_dict = {}
    for pf in sorted(data_dir.glob('*.parquet')):
        stem = pf.stem
        # Market-aware ticker conversion
        if market in ('asx', 'au'):
            if stem == 'IOZ_AX':
                continue
            ticker = stem.replace('_AX', '.AX')
        else:
            ticker = stem
        df = pd.read_parquet(pf)
        df.columns = [c.lower() for c in df.columns]
        if 'date' in df.columns:
            df['date'] = pd.to_datetime(df['date'])
            df = df.set_index('date')
        df.index = pd.to_datetime(df.index)
        data_dict[ticker] = df
    return data_dict


def resolve_path(path_value, default_path):
    p = Path(path_value) if path_value else Path(default_path)
    if not p.is_absolute():
        p = PROJECT_ROOT / p
    return p


def parse_args():
    parser = argparse.ArgumentParser(
        description="Run OOS validation against a specified config JSON and output JSON path."
    )
    parser.add_argument(
        '--config-path',
        type=str,
        default=None,
        help='Config JSON to validate (default: config/active/asx.json)',
    )
    parser.add_argument(
        '--output-path',
        type=str,
        default=None,
        help='Output path for validation JSON (default: backtest/results/v92_oos_validation.json)',
    )
    parser.add_argument(
        '--market',
        type=str,
        default=None,
        help=(
            'Market identifier (e.g. asx, sp500). '
            'Defaults to value in config JSON, then to market inferred from --config-path.'
        ),
    )
    parser.add_argument(
        '--grid-size',
        type=int,
        default=N_PERTURBATION_TRIALS,
        help=('Number of perturbed config variants to run for the PBO/DSR config grid '
              f'(default {N_PERTURBATION_TRIALS}). The grid doubles as the legacy '
              'perturbation projection.'),
    )
    return parser.parse_args()


def load_config(config_path):
    """Load config JSON."""
    with open(config_path) as f:
        return json.load(f)


def detect_market(args_market, config_path, cfg):
    """Determine the market identifier.

    Priority:
        1. --market CLI flag
        2. cfg['market'] field in config JSON
        3. Inferred from config path (directory or filename containing 'asx' / 'sp500')
        4. Default: 'asx'
    """
    if args_market:
        return args_market.lower()
    if cfg.get('market'):
        return cfg['market'].lower()
    path_str = str(config_path).lower()
    for candidate in ('sp500', 'nasdaq', 'us', 'asx', 'au'):
        if candidate in path_str:
            return candidate
    return 'sp500'


def compute_split_dates(data_all):
    """Derive IS/OOS split from data: use the last 20% of the date range.

    Returns (split_date_str, warmup_date_str) where warmup_date is 90 days
    before split_date.
    """
    all_dates = []
    for df in data_all.values():
        all_dates.extend(df.index.tolist())
    if not all_dates:
        warmup_ts = pd.Timestamp(_FALLBACK_SPLIT_DATE) - datetime.timedelta(days=90)
        return _FALLBACK_SPLIT_DATE, warmup_ts.strftime('%Y-%m-%d')

    min_date = min(all_dates)
    max_date = max(all_dates)
    total_days = (max_date - min_date).days
    # Split at 80% of date range
    split_offset = int(total_days * 0.80)
    split_ts = min_date + datetime.timedelta(days=split_offset)
    warmup_ts = split_ts - datetime.timedelta(days=90)
    return split_ts.strftime('%Y-%m-%d'), warmup_ts.strftime('%Y-%m-%d')


def extract_perturbable_params(cfg):
    """Extract numeric strategy parameters from the config for perturbation.

    Reads all enabled strategy sections from cfg['strategies'] and extracts
    every top-level numeric (int/float) param, skipping booleans and 'enabled'.
    Returns a dict compatible with the shape expected by perturb_params().
    """
    result = {}
    for strat_name, strat_cfg in cfg.get('strategies', {}).items():
        if not isinstance(strat_cfg, dict):
            continue
        if not strat_cfg.get('enabled', True):
            continue
        numeric_params = {}
        for k, v in strat_cfg.items():
            if k == 'enabled':
                continue
            if isinstance(v, bool):
                continue
            if isinstance(v, (int, float)):
                numeric_params[k] = v
        if numeric_params:
            result[strat_name] = numeric_params
    return result


def make_strategies(cfg):
    """Instantiate every enabled strategy from config using the canonical registry.

    Uses scripts.strategy_evaluator.STRATEGY_REGISTRY (the same name->class map the
    live/research paths use) so validation covers ANY enabled strategy — including
    momentum_breakout, mtf_momentum, connors_rsi2, sector_rotation, short_term_mr —
    not just the four originally hardcoded here. A strategy is included when its
    config section is missing an 'enabled' flag (treated as enabled) or has
    enabled=True; enabled=False is skipped.
    """
    from scripts.strategy_evaluator import STRATEGY_REGISTRY
    strategies = []
    strats = cfg.get('strategies', {})
    for name, strat_cfg in strats.items():
        if not isinstance(strat_cfg, dict):
            continue
        if not strat_cfg.get('enabled', True):
            continue
        cls = STRATEGY_REGISTRY.get(name)
        if cls is None:
            print(f"  [warn] strategy '{name}' enabled in config but not in "
                  f"STRATEGY_REGISTRY {list(STRATEGY_REGISTRY.keys())}; skipping")
            continue
        strategies.append(cls(cfg))
    return strategies


def run_backtest(cfg, data, label=''):
    """Run walk-forward backtest, return result and elapsed time."""
    t0 = time.time()
    strategies = make_strategies(cfg)
    engine = BacktestEngine(cfg)
    result = engine.run_walkforward(data, strategies)
    elapsed = time.time() - t0
    m = result.metrics
    # Normalize CAGR
    cagr = m.get('cagr', 0)
    cagr_pct = cagr * 100 if abs(cagr) < 2 else cagr
    print(f"  [{label}] Trades={m.get('total_trades',0)} "
          f"CAGR={cagr_pct:.2f}% Sharpe={m.get('sharpe',0):.4f} "
          f"PF={m.get('profit_factor',0):.4f} MaxDD={m.get('max_drawdown',0)*100:.2f}% "
          f"WR={m.get('win_rate',0)*100:.1f}% PnL=${m.get('total_pnl',0):.2f} "
          f"({elapsed:.0f}s)")
    return result, elapsed


def extract_metrics(result):
    """Extract key metrics dict from BacktestResult."""
    m = result.metrics
    cagr = m.get('cagr', 0)
    cagr_pct = cagr * 100 if abs(cagr) < 2 else cagr
    return {
        'total_trades': m.get('total_trades', 0),
        'cagr_pct': round(cagr_pct, 4),
        'sharpe': round(m.get('sharpe', 0), 4),
        'profit_factor': round(m.get('profit_factor', 0), 4),
        'max_drawdown_pct': round(m.get('max_drawdown', 0) * 100, 4),
        'win_rate_pct': round(m.get('win_rate', 0) * 100, 2),
        'total_pnl': round(m.get('total_pnl', 0), 2),
        'sortino': round(m.get('sortino', 0), 4),
        'avg_trade': round(m.get('avg_trade', 0), 2),
        'final_equity': round(m.get('final_equity', 0), 2),
    }


def perturb_params(cfg, seed, params_dict=None):
    """Create a copy of cfg with randomly perturbed strategy parameters.

    Args:
        cfg: Full config dict.
        seed: RNG seed for reproducibility.
        params_dict: Dict of {strategy_name: {param_name: original_value}} to
                     perturb.  Defaults to extract_perturbable_params(cfg) so
                     that the params come from the config being validated rather
                     than a hardcoded ASX-specific dict.
    """
    if params_dict is None:
        params_dict = extract_perturbable_params(cfg)

    rng = random.Random(seed)
    cfg_new = copy.deepcopy(cfg)
    perturbed_log = {}

    for strat_name, params in params_dict.items():
        strat_cfg = cfg_new.get('strategies', {}).get(strat_name, {})
        if not isinstance(strat_cfg, dict):
            continue
        perturbed_log[strat_name] = {}
        for param_name, orig_val in params.items():
            factor = rng.uniform(PERTURB_MIN, PERTURB_MAX)
            new_val = orig_val * factor
            # Preserve type: round integers
            if isinstance(orig_val, int):
                new_val = max(1, round(new_val))
            else:
                new_val = round(new_val, 4)
            strat_cfg[param_name] = new_val
            perturbed_log[strat_name][param_name] = {
                'original': orig_val,
                'factor': round(factor, 4),
                'perturbed': new_val,
            }
    return cfg_new, perturbed_log


def analyze_walk_forward_windows(result):
    """Analyze per-window metrics for consistency."""
    windows = result.walk_forward_windows
    if not windows:
        return {'error': 'No walk-forward windows found'}

    window_returns = []
    for w in windows:
        eq_start = w.get('equity_start', 0)
        eq_end = w.get('equity_end', 0)
        if eq_start > 0:
            ret = (eq_end - eq_start) / eq_start
        else:
            ret = 0.0
        window_returns.append(ret)

    window_pnls = [w.get('pnl', 0) for w in windows]
    window_trades = [w.get('trades', 0) for w in windows]

    n_positive = sum(1 for r in window_returns if r > 0)
    n_negative = sum(1 for r in window_returns if r <= 0)

    analysis = {
        'n_windows': len(windows),
        'n_positive_windows': n_positive,
        'n_negative_windows': n_negative,
        'win_rate_windows_pct': round(n_positive / len(windows) * 100, 1) if windows else 0,
        'mean_window_return_pct': round(np.mean(window_returns) * 100, 4),
        'std_window_return_pct': round(np.std(window_returns) * 100, 4),
        'min_window_return_pct': round(min(window_returns) * 100, 4),
        'max_window_return_pct': round(max(window_returns) * 100, 4),
        'median_window_return_pct': round(np.median(window_returns) * 100, 4),
        'mean_window_pnl': round(np.mean(window_pnls), 2),
        'std_window_pnl': round(np.std(window_pnls), 2),
        'mean_trades_per_window': round(np.mean(window_trades), 1),
        'total_trades_across_windows': sum(window_trades),
        'per_window_detail': [
            {
                'window': w.get('window', i),
                'test_start': str(w.get('test_start', ''))[:10],
                'test_end': str(w.get('test_end', ''))[:10],
                'trades': w.get('trades', 0),
                'pnl': round(w.get('pnl', 0), 2),
                'return_pct': round(window_returns[i] * 100, 4),
                'equity_start': round(w.get('equity_start', 0), 2),
                'equity_end': round(w.get('equity_end', 0), 2),
            }
            for i, w in enumerate(windows)
        ],
    }
    return analysis


def result_daily_returns(result):
    """Daily fractional returns from a BacktestResult's mark-to-market equity curve."""
    return adapter.daily_returns(getattr(result, 'equity_curve', None))


def main():
    args = parse_args()
    config_path = resolve_path(args.config_path, DEFAULT_CONFIG_PATH)
    output_path = resolve_path(args.output_path, DEFAULT_OUTPUT_PATH)
    grid_size = max(2, int(getattr(args, 'grid_size', N_PERTURBATION_TRIALS)))
    overall_start = time.time()

    # ----------------------------------------------------------
    # Load config and detect market
    # ----------------------------------------------------------
    cfg = load_config(config_path)
    market = detect_market(args.market, config_path, cfg)

    print("=" * 70)
    print(f"ATLAS {market.upper()} CROSS-OOS VALIDATION (battery)")
    print("=" * 70)
    print(f"\nLoading data from {DATA_DIR / market}...")
    data_all = load_data(market=market)
    print(f"Loaded {len(data_all)} tickers")
    print(f"Config: {cfg.get('version', 'unknown')}")
    print(f"Config path: {config_path}")
    print(f"Market: {market}")

    # ----------------------------------------------------------
    # Derive split and warmup dates from actual data date range
    # ----------------------------------------------------------
    SPLIT_DATE, WARMUP_DATE = compute_split_dates(data_all)
    print(f"Split date (80/20): {SPLIT_DATE}")
    print(f"Warmup date for OOS (split - 90d): {WARMUP_DATE}")
    print(f"PBO/DSR config grid size: {grid_size}")

    optimized_params = extract_perturbable_params(cfg)
    print(f"Perturbable strategy params: {list(optimized_params.keys())}")

    results = {
        'validation_type': 'cross_oos_battery_v1',
        'timestamp': datetime.datetime.now().isoformat(),
        'config_version': cfg.get('version', 'unknown'),
        'config_path': str(config_path),
        'output_path': str(output_path),
        'market': market,
        'split_date': SPLIT_DATE,
        'warmup_date': WARMUP_DATE,
        'grid_size': grid_size,
        'n_perturbation_trials': grid_size,  # legacy alias
        'perturbation_range': [PERTURB_MIN, PERTURB_MAX],
        'gate_table': 'adapter.ATLAS_DEFAULT_GATES',
    }

    data_all = {k: v for k, v in data_all.items() if len(v) >= MIN_ROWS}

    # ----------------------------------------------------------
    # Full-period primary run (supplies the daily return series + trade attribution)
    # ----------------------------------------------------------
    print("\n" + "-" * 70)
    print("PRIMARY: Full-period walk-forward backtest")
    print("-" * 70)
    result_full, t_full = run_backtest(cfg, data_all, label='FULL')
    m_full = extract_metrics(result_full)
    primary_returns = result_daily_returns(result_full)
    print(f"  primary daily-return observations: {len(primary_returns)}")

    # ----------------------------------------------------------
    # Time split (forward holdout + IS->OOS degradation gates)
    # ----------------------------------------------------------
    print("\n" + "-" * 70)
    print("TIME SPLIT: in-sample vs out-of-sample (forward holdout)")
    print("-" * 70)
    split_ts = pd.Timestamp(SPLIT_DATE)
    warmup_ts = pd.Timestamp(WARMUP_DATE)
    data_is = {
        k: v[v.index < split_ts] for k, v in data_all.items()
        if len(v[v.index < split_ts]) >= MIN_ROWS
    }
    data_oos = {
        k: v[v.index >= warmup_ts] for k, v in data_all.items()
        if len(v[v.index >= warmup_ts]) >= MIN_ROWS
    }
    print(f"In-sample tickers: {len(data_is)} | OOS tickers (warmup incl.): {len(data_oos)}")
    result_is, t_is = run_backtest(cfg, data_is, label='IS')
    result_oos, t_oos = run_backtest(cfg, data_oos, label='OOS')
    m_is = extract_metrics(result_is)
    m_oos = extract_metrics(result_oos)

    degradation = {}
    for key in ('cagr_pct', 'sharpe', 'profit_factor', 'win_rate_pct'):
        is_val = m_is.get(key, 0)
        oos_val = m_oos.get(key, 0)
        if is_val and abs(is_val) > 1e-9:
            degradation[key] = round(((oos_val - is_val) / abs(is_val)) * 100, 2)
        else:
            degradation[key] = None

    # ----------------------------------------------------------
    # Config grid (cross-CONFIG axis: PBO + DSR). Doubles as the legacy
    # perturbation projection. cfg0 == primary (unperturbed).
    # ----------------------------------------------------------
    print("\n" + "-" * 70)
    print(f"CONFIG GRID: {grid_size} perturbed variants for PBO / DSR")
    print("-" * 70)
    random.seed(RANDOM_SEED)
    grid_returns = {'cfg0_primary': primary_returns}
    grid_metrics = []
    for i in range(grid_size):
        seed = RANDOM_SEED + i
        cfg_perturbed, perturbation_log = perturb_params(cfg, seed, params_dict=optimized_params)
        result_p, elapsed_p = run_backtest(cfg_perturbed, data_all, label=f'GRID-{i+1}')
        grid_returns[f'cfg{i+1}'] = result_daily_returns(result_p)
        m_p = extract_metrics(result_p)
        m_p['trial'] = i + 1
        m_p['seed'] = seed
        m_p['runtime_s'] = round(elapsed_p, 1)
        m_p['perturbation_log'] = perturbation_log
        grid_metrics.append(m_p)

    # ----------------------------------------------------------
    # Cross-OOS battery (AUTHORITATIVE verdict)
    # ----------------------------------------------------------
    print("\n" + "-" * 70)
    print("CROSS-OOS BATTERY: CPCV / PBO / DSR / leave-one-group-out / regime")
    print("-" * 70)
    forward_net = m_oos.get('total_pnl', 0.0)
    oos_cagr_deg = degradation.get('cagr_pct')
    battery = adapter.assemble_bundle(
        primary_returns,
        getattr(result_full, 'trades', []) or [],
        grid_returns=grid_returns,
        forward_net=forward_net,
        oos_cagr_degradation_pct=oos_cagr_deg,
    )
    gate_report = adapter.evaluate(battery['bundle'])
    overall_pass = bool(gate_report['overall_pass'])

    results['cross_oos'] = {
        'bundle': battery['bundle'],
        'diagnostics': battery['diagnostics'],
        'gate_checks': {
            g.name: {
                'value': g.value, 'threshold': g.threshold,
                'comparator': g.comparator, 'status': g.status,
                'description': g.description,
            } for g in gate_report['gates']
        },
        'gate_summary': {
            'pass': gate_report['n_pass'], 'fail': gate_report['n_fail'],
            'missing': gate_report['n_missing'],
        },
        'verdict': 'PASS' if overall_pass else 'FAIL',
    }

    # ----------------------------------------------------------
    # Legacy projections (back-compat for existing consumers)
    # test1 (time split) and test3 (walk-forward) are REAL measurements;
    # test2.robust is projected from the PBO + DSR overfitting controls.
    # ----------------------------------------------------------
    results['test1_time_period_split'] = {
        'in_sample': m_is,
        'out_of_sample': m_oos,
        'degradation_pct': degradation,
        'full_metrics': m_full,
        'runtime_s': round(t_is + t_oos + t_full, 1),
    }

    window_analysis = analyze_walk_forward_windows(result_full)
    results['test3_walkforward_consistency'] = {
        'full_metrics': m_full,
        'window_analysis': window_analysis,
        'runtime_s': round(t_full, 1),
    }

    def summarize_numeric(field):
        vals = [t[field] for t in grid_metrics if isinstance(t.get(field), (int, float))]
        if not vals:
            return {'mean': None, 'std': None, 'min': None, 'max': None}
        return {
            'mean': round(float(np.mean(vals)), 4),
            'std': round(float(np.std(vals)), 4),
            'min': round(float(np.min(vals)), 4),
            'max': round(float(np.max(vals)), 4),
        }

    perturb_summary = {
        'cagr_pct': summarize_numeric('cagr_pct'),
        'sharpe': summarize_numeric('sharpe'),
        'profit_factor': summarize_numeric('profit_factor'),
        'max_drawdown_pct': summarize_numeric('max_drawdown_pct'),
        'total_trades': summarize_numeric('total_trades'),
    }
    collapse_count = sum(1 for t in grid_metrics if (t.get('cagr_pct') or 0) < 0)
    pbo_val = battery['bundle'].get('pbo')
    dsr_val = battery['bundle'].get('dsr')
    pbo_ok = isinstance(pbo_val, (int, float)) and pbo_val == pbo_val and pbo_val <= 0.50
    dsr_ok = isinstance(dsr_val, (int, float)) and dsr_val == dsr_val and dsr_val >= 0.90
    robust = bool(pbo_ok and dsr_ok
                  and collapse_count < max(3, int(grid_size * 0.3)))
    results['test2_perturbation'] = {
        'summary': perturb_summary,
        'trials': grid_metrics,
        'collapse_count': collapse_count,
        'robust': robust,
        'pbo': pbo_val,
        'deflated_sharpe': dsr_val,
        'note': ('robust projected from PBO+DSR overfitting controls; replaces the legacy '
                 'perturbation-collapse heuristic. Grid runs double as the PBO/DSR config grid.'),
    }

    # ----------------------------------------------------------
    # Verdicts (authoritative = cross-OOS battery)
    # ----------------------------------------------------------
    oos_sharpe = m_oos.get('sharpe', 0) or 0
    oos_pf = m_oos.get('profit_factor', 0) or 0
    cagr_deg = degradation.get('cagr_pct')
    test1_fail = (
        (cagr_deg is not None and cagr_deg < -50)
        or oos_sharpe < 0
        or oos_pf < 1.0
    )
    test1_verdict = 'FAIL - significant OOS degradation' if test1_fail else 'PASS'
    test2_verdict = 'PASS' if robust else 'FAIL - overfitting controls (PBO/DSR) not satisfied'
    win_rate_windows = window_analysis.get('win_rate_windows_pct') if isinstance(window_analysis, dict) else None
    test3_pass = isinstance(win_rate_windows, (int, float)) and win_rate_windows >= 50
    test3_verdict = 'PASS - majority profitable' if test3_pass else 'FAIL - inconsistent windows'

    # The battery is authoritative: overall PASS iff every enforced gate passes.
    overall_verdict = 'PASS' if overall_pass else 'FAIL'

    total_runtime_s = round(time.time() - overall_start, 1)
    results['summary'] = {
        'overall_verdict': overall_verdict,
        'cross_oos_verdict': results['cross_oos']['verdict'],
        'gate_summary': results['cross_oos']['gate_summary'],
        'test1_verdict': test1_verdict,
        'test2_verdict': test2_verdict,
        'test3_verdict': test3_verdict,
        'total_runtime_s': total_runtime_s,
        'total_runtime_min': round(total_runtime_s / 60, 1),
    }

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, 'w') as f:
        json.dump(results, f, indent=2, default=str)

    print("\n" + "=" * 70)
    print("CROSS-OOS VALIDATION SUMMARY")
    print("=" * 70)
    b = battery['bundle']
    print("  gates:", {k: v['status'] for k, v in results['cross_oos']['gate_checks'].items()})
    print(f"  CPCV median Sharpe: {b.get('median_cpcv_sharpe')}, "
          f"frac+ : {b.get('frac_paths_positive')}")
    print(f"  PBO: {b.get('pbo')} | DSR: {b.get('dsr')} | "
          f"top_ticker_frac: {b.get('top_group_frac')}")
    print(f"  min regime Sharpe: {b.get('min_regime_sharpe')} | "
          f"max regime PnL frac: {b.get('max_regime_pnl_frac')}")
    print(f"  forward_net: {b.get('forward_net')} | loo_group_ok: {b.get('loo_group_ok')}")
    print(f"  AUTHORITATIVE VERDICT: {overall_verdict}")
    print(f"  (legacy projections) test1={test1_verdict} test2={test2_verdict} test3={test3_verdict}")
    print(f"Saved: {output_path}")
    print(f"Runtime: {total_runtime_s:.1f}s ({total_runtime_s/60:.1f} min)")
    return 0


if __name__ == '__main__':
    sys.exit(main())
