"""Tests for services/api/portfolio.py — Phase 3 extraction."""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

from fastapi.testclient import TestClient
from fastapi import FastAPI
from services.api.portfolio import router

_AUTH = ("test", "test")


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setattr("services.auth._get_credentials", lambda: _AUTH)
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


class TestPortfolioEndpoint:
    def test_endpoint_exists(self, client):
        """GET /api/portfolio route is registered (not 404)."""
        with patch("db.atlas_db.get_open_positions", side_effect=RuntimeError("no db")):
            resp = client.get("/api/portfolio", auth=_AUTH)
        assert resp.status_code != 404

    def test_db_portfolio_alias(self, client):
        """GET /api/db/portfolio is also registered."""
        with patch("db.atlas_db.get_open_positions", side_effect=RuntimeError("no db")):
            resp = client.get("/api/db/portfolio", auth=_AUTH)
        assert resp.status_code != 404

    def test_returns_positions_regime_equity(self, client):
        """GET /api/portfolio returns positions, regime, equity keys."""
        import sqlite3 as _sq

        mem = _sq.connect(":memory:", check_same_thread=False)
        mem.row_factory = _sq.Row
        mem.execute(
            "CREATE TABLE equity_curve "
            "(id INTEGER PRIMARY KEY, date TEXT, equity REAL)"
        )
        mem.execute("INSERT INTO equity_curve VALUES (1,'2026-04-01',5000.0)")
        mem.commit()

        class _Ctx:
            def __enter__(self): return mem
            def __exit__(self, *a): mem.rollback()

        with patch("db.atlas_db.get_open_positions", return_value=[]), \
             patch("db.atlas_db.get_current_regime", return_value={"state": "bull"}), \
             patch("db.atlas_db.get_db", return_value=_Ctx()):
            resp = client.get("/api/portfolio", auth=_AUTH)
        mem.close()
        assert resp.status_code == 200
        body = resp.json()
        assert "positions" in body
        assert "regime" in body


class TestTradesEndpoint:
    def test_trades_endpoint_exists(self, client):
        """GET /api/trades is registered."""
        with patch("db.atlas_db.get_db", side_effect=RuntimeError("no db")):
            resp = client.get("/api/trades", auth=_AUTH)
        assert resp.status_code != 404

    def test_db_trades_alias(self, client):
        """GET /api/db/trades is also registered."""
        with patch("db.atlas_db.get_db", side_effect=RuntimeError("no db")):
            resp = client.get("/api/db/trades", auth=_AUTH)
        assert resp.status_code != 404

    def test_trades_returns_list(self, client):
        """GET /api/trades returns {trades: [], count: 0} for empty DB."""
        import sqlite3 as _sq
        mem = _sq.connect(":memory:", check_same_thread=False)
        mem.row_factory = _sq.Row
        mem.execute(
            "CREATE TABLE trades (id INTEGER PRIMARY KEY, ticker TEXT, "
            "strategy TEXT, status TEXT, exit_date TEXT, universe TEXT)"
        )
        mem.execute(
            "CREATE TABLE signals (ticker TEXT, sector TEXT)"
        )
        mem.commit()

        class _Ctx:
            def __enter__(self): return mem
            def __exit__(self, *a): pass

        with patch("db.atlas_db.get_db", return_value=_Ctx()):
            resp = client.get("/api/trades", auth=_AUTH)
        mem.close()
        assert resp.status_code == 200
        body = resp.json()
        assert "trades" in body
        assert "count" in body


class TestPnlFilterOptions:
    def test_endpoint_exists(self, client):
        """GET /api/pnl_filter_options is registered (not 404)."""
        with patch("db.atlas_db.get_db", side_effect=RuntimeError("no db")):
            resp = client.get("/api/pnl_filter_options", auth=_AUTH)
        assert resp.status_code != 404

    def test_returns_correct_shape(self, client):
        """Response includes markets, strategies, sectors keys."""
        import sqlite3 as _sq
        mem = _sq.connect(":memory:", check_same_thread=False)
        mem.row_factory = _sq.Row
        mem.execute(
            "CREATE TABLE trades (id INTEGER PRIMARY KEY, universe TEXT, "
            "strategy TEXT, status TEXT)"
        )
        mem.execute("CREATE TABLE signals (ticker TEXT, sector TEXT)")
        mem.commit()

        class _Ctx:
            def __enter__(self): return mem
            def __exit__(self, *a): pass

        with patch("db.atlas_db.get_db", return_value=_Ctx()):
            resp = client.get("/api/pnl_filter_options", auth=_AUTH)
        mem.close()
        assert resp.status_code == 200
        body = resp.json()
        assert "markets" in body
        assert "strategies" in body
        assert "sectors" in body


class TestPerformanceEndpoint:
    def test_endpoint_exists(self, client):
        """GET /api/performance is registered."""
        with patch("db.atlas_db.performance_summary", side_effect=RuntimeError("no db")):
            resp = client.get("/api/performance", auth=_AUTH)
        assert resp.status_code != 404

    def test_db_performance_alias(self, client):
        """GET /api/db/performance is registered."""
        with patch("db.atlas_db.performance_summary", side_effect=RuntimeError("no db")):
            resp = client.get("/api/db/performance", auth=_AUTH)
        assert resp.status_code != 404


class TestEquityCurve:
    def test_endpoint_exists(self, client):
        """GET /api/equity-curve is registered."""
        with patch("db.atlas_db.get_equity_curve", side_effect=RuntimeError("no db")):
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
        with patch("db.atlas_db.get_equity_curve", return_value=list(rows_desc)):
            resp = client.get("/api/equity-curve", auth=_AUTH)
        assert resp.status_code == 200
        body = resp.json()
        assert body[0]["date"] == "2026-04-01"  # oldest first after .reverse()


class TestMarketEquityHistory:
    def test_endpoint_exists(self, client):
        """GET /api/market_equity_history is registered."""
        with patch("db.atlas_db.get_db", side_effect=RuntimeError("no db")):
            resp = client.get("/api/market_equity_history", auth=_AUTH)
        assert resp.status_code != 404


class TestOverlayDecisions:
    def test_endpoint_exists(self, client):
        """GET /api/overlay/decisions is registered."""
        with patch("db.atlas_db.get_overlay_decisions", side_effect=RuntimeError("no db")):
            resp = client.get("/api/overlay/decisions", auth=_AUTH)
        assert resp.status_code != 404

    def test_returns_decisions(self, client):
        """Returns list of decisions from atlas_db."""
        fake = [{"id": 1, "decision": "hold", "date": "2026-04-01"}]
        with patch("db.atlas_db.get_overlay_decisions", return_value=fake):
            resp = client.get("/api/overlay/decisions", auth=_AUTH)
        assert resp.status_code == 200
        assert resp.json() == fake
