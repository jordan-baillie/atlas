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


def _build_prompt(minutes: int, strategies: list[str] | None = None) -> str:
    """Construct the full prompt for Claude."""
    # Load program.md
    program = ""
    if PROGRAM_MD.exists():
        program = PROGRAM_MD.read_text()

    context = _gather_context(strategies)

    strategy_focus = ""
    if strategies:
        strategy_focus = f"""
## Strategy Focus
Focus on these strategies in this session: {', '.join(strategies)}
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

s = ResearchSession('mean_reversion', 'sp500')
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
) -> dict:
    """Invoke Claude CLI to drive the research loop.

    Args:
        minutes:    Time budget in minutes.
        strategies: Optional list of strategies to focus on.
        log_path:   Path to write Claude's output log.

    Returns:
        dict with status, experiments_mentioned, runtime_s
    """
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    date_str = datetime.now().strftime("%Y%m%d_%H%M%S")
    if log_path is None:
        log_path = LOGS_DIR / f"llm_loop_{date_str}.log"

    # Pre-check Claude auth
    try:
        from scripts.claude_auth_check import check_claude_auth
        auth = check_claude_auth()
        if not auth["logged_in"]:
            logger.error("Claude CLI not authenticated (method=%s). Run 'claude setup-token' to fix.", auth["method"])
            return {"status": "auth_error", "error": "Claude not authenticated. Run: claude setup-token", "runtime_s": 0}
    except ImportError:
        pass  # Auth check not available, proceed anyway

    logger.info("Building LLM loop prompt (strategies=%s, minutes=%d)", strategies, minutes)
    prompt = _build_prompt(minutes, strategies)

    # Write prompt to temp file
    with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False, encoding="utf-8") as tf:
        tf.write(prompt)
        prompt_path = tf.name

    # Invoke Claude CLI
    cmd = [
        "claude", "-p",
        "--model", "claude-sonnet-4-6",
        "--output-format", "json",
        "--allowedTools", "Bash,Read",
    ]

    timeout_s = (minutes + 5) * 60  # extra 5 min buffer for startup/cleanup
    logger.info("Invoking Claude CLI (timeout=%ds, log=%s)", timeout_s, log_path)
    start = time.time()

    try:
        with open(prompt_path, "r", encoding="utf-8") as stdin_f:
            result = subprocess.run(
                cmd,
                stdin=stdin_f,
                capture_output=True,
                text=True,
                timeout=timeout_s,
                cwd=str(ATLAS_ROOT),
                env={**os.environ, "PYTHONUNBUFFERED": "1"},
            )

        runtime_s = time.time() - start
        Path(prompt_path).unlink(missing_ok=True)

        # Write raw output to log
        output = result.stdout or ""
        stderr = result.stderr or ""
        with open(log_path, "w") as f:
            f.write(f"=== LLM Loop Run {date_str} ===\n")
            f.write(f"Strategies: {strategies}\n")
            f.write(f"Minutes: {minutes}\n")
            f.write(f"Exit code: {result.returncode}\n")
            f.write(f"Runtime: {runtime_s:.1f}s\n")
            f.write(f"\n=== STDOUT ===\n{output}\n")
            if stderr:
                f.write(f"\n=== STDERR ===\n{stderr}\n")

        if result.returncode != 0:
            logger.warning("Claude CLI exited with code %d", result.returncode)
            logger.warning("stderr: %s", stderr[:500] if stderr else "(empty)")

        # Try to parse JSON output for structured result
        summary = {"status": "complete" if result.returncode == 0 else "error",
                    "exit_code": result.returncode,
                    "runtime_s": round(runtime_s, 1),
                    "log_path": str(log_path)}

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

    except subprocess.TimeoutExpired:
        runtime_s = time.time() - start
        Path(prompt_path).unlink(missing_ok=True)
        logger.error("Claude CLI timed out after %ds", timeout_s)
        with open(log_path, "w") as f:
            f.write(f"=== LLM Loop TIMEOUT {date_str} ===\nTimeout after {timeout_s}s\n")
        return {"status": "timeout", "runtime_s": round(runtime_s, 1), "log_path": str(log_path)}

    except FileNotFoundError:
        Path(prompt_path).unlink(missing_ok=True)
        logger.error("Claude CLI not found. Install: npm install -g @anthropic-ai/claude-code")
        return {"status": "error", "error": "claude not found", "runtime_s": 0}

    except Exception as e:
        Path(prompt_path).unlink(missing_ok=True)
        logger.error("LLM loop error: %s", e)
        return {"status": "error", "error": str(e), "runtime_s": 0}


def _send_telegram(summary: dict) -> None:
    """Send a brief Telegram notification about the LLM loop run."""
    try:
        from utils.telegram import notify
        status = summary.get("status", "unknown")
        runtime = summary.get("runtime_s", 0) / 60
        emoji = "🧠" if status == "complete" else "⚠️"
        msg = (
            f"{emoji} <b>LLM Research Loop</b>\n"
            f"Status: {status}\n"
            f"Runtime: {runtime:.1f} min\n"
            f"Turns: {summary.get('num_turns', '?')}"
        )
        notify(msg, category="autoresearch")
    except Exception as e:
        logger.warning("Telegram notify failed: %s", e)


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
    args = parser.parse_args()

    strategies = None
    if args.strategy:
        strategies = [args.strategy]
    elif args.strategies:
        strategies = [s.strip() for s in args.strategies.split(",")]

    summary = run_llm_loop(minutes=args.minutes, strategies=strategies)

    if args.notify:
        _send_telegram(summary)

    # Print summary
    print(json.dumps(summary, indent=2, default=str))
    return 0 if summary.get("status") == "complete" else 1


if __name__ == "__main__":
    sys.exit(main())
