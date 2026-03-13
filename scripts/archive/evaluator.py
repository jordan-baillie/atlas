#!/usr/bin/env python3
"""
Atlas Experiment Evaluator — Deterministic verdict engine with auto-advance.

Evaluates experiment results against acceptance criteria and auto-advances
lifecycle stages (solo → optimize → combined → oos → promote).

Also provides the autoresearch-style keep_or_discard() for the loop engine.

Usage:
    from research.evaluator import ExperimentEvaluator
    evaluator = ExperimentEvaluator()
    result = evaluator.evaluate('exp_001', metrics, stage='solo')
    evaluator.auto_advance('exp_001', result['verdict'], 'solo', 'mean_reversion')

    # Autoresearch-style binary decision:
    verdict = evaluator.keep_or_discard(baseline_metrics, experiment_metrics)
"""

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import numpy as np
from scipy import stats

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# ─── Stage → ExperimentType mapping ──────────────────────────────────────────

_STAGE_TO_METHOD = {
    "solo": "single_strategy_test",
    "optimize": "full_optimization",
    "combined": "combined_portfolio_test",
    "oos": "oos_validation",
}

# Lifecycle order
_STAGE_ORDER = ["solo", "optimize", "combined", "oos"]


class ExperimentEvaluator:
    """Evaluates experiment results against acceptance criteria and auto-advances lifecycle."""

    # Default acceptance criteria (used when experiment doesn't specify its own)
    DEFAULT_CRITERIA: dict[str, dict] = {
        "solo": {
            "min_sharpe": 0.3,
            "min_trades": 15,
            "max_max_drawdown_pct": 15,
        },
        "optimize": {
            "min_sharpe": 0.4,
            "min_trades": 15,
            "max_max_drawdown_pct": 12,
        },
        "combined": {
            "min_sharpe": 0.3,
            "min_trades": 50,
            "max_max_drawdown_pct": 10,
            "min_profit_factor": 1.0,
        },
        "oos": {
            "min_sharpe": 0.2,
            "min_trades": 10,
            "max_max_drawdown_pct": 15,
        },
    }

    # Metric aliases — maps criterion key to possible metric dict keys (first match wins)
    _METRIC_ALIASES: dict[str, list[str]] = {
        "sharpe": ["sharpe", "combined_sharpe"],
        "trades": ["total_trades", "trades", "trade_count"],
        "max_drawdown_pct": ["max_drawdown_pct", "max_dd", "max_dd_pct", "combined_dd"],
        "profit_factor": ["profit_factor", "pf"],
        "cagr_pct": ["cagr_pct", "cagr", "combined_cagr"],
        "win_rate_pct": ["win_rate_pct", "win_rate", "wr"],
        "sortino": ["sortino"],
    }

    def _resolve_metric(self, metrics: dict, criterion_key: str) -> Optional[float]:
        """Resolve a criterion key to its actual value from the metrics dict.

        Handles aliases (e.g. 'max_drawdown_pct' → checks 'max_dd', 'max_dd_pct', etc.)
        and strips the min_/max_ prefix to find the base metric name.
        """
        # Strip prefix to get base name
        base = criterion_key
        for prefix in ("min_", "max_"):
            if criterion_key.startswith(prefix):
                base = criterion_key[len(prefix):]
                break

        # Try aliases first
        aliases = self._METRIC_ALIASES.get(base, [base])
        for alias in aliases:
            v = metrics.get(alias)
            if v is not None:
                try:
                    return float(v)
                except (TypeError, ValueError):
                    pass
        return None

    def evaluate(
        self,
        experiment_id: str,
        metrics: dict,
        acceptance_criteria: dict = None,
        stage: str = None,
    ) -> dict:
        """Evaluate experiment metrics against acceptance criteria.

        Args:
            experiment_id: Experiment ID string
            metrics: Dict with keys like sharpe, cagr_pct, max_drawdown_pct,
                     total_trades, profit_factor, win_rate_pct
            acceptance_criteria: Custom criteria dict (e.g. {'min_sharpe': 0.5}),
                                  or None to use defaults for stage
            stage: Lifecycle stage ('solo'/'optimize'/'combined'/'oos') —
                   used to pick default criteria when acceptance_criteria is None

        Returns:
            {
                "verdict": "pass" | "fail" | "partial",
                "criteria_results": {
                    criterion: {"threshold": x, "actual": y, "passed": bool}
                },
                "failing_criteria": [names],
                "passing_criteria": [names],
                "rationale": "human-readable summary"
            }
        """
        # Determine criteria to use
        if acceptance_criteria is not None:
            criteria = acceptance_criteria
        elif stage is not None and stage in self.DEFAULT_CRITERIA:
            criteria = self.DEFAULT_CRITERIA[stage]
        else:
            # Fall back to solo criteria as a safe default
            criteria = self.DEFAULT_CRITERIA.get("solo", {})

        criteria_results: dict[str, dict] = {}
        failing: list[str] = []
        passing: list[str] = []

        for criterion, threshold in criteria.items():
            actual = self._resolve_metric(metrics, criterion)

            if actual is None:
                # Missing metric → treat as failing (conservative)
                criteria_results[criterion] = {
                    "threshold": threshold,
                    "actual": None,
                    "passed": False,
                }
                failing.append(criterion)
                continue

            # Determine pass/fail direction from prefix
            if criterion.startswith("min_"):
                passed = actual >= threshold
            elif criterion.startswith("max_"):
                passed = actual <= threshold
            else:
                # No prefix → treat as min_ by default
                passed = actual >= threshold

            criteria_results[criterion] = {
                "threshold": threshold,
                "actual": actual,
                "passed": passed,
            }
            if passed:
                passing.append(criterion)
            else:
                failing.append(criterion)

        # Determine overall verdict
        total = len(criteria)
        n_pass = len(passing)
        n_fail = len(failing)

        if total == 0:
            verdict = "pass"  # No criteria → pass by default
            rationale = "No acceptance criteria defined — defaulting to pass."
        elif n_fail == 0:
            verdict = "pass"
            rationale = self._build_rationale(
                "pass", experiment_id, passing, failing, criteria_results
            )
        elif n_pass == 0:
            verdict = "fail"
            rationale = self._build_rationale(
                "fail", experiment_id, passing, failing, criteria_results
            )
        else:
            # Partial: some pass, some fail
            verdict = "partial"
            rationale = self._build_rationale(
                "partial", experiment_id, passing, failing, criteria_results
            )

        # Compute DSR as informational metric on every evaluation
        sharpe_val = self._resolve_metric(metrics, "sharpe")
        dsr_info = None
        if sharpe_val is not None and sharpe_val != 0:
            try:
                dsr_info = self.deflated_sharpe_ratio(
                    observed_sr=sharpe_val, n_strategies=29, T_months=60,
                )
            except Exception:
                pass  # DSR is informational — never block on failure

        result = {
            "verdict": verdict,
            "criteria_results": criteria_results,
            "failing_criteria": failing,
            "passing_criteria": passing,
            "rationale": rationale,
        }
        if dsr_info is not None:
            result["dsr"] = dsr_info
        return result

    def _build_rationale(
        self,
        verdict: str,
        experiment_id: str,
        passing: list[str],
        failing: list[str],
        criteria_results: dict,
    ) -> str:
        """Build a human-readable rationale string."""
        parts: list[str] = [f"[{experiment_id}] Verdict: {verdict.upper()}."]

        if passing:
            pass_details = []
            for c in passing:
                r = criteria_results[c]
                actual_str = f"{r['actual']:.3f}" if r["actual"] is not None else "N/A"
                pass_details.append(f"{c}={actual_str} (≥{r['threshold']})" if c.startswith("min_") else
                                    f"{c}={actual_str} (≤{r['threshold']})")
            parts.append(f"PASSED: {', '.join(pass_details)}.")

        if failing:
            fail_details = []
            for c in failing:
                r = criteria_results[c]
                actual_str = f"{r['actual']:.3f}" if r["actual"] is not None else "missing"
                if r["actual"] is None:
                    fail_details.append(f"{c}=missing")
                elif c.startswith("min_"):
                    fail_details.append(f"{c}={actual_str} (need ≥{r['threshold']})")
                elif c.startswith("max_"):
                    fail_details.append(f"{c}={actual_str} (need ≤{r['threshold']})")
                else:
                    fail_details.append(f"{c}={actual_str} (need ≥{r['threshold']})")
            parts.append(f"FAILED: {', '.join(fail_details)}.")

        return " ".join(parts)

    # ── Autoresearch-style keep/discard ──────────────────────────────────

    SIMPLICITY_THRESHOLD = 0.02  # Sharpe improvement required per added param

    def keep_or_discard(
        self,
        baseline_metrics: dict,
        experiment_metrics: dict,
        params_added: int = 0,
    ) -> dict:
        """Binary keep/discard decision for the autoresearch loop.

        Keep if:
        1. Sharpe improved by a meaningful amount (≥ 0.01 base)
        2. Trade count didn't collapse (≥ 70% of baseline or ≥ 10)
        3. Max drawdown didn't explode (≤ 150% of baseline or ≤ 20%)
        4. Simplicity: improvement per added param ≥ threshold

        Args:
            baseline_metrics:   Metrics dict from the current best.
            experiment_metrics: Metrics dict from the experiment.
            params_added:       Net new params added (negative = simplification).

        Returns:
            {"decision": "keep"|"discard", "rationale": str,
             "delta_sharpe": float, "delta_trades": int, "delta_dd": float,
             "simplicity_ok": bool}
        """
        b_sharpe = float(baseline_metrics.get("sharpe", 0) or 0)
        e_sharpe = float(experiment_metrics.get("sharpe", 0) or 0)
        b_trades = int(baseline_metrics.get("total_trades", 0) or 0)
        e_trades = int(experiment_metrics.get("total_trades", 0) or 0)
        b_dd = float(baseline_metrics.get("max_drawdown_pct", 0) or 0)
        e_dd = float(experiment_metrics.get("max_drawdown_pct", 0) or 0)

        delta_sharpe = round(e_sharpe - b_sharpe, 4)
        delta_trades = e_trades - b_trades
        delta_dd = round(e_dd - b_dd, 2)
        reasons: list[str] = []

        # Gate 1: Sharpe must improve (bar scales with complexity)
        min_improvement = 0.01
        if params_added > 0:
            min_improvement = self.SIMPLICITY_THRESHOLD * max(params_added, 1)
        elif params_added < 0:
            min_improvement = 0.0  # simplification: any non-negative delta is fine

        simplicity_ok = delta_sharpe >= min_improvement
        if not simplicity_ok:
            reasons.append(
                f"Sharpe {delta_sharpe:+.4f} < threshold +{min_improvement:.3f}"
            )

        # Gate 2: Trades can't collapse
        min_trades = max(10, int(b_trades * 0.7)) if b_trades > 0 else 10
        if e_trades < min_trades:
            reasons.append(f"Trades {e_trades} < {min_trades}")

        # Gate 3: Drawdown can't explode
        max_dd = max(20.0, b_dd * 1.5) if b_dd > 0 else 20.0
        if e_dd > max_dd:
            reasons.append(f"DD {e_dd:.1f}% > {max_dd:.1f}%")

        if reasons:
            decision = "discard"
            rationale = "DISCARD: " + "; ".join(reasons)
        else:
            decision = "keep"
            parts = [f"Sharpe {delta_sharpe:+.4f}"]
            if delta_trades != 0:
                parts.append(f"trades {delta_trades:+d}")
            if abs(delta_dd) > 0.01:
                parts.append(f"DD {delta_dd:+.1f}%")
            rationale = "KEEP: " + ", ".join(parts)

        return {
            "decision": decision,
            "rationale": rationale,
            "delta_sharpe": delta_sharpe,
            "delta_trades": delta_trades,
            "delta_dd": delta_dd,
            "simplicity_ok": simplicity_ok,
        }

    # ── Statistical Rigour Methods ────────────────────────────────────────

    def deflated_sharpe_ratio(
        self,
        observed_sr: float,
        n_strategies: int,
        T_months: int,
        skew: float = 0.0,
        kurt: float = 3.0,
    ) -> dict:
        """Bailey & López de Prado (2014) Deflated Sharpe Ratio.

        Computes the probability that the observed Sharpe ratio is significant
        after correcting for multiple strategy testing.

        Args:
            observed_sr:   Annualised Sharpe ratio to test.
            n_strategies:  Number of strategies tried (including this one).
            T_months:      Number of months in the backtest sample.
            skew:          Return distribution skewness (default 0 = normal).
            kurt:          Return distribution kurtosis (default 3 = normal).

        Returns:
            dict with keys: dsr_pvalue, expected_max_sr, observed_sr,
                            n_strategies_tested, T_months, is_significant
        """
        gamma = 0.5772156649  # Euler-Mascheroni constant
        N = max(n_strategies, 1)

        # E[max(SR)] under the null — Euler-Mascheroni approximation
        if N > 1:
            e_max_sr = (
                (1 - gamma) * stats.norm.ppf(1 - 1 / N)
                + gamma * stats.norm.ppf(1 - 1 / (N * np.e))
            )
        else:
            e_max_sr = 0.0

        # Convert annualised SR to monthly SR for the SE formula (Lo 2002)
        sr_monthly = observed_sr / np.sqrt(12)
        e_max_sr_monthly = e_max_sr / np.sqrt(12)

        # Standard error of the Sharpe ratio with skew/kurtosis correction
        se = np.sqrt(
            (1 - skew * sr_monthly + (kurt - 1) / 4 * sr_monthly ** 2) / T_months
        )

        # DSR test statistic and p-value
        if se > 0:
            dsr_stat = (sr_monthly - e_max_sr_monthly) / se
            dsr_pvalue = float(1 - stats.norm.cdf(dsr_stat))
        else:
            dsr_pvalue = 1.0

        return {
            "dsr_pvalue": round(dsr_pvalue, 4),
            "expected_max_sr": round(float(e_max_sr), 4),
            "observed_sr": round(float(observed_sr), 4),
            "n_strategies_tested": n_strategies,
            "T_months": T_months,
            "is_significant": dsr_pvalue < 0.05,
        }

    def parameter_stability_test(
        self,
        metrics_at_variations: list,
        base_sharpe: float,
        tolerance: float = 0.50,
    ) -> dict:
        """Test if Sharpe is stable when params vary ±15%.

        Args:
            metrics_at_variations: list of dicts, each with a 'sharpe' key
            base_sharpe:           the Sharpe at base params
            tolerance:             max allowed relative change (0.50 = 50%)

        Returns:
            dict with: is_stable, min_sharpe, max_sharpe, base_sharpe,
                       max_relative_change, tolerance, n_variations
        """
        sharpes = [m.get("sharpe", 0) for m in metrics_at_variations]
        if not sharpes or base_sharpe == 0:
            return {"is_stable": False, "reason": "insufficient data"}

        min_s = min(sharpes)
        max_s = max(sharpes)
        max_change = (
            max(abs(s - base_sharpe) / abs(base_sharpe) for s in sharpes)
            if base_sharpe != 0
            else 999
        )

        return {
            "is_stable": max_change <= tolerance,
            "min_sharpe": round(min_s, 4),
            "max_sharpe": round(max_s, 4),
            "base_sharpe": round(base_sharpe, 4),
            "max_relative_change": round(max_change, 4),
            "tolerance": tolerance,
            "n_variations": len(sharpes),
        }

    def sub_period_test(
        self,
        period_sharpes: list,
        min_profitable_ratio: float = 0.6,
    ) -> dict:
        """Test if strategy is profitable across majority of sub-periods.

        Args:
            period_sharpes:        Sharpe ratios for each sub-period.
            min_profitable_ratio:  Fraction of periods that must be positive.

        Returns:
            dict with: passes, n_profitable, n_periods, profitable_ratio,
                       min_required, period_sharpes
        """
        n_pos = sum(1 for s in period_sharpes if s > 0)
        ratio = n_pos / len(period_sharpes) if period_sharpes else 0.0
        return {
            "passes": ratio >= min_profitable_ratio,
            "n_profitable": n_pos,
            "n_periods": len(period_sharpes),
            "profitable_ratio": round(ratio, 4),
            "min_required": min_profitable_ratio,
            "period_sharpes": [round(s, 4) for s in period_sharpes],
        }

    def run_statistical_validation(
        self,
        observed_sr: float,
        n_strategies: int = 29,
        T_months: int = 60,
        skew: float = 0.0,
        kurt: float = 3.0,
        metrics_at_variations: Optional[list] = None,
        base_sharpe: Optional[float] = None,
        stability_tolerance: float = 0.50,
        period_sharpes: Optional[list] = None,
        min_profitable_ratio: float = 0.6,
    ) -> dict:
        """Run all three statistical validation tests and return a combined result.

        Args:
            observed_sr:            Annualised Sharpe ratio to validate.
            n_strategies:           Strategies tested (DSR correction).
            T_months:               Backtest length in months.
            skew:                   Return skewness (DSR correction).
            kurt:                   Return kurtosis (DSR correction).
            metrics_at_variations:  list[dict] for parameter stability test
                                    (each dict must have 'sharpe' key).
                                    If None, stability test is skipped.
            base_sharpe:            Base-param Sharpe for stability test.
                                    Defaults to observed_sr if not provided.
            stability_tolerance:    Max relative Sharpe change (default 0.50).
            period_sharpes:         list[float] Sharpe per sub-period.
                                    If None, sub-period test is skipped.
            min_profitable_ratio:   Fraction of profitable sub-periods required.

        Returns:
            dict with keys:
              - dsr: result of deflated_sharpe_ratio()
              - stability: result of parameter_stability_test() or None
              - sub_period: result of sub_period_test() or None
              - overall_pass: True only when every available test passes
              - summary: human-readable string
        """
        dsr = self.deflated_sharpe_ratio(observed_sr, n_strategies, T_months, skew, kurt)

        stability = None
        if metrics_at_variations is not None:
            _base = base_sharpe if base_sharpe is not None else observed_sr
            stability = self.parameter_stability_test(
                metrics_at_variations, _base, stability_tolerance
            )

        sub_period = None
        if period_sharpes is not None:
            sub_period = self.sub_period_test(period_sharpes, min_profitable_ratio)

        # Aggregate pass/fail
        tests_pass: list[bool] = [dsr["is_significant"]]
        if stability is not None:
            tests_pass.append(stability.get("is_stable", False))
        if sub_period is not None:
            tests_pass.append(sub_period.get("passes", False))

        overall_pass = all(tests_pass)

        # Build human-readable summary
        parts = [
            f"DSR p={dsr['dsr_pvalue']:.4f} "
            f"({'✓' if dsr['is_significant'] else '✗'}, "
            f"E[SR_max]={dsr['expected_max_sr']:.2f})"
        ]
        if stability is not None:
            parts.append(
                f"Stability {'✓' if stability.get('is_stable') else '✗'} "
                f"(max_chg={stability.get('max_relative_change', 'N/A'):.1%})"
            )
        if sub_period is not None:
            parts.append(
                f"SubPeriods {'✓' if sub_period.get('passes') else '✗'} "
                f"({sub_period['n_profitable']}/{sub_period['n_periods']} profitable)"
            )

        summary = f"overall={'PASS' if overall_pass else 'FAIL'}: " + " | ".join(parts)

        return {
            "dsr": dsr,
            "stability": stability,
            "sub_period": sub_period,
            "overall_pass": overall_pass,
            "summary": summary,
        }

    @staticmethod
    def complexity_score(params: dict, default_params: dict) -> int:
        """Count non-default parameters (a rough complexity measure).

        Args:
            params:         Current parameter dict.
            default_params: Default parameter dict for comparison.

        Returns:
            Number of parameters that differ from defaults.
        """
        diff = 0
        for k, v in params.items():
            if k not in default_params or default_params[k] != v:
                diff += 1
        return diff

    def get_next_stage(self, current_stage: str) -> Optional[str]:
        """Return next lifecycle stage, or None if at end.

        solo → optimize → combined → oos → None (promote signal)
        """
        try:
            idx = _STAGE_ORDER.index(current_stage)
        except ValueError:
            return None
        next_idx = idx + 1
        if next_idx >= len(_STAGE_ORDER):
            return None  # At 'oos' → next is 'promote' (handled separately)
        return _STAGE_ORDER[next_idx]

    def auto_advance(
        self,
        experiment_id: str,
        verdict: str,
        current_stage: str,
        strategy_name: str,
        market: str = "sp500",
        optimized_params: dict = None,
    ) -> Optional[dict]:
        """If verdict is 'pass', create and queue the next lifecycle experiment.

        Stage transitions:
        - solo → optimize: creates FULL_OPTIMIZATION experiment
        - optimize → combined: creates COMBINED_PORTFOLIO_TEST with optimized params
        - combined → oos: creates OOS_VALIDATION experiment
        - oos → promote: returns signal dict (does NOT auto-promote — needs human)

        Args:
            experiment_id: Source experiment ID
            verdict: 'pass', 'fail', or 'partial'
            current_stage: Current lifecycle stage
            strategy_name: Strategy identifier
            market: Market identifier (default: 'sp500')
            optimized_params: Optimized params from previous stage (used for combined/oos)

        Returns:
            The created QueueEntry as a dict, or None if no advance (fail/end stage).
        """
        if verdict != "pass":
            return None

        next_stage = self.get_next_stage(current_stage)

        # Special case: oos is final stage — return promote signal with DSR check
        if current_stage == "oos":
            promote_result = {
                "action": "promote",
                "experiment_id": experiment_id,
                "strategy_name": strategy_name,
                "market": market,
                "message": (
                    f"Strategy '{strategy_name}' has passed OOS validation. "
                    "Manual review and promotion required."
                ),
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
            # Add DSR warning if optimized params contain a Sharpe value
            if optimized_params and "sharpe" in optimized_params:
                try:
                    dsr = self.deflated_sharpe_ratio(
                        optimized_params["sharpe"], n_strategies=29, T_months=60,
                    )
                    promote_result["dsr"] = dsr
                    if not dsr["is_significant"]:
                        promote_result["dsr_warning"] = (
                            f"⚠️ DSR: Sharpe {dsr['observed_sr']:.2f} is NOT statistically "
                            f"significant after testing 29 strategies (p={dsr['dsr_pvalue']:.3f}). "
                            f"Expected max SR by chance alone: {dsr['expected_max_sr']:.2f}."
                        )
                except Exception:
                    pass
            return promote_result

        if next_stage is None:
            return None

        # Lazy import to avoid circular imports and allow testing without full research stack
        try:
            from research.models import (
                QueueEntry,
                ExperimentType,
                Priority,
                ExperimentStatus,
                append_to_queue,
                generate_experiment_id,
            )
        except ImportError as e:
            raise ImportError(
                f"Cannot auto-advance: research.models not available. {e}"
            ) from e

        now = datetime.now(timezone.utc).isoformat()
        new_id = generate_experiment_id()
        next_method = _STAGE_TO_METHOD[next_stage]

        # Build the next experiment based on transition type
        if current_stage == "solo" and next_stage == "optimize":
            # solo → optimize: full coordinate descent
            entry = QueueEntry(
                id=new_id,
                title=f"{strategy_name} optimization (auto-advanced from {experiment_id})",
                category="dormant",
                market=market,
                hypothesis=(
                    f"Optimize {strategy_name} parameters after passing solo test. "
                    f"Source: {experiment_id}."
                ),
                method=ExperimentType.FULL_OPTIMIZATION,
                acceptance_criteria=self.DEFAULT_CRITERIA["optimize"],
                estimated_runtime_min=60,
                priority=Priority.P2_HIGH,
                strategy_name=strategy_name,
                params_override={
                    "param_grid": _default_param_grid(strategy_name),
                },
                depends_on=[experiment_id],
                tags=["auto-advance", f"stage/{next_stage}", f"strategy/{strategy_name}"],
                notes=f"Auto-created from auto_advance() after {experiment_id} passed solo.",
            )

        elif current_stage == "optimize" and next_stage == "combined":
            # optimize → combined: combined portfolio test with optimized params
            params: dict = {}
            if optimized_params:
                params["strategy_params"] = optimized_params
            entry = QueueEntry(
                id=new_id,
                title=f"{strategy_name} combined portfolio test (auto-advanced from {experiment_id})",
                category="dormant",
                market=market,
                hypothesis=(
                    f"Test {strategy_name} in combined portfolio after optimization. "
                    f"Source: {experiment_id}."
                ),
                method=ExperimentType.COMBINED_PORTFOLIO_TEST,
                acceptance_criteria=self.DEFAULT_CRITERIA["combined"],
                estimated_runtime_min=30,
                priority=Priority.P2_HIGH,
                strategy_name=strategy_name,
                params_override=params or None,
                depends_on=[experiment_id],
                tags=["auto-advance", f"stage/{next_stage}", f"strategy/{strategy_name}"],
                notes=f"Auto-created from auto_advance() after {experiment_id} passed optimize.",
            )

        elif current_stage == "combined" and next_stage == "oos":
            # combined → oos: out-of-sample validation
            params = {}
            if optimized_params:
                params["strategy_params"] = optimized_params
            entry = QueueEntry(
                id=new_id,
                title=f"{strategy_name} OOS validation (auto-advanced from {experiment_id})",
                category="dormant",
                market=market,
                hypothesis=(
                    f"Validate {strategy_name} on out-of-sample data after passing combined test. "
                    f"Source: {experiment_id}."
                ),
                method=ExperimentType.OOS_VALIDATION,
                acceptance_criteria=self.DEFAULT_CRITERIA["oos"],
                estimated_runtime_min=20,
                priority=Priority.P2_HIGH,
                strategy_name=strategy_name,
                params_override=params or None,
                depends_on=[experiment_id],
                tags=["auto-advance", f"stage/{next_stage}", f"strategy/{strategy_name}"],
                notes=f"Auto-created from auto_advance() after {experiment_id} passed combined.",
            )

        else:
            # Unrecognized transition — don't queue
            return None

        append_to_queue(entry, skip_validation=True)
        return entry.to_dict()

    def auto_defer(self, experiment_id: str, strategy_name: str) -> None:
        """On FAIL, find any downstream experiments in queue for this strategy
        and set them to DEFERRED status.

        E.g., if solo fails, defer the downstream optimize experiment.

        Args:
            experiment_id: The failed experiment ID
            strategy_name: Strategy that failed
        """
        try:
            from research.models import (
                read_queue,
                update_queue_entry,
                ExperimentStatus,
            )
        except ImportError:
            return  # Gracefully skip if models not available

        queue = read_queue()
        for item in queue:
            # Defer downstream experiments that depend on the failed experiment
            deps = item.get("depends_on", [])
            if experiment_id in deps and item.get("status") == ExperimentStatus.QUEUED:
                update_queue_entry(
                    item["id"],
                    {
                        "status": ExperimentStatus.DEFERRED,
                        "notes": (
                            item.get("notes", "")
                            + f"\n[auto-deferred] Upstream experiment {experiment_id} "
                            f"failed for strategy '{strategy_name}'."
                        ),
                    },
                )


# ─── Default param grids ─────────────────────────────────────────────────────


def _default_param_grid(strategy_name: str) -> dict[str, list]:
    """Return a sensible default param grid for a strategy, used for auto-advance.

    These are conservative grids — enough to kick off optimization.
    The actual backtester will determine which values are tried.
    """
    grids: dict[str, dict[str, list]] = {
        "mean_reversion": {
            "rsi_period": [7, 10, 14, 21],
            "rsi_oversold": [20, 25, 30, 35],
            "z_score_entry": [1.0, 1.5, 2.0, 2.5],
            "atr_stop_mult": [1.5, 2.0, 2.5, 3.0],
        },
        "trend_following": {
            "fast_ma": [10, 20, 50],
            "slow_ma": [50, 100, 200],
            "atr_stop_mult": [1.5, 2.0, 2.5, 3.0],
        },
        "opening_gap": {
            "gap_pct": [0.5, 1.0, 1.5, 2.0],
            "ibs_threshold": [0.2, 0.3, 0.4, 0.5],
            "max_hold": [3, 5, 7, 10],
        },
        "momentum_breakout": {
            "lookback": [20, 40, 60],
            "trend_ma": [50, 100, 200],
            "atr_stop_mult": [1.5, 2.0, 2.5],
        },
        "short_term_mr": {
            "rsi_period": [2, 3, 4, 5],
            "rsi_oversold": [10, 15, 20, 25],
            "max_hold": [2, 3, 5, 7],
        },
        "sector_rotation": {
            "momentum_period": [20, 40, 60, 90],
            "n_sectors": [1, 2, 3, 4, 5],
            "rebalance_period": [5, 10, 20],
        },
        "bb_squeeze": {
            "bb_period": [10, 20, 30],
            "bb_std": [1.5, 2.0, 2.5],
            "kc_period": [10, 20, 30],
            "kc_mult": [1.0, 1.5, 2.0],
        },
        "triple_rsi": {
            "rsi_period": [3, 5, 7, 10],
            "rsi_threshold": [20, 25, 30, 35],
            "hold_days": [2, 3, 5],
        },
    }
    return grids.get(strategy_name, {
        "lookback": [10, 20, 30, 40, 60],
    })
