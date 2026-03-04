#!/usr/bin/env python3
"""Atlas Research Runner — Experiment Execution Engine

The main orchestrator that:
1. Reads next experiment from research/queue.json (highest priority, status=queued)
2. Claims experiment (prevents double-pickup in multi-agent future)
3. Dispatches to the right execution method based on experiment type
4. Enforces experiment budget (max 4h, kills long-running jobs)
5. Updates queue status through state machine
6. Writes self-contained experiment envelope to research/experiments/
7. Appends summary to research/journal.json (append-only, file lock)

Experiment Types:
    single_strategy_test  → strategy_evaluator.py --strategy X --market Y
    combined_portfolio_test → strategy_evaluator.py --combined
    param_sweep           → coordinate descent on single param
    full_optimization     → reoptimize_parallel.py --market X
    oos_validation        → validate_oos.py --config-path candidate
    filter_test           → backtest with/without filter, compare
    reoptimization        → reoptimize_parallel.py --market X

Usage:
    python3 scripts/research_runner.py                     # Run next queued experiment
    python3 scripts/research_runner.py --experiment-id X   # Run specific experiment
    python3 scripts/research_runner.py --dry-run           # Print what would execute
    python3 scripts/research_runner.py --market sp500      # Filter queue by market
    python3 scripts/research_runner.py --agent-id worker-1 # Set agent identity
    python3 scripts/research_runner.py --run-all           # Run all queued experiments
"""
import sys
import json
import time
import copy
import signal
import logging
import argparse
import traceback
import subprocess
from pathlib import Path
from datetime import datetime, timezone

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

from utils.config import get_active_config
from research.models import (
    read_queue, claim_experiment, update_queue_entry, get_next_queued,
    append_to_journal, ExperimentEnvelope, JournalEntry,
    ExperimentStatus, ExperimentType,
    EXPERIMENTS_DIR, QUEUE_PATH, JOURNAL_PATH,
    generate_experiment_id,
)

from utils.logging_config import setup_logging
logger = setup_logging("research_runner")

MAX_RUNTIME_S = 4 * 3600  # 4 hour budget per experiment

# Code-level errors that indicate a programming bug (not a research failure).
# These are retried once automatically before being marked as failed.
# Non-code errors (data missing, acceptance criteria not met) are NOT in this list.
CODE_ERRORS = (TypeError, AttributeError, NameError, KeyError, ValueError)


class TimeoutError(Exception):
    pass


def timeout_handler(signum, frame):
    raise TimeoutError("Experiment exceeded time budget")


# ---------------------------------------------------------------------------
# Dispatch: route experiment type to execution method
# ---------------------------------------------------------------------------

def dispatch_experiment(entry: dict, agent_id: str, dry_run: bool = False) -> dict:
    """Route an experiment to the correct execution method. Returns result dict."""
    exp_type = entry.get('method', '')
    exp_id = entry['id']
    market = entry['market']
    strategy = entry.get('strategy_name')
    params = entry.get('params_override')

    logger.info(f"Dispatching {exp_id}: type={exp_type}, market={market}, strategy={strategy}")

    if dry_run:
        return _dry_run_result(entry)

    if exp_type == ExperimentType.SINGLE_STRATEGY_TEST:
        return _run_single_strategy(entry)
    elif exp_type == ExperimentType.COMBINED_PORTFOLIO_TEST:
        return _run_combined_test(entry)
    elif exp_type == ExperimentType.PARAM_SWEEP:
        return _run_param_sweep(entry)
    elif exp_type == ExperimentType.FULL_OPTIMIZATION:
        return _run_full_optimization(entry)
    elif exp_type == ExperimentType.OOS_VALIDATION:
        return _run_oos_validation(entry)
    elif exp_type == ExperimentType.FILTER_TEST:
        return _run_filter_test(entry)
    elif exp_type == ExperimentType.REOPTIMIZATION:
        return _run_full_optimization(entry)
    else:
        return {'error': f'Unknown experiment type: {exp_type}', 'status': 'failed'}


def _dry_run_result(entry: dict) -> dict:
    """Return what would be executed without running anything."""
    return {
        'dry_run': True,
        'would_execute': entry.get('method'),
        'strategy': entry.get('strategy_name'),
        'market': entry.get('market'),
        'params': entry.get('params_override'),
        'estimated_runtime_min': entry.get('estimated_runtime_min', '?'),
    }


def _run_single_strategy(entry: dict) -> dict:
    """Run a single strategy evaluation."""
    from scripts.strategy_evaluator import evaluate_strategy
    return evaluate_strategy(
        strategy_name=entry['strategy_name'],
        market_id=entry['market'],
        params_override=entry.get('params_override'),
        combined=False,
        experiment_id=entry['id'],
    )


def _run_combined_test(entry: dict) -> dict:
    """Run strategy evaluation in combined mode (portfolio impact).

    If this experiment depends on an optimization step, load the optimized
    params from the candidate config instead of using defaults.
    """
    from scripts.strategy_evaluator import evaluate_strategy

    params_override = entry.get('params_override')
    strategy_name = entry['strategy_name']
    market = entry['market']

    # Try to load optimized params from dependency chain
    if not params_override:
        deps = entry.get('depends_on', [])
        for dep_id in deps:
            candidate = PROJECT / 'config' / 'candidates' / f'{market}_{dep_id}.json'
            if candidate.exists():
                with open(candidate) as f:
                    candidate_cfg = json.load(f)
                # Extract the strategy-specific params from the candidate config
                strat_params = candidate_cfg.get('strategies', {}).get(strategy_name, {})
                # Filter out meta keys like 'enabled'
                params_override = {k: v for k, v in strat_params.items()
                                   if k not in ('enabled',)}
                logger.info(f"Combined test using optimized params from {candidate.name}: "
                            f"{list(params_override.keys())}")
                break

            # Also check experiment envelope for best_params
            exp_path = PROJECT / 'research' / 'experiments' / f'exp-{dep_id}.json'
            if exp_path.exists():
                with open(exp_path) as f:
                    exp_data = json.load(f)
                best_params = (exp_data.get('outputs') or {}).get('best_params')
                if best_params:
                    params_override = best_params
                    logger.info(f"Combined test using best_params from exp-{dep_id}: "
                                f"{list(params_override.keys())}")
                    break

    return evaluate_strategy(
        strategy_name=strategy_name,
        market_id=market,
        params_override=params_override,
        combined=True,
        experiment_id=entry['id'],
    )


def _run_param_sweep(entry: dict) -> dict:
    """Run a parameter sweep for a specific param."""
    from scripts.strategy_evaluator import (
        get_active_config, load_market_data, make_config_with_strategy,
        run_backtest, get_strategy_class
    )

    market = entry['market']
    strategy = entry['strategy_name']
    config = get_active_config(market)
    data = load_market_data(market)

    # Get sweep params from acceptance_criteria or params_override
    sweep_config = entry.get('params_override', {})
    sweep_param = sweep_config.get('sweep_param')
    sweep_values = sweep_config.get('sweep_values', [])

    if not sweep_param or not sweep_values:
        return {'error': 'param_sweep requires sweep_param and sweep_values in params_override'}

    results = []
    for val in sweep_values:
        params = {sweep_param: val}
        cfg = make_config_with_strategy(config, strategy, params, solo=True)
        metrics = run_backtest(cfg, data)
        metrics['param_value'] = val
        results.append(metrics)
        logger.info(f"  {sweep_param}={val}: Sharpe={metrics.get('sharpe', 0):.3f} "
                     f"CAGR={metrics.get('cagr_pct', 0):.2f}%")

    # Find best
    best = max(results, key=lambda r: r.get('sharpe', -999))

    return {
        'sweep_param': sweep_param,
        'sweep_results': results,
        'best_value': best['param_value'],
        'best_metrics': best,
        'strategy': strategy,
        'market': market,
    }


def _run_full_optimization(entry: dict) -> dict:
    """Run parameter optimization.

    Routes to:
      - Per-strategy coordinate descent if strategy_name is set (dormant activation)
      - Full portfolio reoptimization via reoptimize_parallel.py otherwise
    """
    strategy_name = entry.get('strategy_name')
    category = entry.get('category', '')

    if strategy_name and category in ('dormant', 'new_strategy'):
        return _run_strategy_coord_descent(entry)
    else:
        return _run_portfolio_reoptimization(entry)


def _run_strategy_coord_descent(entry: dict) -> dict:
    """Coordinate descent optimization for a single strategy.

    Uses the param_grid from entry['params_override']['param_grid'].
    Iterates through each parameter, sweeping all values while holding
    others at current best. Repeats until no improvement.
    """
    from scripts.strategy_evaluator import (
        get_strategy_class, load_market_data, make_config_with_strategy,
        run_backtest
    )

    market = entry['market']
    strategy_name = entry['strategy_name']
    config = get_active_config(market)
    data = load_market_data(market)

    param_grid = (entry.get('params_override') or {}).get('param_grid', {})
    if not param_grid:
        return {'error': f'No param_grid in params_override for {strategy_name}'}

    # Get current params: merge config values with strategy class defaults.
    # The config may have no params for a dormant strategy, so we instantiate
    # the strategy class to get its __init__ defaults.
    strat_cfg = config.get('strategies', {}).get(strategy_name, {})
    strat_cls = get_strategy_class(strategy_name)
    test_config = copy.deepcopy(config)
    test_config.setdefault('strategies', {}).setdefault(strategy_name, {})['enabled'] = True
    try:
        strat_instance = strat_cls(test_config)
        # Read defaults from the instance attributes
        strat_defaults = {}
        for param in param_grid:
            if hasattr(strat_instance, param):
                strat_defaults[param] = getattr(strat_instance, param)
            else:
                strat_defaults[param] = None
    except Exception:
        strat_defaults = {}

    current_params = {}
    for param in param_grid:
        # Config value takes priority, then instance default, then middle of grid
        val = strat_cfg.get(param)
        if val is None:
            val = strat_defaults.get(param)
        if val is None:
            # Use middle value from the grid as fallback
            val = param_grid[param][len(param_grid[param]) // 2]
        current_params[param] = val

    logger.info(f"Coord descent for {strategy_name}: {len(param_grid)} params, "
                f"initial: {current_params}")

    # Run baseline with current params
    best_params = dict(current_params)
    baseline_cfg = make_config_with_strategy(config, strategy_name, best_params, solo=True)
    baseline_metrics = run_backtest(baseline_cfg, data)
    best_score = _score(baseline_metrics)
    logger.info(f"  Baseline score: {best_score:.4f} (Sharpe={baseline_metrics.get('sharpe', 0):.4f}, "
                f"CAGR={baseline_metrics.get('cagr_pct', 0):.2f}%, trades={baseline_metrics.get('total_trades', 0)})")

    all_trials = [{'params': dict(best_params), 'metrics': baseline_metrics, 'score': best_score, 'round': 0}]

    MAX_ROUNDS = 3
    for round_num in range(1, MAX_ROUNDS + 1):
        improved = False
        for param, values in param_grid.items():
            best_val = best_params.get(param)
            for val in values:
                if val == best_val:
                    continue
                trial_params = dict(best_params)
                trial_params[param] = val
                trial_cfg = make_config_with_strategy(config, strategy_name, trial_params, solo=True)
                trial_metrics = run_backtest(trial_cfg, data)
                trial_score = _score(trial_metrics)

                all_trials.append({
                    'params': dict(trial_params), 'metrics': trial_metrics,
                    'score': trial_score, 'round': round_num,
                })

                if trial_score > best_score:
                    logger.info(f"  [{round_num}] {param}={val}: score {trial_score:.4f} > {best_score:.4f} ✓")
                    best_score = trial_score
                    best_params[param] = val
                    improved = True

        if not improved:
            logger.info(f"  Round {round_num}: no improvement, stopping")
            break

    # Run final evaluation with best params
    final_cfg = make_config_with_strategy(config, strategy_name, best_params, solo=True)
    final_metrics = run_backtest(final_cfg, data)

    # Save optimized config as candidate
    candidate_cfg = copy.deepcopy(config)
    candidate_cfg.setdefault('strategies', {}).setdefault(strategy_name, {}).update(best_params)
    candidate_cfg['strategies'][strategy_name]['enabled'] = True
    candidate_path = PROJECT / 'config' / 'candidates' / f'{market}_{entry["id"]}.json'
    candidate_path.parent.mkdir(parents=True, exist_ok=True)
    with open(candidate_path, 'w') as f:
        json.dump(candidate_cfg, f, indent=2)

    # Compute derived improvement metrics
    bl_sharpe = baseline_metrics.get('sharpe', 0) or 0
    opt_sharpe = final_metrics.get('sharpe', 0) or 0
    bl_trades = baseline_metrics.get('total_trades', 0) or 0
    opt_trades = final_metrics.get('total_trades', 0) or 0
    bl_pf = baseline_metrics.get('profit_factor', 0) or 0
    opt_pf = final_metrics.get('profit_factor', 0) or 0

    result = {
        'strategy': strategy_name,
        'market': market,
        'method': 'coordinate_descent',
        'n_params': len(param_grid),
        'n_trials': len(all_trials),
        'n_rounds': round_num,
        'initial_params': current_params,
        'best_params': best_params,
        'params_changed': {k: v for k, v in best_params.items() if v != current_params.get(k)},
        'baseline': baseline_metrics,
        'optimized': final_metrics,
        'score_baseline': _score(baseline_metrics),
        'score_optimized': _score(final_metrics),
        'candidate_config_path': str(candidate_path),
        'runtime_s': sum(t['metrics'].get('runtime_s', 0) for t in all_trials if 'runtime_s' in t.get('metrics', {})),
        # Derived improvement metrics for acceptance criteria
        'sharpe_improvement_vs_solo': round(opt_sharpe - bl_sharpe, 4),
        'trades': opt_trades,
        'profit_factor': opt_pf,
        'sharpe': opt_sharpe,
        'total_trades': opt_trades,
    }

    return result


def _score(metrics: dict) -> float:
    """Score a backtest result for optimization (higher is better).

    Composite: Sharpe * sqrt(trades) * profit_factor, penalized for high drawdown.
    """
    sharpe = metrics.get('sharpe', 0) or 0
    trades = max(metrics.get('total_trades', 0) or 0, 1)
    pf = max(metrics.get('profit_factor', 0) or 0, 0.01)
    dd = abs(metrics.get('max_drawdown_pct', 0) or 0)

    if trades < 5:
        return -999  # Too few trades — unreliable

    score = sharpe * (trades ** 0.5) * min(pf, 3.0)
    # Penalize high drawdown
    if dd > 15:
        score *= 0.5
    elif dd > 10:
        score *= 0.7

    return round(score, 4)


def _run_portfolio_reoptimization(entry: dict) -> dict:
    """Run full portfolio reoptimization via subprocess (reoptimize_parallel.py)."""
    market = entry['market']
    candidate_path = PROJECT / 'config' / 'candidates' / f'{market}_{entry["id"]}.json'
    results_path = PROJECT / 'backtest' / 'results' / f'reopt_{entry["id"]}.json'

    candidate_path.parent.mkdir(parents=True, exist_ok=True)

    cmd = [
        sys.executable, str(PROJECT / 'scripts' / 'reoptimize_parallel.py'),
        '--market', market,
        '--candidate-path', str(candidate_path),
        '--results-path', str(results_path),
    ]

    logger.info(f"Running: {' '.join(cmd)}")
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=MAX_RUNTIME_S)

    result = {
        'strategy': entry.get('strategy_name', 'all'),
        'market': market,
        'candidate_config_path': str(candidate_path),
        'results_path': str(results_path),
        'returncode': proc.returncode,
        'stdout_tail': proc.stdout[-2000:] if proc.stdout else '',
        'stderr_tail': proc.stderr[-1000:] if proc.stderr else '',
    }

    # Parse results file if it exists
    if results_path.exists():
        with open(results_path) as f:
            reopt_results = json.load(f)
        result['baseline'] = reopt_results.get('baseline_combined', {})
        result['optimized'] = reopt_results.get('final_combined', {})
        result['total_runtime_s'] = reopt_results.get('total_runtime_s', 0)

    return result


def _run_oos_validation(entry: dict) -> dict:
    """Run OOS validation via subprocess.

    Config resolution order:
      1. Explicit config_path in params_override
      2. Candidate config from the optimization step that this depends on
         (looks in config/candidates/ for a matching file)
      3. Falls back to the active config for the market
    """
    config_path = (entry.get('params_override') or {}).get('config_path')

    if not config_path:
        # Try to find candidate config from dependency chain
        deps = entry.get('depends_on', [])
        for dep_id in deps:
            candidate = PROJECT / 'config' / 'candidates' / f'{entry["market"]}_{dep_id}.json'
            if candidate.exists():
                config_path = str(candidate)
                logger.info(f"OOS using candidate config from dependency: {candidate.name}")
                break

        # Also check the combined test's optimization dependency
        if not config_path:
            queue = read_queue()
            for dep_id in deps:
                dep_entry = next((e for e in queue if e['id'] == dep_id), None)
                if dep_entry:
                    for inner_dep in dep_entry.get('depends_on', []):
                        candidate = PROJECT / 'config' / 'candidates' / f'{entry["market"]}_{inner_dep}.json'
                        if candidate.exists():
                            config_path = str(candidate)
                            logger.info(f"OOS using candidate config from transitive dep: {candidate.name}")
                            break

    if not config_path:
        config_path = str(PROJECT / 'config' / 'active' / f'{entry["market"]}.json')
        logger.warning(f"OOS falling back to active config: {config_path}")

    output_path = PROJECT / 'backtest' / 'results' / f'oos_{entry["id"]}.json'

    cmd = [
        sys.executable, str(PROJECT / 'scripts' / 'validate_oos.py'),
        '--config-path', str(config_path),
        '--output-path', str(output_path),
    ]

    logger.info(f"Running: {' '.join(cmd)}")
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=MAX_RUNTIME_S)

    result = {
        'market': entry['market'],
        'config_path': config_path,
        'output_path': str(output_path),
        'returncode': proc.returncode,
        'stdout_tail': proc.stdout[-2000:] if proc.stdout else '',
    }

    if output_path.exists():
        with open(output_path) as f:
            oos_results = json.load(f)
        result['summary'] = oos_results.get('summary', {})
        result['test1'] = oos_results.get('test1_time_period_split', {})
        result['test2'] = oos_results.get('test2_perturbation', {})
        result['test3'] = oos_results.get('test3_walkforward_consistency', {})

    return result


def _set_nested_config(config: dict, path: str, value) -> None:
    """Set a nested config value using a dotted path.

    Traverses the config dict, creating intermediate dicts as needed.

    Example:
        _set_nested_config(cfg, "strategies.mean_reversion.breadth.enabled", True)
        → cfg["strategies"]["mean_reversion"]["breadth"]["enabled"] = True
    """
    keys = path.split(".")
    d = config
    for k in keys[:-1]:
        d = d.setdefault(k, {})
    d[keys[-1]] = value


def _run_filter_test(entry: dict) -> dict:
    """Run a filter A/B test: backtest with and without a filter.

    If strategy_name is set, tests the filter on that strategy only (solo mode).
    If strategy_name is None, tests the filter across the full active portfolio.

    Supports two modes via params_override:
      a) Simple A/B: {filter_param, filter_on, filter_off}
      b) Multi-variant: {filter_param, variants: [{name, value}, ...]}
                     or {filter_param, variants: [val1, val2, ...]}  (scalar list)

    filter_param may be a dotted path (e.g. "strategies.mean_reversion.breadth.enabled")
    to target nested config keys.  TOM (turn-of-month) is treated as an engine-level param.
    """
    from scripts.strategy_evaluator import (
        load_market_data, make_config_with_strategy, run_backtest
    )

    market = entry['market']
    strategy = entry.get('strategy_name')
    config = get_active_config(market)
    data = load_market_data(market)

    filter_config = entry.get('params_override') or {}
    filter_param = filter_config.get('filter_param')
    variants = filter_config.get('variants')

    # Portfolio-wide mode: test filter on all active strategies
    solo = strategy is not None

    # Detect engine-level params (vix_filter, regime_filter, tom_filter, etc.)
    # These go on the top-level config, not per-strategy.
    # TOM (Turn-of-Month) is treated as an engine-level scheduling filter.
    _ENGINE_LEVEL_PARAMS = {'vix_filter', 'regime_filter', 'fee_aware_filter', 'tom_filter'}
    is_engine_param = filter_param in _ENGINE_LEVEL_PARAMS

    # Detect dotted-path params (e.g. "strategies.mean_reversion.breadth.enabled").
    # Dotted paths bypass make_config_with_strategy and use _set_nested_config directly.
    is_nested = bool(filter_param and '.' in filter_param)

    def _build_cfg(param: str, value) -> dict:
        """Build a config copy with param=value applied via the correct strategy."""
        cfg = copy.deepcopy(config)
        if is_nested:
            _set_nested_config(cfg, param, value)
            if solo:
                # Disable all strategies except the target (solo mode)
                for s_name in list(cfg.get('strategies', {}).keys()):
                    if s_name != strategy:
                        cfg['strategies'][s_name]['enabled'] = False
                if strategy:
                    cfg.setdefault('strategies', {}).setdefault(strategy, {})['enabled'] = True
        elif is_engine_param:
            cfg[param] = value
        elif solo:
            cfg = make_config_with_strategy(config, strategy, {param: value}, solo=True)
        else:
            for s_name, s_cfg in cfg.get('strategies', {}).items():
                if s_cfg.get('enabled', False):
                    s_cfg[param] = value
        return cfg

    if variants is not None:
        # Multi-variant mode: test several values of the filter.
        # Accepts both dict variants ({name, value}) and scalar variants (true/false/int/float).
        results = []
        for var in variants:
            if isinstance(var, dict):
                name = var.get('name', str(var.get('value', '?')))
                value = var['value']
            else:
                # Scalar variant (e.g. True, False, 5, 0.1)
                name = str(var)
                value = var
            cfg = _build_cfg(filter_param, value)
            metrics = run_backtest(cfg, data)
            metrics['variant_name'] = name
            metrics['variant_value'] = value
            results.append(metrics)
            logger.info(f"  variant={name}: Sharpe={metrics.get('sharpe', 0):.3f} "
                        f"trades={metrics.get('total_trades', 0)}")

        # Baseline (no filter / current config)
        if solo and not is_nested:
            baseline_cfg = make_config_with_strategy(config, strategy, {}, solo=True)
        else:
            baseline_cfg = copy.deepcopy(config)
        baseline = run_backtest(baseline_cfg, data)

        best = max(results, key=lambda r: r.get('sharpe', -999))
        return {
            'filter_param': filter_param,
            'mode': 'multi_variant',
            'baseline': baseline,
            'variants': results,
            'best_variant': best.get('variant_name'),
            'best_metrics': best,
            'strategy': strategy,
            'market': market,
        }

    elif filter_param:
        # Simple A/B mode
        filter_on_value = filter_config.get('filter_on')
        filter_off_value = filter_config.get('filter_off')

        cfg_off = _build_cfg(filter_param, filter_off_value)
        cfg_on = _build_cfg(filter_param, filter_on_value)

        metrics_off = run_backtest(cfg_off, data)
        metrics_on = run_backtest(cfg_on, data)

        delta = {}
        for key in ('cagr_pct', 'sharpe', 'sortino', 'max_drawdown_pct',
                     'win_rate_pct', 'profit_factor', 'total_trades'):
            off_val = metrics_off.get(key, 0) or 0
            on_val = metrics_on.get(key, 0) or 0
            delta[key] = round(on_val - off_val, 4)

        return {
            'filter_param': filter_param,
            'mode': 'ab_test',
            'filter_off': metrics_off,
            'filter_on': metrics_on,
            'delta': delta,
            'strategy': strategy,
            'market': market,
        }

    else:
        return {'error': 'filter_test requires filter_param or variants in params_override'}


# ---------------------------------------------------------------------------
# Main execution loop
# ---------------------------------------------------------------------------

def run_experiment(entry: dict, agent_id: str, dry_run: bool = False) -> dict:
    """Execute a single experiment end-to-end."""
    exp_id = entry['id']
    config = get_active_config(entry['market'])

    # 1. Update status to running
    if not dry_run:
        update_queue_entry(exp_id, {'status': ExperimentStatus.RUNNING})

    # 2. Create experiment envelope
    envelope = ExperimentEnvelope(
        id=exp_id,
        queue_entry=entry,
        config_snapshot=config,
        inputs={
            'strategy': entry.get('strategy_name'),
            'market': entry['market'],
            'method': entry['method'],
            'params_override': entry.get('params_override'),
        },
        metadata={
            'agent_id': agent_id,
            'started_at': datetime.now(timezone.utc).isoformat(),
        },
    )

    # 3. Set timeout
    if not dry_run:
        old_handler = signal.signal(signal.SIGALRM, timeout_handler)
        signal.alarm(MAX_RUNTIME_S)

    try:
        # 4. Dispatch — auto-retry once on code-level errors (programming bugs).
        # Non-code errors (data missing, acceptance criteria failures) are NOT retried.
        envelope.metadata['retries'] = 0
        result = None
        retry_count = 0
        while True:
            try:
                result = dispatch_experiment(entry, agent_id, dry_run)
                break  # Success — exit retry loop
            except CODE_ERRORS as code_err:
                retry_count += 1
                envelope.metadata['retries'] = retry_count
                if retry_count <= 1:
                    logger.warning(
                        f"Code error in {exp_id} ({type(code_err).__name__}: {code_err}) — "
                        f"retrying (attempt {retry_count}/1)"
                    )
                    # Brief pause before retry to let transient state settle
                    time.sleep(1)
                    continue
                else:
                    # Second failure — do not retry again; record full traceback
                    tb = traceback.format_exc()
                    logger.error(
                        f"Experiment {exp_id} failed after {retry_count} attempt(s): "
                        f"{type(code_err).__name__}: {code_err}"
                    )
                    update_queue_entry(exp_id, {'status': ExperimentStatus.FAILED})
                    envelope.verdict = 'fail'
                    envelope.verdict_rationale = (
                        f'Code error after {retry_count} attempt(s): '
                        f'{type(code_err).__name__}: {code_err}\n\nTraceback:\n{tb}'
                    )
                    envelope.metadata['error'] = str(code_err)
                    if not dry_run:
                        envelope.save()
                    return envelope.to_dict()

        # 5. Update envelope with results
        envelope.outputs = result
        envelope.metadata['finished_at'] = datetime.now(timezone.utc).isoformat()
        envelope.metadata['runtime_s'] = result.get('runtime_s', 0)

        if dry_run:
            envelope.verdict = 'dry_run'
            return envelope.to_dict()

        # 6. Evaluate against acceptance criteria
        verdict, rationale = _evaluate_result(result, entry.get('acceptance_criteria', {}))
        envelope.verdict = verdict
        envelope.verdict_rationale = rationale

        # 7. Update queue status
        status_map = {
            'pass': ExperimentStatus.PASSED,
            'fail': ExperimentStatus.FAILED,
            'partial': ExperimentStatus.PARTIAL,
        }
        update_queue_entry(exp_id, {
            'status': status_map.get(verdict, ExperimentStatus.EVALUATING)
        })

        # 8. Save envelope
        envelope.save()

        # 9. Append to journal
        key_metrics = _extract_key_metrics(result)
        delta = result.get('delta', {})
        journal_entry = JournalEntry(
            experiment_id=exp_id,
            timestamp=datetime.now(timezone.utc).isoformat(),
            market=entry['market'],
            category=entry.get('category', 'unknown'),
            strategy=entry.get('strategy_name'),
            hypothesis=entry.get('hypothesis', ''),
            verdict=verdict,
            key_metrics=key_metrics,
            delta_vs_baseline=delta,
            learnings=envelope.learnings,
            runtime_s=result.get('runtime_s', 0),
            agent_id=agent_id,
        )
        append_to_journal(journal_entry)

        logger.info(f"Experiment {exp_id} completed: verdict={verdict}")
        return envelope.to_dict()

    except TimeoutError:
        logger.error(f"Experiment {exp_id} exceeded time budget ({MAX_RUNTIME_S}s)")
        update_queue_entry(exp_id, {'status': ExperimentStatus.FAILED})
        envelope.verdict = 'fail'
        envelope.verdict_rationale = f'Exceeded time budget of {MAX_RUNTIME_S}s'
        envelope.save()
        return envelope.to_dict()

    except Exception as e:
        logger.error(f"Experiment {exp_id} failed: {e}")
        update_queue_entry(exp_id, {'status': ExperimentStatus.FAILED})
        envelope.verdict = 'fail'
        envelope.verdict_rationale = str(e)
        envelope.metadata['error'] = str(e)
        envelope.save()
        return envelope.to_dict()

    finally:
        if not dry_run:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, old_handler)


def _evaluate_result(result: dict, criteria: dict) -> tuple:
    """Evaluate experiment result against acceptance criteria.

    Returns (verdict, rationale) where verdict is 'pass', 'fail', or 'partial'.
    """
    if not criteria:
        return ('partial', 'No acceptance criteria defined')

    if result.get('error'):
        return ('fail', f'Execution error: {result["error"]}')

    if result.get('dry_run'):
        return ('dry_run', 'Dry run — no evaluation')

    # Extract metrics from the right place depending on experiment type
    metrics = _extract_key_metrics(result)

    passes = []
    failures = []

    # Common metric name aliases (criteria_name -> possible metric key names)
    ALIASES = {
        'trades': ['total_trades', 'trades'],
        'win_rate': ['win_rate_pct', 'win_rate'],
        'pnl': ['total_pnl', 'pnl'],
        'cagr': ['cagr_pct', 'cagr'],
        'dd': ['max_drawdown_pct', 'max_dd', 'drawdown'],
        'drawdown': ['max_drawdown_pct', 'max_dd', 'drawdown'],
        'combined_sharpe': ['sharpe'],
        'combined_dd': ['max_drawdown_pct'],
        'combined_trades': ['total_trades'],
        'strategy_trades': ['total_trades'],
    }

    def _resolve_metric(name: str) -> float:
        """Resolve a metric name to its value, trying aliases and location prefixes.

        Handles prefixed metric names like:
            combined_sharpe → result['combined']['sharpe']
            delta_sharpe → result['delta']['sharpe']
            strategy_trades → result['solo']['total_trades'] (strategy's own trades)
        """
        # Check for location-prefixed metric names (combined_X, delta_X, etc.)
        LOCATION_PREFIXES = {
            'combined_': 'combined',
            'delta_': 'delta',
            'strategy_': 'solo',
            'baseline_': 'baseline',
        }
        for prefix, loc in LOCATION_PREFIXES.items():
            if name.startswith(prefix):
                sub_name = name[len(prefix):]
                sub = result.get(loc, {})
                if isinstance(sub, dict):
                    val = sub.get(sub_name)
                    if val is not None:
                        return val
                    sub_aliases = ALIASES.get(sub_name, [])
                    for alias in sub_aliases:
                        val = sub.get(alias)
                        if val is not None:
                            return val

        # Direct lookup in extracted metrics
        val = metrics.get(name)
        if val is not None:
            return val

        # Try aliases
        aliases = ALIASES.get(name, [])
        for alias in aliases:
            val = metrics.get(alias)
            if val is not None:
                return val

        # Try alternative result locations
        for loc in ('solo', 'combined', 'best_metrics', 'optimized', 'baseline'):
            sub = result.get(loc, {})
            if isinstance(sub, dict):
                val = sub.get(name)
                if val is not None:
                    return val
                for alias in aliases:
                    val = sub.get(alias)
                    if val is not None:
                        return val

        # Final fallback: check top-level result dict
        val = result.get(name)
        if val is not None and isinstance(val, (int, float)):
            return val
        for alias in aliases:
            val = result.get(alias)
            if val is not None and isinstance(val, (int, float)):
                return val

        return None

    for criterion, threshold in criteria.items():
        # Skip non-numeric criteria (descriptions, booleans)
        if isinstance(threshold, str):
            continue
        if isinstance(threshold, bool):
            continue

        # Determine direction (min/max) and metric name
        if criterion.startswith('min_'):
            metric_name = criterion[4:]
            direction = 'min'
        elif criterion.startswith('max_'):
            metric_name = criterion[4:]
            direction = 'max'
        elif criterion.startswith('positive_'):
            metric_name = criterion[9:]  # e.g., positive_delta_sharpe
            direction = 'positive'
        else:
            metric_name = criterion
            direction = 'min'  # default: treat as minimum

        actual = _resolve_metric(metric_name)
        if actual is None:
            actual = _resolve_metric(criterion)  # Try the full criterion name

        if actual is None:
            # Skip silently for common non-metric keys
            if criterion in ('description',):
                continue
            failures.append(f'{criterion}: metric not found')
            continue

        # Evaluate
        if direction == 'positive':
            if actual > 0:
                passes.append(f'{criterion}: {actual:.4f} > 0')
            else:
                failures.append(f'{criterion}: {actual:.4f} <= 0')
        elif direction == 'min':
            if actual >= threshold:
                passes.append(f'{criterion}: {actual:.4f} >= {threshold}')
            else:
                failures.append(f'{criterion}: {actual:.4f} < {threshold}')
        elif direction == 'max':
            if actual <= threshold:
                passes.append(f'{criterion}: {actual:.4f} <= {threshold}')
            else:
                failures.append(f'{criterion}: {actual:.4f} > {threshold}')

    # Auto-check: warn if edge is not statistically significant (p >= 0.05)
    edge_p = None
    for loc in ('solo', 'combined', 'best_metrics', 'optimized'):
        sub = result.get(loc, {})
        if isinstance(sub, dict) and 'edge_p_value' in sub:
            edge_p = sub['edge_p_value']
            break
    if edge_p is None:
        edge_p = metrics.get('edge_p_value')

    if edge_p is not None and edge_p >= 0.05:
        failures.append(f'edge_p_value: {edge_p:.4f} >= 0.05 (edge not statistically significant)')

    # Auto-check: warn if Monte Carlo says trade sequencing is fragile
    mc_fragile = None
    for loc in ('solo', 'combined', 'best_metrics', 'optimized'):
        sub = result.get(loc, {})
        if isinstance(sub, dict) and 'mc_fragile' in sub:
            mc_fragile = sub['mc_fragile']
            break
    if mc_fragile is None:
        mc_fragile = metrics.get('mc_fragile')
    if mc_fragile:
        failures.append('mc_fragile: True (p95 MC drawdown > 2× actual — trade sequence dependent)')

    if not failures:
        return ('pass', f'All {len(passes)} criteria met: {"; ".join(passes)}')
    elif passes:
        return ('partial', f'{len(passes)} pass, {len(failures)} fail: {"; ".join(failures)}')
    else:
        return ('fail', f'All {len(failures)} criteria failed: {"; ".join(failures)}')


def _extract_key_metrics(result: dict) -> dict:
    """Extract the most relevant metrics from any result type."""
    # Try various locations
    for loc in ('solo', 'combined', 'metrics', 'best_metrics', 'optimized'):
        sub = result.get(loc, {})
        if isinstance(sub, dict) and 'sharpe' in sub:
            return sub

    # Fallback: return whatever top-level numeric fields exist
    return {k: v for k, v in result.items() if isinstance(v, (int, float))}


def parse_args():
    parser = argparse.ArgumentParser(description='Atlas Research Runner')
    parser.add_argument('--experiment-id', type=str, default=None,
                        help='Run a specific experiment by ID')
    parser.add_argument('--market', type=str, default=None,
                        help='Filter queue by market')
    parser.add_argument('--agent-id', type=str, default='atlas-research',
                        help='Agent identity for claim tracking')
    parser.add_argument('--dry-run', action='store_true',
                        help='Print what would execute without running')
    parser.add_argument('--run-all', action='store_true',
                        help='Run all queued experiments (not just next)')
    parser.add_argument('--max-experiments', type=int, default=10,
                        help='Max experiments to run with --run-all')
    return parser.parse_args()


def main():
    args = parse_args()

    if args.experiment_id:
        # Run specific experiment
        queue = read_queue()
        entry = next((e for e in queue if e['id'] == args.experiment_id), None)
        if not entry:
            logger.error(f"Experiment {args.experiment_id} not found in queue")
            return 1
        if not args.dry_run:
            claimed = claim_experiment(entry['id'], args.agent_id)
            if not claimed:
                logger.error(f"Could not claim {args.experiment_id} (status: {entry.get('status')})")
                return 1
            entry = claimed
        result = run_experiment(entry, args.agent_id, args.dry_run)
        print(json.dumps(result, indent=2, default=str)[:3000])
        return 0

    if args.run_all:
        # Run all queued experiments
        count = 0
        code_errors = []
        while count < args.max_experiments:
            entry = get_next_queued(args.market)
            if not entry:
                logger.info("No more queued experiments")
                break
            if not args.dry_run:
                claimed = claim_experiment(entry['id'], args.agent_id)
                if not claimed:
                    logger.warning(f"Could not claim {entry['id']}, skipping")
                    continue
                entry = claimed
            result = run_experiment(entry, args.agent_id, args.dry_run)
            verdict = result.get('verdict', 'unknown')
            logger.info(f"[{count+1}] {entry['id']}: {entry.get('title', '?')} → {verdict}")

            # Track code errors (TypeError, AttributeError, etc.) vs research failures
            error_msg = result.get('metadata', {}).get('error', '') or result.get('verdict_rationale', '')
            if verdict == 'fail' and any(exc in error_msg for exc in (
                'TypeError', 'AttributeError', 'NameError', 'SyntaxError',
                'KeyError', 'IndexError', 'takes', 'positional argument',
                'has no attribute', 'is not defined', 'unexpected keyword',
            )):
                code_errors.append({'id': entry['id'], 'error': error_msg})

            count += 1
        logger.info(f"Completed {count} experiments")

        # Exit non-zero if code errors occurred (triggers auto-recovery → pi agent)
        if code_errors:
            logger.error(f"CODE ERRORS in {len(code_errors)} experiment(s) — triggering auto-recovery:")
            for ce in code_errors:
                logger.error(f"  {ce['id']}: {ce['error'][:200]}")
            return 2  # Distinct exit code: code error (vs 1 = operational error)

        return 0

    # Default: run next queued experiment
    entry = get_next_queued(args.market)
    if not entry:
        logger.info("No queued experiments")
        return 0

    if not args.dry_run:
        claimed = claim_experiment(entry['id'], args.agent_id)
        if not claimed:
            logger.error(f"Could not claim {entry['id']}")
            return 1
        entry = claimed

    result = run_experiment(entry, args.agent_id, args.dry_run)
    print(json.dumps(result, indent=2, default=str)[:3000])
    return 0


if __name__ == '__main__':
    sys.exit(main())
