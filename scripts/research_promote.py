#!/usr/bin/env python3
"""Atlas Research Promotion Pipeline

Handles the full promotion flow for successful experiments:
1. Candidate staging (config/candidates/)
2. Automated OOS validation (3-test suite)
3. Regression check vs current active config
4. Promotion summary generation
5. Telegram notification with promotion request
6. Approval handling (promote or reject)
7. Rate limiting (max 1 promotion per week per market)
8. Rollback flagging (degradation within 5 days)

Usage:
    # Stage and validate a candidate
    python3 scripts/research_promote.py --stage --experiment-id EXP_ID --market sp500

    # Check promotion readiness (OOS + regression + rate limit)
    python3 scripts/research_promote.py --check --experiment-id EXP_ID --market sp500

    # Promote an approved candidate
    python3 scripts/research_promote.py --promote --experiment-id EXP_ID --market sp500

    # Reject a candidate
    python3 scripts/research_promote.py --reject --experiment-id EXP_ID --reason "Too few trades"

    # Check for degradation after promotion (rollback watchdog)
    python3 scripts/research_promote.py --watchdog --market sp500 --days 5
"""
import sys
import json
import copy
import time
import shutil
import logging
import argparse
import subprocess
from pathlib import Path
from datetime import datetime, timezone, timedelta

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

from utils.config import get_active_config, save_config_version, ACTIVE_DIR, VERSIONS_DIR
from research.models import (
    load_experiment, update_queue_entry, append_to_journal,
    get_recent_promotions, ExperimentEnvelope, JournalEntry,
    ExperimentStatus, EXPERIMENTS_DIR,
)

from utils.logging_config import setup_logging
logger = setup_logging("research_promote")

CANDIDATES_DIR = PROJECT / 'config' / 'candidates'
MAX_PROMOTIONS_PER_WEEK = 1
ROLLBACK_WATCH_DAYS = 5


# ---------------------------------------------------------------------------
# Stage: Create candidate config from experiment results
# ---------------------------------------------------------------------------

def stage_candidate(experiment_id: str, market_id: str,
                    strategy_params: dict = None,
                    enable_strategy: str = None) -> Path:
    """Stage a candidate config from a successful experiment.

    Args:
        experiment_id: The experiment that produced these results
        market_id: Market to create candidate for
        strategy_params: Dict of {strategy_name: {param: value}} to apply
        enable_strategy: Strategy name to enable (for dormant activations)

    Returns:
        Path to the staged candidate config
    """
    config = get_active_config(market_id)

    # Apply strategy parameter changes
    if strategy_params:
        for strat_name, params in strategy_params.items():
            if strat_name not in config.get('strategies', {}):
                config.setdefault('strategies', {})[strat_name] = {}
            for k, v in params.items():
                config['strategies'][strat_name][k] = v

    # Enable strategy if requested
    if enable_strategy:
        if enable_strategy not in config.get('strategies', {}):
            config.setdefault('strategies', {})[enable_strategy] = {}
        config['strategies'][enable_strategy]['enabled'] = True

    # Add promotion metadata
    config['_promotion_metadata'] = {
        'experiment_id': experiment_id,
        'staged_at': datetime.now(timezone.utc).isoformat(),
        'source_version': config.get('version', 'unknown'),
        'changes': {
            'strategy_params': strategy_params,
            'enable_strategy': enable_strategy,
        },
    }

    # Save candidate
    CANDIDATES_DIR.mkdir(parents=True, exist_ok=True)
    candidate_path = CANDIDATES_DIR / f'{market_id}_{experiment_id}.json'
    with open(candidate_path, 'w') as f:
        json.dump(config, f, indent=2, default=str)

    logger.info(f"Staged candidate config: {candidate_path}")
    return candidate_path


# ---------------------------------------------------------------------------
# Validate: OOS + Regression + Rate Limit
# ---------------------------------------------------------------------------

def validate_candidate(experiment_id: str, market_id: str,
                       candidate_path: Path = None,
                       skip_oos: bool = False) -> dict:
    """Run full validation suite on a candidate config.

    Returns dict with:
        - oos_pass: bool
        - regression_pass: bool
        - rate_limit_ok: bool
        - overall_pass: bool
        - details: dict with full validation results
    """
    if candidate_path is None:
        candidate_path = CANDIDATES_DIR / f'{market_id}_{experiment_id}.json'

    if not candidate_path.exists():
        return {'overall_pass': False, 'error': f'Candidate not found: {candidate_path}'}

    result = {
        'experiment_id': experiment_id,
        'market': market_id,
        'candidate_path': str(candidate_path),
        'validated_at': datetime.now(timezone.utc).isoformat(),
    }

    # 1. Rate limit check
    recent = get_recent_promotions(market_id, days=7)
    result['rate_limit_ok'] = len(recent) < MAX_PROMOTIONS_PER_WEEK
    result['recent_promotions'] = len(recent)
    if not result['rate_limit_ok']:
        logger.warning(f"Rate limit: {len(recent)} promotions in last 7 days (max {MAX_PROMOTIONS_PER_WEEK})")

    # 2. OOS validation
    if skip_oos:
        result['oos_pass'] = None
        result['oos_skipped'] = True
        logger.info("OOS validation skipped")
    else:
        oos_result = _run_oos_validation(candidate_path, experiment_id)
        result['oos_pass'] = oos_result.get('overall_pass', False)
        result['oos_details'] = oos_result

    # 3. Regression check vs current active
    regression = _run_regression_check(candidate_path, market_id)
    result['regression_pass'] = regression.get('pass', False)
    result['regression_details'] = regression

    # Overall verdict
    oos_ok = result.get('oos_pass') is not False  # True or None (skipped) both ok
    result['overall_pass'] = (
        result['rate_limit_ok']
        and oos_ok
        and result['regression_pass']
    )

    return result


def _run_oos_validation(candidate_path: Path, experiment_id: str) -> dict:
    """Run the 3-test OOS validation suite."""
    output_path = PROJECT / 'backtest' / 'results' / f'oos_promotion_{experiment_id}.json'

    cmd = [
        sys.executable, str(PROJECT / 'scripts' / 'validate_oos.py'),
        '--config-path', str(candidate_path),
        '--output-path', str(output_path),
    ]

    logger.info(f"Running OOS validation: {' '.join(cmd)}")
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=7200)
        if output_path.exists():
            with open(output_path) as f:
                results = json.load(f)
            summary = results.get('summary', {})
            overall = summary.get('overall_verdict', 'FAIL')
            return {
                'overall_pass': overall == 'PASS',
                'test1': summary.get('test1_verdict', 'unknown'),
                'test2': summary.get('test2_verdict', 'unknown'),
                'test3': summary.get('test3_verdict', 'unknown'),
                'output_path': str(output_path),
            }
        else:
            return {'overall_pass': False, 'error': 'OOS output not generated'}
    except subprocess.TimeoutExpired:
        return {'overall_pass': False, 'error': 'OOS validation timed out (2h)'}
    except Exception as e:
        return {'overall_pass': False, 'error': str(e)}


def _run_regression_check(candidate_path: Path, market_id: str) -> dict:
    """Compare candidate config vs current active on key metrics."""
    from scripts.strategy_evaluator import load_market_data, run_backtest

    current_config = get_active_config(market_id)
    with open(candidate_path) as f:
        candidate_config = json.load(f)

    data = load_market_data(market_id)

    logger.info("Running baseline backtest...")
    baseline = run_backtest(current_config, data)
    logger.info("Running candidate backtest...")
    candidate = run_backtest(candidate_config, data)

    # Check regression: no metric should degrade by more than 10%
    regression_ok = True
    comparisons = {}
    for metric in ('sharpe', 'cagr_pct', 'sortino', 'profit_factor', 'win_rate_pct'):
        b = baseline.get(metric, 0) or 0
        c = candidate.get(metric, 0) or 0
        delta = c - b
        pct_change = ((c - b) / abs(b) * 100) if b != 0 else 0
        comparisons[metric] = {
            'baseline': round(b, 4),
            'candidate': round(c, 4),
            'delta': round(delta, 4),
            'pct_change': round(pct_change, 2),
        }
        if pct_change < -10:
            regression_ok = False
            logger.warning(f"Regression: {metric} degraded {pct_change:.1f}%")

    # Also check drawdown (lower is better)
    b_dd = baseline.get('max_drawdown_pct', 0) or 0
    c_dd = candidate.get('max_drawdown_pct', 0) or 0
    dd_increase = c_dd - b_dd
    comparisons['max_drawdown_pct'] = {
        'baseline': round(b_dd, 4),
        'candidate': round(c_dd, 4),
        'delta': round(dd_increase, 4),
    }
    if dd_increase > 3.0:  # Max 3pp DD increase allowed
        regression_ok = False
        logger.warning(f"Regression: DD increased by {dd_increase:.2f}pp")

    return {
        'pass': regression_ok,
        'baseline_metrics': baseline,
        'candidate_metrics': candidate,
        'comparisons': comparisons,
    }


# ---------------------------------------------------------------------------
# Promote: Apply candidate to active config
# ---------------------------------------------------------------------------

def promote_candidate(experiment_id: str, market_id: str,
                      candidate_path: Path = None) -> dict:
    """Promote a validated candidate to active config.

    1. Version the candidate config
    2. Copy to config/active/
    3. Update queue status to PROMOTED
    4. Append to journal with promoted=True
    """
    if candidate_path is None:
        candidate_path = CANDIDATES_DIR / f'{market_id}_{experiment_id}.json'

    if not candidate_path.exists():
        return {'success': False, 'error': f'Candidate not found: {candidate_path}'}

    with open(candidate_path) as f:
        config = json.load(f)

    # Save versioned copy
    version_path = save_config_version(config, market_id=market_id)

    # Update experiment
    exp = load_experiment(experiment_id)
    if exp:
        envelope = ExperimentEnvelope.from_dict(exp)
        envelope.promoted = True
        envelope.candidate_config_path = str(candidate_path)
        envelope.save()

    # Update queue
    update_queue_entry(experiment_id, {
        'status': ExperimentStatus.PROMOTED,
    })

    # Journal entry
    journal_entry = JournalEntry(
        experiment_id=experiment_id,
        timestamp=datetime.now(timezone.utc).isoformat(),
        market=market_id,
        category='promotion',
        strategy=config.get('_promotion_metadata', {}).get('changes', {}).get('enable_strategy'),
        hypothesis=f'Promoting experiment {experiment_id} to active config',
        verdict='promoted',
        key_metrics={},
        delta_vs_baseline={},
        learnings=[f'Promoted to {version_path}'],
        promoted=True,
    )
    append_to_journal(journal_entry)

    logger.info(f"Promoted {experiment_id} to active: {version_path}")
    return {
        'success': True,
        'version_path': str(version_path),
        'active_path': str(ACTIVE_DIR / f'{market_id}.json'),
        'config_version': config.get('version'),
    }


# ---------------------------------------------------------------------------
# Reject: Archive candidate
# ---------------------------------------------------------------------------

def reject_candidate(experiment_id: str, market_id: str, reason: str) -> dict:
    """Reject a candidate and archive it."""
    candidate_path = CANDIDATES_DIR / f'{market_id}_{experiment_id}.json'

    update_queue_entry(experiment_id, {
        'status': ExperimentStatus.REJECTED,
    })

    # Archive to candidates/rejected/
    rejected_dir = CANDIDATES_DIR / 'rejected'
    rejected_dir.mkdir(parents=True, exist_ok=True)
    if candidate_path.exists():
        shutil.move(str(candidate_path), str(rejected_dir / candidate_path.name))

    # Journal
    journal_entry = JournalEntry(
        experiment_id=experiment_id,
        timestamp=datetime.now(timezone.utc).isoformat(),
        market=market_id,
        category='rejection',
        strategy=None,
        hypothesis=f'Rejected experiment {experiment_id}',
        verdict='rejected',
        key_metrics={},
        delta_vs_baseline={},
        learnings=[f'Rejected: {reason}'],
        promoted=False,
    )
    append_to_journal(journal_entry)

    logger.info(f"Rejected {experiment_id}: {reason}")
    return {'success': True, 'reason': reason}


# ---------------------------------------------------------------------------
# Watchdog: Check for degradation after promotion
# ---------------------------------------------------------------------------

def watchdog_check(market_id: str, days: int = ROLLBACK_WATCH_DAYS) -> dict:
    """Check if a recently promoted config is degrading.

    Compares recent paper trading performance against the promotion baseline.
    """
    recent = get_recent_promotions(market_id, days=days)
    if not recent:
        return {'status': 'no_recent_promotions', 'market': market_id}

    results = []
    for promo in recent:
        exp_id = promo.get('experiment_id', 'unknown')
        exp = load_experiment(exp_id)

        # Check paper trading state for recent performance
        paper_state_path = PROJECT / 'paper_engine' / 'state' / f'paper_{market_id}.json'
        if paper_state_path.exists():
            with open(paper_state_path) as f:
                paper_state = json.load(f)

            equity_history = paper_state.get('equity_history', [])
            if len(equity_history) >= 2:
                recent_equity = [e.get('equity', 0) for e in equity_history[-days:]]
                if recent_equity:
                    peak = max(recent_equity)
                    current = recent_equity[-1]
                    drawdown = (peak - current) / peak * 100 if peak > 0 else 0

                    results.append({
                        'experiment_id': exp_id,
                        'promoted_at': promo.get('timestamp'),
                        'current_equity': current,
                        'recent_peak': peak,
                        'drawdown_pct': round(drawdown, 2),
                        'needs_review': drawdown > 5.0,
                    })

    needs_review = any(r.get('needs_review') for r in results)
    return {
        'market': market_id,
        'recent_promotions': len(recent),
        'checks': results,
        'needs_review': needs_review,
    }


# ---------------------------------------------------------------------------
# Telegram notification for promotion requests
# ---------------------------------------------------------------------------

def send_promotion_request(experiment_id: str, market_id: str,
                           validation_result: dict) -> bool:
    """Send a Telegram message with Approve/Reject inline buttons."""
    try:
        from utils.telegram import send_message, _esc
    except ImportError:
        logger.warning("Telegram module not available")
        return False

    regression = validation_result.get('regression_details', {})
    comparisons = regression.get('comparisons', {})

    # Metrics where lower is better
    _LOWER_IS_BETTER = {'max_drawdown', 'max_drawdown_pct', 'max_dd', 'drawdown'}
    _PCT_METRICS = {'cagr', 'cagr_pct', 'max_drawdown', 'max_drawdown_pct', 'max_dd',
                    'win_rate', 'win_rate_pct', 'drawdown', 'total_return'}
    _LABELS = {
        'sharpe': 'Sharpe', 'cagr': 'CAGR', 'cagr_pct': 'CAGR',
        'max_drawdown': 'Max DD', 'max_drawdown_pct': 'Max DD', 'max_dd': 'Max DD',
        'profit_factor': 'Profit Factor', 'win_rate': 'Win Rate', 'win_rate_pct': 'Win Rate',
        'sortino': 'Sortino', 'total_return': 'Total Return',
    }

    lines = [
        "🔬 <b>Research Promotion Request</b>",
        "",
        f"Experiment: <code>{_esc(experiment_id)}</code>",
        f"Market: {_esc(market_id.upper())}",
        "",
        "<b>Before → After:</b>",
    ]

    for metric, data in comparisons.items():
        b = data.get('baseline', 0)
        c = data.get('candidate', 0)
        delta = data.get('delta', 0)
        inverted = metric in _LOWER_IS_BETTER
        is_pct = metric in _PCT_METRICS
        label = _LABELS.get(metric, metric)

        improved = (delta < 0) if inverted else (delta > 0)
        worsened = (delta > 0) if inverted else (delta < 0)
        icon = "🟢" if improved else "🔴" if worsened else "⚪"
        arrow = "↑" if delta > 0 else "↓" if delta < 0 else "→"

        if is_pct:
            lines.append(f"  {icon} {_esc(label)}: {b*100:.1f}% → {c*100:.1f}% ({delta*100:+.1f}pp {arrow})")
        else:
            lines.append(f"  {icon} {_esc(label)}: {b:.3f} → {c:.3f} ({delta:+.3f} {arrow})")

    oos = validation_result.get('oos_details', {})
    if oos:
        oos_tests = [
            ("Test 1 (Time Split)", oos.get('test1')),
            ("Test 2 (Perturbation)", oos.get('test2')),
            ("Test 3 (Walk-Forward)", oos.get('test3')),
        ]
        has_any = any(v and str(v) not in ('?', 'N/A', 'None', '') for _, v in oos_tests)
        if has_any:
            lines.extend(["", "<b>OOS Validation:</b>"])
            for name, val in oos_tests:
                val_str = str(val) if val else ''
                if val_str in ('?', 'N/A', 'None', ''):
                    continue
                verdict_icon = "✅" if "PASS" in val_str.upper() else "❌" if "FAIL" in val_str.upper() else "⏭️"
                lines.append(f"  {verdict_icon} {name}: {_esc(val_str)}")

    lines.extend([
        "",
        f"Rate limit: {validation_result.get('recent_promotions', 0)}/{MAX_PROMOTIONS_PER_WEEK} this week",
    ])

    keyboard = {
        "inline_keyboard": [[
            {"text": "✅ Approve & Promote", "callback_data": f"research:{experiment_id}:approve:{market_id}"},
            {"text": "❌ Reject", "callback_data": f"research:{experiment_id}:reject:{market_id}"},
        ]]
    }

    msg = "\n".join(lines)
    return send_message(msg, parse_mode="HTML", reply_markup=keyboard)


# ---------------------------------------------------------------------------
# Generate promotion summary report
# ---------------------------------------------------------------------------

def generate_promotion_summary(experiment_id: str, market_id: str,
                                validation_result: dict) -> str:
    """Generate a human-readable promotion summary."""
    lines = [
        f"# Promotion Summary: {experiment_id}",
        f"**Market:** {market_id.upper()}",
        f"**Validated:** {validation_result.get('validated_at', 'unknown')}",
        "",
    ]

    # Overall verdict
    overall = "✅ READY" if validation_result.get('overall_pass') else "❌ NOT READY"
    lines.append(f"## Overall: {overall}")
    lines.append("")

    # Rate limit
    rl_ok = "✅" if validation_result.get('rate_limit_ok') else "❌"
    lines.append(f"- Rate Limit: {rl_ok} ({validation_result.get('recent_promotions', 0)}/{MAX_PROMOTIONS_PER_WEEK})")

    # OOS
    oos = validation_result.get('oos_details', {})
    if validation_result.get('oos_skipped'):
        lines.append("- OOS: ⏭️ Skipped")
    elif oos:
        oos_ok = "✅" if validation_result.get('oos_pass') else "❌"
        lines.append(f"- OOS Validation: {oos_ok}")

    # Regression
    reg = validation_result.get('regression_details', {})
    reg_ok = "✅" if validation_result.get('regression_pass') else "❌"
    lines.append(f"- Regression Check: {reg_ok}")

    # Metric comparisons
    comparisons = reg.get('comparisons', {})
    if comparisons:
        lines.extend(["", "## Metric Comparison", "", "| Metric | Baseline | Candidate | Delta |",
                       "|--------|----------|-----------|-------|"])
        for metric, data in comparisons.items():
            b = data.get('baseline', 0)
            c = data.get('candidate', 0)
            d = data.get('delta', 0)
            lines.append(f"| {metric} | {b:.4f} | {c:.4f} | {d:+.4f} |")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args():
    parser = argparse.ArgumentParser(description='Atlas Research Promotion Pipeline')
    parser.add_argument('--stage', action='store_true', help='Stage a candidate config')
    parser.add_argument('--check', action='store_true', help='Validate a candidate (OOS + regression)')
    parser.add_argument('--promote', action='store_true', help='Promote an approved candidate')
    parser.add_argument('--reject', action='store_true', help='Reject a candidate')
    parser.add_argument('--watchdog', action='store_true', help='Check for post-promotion degradation')
    parser.add_argument('--experiment-id', type=str, help='Experiment ID')
    parser.add_argument('--market', type=str, default='sp500', help='Market ID')
    parser.add_argument('--reason', type=str, default='', help='Rejection reason')
    parser.add_argument('--skip-oos', action='store_true', help='Skip OOS validation')
    parser.add_argument('--days', type=int, default=ROLLBACK_WATCH_DAYS, help='Watchdog lookback days')
    parser.add_argument('--notify', action='store_true', help='Send Telegram notification')
    parser.add_argument('--enable-strategy', type=str, help='Strategy to enable in candidate')
    parser.add_argument('--strategy-params', type=str, help='JSON string of strategy params to apply')
    return parser.parse_args()


def main():
    args = parse_args()

    if args.stage:
        if not args.experiment_id:
            print("Error: --experiment-id required for --stage")
            return 1
        strategy_params = json.loads(args.strategy_params) if args.strategy_params else None
        path = stage_candidate(args.experiment_id, args.market,
                               strategy_params=strategy_params,
                               enable_strategy=args.enable_strategy)
        print(f"Staged: {path}")

    elif args.check:
        if not args.experiment_id:
            print("Error: --experiment-id required for --check")
            return 1
        result = validate_candidate(args.experiment_id, args.market,
                                    skip_oos=args.skip_oos)
        print(json.dumps(result, indent=2, default=str))
        if args.notify and result.get('overall_pass'):
            send_promotion_request(args.experiment_id, args.market, result)

        # Print summary
        summary = generate_promotion_summary(args.experiment_id, args.market, result)
        print(f"\n{summary}")

    elif args.promote:
        if not args.experiment_id:
            print("Error: --experiment-id required for --promote")
            return 1
        result = promote_candidate(args.experiment_id, args.market)
        print(json.dumps(result, indent=2, default=str))

    elif args.reject:
        if not args.experiment_id:
            print("Error: --experiment-id required for --reject")
            return 1
        result = reject_candidate(args.experiment_id, args.market, args.reason)
        print(json.dumps(result, indent=2, default=str))

    elif args.watchdog:
        result = watchdog_check(args.market, args.days)
        print(json.dumps(result, indent=2, default=str))
        if result.get('needs_review'):
            print("\n⚠️  DEGRADATION DETECTED — review needed!")

    else:
        print("Error: specify one of --stage, --check, --promote, --reject, --watchdog")
        return 1

    return 0


if __name__ == '__main__':
    sys.exit(main())
