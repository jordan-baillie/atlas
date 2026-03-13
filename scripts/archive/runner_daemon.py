#!/usr/bin/env python3
"""Atlas Research Runner Daemon — Continuous Queue Experiment Executor

A long-running systemd service that continuously pulls experiments from
research/queue.json (by priority), executes them, evaluates results with DSR,
auto-advances lifecycle stages, and writes heartbeat telemetry.

Architecture:
    queue.json (118 queued exps)
        └─→ [RUNNER DAEMON] picks highest-priority, status=queued
            ├─ claim_experiment()       (prevents double-pickup)
            ├─ dispatch_experiment()    (routes to correct handler)
            ├─ ExperimentEvaluator.evaluate()  (DSR-aware verdict)
            ├─ evaluator.auto_advance() (queues next lifecycle stage)
            └─ heartbeat /tmp/runner-daemon-heartbeat.json  (every 30s)

Guards:
    - Respects grinder lock (/tmp/atlas-research-cron.lock) — yields to sweep.py
    - Sleeps 5 minutes when queue is empty
    - Resets stale claims on startup (crash recovery)
    - 4-hour per-experiment timeout via research_runner.py

Usage (manual):
    python3 research/runner_daemon.py

Systemd (automatic):
    systemctl start atlas-research-runner
    systemctl status atlas-research-runner
    journalctl -u atlas-research-runner -f
"""

import json
import logging
import os
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from utils.logging_config import setup_logging

logger = setup_logging("runner_daemon")

# ── Constants ────────────────────────────────────────────────────────────────

HEARTBEAT_PATH = Path("/tmp/runner-daemon-heartbeat.json")
GRINDER_LOCK   = Path("/tmp/atlas-research-cron.lock")
GRINDER_YIELD_SLEEP  = 60      # seconds to sleep while grinder holds lock
EMPTY_QUEUE_SLEEP    = 5 * 60  # 5 minutes — sleep when queue is exhausted
HEARTBEAT_INTERVAL   = 30      # seconds between heartbeat refreshes
BETWEEN_EXP_SLEEP    = 5       # seconds between experiments
STALE_CLAIM_TIMEOUT  = 2.0     # hours before stale claims are reset

AGENT_ID = "atlas-research-runner"

# ── Heartbeat ────────────────────────────────────────────────────────────────

_hb_state: dict = {
    "status": "starting",
    "phase": "starting",
    "experiment_id": None,
    "experiment_title": None,
    "strategy": None,
    "queue_depth": 0,
    "experiments_completed": 0,
    "experiments_passed": 0,
    "experiments_failed": 0,
    "current_stage": None,
    "activity": "loading",
    "detail": "Runner daemon starting...",
    "started": datetime.now(timezone.utc).isoformat(),
}


def _write_heartbeat(**overrides) -> None:
    """Write current daemon state to the heartbeat file (atomic)."""
    _hb_state.update(overrides)
    _hb_state["timestamp"] = datetime.now(timezone.utc).astimezone().isoformat()
    tmp = HEARTBEAT_PATH.with_suffix(".tmp")
    try:
        with open(tmp, "w") as f:
            json.dump(_hb_state, f, indent=2, default=str)
        os.replace(tmp, HEARTBEAT_PATH)
    except Exception as e:
        logger.warning("Heartbeat write failed: %s", e)


# ── Shutdown handling ────────────────────────────────────────────────────────

_shutdown = False


def _handle_signal(signum, frame):
    global _shutdown
    logger.info("Signal %d received — shutting down gracefully", signum)
    _shutdown = True
    _write_heartbeat(status="stopping", phase="shutdown",
                     detail="Signal received, stopping after current experiment")


signal.signal(signal.SIGTERM, _handle_signal)
signal.signal(signal.SIGINT,  _handle_signal)


# ── Grinder lock check ───────────────────────────────────────────────────────

def _grinder_active() -> bool:
    """Return True if sweep.py is currently holding its exclusive lock."""
    if not GRINDER_LOCK.exists():
        return False
    try:
        import fcntl
        with open(GRINDER_LOCK, "r") as f:
            # Non-blocking attempt to acquire a shared lock.
            # If grinder holds the exclusive lock, this will raise BlockingIOError.
            fcntl.flock(f, fcntl.LOCK_SH | fcntl.LOCK_NB)
            fcntl.flock(f, fcntl.LOCK_UN)
            return False  # Got the lock → grinder is not active
    except OSError:
        return True  # Grinder holds lock


# ── Queue helpers ────────────────────────────────────────────────────────────

def _get_queue_depth() -> int:
    """Count pending (queued) experiments in queue.json."""
    try:
        from research.models import read_queue, ExperimentStatus
        queue = read_queue()
        return sum(1 for e in queue if e.get("status") == ExperimentStatus.QUEUED)
    except Exception as e:
        logger.warning("Could not read queue depth: %s", e)
        return 0


def _get_experiment_stage(entry: dict) -> str:
    """Infer lifecycle stage from experiment method."""
    method = entry.get("method", "")
    tags   = entry.get("tags", [])
    # Check explicit stage tag first
    for tag in tags:
        if tag.startswith("stage/"):
            return tag.split("/", 1)[1]
    # Infer from method
    stage_map = {
        "single_strategy_test":   "solo",
        "full_optimization":      "optimize",
        "reoptimization":         "optimize",
        "combined_portfolio_test": "combined",
        "oos_validation":         "oos",
        "param_sweep":            "solo",
        "filter_test":            "solo",
    }
    return stage_map.get(method, "solo")


# ── Telegram notifications ────────────────────────────────────────────────────

def _notify_promotion_candidate(entry: dict, advance_result: dict) -> None:
    """Send Telegram alert when a strategy reaches OOS stage (promotion candidate)."""
    try:
        from utils.telegram import send_message
        strategy = entry.get("strategy_name", "unknown")
        exp_id   = entry.get("id", "?")
        dsr_warn = advance_result.get("dsr_warning", "")
        msg = (
            f"🎯 <b>Promotion Candidate</b>\n"
            f"Strategy: <code>{strategy}</code>\n"
            f"Source: <code>{exp_id}</code>\n"
            f"Passed solo → optimize → combined → OOS validation.\n"
            f"Manual review required.\n"
        )
        if dsr_warn:
            msg += f"\n⚠️ {dsr_warn}"
        send_message(msg)
        logger.info("Telegram: promotion candidate alert sent for %s", strategy)
    except Exception as e:
        logger.debug("Telegram notification failed (non-critical): %s", e)


# ── Core experiment loop ─────────────────────────────────────────────────────

def run_one_experiment() -> Optional[dict]:
    """Pick, run, evaluate, and auto-advance one experiment from the queue.

    Returns the result dict, or None if no experiment was available.
    """
    from research.models import (
        get_next_queued, claim_experiment, update_queue_entry,
        ExperimentStatus, read_queue,
    )
    from scripts.research_runner import dispatch_experiment, run_experiment
    from research.evaluator import ExperimentEvaluator

    evaluator = ExperimentEvaluator()

    # 1. Peek at queue depth for heartbeat
    queue_depth = _get_queue_depth()

    # 2. Get highest-priority pending experiment
    entry = get_next_queued()
    if entry is None:
        _write_heartbeat(
            status="waiting",
            phase="sleeping",
            experiment_id=None,
            experiment_title=None,
            queue_depth=queue_depth,
            activity="waiting",
            detail=f"Queue empty — sleeping {EMPTY_QUEUE_SLEEP // 60}m",
        )
        return None

    exp_id    = entry["id"]
    exp_title = entry.get("title", exp_id)
    strategy  = entry.get("strategy_name") or entry.get("category", "")
    stage     = _get_experiment_stage(entry)

    # 3. Claim the experiment
    claimed = claim_experiment(exp_id, AGENT_ID)
    if claimed is None:
        logger.warning("Could not claim %s — already taken, skipping", exp_id)
        return None
    entry = claimed

    logger.info("▶ Running: [%s] %s (stage=%s)", exp_id, exp_title, stage)

    _write_heartbeat(
        status="running",
        phase="executing",
        experiment_id=exp_id,
        experiment_title=exp_title,
        strategy=strategy,
        queue_depth=queue_depth,
        current_stage=stage,
        activity="testing",
        detail=f"Running backtest for {strategy or exp_id}",
    )

    # 4. Run experiment via research_runner (handles dispatch + timeout + journal)
    try:
        result = run_experiment(entry, AGENT_ID, dry_run=False)
    except Exception as e:
        logger.error("Experiment %s crashed: %s", exp_id, e, exc_info=True)
        update_queue_entry(exp_id, {"status": ExperimentStatus.FAILED})
        _hb_state["experiments_failed"] = _hb_state.get("experiments_failed", 0) + 1
        _hb_state["experiments_completed"] = _hb_state.get("experiments_completed", 0) + 1
        _write_heartbeat(
            status="running",
            phase="error",
            activity="error",
            detail=f"Experiment failed: {e}",
        )
        return None

    # 5. Supplementary evaluation with ExperimentEvaluator (adds DSR)
    verdict = result.get("verdict", "fail")
    outputs = result.get("outputs") or {}

    # Attempt DSR evaluation on top-level metrics extracted from outputs
    try:
        _write_heartbeat(
            status="running",
            phase="evaluating",
            activity="evaluating",
            detail=f"DSR evaluation for {exp_id}",
        )
        # Extract metrics for DSR (try common locations)
        metrics_for_eval = {}
        for loc in ("solo", "combined", "metrics", "best_metrics", "optimized"):
            sub = outputs.get(loc, {})
            if isinstance(sub, dict) and "sharpe" in sub:
                metrics_for_eval = sub
                break
        if not metrics_for_eval:
            metrics_for_eval = {k: v for k, v in outputs.items()
                                if isinstance(v, (int, float))}

        if metrics_for_eval:
            eval_result = evaluator.evaluate(
                experiment_id=exp_id,
                metrics=metrics_for_eval,
                acceptance_criteria=entry.get("acceptance_criteria"),
                stage=stage,
            )
            # Attach DSR info to result metadata
            if "dsr" in eval_result:
                result.setdefault("metadata", {})["dsr"] = eval_result["dsr"]
                logger.info(
                    "DSR [%s]: p=%.4f, significant=%s",
                    exp_id,
                    eval_result["dsr"].get("dsr_pvalue", 1.0),
                    eval_result["dsr"].get("is_significant", False),
                )
    except Exception as e:
        logger.debug("DSR evaluation error (non-critical): %s", e)

    # 6. Update counters
    _hb_state["experiments_completed"] = _hb_state.get("experiments_completed", 0) + 1
    if verdict == "pass":
        _hb_state["experiments_passed"] = _hb_state.get("experiments_passed", 0) + 1
    else:
        _hb_state["experiments_failed"] = _hb_state.get("experiments_failed", 0) + 1

    logger.info("✓ %s verdict=%s", exp_id, verdict)

    # 7. Auto-advance lifecycle if passed
    if verdict == "pass":
        try:
            _write_heartbeat(
                status="running",
                phase="advancing",
                activity="writing",
                detail=f"Auto-advancing lifecycle from stage={stage}",
            )
            # Extract optimized params for passing to next stage
            optimized_params = None
            if stage == "optimize":
                optimized_params = (
                    outputs.get("best_params")
                    or outputs.get("optimized", {})
                )
                if not isinstance(optimized_params, dict):
                    optimized_params = None

            advance_result = evaluator.auto_advance(
                experiment_id=exp_id,
                verdict=verdict,
                current_stage=stage,
                strategy_name=entry.get("strategy_name", ""),
                market=entry.get("market", "sp500"),
                optimized_params=optimized_params,
            )

            if advance_result:
                action = advance_result.get("action")
                if action == "promote":
                    # Strategy completed full pipeline → notify
                    logger.info(
                        "🎯 Promotion candidate: %s", entry.get("strategy_name")
                    )
                    _notify_promotion_candidate(entry, advance_result)
                elif advance_result.get("id"):
                    logger.info(
                        "⏭ Auto-advanced: %s → %s",
                        exp_id, advance_result["id"]
                    )
        except Exception as e:
            logger.warning("Auto-advance failed (non-critical): %s", e)

    _write_heartbeat(
        status="running",
        phase="idle",
        experiment_id=exp_id,
        experiment_title=exp_title,
        strategy=strategy,
        queue_depth=max(0, queue_depth - 1),
        current_stage=stage,
        activity="writing",
        detail=f"Completed {exp_id}: verdict={verdict}",
    )

    return result


# ── Main daemon loop ─────────────────────────────────────────────────────────

def main() -> int:
    """Run the runner daemon indefinitely."""
    global _shutdown

    logger.info("=== Atlas Research Runner Daemon starting ===")
    logger.info("Heartbeat: %s", HEARTBEAT_PATH)
    logger.info("Grinder lock: %s", GRINDER_LOCK)

    # Startup: reset stale claims from previous crashes
    try:
        from research.models import cleanup_stale_claims
        reset_count = cleanup_stale_claims(timeout_h=STALE_CLAIM_TIMEOUT)
        if reset_count:
            logger.info("Reset %d stale claims from previous run", reset_count)
    except Exception as e:
        logger.warning("Stale claim cleanup failed: %s", e)

    _write_heartbeat(
        status="running",
        phase="idle",
        detail="Daemon ready, polling queue...",
    )

    last_heartbeat = time.monotonic()
    consecutive_errors = 0
    MAX_CONSECUTIVE_ERRORS = 10

    while not _shutdown:
        # Periodic heartbeat refresh (even when idle)
        now = time.monotonic()
        if now - last_heartbeat > HEARTBEAT_INTERVAL:
            _write_heartbeat(queue_depth=_get_queue_depth())
            last_heartbeat = now

        # Yield to grinder if it holds the lock
        if _grinder_active():
            logger.debug("Grinder active — yielding for %ds", GRINDER_YIELD_SLEEP)
            _write_heartbeat(
                status="waiting",
                phase="yielding",
                activity="waiting",
                detail="Grinder active — yielding",
            )
            for _ in range(GRINDER_YIELD_SLEEP):
                if _shutdown:
                    break
                time.sleep(1)
            continue

        # Run one experiment
        try:
            result = run_one_experiment()
            consecutive_errors = 0

            if result is None:
                # Queue empty — sleep before rechecking
                logger.info("Queue empty — sleeping %dm", EMPTY_QUEUE_SLEEP // 60)
                _write_heartbeat(
                    status="waiting",
                    phase="sleeping",
                    activity="waiting",
                    detail=f"Queue empty — sleeping {EMPTY_QUEUE_SLEEP // 60}m",
                )
                for _ in range(EMPTY_QUEUE_SLEEP):
                    if _shutdown:
                        break
                    time.sleep(1)
            else:
                last_heartbeat = time.monotonic()
                # Brief pause between experiments
                for _ in range(BETWEEN_EXP_SLEEP):
                    if _shutdown:
                        break
                    time.sleep(1)

        except Exception as e:
            consecutive_errors += 1
            logger.error(
                "Daemon loop error (%d/%d): %s",
                consecutive_errors, MAX_CONSECUTIVE_ERRORS, e,
                exc_info=True,
            )
            _write_heartbeat(
                status="error",
                phase="error",
                activity="error",
                detail=f"Loop error: {e}",
            )
            if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                logger.critical(
                    "Too many consecutive errors (%d) — exiting for systemd restart",
                    consecutive_errors,
                )
                break
            # Back-off sleep before retry
            backoff = min(60 * consecutive_errors, 600)
            logger.info("Backing off %ds before retry", backoff)
            for _ in range(backoff):
                if _shutdown:
                    break
                time.sleep(1)

    logger.info("=== Atlas Research Runner Daemon stopped ===")
    _write_heartbeat(
        status="stopped",
        phase="stopped",
        detail="Daemon stopped",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
