#!/usr/bin/env python3
"""Atlas Autoresearch Orchestrator — pipelined sweep + agent loop.

Supports partitioned mode: two instances split the strategy list and
coordinate via a file lock so only one sweep (CPU-bound) runs at a time.

Pipeline per instance:
  sweep(A) → [agent(A) + sweep(B)] → [agent(B) + sweep(C)] → ...

Usage:
    python3 scripts/autoresearch.py                  # solo (all strategies)
    python3 scripts/autoresearch.py --partition 0    # even strategies
    python3 scripts/autoresearch.py --partition 1    # odd strategies
"""

import argparse
import fcntl
import json
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

# ─── Project Setup ───────────────────────────────────────────────────────────

PROJECT = Path(__file__).resolve().parent.parent
os.chdir(PROJECT)
sys.path.insert(0, str(PROJECT))

# ─── CLI ─────────────────────────────────────────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser(description="Atlas Autoresearch Orchestrator")
    p.add_argument("--partition", type=int, default=None, choices=[0, 1],
                   help="Strategy partition: 0=even indices, 1=odd indices. "
                        "Omit for solo mode (all strategies).")
    return p.parse_args()

# ─── Configuration ───────────────────────────────────────────────────────────

ALL_STRATEGIES = [
    "mean_reversion",
    "trend_following",
    "opening_gap",
    "connors_rsi2",
    "momentum_breakout",
    "short_term_mr",
    "bb_squeeze",
]

SWEEP_TOP_N = 50
SWEEP_WORKERS = max(1, os.cpu_count() - 2)
SWEEP_MAX_FAILS = 8       # bumped from 5 — parallel batches explore more params
SWEEP_TIMEOUT = 3600       # 1 hour max per sweep
AGENT_TIMEOUT = 1800       # 30 min budget per agent
SKILL_DIR = str(PROJECT / "pi-package" / "atlas-ops" / "skills" / "atlas-research-loop")

# Sweep lock — only one instance sweeps at a time (CPU-saturating)
SWEEP_LOCK_PATH = Path("/tmp/autoresearch-sweep.lock")

LOG_MAX_BYTES = 50 * 1024 * 1024  # 50 MB

# These get set in main() based on --partition
PARTITION: int | None = None       # None = solo, 0 or 1 = partitioned
PARTITION_TAG: str = ""            # "" or "-0" or "-1"
STRATEGIES: list[str] = []
LOG_PATH: Path = Path("/tmp/autoresearch.log")
HEARTBEAT_PATH: Path = Path("/tmp/autoresearch-parent-heartbeat.json")
STOP_PATH: Path = Path("/tmp/autoresearch-stop")

# ─── Logging ─────────────────────────────────────────────────────────────────

logger = logging.getLogger("autoresearch")


def setup_logging():
    """Configure logging to file + stdout."""
    # Remove any existing handlers (re-init safe)
    root = logging.getLogger()
    root.handlers.clear()
    handlers = [
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOG_PATH, mode="a"),
    ]
    logging.basicConfig(
        level=logging.INFO,
        format=f"%(asctime)s [%(name)s{PARTITION_TAG}] %(levelname)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=handlers,
        force=True,
    )
    # Quiet noisy libs
    for name in ("urllib3", "matplotlib", "numexpr"):
        logging.getLogger(name).setLevel(logging.WARNING)


def rotate_log():
    """Rotate log if it exceeds max size."""
    try:
        if LOG_PATH.exists() and LOG_PATH.stat().st_size > LOG_MAX_BYTES:
            backup = LOG_PATH.with_suffix(".old")
            LOG_PATH.rename(backup)
            logger.info("Rotated log to %s", backup)
    except OSError:
        pass


# ─── Signals & Heartbeat ────────────────────────────────────────────────────

_shutdown_requested = threading.Event()


def _handle_signal(signum, frame):
    logger.info("Received signal %s — requesting shutdown.", signum)
    _shutdown_requested.set()
    STOP_PATH.touch()


def should_stop() -> bool:
    return _shutdown_requested.is_set() or STOP_PATH.exists()


def write_heartbeat(phase: str, strategy: str, cycle: int, **extra):
    try:
        strat_idx = STRATEGIES.index(strategy) if strategy in STRATEGIES else -1
        data = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "pid": os.getpid(),
            "partition": PARTITION,
            "phase": phase,
            "strategy": strategy,
            "strategy_index": strat_idx,
            "strategy_total": len(STRATEGIES),
            "cycle": cycle,
            "status": "running",
            **extra,
        }
        HEARTBEAT_PATH.write_text(json.dumps(data, indent=2))
    except OSError:
        pass


# ─── Telegram ───────────────────────────────────────────────────────────────

def send_telegram(message: str, level=None, category: str = "general"):
    """Best-effort smart Telegram notification.

    Uses the SmartNotifier for rate limiting and batching.
    Falls back to raw send_message if SmartNotifier fails.
    """
    try:
        from utils.telegram import notify, IMPORTANT
        if level is None:
            level = IMPORTANT
        notify(message, level=level, category=category)
    except Exception as e:
        logger.warning("Telegram failed: %s", e)


# ─── Sweep Phase ─────────────────────────────────────────────────────────────

def _acquire_sweep_lock() -> 'int | None':
    """Acquire exclusive sweep lock. Returns fd on success, None if busy.

    In partitioned mode, blocks until the lock is available (the other
    instance's sweep finishes). In solo mode, returns immediately.
    """
    if PARTITION is None:
        return None  # Solo mode — no coordination needed
    fd = os.open(str(SWEEP_LOCK_PATH), os.O_CREAT | os.O_RDWR)
    logger.info("SWEEP LOCK: waiting for exclusive access...")
    t0 = time.time()
    fcntl.flock(fd, fcntl.LOCK_EX)  # Blocks until available
    wait = time.time() - t0
    if wait > 1:
        logger.info("SWEEP LOCK: acquired after %.1fs wait", wait)
    else:
        logger.info("SWEEP LOCK: acquired immediately")
    # Write who holds it
    os.ftruncate(fd, 0)
    os.lseek(fd, 0, 0)
    os.write(fd, f"partition={PARTITION} pid={os.getpid()} since={datetime.now().isoformat()}\n".encode())
    return fd


def _release_sweep_lock(fd: 'int | None'):
    """Release the sweep lock."""
    if fd is None:
        return
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
        logger.info("SWEEP LOCK: released")
    except OSError:
        pass


def run_sweep(strategy: str) -> dict:
    """Run parallel parameter sweep for one strategy. CPU-bound.

    In partitioned mode, acquires an exclusive lock first so only one
    instance sweeps at a time (sweeps saturate all CPU cores).

    Returns: {"exit_code": int, "duration_s": float}
    """
    lock_fd = _acquire_sweep_lock()
    logger.info("SWEEP: %s (top %d, %d workers, max-fails %d)",
                strategy, SWEEP_TOP_N, SWEEP_WORKERS, SWEEP_MAX_FAILS)
    t0 = time.time()
    try:
        result = subprocess.run(
            [
                sys.executable, "research/sweep.py",
                "--strategy", strategy,
                "--top-n", str(SWEEP_TOP_N),
                "--workers", str(SWEEP_WORKERS),
                "--max-fails", str(SWEEP_MAX_FAILS),
                "--cycles", "1",
            ],
            timeout=SWEEP_TIMEOUT,
            capture_output=False,
        )
        duration = time.time() - t0
        logger.info("SWEEP done: %s (exit=%d, %.1f min)",
                     strategy, result.returncode, duration / 60)
        return {"exit_code": result.returncode, "duration_s": duration}
    except subprocess.TimeoutExpired:
        duration = time.time() - t0
        logger.warning("SWEEP timeout: %s after %.1f min", strategy, duration / 60)
        return {"exit_code": -1, "duration_s": duration}
    except Exception as e:
        duration = time.time() - t0
        logger.error("SWEEP error: %s — %s", strategy, e)
        return {"exit_code": -2, "duration_s": duration}
    finally:
        _release_sweep_lock(lock_fd)


# ─── Agent Phase ─────────────────────────────────────────────────────────────

def build_agent_prompt(strategy: str, cycle: int) -> str:
    """Build the LLM agent prompt with current state."""
    # Get best info
    try:
        from research.loop import load_best, read_results
        best = load_best(strategy)
        if best:
            m = best.get("metrics", {})
            best_info = (
                f"Best Sharpe: {m.get('sharpe', 0):.4f}, "
                f"Trades: {m.get('total_trades', 0)}, "
                f"Runs: {best.get('experiments_run', 0)}, "
                f"Kept: {best.get('experiments_kept', 0)}\n"
                f"Params: {json.dumps(best.get('params', {}), default=str)[:200]}"
            )
        else:
            best_info = "No results yet — needs baseline."
        best_info += "\n\nRecent history:\n" + read_results(strategy, 10)
    except Exception:
        best_info = "(failed to load)"

    # Get leaderboard
    try:
        from research.loop import leaderboard
        lb = leaderboard()
    except Exception:
        lb = "(failed)"

    return f"""You are an autonomous researcher. Read research/program.md first.

CURRENT STRATEGY: {strategy} (cycle {cycle})
The mechanical sweeper just finished a parameter grid pass on this strategy.
Now it's your turn to do what the sweeper can't.

SWEEPER RESULTS FOR {strategy}:
{best_info}

FULL LEADERBOARD:
{lb}

YOUR TASK (budget: 1 hour on {strategy}):
1. Start a ResearchSession('{strategy}', 'sp500') and baseline()
2. Look at the sweep history — what params improved? What patterns?
3. Try things the grid missed:
   - Parameter COMBINATIONS (pairs that interact, e.g. RSI period + oversold threshold)
   - Values BETWEEN grid points (the sweeper tried 7,10,14 — try 8,9,11,12)
   - Radical changes (disable a filter, flip a boolean, extreme values)
   - If Sharpe > 0.3: run combined_test() to check portfolio fit
4. When stuck (5+ discards): stop and let the loop move to the next strategy

RULES:
- NEVER ask the human — you are fully autonomous
- NEVER stop early — use your budget
- Move fast between experiments
- Follow keep/discard recommendations from the system
- The next cycle will sweep the grid again with your improvements as the new baseline"""


def run_agent(strategy: str, cycle: int) -> dict:
    """Run the LLM research agent. Mostly LLM-bound.

    Returns: {"exit_code": int, "duration_s": float}
    """
    logger.info("AGENT: %s (budget %ds)", strategy, AGENT_TIMEOUT)
    prompt = build_agent_prompt(strategy, cycle)
    t0 = time.time()
    try:
        result = subprocess.run(
            ["pi", "--print", "--skill", SKILL_DIR, "--no-session", prompt],
            timeout=AGENT_TIMEOUT + 60,
            capture_output=False,
        )
        duration = time.time() - t0
        logger.info("AGENT done: %s (exit=%d, %.1f min)",
                     strategy, result.returncode, duration / 60)
        return {"exit_code": result.returncode, "duration_s": duration}
    except subprocess.TimeoutExpired:
        duration = time.time() - t0
        logger.warning("AGENT timeout: %s after %.1f min", strategy, duration / 60)
        return {"exit_code": -1, "duration_s": duration}
    except Exception as e:
        duration = time.time() - t0
        logger.error("AGENT error: %s — %s", strategy, e)
        return {"exit_code": -2, "duration_s": duration}


# ─── Pipeline Orchestrator ───────────────────────────────────────────────────

def run_cycle(cycle: int) -> dict:
    """Run one full cycle through all strategies with pipelined execution.

    Pipeline pattern:
      sweep(A) → [agent(A) + sweep(B)] → [agent(B) + sweep(C)] → ...

    While the agent works on strategy N (LLM-bound, ~0 CPU),
    the sweep pre-runs on strategy N+1 (CPU-bound, all cores).
    When the agent finishes, the next strategy's sweep is already done.

    Returns: {"strategies": int, "sweep_time_s": float, "agent_time_s": float}
    """
    total_sweep_time = 0.0
    total_agent_time = 0.0
    strategies_completed = 0

    # Track pre-swept strategies
    sweep_done = {}         # strategy -> sweep result dict
    sweep_thread = None     # background sweep thread
    sweep_target = None     # which strategy is being pre-swept

    for i, strategy in enumerate(STRATEGIES):
        if should_stop():
            break

        logger.info("── Strategy: %s (%d/%d) ──", strategy, i + 1, len(STRATEGIES))

        # ── SWEEP: run if not already pre-swept ──────────────────────
        write_heartbeat("sweep", strategy, cycle)

        if strategy in sweep_done:
            logger.info("SWEEP: %s already pre-swept (%.1f min ago)",
                        strategy, sweep_done[strategy]["duration_s"] / 60)
        else:
            # If there's a background sweep running for a different strategy, wait for it
            if sweep_thread and sweep_thread.is_alive():
                sweep_thread.join()

            sweep_result = run_sweep(strategy)
            sweep_done[strategy] = sweep_result
            total_sweep_time += sweep_result["duration_s"]

        if should_stop():
            break

        # ── AGENT + pre-sweep next strategy in background ────────────
        write_heartbeat("agent", strategy, cycle)

        # Start pre-sweeping next strategy in background
        next_strategy = STRATEGIES[i + 1] if i + 1 < len(STRATEGIES) else None
        if next_strategy and next_strategy not in sweep_done and not should_stop():
            def _bg_sweep(strat=next_strategy):
                result = run_sweep(strat)
                sweep_done[strat] = result

            sweep_thread = threading.Thread(target=_bg_sweep, daemon=True)
            sweep_thread.start()
            sweep_target = next_strategy
            logger.info("PRE-SWEEP: %s started in background", next_strategy)

        # Run agent (foreground, blocks until done)
        agent_result = run_agent(strategy, cycle)
        total_agent_time += agent_result["duration_s"]
        strategies_completed += 1

        # If background sweep is still running (shouldn't be — agent takes way longer),
        # it'll be picked up next iteration.
        time.sleep(5)

    # Wait for any lingering background sweep
    if sweep_thread and sweep_thread.is_alive():
        logger.info("Waiting for background sweep to finish...")
        sweep_thread.join(timeout=SWEEP_TIMEOUT)

    return {
        "strategies": strategies_completed,
        "sweep_time_s": total_sweep_time,
        "agent_time_s": total_agent_time,
    }


# ─── Main Loop ───────────────────────────────────────────────────────────────

def main():
    global PARTITION, PARTITION_TAG, STRATEGIES, LOG_PATH, HEARTBEAT_PATH, STOP_PATH

    args = parse_args()
    PARTITION = args.partition

    # Configure partition-specific paths
    if PARTITION is not None:
        PARTITION_TAG = f"-{PARTITION}"
        STRATEGIES = [s for i, s in enumerate(ALL_STRATEGIES) if i % 2 == PARTITION]
    else:
        PARTITION_TAG = ""
        STRATEGIES = list(ALL_STRATEGIES)

    LOG_PATH = Path(f"/tmp/autoresearch{PARTITION_TAG}.log")
    HEARTBEAT_PATH = Path(f"/tmp/autoresearch-parent{PARTITION_TAG}-heartbeat.json")
    STOP_PATH = Path(f"/tmp/autoresearch{PARTITION_TAG}-stop")

    setup_logging()

    # Signal handling
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)
    STOP_PATH.unlink(missing_ok=True)

    mode = f"partition {PARTITION} ({len(STRATEGIES)} strategies)" if PARTITION is not None else "solo (all strategies)"
    logger.info("Autoresearch orchestrator starting (PID %d, %s)", os.getpid(), mode)
    logger.info("Strategies: %s", ", ".join(STRATEGIES))
    logger.info("Config: top_n=%d, workers=%d, max_fails=%d, agent_timeout=%ds",
                SWEEP_TOP_N, SWEEP_WORKERS, SWEEP_MAX_FAILS, AGENT_TIMEOUT)

    # Clear stale digest queue on fresh start
    try:
        from utils.telegram import flush_digest
        flush_digest()
    except Exception:
        pass

    tag = f" [P{PARTITION}]" if PARTITION is not None else ""
    send_telegram(
        f"🔬 <b>Autoresearch{tag} started</b>\n"
        f"Strategies: {', '.join(STRATEGIES)}\n"
        f"Workers: {SWEEP_WORKERS}, Top-N: {SWEEP_TOP_N}\n"
        f"Mode: {'partitioned (sweep lock)' if PARTITION is not None else 'solo pipeline'}",
        category="session",
    )

    cycle = 0
    session_start = time.time()

    while not should_stop():
        cycle += 1
        rotate_log()
        logger.info("════════════════════ Cycle %d ════════════════════", cycle)
        write_heartbeat("cycle_start", "", cycle)

        t0 = time.time()
        result = run_cycle(cycle)
        cycle_time = time.time() - t0

        # Cycle summary
        logger.info(
            "Cycle %d complete — %d strategies, sweep %.1f min, agent %.1f min, total %.1f min",
            cycle, result["strategies"],
            result["sweep_time_s"] / 60,
            result["agent_time_s"] / 60,
            cycle_time / 60,
        )

        # Leaderboard
        try:
            from research.loop import leaderboard
            lb = leaderboard()
            logger.info(lb)
        except Exception:
            lb = "(failed)"

        # Queue cycle summary for digest (INFO level — batched, not spammed)
        from utils.telegram import INFO, flush_digest
        elapsed_h = (time.time() - session_start) / 3600
        send_telegram(
            f"🔄 <b>Cycle {cycle}</b> — {cycle_time / 60:.0f}min "
            f"(sweep {result['sweep_time_s'] / 60:.0f}m + agent {result['agent_time_s'] / 60:.0f}m), "
            f"session {elapsed_h:.1f}h",
            level=INFO,
            category="cycle",
        )

        # Flush digest at end of each cycle (sends if enough time passed)
        try:
            flush_digest()
        except Exception:
            pass

        write_heartbeat("cycle_done", "", cycle, cycle_time_min=round(cycle_time / 60, 1))
        time.sleep(10)

    # Shutdown — flush any pending digest, then send stop message
    logger.info("Autoresearch stopped after %d cycles.", cycle)
    write_heartbeat("stopped", "", cycle)
    try:
        from utils.telegram import flush_digest
        flush_digest()
    except Exception:
        pass
    tag = f" [P{PARTITION}]" if PARTITION is not None else ""
    elapsed_h = (time.time() - session_start) / 3600
    send_telegram(
        f"🔬 <b>Autoresearch{tag} stopped</b>\n"
        f"Cycles: {cycle}\n"
        f"Runtime: {elapsed_h:.1f}h",
        category="session",
    )


if __name__ == "__main__":
    main()
