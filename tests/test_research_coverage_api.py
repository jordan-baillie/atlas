"""Tests for GET /api/research/coverage — Item C4."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.security import HTTPBasicCredentials
from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_client(app):
    """Return a TestClient with auth dependency bypassed."""
    from services.chat_server import check_auth
    app.dependency_overrides[check_auth] = lambda: HTTPBasicCredentials(
        username="test", password="test"
    )
    return TestClient(app, raise_server_exceptions=True)


def _seed_research_best(db_path: str, rows: list[dict]) -> None:
    """Directly insert rows into research_best for testing."""
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS research_best "
        "(strategy TEXT, universe TEXT, params TEXT, sharpe REAL, "
        "trades INTEGER, max_dd_pct REAL, updated_at TEXT, "
        "PRIMARY KEY (strategy, universe))"
    )
    for r in rows:
        conn.execute(
            "INSERT OR REPLACE INTO research_best "
            "(strategy, universe, params, sharpe, trades, max_dd_pct, updated_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (
                r["strategy"],
                r["universe"],
                r.get("params", "{}"),
                r["sharpe"],
                r["trades"],
                r.get("max_dd_pct", 5.0),
                r.get("updated_at"),
            ),
        )
    conn.commit()
    conn.close()


def _iso(days_ago: float) -> str:
    """Return UTC ISO string N days in the past."""
    ts = datetime.now(timezone.utc) - timedelta(days=days_ago)
    return ts.isoformat()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestResearchCoverageMatrix:
    """C4 — /api/research/coverage endpoint."""

    def test_coverage_full_matrix(self, tmp_path, monkeypatch):
        """Full 3×3 matrix: each cell has a status based on age_days."""
        import db.atlas_db as _adb

        db_path = str(tmp_path / "test_coverage.db")
        monkeypatch.setattr(_adb, "_db_path_override", db_path)

        rows = [
            # strategy_a / sp500 — 2 days old → fresh
            {"strategy": "strategy_a", "universe": "sp500", "sharpe": 1.5,
             "trades": 100, "updated_at": _iso(2)},
            # strategy_a / asx — 10 days old → stale
            {"strategy": "strategy_a", "universe": "asx", "sharpe": 0.9,
             "trades": 40, "updated_at": _iso(10)},
            # strategy_a / commodity_etfs — 20 days old → very_stale
            {"strategy": "strategy_a", "universe": "commodity_etfs", "sharpe": 0.5,
             "trades": 20, "updated_at": _iso(20)},
            # strategy_b — fresh entries
            {"strategy": "strategy_b", "universe": "sp500", "sharpe": 1.2,
             "trades": 80, "updated_at": _iso(1)},
            {"strategy": "strategy_b", "universe": "asx", "sharpe": 0.8,
             "trades": 30, "updated_at": _iso(5)},
            {"strategy": "strategy_b", "universe": "commodity_etfs", "sharpe": 0.6,
             "trades": 15, "updated_at": _iso(3)},
            # strategy_c — mix
            {"strategy": "strategy_c", "universe": "sp500", "sharpe": 0.4,
             "trades": 60, "updated_at": _iso(8)},
            {"strategy": "strategy_c", "universe": "asx", "sharpe": 1.1,
             "trades": 50, "updated_at": _iso(6)},
            {"strategy": "strategy_c", "universe": "commodity_etfs", "sharpe": 0.7,
             "trades": 25, "updated_at": _iso(18)},
        ]
        _seed_research_best(db_path, rows)

        from services.chat_server import app
        client = _make_client(app)
        try:
            resp = client.get("/api/research/coverage")
            assert resp.status_code == 200, resp.text
            data = resp.json()

            # Top-level keys
            assert "strategies" in data
            assert "universes" in data
            assert "matrix" in data
            assert "generated_at" in data

            # Sorted order
            assert data["strategies"] == sorted(data["strategies"])
            assert data["universes"] == sorted(data["universes"])
            assert set(data["strategies"]) == {"strategy_a", "strategy_b", "strategy_c"}
            assert set(data["universes"]) == {"asx", "commodity_etfs", "sp500"}

            # Status assertions
            cell_fresh = data["matrix"]["strategy_a"]["sp500"]
            assert cell_fresh is not None
            assert cell_fresh["status"] == "fresh"
            assert cell_fresh["sharpe"] == pytest.approx(1.5)
            assert cell_fresh["trades"] == 100
            assert cell_fresh["age_days"] is not None
            assert cell_fresh["age_days"] < 7

            cell_stale = data["matrix"]["strategy_a"]["asx"]
            assert cell_stale is not None
            assert cell_stale["status"] == "stale"
            assert 7 <= cell_stale["age_days"] < 14

            cell_very_stale = data["matrix"]["strategy_a"]["commodity_etfs"]
            assert cell_very_stale is not None
            assert cell_very_stale["status"] == "very_stale"
            assert cell_very_stale["age_days"] >= 14
        finally:
            app.dependency_overrides.clear()

    def test_coverage_sparse_matrix(self, tmp_path, monkeypatch):
        """Only 2 out of a possible 3×3 cells exist — the rest are null."""
        import db.atlas_db as _adb

        db_path = str(tmp_path / "test_sparse.db")
        monkeypatch.setattr(_adb, "_db_path_override", db_path)

        # strategy_a appears in sp500 and asx but NOT commodity_etfs;
        # strategy_b appears in sp500 only
        rows = [
            {"strategy": "strategy_a", "universe": "sp500", "sharpe": 1.0,
             "trades": 50, "updated_at": _iso(3)},
            {"strategy": "strategy_a", "universe": "asx", "sharpe": 0.8,
             "trades": 20, "updated_at": _iso(4)},
            {"strategy": "strategy_b", "universe": "sp500", "sharpe": 1.2,
             "trades": 60, "updated_at": _iso(2)},
        ]
        _seed_research_best(db_path, rows)

        from services.chat_server import app
        client = _make_client(app)
        try:
            resp = client.get("/api/research/coverage")
            assert resp.status_code == 200, resp.text
            data = resp.json()

            # strategy_a has asx, sp500 — universe list includes only what's in DB
            # so the universe axis is {"asx", "sp500"} (not commodity_etfs)
            assert set(data["universes"]) == {"asx", "sp500"}
            assert set(data["strategies"]) == {"strategy_a", "strategy_b"}

            # strategy_a/sp500 → populated
            assert data["matrix"]["strategy_a"]["sp500"] is not None
            # strategy_a/asx → populated
            assert data["matrix"]["strategy_a"]["asx"] is not None
            # strategy_b/sp500 → populated
            assert data["matrix"]["strategy_b"]["sp500"] is not None
            # strategy_b/asx → null (no row in DB)
            assert data["matrix"]["strategy_b"]["asx"] is None
        finally:
            app.dependency_overrides.clear()

    def test_coverage_empty_db(self, tmp_path, monkeypatch):
        """Empty research_best table returns empty strategies/universes/matrix."""
        import db.atlas_db as _adb

        db_path = str(tmp_path / "test_empty.db")
        monkeypatch.setattr(_adb, "_db_path_override", db_path)
        # Ensure table exists (init_db creates it) but is empty
        from db.atlas_db import init_db
        try:
            init_db()
        except Exception:
            # Create minimal table so endpoint doesn't crash
            _seed_research_best(db_path, [])

        from services.chat_server import app
        client = _make_client(app)
        try:
            resp = client.get("/api/research/coverage")
            assert resp.status_code == 200, resp.text
            data = resp.json()
            assert data["strategies"] == []
            assert data["universes"] == []
            assert data["matrix"] == {}
            assert "generated_at" in data
        finally:
            app.dependency_overrides.clear()

    def test_coverage_unauthenticated(self):
        """GET /api/research/coverage without auth → 401."""
        from services.chat_server import app
        client = TestClient(app, raise_server_exceptions=False)
        # No dependency override — real auth check fires
        resp = client.get("/api/research/coverage")
        assert resp.status_code == 401, f"Expected 401, got {resp.status_code}"

    def test_coverage_response_shape(self, tmp_path, monkeypatch):
        """Each non-null cell has exactly the required keys."""
        import db.atlas_db as _adb

        db_path = str(tmp_path / "test_shape.db")
        monkeypatch.setattr(_adb, "_db_path_override", db_path)

        rows = [
            {"strategy": "momentum", "universe": "sp500", "sharpe": 0.95,
             "trades": 77, "updated_at": _iso(1)},
        ]
        _seed_research_best(db_path, rows)

        from services.chat_server import app
        client = _make_client(app)
        try:
            resp = client.get("/api/research/coverage")
            assert resp.status_code == 200
            data = resp.json()
            cell = data["matrix"]["momentum"]["sp500"]
            assert cell is not None
            for key in ("sharpe", "trades", "updated_at", "age_days", "status"):
                assert key in cell, f"Missing key '{key}' in cell: {cell}"
            assert isinstance(cell["status"], str)
            assert cell["status"] in ("fresh", "stale", "very_stale", "never")
        finally:
            app.dependency_overrides.clear()
