#!/usr/bin/env python3
"""
Atlas Experiment Evaluator — Deterministic verdict engine with auto-advance.

Evaluates experiment results against acceptance criteria and auto-advances
lifecycle stages (solo → optimize → combined → oos → promote).

Usage:
    from research.evaluator import ExperimentEvaluator
    evaluator = ExperimentEvaluator()
    result = evaluator.evaluate('exp_001', metrics, stage='solo')
    evaluator.auto_advance('exp_001', result['verdict'], 'solo', 'mean_reversion')
"""

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

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

        return {
            "verdict": verdict,
            "criteria_results": criteria_results,
            "failing_criteria": failing,
            "passing_criteria": passing,
            "rationale": rationale,
        }

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

        # Special case: oos is final stage — return promote signal
        if current_stage == "oos":
            return {
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
