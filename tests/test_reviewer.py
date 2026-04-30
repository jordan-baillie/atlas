"""Tests for core/reviewer.py — adversarial reviewer, default-deny.

All subprocess.run calls mocked — no real pi invocations occur.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

from core.reviewer import (
    MIN_APPROVE_CONFIDENCE,
    REVIEWER_SYSTEM_PROMPT,
    ReviewOutcome,
    build_review_prompt,
    invoke_reviewer_via_pi_team,
    parse_review_output,
    review_fix,
)


# ── Shared fixtures ──────────────────────────────────────────────────────────

@pytest.fixture()
def sample_error() -> dict:
    return {
        "id": 7,
        "fingerprint": "cafebabe12345678",
        "service": "atlas-dashboard",
        "level": "ERROR",
        "exc_type": "ValueError",
        "message": "invalid literal for int() with base 10: 'abc'",
        "file_path": "tests/helper.py",
        "line_number": 12,
        "function_name": "parse_qty",
    }


@pytest.fixture()
def sample_diff() -> str:
    return (
        "--- a/tests/helper.py\n"
        "+++ b/tests/helper.py\n"
        "@@ -10,3 +10,6 @@\n"
        " def parse_qty(s):\n"
        "-    return int(s)\n"
        '+    try:\n'
        '+        return int(s)\n'
        '+    except ValueError:\n'
        '+        return 0\n'
    )


def _approved_json(**overrides) -> str:
    data = {
        "addresses_root_cause": True,
        "root_cause_analysis": "safe",
        "worst_case_interpretation": "none",
        "could_lose_money": False,
        "money_loss_path": "",
        "could_mask_real_bug": False,
        "mask_bug_analysis": "no",
        "introduces_regression": False,
        "regression_analysis": "no",
        "verdict": "APPROVE",
        "confidence": 0.85,
        "reject_reasons": [],
    }
    data.update(overrides)
    return json.dumps(data)


# ── 1. build_review_prompt ───────────────────────────────────────────────────

class TestBuildReviewPrompt:
    def test_includes_adversarial_system_prompt_verbatim(self, sample_error, sample_diff):
        prompt = build_review_prompt(sample_error, sample_diff)
        # System prompt must appear in the constructed prompt
        assert "adversarial code reviewer" in prompt
        assert "ASSUME THE FIX IS WRONG" in prompt

    def test_includes_error_context(self, sample_error, sample_diff):
        prompt = build_review_prompt(sample_error, sample_diff)
        assert sample_error["exc_type"] in prompt
        assert sample_error["file_path"] in prompt
        assert sample_error["function_name"] in prompt

    def test_includes_diff(self, sample_error, sample_diff):
        prompt = build_review_prompt(sample_error, sample_diff)
        assert "parse_qty" in prompt
        assert "+        return 0" in prompt

    def test_includes_test_output_when_provided(self, sample_error, sample_diff):
        prompt = build_review_prompt(
            sample_error, sample_diff, test_output="PASSED 3 tests"
        )
        assert "PASSED 3 tests" in prompt

    def test_diagnosis_labelled_for_adversarial_scrutiny(self, sample_error, sample_diff):
        """Diagnosis must be labelled as 'for adversarial scrutiny only'."""
        prompt = build_review_prompt(
            sample_error, sample_diff, diagnosis="author thinks it's X"
        )
        assert "adversarial scrutiny" in prompt.lower()
        assert "author thinks it's X" in prompt

    def test_no_test_output_shows_placeholder(self, sample_error, sample_diff):
        prompt = build_review_prompt(sample_error, sample_diff, test_output="")
        assert "no test output captured" in prompt

    def test_no_diagnosis_shows_placeholder(self, sample_error, sample_diff):
        prompt = build_review_prompt(sample_error, sample_diff)
        assert "no diagnosis provided" in prompt

    def test_8_approve_conditions_listed(self, sample_error, sample_diff):
        """All 8 approval conditions must appear."""
        prompt = build_review_prompt(sample_error, sample_diff)
        # Check the numbered list 1..8 is present
        for i in range(1, 9):
            assert str(i) + "." in prompt


# ── 2. parse_review_output ───────────────────────────────────────────────────

class TestParseReviewOutput:
    def test_parses_approve_verdict(self):
        stdout = '{"verdict": "APPROVE", "confidence": 0.8, "reject_reasons": []}'
        result = parse_review_output(stdout)
        assert result["verdict"] == "APPROVE"
        assert result["confidence"] == 0.8

    def test_garbage_returns_empty_dict(self):
        result = parse_review_output("this is prose not JSON")
        assert result == {}

    def test_empty_string_returns_empty_dict(self):
        result = parse_review_output("")
        assert result == {}

    def test_parses_reject_reasons_list(self):
        stdout = json.dumps({
            "verdict": "REJECT",
            "confidence": 0.3,
            "reject_reasons": ["broadens catch", "silences error"],
        })
        result = parse_review_output(stdout)
        assert result["verdict"] == "REJECT"
        assert len(result["reject_reasons"]) == 2

    def test_finds_json_amid_prose(self):
        stdout = "Thinking...\nSome prose.\n" + json.dumps({"verdict": "APPROVE", "confidence": 0.9})
        result = parse_review_output(stdout)
        assert result["verdict"] == "APPROVE"


# ── 3. review_fix orchestration ──────────────────────────────────────────────

class TestReviewFix:
    def test_dry_run_returns_reject(self, sample_error, sample_diff):
        """Default-deny must be preserved even in dry-run."""
        out = review_fix(sample_error, sample_diff, dry_run=True)
        assert out.success is True
        assert out.verdict == "REJECT"
        assert "DRY_RUN" in out.reason

    def test_approve_when_verdict_approve_and_high_confidence(self, sample_error, sample_diff):
        with patch(
            "core.reviewer.invoke_reviewer_via_pi_team",
            return_value=(0, _approved_json(verdict="APPROVE", confidence=0.85), ""),
        ):
            out = review_fix(sample_error, sample_diff)
        assert out.verdict == "APPROVE"
        assert out.success is True

    def test_reject_when_confidence_below_threshold(self, sample_error, sample_diff):
        """APPROVE with confidence=0.5 must still be REJECT (below 0.75 threshold)."""
        with patch(
            "core.reviewer.invoke_reviewer_via_pi_team",
            return_value=(0, _approved_json(verdict="APPROVE", confidence=0.5), ""),
        ):
            out = review_fix(sample_error, sample_diff)
        assert out.verdict == "REJECT"

    def test_reject_on_timeout(self, sample_error, sample_diff):
        with patch(
            "core.reviewer.invoke_reviewer_via_pi_team",
            side_effect=subprocess.TimeoutExpired(cmd="pi", timeout=300),
        ):
            out = review_fix(sample_error, sample_diff)
        assert out.verdict == "REJECT"
        assert out.success is False
        assert "timeout" in out.reason.lower()

    def test_reject_on_nonzero_exit(self, sample_error, sample_diff):
        with patch(
            "core.reviewer.invoke_reviewer_via_pi_team",
            return_value=(1, "", "crash"),
        ):
            out = review_fix(sample_error, sample_diff)
        assert out.verdict == "REJECT"
        assert out.success is False

    def test_reject_on_unparseable_output(self, sample_error, sample_diff):
        with patch(
            "core.reviewer.invoke_reviewer_via_pi_team",
            return_value=(0, "just prose no json", ""),
        ):
            out = review_fix(sample_error, sample_diff)
        assert out.verdict == "REJECT"
        assert out.success is False

    def test_reject_when_verdict_field_missing(self, sample_error, sample_diff):
        """JSON returned but no 'verdict' key — default-deny must fire."""
        payload = json.dumps({"confidence": 0.9, "reject_reasons": []})
        with patch(
            "core.reviewer.invoke_reviewer_via_pi_team",
            return_value=(0, payload, ""),
        ):
            out = review_fix(sample_error, sample_diff)
        assert out.verdict == "REJECT"

    def test_adversarial_flags_default_pessimistic(self, sample_error, sample_diff):
        """Even on a successful parse, adversarial flags default to True (worst case)."""
        # Return JSON that doesn't set these fields explicitly
        payload = json.dumps({
            "verdict": "REJECT",
            "confidence": 0.1,
            "reject_reasons": ["test"],
        })
        with patch(
            "core.reviewer.invoke_reviewer_via_pi_team",
            return_value=(0, payload, ""),
        ):
            out = review_fix(sample_error, sample_diff)
        assert out.could_lose_money is True
        assert out.could_mask_real_bug is True
        assert out.introduces_regression is True

    def test_approve_verdict_sets_positive_flags(self, sample_error, sample_diff):
        """An APPROVE response with all positive flags propagated correctly."""
        payload = _approved_json(
            verdict="APPROVE",
            confidence=0.9,
            addresses_root_cause=True,
            could_lose_money=False,
            could_mask_real_bug=False,
            introduces_regression=False,
        )
        with patch(
            "core.reviewer.invoke_reviewer_via_pi_team",
            return_value=(0, payload, ""),
        ):
            out = review_fix(sample_error, sample_diff)
        assert out.addresses_root_cause is True
        assert out.could_lose_money is False
        assert out.could_mask_real_bug is False
        assert out.introduces_regression is False

    def test_reject_reasons_propagated(self, sample_error, sample_diff):
        payload = json.dumps({
            "verdict": "REJECT",
            "confidence": 0.2,
            "reject_reasons": ["broadens except", "removes assertion"],
        })
        with patch(
            "core.reviewer.invoke_reviewer_via_pi_team",
            return_value=(0, payload, ""),
        ):
            out = review_fix(sample_error, sample_diff)
        assert "broadens except" in out.reject_reasons
        assert "removes assertion" in out.reject_reasons

    def test_duration_always_set(self, sample_error, sample_diff):
        """duration_seconds must be set even on failure paths."""
        with patch(
            "core.reviewer.invoke_reviewer_via_pi_team",
            side_effect=subprocess.TimeoutExpired(cmd="pi", timeout=300),
        ):
            out = review_fix(sample_error, sample_diff)
        assert out.duration_seconds >= 0.0

    def test_property_only_approve_on_threshold(self, sample_error, sample_diff):
        """Property: any 100 random verdict/confidence combos — only APPROVE/>=0.75 = APPROVE."""
        import random
        rng = random.Random(99)
        verdicts = ["APPROVE", "REJECT", "approve", "REJECT", "", None, "ESCALATE"]
        for _ in range(100):
            v = rng.choice(verdicts)
            c = rng.uniform(0.0, 1.0)
            payload = json.dumps({
                "verdict": v,
                "confidence": c,
                "reject_reasons": [],
            })
            with patch(
                "core.reviewer.invoke_reviewer_via_pi_team",
                return_value=(0, payload, ""),
            ):
                out = review_fix(sample_error, sample_diff)
            expected = (
                "APPROVE"
                if (v or "").upper() == "APPROVE" and c >= MIN_APPROVE_CONFIDENCE
                else "REJECT"
            )
            assert out.verdict == expected, (
                f"verdict={v!r} confidence={c:.3f}: expected {expected}, got {out.verdict}"
            )


# ── 4. invoke_reviewer_via_pi_team — subprocess shape ───────────────────────

class TestInvokeReviewerViaPiTeam:
    def test_includes_team_remediation_and_member(self):
        captured_cmd = []

        def fake_run(cmd, **kwargs):
            captured_cmd.extend(cmd)
            return MagicMock(returncode=0, stdout='{"verdict":"REJECT"}', stderr="")

        with patch("core.reviewer.subprocess.run", side_effect=fake_run):
            invoke_reviewer_via_pi_team("prompt", timeout_sec=10)

        assert "--team" in captured_cmd
        team_idx = captured_cmd.index("--team")
        assert captured_cmd[team_idx + 1] == "remediation"

        assert "--member" in captured_cmd
        member_idx = captured_cmd.index("--member")
        assert captured_cmd[member_idx + 1] == "Review Worker"

    def test_includes_no_tools_flag(self):
        """Reviewer is read-only — --no-tools must be present."""
        captured_cmd = []

        def fake_run(cmd, **kwargs):
            captured_cmd.extend(cmd)
            return MagicMock(returncode=0, stdout="", stderr="")

        with patch("core.reviewer.subprocess.run", side_effect=fake_run):
            invoke_reviewer_via_pi_team("prompt", timeout_sec=10)

        assert "--no-tools" in captured_cmd

    def test_includes_system_prompt(self):
        captured_cmd = []

        def fake_run(cmd, **kwargs):
            captured_cmd.extend(cmd)
            return MagicMock(returncode=0, stdout="", stderr="")

        with patch("core.reviewer.subprocess.run", side_effect=fake_run):
            invoke_reviewer_via_pi_team("prompt", timeout_sec=10)

        assert "--system-prompt" in captured_cmd
        sp_idx = captured_cmd.index("--system-prompt")
        assert "Claude" in captured_cmd[sp_idx + 1]

    def test_unsets_anthropic_api_key(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-secret")
        captured_env = {}

        def fake_run(cmd, **kwargs):
            captured_env.update(kwargs.get("env", {}))
            return MagicMock(returncode=0, stdout="", stderr="")

        with patch("core.reviewer.subprocess.run", side_effect=fake_run):
            invoke_reviewer_via_pi_team("prompt", timeout_sec=10)

        assert "ANTHROPIC_API_KEY" not in captured_env

    def test_uses_opus_model(self):
        """Reviewer must invoke Opus 4.7 (upgraded 2026-04-30 from Sonnet 4.6
        for parity with Fix Worker; decorrelation property now provided by
        separate process + adversarial prompt + default-REJECT, not model diff)."""
        captured_cmd = []

        def fake_run(cmd, **kwargs):
            captured_cmd.extend(cmd)
            return MagicMock(returncode=0, stdout="", stderr="")

        with patch("core.reviewer.subprocess.run", side_effect=fake_run):
            invoke_reviewer_via_pi_team("prompt", timeout_sec=10)

        assert "--model" in captured_cmd
        model_idx = captured_cmd.index("--model")
        assert captured_cmd[model_idx + 1] == "claude-opus-4-7"
