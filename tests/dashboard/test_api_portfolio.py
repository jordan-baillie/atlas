"""Tests for services/api/portfolio.py — Phase 3 extraction."""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

PROJECT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT))

from fastapi.testclient import TestClient
from fastapi import FastAPI
from atlas.dashboard.api.portfolio import router

_AUTH = ("test", "test")


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setattr("atlas.dashboard.auth._get_credentials", lambda: _AUTH)
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


class TestPortfolioEndpoint:
    def test_endpoint_exists(self, client):
        """GET /api/portfolio route is registered (not 404)."""
        with patch("atlas.db.get_open_positions", side_effect=RuntimeError("no db")):
            resp = client.get("/api/portfolio", auth=_AUTH)
        assert resp.status_code != 404

    def test_db_portfolio_alias(self, client):
        """GET /api/db/portfolio is also registered."""
        with patch("atlas.db.get_open_positions", side_effect=RuntimeError("no db")):
            resp = client.get("/api/db/portfolio", auth=_AUTH)
        assert resp.status_code != 404

    def test_returns_positions_regime_equity(self, client):
        """GET /api/portfolio returns positions, regime, equity keys."""
        import sqlite3 as _sq

        mem = _sq.connect(":memory:", check_same_thread=False)
        mem.row_factory = _sq.Row
        mem.execute(
            "CREATE TABLE market_equity_history "
            "(date TEXT, market_id TEXT, broker_equity REAL, allocated_equity REAL, "
            "cash_attributed REAL, position_mv REAL, broker_cash REAL)"
        )
        mem.execute(
            "INSERT INTO market_equity_history VALUES "
            "('2026-04-01','sp500',5000.0,5000.0,2000.0,3000.0,2000.0)"
        )
        mem.commit()

        class _Ctx:
            def __enter__(self): return mem
            def __exit__(self, *a): mem.rollback()

        with patch("atlas.db.get_open_positions", return_value=[]), \
             patch("atlas.db.get_current_regime", return_value={"state": "bull"}), \
             patch("atlas.db.get_db", return_value=_Ctx()):
            resp = client.get("/api/portfolio", auth=_AUTH)
        mem.close()
        assert resp.status_code == 200
        body = resp.json()
        assert "positions" in body
        assert "regime" in body
        assert body["equity"]["equity"] == 5000.0


class TestPerformanceEndpoint:
    def test_endpoint_exists(self, client):
        """GET /api/performance is registered."""
        with patch("atlas.db.performance_summary", side_effect=RuntimeError("no db")):
            resp = client.get("/api/performance", auth=_AUTH)
        assert resp.status_code != 404

    def test_db_performance_alias(self, client):
        """GET /api/db/performance is registered."""
        with patch("atlas.db.performance_summary", side_effect=RuntimeError("no db")):
            resp = client.get("/api/db/performance", auth=_AUTH)
        assert resp.status_code != 404


class TestEquityCurve:
    def test_endpoint_exists(self, client):
        """GET /api/equity-curve is registered."""
        with patch("atlas.db.get_equity_curve", side_effect=RuntimeError("no db")):
            resp = client.get("/api/equity-curve", auth=_AUTH)
        assert resp.status_code != 404

    def test_returns_list_reversed(self, client):
        """Returns rows oldest-first (reversed from DB descending order)."""
        # get_equity_curve returns desc → we reverse → oldest first
        rows_desc = [
            {"date": "2026-04-03", "equity": 5200.0},
            {"date": "2026-04-02", "equity": 5100.0},
            {"date": "2026-04-01", "equity": 5000.0},
        ]
        with patch("atlas.db.get_equity_curve", return_value=list(rows_desc)):
            resp = client.get("/api/equity-curve", auth=_AUTH)
        assert resp.status_code == 200
        body = resp.json()
        assert body[0]["date"] == "2026-04-01"  # oldest first after .reverse()


class TestMarketEquityHistory:
    def test_endpoint_exists(self, client):
        """GET /api/market_equity_history is registered."""
        with patch("atlas.db.get_db", side_effect=RuntimeError("no db")):
            resp = client.get("/api/market_equity_history", auth=_AUTH)
        assert resp.status_code != 404
