"""Tests for core/merge_gates.py — 15 merge gate functions.

Uses subprocess mocking throughout; no live git or pytest invocations.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch, call

import pytest

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

import core.merge_gates as mg
from core.merge_gates import (
    GateResult,
    GateRunOutcome,
    _function_nodes,
    _load_blocked_funcs,
    _load_deny_globs,
    gate_diff_size_cap,
    gate_mypy_clean,
    gate_no_new_bare_except,
    gate_no_never_list_touched,
    gate_no_safety_critical_function_modified,
    gate_no_warning_demotion,
    gate_pi_system_prompt_lint,
    gate_post_merge_no_fingerprint_recurrence,
    gate_post_merge_no_healthcheck_regression,
    gate_pre_commit_hooks,
    gate_regression_test_present,
    gate_reviewer_approved,
    gate_targeted_tests,
    run_all_gates,
)


# ── Fixtures ──────────────────────────────────────────────────────────

@pytest.fixture
def tmp_worktree(tmp_path: Path) -> Path:
    """A minimal fake worktree directory."""
    (tmp_path / "tests").mkdir()
    (tmp_path / "scripts").mkdir()
    return tmp_path


@pytest.fixture
def deny_yaml(tmp_path: Path) -> Path:
    """Write a minimal auto_fix_deny.yaml next to its expected location."""
    cfg = tmp_path / "config" / "auto_fix_deny.yaml"
    cfg.parent.mkdir(parents=True)
    cfg.write_text(
        "file_globs:\n"
        "  - \"brokers/**\"\n"
        "  - \"scripts/eod_settlement.py\"\n"
        "  - \"**/*.sql\"\n"
    )
    return cfg


@pytest.fixture
def funcs_txt(tmp_path: Path) -> Path:
    """Write a minimal safety_critical_functions.txt."""
    f = tmp_path / "config" / "safety_critical_functions.txt"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("place_order\nhalt\ncheck_kill_switch\n")
    return f


# ── GateResult dataclass ──────────────────────────────────────────────

class TestGateResultDataclass:
    def test_defaults(self):
        gr = GateResult("foo", True)
        assert gr.name == "foo"
        assert gr.passed is True
        assert gr.detail == {}
        assert gr.severity == "BLOCKING"

    def test_warning_severity(self):
        gr = GateResult("bar", False, {"k": "v"}, severity="WARNING")
        assert gr.severity == "WARNING"

    def test_detail_populated(self):
        gr = GateResult("baz", False, {"exit": 1, "tail": "err"})
        assert gr.detail["exit"] == 1


class TestGateRunOutcomeDataclass:
    def test_all_passed_true(self):
        results = [GateResult("g1", True), GateResult("g2", True)]
        out = GateRunOutcome(
            all_passed=True,
            results=results,
            summary={"passed": ["g1", "g2"], "failed": [], "blocking_failures": []},
        )
        assert out.all_passed is True
        assert len(out.results) == 2

    def test_blocking_failure_tracked(self):
        out = GateRunOutcome(
            all_passed=False,
            results=[],
            summary={"passed": [], "failed": ["diff_size_cap"], "blocking_failures": ["diff_size_cap"]},
        )
        assert "diff_size_cap" in out.summary["blocking_failures"]


# ── Gate 1: targeted tests ────────────────────────────────────────────

class TestGateTargetedTests:
    def test_pass(self, tmp_worktree: Path):
        with patch("core.merge_gates.subprocess.run") as mock_run:
            mock_run.return_value = Mock(returncode=0, stdout="3 passed\n", stderr="")
            result = gate_targeted_tests(tmp_worktree, test_paths=["tests/foo.py"])
        assert result.passed is True
        assert result.name == "targeted_tests"
        assert result.severity == "BLOCKING"

    def test_fail_returncode(self, tmp_worktree: Path):
        with patch("core.merge_gates.subprocess.run") as mock_run:
            mock_run.return_value = Mock(returncode=1, stdout="1 failed\n", stderr="")
            result = gate_targeted_tests(tmp_worktree, test_paths=["tests/foo.py"])
        assert result.passed is False
        assert "tail" in result.detail

    def test_timeout(self, tmp_worktree: Path):
        with patch("core.merge_gates.subprocess.run") as mock_run:
            import subprocess
            mock_run.side_effect = subprocess.TimeoutExpired(cmd="pytest", timeout=300)
            result = gate_targeted_tests(tmp_worktree)
        assert result.passed is False
        assert result.detail["error"] == "timeout"

    def test_default_test_paths(self, tmp_worktree: Path):
        """Default test_paths=None → runs tests/ directory."""
        with patch("core.merge_gates.subprocess.run") as mock_run:
            mock_run.return_value = Mock(returncode=0, stdout="", stderr="")
            gate_targeted_tests(tmp_worktree)
        cmd = mock_run.call_args[0][0]
        assert "tests/" in cmd


# ── Gate 2: regression test present ──────────────────────────────────

class TestGateRegressionTestPresent:
    def _mock_git(self, files_out: str, diff_out: str):
        """Return a side_effect list for two subprocess.run calls."""
        return [
            Mock(returncode=0, stdout=files_out, stderr=""),
            Mock(returncode=0, stdout=diff_out, stderr=""),
        ]

    def test_new_test_file_added(self, tmp_worktree: Path):
        with patch("core.merge_gates.subprocess.run") as mock_run:
            mock_run.side_effect = self._mock_git(
                "tests/test_new_feature.py\n",
                "+def test_something():\n    pass\n",
            )
            result = gate_regression_test_present(tmp_worktree, "fix-branch")
        assert result.passed is True
        assert "tests/test_new_feature.py" in result.detail["test_files_modified"]

    def test_new_test_function_in_existing_file(self, tmp_worktree: Path):
        with patch("core.merge_gates.subprocess.run") as mock_run:
            mock_run.side_effect = self._mock_git(
                "utils/helper.py\n",
                "+def test_helper_edge_case():\n    assert True\n",
            )
            result = gate_regression_test_present(tmp_worktree, "fix-branch")
        assert result.passed is True
        assert result.detail["new_test_functions_added"] >= 1

    def test_no_test_added_fails(self, tmp_worktree: Path):
        with patch("core.merge_gates.subprocess.run") as mock_run:
            mock_run.side_effect = self._mock_git(
                "utils/helper.py\n",
                "+def helper_func():\n    pass\n",
            )
            result = gate_regression_test_present(tmp_worktree, "fix-branch")
        assert result.passed is False
        assert result.detail["new_test_functions_added"] == 0

    def test_multiple_test_funcs_counted(self, tmp_worktree: Path):
        with patch("core.merge_gates.subprocess.run") as mock_run:
            diff = "+def test_a():\n    pass\n+def test_b():\n    pass\n"
            mock_run.side_effect = self._mock_git("", diff)
            result = gate_regression_test_present(tmp_worktree, "fix-branch")
        assert result.detail["new_test_functions_added"] == 2


# ── Gate 7: diff size cap ─────────────────────────────────────────────

class TestGateDiffSizeCap:
    def _make_diff(self, added: int, removed: int) -> str:
        lines = ["--- a/foo.py", "+++ b/foo.py"]
        lines += [f"+line{i}" for i in range(added)]
        lines += [f"-line{i}" for i in range(removed)]
        return "\n".join(lines)

    def test_within_limit(self, tmp_worktree: Path):
        diff = self._make_diff(10, 5)  # total=15, limit=30
        with patch("core.merge_gates.subprocess.run") as mock_run:
            mock_run.return_value = Mock(returncode=0, stdout=diff, stderr="")
            result = gate_diff_size_cap(tmp_worktree, "fix-branch", max_lines=30)
        assert result.passed is True
        assert result.detail["total"] == 15

    def test_over_limit(self, tmp_worktree: Path):
        diff = self._make_diff(20, 15)  # total=35 > limit=30
        with patch("core.merge_gates.subprocess.run") as mock_run:
            mock_run.return_value = Mock(returncode=0, stdout=diff, stderr="")
            result = gate_diff_size_cap(tmp_worktree, "fix-branch", max_lines=30)
        assert result.passed is False
        assert result.detail["total"] == 35

    def test_exactly_at_limit(self, tmp_worktree: Path):
        diff = self._make_diff(15, 15)  # total=30 == limit=30
        with patch("core.merge_gates.subprocess.run") as mock_run:
            mock_run.return_value = Mock(returncode=0, stdout=diff, stderr="")
            result = gate_diff_size_cap(tmp_worktree, "fix-branch", max_lines=30)
        assert result.passed is True  # ≤ 30

    def test_custom_max_lines(self, tmp_worktree: Path):
        diff = self._make_diff(5, 5)  # total=10
        with patch("core.merge_gates.subprocess.run") as mock_run:
            mock_run.return_value = Mock(returncode=0, stdout=diff, stderr="")
            result = gate_diff_size_cap(tmp_worktree, "fix-branch", max_lines=8)
        assert result.passed is False
        assert result.detail["max_lines"] == 8

    def test_hunk_headers_not_counted(self, tmp_worktree: Path):
        diff = "--- a/foo.py\n+++ b/foo.py\n+line1\n-line2\n"
        with patch("core.merge_gates.subprocess.run") as mock_run:
            mock_run.return_value = Mock(returncode=0, stdout=diff, stderr="")
            result = gate_diff_size_cap(tmp_worktree, "fix-branch", max_lines=30)
        # +++ and --- lines should not be counted
        assert result.detail["added"] == 1
        assert result.detail["removed"] == 1


# ── Gate 8: never-list ────────────────────────────────────────────────

class TestGateNoNeverListTouched:
    def test_clean_diff_passes(self, tmp_worktree: Path, tmp_path: Path, deny_yaml: Path):
        orig_deny = mg.DENY_YAML
        mg.DENY_YAML = deny_yaml
        try:
            with patch("core.merge_gates.subprocess.run") as mock_run:
                mock_run.return_value = Mock(returncode=0, stdout="utils/helper.py\n", stderr="")
                result = gate_no_never_list_touched(tmp_worktree, "fix-branch")
            assert result.passed is True
            assert result.detail["violations"] == []
        finally:
            mg.DENY_YAML = orig_deny

    def test_never_list_glob_match(self, tmp_worktree: Path, deny_yaml: Path):
        orig_deny = mg.DENY_YAML
        mg.DENY_YAML = deny_yaml
        try:
            with patch("core.merge_gates.subprocess.run") as mock_run:
                mock_run.return_value = Mock(
                    returncode=0, stdout="brokers/live_executor.py\n", stderr="")
                result = gate_no_never_list_touched(tmp_worktree, "fix-branch")
            assert result.passed is False
            assert len(result.detail["violations"]) == 1
            assert result.detail["violations"][0][0] == "brokers/live_executor.py"
        finally:
            mg.DENY_YAML = orig_deny

    def test_exact_glob_match(self, tmp_worktree: Path, deny_yaml: Path):
        orig_deny = mg.DENY_YAML
        mg.DENY_YAML = deny_yaml
        try:
            with patch("core.merge_gates.subprocess.run") as mock_run:
                mock_run.return_value = Mock(
                    returncode=0, stdout="scripts/eod_settlement.py\n", stderr="")
                result = gate_no_never_list_touched(tmp_worktree, "fix-branch")
            assert result.passed is False
        finally:
            mg.DENY_YAML = orig_deny

    def test_no_deny_yaml_passes(self, tmp_worktree: Path, tmp_path: Path):
        orig_deny = mg.DENY_YAML
        mg.DENY_YAML = tmp_path / "nonexistent.yaml"
        try:
            with patch("core.merge_gates.subprocess.run") as mock_run:
                mock_run.return_value = Mock(
                    returncode=0, stdout="brokers/anything.py\n", stderr="")
                result = gate_no_never_list_touched(tmp_worktree, "fix-branch")
            assert result.passed is True  # no globs loaded → no violations
        finally:
            mg.DENY_YAML = orig_deny

    def test_sql_glob_matches_sql_file(self, tmp_worktree: Path, deny_yaml: Path):
        orig_deny = mg.DENY_YAML
        mg.DENY_YAML = deny_yaml
        try:
            with patch("core.merge_gates.subprocess.run") as mock_run:
                mock_run.return_value = Mock(
                    returncode=0, stdout="db/schema.sql\n", stderr="")
                result = gate_no_never_list_touched(tmp_worktree, "fix-branch")
            assert result.passed is False
        finally:
            mg.DENY_YAML = orig_deny


# ── Gate 9: safety-critical functions ────────────────────────────────

class TestGateNoSafetyCriticalFunctionModified:
    def test_no_blocked_funcs_config(self, tmp_worktree: Path, tmp_path: Path):
        orig = mg.FUNCS_FILE
        mg.FUNCS_FILE = tmp_path / "nonexistent.txt"
        try:
            with patch("core.merge_gates.subprocess.run") as mock_run:
                mock_run.return_value = Mock(returncode=0, stdout="utils/helper.py\n", stderr="")
                result = gate_no_safety_critical_function_modified(tmp_worktree, "fix-branch")
            assert result.passed is True
            assert result.severity == "WARNING"
        finally:
            mg.FUNCS_FILE = orig

    def test_unmodified_safe_function_passes(self, tmp_worktree: Path, funcs_txt: Path, tmp_path: Path):
        """A blocked function that is unchanged (same AST) passes."""
        orig = mg.FUNCS_FILE
        mg.FUNCS_FILE = funcs_txt
        # create a fake .py file in worktree
        py_file = tmp_worktree / "utils" / "helper.py"
        py_file.parent.mkdir(exist_ok=True)
        src = "def safe_function():\n    return 1\n"
        py_file.write_text(src)
        try:
            with patch("core.merge_gates.subprocess.run") as mock_run:
                # files call returns our helper.py
                # git show main:... returns the SAME source (unchanged)
                mock_run.side_effect = [
                    Mock(returncode=0, stdout="utils/helper.py\n", stderr=""),
                    Mock(returncode=0, stdout=src, stderr=""),
                ]
                result = gate_no_safety_critical_function_modified(tmp_worktree, "fix-branch")
            assert result.passed is True
            assert result.detail["violations"] == []
        finally:
            mg.FUNCS_FILE = orig

    def test_blocked_function_modified_fails(self, tmp_worktree: Path, funcs_txt: Path):
        """Modifying 'halt' (in blocked list) must fail the gate."""
        orig = mg.FUNCS_FILE
        mg.FUNCS_FILE = funcs_txt
        py_file = tmp_worktree / "utils" / "helper.py"
        py_file.parent.mkdir(exist_ok=True)
        new_src = "def halt():\n    return \'new_impl\'\n"
        py_file.write_text(new_src)
        old_src = "def halt():\n    return \'old_impl\'\n"
        try:
            with patch("core.merge_gates.subprocess.run") as mock_run:
                mock_run.side_effect = [
                    Mock(returncode=0, stdout="utils/helper.py\n", stderr=""),
                    Mock(returncode=0, stdout=old_src, stderr=""),
                ]
                result = gate_no_safety_critical_function_modified(tmp_worktree, "fix-branch")
            assert result.passed is False
            violations = result.detail["violations"]
            assert any(v.get("function") == "halt" for v in violations)
        finally:
            mg.FUNCS_FILE = orig

    def test_non_py_files_skipped(self, tmp_worktree: Path, funcs_txt: Path):
        orig = mg.FUNCS_FILE
        mg.FUNCS_FILE = funcs_txt
        try:
            with patch("core.merge_gates.subprocess.run") as mock_run:
                mock_run.return_value = Mock(returncode=0, stdout="README.md\n", stderr="")
                result = gate_no_safety_critical_function_modified(tmp_worktree, "fix-branch")
            assert result.passed is True
            assert result.detail["violations"] == []
        finally:
            mg.FUNCS_FILE = orig


# ── Gate 10: reviewer ─────────────────────────────────────────────────

class TestGateReviewerApproved:
    @dataclass
    class FakeReviewer:
        success: bool
        verdict: str
        confidence: float
        addresses_root_cause: bool = True
        could_lose_money: bool | None = None
        reject_reasons: list | None = None

    def test_none_outcome_fails(self):
        result = gate_reviewer_approved(None)
        assert result.passed is False
        assert "no reviewer run" in result.detail["error"]

    def test_approve_verdict_passes(self):
        rv = self.FakeReviewer(success=True, verdict="APPROVE", confidence=0.9)
        result = gate_reviewer_approved(rv)
        assert result.passed is True
        assert result.detail["verdict"] == "APPROVE"
        assert result.detail["confidence"] == 0.9

    def test_reject_verdict_fails(self):
        rv = self.FakeReviewer(success=True, verdict="REJECT", confidence=0.85,
                               reject_reasons=["modifies trading path"])
        result = gate_reviewer_approved(rv)
        assert result.passed is False
        assert result.detail["verdict"] == "REJECT"
        assert result.detail["reject_reasons"] == ["modifies trading path"]

    def test_success_false_with_approve_verdict_fails(self):
        """success=False means reviewer errored — gate should fail."""
        rv = self.FakeReviewer(success=False, verdict="APPROVE", confidence=0.0)
        result = gate_reviewer_approved(rv)
        assert result.passed is False

    def test_could_lose_money_propagated(self):
        rv = self.FakeReviewer(success=True, verdict="APPROVE", confidence=0.7,
                               could_lose_money=False)
        result = gate_reviewer_approved(rv)
        assert result.detail["could_lose_money"] is False


# ── Gate 13: warning demotion ─────────────────────────────────────────

class TestGateNoWarningDemotion:
    def test_clean_diff_passes(self, tmp_worktree: Path):
        diff = "+logger.info(\"everything fine\")\n"
        with patch("core.merge_gates.subprocess.run") as mock_run:
            mock_run.return_value = Mock(returncode=0, stdout=diff, stderr="")
            result = gate_no_warning_demotion(tmp_worktree, "fix-branch")
        assert result.passed is True
        assert result.detail["suspicious_lines"] == 0

    def test_suspicious_warning_with_error_text_flagged(self, tmp_worktree: Path):
        diff = '+logger.warning("critical error occurred — ignoring")\n'
        with patch("core.merge_gates.subprocess.run") as mock_run:
            mock_run.return_value = Mock(returncode=0, stdout=diff, stderr="")
            result = gate_no_warning_demotion(tmp_worktree, "fix-branch")
        assert result.passed is False
        assert result.detail["suspicious_lines"] >= 1

    def test_severity_is_warning_not_blocking(self, tmp_worktree: Path):
        """Gate 13 is informational — should be WARNING severity."""
        with patch("core.merge_gates.subprocess.run") as mock_run:
            mock_run.return_value = Mock(returncode=0, stdout="", stderr="")
            result = gate_no_warning_demotion(tmp_worktree, "fix-branch")
        assert result.severity == "WARNING"


# ── Gate 14: mypy ─────────────────────────────────────────────────────

class TestGateMypyClean:
    def test_no_py_files_passes(self, tmp_worktree: Path):
        with patch("core.merge_gates.subprocess.run") as mock_run:
            mock_run.return_value = Mock(returncode=0, stdout="README.md\n", stderr="")
            result = gate_mypy_clean(tmp_worktree, "fix-branch")
        # files list will be empty since README.md doesn't end in .py
        assert result.passed is True
        assert result.detail.get("note") == "no .py files modified"

    def test_mypy_unavailable_passes_with_warning(self, tmp_worktree: Path):
        import subprocess as sp
        with patch("core.merge_gates.subprocess.run") as mock_run:
            mock_run.side_effect = [
                Mock(returncode=0, stdout="utils/foo.py\n", stderr=""),  # git diff files
                FileNotFoundError("mypy not found"),
            ]
            result = gate_mypy_clean(tmp_worktree, "fix-branch")
        assert result.passed is True
        assert result.severity == "WARNING"


# ── Gate 15: pre-commit hooks ─────────────────────────────────────────

class TestGatePreCommitHooks:
    def test_no_precommit_config_passes_with_warning(self, tmp_worktree: Path):
        result = gate_pre_commit_hooks(tmp_worktree)
        assert result.passed is True
        assert result.severity == "WARNING"

    def test_precommit_config_present_passes(self, tmp_worktree: Path):
        (tmp_worktree / ".pre-commit-config.yaml").write_text("repos:\n")
        result = gate_pre_commit_hooks(tmp_worktree)
        assert result.passed is True


# ── _function_nodes helper ────────────────────────────────────────────

class TestFunctionNodes:
    def test_basic_function(self):
        src = "def foo():\n    return 1\n"
        nodes = _function_nodes(src)
        assert "foo" in nodes

    def test_nested_functions(self):
        src = "def outer():\n    def inner():\n        pass\n    return inner\n"
        nodes = _function_nodes(src)
        assert "outer" in nodes
        assert "inner" in nodes

    def test_async_function(self):
        src = "async def async_handler():\n    pass\n"
        nodes = _function_nodes(src)
        assert "async_handler" in nodes

    def test_empty_src(self):
        assert _function_nodes("") == {}

    def test_invalid_syntax(self):
        assert _function_nodes("def broken(:\n    pass") == {}

    def test_multiple_functions(self):
        src = "def a():\n    pass\ndef b():\n    pass\n"
        nodes = _function_nodes(src)
        assert "a" in nodes
        assert "b" in nodes
        assert len(nodes) == 2


# ── Stub gates 11/12 ─────────────────────────────────────────────────

class TestStubGates:
    def test_healthcheck_stub_passes(self):
        result = gate_post_merge_no_healthcheck_regression()
        assert result.passed is True
        assert result.severity == "WARNING"
        assert "stub" in result.detail["note"]

    def test_fingerprint_stub_passes(self):
        result = gate_post_merge_no_fingerprint_recurrence()
        assert result.passed is True
        assert result.severity == "WARNING"


# ── run_all_gates integration ─────────────────────────────────────────

class TestRunAllGates:
    def test_returns_exactly_15_results(self, tmp_worktree: Path):
        """run_all_gates must always return exactly 15 GateResult objects."""
        @dataclass
        class FakeReviewer:
            success: bool = True
            verdict: str = "APPROVE"
            confidence: float = 0.95

        with patch("core.merge_gates.subprocess.run") as mock_run:
            # All subprocess calls succeed (targeted tests, git diff calls, etc.)
            mock_run.return_value = Mock(returncode=0, stdout="", stderr="")
            outcome = run_all_gates(
                tmp_worktree, "fix-branch",
                reviewer_outcome=FakeReviewer(),
            )
        assert len(outcome.results) == 15

    def test_all_passed_true_when_no_blocking_failures(self, tmp_worktree: Path):
        @dataclass
        class FakeReviewer:
            success: bool = True
            verdict: str = "APPROVE"
            confidence: float = 0.9

        # Smart mock: --name-only returns a non-.py file so gate 9 (AST check)
        # skips it entirely; full diff contains a +def test_ line so gate 2 passes.
        def smart_git(cmd, **kwargs):
            cmd_list = list(cmd)
            if "--name-only" in cmd_list:
                # Non-Python file: gate 9 skips .py-only check; gate 8 uses real DENY list
                return Mock(returncode=0, stdout="docs/changelog.md\n", stderr="")
            if "diff" in cmd_list:
                # New test function satisfies gate 2 regression_test_present
                return Mock(returncode=0, stdout="+def test_new_case():\n    pass\n", stderr="")
            return Mock(returncode=0, stdout="", stderr="")

        with patch("core.merge_gates.subprocess.run", side_effect=smart_git):
            outcome = run_all_gates(
                tmp_worktree, "fix-branch",
                reviewer_outcome=FakeReviewer(),
            )
        # Even with WARNING-severity failures, all_passed should be True
        assert outcome.all_passed is True
        assert outcome.summary["blocking_failures"] == []

    def test_blocking_failure_sets_all_passed_false(self, tmp_worktree: Path):
        """A BLOCKING gate failure → all_passed=False."""
        with patch("core.merge_gates.subprocess.run") as mock_run:
            # targeted_tests fails (returncode=1)
            mock_run.return_value = Mock(returncode=1, stdout="1 failed\n", stderr="")
            outcome = run_all_gates(
                tmp_worktree, "fix-branch",
                reviewer_outcome=None,  # gate 10 will also fail (BLOCKING)
            )
        assert outcome.all_passed is False
        assert len(outcome.summary["blocking_failures"]) >= 1

    def test_summary_keys_present(self, tmp_worktree: Path):
        with patch("core.merge_gates.subprocess.run") as mock_run:
            mock_run.return_value = Mock(returncode=0, stdout="", stderr="")
            outcome = run_all_gates(tmp_worktree, "fix-branch")
        assert "passed" in outcome.summary
        assert "failed" in outcome.summary
        assert "blocking_failures" in outcome.summary

    def test_full_suite_skipped_by_default(self, tmp_worktree: Path):
        """run_full_suite=False → full_suite gate should report skipped (WARNING)."""
        with patch("core.merge_gates.subprocess.run") as mock_run:
            mock_run.return_value = Mock(returncode=0, stdout="", stderr="")
            outcome = run_all_gates(tmp_worktree, "fix-branch", run_full_suite=False)
        full_suite_result = next(r for r in outcome.results if r.name == "full_suite")
        assert full_suite_result.severity == "WARNING"
        assert "skipped" in full_suite_result.detail.get("note", "")

    def test_warning_only_gates_not_in_blocking_failures(self, tmp_worktree: Path):
        """Gates with severity=WARNING that fail must NOT appear in blocking_failures."""
        with patch("core.merge_gates.subprocess.run") as mock_run:
            mock_run.return_value = Mock(returncode=0, stdout="", stderr="")
            outcome = run_all_gates(tmp_worktree, "fix-branch", reviewer_outcome=None)
        # reviewer_approved fails with BLOCKING; check it IS in blocking_failures
        assert "reviewer_approved" in outcome.summary["blocking_failures"]
        # full_suite (WARNING, skipped) must NOT be in blocking_failures
        assert "full_suite" not in outcome.summary["blocking_failures"]
