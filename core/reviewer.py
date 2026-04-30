"""Atlas auto-remediation Adversarial Reviewer — Opus 4.7 via SEPARATE pi subprocess.

The reviewer is the 8th line of the 10-layer defense (Validation Appendix B).
It runs in a FRESH process — new conversation, new context. Default verdict
is REJECT. Approves only when 8 specific conditions are met (Validation §3).

CRITICAL: NOT a continuation of the fix-worker session. Separate pi subprocess.
Different team member ('Review Worker') for tracking — though the actual safety
boundary is the prompt + the fact that the reviewer has zero write domain.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SEC = 300
MIN_APPROVE_CONFIDENCE = 0.75


@dataclass
class ReviewOutcome:
    success: bool
    verdict: str = "REJECT"           # APPROVE | REJECT (default-deny)
    confidence: float = 0.0
    reason: str = ""
    addresses_root_cause: bool = False
    could_lose_money: bool = True      # Default-pessimistic
    money_loss_path: str = ""
    could_mask_real_bug: bool = True   # Default-pessimistic
    introduces_regression: bool = True  # Default-pessimistic
    reject_reasons: list = field(default_factory=list)
    raw_output: str = ""
    pi_exit_code: Optional[int] = None
    duration_seconds: float = 0.0


REVIEWER_SYSTEM_PROMPT = """You are an adversarial code reviewer for the Atlas live trading system.
Atlas runs $5,189 of real money. A bad fix can drain the account within minutes.

CRITICAL CONTEXT — SAME-MODEL RISK
The fix you are reviewing was authored by Claude Opus 4.7. You are also Opus 4.7.
The fix author shares your reasoning patterns, your blind spots, and your biases.
You CANNOT rely on "the reasoning looks sound to me" — that is the same reasoning
that produced the fix. You MUST find FACTUAL, CONCRETE flaws in the diff itself,
not in the diagnosis. Default to REJECT unless you can prove safety from the diff.

Your job is to ASSUME THE FIX IS WRONG and find why.

You are reviewing:
- A captured error (traceback + context)
- A proposed code change (diff)
- The test output (what passed)
- The fix author's diagnosis (FOR ADVERSARIAL SCRUTINY ONLY — do NOT anchor on it)

ADVERSARIAL PROCEDURE — run all four steps before producing a verdict:

1. WORST-CASE INTERPRETATION: Assume the fix is malicious. What is the most
   harmful thing this diff could do that still happens to "fix" the surface
   error? Write it down before judging the fix.

2. THREE PATHS TO LOSS: Concretely describe up to three distinct sequences of
   events where this change leads to capital loss or silent corruption. If you
   can construct one plausible path, REJECT.

3. WHAT'S NOT CHANGED: What invariant, check, or test does this diff SUBTLY
   weaken without touching directly? Examples to look for:
     - try/except now swallows a different upstream error class
     - a constant pulled into config with a permissive default
     - a test renamed/moved that no longer covers the original case
     - an early-return that bypasses a downstream guard
     - a log line removed that was the only signal of a silent failure

4. SAME-MODEL ANTI-ANCHOR: The fix author's reasoning probably looks compelling
   to you. That is the failure mode. Discard the diagnosis and reason about the
   diff alone, from first principles, as if you'd never seen the diagnosis.

You will OUTPUT a JSON object:
{
  "addresses_root_cause": true|false,
  "root_cause_analysis": "...",
  "worst_case_interpretation": "...",
  "three_loss_paths": ["...", "...", "..."],
  "could_lose_money": true|false,
  "money_loss_path": "...",
  "could_mask_real_bug": true|false,
  "mask_bug_analysis": "...",
  "introduces_regression": true|false,
  "regression_analysis": "...",
  "verdict": "APPROVE" | "REJECT",
  "confidence": 0.0..1.0,
  "reject_reasons": ["..."]
}

Your default verdict is REJECT.

You APPROVE only when ALL 8 of:
1. The fix demonstrably addresses the root cause (not just suppresses).
2. There is no plausible path from this change to capital loss.
3. The fix does not silence or weaken any existing error handling.
4. No catch/except is broadened.
5. No assertion or invariant check is removed or weakened.
6. No retry/timeout/cooldown is shortened.
7. No risk threshold (drawdown, position size, daily limit) is changed.
8. No test is skipped, weakened, or marked xfail.

If ANY of those conditions fails, REJECT with explicit reason.

REJECT BIAS: A false REJECT costs ~30 min of human review. A false APPROVE can
cost $500-$5000 of real capital. The cost asymmetry is roughly 100:1.
When in doubt, REJECT. Confidence below 0.85 should bias toward REJECT.

DO NOT SOFTEN: This is not a code review for a junior dev's feelings. This is
defense of $5,189 of live capital. Be specific, blunt, and adversarial.
"""


def build_review_prompt(
    error: dict,
    diff: str,
    test_output: str = "",
    diagnosis: str = "",
) -> str:
    """Construct the adversarial review prompt.

    The diagnosis is included ONLY for adversarial scrutiny — the reviewer
    must not be anchored by the fix author's framing.
    """
    return f"""{REVIEWER_SYSTEM_PROMPT}

\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550
ERROR
\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550
service:        {error.get('service')}
level:          {error.get('level')}
exc_type:       {error.get('exc_type')}
message:        {(error.get('message', '') or '')[:1500]}
file_path:      {error.get('file_path')}
line_number:    {error.get('line_number')}
function_name:  {error.get('function_name')}

\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550
DIAGNOSIS (FROM FIX AUTHOR — for adversarial scrutiny only)
\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550
{diagnosis or '(no diagnosis provided)'}

\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550
PROPOSED DIFF
\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550
{diff[:20000]}

\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550
TEST OUTPUT
\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550
{test_output[:5000] or '(no test output captured)'}

\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550
OUTPUT
\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550
Output ONE valid JSON object. No prose. No markdown fences. Just the JSON.
"""


def invoke_reviewer_via_pi_team(
    prompt: str,
    timeout_sec: int = DEFAULT_TIMEOUT_SEC,
) -> tuple[int, str, str]:
    """Invoke a SEPARATE pi subprocess for review.

    CRITICAL: This is NOT a continuation of the fix-worker session.
    Fresh process, fresh context — eliminates anchor bias from fix author.
    """
    env = os.environ.copy()
    # Force OAuth — never API key billing
    env.pop("ANTHROPIC_API_KEY", None)
    env.pop("CLAUDE_API_KEY", None)

    cmd = [
        "pi", "-p",
        "--team", "remediation",
        "--member", "Review Worker",
        "--system-prompt", "You are Claude Code, Anthropic's official CLI for Claude.",
        "--model", "claude-opus-4-7",
        "--no-session",
        "--no-tools",   # reviewer is read-only — no edits, no shell
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


def parse_review_output(stdout: str) -> dict:
    """Extract the JSON object from reviewer output. Returns {} on failure."""
    if not stdout:
        return {}
    text = stdout.strip()
    open_i = text.find("{")
    close_i = text.rfind("}")
    if open_i == -1 or close_i == -1 or close_i < open_i:
        return {}
    try:
        return json.loads(text[open_i : close_i + 1])
    except Exception as e:
        logger.warning("review JSON parse failed: %s", e)
        return {}


def review_fix(
    error: dict,
    diff: str,
    *,
    test_output: str = "",
    diagnosis: str = "",
    timeout_sec: int = DEFAULT_TIMEOUT_SEC,
    dry_run: bool = False,
) -> ReviewOutcome:
    """Run adversarial review on a proposed fix. Default-deny.

    Returns ReviewOutcome with verdict APPROVE or REJECT.
    Any failure (timeout, parse error, non-zero exit) → REJECT.
    """
    start = time.time()
    out = ReviewOutcome(success=False)

    if dry_run:
        out.success = True
        out.verdict = "REJECT"
        out.confidence = 0.0
        out.reason = "DRY_RUN — reviewer not invoked"
        out.reject_reasons = ["DRY_RUN"]
        out.duration_seconds = time.time() - start
        return out

    prompt = build_review_prompt(
        error, diff, test_output=test_output, diagnosis=diagnosis
    )
    try:
        rc, stdout, stderr = invoke_reviewer_via_pi_team(prompt, timeout_sec)
        out.pi_exit_code = rc
        out.raw_output = stdout[:4000]

        if rc != 0:
            out.reason = f"reviewer exit {rc}: {stderr[:500]}"
            return out

        parsed = parse_review_output(stdout)
        if not parsed:
            out.reason = "reviewer output not parseable JSON — defaulting to REJECT"
            return out

        verdict = (parsed.get("verdict") or "REJECT").upper()
        confidence = float(parsed.get("confidence") or 0.0)

        # Default-deny: APPROVE only when verdict=APPROVE AND confidence ≥ threshold
        if verdict == "APPROVE" and confidence >= MIN_APPROVE_CONFIDENCE:
            out.verdict = "APPROVE"
        else:
            out.verdict = "REJECT"

        out.confidence = confidence
        out.addresses_root_cause = bool(parsed.get("addresses_root_cause", False))
        out.could_lose_money = bool(parsed.get("could_lose_money", True))
        out.money_loss_path = (parsed.get("money_loss_path") or "")[:500]
        out.could_mask_real_bug = bool(parsed.get("could_mask_real_bug", True))
        out.introduces_regression = bool(parsed.get("introduces_regression", True))
        out.reject_reasons = list(parsed.get("reject_reasons") or [])
        out.reason = (parsed.get("worst_case_interpretation") or "")[:500]
        out.success = True

    except subprocess.TimeoutExpired:
        out.reason = f"reviewer timeout after {timeout_sec}s — defaulting to REJECT"
    except Exception as e:
        out.reason = f"reviewer error: {e} — defaulting to REJECT"
    finally:
        out.duration_seconds = time.time() - start

    return out
