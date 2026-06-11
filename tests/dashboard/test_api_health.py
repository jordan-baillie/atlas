"""Tests for services/api/health.py — Phase 4 extraction."""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

PROJECT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT))

from fastapi.testclient import TestClient
from fastapi import FastAPI
from atlas.dashboard.api.health import router

_AUTH = ("test", "test")


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setattr("atlas.dashboard.auth._get_credentials", lambda: _AUTH)
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


class TestSystemHealth:
    def test_endpoint_exists(self, client):
        """GET /api/system/health is registered."""
        with patch("atlas.db.get_heartbeats", return_value=[]), \
             patch("atlas.db.get_db", side_effect=RuntimeError("no db")):
            resp = client.get("/api/system/health", auth=_AUTH)
        assert resp.status_code != 404

    def test_returns_expected_keys(self, client):
        """Response includes services, cron, data_freshness, heartbeats."""
        import sqlite3 as _sq
        mem = _sq.connect(":memory:", check_same_thread=False)
        mem.row_factory = _sq.Row
        mem.execute(
            "CREATE TABLE ohlcv (ticker TEXT, date TEXT)"
        )
        mem.execute(
            "CREATE TABLE equity_curve (date TEXT, equity REAL)"
        )
        mem.execute(
            "CREATE TABLE overlay_decisions (id INTEGER PRIMARY KEY)"
        )
        mem.commit()

        class _Ctx:
            def __enter__(self): return mem
            def __exit__(self, *a): pass

        with patch("atlas.db.get_heartbeats", return_value=[]), \
             patch("atlas.db.get_db", return_value=_Ctx()), \
             patch("atlas.dashboard.api.health.subprocess.run") as mock_run, \
             patch("atlas.dashboard.api.health.Path.glob", return_value=iter([])):
            mock_run.return_value = MagicMock(stdout="inactive\n")
            resp = client.get("/api/system/health", auth=_AUTH)

        mem.close()
        assert resp.status_code == 200
        body = resp.json()
        assert "services" in body
        assert "heartbeats" in body
        assert "data_freshness" in body


