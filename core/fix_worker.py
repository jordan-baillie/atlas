"""Atlas auto-remediation Fix Worker — invokes Opus 4.7 via multi-team dispatch.

Workflow per error:
  1. Pre-flight: kill-switch check, OAuth check, error fetched from DB
  2. Create git worktree at /tmp/atlas-fix-<error_id>/
  3. Construct fix prompt with NEVER-list preamble
  4. Invoke pi --team remediation (Fix Worker member, Opus 4.7) via subprocess
  5. Worker writes diff into worktree; we capture branch name + diff
  6. Run verify gates (deferred to merge_gates.py)
  7. Persist fix_attempts row + fix_audit_log entries
  8. Return FixOutcome

This module DOES NOT merge or revert. It only PROPOSES a fix branch.
core/merger.py handles the merge decision.

CRITICAL: Always invoke via `pi --team remediation` for domain enforcement.
Raw `pi -p` is FORBIDDEN — it bypasses the multi-team minimatch domain check.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SEC = 600   # 10-min hard cap per attempt
MAX_DIFF_LINES = 30          # Hard cap (validation report's strict default)


@dataclass
class FixOutcome:
    error_id: int
    fingerprint: str
    success: bool
    classification: str       # AUTO_FIX | ASSIST | ESCALATE
    branch: Optional[str] = None
    diff: Optional[str] = None
    diff_lines: int = 0
    diagnosis: Optional[str] = None
    fix_reasoning: Optional[str] = None
    worktree_path: Optional[str] = None
    error: Optional[str] = None
    duration_seconds: float = 0.0
    pi_exit_code: Optional[int] = None
    pi_stdout: Optional[str] = None
    pi_stderr: Optional[str] = None


def preflight_oauth() -> bool:
    """Verify pi CLI + OAuth before any LLM call."""
    try:
        from scripts.claude_auth_check import check_pi_auth  # type: ignore
        result = check_pi_auth()
        return result.get("logged_in", False)
    except Exception as e:
        logger.warning("OAuth preflight failed: %s", e)
        return False


def build_fix_prompt(error: dict, classification: str) -> str:
    """Construct the fix-author prompt with NEVER-list preamble.

    The preamble is non-negotiable: every fix-author session reminds the
    LLM of the safety bounds. Defense-in-depth — the OS-layer domain check
    is the actual enforcement.
    """
    deny_path = PROJECT_ROOT / "config" / "auto_fix_deny.yaml"
    funcs_path = PROJECT_ROOT / "config" / "safety_critical_functions.txt"
    try:
        deny_yaml = deny_path.read_text()
    except FileNotFoundError:
        deny_yaml = "(auto_fix_deny.yaml not found)"
    try:
        funcs_txt = funcs_path.read_text()
    except FileNotFoundError:
        funcs_txt = "(safety_critical_functions.txt not found)"

    body = f"""ATLAS LIVE TRADING SYSTEM — AUTONOMOUS REMEDIATION (Phase 2 ASSIST mode)

YOU ARE FORBIDDEN FROM MODIFYING:
- brokers/**, risk/**, regime/**, signals/**, monitor/lifecycle.py, monitor/evaluator.py,
  portfolio/**, overlay/**, strategies/**, core/reconcile.py, plans/**, approve/**,
  any file matching scripts/eod_*, scripts/intraday_monitor.py, scripts/director_cron.py,
  any config under config/active/, any file under data/, services/telegram_bot.py
  related to halt/approve/reject buttons, scripts/execute_approved.py, brokers/kill_switch.py,
  brokers/live_executor.py, brokers/live_portfolio.py, brokers/plan.py, brokers/alpaca/*,
  brokers/pdt_state.py, brokers/price_arbiter.py.

YOU ARE FORBIDDEN FROM:
- Modifying CHECK constraints, schema migrations, or any DDL
- Modifying any *_baseline.txt file other than via lint_*.py --update-baseline
- Adding new bare except: or except Exception: pass blocks
- Removing or weakening any test, assertion, or invariant
- Modifying any function listed in safety_critical_functions.txt ({len(funcs_txt.splitlines())} functions)
- Touching any file modified by a human in the last 24 hours
- Making changes >30 lines

If your proposed fix would require any of the above, RESPOND WITH:
"ESCALATE: <reason>" and STOP.

\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550
THE ERROR TO FIX
\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550

  error_id:        {error.get('id')}
  fingerprint:     {error.get('fingerprint')}
  occurrence_count: {error.get('occurrence_count', 1)}
  classification:  {classification}
  service:         {error.get('service')}
  level:           {error.get('level')}
  exc_type:        {error.get('exc_type')}
  message:         {(error.get('message', '') or '')[:1500]}
  file_path:       {error.get('file_path')}
  line_number:     {error.get('line_number')}
  function_name:   {error.get('function_name')}

  Traceback (top of stack, possibly truncated):
{(error.get('traceback') or '')[:3000]}

\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550
WORKFLOW
\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550

You are working in a git worktree (you should already be inside it). Your job:

1. REPRODUCE: Find the failing test or construct a minimal repro
   If you cannot reproduce, respond "ESCALATE: cannot reproduce: <reason>"

2. DIAGNOSE: Identify the root cause. Distinguish from symptoms.
   Output a short diagnosis (\u2264500 chars).

3. FIX: Write the minimal patch
   \u2022 Diff size MUST be \u226430 lines (hard cap)
   \u2022 Add a NEW regression test that FAILS on pre-fix code and PASSES on the fix
   \u2022 Test must reside in tests/ directory
   \u2022 Run pytest <affected> --timeout=30 in the worktree to verify

4. COMMIT: git add + git commit -m "auto-fix: <fingerprint> <one-line>"
   Commit message body should explain the diagnosis + the fix in 3-5 lines

5. REPORT: Print a JSON object as your FINAL output:
   {{"status": "PROPOSED", "branch": "<branch>", "diff_lines": <N>,
     "diagnosis": "<\u2264500 chars>", "fix_reasoning": "<\u2264500 chars>"}}

If anything fails, print:
   {{"status": "ESCALATE", "reason": "<why>"}}

DO NOT push. DO NOT merge. The branch will be merged by core/merger.py
after the Reviewer agent independently approves.
"""
    return body


def create_worktree(error_id: int, fingerprint: str) -> tuple[Path, str]:
    """Create a worktree at /tmp/atlas-fix-<id>/ with a new branch.

    Returns (worktree_path, branch_name).
    """
    short_fp = fingerprint[:8]
    branch = f"auto-fix/err-{error_id}-{short_fp}"
    worktree = Path(tempfile.gettempdir()) / f"atlas-fix-{error_id}-{int(time.time())}"

    # Ensure the branch doesn't already exist (clean up old attempt)
    subprocess.run(
        ["git", "branch", "-D", branch],
        cwd=PROJECT_ROOT,
        capture_output=True,
        timeout=10,
    )

    # Create worktree with new branch from main
    base = "main"
    result = subprocess.run(
        ["git", "worktree", "add", "-b", branch, str(worktree), base],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode != 0:
        raise RuntimeError(f"git worktree add failed: {result.stderr}")
    return worktree, branch


def remove_worktree(worktree: Path, *, prune_branch: Optional[str] = None) -> None:
    """Remove worktree from filesystem and git registry; optionally delete branch."""
    if worktree and worktree.exists():
        subprocess.run(
            ["git", "worktree", "remove", str(worktree), "--force"],
            cwd=PROJECT_ROOT,
            capture_output=True,
            timeout=30,
        )
    if prune_branch:
        subprocess.run(
            ["git", "branch", "-D", prune_branch],
            cwd=PROJECT_ROOT,
            capture_output=True,
            timeout=10,
        )


def invoke_fix_worker_via_pi_team(
    prompt: str,
    worktree: Path,
    timeout_sec: int = DEFAULT_TIMEOUT_SEC,
) -> tuple[int, str, str]:
    """Invoke pi via the multi-team dispatch path (NOT raw pi -p).

    The multi-team extension reads /root/.pi/teams/config.yaml and applies
    domain.write glob enforcement at the OS-tool layer. This is the
    structural safety boundary — even a hallucinating LLM can't write to
    NEVER-list paths because the tool refuses.

    Raw `pi -p` is FORBIDDEN — bypasses domain enforcement.
    """
    env = os.environ.copy()
    # Force OAuth — never API key billing
    env.pop("ANTHROPIC_API_KEY", None)
    env.pop("CLAUDE_API_KEY", None)
    env["TZ"] = "Australia/Brisbane"

    cmd = [
        "pi", "-p",
        "--team", "remediation",
        "--member", "Fix Worker",
        "--system-prompt", "You are Claude Code, Anthropic's official CLI for Claude.",
        "--no-session",
        "--cwd", str(worktree),
        "--mode", "json",
    ]
    proc = subprocess.run(
        cmd,
        input=prompt,
        capture_output=True,
        text=True,
        timeout=timeout_sec,
        env=env,
    )
    return proc.returncode, proc.stdout or "", proc.stderr or ""


def parse_fix_worker_output(stdout: str) -> dict:
    """Find the trailing JSON object from the worker output."""
    if not stdout:
        return {"status": "ERROR", "reason": "empty stdout"}
    # Strip non-JSON noise — find the last balanced { ... }
    text = stdout.strip()
    last_open = text.rfind("{")
    last_close = text.rfind("}")
    if last_open == -1 or last_close == -1 or last_close < last_open:
        return {"status": "ERROR", "reason": "no JSON found", "raw": text[:1000]}
    try:
        return json.loads(text[last_open : last_close + 1])
    except Exception as e:
        return {
            "status": "ERROR",
            "reason": f"invalid JSON: {e}",
            "raw": text[last_open : last_close + 1][:500],
        }


def capture_diff(worktree: Path, branch: str) -> tuple[str, int]:
    """Get the diff of the branch vs main, return (text, line_count)."""
    result = subprocess.run(
        ["git", "diff", f"main...{branch}"],
        cwd=worktree,
        capture_output=True,
        text=True,
        timeout=30,
    )
    diff = result.stdout or ""
    # Count added/removed lines (exclude +++ / --- header lines)
    added = sum(
        1 for ln in diff.split("\n")
        if ln.startswith("+") and not ln.startswith("+++")
    )
    removed = sum(
        1 for ln in diff.split("\n")
        if ln.startswith("-") and not ln.startswith("---")
    )
    return diff, added + removed


def run_fix(
    error: dict,
    *,
    classification: str = "ASSIST",
    timeout_sec: int = DEFAULT_TIMEOUT_SEC,
    dry_run: bool = False,
) -> FixOutcome:
    """Top-level fix attempt. Returns a FixOutcome. Does NOT merge."""
    start = time.time()
    error_id = error.get("id", 0)
    fingerprint = error.get("fingerprint", "")
    outcome = FixOutcome(
        error_id=error_id,
        fingerprint=fingerprint,
        success=False,
        classification=classification,
    )

    # Preflight kill-switch — must run even in dry_run
    try:
        from core.remediation_kill_switch import check_all_layers  # type: ignore
        block = check_all_layers()
        if block:
            outcome.error = f"kill-switch L{block.layer}: {block.reason}"
            outcome.duration_seconds = time.time() - start
            return outcome
    except Exception as e:
        outcome.error = f"kill-switch import failed: {e}"
        return outcome

    # OAuth preflight — skipped in dry_run
    if not dry_run and not preflight_oauth():
        outcome.error = "OAuth preflight failed — pi CLI not available"
        outcome.duration_seconds = time.time() - start
        return outcome

    worktree: Optional[Path] = None
    branch: Optional[str] = None
    try:
        worktree, branch = create_worktree(error_id, fingerprint)
        outcome.worktree_path = str(worktree)
        outcome.branch = branch

        prompt = build_fix_prompt(error, classification)

        if dry_run:
            outcome.success = True
            outcome.diagnosis = "DRY_RUN: prompt constructed, worker not invoked"
            outcome.fix_reasoning = "DRY_RUN"
            return outcome

        rc, stdout, stderr = invoke_fix_worker_via_pi_team(prompt, worktree, timeout_sec)
        outcome.pi_exit_code = rc
        outcome.pi_stdout = stdout[:4000]
        outcome.pi_stderr = stderr[:4000]

        if rc != 0:
            outcome.error = f"pi exit {rc}: {stderr[:500]}"
            return outcome

        result = parse_fix_worker_output(stdout)
        if result.get("status") == "PROPOSED":
            diff, n = capture_diff(worktree, branch)
            outcome.diff = diff[:20000]
            outcome.diff_lines = n
            outcome.diagnosis = (result.get("diagnosis") or "")[:500]
            outcome.fix_reasoning = (result.get("fix_reasoning") or "")[:500]
            outcome.success = True
        elif result.get("status") == "ESCALATE":
            outcome.error = f"worker ESCALATED: {result.get('reason', 'unknown')}"
        else:
            outcome.error = f"unexpected worker output: {result}"

    except subprocess.TimeoutExpired:
        outcome.error = f"timeout after {timeout_sec}s"
    except Exception as e:
        outcome.error = f"unexpected error: {e}"
    finally:
        outcome.duration_seconds = time.time() - start
        # If failed, clean up worktree + branch (no orphan branches)
        if not outcome.success and worktree is not None:
            try:
                remove_worktree(worktree, prune_branch=branch)
            except Exception:
                pass

    return outcome
