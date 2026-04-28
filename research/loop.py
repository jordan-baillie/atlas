#!/usr/bin/env python3
"""Atlas Autoresearch Loop — LLM-driven experiment engine.

Inspired by karpathy/autoresearch. The LLM agent drives the loop:
read program.md → propose params → run experiment → keep/discard → repeat.

The agent calls these functions. The intelligence is in the agent's head.
This module just runs backtests and tracks results.

Design principles (from karpathy/autoresearch):
- Fixed evaluation: backtest engine is immutable (like prepare.py)
- Binary keep/discard: every experiment either advances or reverts
- Simple results tracking: TSV per strategy
- Best-known params: JSON per strategy
- Simplicity criterion: complexity cost vs improvement magnitude
- Never stop: the agent runs experiments indefinitely

Usage (from LLM agent via pi):

    import sys; sys.path.insert(0, '/root/atlas')
    from research.loop import ResearchSession

    s = ResearchSession('mean_reversion', 'sp500')
    s.baseline()                                          # establish baseline
    r = s.experiment({'rsi_period': 7}, 'shorter RSI')    # try something
    s.keep()     # if improved
    s.discard()  # if not
    print(s.history())
"""

import copy
import json
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from backtest.metrics import calc_deflated_sharpe


ATLAS_ROOT = Path(__file__).resolve().parent.parent
if str(ATLAS_ROOT) not in sys.path:
    sys.path.insert(0, str(ATLAS_ROOT))

logger = logging.getLogger("autoresearch")

# Module-level imports for test patchability
try:
    from research.freshness import check_freshness  # noqa: F401
except Exception:  # pragma: no cover
    check_freshness = None  # type: ignore[assignment]

try:
    from db.atlas_db import upsert_research_best  # noqa: F401
except Exception:  # pragma: no cover
    upsert_research_best = None  # type: ignore[assignment]

BEST_DIR = ATLAS_ROOT / "research" / "best"
RESULTS_DIR = ATLAS_ROOT / "research" / "results"
JOURNAL_PATH = ATLAS_ROOT / "research" / "journal.json"

# TSV header
_TSV_HEADER = "timestamp\tsharpe\ttrades\tmax_dd_pct\tpf\tcagr_pct\tparams_changed\tstatus\tdescription"


# ─── Results TSV ─────────────────────────────────────────────────────────────


def _results_path(strategy: str) -> Path:
    return RESULTS_DIR / f"{strategy}.tsv"


def _ensure_results_file(strategy: str) -> Path:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    path = _results_path(strategy)
    if not path.exists():
        path.write_text(_TSV_HEADER + "\n")
    return path


def _append_result(
    strategy: str,
    metrics: dict,
    params_changed: str,
    status: str,
    description: str,
    market: str = "sp500",
) -> None:
    path = _ensure_results_file(strategy)
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    row = "\t".join([
        ts,
        f"{metrics.get('sharpe', 0):.4f}",
        str(metrics.get('total_trades', 0)),
        f"{metrics.get('max_drawdown_pct', 0):.2f}",
        f"{metrics.get('profit_factor', 0):.4f}",
        f"{metrics.get('cagr_pct', 0):.2f}",
        params_changed,
        status,
        description.replace("\t", " ").replace("\n", " "),
    ])
    with open(path, "a") as f:
        f.write(row + "\n")
    # SQLite dual-write (non-fatal)
    try:
        from research.db import log_experiment
        log_experiment(
            strategy=strategy,
            metrics=metrics,
            params_changed=params_changed,
            status=status,
            description=description,
            market=market,
        )
    except Exception:
        pass  # TSV is primary, SQLite is additive


def read_results(strategy: str, n: int = 50) -> str:
    """Read the last N results for a strategy as a formatted string."""
    path = _results_path(strategy)
    if not path.exists():
        return f"No results yet for {strategy}."
    lines = path.read_text().strip().split("\n")
    if len(lines) <= 1:
        return f"No results yet for {strategy}."
    header = lines[0]
    data_lines = lines[1:]
    recent = data_lines[-n:]
    return header + "\n" + "\n".join(recent)


# ─── Best Params ─────────────────────────────────────────────────────────────


def _best_path(strategy: str, universe: str = "sp500") -> Path:
    if universe and universe != "sp500":
        return BEST_DIR / f"{strategy}_{universe}.json"
    return BEST_DIR / f"{strategy}.json"


def load_best(strategy: str, universe: str = "sp500") -> Optional[dict]:
    """Load the current best-known params for a strategy (optionally per-universe)."""
    path = _best_path(strategy, universe)
    if not path.exists():
        return None
    try:
        with open(path) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def save_best(
    strategy: str,
    market: str,
    params: dict,
    metrics: dict,
    description: str = "",
    solo_sharpe: Optional[float] = None,
    portfolio_sharpe: Optional[float] = None,
) -> None:
    """Save new best-known params for a strategy.

    Writes to both JSON file (keyed by strategy+universe) and SQLite
    research_best table.

    Args:
        solo_sharpe:      Strategy-standalone backtest Sharpe (M2 2026-04-28).
        portfolio_sharpe: Whole-portfolio Sharpe with this strategy (M2 2026-04-28).
                          When both are supplied, metric_type is stored as 'both'.
    """
    universe = market or "sp500"

    # Freshness guard — reject stale or time-regressing writes
    # check_freshness is imported at module level for test patchability
    if check_freshness is not None:
        try:
            _allow, _reason = check_freshness(strategy, universe)
            if not _allow:
                logger.warning("[save_best] blocked by freshness guard: %s", _reason)
                return
        except Exception as _fg_exc:
            logger.debug("[save_best] freshness guard failed (non-fatal): %s", _fg_exc)

    BEST_DIR.mkdir(parents=True, exist_ok=True)
    existing = load_best(strategy, universe) or {}
    existing.update({
        "strategy": strategy,
        "market": market,
        "params": params,
        "metrics": metrics,
        "description": description,
        "experiments_run": existing.get("experiments_run", 0),
        "experiments_kept": existing.get("experiments_kept", 0) + 1,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    })
    path = _best_path(strategy, universe)
    with open(path, "w") as f:
        json.dump(existing, f, indent=2, default=str)

    # Also write to SQLite research_best table
    # upsert_research_best imported at module level for test patchability
    if upsert_research_best is not None:
        try:
            upsert_research_best(
                strategy=strategy,
                universe=universe,
                params=params,
                sharpe=float(metrics.get("sharpe", 0) or 0),
                trades=int(metrics.get("total_trades", 0) or 0),
                max_dd_pct=float(metrics.get("max_drawdown_pct", 0) or 0),
                solo_sharpe=solo_sharpe,
                portfolio_sharpe=portfolio_sharpe,
            )
        except Exception as exc:
            logger.warning("Failed to write research_best to SQLite: %s", exc)

    # Regenerate brain/strategies/{strategy}.md so LLM context stays fresh.
    # Non-fatal — brain.md is a derived artifact, not authoritative.
    try:
        from research.brain.writer import update_strategy
        update_strategy(strategy, metrics, params,
                        description=description or "autoresearch keep")
    except Exception as _bexc:
        import logging
        logging.getLogger(__name__).warning("brain update_strategy failed (non-fatal): %s", _bexc)


def _increment_run_count(strategy: str, universe: str = "sp500") -> None:
    existing = load_best(strategy, universe) or {}
    existing["experiments_run"] = existing.get("experiments_run", 0) + 1
    BEST_DIR.mkdir(parents=True, exist_ok=True)
    path = _best_path(strategy, universe)
    with open(path, "w") as f:
        json.dump(existing, f, indent=2, default=str)


# ─── Journal Compatibility ───────────────────────────────────────────────────


def _append_journal(
    strategy: str,
    market: str,
    metrics: dict,
    status: str,
    description: str,
    params_override: Optional[dict] = None,
) -> None:
    """Append to research/journal.json for compatibility with dashboards."""
    entry = {
        "experiment_id": f"ar-{datetime.now().strftime('%Y%m%d_%H%M%S')}",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "market": market,
        "category": "autoresearch",
        "strategy": strategy,
        "hypothesis": description,
        "verdict": "pass" if status == "keep" else "fail",
        "key_metrics": {
            "sharpe": metrics.get("sharpe", 0),
            "cagr_pct": metrics.get("cagr_pct", 0),
            "max_drawdown_pct": metrics.get("max_drawdown_pct", 0),
            "total_trades": metrics.get("total_trades", 0),
            "profit_factor": metrics.get("profit_factor", 0),
        },
        "delta_vs_baseline": {},
        "learnings": [f"autoresearch: {status} — {description}"],
        "promoted": False,
        "runtime_s": metrics.get("runtime_s", 0),
        "agent_id": "autoresearch",
    }
    try:
        from research.models import _locked_append
        _locked_append(JOURNAL_PATH, entry)
    except Exception as e:
        logger.warning("Failed to append journal: %s", e)


# ─── Keep/Discard Logic ─────────────────────────────────────────────────────


def _get_dsr_stats(market: str = "sp500") -> dict:
    """Pull experiment count and sharpe variance from SQLite for DSR calculation.

    Reads from the production atlas.db via db.atlas_db.get_db() so we
    respect any _db_path_override that tests use.
    """
    try:
        from db.atlas_db import get_db
        with get_db() as conn:
            row = conn.execute(
                "SELECT COUNT(*) as cnt, "
                "COALESCE(AVG((sharpe - sub.avg_s) * (sharpe - sub.avg_s)), 0) as var_s "
                "FROM research_experiments, "
                "(SELECT AVG(sharpe) as avg_s FROM research_experiments "
                " WHERE sharpe IS NOT NULL) sub "
                "WHERE sharpe IS NOT NULL AND universe = ?",
                (market,),
            ).fetchone()
        if row:
            cnt = row[0] if isinstance(row, tuple) else (row["cnt"] if hasattr(row, "keys") else row[0])
            var = row[1] if isinstance(row, tuple) else (row["var_s"] if hasattr(row, "keys") else row[1])
            return {"num_experiments": int(cnt or 0),
                    "variance_of_sharpes": float(var or 0.0)}
    except Exception as exc:
        import logging
        logging.getLogger(__name__).warning("DSR stats query failed: %s", exc)

    return {"num_experiments": 0, "variance_of_sharpes": 0.0}


def keep_or_discard(
    baseline: dict,
    experiment: dict,
    params_added: int = 0,
) -> dict:
    """Binary keep/discard decision.

    Keep if:
    1. Sharpe improved by a meaningful amount
    2. Trade count didn't collapse
    3. Max drawdown didn't explode
    4. Simplicity: improvement justifies any added complexity

    Args:
        baseline:     Metrics dict from the current best.
        experiment:   Metrics dict from the experiment.
        params_added: Net number of new params added (negative = simplification).

    Returns:
        {"decision": "keep"|"discard", "rationale": str,
         "delta_sharpe": float, "delta_trades": int, "delta_dd": float}
    """
    b_sharpe = baseline.get("sharpe", 0) or 0
    e_sharpe = experiment.get("sharpe", 0) or 0
    b_trades = baseline.get("total_trades", 0) or 0
    e_trades = experiment.get("total_trades", 0) or 0
    b_dd = baseline.get("max_drawdown_pct", 0) or 0
    e_dd = experiment.get("max_drawdown_pct", 0) or 0

    delta_sharpe = round(e_sharpe - b_sharpe, 4)
    delta_trades = e_trades - b_trades
    delta_dd = round(e_dd - b_dd, 2)

    reasons = []

    # Gate 1: Sharpe must improve
    min_improvement = 0.01
    if params_added > 0:
        # Adding complexity requires proportionally more improvement
        min_improvement = 0.02 * max(params_added, 1)
    elif params_added < 0:
        # Simplification: keep even if Sharpe is equal (≥ 0 improvement)
        min_improvement = 0.0

    if delta_sharpe < min_improvement:
        reasons.append(
            f"Sharpe +{delta_sharpe:.4f} below threshold +{min_improvement:.3f}"
        )

    # Gate 2: Trades can't collapse
    min_trades = max(10, int(b_trades * 0.7)) if b_trades > 0 else 10
    if e_trades < min_trades:
        reasons.append(
            f"Trades collapsed: {e_trades} < {min_trades} (70% of {b_trades})"
        )

    # Gate 3: Drawdown can't explode
    max_dd = max(20.0, b_dd * 1.5) if b_dd > 0 else 20.0
    if e_dd > max_dd:
        reasons.append(
            f"Drawdown exploded: {e_dd:.1f}% > {max_dd:.1f}% (150% of {b_dd:.1f}%)"
        )

    # Gate 4: Deflated Sharpe Ratio (multiple testing correction)
    if not reasons and e_sharpe > 0:
        try:
            dsr_stats = _get_dsr_stats()
            if dsr_stats["num_experiments"] >= 5:
                # We need returns to compute skewness/kurtosis but don't have them here.
                # Use the experiment sharpe and stats to compute a lightweight DSR check.
                import numpy as np
                from scipy import stats as sp_stats
                n_exp = dsr_stats["num_experiments"]
                var_s = dsr_stats["variance_of_sharpes"]
                if var_s > 0:
                    e_max_s = np.sqrt(var_s) * (
                        (1 - np.euler_gamma) * sp_stats.norm.ppf(1 - 1 / n_exp)
                        + np.euler_gamma * sp_stats.norm.ppf(1 - 1 / (n_exp * np.e))
                    )
                    # O12 guard: the DSR formula assumes Gaussian-like strategy
                    # Sharpe distribution; with long-tailed variance from mixed
                    # strategy×universe experiments it produces impossibly-high
                    # thresholds (e.g. >3.0 Sharpe). When that happens, log but
                    # don't block — this gate will be re-enabled once the
                    # formula is fixed per Bailey et al. (audit item O12).
                    _DSR_SANITY_CAP = 3.0
                    if e_max_s > _DSR_SANITY_CAP:
                        import logging
                        logging.getLogger(__name__).warning(
                            "DSR gate skipped — expected max Sharpe %.2f exceeds sanity cap %.2f "
                            "(n=%d, var=%.4f). See audit item O12.",
                            e_max_s, _DSR_SANITY_CAP, n_exp, var_s,
                        )
                    elif e_sharpe < e_max_s * 0.8:
                        reasons.append(
                            f"DSR: Sharpe {e_sharpe:.4f} < 80% of expected max {e_max_s:.4f} "
                            f"({n_exp} experiments, var={var_s:.4f})"
                        )
        except Exception as exc:
            import logging
            logging.getLogger(__name__).warning("DSR check skipped: %s", exc)

    # Gate 5: Walk-forward window coverage
    # Reject experiments where >20% of planned windows were skipped (no data).
    # Default to 100.0 when the metric isn't present so pre-telemetry
    # code paths don't regress.
    coverage = experiment.get("window_coverage_pct", 100.0) or 100.0
    try:
        coverage = float(coverage)
    except (TypeError, ValueError):
        coverage = 100.0
    if coverage < 80.0 and experiment.get("windows_configured", 0):
        reasons.append(
            f"Window coverage {coverage:.1f}% < 80% "
            f"({experiment.get('windows_used', 0)}/{experiment.get('windows_configured', 0)} windows used)"
        )

    if reasons:
        decision = "discard"
        rationale = "DISCARD: " + "; ".join(reasons)
    else:
        decision = "keep"
        rationale = f"KEEP: Sharpe +{delta_sharpe:.4f}, trades {delta_trades:+d}, DD {delta_dd:+.1f}%"

    return {
        "decision": decision,
        "rationale": rationale,
        "delta_sharpe": delta_sharpe,
        "delta_trades": delta_trades,
        "delta_dd": delta_dd,
    }


# ─── Snapshot Discovery ──────────────────────────────────────────────────────


def _find_latest_snapshot(market: str) -> str:
    """Find the most recent snapshot directory matching *market*.

    Searches ``data/snapshots/`` for directories whose name contains the market
    identifier (case-insensitive), then returns the one with the latest
    modification time.

    Args:
        market: Market ID (e.g. ``'sp500'``).

    Returns:
        Snapshot directory name (e.g. ``'sp500_v3_unadj_20260310_7yr'``).

    Raises:
        RuntimeError: If no snapshot directory exists for this market.
    """
    snapshots_root = ATLAS_ROOT / "data" / "snapshots"
    if not snapshots_root.exists():
        raise RuntimeError(
            f"Snapshots directory not found: {snapshots_root}. "
            f"Create a snapshot first with: "
            f"from scripts.strategy_evaluator import save_snapshot; "
            f"save_snapshot('{market}', '<snapshot_id>')"
        )

    matching = [
        d for d in snapshots_root.iterdir()
        if d.is_dir() and market.lower() in d.name.lower()
    ]
    if not matching:
        raise RuntimeError(
            f"No snapshot found for market '{market}' in {snapshots_root}. "
            f"Create one first with: "
            f"from scripts.strategy_evaluator import save_snapshot; "
            f"save_snapshot('{market}', '<snapshot_id>')"
        )

    # Most recent by modification time
    matching.sort(key=lambda d: d.stat().st_mtime, reverse=True)
    return matching[0].name


# ─── Research Session ────────────────────────────────────────────────────────


class ResearchSession:
    """An autoresearch session for one strategy.

    The LLM agent creates a session, runs experiments, keeps or discards.
    Market data is loaded once at init (expensive), then reused.

    Args:
        strategy: Strategy name (e.g. 'mean_reversion').
        market:   Market ID (default 'sp500').
        top_n:    Number of tickers to use (default None = all).
                  Set to 50 for faster ~15s iterations during screening.
                  Set to None for full-universe accuracy during final optimization.

    Typical usage:
        s = ResearchSession('mean_reversion')
        s.baseline()                                  # always first
        r = s.experiment({'rsi_period': 7}, 'shorter RSI')
        s.keep()   # or s.discard()
        print(s.history())
    """

    def __init__(
        self,
        strategy: str,
        market: str = "sp500",
        top_n: Optional[int] = None,
        snapshot_id: Optional[str] = None,
    ):
        """Initialise a research session for *strategy* on *market*.

        Args:
            strategy:    Strategy name (e.g. ``'mean_reversion'``).
            market:      Market ID (default ``'sp500'``).
            top_n:       Number of tickers to use (default ``None`` = all).
                         Set to 50 for faster ~15 s iterations during screening.
            snapshot_id: Data snapshot to use for all backtests in this session.
                         If ``None``, the most recent snapshot matching *market*
                         is auto-discovered.  Raises ``RuntimeError`` if no
                         snapshot exists — create one first with
                         ``save_snapshot(market, snapshot_id)``.
        """
        self.strategy = strategy
        self.market = market
        self.top_n = top_n
        self._data: Optional[dict] = None
        self._config: Optional[dict] = None
        self._baseline_metrics: Optional[dict] = None
        self._last_experiment: Optional[dict] = None  # {params, metrics, description}
        self._experiments_run = 0
        self._experiments_kept = 0
        self._session_start = time.time()

        # Unique session identifier used for lock files
        self.session_id = (
            datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
            + f"_{strategy}"
        )

        # ── Resolve snapshot ────────────────────────────────────────────────
        if snapshot_id is not None:
            self.snapshot_id = snapshot_id
        else:
            self.snapshot_id = _find_latest_snapshot(market)

        logger.info("Using snapshot: %s", self.snapshot_id)

        # Load market data from the snapshot (one-time, ~3-5 s)
        logger.info("Loading market data for %s from snapshot...", market)
        from scripts.strategy_evaluator import load_market_data
        data = load_market_data(market, snapshot_id=self.snapshot_id)

        # Optionally subset to top N tickers by volume for speed
        if top_n is not None and len(data) > top_n:
            from research.quick_screen import _top_n_tickers
            data = _top_n_tickers(data, n=top_n)
            logger.info("Using top %d tickers (fast mode).", top_n)
        self._data = data
        logger.info("Loaded %d tickers.", len(self._data))

        # Load active config
        from utils.config import get_active_config
        self._config = get_active_config(market)

        # ── Evaluation lock ─────────────────────────────────────────────────
        # Hash all engine files + snapshot parquet files.  Any modification
        # during the session will be caught by verify_lock() in experiment().
        from research.lockfile import (
            LOCKED_FILES,
            compute_lock,
            save_lock,
        )
        _snapshot_dir = ATLAS_ROOT / "data" / "snapshots" / self.snapshot_id
        self._eval_lock = compute_lock(LOCKED_FILES, _snapshot_dir)
        save_lock(self._eval_lock, self.session_id)
        logger.info(
            "Evaluation lock computed (%d files) → research/locks/%s.json",
            len(self._eval_lock),
            self.session_id,
        )

        # Load best-known params if they exist
        best = load_best(strategy, market)
        if best and best.get("params"):
            self._best_params = best["params"]
            logger.info(
                "Loaded best params for %s/%s (Sharpe %.4f from %d experiments).",
                strategy, market,
                best.get("metrics", {}).get("sharpe", 0),
                best.get("experiments_run", 0),
            )
        else:
            # Extract current params from active config
            strat_cfg = self._config.get("strategies", {}).get(strategy, {})
            self._best_params = {
                k: v for k, v in strat_cfg.items() if k != "enabled"
            }
            logger.info("No saved best params — using active config defaults.")

    def baseline(self) -> dict:
        """Run backtest with current best params. Sets the bar to beat.

        Always call this first. Returns metrics dict.
        """
        metrics = self._run_backtest(self._best_params)
        self._baseline_metrics = metrics
        self._experiments_run += 1

        # Save as best if we don't have one yet
        best = load_best(self.strategy, self.market)
        if not best or not best.get("metrics"):
            save_best(
                self.strategy, self.market,
                self._best_params, metrics, "baseline",
            )

        # Log to TSV
        _append_result(
            self.strategy, metrics, "", "keep", "baseline",
            market=self.market,
        )

        # Print summary
        _print_metrics("Baseline", self.strategy, metrics, None)
        return metrics

    def experiment(self, params: dict, description: str) -> dict:
        """Run a backtest with param overrides on top of current best.

        Args:
            params:      Dict of param overrides (e.g. {'rsi_period': 7}).
            description: Short text describing what this experiment tries.

        Returns:
            Dict with keys: metrics, recommendation, rationale, delta.
        """
        if self._baseline_metrics is None:
            raise RuntimeError("Call baseline() first to establish the bar.")

        # ── Evaluation lock check ────────────────────────────────────────────
        from research.lockfile import verify_lock, EvaluationLockViolation
        ok, changed = verify_lock(self._eval_lock)
        if not ok:
            raise EvaluationLockViolation(
                f"Evaluation files changed during session: {changed}",
                changed=changed,
            )

        # Merge overrides onto best params
        merged = {**self._best_params, **params}

        # Count net new params vs best
        params_added = len(set(params.keys()) - set(self._best_params.keys()))

        # Run
        metrics = self._run_backtest(merged)
        self._experiments_run += 1
        _increment_run_count(self.strategy)

        # Decide
        verdict = keep_or_discard(self._baseline_metrics, metrics, params_added)

        # Store for keep/discard call
        self._last_experiment = {
            "params": params,
            "merged_params": merged,
            "metrics": metrics,
            "description": description,
            "verdict": verdict,
        }

        # Print summary
        _print_metrics(
            f"Experiment: {description}", self.strategy, metrics,
            verdict,
        )
        return {
            "metrics": metrics,
            "recommendation": verdict["decision"],
            "rationale": verdict["rationale"],
            "delta": {
                "sharpe": verdict["delta_sharpe"],
                "trades": verdict["delta_trades"],
                "max_dd_pct": verdict["delta_dd"],
            },
        }

    def keep(
        self,
        solo_sharpe: Optional[float] = None,
        portfolio_sharpe: Optional[float] = None,
    ) -> str:
        """Keep the last experiment. Updates best params, logs to TSV + journal.

        Args:
            solo_sharpe:      Strategy-standalone Sharpe for this experiment (M2 2026-04-28).
            portfolio_sharpe: Whole-portfolio Sharpe after including this strategy (M2 2026-04-28).

        Returns confirmation string.
        """
        if self._last_experiment is None:
            return "Nothing to keep — run an experiment first."

        exp = self._last_experiment
        params_str = ", ".join(f"{k}={v}" for k, v in exp["params"].items())

        # Update best
        self._best_params = exp["merged_params"]
        self._baseline_metrics = exp["metrics"]
        self._experiments_kept += 1

        save_best(
            self.strategy, self.market,
            self._best_params, exp["metrics"], exp["description"],
            solo_sharpe=solo_sharpe,
            portfolio_sharpe=portfolio_sharpe,
        )

        # Log
        _append_result(
            self.strategy, exp["metrics"],
            params_str, "keep", exp["description"],
            market=self.market,
        )
        _append_journal(
            self.strategy, self.market, exp["metrics"],
            "keep", exp["description"], exp["params"],
        )

        self._last_experiment = None

        msg = (
            f"KEPT: {exp['description']} "
            f"(Sharpe {exp['metrics'].get('sharpe', 0):.4f}, "
            f"trades {exp['metrics'].get('total_trades', 0)})"
        )
        print(f"\n✅ {msg}")
        return msg

    def discard(self) -> str:
        """Discard the last experiment. Logs to TSV, reverts state.

        Returns confirmation string.
        """
        if self._last_experiment is None:
            return "Nothing to discard — run an experiment first."

        exp = self._last_experiment
        params_str = ", ".join(f"{k}={v}" for k, v in exp["params"].items())

        # Log (but don't update best)
        _append_result(
            self.strategy, exp["metrics"],
            params_str, "discard", exp["description"],
            market=self.market,
        )

        self._last_experiment = None

        msg = (
            f"DISCARDED: {exp['description']} "
            f"(Sharpe {exp['metrics'].get('sharpe', 0):.4f}, "
            f"trades {exp['metrics'].get('total_trades', 0)})"
        )
        print(f"\n❌ {msg}")
        return msg

    def history(self, n: int = 20) -> str:
        """Last N results for this strategy."""
        return read_results(self.strategy, n)

    def best(self) -> dict:
        """Current best params and metrics."""
        saved = load_best(self.strategy, self.market)
        if saved:
            return {
                "params": saved.get("params", {}),
                "metrics": saved.get("metrics", {}),
                "experiments_run": saved.get("experiments_run", 0),
                "experiments_kept": saved.get("experiments_kept", 0),
                "updated_at": saved.get("updated_at", ""),
            }
        return {
            "params": self._best_params,
            "metrics": self._baseline_metrics or {},
            "experiments_run": self._experiments_run,
            "experiments_kept": self._experiments_kept,
        }

    def summary(self) -> str:
        """Session summary."""
        elapsed = time.time() - self._session_start
        best = self.best()
        lines = [
            f"\n{'='*60}",
            f"Research Session: {self.strategy} on {self.market}",
            f"{'='*60}",
            f"  Experiments run:  {self._experiments_run}",
            f"  Experiments kept: {self._experiments_kept}",
            f"  Session time:     {elapsed/60:.1f} min",
            f"  Best Sharpe:      {best['metrics'].get('sharpe', 0):.4f}",
            f"  Best trades:      {best['metrics'].get('total_trades', 0)}",
            f"  Best max DD:      {best['metrics'].get('max_drawdown_pct', 0):.2f}%",
            f"  Best params:",
        ]
        for k, v in best.get("params", {}).items():
            lines.append(f"    {k}: {v}")
        lines.append("")
        return "\n".join(lines)

    # ── Internal ─────────────────────────────────────────────────────────

    def _run_backtest(self, params: dict) -> dict:
        """Run a combined portfolio backtest with given params for this strategy.

        Runs the full portfolio (all active strategies from the current config)
        with the target strategy using *params*.  Combined portfolio Sharpe is
        the primary metric; solo Sharpe is recorded as ``solo_sharpe`` for
        diagnostic purposes.

        Args:
            params: Parameter dict for ``self.strategy``.  These are applied on
                    top of the active config values for that strategy.

        Returns:
            Metrics dict with combined portfolio metrics as primary values plus
            a ``solo_sharpe`` key for the isolated strategy Sharpe.
        """
        from scripts.strategy_evaluator import (
            make_config_with_strategy,
            run_backtest,
        )

        t0 = time.time()

        # Combined: all active strategies + this one with candidate params
        combined_cfg = make_config_with_strategy(
            self._config, self.strategy,
            params_override=params, solo=False,
        )
        combined_metrics = run_backtest(combined_cfg, self._data)

        # Solo: only this strategy with candidate params (diagnostics)
        solo_cfg = make_config_with_strategy(
            self._config, self.strategy,
            params_override=params, solo=True,
        )
        solo_metrics = run_backtest(solo_cfg, self._data)

        elapsed = round(time.time() - t0, 1)

        # Primary result is the combined portfolio metrics
        result = dict(combined_metrics)
        result["solo_sharpe"] = solo_metrics.get("sharpe", 0)
        result["runtime_s"] = elapsed
        return result


# ─── Standalone Helpers ──────────────────────────────────────────────────────


def leaderboard(market: str = "sp500") -> str:
    """All strategies ranked by best-known Sharpe.

    Reads from research/best/*.json. Returns formatted table.
    """
    BEST_DIR.mkdir(parents=True, exist_ok=True)
    entries = []
    for path in sorted(BEST_DIR.glob("*.json")):
        try:
            with open(path) as f:
                data = json.load(f)
            m = data.get("metrics", {})
            entries.append({
                "strategy": data.get("strategy", path.stem),
                "sharpe": m.get("sharpe", 0),
                "trades": m.get("total_trades", 0),
                "max_dd": m.get("max_drawdown_pct", 0),
                "pf": m.get("profit_factor", 0),
                "cagr": m.get("cagr_pct", 0),
                "runs": data.get("experiments_run", 0),
                "kept": data.get("experiments_kept", 0),
            })
        except (json.JSONDecodeError, OSError):
            pass

    if not entries:
        return "No results yet. Run some experiments first."

    entries.sort(key=lambda e: e["sharpe"], reverse=True)

    lines = [
        f"\n{'Strategy':<28} {'Sharpe':>7} {'Trades':>7} {'MaxDD%':>7} {'PF':>7} {'CAGR%':>7} {'Runs':>5} {'Kept':>5}",
        "-" * 85,
    ]
    for e in entries:
        lines.append(
            f"{e['strategy']:<28} {e['sharpe']:>7.4f} {e['trades']:>7d} "
            f"{e['max_dd']:>7.2f} {e['pf']:>7.4f} {e['cagr']:>7.2f} "
            f"{e['runs']:>5d} {e['kept']:>5d}"
        )
    lines.append("")
    return "\n".join(lines)


def strategy_status() -> str:
    """Strategy universe: what's available, what's been tested."""
    from scripts.strategy_evaluator import STRATEGY_REGISTRY

    lines = [
        f"\n{'Strategy':<28} {'In Registry':>11} {'Has Best':>9} {'Best Sharpe':>12}",
        "-" * 65,
    ]

    # Check all known strategies
    all_names = set(STRATEGY_REGISTRY.keys())

    # Also check research/strategies/ sandbox
    sandbox_dir = ATLAS_ROOT / "research" / "strategies"
    if sandbox_dir.exists():
        for p in sandbox_dir.glob("*.py"):
            if p.stem != "__init__":
                all_names.add(p.stem)

    for name in sorted(all_names):
        in_registry = "yes" if name in STRATEGY_REGISTRY else "sandbox"
        best = load_best(name)
        has_best = "yes" if best else "no"
        best_sharpe = f"{best['metrics'].get('sharpe', 0):.4f}" if best else "-"
        lines.append(f"{name:<28} {in_registry:>11} {has_best:>9} {best_sharpe:>12}")

    lines.append("")
    return "\n".join(lines)


def quick_check(strategy: str, market: str = "sp500") -> dict:
    """10-second signal check. Is this strategy generating signals at all?

    Returns: {"alive": bool, "signal_count": int, "reason": str}
    """
    try:
        from research.quick_screen import screen_strategy
        from utils.config import get_active_config

        config = get_active_config(market)
        result = screen_strategy(strategy, config, market=market)
        return {
            "alive": result.passed,
            "signal_count": result.signal_count,
            "reason": result.reason,
            "sharpe": result.quick_sharpe,
            "trades": result.quick_trades,
        }
    except Exception as e:
        return {"alive": False, "signal_count": 0, "reason": str(e)}


def combined_test(
    strategy: str,
    params: Optional[dict] = None,
    market: str = "sp500",
) -> dict:
    """Run combined portfolio test: strategy + all active strategies.

    Returns: {baseline: metrics, combined: metrics, delta: metrics}
    """
    from scripts.strategy_evaluator import evaluate_strategy
    result = evaluate_strategy(
        strategy, market, params_override=params, combined=True,
    )
    return {
        "baseline": result.get("baseline", {}),
        "combined": result.get("combined", {}),
        "solo": result.get("solo", {}),
        "delta": result.get("delta", {}),
        "runtime_s": result.get("runtime_s", 0),
    }


# ─── Output Formatting ──────────────────────────────────────────────────────


def _print_metrics(
    label: str,
    strategy: str,
    metrics: dict,
    verdict: Optional[dict],
) -> None:
    """Print clean experiment summary (autoresearch-style)."""
    print(f"\n--- {label} ({strategy}) ---")
    print(f"sharpe:           {metrics.get('sharpe', 0):.4f}")
    print(f"trades:           {metrics.get('total_trades', 0)}")
    print(f"max_dd_pct:       {metrics.get('max_drawdown_pct', 0):.2f}")
    print(f"profit_factor:    {metrics.get('profit_factor', 0):.4f}")
    print(f"cagr_pct:         {metrics.get('cagr_pct', 0):.2f}")
    print(f"win_rate_pct:     {metrics.get('win_rate_pct', 0):.1f}")
    print(f"sortino:          {metrics.get('sortino', 0):.4f}")
    print(f"runtime_s:        {metrics.get('runtime_s', 0):.1f}")

    if verdict:
        rec = verdict["decision"].upper()
        ds = verdict["delta_sharpe"]
        dt = verdict["delta_trades"]
        dd = verdict["delta_dd"]
        print(f"recommendation:   {rec} (Sharpe {ds:+.4f}, trades {dt:+d}, DD {dd:+.1f}%)")
        print(f"rationale:        {verdict['rationale']}")
    print("---")


# ─── CLI ─────────────────────────────────────────────────────────────────────


def main():
    """Minimal CLI for testing."""
    import argparse

    parser = argparse.ArgumentParser(description="Atlas Autoresearch Loop")
    sub = parser.add_subparsers(dest="cmd")

    sub.add_parser("leaderboard", help="Show strategy leaderboard")
    sub.add_parser("status", help="Show strategy universe status")

    check_p = sub.add_parser("check", help="Quick signal check")
    check_p.add_argument("strategy")
    check_p.add_argument("--market", default="sp500")

    baseline_p = sub.add_parser("baseline", help="Run baseline for a strategy")
    baseline_p.add_argument("strategy")
    baseline_p.add_argument("--market", default="sp500")

    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    if args.cmd == "leaderboard":
        print(leaderboard())
    elif args.cmd == "status":
        print(strategy_status())
    elif args.cmd == "check":
        result = quick_check(args.strategy, args.market)
        print(json.dumps(result, indent=2))
    elif args.cmd == "baseline":
        s = ResearchSession(args.strategy, args.market)
        s.baseline()
        print(s.summary())
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
