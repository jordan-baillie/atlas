#!/usr/bin/env python3
"""LLM-driven research loop runner.

Invokes Claude CLI to autonomously drive the research experiment loop.
Claude reads program.md, reviews history, proposes experiments, runs them
via ResearchSession, and keeps/discards based on results.

Usage:
    python3 research/llm_loop_runner.py --minutes 25
    python3 research/llm_loop_runner.py --minutes 25 --strategy mean_reversion
    python3 research/llm_loop_runner.py --minutes 25 --strategies mean_reversion,trend_following
"""

import argparse
import json
import logging
import os
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

ATLAS_ROOT = Path(__file__).resolve().parent.parent
if str(ATLAS_ROOT) not in sys.path:
    sys.path.insert(0, str(ATLAS_ROOT))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("llm_loop")

PROGRAM_MD = ATLAS_ROOT / "research" / "program.md"
LOGS_DIR = ATLAS_ROOT / "logs"


def _gather_context(strategies: list[str] | None = None) -> str:
    """Build the context block: leaderboard, strategy status, recent history, best params."""
    from research.loop import leaderboard, strategy_status, read_results, load_best

    sections = []

    # Strategy status overview
    try:
        sections.append("## Current Strategy Status\n" + strategy_status())
    except Exception as e:
        sections.append(f"## Strategy Status\n(error: {e})")

    # Leaderboard
    try:
        sections.append("## Leaderboard\n" + leaderboard())
    except Exception as e:
        sections.append(f"## Leaderboard\n(error: {e})")

    # Per-strategy details
    if strategies is None:
        # Use tier 1 strategies
        strategies = ["mean_reversion", "trend_following", "opening_gap",
                       "momentum_breakout", "sector_rotation"]

    for strat in strategies:
        try:
            history = read_results(strat, n=30)
            best = load_best(strat)
            best_str = json.dumps(best, indent=2, default=str) if best else "No best params saved."
            sections.append(
                f"## {strat} — Recent History (last 30)\n{history}\n\n"
                f"### Best Known Params\n```json\n{best_str}\n```"
            )
        except Exception as e:
            sections.append(f"## {strat}\n(error loading history: {e})")

    return "\n\n".join(sections)


def _build_prompt(minutes: int, strategies: list[str] | None = None, universe: str = "sp500") -> str:
    """Construct the full prompt for Claude."""
    # Load program.md
    program = ""
    if PROGRAM_MD.exists():
        program = PROGRAM_MD.read_text()

    # Load structured hypothesis bank (migrated from retired queue system)
    hypotheses_path = ATLAS_ROOT / "research" / "hypotheses.json"
    hypotheses_snippet = ""
    if hypotheses_path.exists():
        try:
            h = json.loads(hypotheses_path.read_text())
            hypotheses_snippet = "\n## Candidate Hypotheses (priority-ranked)\n"
            for hyp in h.get("hypotheses", [])[:10]:  # cap at 10 for prompt budget
                hypotheses_snippet += (
                    f"- [{hyp.get('priority')}] {hyp.get('title')} — {hyp.get('notes', '')[:120]}\n"
                )
            logger.debug("Loaded %d hypotheses from %s", len(h.get("hypotheses", [])), hypotheses_path)
        except Exception:
            pass

    context = _gather_context(strategies)

    strategy_focus = ""
    if strategies:
        strategy_focus = f"""
## Strategy Focus
Focus on these strategies in this session: {", ".join(strategies)}
Work through them in order. If one is already well-optimized (5+ consecutive discards), move to the next.
"""

    prompt = f"""You are an autonomous research agent running parameter optimization experiments on trading strategies.

## Time Budget
You have {minutes} minutes. Work efficiently. Run as many experiments as possible.
Stop running experiments 2 minutes before your time is up.

## Operating Manual
{program}

## Current State
{context}

{strategy_focus}
{hypotheses_snippet}

## Your Task
1. Review the current state above — leaderboard, history, best params.
2. Pick the highest-value strategy to work on (or use the focus list if provided).
3. Use Bash to run Python code that creates a ResearchSession and runs experiments.
4. Follow the keep/discard rules from the operating manual strictly.
5. Run as many experiments as the time budget allows.

## How to Run Experiments
Use the Bash tool to run Python code like this:

```bash
cd /root/atlas && python3 -c "
import sys; sys.path.insert(0, '/root/atlas')
from research.loop import ResearchSession

s = ResearchSession('mean_reversion', '{universe}')
baseline = s.baseline()
print('Baseline:', baseline)

# Try an experiment
r = s.experiment({{'rsi_period': 7}}, 'shorter RSI period for faster signals')
print('Result:', r)
print('Recommendation:', r.get('recommendation'))

# Keep or discard based on recommendation
if r.get('recommendation') == 'keep':
    s.keep()
    print('KEPT')
else:
    s.discard()
    print('DISCARDED')

print(s.summary())
"
```

IMPORTANT:
- Always call baseline() first for each new strategy session
- Each experiment takes 10-60 seconds depending on universe size
- Read the recommendation from experiment() result before deciding keep/discard
- Use top_n=50 for faster iterations: ResearchSession('strat', 'sp500', top_n=50)
- After finding improvements with top_n=50, verify with full universe (top_n=None)
- Record your reasoning for each experiment

## Output
After running experiments, print a summary of what you tried and the outcomes.
"""
    return prompt


def run_llm_loop(
    minutes: int = 25,
    strategies: list[str] | None = None,
    log_path: Path | None = None,
    universe: str = "sp500",
) -> dict:
    """Invoke Pi CLI to drive the research loop.

    Args:
        minutes:    Time budget in minutes.
        strategies: Optional list of strategies to focus on.
        log_path:   Path to write Pi's output log.

    Returns:
        dict with status, experiments_mentioned, runtime_s
    """
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    date_str = datetime.now().strftime("%Y%m%d_%H%M%S")
    if log_path is None:
        log_path = LOGS_DIR / f"llm_loop_{date_str}.log"

    # Circuit breaker — skip if Claude is exhausted
    try:
        from utils.claude_circuit_breaker import is_tripped, remaining_cooldown_sec
        if is_tripped():
            mins = remaining_cooldown_sec() // 60
            logger.error("Claude circuit breaker tripped — %d min cooldown remaining. Skipping LLM loop.", mins)
            return {"status": "breaker_tripped", "error": f"circuit breaker tripped, {mins}m left", "runtime_s": 0}
    except ImportError:
        pass  # Breaker not available, proceed anyway

    # Pre-check Pi auth
    try:
        from scripts.claude_auth_check import check_pi_auth
        auth = check_pi_auth()
        if not auth["logged_in"]:
            logger.error("Pi CLI auth failed: %s", auth.get("error", "unknown"))
            logger.error("The LLM loop requires working pi CLI. Fix auth before retrying.")
            logger.error("Atlas uses Claude Max via pi CLI. If 'out of extra usage', the Max subscription hit its quota window.")
            return {"status": "auth_error", "error": auth.get("error", "Pi CLI not available"), "runtime_s": 0}
    except ImportError:
        pass  # Auth check not available, proceed anyway

    # Fast probe — 30s haiku call to validate model routing before a 30-min hang
    try:
        from utils.pi_subprocess import call_pi as _probe_call
        _probe = _probe_call("ok", model="claude-haiku-4-5", timeout=30, mode=None, extra_args=["--no-tools"])
        if not _probe or not _probe.strip():
            logger.error("Pi probe returned empty — model routing may be broken. Aborting full LLM loop.")
            return {"status": "probe_failed", "runtime_s": 0}
    except Exception as e:
        logger.error("Pi probe failed (%s) — aborting full LLM loop to avoid 30-min hang.", e)
        return {"status": "probe_failed", "error": str(e), "runtime_s": 0}

    logger.info("Building LLM loop prompt (strategies=%s, minutes=%d)", strategies, minutes)
    prompt = _build_prompt(minutes, strategies, universe=universe)

    from utils.pi_subprocess import call_pi, PiSubprocessError  # noqa: PLC0415

    timeout_s = (minutes + 5) * 60  # extra 5 min buffer for startup/cleanup
    logger.info("Invoking Pi CLI via utils.pi_subprocess (timeout=%ds, log=%s)", timeout_s, log_path)
    start = time.time()

    try:
        output = call_pi(
            prompt,
            model="claude-sonnet-4-6",
            timeout=timeout_s,
            mode="json",
            extra_args=["--tools", "bash,read"],
            cwd=str(ATLAS_ROOT),
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
        )
        runtime_s = time.time() - start

        # Write raw output to log
        with open(log_path, "w") as f:
            f.write(f"=== LLM Loop Run {date_str}\n")
            f.write(f"Strategies: {strategies}\n")
            f.write(f"Minutes: {minutes}\n")
            f.write(f"Exit code: 0\n")
            f.write(f"Runtime: {runtime_s:.1f}s\n")
            f.write(f"\n=== STDOUT ===\n{output}\n")

        # Scan for exhaustion markers and trip breaker if found
        try:
            from utils.claude_circuit_breaker import scan_and_trip
            scan_and_trip(output, reason_prefix="llm_loop_runner")
        except ImportError:
            pass

        # Try to parse JSON output for structured result
        summary: dict = {
            "status": "complete",
            "exit_code": 0,
            "runtime_s": round(runtime_s, 1),
            "log_path": str(log_path),
        }

        try:
            parsed = json.loads(output)
            if isinstance(parsed, dict):
                summary["result_text"] = parsed.get("result", "")[:2000]
                summary["cost_usd"] = parsed.get("cost_usd", 0)
                summary["num_turns"] = parsed.get("num_turns", 0)
        except (json.JSONDecodeError, TypeError):
            summary["result_text"] = output[:2000] if output else ""

        logger.info("LLM loop finished: status=%s runtime=%.1fs", summary["status"], runtime_s)
        return summary

    except PiSubprocessError as e:
        runtime_s = time.time() - start
        err_msg = str(e)
        if "timed out" in err_msg:
            logger.error("Pi CLI timed out after %ds", timeout_s, extra={"timeout_s": timeout_s, "model": "claude-sonnet-4-6"})
            with open(log_path, "w") as f:
                f.write(f"=== LLM Loop TIMEOUT {date_str} ===\nTimeout after {timeout_s}s\n")
            return {"status": "timeout", "runtime_s": round(runtime_s, 1), "log_path": str(log_path)}
        if "not found on PATH" in err_msg:
            logger.error("Pi CLI not found. Install pi and ensure it's on PATH.")
            return {"status": "error", "error": "pi not found", "runtime_s": 0}
        logger.error("LLM loop error: %s", e)
        return {"status": "error", "error": str(e), "runtime_s": 0}

    except Exception as e:
        runtime_s = time.time() - start
        logger.error("LLM loop error: %s", e)
        return {"status": "error", "error": str(e), "runtime_s": 0}




def main():
    parser = argparse.ArgumentParser(
        description="LLM-driven research loop — Claude autonomously optimizes strategy parameters",
    )
    parser.add_argument("--minutes", type=int, default=25,
                        help="Time budget in minutes (default: 25)")
    parser.add_argument("--strategy", type=str, default=None,
                        help="Single strategy to focus on")
    parser.add_argument("--strategies", type=str, default=None,
                        help="Comma-separated list of strategies to focus on")
    parser.add_argument("--notify", action="store_true",
                        help="Send Telegram notification on completion")
    parser.add_argument("--universe", type=str, default="sp500",
                        help="Universe ID (default: sp500)")
    args = parser.parse_args()

    strategies = None
    if args.strategy:
        strategies = [args.strategy]
    elif args.strategies:
        strategies = [s.strip() for s in args.strategies.split(",")]

    summary = run_llm_loop(minutes=args.minutes, strategies=strategies, universe=args.universe)

    if args.notify:
        try:
            from alerting import get_alert_manager
            status = summary.get("status", "unknown")
            runtime = summary.get("runtime_s", 0) / 60
            emoji = "🧠" if status == "complete" else "⚠️"
            msg = (
                f"{emoji} <b>LLM Research Loop</b>\n"
                f"Status: {status}\n"
                f"Runtime: {runtime:.1f} min\n"
                f"Turns: {summary.get('num_turns', '?')}"
            )
            get_alert_manager().send(msg)
        except Exception as e:
            logger.warning("Telegram notify failed: %s", e)

    # Print summary
    print(json.dumps(summary, indent=2, default=str))
    return 0 if summary.get("status") == "complete" else 1


if __name__ == "__main__":
    sys.exit(main())
