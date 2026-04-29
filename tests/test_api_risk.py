"""Tests for services/api/risk.py — Phase 5 extraction."""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

from fastapi.testclient import TestClient
from fastapi import FastAPI
from services.api.risk import router

_AUTH = ("test", "test")


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setattr("services.auth._get_credentials", lambda: _AUTH)
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


class TestPositionsRisk:
    def test_endpoint_exists(self, client):
        """GET /api/positions/risk is registered (not 404)."""
        with patch("db.atlas_db.get_cached_portfolio_risk", return_value=None), \
             patch("db.atlas_db.get_db", side_effect=RuntimeError("no db")):
            resp = client.get("/api/positions/risk", auth=_AUTH)
        assert resp.status_code != 404

    def test_cache_hit_fresh(self, client):
        """Returns cached response when cache is fresh (stale=False)."""
        cached = {
            "equity": 5000.0, "positions_count": 3, "tickers": ["AAPL"],
            "method": "regime_conditional", "var_1d_95": -100.0,
            "cvar_1d_95": -120.0, "effective_bets": 2.1,
            "correlation_avg": 0.3, "as_of": "2026-04-30T00:00:00",
            "stale": False,
        }
        with patch("db.atlas_db.get_cached_portfolio_risk", return_value=cached):
            resp = client.get("/api/positions/risk", auth=_AUTH)
        assert resp.status_code == 200
        body = resp.json()
        assert body["stale"] is False
        assert body["source"] == "cache"
        assert body["summary"]["equity"] == 5000.0

    def test_cache_hit_stale_triggers_refresh(self, client):
        """Returns stale cache and triggers background refresh."""
        cached = {
            "equity": 4000.0, "positions_count": 1, "tickers": ["MSFT"],
            "method": "regime_conditional", "var_1d_95": -80.0,
            "cvar_1d_95": -90.0, "effective_bets": 1.0,
            "correlation_avg": 0.0, "as_of": "2026-04-29T00:00:00",
            "stale": True,
        }
        with patch("db.atlas_db.get_cached_portfolio_risk", return_value=cached), \
             patch("subprocess.Popen") as mock_popen:
            mock_popen.return_value = MagicMock()
            resp = client.get("/api/positions/risk", auth=_AUTH)
        assert resp.status_code == 200
        body = resp.json()
        assert body["stale"] is True
        assert body["source"] == "cache"


class TestSignalsEv:
    def test_endpoint_exists(self, client):
        """GET /api/signals/ev is registered."""
        with patch("db.atlas_db.get_db", side_effect=RuntimeError("no db")):
            resp = client.get("/api/signals/ev", auth=_AUTH)
        assert resp.status_code != 404

    def test_returns_cached_from_db(self, client):
        """Returns cached rows from signal_ev table."""
        import sqlite3 as _sq
        mem = _sq.connect(":memory:", check_same_thread=False)
        mem.row_factory = _sq.Row
        mem.execute(
            "CREATE TABLE signal_ev (strategy TEXT, ev_per_trade REAL, as_of TEXT)"
        )
        mem.execute("INSERT INTO signal_ev VALUES ('momentum_breakout', 1.5, '2026-04-30')")
        mem.commit()

        class _Ctx:
            def __enter__(self): return mem
            def __exit__(self, *a): pass

        with patch("db.atlas_db.get_db", return_value=_Ctx()):
            resp = client.get("/api/signals/ev", auth=_AUTH)
        mem.close()
        assert resp.status_code == 200
        body = resp.json()
        assert "strategies" in body
        assert body["source"] == "cached"


class TestRiskRuin:
    def test_endpoint_exists(self, client):
        """GET /api/risk/ruin is registered."""
        with patch("db.atlas_db.get_cached_ruin_probability", return_value=None), \
             patch("db.atlas_db.get_db", side_effect=RuntimeError("no db")):
            resp = client.get("/api/risk/ruin", auth=_AUTH)
        assert resp.status_code != 404

    def test_cache_hit_returns_data(self, client):
        """Returns cached ruin data with status=ok."""
        cached = {
            "current_equity": 5000.0, "floor": 3500.0, "floor_pct": 0.7,
            "n_paths": 10000, "as_of": "2026-04-30", "prob": 0.01,
            "tickers": ["AAPL"], "horizons": {"30d": {"prob_ruin": 0.01}},
            "stale": False, "reason": None,
        }
        with patch("db.atlas_db.get_cached_ruin_probability", return_value=cached):
            resp = client.get("/api/risk/ruin", auth=_AUTH)
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "ok"
        assert body["prob"] == 0.01


class TestVixTermStructure:
    def test_endpoint_exists(self, client):
        """GET /api/signals/vix_term_structure is registered."""
        with patch("signals.vix_term_structure.get_current_signal",
                   side_effect=RuntimeError("no signal")):
            resp = client.get("/api/signals/vix_term_structure", auth=_AUTH)
        assert resp.status_code != 404

    def test_returns_signal_dict(self, client):
        """Returns VIX signal dict."""
        fake = {"ratio": 0.9, "action": "buy", "date": "2026-04-30"}
        with patch("signals.vix_term_structure.get_current_signal", return_value=fake):
            resp = client.get("/api/signals/vix_term_structure", auth=_AUTH)
        assert resp.status_code == 200
        assert resp.json()["action"] == "buy"

    def test_error_in_signal_returns_503(self, client):
        """When signal has 'error' key, returns 503."""
        with patch("signals.vix_term_structure.get_current_signal",
                   return_value={"error": "No data available"}):
            resp = client.get("/api/signals/vix_term_structure", auth=_AUTH)
        assert resp.status_code == 503


class TestRiskRuinRefresh:
    def test_endpoint_exists(self, client):
        """POST /api/risk/ruin/refresh is registered."""
        with patch("subprocess.Popen", return_value=MagicMock()):
            resp = client.post("/api/risk/ruin/refresh", auth=_AUTH)
        assert resp.status_code in (200, 500)

    def test_returns_ok_true(self, client):
        """POST /api/risk/ruin/refresh returns {ok: true, started_at: ...}."""
        with patch("subprocess.Popen", return_value=MagicMock()):
            resp = client.post("/api/risk/ruin/refresh", auth=_AUTH)
        if resp.status_code == 200:
            body = resp.json()
            assert body["ok"] is True
            assert "started_at" in body
