"""Verify that the autouse halt-file isolation fixture works.

These tests assert that the session+function-scope autouse fixtures in conftest.py
correctly redirect kill_switch._HALT_FILE away from the production path so that
test-time halt() calls never write to /root/atlas/data/HALT.

See: conftest._isolate_halt_file_session + _isolate_halt_file
Root cause fixed: 2026-04-29 — pytest run wrote real HALT file, blocking
pre-market execute_approved.
"""
from __future__ import annotations

import os
from pathlib import Path


def test_halt_file_does_not_point_to_production() -> None:
    """_HALT_FILE must not be the production path during any test."""
    from brokers import kill_switch

    halt_path = str(kill_switch._HALT_FILE)
    assert halt_path != "/root/atlas/data/HALT", (
        f"Test isolation broken — _HALT_FILE points to production: {halt_path}"
    )
    # Should be somewhere in the system tmp dir
    assert (
        "/tmp" in halt_path
        or "pytest" in halt_path
        or halt_path.startswith("/tmp")
    ), f"_HALT_FILE should be in a tmp dir, got: {halt_path}"


def test_halt_via_kill_switch_does_not_touch_production() -> None:
    """kill_switch.halt() during a test must not create production data/HALT."""
    from brokers.kill_switch import halt as _halt, resume as _resume

    _halt("test_halt_isolation reason")

    # Production file MUST NOT exist
    assert not os.path.exists("/root/atlas/data/HALT"), (
        "kill_switch.halt() during test wrote to production /root/atlas/data/HALT — "
        "fixture isolation broken"
    )

    _resume()  # clean up the tmp file too

    # After resume(), the tmp path must also be clear
    from brokers import kill_switch
    assert not kill_switch._HALT_FILE.exists(), (
        "kill_switch.resume() should have removed the tmp HALT file"
    )
