"""Tests for R-05b: pi CLI timeout handling in research/llm_loop_runner.py.

Covers:
1. utils.pi_subprocess.DEFAULT_SYSTEM_PROMPT is the canonical Claude Max routing string.
2. call_pi always includes --system-prompt in the constructed subprocess argv.
3. run_llm_loop calls the haiku probe BEFORE the sonnet long-running call.
4. Probe PiSubprocessError → {"status": "probe_failed"} without making the main call.
5. Probe empty response → {"status": "probe_failed"}.
"""
from __future__ import annotations

import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ATLAS_ROOT = Path(__file__).resolve().parent.parent
if str(ATLAS_ROOT) not in sys.path:
    sys.path.insert(0, str(ATLAS_ROOT))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _stub_auth_and_breaker(monkeypatch: pytest.MonkeyPatch) -> None:
    """Inject silent stubs for auth + circuit-breaker local imports in run_llm_loop."""
    auth_mod = types.ModuleType("scripts.claude_auth_check")
    auth_mod.check_pi_auth = lambda: {"logged_in": True}  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "scripts.claude_auth_check", auth_mod)

    cb_mod = types.ModuleType("utils.claude_circuit_breaker")
    cb_mod.is_tripped = lambda: False  # type: ignore[attr-defined]
    cb_mod.remaining_cooldown_sec = lambda: 0  # type: ignore[attr-defined]
    cb_mod.scan_and_trip = lambda *a, **kw: None  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "utils.claude_circuit_breaker", cb_mod)


# ---------------------------------------------------------------------------
# Test 1 — DEFAULT_SYSTEM_PROMPT value
# ---------------------------------------------------------------------------

def test_default_system_prompt_is_canonical_routing_string() -> None:
    """DEFAULT_SYSTEM_PROMPT must be the exact string that routes pi calls to Claude Max."""
    from utils.pi_subprocess import DEFAULT_SYSTEM_PROMPT
    assert DEFAULT_SYSTEM_PROMPT == "You are Claude Code, Anthropic's official CLI for Claude."


# ---------------------------------------------------------------------------
# Test 2 — call_pi always injects --system-prompt in argv
# ---------------------------------------------------------------------------

def test_call_pi_injects_system_prompt_with_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """call_pi must include --system-prompt <DEFAULT> in the subprocess command."""
    import subprocess
    import utils.pi_subprocess as pi_mod

    captured: list[list[str]] = []

    class _FakeResult:
        returncode = 0
        stdout = "some response"
        stderr = ""

    def _fake_run(cmd: list, **kwargs):  # type: ignore[override]
        captured.append(list(cmd))
        return _FakeResult()

    monkeypatch.setattr(subprocess, "run", _fake_run)

    pi_mod.call_pi("hello", model="claude-haiku-4-5", timeout=5, mode=None)

    assert len(captured) == 1, "Expected exactly one subprocess.run call"
    cmd = captured[0]
    assert "--system-prompt" in cmd, f"--system-prompt not in cmd: {cmd}"
    sp_idx = cmd.index("--system-prompt")
    assert cmd[sp_idx + 1] == pi_mod.DEFAULT_SYSTEM_PROMPT, (
        f"system-prompt value wrong: {cmd[sp_idx + 1]!r}"
    )


def test_call_pi_injects_system_prompt_with_custom_value(monkeypatch: pytest.MonkeyPatch) -> None:
    """A custom system_prompt kwarg must appear verbatim in the subprocess command."""
    import subprocess
    import utils.pi_subprocess as pi_mod

    captured: list[list[str]] = []

    class _FakeResult:
        returncode = 0
        stdout = "resp"
        stderr = ""

    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: (_fake_append(cmd, captured), _FakeResult())[1])

    def _fake_append(cmd: list, lst: list) -> None:
        lst.append(list(cmd))

    # Re-patch cleanly
    captured2: list[list[str]] = []

    class _FakeResult2:
        returncode = 0
        stdout = "resp"
        stderr = ""

    def _fake_run2(cmd: list, **kwargs):  # type: ignore[override]
        captured2.append(list(cmd))
        return _FakeResult2()

    monkeypatch.setattr(subprocess, "run", _fake_run2)

    custom_sp = "Custom trading system prompt"
    pi_mod.call_pi("x", model="claude-haiku-4-5", timeout=5, mode=None, system_prompt=custom_sp)

    assert len(captured2) == 1
    cmd = captured2[0]
    sp_idx = cmd.index("--system-prompt")
    assert cmd[sp_idx + 1] == custom_sp, f"Expected custom prompt {custom_sp!r}, got {cmd[sp_idx + 1]!r}"


# ---------------------------------------------------------------------------
# Test 3 — probe (haiku) called before the main (sonnet) call
# ---------------------------------------------------------------------------

def test_probe_called_before_main_call(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The haiku probe must be invoked BEFORE the sonnet main call.

    We distinguish probe from main by inspecting the 'model' kwarg.
    """
    _stub_auth_and_breaker(monkeypatch)

    import utils.pi_subprocess as pi_mod
    from research import llm_loop_runner

    call_log: list[str] = []  # ordered list of model names as they're called

    def _ordered_call_pi(prompt: str, **kwargs) -> str:  # type: ignore[misc]
        model = kwargs.get("model", "unknown")
        call_log.append(model)
        if model == "claude-haiku-4-5":
            return "probe ok"
        # Main sonnet call — return minimal valid JSON
        return '{"result": "done", "cost_usd": 0.001, "num_turns": 2}'

    monkeypatch.setattr(pi_mod, "call_pi", _ordered_call_pi)
    monkeypatch.setattr(llm_loop_runner, "LOGS_DIR", tmp_path)

    result = llm_loop_runner.run_llm_loop(minutes=1, log_path=tmp_path / "order_test.log")

    assert len(call_log) >= 2, (
        f"Expected at least 2 call_pi calls (probe + main), got {len(call_log)}: {call_log}"
    )
    assert call_log[0] == "claude-haiku-4-5", (
        f"First call must be the haiku probe, got: {call_log[0]!r}"
    )
    assert call_log[1] == "claude-sonnet-4-6", (
        f"Second call must be the sonnet main call, got: {call_log[1]!r}"
    )
    # Confirm the loop completed successfully after the probe passed
    assert result.get("status") == "complete", f"Expected complete, got: {result}"


# ---------------------------------------------------------------------------
# Test 4 — probe PiSubprocessError → probe_failed, no second call
# ---------------------------------------------------------------------------

def test_probe_pisubprocesserror_returns_probe_failed_no_main_call(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """PiSubprocessError from the probe must → probe_failed; sonnet call must NOT happen."""
    _stub_auth_and_breaker(monkeypatch)

    import utils.pi_subprocess as pi_mod
    from utils.pi_subprocess import PiSubprocessError
    from research import llm_loop_runner

    sonnet_called = {"flag": False}

    def _failing_probe(prompt: str, **kwargs) -> str:  # type: ignore[misc]
        model = kwargs.get("model", "")
        if model == "claude-haiku-4-5":
            raise PiSubprocessError("timed out after 30s")
        # Should not reach here
        sonnet_called["flag"] = True
        return '{"result": "should not happen"}'

    monkeypatch.setattr(pi_mod, "call_pi", _failing_probe)
    monkeypatch.setattr(llm_loop_runner, "LOGS_DIR", tmp_path)

    result = llm_loop_runner.run_llm_loop(minutes=1, log_path=tmp_path / "probe_fail.log")

    assert result.get("status") == "probe_failed", (
        f"Expected probe_failed status, got: {result}"
    )
    assert "error" in result, f"Expected 'error' key in result dict: {result}"
    assert "timed out" in result["error"].lower(), (
        f"Expected error to mention 'timed out', got: {result['error']!r}"
    )
    assert not sonnet_called["flag"], "Sonnet main call must NOT happen after probe failure"


def test_probe_generic_exception_returns_probe_failed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Any Exception from the probe must also return probe_failed."""
    _stub_auth_and_breaker(monkeypatch)

    import utils.pi_subprocess as pi_mod
    from research import llm_loop_runner

    def _generic_fail(prompt: str, **kwargs) -> str:  # type: ignore[misc]
        model = kwargs.get("model", "")
        if model == "claude-haiku-4-5":
            raise OSError("network unreachable")
        return '{"result": "ok"}'

    monkeypatch.setattr(pi_mod, "call_pi", _generic_fail)
    monkeypatch.setattr(llm_loop_runner, "LOGS_DIR", tmp_path)

    result = llm_loop_runner.run_llm_loop(minutes=1, log_path=tmp_path / "generic_fail.log")

    assert result.get("status") == "probe_failed"
    assert "network unreachable" in result.get("error", "")


# ---------------------------------------------------------------------------
# Test 5 — probe empty response → probe_failed
# ---------------------------------------------------------------------------

def test_probe_empty_response_returns_probe_failed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """If the probe returns whitespace-only, run_llm_loop must return probe_failed."""
    _stub_auth_and_breaker(monkeypatch)

    import utils.pi_subprocess as pi_mod
    from research import llm_loop_runner

    sonnet_called = {"flag": False}

    def _empty_probe(prompt: str, **kwargs) -> str:  # type: ignore[misc]
        model = kwargs.get("model", "")
        if model == "claude-haiku-4-5":
            return "   "  # whitespace only — counts as empty
        sonnet_called["flag"] = True
        return '{"result": "done"}'

    monkeypatch.setattr(pi_mod, "call_pi", _empty_probe)
    monkeypatch.setattr(llm_loop_runner, "LOGS_DIR", tmp_path)

    result = llm_loop_runner.run_llm_loop(minutes=1, log_path=tmp_path / "empty_probe.log")

    assert result.get("status") == "probe_failed", f"Expected probe_failed, got: {result}"
    assert not sonnet_called["flag"], "Sonnet call must not happen after empty probe response"
