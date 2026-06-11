"""Shared helper for invoking the `pi` CLI as a subprocess.

All calls automatically pass the required --system-prompt flag to route
through the Claude Max OAuth subscription at $0 marginal cost. Without
that flag, pi falls back to pay-per-token "extra usage" billing.

Usage:
    from atlas.kernel.pi_subprocess import call_pi, call_pi_structured, call_pi_exec, call_pi_vision

    # Capture output (default — JSON mode)
    raw = call_pi("Summarise this: ...", model="claude-opus-4-8")
    data = call_pi_structured("Extract JSON: ...")

    # Streaming — output goes directly to terminal, prompt passed as positional arg
    exit_code = call_pi_exec("Do research on X", extra_args=["--skill", SKILL_DIR])

    # Vision — attach chart images via @path references (Claude Opus 4.7)
    from pathlib import Path
    raw = call_pi_vision(
        "Analyse this SPY chart...",
        [Path("/tmp/spy_daily.png")],
        model="claude-opus-4-8",
    )
"""
from __future__ import annotations

import json
from pathlib import Path
import logging
import subprocess
from typing import Optional

logger = logging.getLogger(__name__)

DEFAULT_SYSTEM_PROMPT = "You are Claude Code, Anthropic's official CLI for Claude."
DEFAULT_MODEL = "claude-opus-4-8"
DEFAULT_TIMEOUT = 1800


class PiSubprocessError(RuntimeError):
    """Raised when the pi CLI fails or returns an error in stdout."""


def call_pi(
    prompt: str,
    model: str = DEFAULT_MODEL,
    timeout: int = DEFAULT_TIMEOUT,
    mode: Optional[str] = "json",
    system_prompt: str = DEFAULT_SYSTEM_PROMPT,
    extra_args: Optional[list[str]] = None,
    pi_bin: str = "pi",
    cwd: Optional[str] = None,
    env: Optional[dict] = None,
) -> str:
    """Invoke `pi -p` with the Claude Max routing flag. Returns raw stdout.

    Raises PiSubprocessError on non-zero exit, "out of extra usage" errors,
    timeout, or FileNotFoundError (pi not on PATH).

    Parameters
    ----------
    prompt:
        Text to send to pi via stdin.
    model:
        Claude model to use (passed as --model).
    timeout:
        Subprocess timeout in seconds.
    mode:
        Value for --mode flag (e.g. ``"json"``, ``"text"``).
        Pass ``None`` or ``""`` to omit the flag entirely.
    system_prompt:
        Value for --system-prompt.  Defaults to the Claude Max OAuth routing
        string.  Override only when a richer system prompt is required (e.g.
        overlay/engine.py injects trading-specific instructions while still
        starting with the required prefix).
    extra_args:
        Additional CLI flags appended after the fixed flags, e.g.
        ``["--tools", "bash,read"]`` or ``["--no-tools"]``.
    pi_bin:
        Name/path of the pi executable. Defaults to ``"pi"``.
    cwd:
        Working directory for the subprocess (forwarded to subprocess.run).
    env:
        Environment for the subprocess (forwarded to subprocess.run).
        If None the current process environment is inherited.
    """
    cmd = [pi_bin, "-p", "--model", model, "--system-prompt", system_prompt]
    if mode:
        cmd.extend(["--mode", mode])
    if extra_args:
        cmd.extend(extra_args)

    try:
        result = subprocess.run(
            cmd,
            input=prompt,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=cwd,
            env=env,
        )
    except subprocess.TimeoutExpired as e:
        raise PiSubprocessError(f"pi CLI timed out after {timeout}s") from e
    except FileNotFoundError as e:
        raise PiSubprocessError(
            "pi CLI not found on PATH. Ensure pi is installed and on PATH."
        ) from e

    if result.returncode != 0:
        raise PiSubprocessError(
            f"pi CLI failed (rc={result.returncode}): {result.stderr[:500]}"
        )

    # Pi CLI can surface errors in stdout with exit code 0
    lowered = result.stdout.lower()
    if "out of extra usage" in lowered or "invalid_request_error" in lowered:
        raise PiSubprocessError(
            f"pi CLI auth/quota error in stdout: {result.stdout[:500]}"
        )

    return result.stdout


def call_pi_structured(
    prompt: str,
    model: str = DEFAULT_MODEL,
    timeout: int = DEFAULT_TIMEOUT,
    system_prompt: str = DEFAULT_SYSTEM_PROMPT,
    extra_args: Optional[list[str]] = None,
    cwd: Optional[str] = None,
    env: Optional[dict] = None,
) -> "dict | list":
    """Invoke pi in JSON mode and return parsed JSON.

    Thin wrapper around :func:`call_pi` that always uses ``--mode json`` and
    parses the result.  Raises :class:`PiSubprocessError` on parse failure.
    """
    raw = call_pi(
        prompt,
        model=model,
        timeout=timeout,
        mode="json",
        system_prompt=system_prompt,
        extra_args=extra_args,
        cwd=cwd,
        env=env,
    )
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        raise PiSubprocessError(
            f"pi CLI returned non-JSON output: {raw[:500]}"
        ) from e


def call_pi_exec(
    prompt: str,
    timeout: int = DEFAULT_TIMEOUT,
    system_prompt: str = DEFAULT_SYSTEM_PROMPT,
    extra_args: Optional[list[str]] = None,
    pi_bin: str = "pi",
) -> int:
    """Invoke ``pi --print`` with the prompt as a positional argument.

    Output streams directly to the terminal (``capture_output=False``).
    This variant is intended for long-running agent tasks where real-time
    output visibility matters more than capturing the result.

    Returns the subprocess exit code.  Raises :class:`PiSubprocessError` on
    timeout or if pi is not found — other non-zero exit codes are returned
    to the caller unchanged so it can decide how to handle them.

    Note: because output is not captured, stdout cannot be scanned for auth
    errors.  Callers should monitor the circuit breaker separately.
    """
    cmd = [pi_bin, "--print", "--system-prompt", system_prompt]
    if extra_args:
        cmd.extend(extra_args)
    # Prompt is the final positional argument (pi --print usage)
    cmd.append(prompt)

    try:
        result = subprocess.run(
            cmd,
            capture_output=False,
            timeout=timeout,
        )
        return result.returncode
    except subprocess.TimeoutExpired as e:
        raise PiSubprocessError(f"pi CLI timed out after {timeout}s") from e
    except FileNotFoundError as e:
        raise PiSubprocessError(
            "pi CLI not found on PATH. Ensure pi is installed and on PATH."
        ) from e


def call_pi_vision(
    prompt: str,
    image_paths,  # list[Path] | list[str]
    model: str = "claude-opus-4-8",
    timeout: int = 300,
    mode: Optional[str] = "json",
    system_prompt: str = DEFAULT_SYSTEM_PROMPT,
    extra_args: Optional[list[str]] = None,
    pi_bin: str = "pi",
    cwd: Optional[str] = None,
    env: Optional[dict] = None,
) -> str:
    """Invoke `pi -p` with chart images attached via @path references.

    Images are validated and attached as ``@<path>`` positional arguments.
    The text prompt is passed as the **final positional argument** after the
    @-refs (not via stdin) — this mirrors the behaviour in
    ``services/pi_session.py`` which is the verified working pattern for
    image attachments.  Stdin is left empty.

    Parameters
    ----------
    prompt:
        Text prompt sent to the model.
    image_paths:
        Paths to image files to attach. Each must exist on disk; raises
        ``FileNotFoundError`` for any missing path before spawning the process.
    model:
        Vision-capable Claude model. Default: ``"claude-opus-4-8"``.
    timeout:
        Subprocess timeout in seconds (default: 300 — vision calls are slow).
    mode:
        Value for --mode flag (e.g. ``"json"``).  Pass ``None`` to omit.
    system_prompt:
        Value for --system-prompt. Defaults to the Claude Max OAuth routing
        string.
    extra_args:
        Additional CLI flags appended before the @-refs.
    pi_bin:
        Name/path of the pi executable.
    cwd:
        Working directory for the subprocess.
    env:
        Environment for the subprocess. Inherits current env if None.

    Returns
    -------
    str
        Raw stdout from the pi CLI.

    Raises
    ------
    FileNotFoundError
        If any image path does not exist on disk.
    PiSubprocessError
        On timeout, non-zero exit code, auth/quota errors, or pi not found.
    """
    # Validate all image paths before spawning a subprocess
    paths: list[Path] = []
    for raw_p in image_paths:
        p = Path(raw_p)
        if not p.is_file():
            raise FileNotFoundError(f"Image not found: {p}")
        paths.append(p)

    cmd = [pi_bin, "-p", "--model", model, "--system-prompt", system_prompt]
    if mode:
        cmd.extend(["--mode", mode])
    if extra_args:
        cmd.extend(extra_args)
    # Attach images as @path positional references
    for p in paths:
        cmd.append(f"@{p}")
    # Prompt is the final positional argument (mirrors pi_session.py pattern)
    cmd.append(prompt)

    try:
        result = subprocess.run(
            cmd,
            input=None,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=cwd,
            env=env,
        )
    except subprocess.TimeoutExpired as e:
        raise PiSubprocessError(f"pi CLI timed out after {timeout}s") from e
    except FileNotFoundError as e:
        raise PiSubprocessError(
            "pi CLI not found on PATH. Ensure pi is installed and on PATH."
        ) from e

    if result.returncode != 0:
        raise PiSubprocessError(
            f"pi CLI failed (rc={result.returncode}): {result.stderr[:500]}"
        )

    # Pi CLI can surface auth errors in stdout with exit code 0
    lowered = result.stdout.lower()
    if "out of extra usage" in lowered or "invalid_request_error" in lowered:
        raise PiSubprocessError(
            f"pi CLI auth/quota error in stdout: {result.stdout[:500]}"
        )

    return result.stdout
