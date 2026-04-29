"""Tests for core/fix_worker.py — full coverage via monkey-patching.

All subprocess.run calls, kill-switch, and OAuth checks are mocked so no
real pi, git, or filesystem operations occur during the test suite.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

from core.fix_worker import (
    FixOutcome,
    build_fix_prompt,
    capture_diff,
    create_worktree,
    invoke_fix_worker_via_pi_team,
    parse_fix_worker_output,
    remove_worktree,
    run_fix,
)


# ── Shared helpers ───────────────────────────────────────────────────────────

def _make_ks_module(return_value=None) -> SimpleNamespace:
    """Create a minimal kill-switch module substitute."""
    return SimpleNamespace(check_all_layers=MagicMock(return_value=return_value))


def _fake_worktree():
    return (Path("/tmp/atlas-fix-42-999999"), "auto-fix/err-42-deadbeef")


@pytest.fixture()
def sample_error() -> dict:
    return {
        "id": 42,
        "fingerprint": "deadbeef12345678",
        "occurrence_count": 3,
        "service": "atlas-dashboard",
        "level": "ERROR",
        "exc_type": "KeyError",
        "message": "missing key 'price' in response",
        "file_path": "tests/fake_module.py",
        "line_number": 99,
        "function_name": "fetch_price",
        "traceback": (
            "Traceback (most recent call last):\n"
            "  File tests/fake_module.py line 99\n"
            "KeyError: 'price'"
        ),
        "classification": "ASSIST",
    }


@pytest.fixture(autouse=True)
def _inject_ks(monkeypatch) -> SimpleNamespace:
    """Inject a default-clear kill-switch module so the lazy import in run_fix succeeds."""
    mod = _make_ks_module(return_value=None)
    monkeypatch.setitem(sys.modules, "core.remediation_kill_switch", mod)
    return mod


# ── 1. build_fix_prompt — NEVER preamble present ────────────────────────────

class TestBuildFixPrompt:
    def test_never_list_includes_brokers(self, sample_error):
        prompt = build_fix_prompt(sample_error, "ASSIST")
        assert "brokers/**" in prompt

    def test_never_list_includes_risk(self, sample_error):
        prompt = build_fix_prompt(sample_error, "ASSIST")
        assert "risk/**" in prompt

    def test_never_list_includes_kill_switch(self, sample_error):
        prompt = build_fix_prompt(sample_error, "ASSIST")
        assert "kill_switch" in prompt

    def test_never_list_includes_forbidden_keyword(self, sample_error):
        prompt = build_fix_prompt(sample_error, "ASSIST")
        assert "FORBIDDEN" in prompt

    def test_includes_error_fingerprint(self, sample_error):
        prompt = build_fix_prompt(sample_error, "ASSIST")
        assert sample_error["fingerprint"] in prompt

    def test_includes_error_message(self, sample_error):
        prompt = build_fix_prompt(sample_error, "ASSIST")
        assert "missing key 'price' in response" in prompt

    def test_includes_file_path(self, sample_error):
        prompt = build_fix_prompt(sample_error, "ASSIST")
        assert sample_error["file_path"] in prompt

    def test_includes_30_line_cap(self, sample_error):
        prompt = build_fix_prompt(sample_error, "ASSIST")
        assert "30 lines" in prompt or "30 line" in prompt

    def test_includes_escalate_instruction(self, sample_error):
        prompt = build_fix_prompt(sample_error, "ASSIST")
        assert "ESCALATE" in prompt

    def test_no_secrets_in_prompt(self, sample_error, monkeypatch):
        """Env API key values must not appear verbatim in the prompt."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-secret123")
        prompt = build_fix_prompt(sample_error, "ASSIST")
        assert "sk-ant-secret123" not in prompt

    def test_property_never_crashes_on_random_dicts(self):
        """Property: 100 random error dicts — build_fix_prompt never raises."""
        import random
        import string

        rng = random.Random(42)
        for _ in range(100):
            error = {
                "id": rng.randint(0, 10000),
                "fingerprint": "".join(rng.choices(string.hexdigits, k=16)),
                "occurrence_count": rng.randint(1, 500),
                "service": rng.choice([None, "atlas-dashboard", "atlas-bot", ""]),
                "level": rng.choice(["ERROR", "WARNING", "CRITICAL", None]),
                "exc_type": rng.choice(["KeyError", "ValueError", None, ""]),
                "message": rng.choice([
                    None, "", "something went wrong", "a" * 2000,
                    "<script>alert(1)</script>",
                ]),
                "file_path": rng.choice([
                    None, "tests/foo.py", "brokers/live_executor.py",
                ]),
                "line_number": rng.choice([None, 0, 99, 9999]),
                "function_name": rng.choice([None, "do_stuff", ""]),
                "traceback": rng.choice([None, "", "Traceback...\n  line 1"]),
            }
            prompt = build_fix_prompt(
                error, rng.choice(["ASSIST", "AUTO_FIX", "ESCALATE"])
            )
            assert isinstance(prompt, str)
            assert len(prompt) > 100


# ── 2. parse_fix_worker_output ───────────────────────────────────────────────

class TestParseFixWorkerOutput:
    def test_parses_proposed_json_string(self):
        payload = json.dumps({
            "status": "PROPOSED",
            "branch": "auto-fix/err-1-abc",
            "diff_lines": 5,
            "diagnosis": "root cause found",
            "fix_reasoning": "added null check",
        })
        result = parse_fix_worker_output(payload)
        assert result["status"] == "PROPOSED"
        assert result["branch"] == "auto-fix/err-1-abc"
        assert result["diff_lines"] == 5

    def test_garbage_input_returns_error(self):
        result = parse_fix_worker_output("this is not json at all")
        assert result.get("status") == "ERROR"

    def test_empty_string_returns_error(self):
        result = parse_fix_worker_output("")
        assert result.get("status") == "ERROR"
        assert "empty stdout" in result.get("reason", "")

    def test_parses_last_json_when_trailing_noise(self):
        """Worker often prints prose then JSON — must extract the LAST object."""
        stdout = (
            "Thinking... some prose output here.\n"
            '{"status": "IGNORE_ME", "branch": "wrong"}\n'
            "More prose.\n"
            '{"status": "PROPOSED", "branch": "auto-fix/err-42-dead", "diff_lines": 8}'
        )
        result = parse_fix_worker_output(stdout)
        assert result["status"] == "PROPOSED"
        assert result["branch"] == "auto-fix/err-42-dead"

    def test_escalate_status_parsed(self):
        payload = '{"status": "ESCALATE", "reason": "cannot reproduce"}'
        result = parse_fix_worker_output(payload)
        assert result["status"] == "ESCALATE"
        assert result["reason"] == "cannot reproduce"

    def test_invalid_json_braces_returns_error(self):
        result = parse_fix_worker_output("{not valid json{{")
        assert result.get("status") == "ERROR"


# ── 3. create_worktree ───────────────────────────────────────────────────────

class TestCreateWorktree:
    def test_calls_git_worktree_add(self):
        calls_seen = []

        def fake_run(cmd, **kwargs):
            calls_seen.append(list(cmd))
            r = MagicMock()
            r.returncode = 0
            r.stdout = r.stderr = ""
            return r

        with patch("core.fix_worker.subprocess.run", side_effect=fake_run):
            _worktree, branch = create_worktree(42, "deadbeef12345678")

        worktree_add = [c for c in calls_seen if "worktree" in c and "add" in c]
        assert len(worktree_add) >= 1
        cmd = worktree_add[0]
        assert "-b" in cmd
        assert branch in cmd

    def test_branch_name_contains_error_id(self):
        def fake_run(cmd, **kwargs):
            r = MagicMock()
            r.returncode = 0
            r.stdout = r.stderr = ""
            return r

        with patch("core.fix_worker.subprocess.run", side_effect=fake_run):
            _, branch = create_worktree(99, "cafebabe12345678")

        assert "99" in branch
        assert branch.startswith("auto-fix/err-")

    def test_raises_on_git_failure(self):
        def fake_run(cmd, **kwargs):
            r = MagicMock()
            if "worktree" in cmd and "add" in cmd:
                r.returncode = 1
                r.stderr = "fatal: branch already exists"
            else:
                r.returncode = 0
                r.stderr = ""
            r.stdout = ""
            return r

        with patch("core.fix_worker.subprocess.run", side_effect=fake_run):
            with pytest.raises(RuntimeError, match="git worktree add failed"):
                create_worktree(1, "abc12345")


# ── 4. remove_worktree ───────────────────────────────────────────────────────

class TestRemoveWorktree:
    def test_calls_worktree_remove_and_branch_delete(self, tmp_path):
        calls_seen = []

        def fake_run(cmd, **kwargs):
            calls_seen.append(list(cmd))
            return MagicMock(returncode=0)

        with patch("core.fix_worker.subprocess.run", side_effect=fake_run):
            remove_worktree(tmp_path, prune_branch="auto-fix/err-1-abc")

        flat = [" ".join(c) for c in calls_seen]
        assert any("worktree" in c and "remove" in c for c in flat)
        assert any(
            "branch" in c and "-D" in c and "auto-fix/err-1-abc" in c for c in flat
        )

    def test_no_branch_delete_when_prune_branch_none(self, tmp_path):
        calls_seen = []

        def fake_run(cmd, **kwargs):
            calls_seen.append(list(cmd))
            return MagicMock(returncode=0)

        with patch("core.fix_worker.subprocess.run", side_effect=fake_run):
            remove_worktree(tmp_path)

        flat = [" ".join(c) for c in calls_seen]
        assert not any("branch" in c and "-D" in c for c in flat)


# ── 5. capture_diff ──────────────────────────────────────────────────────────

class TestCaptureDiff:
    def test_counts_added_and_removed_lines(self, tmp_path):
        diff_text = (
            "--- a/foo.py\n"
            "+++ b/foo.py\n"
            "+added line 1\n"
            "+added line 2\n"
            "-removed line\n"
            " context\n"
        )
        mock_r = MagicMock()
        mock_r.stdout = diff_text

        with patch("core.fix_worker.subprocess.run", return_value=mock_r):
            _, n = capture_diff(tmp_path, "auto-fix/err-42-dead")

        assert n == 3  # 2 added + 1 removed

    def test_excludes_triple_plus_minus_headers(self, tmp_path):
        diff_text = (
            "--- a/foo.py\n"
            "+++ b/foo.py\n"
            "+real add\n"
            "-real remove\n"
        )
        mock_r = MagicMock()
        mock_r.stdout = diff_text

        with patch("core.fix_worker.subprocess.run", return_value=mock_r):
            _, n = capture_diff(tmp_path, "branch")

        assert n == 2

    def test_empty_diff_returns_zero(self, tmp_path):
        mock_r = MagicMock()
        mock_r.stdout = ""

        with patch("core.fix_worker.subprocess.run", return_value=mock_r):
            diff, n = capture_diff(tmp_path, "branch")

        assert n == 0
        assert diff == ""


# ── 6. run_fix orchestration ─────────────────────────────────────────────────

class TestRunFix:
    @pytest.fixture(autouse=True)
    def _patch_oauth(self):
        with patch("core.fix_worker.preflight_oauth", return_value=True):
            yield

    @pytest.fixture(autouse=True)
    def _patch_worktree(self):
        with patch("core.fix_worker.create_worktree", return_value=_fake_worktree()):
            yield

    @pytest.fixture(autouse=True)
    def _patch_remove(self):
        with patch("core.fix_worker.remove_worktree") as m:
            yield m

    def test_kill_switch_active_returns_failure(self, sample_error, _inject_ks, _patch_remove):
        block = SimpleNamespace(layer=1, reason="budget exceeded")
        _inject_ks.check_all_layers.return_value = block
        outcome = run_fix(sample_error, classification="ASSIST")
        assert outcome.success is False
        assert "kill-switch" in outcome.error
        assert "budget exceeded" in outcome.error

    def test_kill_switch_raises_surfaces_error(
        self, sample_error, monkeypatch, _patch_remove
    ):
        """If check_all_layers() raises, run_fix surfaces the error gracefully."""
        broken = SimpleNamespace(
            check_all_layers=MagicMock(side_effect=RuntimeError("simulated ks failure"))
        )
        monkeypatch.setitem(sys.modules, "core.remediation_kill_switch", broken)
        outcome = run_fix(sample_error, classification="ASSIST")
        assert outcome.success is False
        assert "kill-switch" in outcome.error

    def test_oauth_failure_returns_failure(self, sample_error):
        with patch("core.fix_worker.preflight_oauth", return_value=False):
            outcome = run_fix(sample_error, classification="ASSIST")
        assert outcome.success is False
        assert "OAuth" in outcome.error

    def test_dry_run_succeeds_without_pi(self, sample_error):
        with patch("core.fix_worker.invoke_fix_worker_via_pi_team") as mock_pi:
            outcome = run_fix(sample_error, dry_run=True)
        assert outcome.success is True
        assert outcome.error is None
        mock_pi.assert_not_called()

    def test_dry_run_skips_oauth_check(self, sample_error):
        with patch("core.fix_worker.preflight_oauth", return_value=False) as mock_oauth:
            outcome = run_fix(sample_error, dry_run=True)
        assert outcome.success is True
        mock_oauth.assert_not_called()

    def test_proposed_status_returns_success(self, sample_error):
        pi_payload = json.dumps({
            "status": "PROPOSED",
            "branch": "auto-fix/err-42-dead",
            "diff_lines": 7,
            "diagnosis": "null check missing",
            "fix_reasoning": "added guard",
        })
        with (
            patch(
                "core.fix_worker.invoke_fix_worker_via_pi_team",
                return_value=(0, pi_payload, ""),
            ),
            patch("core.fix_worker.capture_diff", return_value=("+ added\n", 1)),
        ):
            outcome = run_fix(sample_error)
        assert outcome.success is True
        assert outcome.diagnosis == "null check missing"
        assert outcome.fix_reasoning == "added guard"

    def test_escalate_status_returns_failure(self, sample_error, _patch_remove):
        pi_payload = json.dumps({"status": "ESCALATE", "reason": "cannot reproduce"})
        with patch(
            "core.fix_worker.invoke_fix_worker_via_pi_team",
            return_value=(0, pi_payload, ""),
        ):
            outcome = run_fix(sample_error)
        assert outcome.success is False
        assert "ESCALATED" in outcome.error
        assert "cannot reproduce" in outcome.error

    def test_pi_timeout_returns_failure(self, sample_error, _patch_remove):
        with patch(
            "core.fix_worker.invoke_fix_worker_via_pi_team",
            side_effect=subprocess.TimeoutExpired(cmd="pi", timeout=600),
        ):
            outcome = run_fix(sample_error)
        assert outcome.success is False
        assert "timeout" in outcome.error

    def test_pi_nonzero_exit_returns_failure(self, sample_error, _patch_remove):
        with patch(
            "core.fix_worker.invoke_fix_worker_via_pi_team",
            return_value=(1, "", "pi crashed"),
        ):
            outcome = run_fix(sample_error)
        assert outcome.success is False
        assert "pi exit 1" in outcome.error

    def test_worktree_removed_on_failure(self, sample_error, _patch_remove):
        with patch(
            "core.fix_worker.invoke_fix_worker_via_pi_team",
            return_value=(1, "", "error"),
        ):
            run_fix(sample_error)
        _patch_remove.assert_called_once()

    def test_worktree_not_removed_on_success(self, sample_error, _patch_remove):
        pi_payload = json.dumps({
            "status": "PROPOSED",
            "diff_lines": 3,
            "diagnosis": "d",
            "fix_reasoning": "r",
        })
        with (
            patch(
                "core.fix_worker.invoke_fix_worker_via_pi_team",
                return_value=(0, pi_payload, ""),
            ),
            patch("core.fix_worker.capture_diff", return_value=("+ line\n", 1)),
        ):
            outcome = run_fix(sample_error)
        assert outcome.success is True
        _patch_remove.assert_not_called()


# ── 7. invoke_fix_worker_via_pi_team — subprocess shape ─────────────────────

class TestInvokeFixWorkerViaPiTeam:
    def _run_and_capture(self, tmp_path):
        captured_cmd = []
        captured_env = {}

        def fake_run(cmd, **kwargs):
            captured_cmd.extend(cmd)
            captured_env.update(kwargs.get("env", {}))
            return MagicMock(returncode=0, stdout="", stderr="")

        with patch("core.fix_worker.subprocess.run", side_effect=fake_run):
            invoke_fix_worker_via_pi_team("prompt", tmp_path, timeout_sec=10)

        return captured_cmd, captured_env

    def test_includes_team_remediation(self, tmp_path):
        cmd, _ = self._run_and_capture(tmp_path)
        assert "--team" in cmd
        assert cmd[cmd.index("--team") + 1] == "remediation"

    def test_includes_system_prompt_for_oauth(self, tmp_path):
        cmd, _ = self._run_and_capture(tmp_path)
        assert "--system-prompt" in cmd
        sp_val = cmd[cmd.index("--system-prompt") + 1]
        assert "Claude" in sp_val

    def test_unsets_anthropic_api_key(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-should-be-removed")
        _, env = self._run_and_capture(tmp_path)
        assert "ANTHROPIC_API_KEY" not in env

    def test_does_not_use_raw_pi_without_team(self, tmp_path):
        """--team remediation must always be present (raw pi -p is FORBIDDEN)."""
        cmd, _ = self._run_and_capture(tmp_path)
        assert "--team" in cmd
        assert cmd[cmd.index("--team") + 1] == "remediation"

    def test_includes_member_fix_worker(self, tmp_path):
        cmd, _ = self._run_and_capture(tmp_path)
        assert "--member" in cmd
        assert cmd[cmd.index("--member") + 1] == "Fix Worker"
