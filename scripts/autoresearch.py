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

# Dynamic strategy queue — autoresearch reads active strategies from here each cycle
QUEUE_PATH = PROJECT / "research" / "strategy_queue.json"

# Directives — written by Director agent, consumed by Atlas/Nova to guide research focus
DIRECTIVES_PATH = PROJECT / "research" / "directives.json"

# Director research queue — queued experiments written by Director for agents to consume
RESEARCH_QUEUE_PATH = PROJECT / "research" / "queue.json"

# Cycle report — written at end of each cycle for Director to read (path set in main)
REPORT_PATH: Path = Path("/tmp/autoresearch-report-solo.json")

# Fallback strategy list used when QUEUE_PATH is missing or unreadable
_FALLBACK_STRATEGIES = [
    "mean_reversion",
    "trend_following",
    "opening_gap",
    "connors_rsi2",
    "momentum_breakout",
    "short_term_mr",
    "bb_squeeze",
]


def load_strategies_from_queue() -> list[str]:
    """Load active strategy names from research/strategy_queue.json.

    Reads the 'active' array and returns names in order.
    Falls back to _FALLBACK_STRATEGIES if the file is missing, empty, or invalid.
    """
    try:
        data = json.loads(QUEUE_PATH.read_text())
        active = [s["name"] for s in data.get("active", []) if s.get("name")]
        if active:
            return active
        logger.warning("QUEUE: No active strategies in %s — using fallback", QUEUE_PATH)
    except FileNotFoundError:
        logger.warning("QUEUE: %s not found — using fallback strategies", QUEUE_PATH)
    except Exception as e:
        logger.error("QUEUE: Failed to load %s: %s — using fallback", QUEUE_PATH, e)
    return list(_FALLBACK_STRATEGIES)

def load_directives() -> dict:
    """Load research/directives.json. Returns empty dict on missing/error.

    Directives are written by the Director agent to guide Atlas, Nova, and Sage.
    Each agent reads its own section at cycle start.
    """
    try:
        return json.loads(DIRECTIVES_PATH.read_text())
    except FileNotFoundError:
        return {}
    except Exception as e:
        logger.warning("DIRECTIVES: Failed to load %s: %s", DIRECTIVES_PATH, e)
        return {}


def apply_directives_to_strategies(strategies: list[str]) -> list[str]:
    """Apply Director directives to the strategy list.

    Reads the appropriate agent section (atlas for partition 0 / solo,
    nova for partition 1) and applies in order:
      1. assignments  — if set, override the partition-split list entirely
      2. deprioritized — filter these strategies out of this cycle
      3. focus        — sort focused strategies to the front (preserving order)

    Returns the modified strategy list.
    """
    directives = load_directives()
    if not directives:
        return strategies

    # Determine which agent section to use
    agent_key = "nova" if PARTITION == 1 else "atlas"
    agent_dir = directives.get(agent_key, {})
    if not agent_dir:
        return strategies

    # 1. Assignments override partition split
    assignments = agent_dir.get("assignments")
    if assignments and isinstance(assignments, list):
        logger.info("DIRECTIVES: %s using Director-assigned strategies: %s",
                    agent_key, assignments)
        strategies = [s for s in assignments if s]

    # 2. Filter deprioritized
    deprioritized = set(agent_dir.get("deprioritized", []))
    if deprioritized:
        before = len(strategies)
        strategies = [s for s in strategies if s not in deprioritized]
        removed = before - len(strategies)
        if removed:
            logger.info("DIRECTIVES: Removed %d deprioritized strategies from %s cycle",
                        removed, agent_key)

    # 3. Sort by focus (focused strategies bubble to front, preserve declared order)
    focus = agent_dir.get("focus", [])
    if focus:
        strat_set = set(strategies)
        focused = [s for s in focus if s in strat_set]   # keep focus order
        others  = [s for s in strategies if s not in set(focus)]
        strategies = focused + others
        logger.info("DIRECTIVES: Focus applied — %d focused, %d other strategies",
                    len(focused), len(others))

    return strategies


# ─── Director Queue Integration ──────────────────────────────────────────────

def _check_director_queue(strategies: list[str], agent_name: str) -> list[dict]:
    """Find and claim queued experiments from research/queue.json matching our strategies.

    Looks for entries with status=="queued", no claimed_by, and strategy_name in our
    partition's strategies. Atomically claims them and returns the list.
    """
    try:
        queue = json.loads(RESEARCH_QUEUE_PATH.read_text())
    except Exception:
        return []

    claimed = []
    for entry in queue:
        if (
            entry.get("status") == "queued"
            and not entry.get("claimed_by")
            and entry.get("strategy_name") in strategies
        ):
            entry["claimed_by"] = agent_name
            entry["claimed_at"] = datetime.now(timezone.utc).isoformat()
            entry["status"] = "claimed"
            claimed.append(entry)

    if claimed:
        # Write back atomically
        tmp = RESEARCH_QUEUE_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(queue, indent=2))
        tmp.rename(RESEARCH_QUEUE_PATH)
        logger.info(
            "Director queue: claimed %d experiments for %s: %s",
            len(claimed),
            agent_name,
            [e.get("id", "?") for e in claimed],
        )

    return claimed


def _update_queue_entries(entries: list[dict], status: str) -> None:
    """Update queue entries status after agent completes (done or failed)."""
    if not entries:
        return
    try:
        queue = json.loads(RESEARCH_QUEUE_PATH.read_text())
        entry_ids = {e["id"] for e in entries if "id" in e}
        updated = 0
        for q_entry in queue:
            if q_entry.get("id") in entry_ids:
                q_entry["status"] = status
                q_entry["updated_at"] = datetime.now(timezone.utc).isoformat()
                updated += 1
        if updated:
            tmp = RESEARCH_QUEUE_PATH.with_suffix(".tmp")
            tmp.write_text(json.dumps(queue, indent=2))
            tmp.rename(RESEARCH_QUEUE_PATH)
            logger.info("Director queue: marked %d entries as '%s'", updated, status)
    except Exception as e:
        logger.warning("Failed to update queue entries: %s", e)


def _write_cycle_report(
    cycle: int,
    strategies_details: list[dict],
    total_experiments: int,
) -> None:
    """Write cycle report for Director to read.

    Written atomically to REPORT_PATH (e.g. /tmp/autoresearch-report-0.json).
    The Director reads these in gather_state() to know what each agent accomplished.
    """
    agent_name = "nova" if PARTITION == 1 else "atlas"

    # Compute next strategies (wrap around for preview)
    strat_count = len(STRATEGIES)
    if strat_count > 0:
        next_strategies = STRATEGIES[:min(3, strat_count)]
    else:
        next_strategies = []

    report = {
        "partition": PARTITION,
        "agent_name": agent_name,
        "cycle": cycle,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "strategies_completed": strategies_details,
        "total_experiments_this_cycle": total_experiments,
        "next_strategies": next_strategies,
    }

    tmp = REPORT_PATH.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(report, indent=2))
        tmp.rename(REPORT_PATH)
        logger.info(
            "Cycle report written: cycle=%d, strategies=%d, experiments=%d",
            cycle,
            len(strategies_details),
            total_experiments,
        )
    except Exception as e:
        logger.warning("Failed to write cycle report to %s: %s", REPORT_PATH, e)


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
# NOTE: REPORT_PATH is also set in main() to /tmp/autoresearch-report-{0,1,solo}.json

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

def build_agent_prompt(strategy: str, cycle: int, director_exps: list[dict] | None = None) -> str:
    """Build the LLM agent prompt with current state.

    Args:
        strategy:      Strategy name to research.
        cycle:         Current cycle number.
        director_exps: List of Director-queued experiments to run as priority tasks.
                       These are injected into the prompt so the agent runs them first.
    """
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

    # Build Director-queued experiments section (if any)
    director_section = ""
    if director_exps:
        director_section = (
            "\n\n⚡ DIRECTOR-QUEUED EXPERIMENTS (run these FIRST — high priority):\n"
        )
        for exp in director_exps:
            exp_id = exp.get("id", "?")
            title = exp.get("title", exp_id)
            hypothesis = exp.get("hypothesis", "")[:200]
            params = exp.get("params_override")
            director_section += f"• [{exp_id}] {title}\n"
            if hypothesis:
                director_section += f"  Hypothesis: {hypothesis}\n"
            if params:
                director_section += f"  Params override: {json.dumps(params, default=str)[:300]}\n"
        director_section += (
            "\nFor each Director experiment: run experiment() with the params_override "
            "shown, then keep() or discard() as normal.\n"
        )

    return f"""You are an autonomous researcher. Read research/program.md first.

CURRENT STRATEGY: {strategy} (cycle {cycle})
The mechanical sweeper just finished a parameter grid pass on this strategy.
Now it's your turn to do what the sweeper can't.

SWEEPER RESULTS FOR {strategy}:
{best_info}

FULL LEADERBOARD:
{lb}
{director_section}
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


def run_agent(strategy: str, cycle: int, director_exps: list[dict] | None = None) -> dict:
    """Run the LLM research agent. Mostly LLM-bound.

    Args:
        strategy:     Strategy name to research.
        cycle:        Current cycle number.
        director_exps: Director-queued experiments to inject into the agent prompt.

    Returns: {"exit_code": int, "duration_s": float}
    """
    logger.info("AGENT: %s (budget %ds)", strategy, AGENT_TIMEOUT)
    prompt = build_agent_prompt(strategy, cycle, director_exps=director_exps)
    t0 = time.time()

    # Circuit breaker — skip if Claude is exhausted
    try:
        from utils.claude_circuit_breaker import is_tripped, remaining_cooldown_sec, scan_and_trip
        if is_tripped():
            mins = remaining_cooldown_sec() // 60
            print(f"[autoresearch] Circuit breaker tripped ({mins}m cooldown) — skipping pi call", flush=True)
            logger.warning("Claude circuit breaker tripped — %dm cooldown remaining. Skipping agent pi call.", mins)
            return {"exit_code": -3, "duration_s": 0.0}
    except ImportError:
        scan_and_trip = None  # degrade gracefully

    try:
        result = subprocess.run(
            ["pi", "--print", "--skill", SKILL_DIR, "--no-session",
             "--system-prompt", "You are Claude Code, Anthropic's official CLI for Claude.",
             prompt],
            timeout=AGENT_TIMEOUT + 60,
            capture_output=False,
        )
        duration = time.time() - t0
        logger.info("AGENT done: %s (exit=%d, %.1f min)",
                     strategy, result.returncode, duration / 60)
        # Scan captured output for exhaustion markers (capture_output=False so stdout/stderr are None — safe no-op)
        try:
            if scan_and_trip is not None:
                scan_and_trip((result.stdout or "") + "\n" + (result.stderr or ""), reason_prefix="autoresearch")
        except Exception:
            pass
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

def run_cycle(cycle: int, claimed_experiments: list[dict] | None = None) -> dict:
    """Run one full cycle through all strategies with pipelined execution.

    Pipeline pattern:
      sweep(A) → [agent(A) + sweep(B)] → [agent(B) + sweep(C)] → ...

    While the agent works on strategy N (LLM-bound, ~0 CPU),
    the sweep pre-runs on strategy N+1 (CPU-bound, all cores).
    When the agent finishes, the next strategy's sweep is already done.

    Args:
        cycle:               Current cycle number.
        claimed_experiments: Director-queued experiments claimed at cycle start.
                             These are passed to agents as priority tasks and marked
                             done/failed after the agent completes.

    Returns: {"strategies": int, "sweep_time_s": float, "agent_time_s": float,
              "strategies_details": list, "total_experiments": int}
    """
    total_sweep_time = 0.0
    total_agent_time = 0.0
    strategies_completed = 0
    strategies_details: list[dict] = []

    # Build per-strategy map of director-queued experiments
    director_queue: dict[str, list[dict]] = {}
    if claimed_experiments:
        for exp in claimed_experiments:
            sname = exp.get("strategy_name")
            if sname:
                director_queue.setdefault(sname, []).append(exp)

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

        # Read experiments_run before agent — to compute delta for cycle report
        try:
            from research.loop import load_best as _load_best
            _best_before = _load_best(strategy)
            _exp_before = _best_before.get("experiments_run", 0) if _best_before else 0
        except Exception:
            _exp_before = 0

        # Get director-queued experiments for this strategy (if any)
        strat_director_exps = director_queue.get(strategy, [])
        if strat_director_exps:
            logger.info(
                "AGENT: %s has %d Director-queued experiments to run first",
                strategy, len(strat_director_exps),
            )

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

        # Run agent (foreground, blocks until done), passing director experiments
        agent_result = run_agent(strategy, cycle, director_exps=strat_director_exps or None)
        total_agent_time += agent_result["duration_s"]
        strategies_completed += 1

        # Update Director queue entries based on agent exit code
        if strat_director_exps:
            outcome = "done" if agent_result["exit_code"] == 0 else "failed"
            _update_queue_entries(strat_director_exps, outcome)

        # Read experiments_run after agent — compute delta for cycle report
        try:
            _best_after = _load_best(strategy)
            _exp_after = _best_after.get("experiments_run", 0) if _best_after else 0
            _best_sharpe = (
                _best_after.get("metrics", {}).get("sharpe", 0) if _best_after else 0
            )
        except Exception:
            _exp_after = _exp_before
            _best_sharpe = 0

        strategies_details.append({
            "name": strategy,
            "phase": "agent+sweep",
            "experiments_run": max(0, _exp_after - _exp_before),
            "best_sharpe": round(float(_best_sharpe or 0), 4),
        })

        # If background sweep is still running (shouldn't be — agent takes way longer),
        # it'll be picked up next iteration.
        time.sleep(5)

    # Wait for any lingering background sweep
    if sweep_thread and sweep_thread.is_alive():
        logger.info("Waiting for background sweep to finish...")
        sweep_thread.join(timeout=SWEEP_TIMEOUT)

    total_experiments = sum(d["experiments_run"] for d in strategies_details)

    return {
        "strategies": strategies_completed,
        "sweep_time_s": total_sweep_time,
        "agent_time_s": total_agent_time,
        "strategies_details": strategies_details,
        "total_experiments": total_experiments,
    }


# ─── Main Loop ───────────────────────────────────────────────────────────────

def main():
    global PARTITION, PARTITION_TAG, STRATEGIES, LOG_PATH, HEARTBEAT_PATH, STOP_PATH
    global REPORT_PATH

    args = parse_args()
    PARTITION = args.partition

    # Configure partition-specific paths
    _all = load_strategies_from_queue()
    if PARTITION is not None:
        PARTITION_TAG = f"-{PARTITION}"
        STRATEGIES = [s for i, s in enumerate(_all) if i % 2 == PARTITION]
    else:
        PARTITION_TAG = ""
        STRATEGIES = list(_all)

    LOG_PATH = Path(f"/tmp/autoresearch{PARTITION_TAG}.log")
    HEARTBEAT_PATH = Path(f"/tmp/autoresearch-parent{PARTITION_TAG}-heartbeat.json")
    STOP_PATH = Path(f"/tmp/autoresearch{PARTITION_TAG}-stop")

    # Cycle report path — Director reads these to track agent progress
    # PARTITION=None → solo, PARTITION=0 → 0, PARTITION=1 → 1
    _report_suffix = PARTITION if PARTITION is not None else "solo"
    REPORT_PATH = Path(f"/tmp/autoresearch-report-{_report_suffix}.json")

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

        # Re-read queue each cycle — picks up strategies added/removed at runtime
        _all = load_strategies_from_queue()
        if PARTITION is not None:
            STRATEGIES = [s for i, s in enumerate(_all) if i % 2 == PARTITION]
        else:
            STRATEGIES = list(_all)

        # Apply Director directives: assignments override, deprioritized filter, focus sort
        STRATEGIES = apply_directives_to_strategies(STRATEGIES)
        logger.info("Strategies this cycle: %s", ", ".join(STRATEGIES))

        # Check Director research queue for priority experiments to run this cycle
        _agent_name = "nova" if PARTITION == 1 else "atlas"
        _claimed = _check_director_queue(STRATEGIES, _agent_name)

        write_heartbeat("cycle_start", "", cycle)

        t0 = time.time()
        result = run_cycle(cycle, claimed_experiments=_claimed)
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

        # Write cycle report for Director to read (atomically, before sleep)
        _write_cycle_report(
            cycle,
            result.get("strategies_details", []),
            result.get("total_experiments", 0),
        )

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
