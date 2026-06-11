"""Tests for oneshot-aware systemd status normalisation in services/api/health.py.

Covers:
  - _systemctl_status() normalisation logic (11 unit cases)
  - system_health() endpoint reports the live unit set (dashboard + timers)
  - Backward-compat: services dict values are strings, not dicts
"""
from __future__ import annotations

import subprocess
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_proc(stdout: str, returncode: int = 0):
    """Return a mock subprocess.CompletedProcess-like object."""
    p = MagicMock()
    p.stdout = stdout
    p.returncode = returncode
    return p


def _make_get_db():
    """Return a mock get_db() callable that acts as a proper context manager."""
    db = MagicMock()
    row = MagicMock()
    row.__getitem__ = lambda self, k: None   # row["any_key"] → None
    execute_result = MagicMock()
    execute_result.fetchone.return_value = row
    execute_result.fetchall.return_value = []
    db.execute.return_value = execute_result

    @contextmanager
    def _ctx():
        yield db

    return _ctx


def _mock_systemctl_outputs(svc_outputs: dict):
    """Return a subprocess.run side_effect that dispatches by service name."""
    def _run(cmd, **kwargs):
        # cmd shape: ["systemctl", "show", svc_name, "--property=...", ...]
        svc_name = cmd[2] if len(cmd) > 2 else "__unknown__"
        stdout = svc_outputs.get(svc_name, "")
        return _make_proc(stdout)
    return _run


# ---------------------------------------------------------------------------
# Unit tests for _systemctl_status()
# ---------------------------------------------------------------------------

class TestSystemctlStatus:
    """Tests for the module-level _systemctl_status() helper."""

    def _call(self, stdout: str, raises=None):
        from atlas.dashboard.api import health as h
        if raises:
            with patch("subprocess.run", side_effect=raises):
                return h._systemctl_status("some-svc")
        else:
            with patch("subprocess.run", return_value=_make_proc(stdout)):
                return h._systemctl_status("some-svc")

    # Case 1: normal long-running service, active + successful
    def test_simple_active_success(self):
        result = self._call("simple\nsuccess\nactive\n")
        assert result["status"] == "active"
        assert result["type"] == "simple"
        assert result["result"] == "success"
        assert result["active_state"] == "active"

    # Case 2: oneshot unit — post-success, now inactive  ← THE BUG FIX
    def test_oneshot_inactive_success(self):
        result = self._call("oneshot\nsuccess\ninactive\n")
        assert result["status"] == "oneshot-success"
        assert result["type"] == "oneshot"
        assert result["result"] == "success"
        assert result["active_state"] == "inactive"

    # Case 3: failed unit (Result=failed)
    def test_result_failed(self):
        result = self._call("simple\nfailed\nfailed\n")
        assert result["status"] == "failed"

    # Case 3b: ActiveState=failed even when Result string is empty
    def test_active_state_failed_wins(self):
        result = self._call("simple\n\nfailed\n")
        assert result["status"] == "failed"

    # Case 4: service is activating (starting up)
    def test_activating(self):
        result = self._call("simple\n\nactivating\n")
        assert result["status"] == "activating"

    # Case 5: empty / malformed output → unknown, no crash
    def test_empty_output_returns_unknown(self):
        result = self._call("")
        assert result["status"] == "unknown"
        assert result["type"] == "?"
        assert result["result"] == "?"
        assert result["active_state"] == "?"

    def test_partial_output_returns_unknown(self):
        # Only 2 lines — missing ActiveState
        result = self._call("simple\nsuccess")
        assert result["status"] == "unknown"

    # Case 6: subprocess exception → unknown, no crash
    def test_subprocess_timeout_returns_unknown(self):
        result = self._call("", raises=subprocess.TimeoutExpired("cmd", 5))
        assert result["status"] == "unknown"

    def test_file_not_found_returns_unknown(self):
        result = self._call("", raises=FileNotFoundError("systemctl not found"))
        assert result["status"] == "unknown"

    # Case 7: oneshot but FAILED — must NOT be treated as success
    def test_oneshot_failed_not_success(self):
        result = self._call("oneshot\nfailed\nfailed\n")
        assert result["status"] == "failed"

    # Case 8: simple inactive success → unknown (only oneshot gets the green pass)
    def test_simple_inactive_success_is_unknown(self):
        result = self._call("simple\nsuccess\ninactive\n")
        assert result["status"] == "unknown"


# ---------------------------------------------------------------------------
# Integration tests via FastAPI TestClient
# ---------------------------------------------------------------------------

_DEFAULT_OUTPUTS = {
    "atlas-dashboard": "simple\nsuccess\nactive\n",
    "atlas-live-shadow.timer": "oneshot\nsuccess\ninactive\n",
    "atlas-backup.timer": "simple\nsuccess\nactive\n",
    "unified-healthcheck.timer": "simple\nsuccess\nactive\n",
}


def _build_app():
    from fastapi import FastAPI
    from atlas.dashboard.api.health import router
    from atlas.dashboard.auth import check_auth
    from fastapi.security import HTTPBasicCredentials

    app = FastAPI()

    async def _no_auth():
        return HTTPBasicCredentials(username="test", password="test")

    app.dependency_overrides[check_auth] = _no_auth
    app.include_router(router)
    return app


def _get_health_json(svc_outputs):
    """Run /api/system/health with mocked subprocess + DB, return parsed JSON."""
    from atlas import db as atlas_db

    side_effect = _mock_systemctl_outputs(svc_outputs)
    mock_get_db = _make_get_db()

    with patch("subprocess.run", side_effect=side_effect):
        with patch.object(atlas_db, "get_heartbeats", return_value=[]):
            with patch.object(atlas_db, "get_db", new=mock_get_db):
                with patch.object(atlas_db, "get_latest_equity", return_value=None):
                    client = TestClient(_build_app())
                    resp = client.get("/api/system/health")
    return resp


class TestSystemHealthEndpoint:
    """Integration tests for GET /api/system/health via TestClient."""

    def test_live_unit_set_present(self):
        """The live unit set (dashboard + timers) appears; retired units do not."""
        resp = _get_health_json(_DEFAULT_OUTPUTS)
        assert resp.status_code == 200, resp.text
        services = resp.json()["services"]
        for unit in ("atlas-dashboard", "atlas-live-shadow.timer",
                     "atlas-backup.timer", "unified-healthcheck.timer"):
            assert unit in services
        assert "atlas-dashboard-refresh" not in services
        assert "atlas-telegram-bot" not in services

    def test_oneshot_service_reports_oneshot_success(self):
        """Post-success oneshot unit returns 'oneshot-success'."""
        resp = _get_health_json(_DEFAULT_OUTPUTS)
        assert resp.status_code == 200, resp.text
        assert resp.json()["services"]["atlas-live-shadow.timer"] == "oneshot-success"

    def test_services_values_are_strings(self):
        """services dict must contain plain strings for frontend backward-compat."""
        resp = _get_health_json(_DEFAULT_OUTPUTS)
        assert resp.status_code == 200, resp.text
        for name, val in resp.json()["services"].items():
            assert isinstance(val, str), (
                f"services[{name!r}] is {type(val).__name__!r}, expected str"
            )

    def test_active_dashboard_returns_active(self):
        """Running atlas-dashboard (simple type) returns 'active'."""
        resp = _get_health_json(_DEFAULT_OUTPUTS)
        assert resp.status_code == 200, resp.text
        assert resp.json()["services"]["atlas-dashboard"] == "active"

    def test_failed_service_reported(self):
        """A crashed service returns 'failed'."""
        outputs = {**_DEFAULT_OUTPUTS, "atlas-dashboard": "simple\nfailed\nfailed\n"}
        resp = _get_health_json(outputs)
        assert resp.status_code == 200, resp.text
        assert resp.json()["services"]["atlas-dashboard"] == "failed"
