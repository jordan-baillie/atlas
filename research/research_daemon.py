#!/usr/bin/env python3
"""Atlas Research Daemon — continuous experiment execution engine.

Runs 24/7 as a systemd service. Pulls experiments from queue, runs backtests,
evaluates results, writes to vault, and auto-advances the lifecycle pipeline.

Usage:
    python3 research/research_daemon.py [--workers 2] [--dry-run]

Systemd:
    /etc/systemd/system/atlas-research-daemon.service
"""

import argparse
import json
import logging
import os
import signal
import sys
import time
import traceback
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
from pathlib import Path

# Ensure atlas root is on sys.path
ATLAS_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ATLAS_ROOT))

from research.models import (
    ExperimentStatus,
    claim_experiment,
    get_next_queued,
    read_queue,
    update_queue_entry,
)

logger = logging.getLogger("research_daemon")


# ─── Module-level worker (must be top-level for ProcessPoolExecutor pickling)

def _run_experiment_worker(experiment: dict) -> dict:
    """Worker function for parallel execution. Must be top-level for pickling."""
    # Each worker process needs its own imports
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from scripts.research_runner import run_experiment
    return run_experiment(experiment, agent_id="research-daemon", dry_run=False)


# ─── Constants ───────────────────────────────────────────────────────────────

DATA_DIR = ATLAS_ROOT / "data" / "snapshots"
DATA_STALENESS_HOURS = 48
HEARTBEAT_INTERVAL_S = 60
IDLE_SLEEP_S = 900       # 15 min when queue empty
ERROR_SLEEP_S = 60       # 1 min after error
AGENT_BATCH_SIZE = 10    # signal agent every N experiments


# ─── Daemon ──────────────────────────────────────────────────────────────────

class ResearchDaemon:
    """Continuous experiment execution engine.

    Pulls experiments from queue, runs backtests via research_runner,
    evaluates results, writes vault notes, and auto-advances the lifecycle.
    """

    def __init__(self, workers: int = 2, dry_run: bool = False):
        self.workers = workers
        self.dry_run = dry_run
        self.running = True
        self.experiments_completed = 0
        self.experiments_failed = 0
        self.session_start = datetime.now(timezone.utc)
        self.last_heartbeat = 0.0

        # File paths
        self.heartbeat_path = Path("/tmp/research-daemon-heartbeat.json")
        self.wake_signal_path = Path("/tmp/research-agent-wake.json")
        self.lock_path = Path("/tmp/research-daemon.lock")

        # Lazy-loaded modules (avoid import-time side effects)
        self._runner = None
        self._evaluator = None
        self._vault_writer = None

    # ── Lazy imports ─────────────────────────────────────────────────────

    @property
    def runner(self):
        if self._runner is None:
            from scripts.research_runner import run_experiment
            self._runner = run_experiment
        return self._runner

    @property
    def evaluator(self):
        if self._evaluator is None:
            from research.evaluator import ExperimentEvaluator
            self._evaluator = ExperimentEvaluator()
        return self._evaluator

    @property
    def vault_writer(self):
        if self._vault_writer is None:
            import research.vault_writer as vw
            self._vault_writer = vw
        return self._vault_writer

    # ── Main loop ────────────────────────────────────────────────────────

    def run(self):
        """Main daemon loop — runs until SIGTERM/SIGINT."""
        logger.info("Research daemon starting (workers=%d, dry_run=%s)", self.workers, self.dry_run)

        self._acquire_lock()
        self._setup_signal_handlers()

        try:
            while self.running:
                self._maybe_write_heartbeat()

                # Check data freshness
                if not self._check_data_freshness():
                    logger.info("Market data stale (>%dh), waiting...", DATA_STALENESS_HOURS)
                    self._write_heartbeat("waiting_data")
                    self._sleep(300)
                    continue

                # Collect batch of independent experiments
                batch = self._collect_batch()
                if not batch:
                    self._write_heartbeat("idle")
                    queue_depth = self._queue_depth()
                    if queue_depth == 0:
                        self._signal_agent("queue_empty", "No experiments in queue")
                        # Auto-refill from discovery engine
                        try:
                            from research.discovery import queue_discovery_batch
                            refilled = queue_discovery_batch(max_count=5)
                            if refilled > 0:
                                logger.info("Auto-refilled queue with %d discovery experiments", refilled)
                                continue  # Skip sleep, process new experiments immediately
                        except Exception as e:
                            logger.warning("Discovery auto-refill failed: %s", e)
                    self._sleep(IDLE_SLEEP_S)
                    continue

                # Execute batch (parallel if workers > 1)
                batch_ids = [e.get("id", "?") for e in batch]
                logger.info("Starting batch of %d experiments: %s", len(batch), batch_ids)
                self._write_heartbeat("running", current_experiment=", ".join(batch_ids))

                results = self._execute_batch(batch)

                # Post-process each result
                for experiment, result in results:
                    exp_id = experiment.get("id", "unknown")
                    try:
                        self._post_process(experiment, result)
                    except Exception as e:
                        logger.error("Post-processing failed for %s: %s", exp_id, e)

                    self.experiments_completed += 1
                    logger.info(
                        "Experiment %s done (%d total, %d failed)",
                        exp_id, self.experiments_completed, self.experiments_failed,
                    )

                # Signal agent on batch completion
                if self.experiments_completed % AGENT_BATCH_SIZE == 0:
                    self._signal_agent(
                        "batch_complete",
                        f"Completed {self.experiments_completed} experiments "
                        f"({self.experiments_failed} failed)",
                    )

        except KeyboardInterrupt:
            logger.info("Keyboard interrupt received")
        finally:
            logger.info(
                "Daemon shutting down — completed=%d, failed=%d, uptime=%s",
                self.experiments_completed,
                self.experiments_failed,
                str(datetime.now(timezone.utc) - self.session_start),
            )
            self._write_heartbeat("stopped")
            self._release_lock()

    # ── Experiment execution ─────────────────────────────────────────────

    def _get_next_experiment(self):
        """Get the next queued experiment, claim it, and return it."""
        try:
            entry = get_next_queued(market=None)  # any market
            if entry is None:
                return None
            claimed = claim_experiment(entry["id"], agent_id="research-daemon")
            return claimed
        except Exception as e:
            logger.error("Error getting next experiment: %s", e)
            return None

    def _collect_batch(self) -> list:
        """Collect up to self.workers independent experiments from queue."""
        batch = []
        for _ in range(self.workers):
            exp = self._get_next_experiment()
            if exp is None:
                break
            batch.append(exp)
        return batch

    def _execute_batch(self, experiments: list) -> list:
        """Execute a batch of experiments, parallel if workers > 1.

        Returns list of (experiment, result) tuples.
        """
        if len(experiments) <= 1 or self.workers <= 1 or self.dry_run:
            # Sequential execution
            results = []
            for exp in experiments:
                try:
                    result = self._execute_experiment(exp)
                except Exception as e:
                    self._handle_failure(exp, e)
                    result = {"verdict": "fail", "error": str(e)}
                results.append((exp, result))
            return results

        # Parallel execution
        logger.info("Executing %d experiments in parallel (workers=%d)", len(experiments), self.workers)
        results = []
        with ProcessPoolExecutor(max_workers=self.workers) as pool:
            future_to_exp = {}
            for exp in experiments:
                future = pool.submit(_run_experiment_worker, exp)
                future_to_exp[future] = exp

            for future in as_completed(future_to_exp):
                exp = future_to_exp[future]
                exp_id = exp.get("id", "unknown")
                try:
                    result = future.result(timeout=3600)  # 1hr max per experiment
                    results.append((exp, result))
                except Exception as e:
                    logger.error("Parallel experiment %s failed: %s", exp_id, e)
                    results.append((exp, {"verdict": "fail", "error": str(e)}))

        return results

    def _execute_experiment(self, experiment: dict) -> dict:
        """Run an experiment via research_runner.run_experiment()."""
        if self.dry_run:
            logger.info("DRY RUN: would execute %s", experiment.get("id"))
            return {
                "verdict": "dry_run",
                "dry_run": True,
                "experiment_id": experiment.get("id"),
            }

        return self.runner(experiment, agent_id="research-daemon", dry_run=False)

    def _post_process(self, experiment: dict, result: dict):
        """Evaluate result, write vault notes, advance lifecycle."""
        exp_id = experiment.get("id", "unknown")
        verdict = result.get("verdict", "unknown")
        strategy_name = experiment.get("strategy_name", "unknown")
        market = experiment.get("market", "sp500")

        # run_experiment() already handles: evaluation, queue status update,
        # journal append, and envelope save. We layer on: vault notes + lifecycle.

        # 1. Write vault notes
        try:
            self._write_vault_notes(experiment, result)
        except Exception as e:
            logger.error("Failed to write vault notes for %s: %s", exp_id, e)

        # 2. Write parameter insights
        try:
            self._write_parameter_insights(experiment, result)
        except Exception as e:
            logger.warning("Failed to write parameter insights for %s: %s", exp_id, e)

        # 3. Check hypotheses against this result
        try:
            self._check_hypotheses(experiment, result)
        except Exception as e:
            logger.warning("Failed to check hypotheses for %s: %s", exp_id, e)

        # 4. Detect patterns periodically (every 10 experiments)
        if self.experiments_completed > 0 and self.experiments_completed % 10 == 0:
            try:
                self._detect_and_flag_patterns()
            except Exception as e:
                logger.warning("Pattern detection failed: %s", e)

        # 5. Auto-advance or defer lifecycle
        try:
            self._advance_lifecycle(experiment, result)
        except Exception as e:
            logger.error("Failed to advance lifecycle for %s: %s", exp_id, e)

        # 6. Track failures
        if verdict == "fail":
            self.experiments_failed += 1

        # 7. Signal agent on promotion candidate
        if verdict == "pass":
            stage = self._infer_stage(experiment)
            if stage == "oos":
                self._signal_agent(
                    "promotion_candidate",
                    f"Experiment {exp_id} ({strategy_name}) passed OOS validation — "
                    f"ready for promotion review",
                )

    def _write_vault_notes(self, experiment: dict, result: dict):
        """Write experiment note and update strategy card in vault."""
        exp_id = experiment.get("id", "unknown")
        vw = self.vault_writer

        # Build journal-like entry from result for vault_writer
        journal_entry = {
            "experiment_id": exp_id,
            "strategy": experiment.get("strategy_name"),
            "verdict": result.get("verdict", "unknown"),
            "key_metrics": result.get("outputs", {}).get("metrics", {}),
            "hypothesis": experiment.get("hypothesis", ""),
            "learnings": result.get("learnings", []),
            "market": experiment.get("market", "sp500"),
            "category": experiment.get("category", ""),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

        # Flatten metrics — run_experiment nests them differently per experiment type
        outputs = result.get("outputs", {})
        if not journal_entry["key_metrics"] and isinstance(outputs, dict):
            # Try common locations
            for key in ("metrics", "solo_metrics", "combined_metrics", "best_metrics"):
                if key in outputs and isinstance(outputs[key], dict):
                    journal_entry["key_metrics"] = outputs[key]
                    break

        vw.write_experiment_note(exp_id, journal_entry, envelope=result)

        strategy_id = experiment.get("strategy_name")
        if strategy_id:
            try:
                vw.update_strategy_card(strategy_id)
            except Exception as e:
                logger.warning("Failed to update strategy card for %s: %s", strategy_id, e)

        # Update daily log
        try:
            vw.write_daily_log()
        except Exception as e:
            logger.warning("Failed to update daily log: %s", e)

    def _check_hypotheses(self, experiment: dict, result: dict):
        """Check if experiment results confirm/reject any hypotheses."""
        from research.hypothesis_tracker import check_hypotheses_against_result
        updates = check_hypotheses_against_result(result)
        for update in updates:
            logger.info("Hypothesis update: %s → %s", update.get("id"), update.get("new_status"))

    def _detect_and_flag_patterns(self):
        """Run mechanical pattern detection and flag for agent review."""
        from research.hypothesis_tracker import detect_patterns
        patterns = detect_patterns()
        if patterns:
            pattern_summary = "; ".join(p["description"][:80] for p in patterns[:3])
            self._signal_agent(
                "patterns_detected",
                f"Found {len(patterns)} patterns: {pattern_summary}",
            )
            logger.info("Detected %d patterns — agent signaled", len(patterns))

    def _write_parameter_insights(self, experiment: dict, result: dict):
        """Extract parameter insights from experiment results and write to vault."""
        params_override = experiment.get("params_override") or {}
        param_grid = experiment.get("param_grid") or {}
        strategy_name = experiment.get("strategy_name", "unknown")
        exp_id = experiment.get("id", "unknown")

        # Only write insights for param-testing experiments
        if not params_override and not param_grid:
            return

        outputs = result.get("outputs", {})
        metrics = outputs.get("metrics", {}) if isinstance(outputs, dict) else {}
        sharpe = metrics.get("sharpe", 0)

        vw = self.vault_writer

        # For each overridden parameter, record what was tested and the outcome
        for param_name, param_value in params_override.items():
            findings = {
                "optimal_value": param_value,
                "tested_range": [param_value],
                "sensitivity": "unknown",
                "sharpe_result": sharpe,
                "related_experiments": [exp_id],
            }
            vw.write_parameter_insight(strategy_name, param_name, findings)

        # For param_grid experiments with best_params in output
        best_params = outputs.get("best_params", {}) if isinstance(outputs, dict) else {}
        if best_params:
            for param_name, param_value in best_params.items():
                tested_range = param_grid.get(param_name, [param_value])
                findings = {
                    "optimal_value": param_value,
                    "tested_range": tested_range if isinstance(tested_range, list) else [tested_range],
                    "sensitivity": "unknown",
                    "sharpe_result": sharpe,
                    "related_experiments": [exp_id],
                }
                vw.write_parameter_insight(strategy_name, param_name, findings)

    def _advance_lifecycle(self, experiment: dict, result: dict):
        """Auto-advance on pass, auto-defer on fail."""
        verdict = result.get("verdict", "unknown")
        exp_id = experiment.get("id", "unknown")
        strategy_name = experiment.get("strategy_name", "unknown")
        market = experiment.get("market", "sp500")
        stage = self._infer_stage(experiment)

        if verdict == "pass" and stage:
            optimized_params = result.get("outputs", {}).get("best_params")
            advanced = self.evaluator.auto_advance(
                experiment_id=exp_id,
                verdict=verdict,
                current_stage=stage,
                strategy_name=strategy_name,
                market=market,
                optimized_params=optimized_params,
            )
            if advanced:
                if isinstance(advanced, dict) and advanced.get("action") == "promote":
                    logger.info("Experiment %s ready for promotion (human approval needed)", exp_id)
                else:
                    next_id = advanced.get("id", "?") if isinstance(advanced, dict) else "?"
                    logger.info("Auto-advanced %s → next experiment: %s", exp_id, next_id)

        elif verdict == "fail" and strategy_name:
            self.evaluator.auto_defer(exp_id, strategy_name)
            logger.info("Auto-deferred downstream experiments for %s", strategy_name)

    def _infer_stage(self, experiment: dict) -> str | None:
        """Infer lifecycle stage from experiment method/category."""
        method = experiment.get("method", "")
        category = experiment.get("category", "")

        method_lower = method.lower() if isinstance(method, str) else ""
        category_lower = category.lower() if isinstance(category, str) else ""

        if "oos" in method_lower or "oos" in category_lower:
            return "oos"
        if "combined" in method_lower or "combined" in category_lower:
            return "combined"
        if "optim" in method_lower or "optim" in category_lower or "sweep" in method_lower:
            return "optimize"
        if "single" in method_lower or "solo" in category_lower or "dormant" in category_lower:
            return "solo"
        return None

    def _handle_failure(self, experiment: dict, error: Exception):
        """Handle an experiment execution failure."""
        exp_id = experiment.get("id", "unknown")
        logger.error("Experiment %s failed: %s\n%s", exp_id, error, traceback.format_exc())
        self.experiments_failed += 1

        try:
            update_queue_entry(exp_id, {"status": ExperimentStatus.FAILED})
        except Exception:
            pass

        self._sleep(ERROR_SLEEP_S)

    # ── Data freshness ───────────────────────────────────────────────────

    def _check_data_freshness(self) -> bool:
        """Check if market data is fresh enough to run experiments."""
        if not DATA_DIR.exists():
            logger.warning("Data directory %s does not exist", DATA_DIR)
            return False

        # Find most recent snapshot directory
        snapshots = sorted(DATA_DIR.iterdir()) if DATA_DIR.is_dir() else []
        if not snapshots:
            logger.warning("No snapshot directories found in %s", DATA_DIR)
            return False

        latest = snapshots[-1]
        mtime = datetime.fromtimestamp(latest.stat().st_mtime, tz=timezone.utc)
        age_hours = (datetime.now(timezone.utc) - mtime).total_seconds() / 3600

        if age_hours > DATA_STALENESS_HOURS:
            logger.warning(
                "Latest data snapshot %s is %.1fh old (threshold: %dh)",
                latest.name, age_hours, DATA_STALENESS_HOURS,
            )
            return False

        return True

    # ── Agent signaling ──────────────────────────────────────────────────

    def _signal_agent(self, reason: str, context: str):
        """Write wake signal for the coordinator agent."""
        wake_data = {
            "reason": reason,
            "context": context,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "experiments_completed": self.experiments_completed,
            "experiments_failed": self.experiments_failed,
            "queue_depth": self._queue_depth(),
        }
        try:
            self.wake_signal_path.write_text(json.dumps(wake_data, indent=2))
            logger.info("Agent wake signal: %s — %s", reason, context)
        except Exception as e:
            logger.error("Failed to write agent wake signal: %s", e)

    # ── Heartbeat ────────────────────────────────────────────────────────

    def _maybe_write_heartbeat(self):
        """Write heartbeat if enough time has passed."""
        now = time.monotonic()
        if now - self.last_heartbeat >= HEARTBEAT_INTERVAL_S:
            self._write_heartbeat("running")

    def _write_heartbeat(self, status: str = "running", current_experiment: str = None):
        """Write daemon health status to heartbeat file."""
        self.last_heartbeat = time.monotonic()
        heartbeat = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "status": status,
            "pid": os.getpid(),
            "experiments_completed": self.experiments_completed,
            "experiments_failed": self.experiments_failed,
            "queue_depth": self._queue_depth(),
            "uptime_s": (datetime.now(timezone.utc) - self.session_start).total_seconds(),
            "current_experiment": current_experiment,
        }
        try:
            self.heartbeat_path.write_text(json.dumps(heartbeat, indent=2))
        except Exception:
            pass  # heartbeat is best-effort

    # ── Utilities ────────────────────────────────────────────────────────

    def _queue_depth(self) -> int:
        """Count queued experiments."""
        try:
            queue = read_queue()
            return sum(1 for e in queue if e.get("status") == ExperimentStatus.QUEUED)
        except Exception:
            return -1

    def _sleep(self, seconds: int):
        """Interruptible sleep — checks self.running every 5 seconds."""
        elapsed = 0
        while elapsed < seconds and self.running:
            time.sleep(min(5, seconds - elapsed))
            elapsed += 5

    def _acquire_lock(self):
        """Acquire daemon lock file. Exit if another instance is running."""
        if self.lock_path.exists():
            try:
                old_pid = int(self.lock_path.read_text().strip())
                # Check if process is still alive
                os.kill(old_pid, 0)
                logger.error("Another daemon is running (PID %d). Exiting.", old_pid)
                sys.exit(1)
            except (ValueError, ProcessLookupError, PermissionError):
                logger.warning("Removing stale lock file (PID dead)")
                self.lock_path.unlink(missing_ok=True)

        self.lock_path.write_text(str(os.getpid()))
        logger.info("Lock acquired (PID %d)", os.getpid())

    def _release_lock(self):
        """Release daemon lock file."""
        try:
            self.lock_path.unlink(missing_ok=True)
            logger.info("Lock released")
        except Exception:
            pass

    def _setup_signal_handlers(self):
        """Set up SIGTERM/SIGINT for clean shutdown."""
        def _shutdown(signum, frame):
            signame = signal.Signals(signum).name
            logger.info("Received %s — initiating clean shutdown", signame)
            self.running = False

        signal.signal(signal.SIGTERM, _shutdown)
        signal.signal(signal.SIGINT, _shutdown)


# ── Logging setup ────────────────────────────────────────────────────────────

def setup_logging(log_file: str = None):
    """Configure logging for daemon operation."""
    handlers = [logging.StreamHandler(sys.stdout)]
    if log_file:
        handlers.append(logging.FileHandler(log_file))

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=handlers,
    )

    # Reduce noise from libraries
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("matplotlib").setLevel(logging.WARNING)


# ── CLI ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Atlas Research Daemon — continuous experiment execution engine",
    )
    parser.add_argument(
        "--workers", type=int, default=2,
        help="Number of parallel backtest workers (default: 2). "
             "NOTE: v1 uses sequential execution; parallel planned for Phase 2.",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Don't actually run experiments, just log what would happen",
    )
    parser.add_argument(
        "--log-file", type=str, default=None,
        help="Log file path (default: stdout only)",
    )
    args = parser.parse_args()

    setup_logging(args.log_file)

    daemon = ResearchDaemon(workers=args.workers, dry_run=args.dry_run)
    daemon.run()

    # TODO: Phase 2 — parallel execution with ProcessPoolExecutor
    # The daemon.workers parameter is accepted but not yet used for parallelism.
    # Phase 2 will add a worker pool that pulls multiple experiments simultaneously.


if __name__ == "__main__":
    main()
