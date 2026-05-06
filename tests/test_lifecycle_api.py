"""Tests for services/api/lifecycle.py — strategy lifecycle API endpoints.

Covers:
  1.  GET  /api/strategy-lifecycle          — list all rows (shape check)
  2.  GET  /api/strategy-lifecycle          — research_sharpe enrichment
  3.  GET  /api/strategy-lifecycle          — PAPER-state paper metrics enrichment
  4.  GET  /api/strategy-lifecycle/{s}/{u}/history — ordered history
  5.  POST /api/strategy-lifecycle/transition — allowed move (RESEARCH → PAPER)
  6.  POST /api/strategy-lifecycle/transition — disallowed move → 400
  7.  POST /api/strategy-lifecycle/transition — disallowed + force=true → 200
  8.  POST /api/strategy-lifecycle/promote-paper — clean gates → promotion fires
  9.  POST /api/strategy-lifecycle/promote-paper — failing gate → gate breakdown
  10. GET  /api/strategy-lifecycle          — 401 when unauthenticated

All DB ops use the global _isolate_prod_db autouse fixture from conftest.py
(redirects atlas_db._db_path_override to a tmp SQLite per test).
"""
from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi.security import HTTPBasicCredentials
from fastapi.testclient import TestClient

ATLAS_ROOT = Path(__file__).resolve().parents[1]
if str(ATLAS_ROOT) not in sys.path:
    sys.path.insert(0, str(ATLAS_ROOT))


# ── Fixtures ──────────────────────────────────────────────────────────────────

def _iso(days_ago: float = 0) -> str:
    """UTC ISO timestamp N days ago."""
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()


def _date(days_ago: float = 0) -> str:
    """UTC date string N days ago (YYYY-MM-DD)."""
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).strftime("%Y-%m-%d")


@pytest.fixture()
def seeded_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolated DB seeded with lifecycle + research_best + paper/live trade rows."""
    import db.atlas_db as _adb

    db_path = tmp_path / "lifecycle_test.db"
    monkeypatch.setattr(_adb, "_db_path_override", str(db_path))
    _adb.init_db()

    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")

    # ── strategy_lifecycle rows ───────────────────────────────────────────────
    # momentum_breakout/sp500 — LIVE (8 rows exist in prod, use as archetype)
    conn.execute(
        "INSERT OR REPLACE INTO strategy_lifecycle "
        "(strategy, universe, state, entered_state_at, prev_state, transition_reason) "
        "VALUES (?, ?, 'LIVE', ?, NULL, 'Migration: pre-existing live strategy')",
        ("momentum_breakout", "sp500", _iso(60)),
    )
    # mean_reversion/sp500 — PAPER (entered 5 days ago)
    conn.execute(
        "INSERT OR REPLACE INTO strategy_lifecycle "
        "(strategy, universe, state, entered_state_at, paper_start_date, prev_state) "
        "VALUES (?, ?, 'PAPER', ?, ?, 'RESEARCH')",
        ("mean_reversion", "sp500", _iso(5), _date(5)),
    )
    # adx_trend_pullback/sp500 — RESEARCH
    conn.execute(
        "INSERT OR REPLACE INTO strategy_lifecycle "
        "(strategy, universe, state, entered_state_at, prev_state) "
        "VALUES (?, ?, 'RESEARCH', ?, NULL)",
        ("adx_trend_pullback", "sp500", _iso(10)),
    )

    # ── strategy_lifecycle_history rows ──────────────────────────────────────
    conn.execute(
        "INSERT INTO strategy_lifecycle_history "
        "(strategy, universe, from_state, to_state, transitioned_at, reason, operator) "
        "VALUES (?, ?, NULL, 'RESEARCH', ?, 'Initial seed', 'system')",
        ("mean_reversion", "sp500", _iso(30)),
    )
    conn.execute(
        "INSERT INTO strategy_lifecycle_history "
        "(strategy, universe, from_state, to_state, transitioned_at, reason, operator) "
        "VALUES (?, ?, 'RESEARCH', 'PAPER', ?, 'Passed research gate', 'system')",
        ("mean_reversion", "sp500", _iso(5)),
    )

    # ── research_best rows ────────────────────────────────────────────────────
    conn.execute(
        "INSERT OR REPLACE INTO research_best "
        "(strategy, universe, regime_state, params, sharpe, trades, max_dd_pct, metric_type) "
        "VALUES (?, ?, NULL, '{}', 1.20, 50, 5.0, 'sharpe')",
        ("momentum_breakout", "sp500"),
    )
    conn.execute(
        "INSERT OR REPLACE INTO research_best "
        "(strategy, universe, regime_state, params, sharpe, trades, max_dd_pct, metric_type) "
        "VALUES (?, ?, NULL, '{}', 0.80, 40, 8.0, 'sharpe')",
        ("mean_reversion", "sp500"),
    )

    # ── paper_trades — closed in last 30d for mean_reversion/sp500 ───────────
    # Use alternating [1.6, -0.4] so variance is non-zero → paper_sharpe computable
    for i in range(6):
        pnl_pct = 1.6 if i % 2 == 0 else -0.4
        conn.execute(
            "INSERT INTO paper_trades "
            "(ticker, strategy, universe, direction, entry_date, entry_price, shares, "
            " exit_date, exit_price, pnl, pnl_pct, status, superseded) "
            "VALUES (?, 'mean_reversion', 'sp500', 'long', ?, 100.0, 10, ?, 101.0, 10.0, ?, 'closed', 0)",
            (f"MR{i:02d}", _date(4), _date(2), pnl_pct),
        )

    # ── live trades — closed in last 30d for momentum_breakout/sp500 ─────────
    # Use alternating [2.5, 0.5] so variance is non-zero → live_sharpe computable
    for i in range(4):
        pnl_pct = 2.5 if i % 2 == 0 else 0.5
        conn.execute(
            "INSERT INTO trades "
            "(ticker, strategy, universe, direction, entry_date, entry_price, shares, "
            " exit_date, exit_price, pnl, pnl_pct, status, superseded) "
            "VALUES (?, 'momentum_breakout', 'sp500', 'long', ?, 200.0, 5, ?, 205.0, 25.0, ?, 'closed', 0)",
            (f"MB{i:02d}", _date(10), _date(5), pnl_pct),
        )

    conn.commit()
    conn.close()
    return db_path


@pytest.fixture()
def client(seeded_db: Path) -> TestClient:
    """FastAPI TestClient with auth dependency overridden."""
    from services.chat_server import app
    from services.auth import check_auth

    app.dependency_overrides[check_auth] = lambda: HTTPBasicCredentials(
        username="testuser", password="testpass"
    )
    yield TestClient(app, raise_server_exceptions=True)
    app.dependency_overrides.clear()


# ════════════════════════════════════════════════════════════════════════
# 1. GET /api/strategy-lifecycle — shape check
# ════════════════════════════════════════════════════════════════════════

class TestGetLifecycleList:
    def test_returns_all_rows(self, client: TestClient) -> None:
        resp = client.get("/api/strategy-lifecycle")
        assert resp.status_code == 200
        data = resp.json()
        assert "rows" in data
        assert len(data["rows"]) == 3  # momentum_breakout, mean_reversion, adx_trend_pullback

    def test_rows_have_required_fields(self, client: TestClient) -> None:
        resp = client.get("/api/strategy-lifecycle")
        assert resp.status_code == 200
        row = resp.json()["rows"][0]
        for field in ("strategy", "universe", "state", "entered_state_at"):
            assert field in row, f"Missing field: {field}"

    # ── Test 2: research_sharpe enrichment ────────────────────────────────────
    def test_enriches_research_sharpe(self, client: TestClient) -> None:
        resp = client.get("/api/strategy-lifecycle")
        assert resp.status_code == 200
        rows_by_key = {
            f"{r['strategy']}/{r['universe']}": r for r in resp.json()["rows"]
        }
        # momentum_breakout/sp500 has research_best.sharpe=1.20
        assert rows_by_key["momentum_breakout/sp500"]["research_sharpe"] == pytest.approx(1.20, abs=1e-3)
        # mean_reversion/sp500 has research_best.sharpe=0.80
        assert rows_by_key["mean_reversion/sp500"]["research_sharpe"] == pytest.approx(0.80, abs=1e-3)
        # adx_trend_pullback/sp500 has NO research_best row → None
        assert rows_by_key["adx_trend_pullback/sp500"]["research_sharpe"] is None

    # ── Test 3: PAPER state enrichment ────────────────────────────────────────
    def test_enriches_paper_metrics_for_paper_state(self, client: TestClient) -> None:
        resp = client.get("/api/strategy-lifecycle")
        assert resp.status_code == 200
        rows_by_key = {
            f"{r['strategy']}/{r['universe']}": r for r in resp.json()["rows"]
        }
        mr = rows_by_key["mean_reversion/sp500"]
        assert mr["state"] == "PAPER"
        assert mr["paper_trades_count"] == 6
        assert mr["paper_sharpe"] is not None
        assert mr["days_in_paper"] is not None
        assert mr["days_in_paper"] == pytest.approx(5.0, abs=0.5)
        # gap = |paper_sharpe - research_sharpe| / max(|research_sharpe|, 0.1)
        assert mr["gap"] is not None

    def test_live_state_has_live_metrics(self, client: TestClient) -> None:
        resp = client.get("/api/strategy-lifecycle")
        assert resp.status_code == 200
        rows_by_key = {
            f"{r['strategy']}/{r['universe']}": r for r in resp.json()["rows"]
        }
        mb = rows_by_key["momentum_breakout/sp500"]
        assert mb["state"] == "LIVE"
        assert mb["live_trades_count"] == 4
        assert mb["live_sharpe"] is not None
        # LIVE rows should not have paper_sharpe
        assert mb["paper_sharpe"] is None


# ════════════════════════════════════════════════════════════════════════
# 4. GET /api/strategy-lifecycle/{strategy}/{universe}/history
# ════════════════════════════════════════════════════════════════════════

class TestGetLifecycleHistory:
    def test_returns_transitions_newest_first(self, client: TestClient) -> None:
        resp = client.get("/api/strategy-lifecycle/mean_reversion/sp500/history")
        assert resp.status_code == 200
        data = resp.json()
        assert "history" in data
        assert len(data["history"]) == 2
        # Newest first: RESEARCH→PAPER (5d ago) before NULL→RESEARCH (30d ago)
        assert data["history"][0]["to_state"] == "PAPER"
        assert data["history"][1]["to_state"] == "RESEARCH"

    def test_history_has_required_fields(self, client: TestClient) -> None:
        resp = client.get("/api/strategy-lifecycle/mean_reversion/sp500/history")
        assert resp.status_code == 200
        entry = resp.json()["history"][0]
        for field in ("from_state", "to_state", "transitioned_at", "reason", "operator"):
            assert field in entry, f"Missing field: {field}"

    def test_empty_history_for_unknown_combo(self, client: TestClient) -> None:
        resp = client.get("/api/strategy-lifecycle/nonexistent/sp500/history")
        assert resp.status_code == 200
        assert resp.json()["history"] == []


# ════════════════════════════════════════════════════════════════════════
# 5. POST /api/strategy-lifecycle/transition — allowed move
# ════════════════════════════════════════════════════════════════════════

class TestPostTransition:
    # ── Test 5: allowed move (RESEARCH → PAPER) ────────────────────────────────
    def test_allowed_move_succeeds(self, client: TestClient) -> None:
        payload = {
            "strategy": "adx_trend_pullback",
            "universe": "sp500",
            "new_state": "PAPER",
            "reason": "Operator promotion via dashboard test",
        }
        resp = client.post("/api/strategy-lifecycle/transition", json=payload)
        assert resp.status_code == 200
        data = resp.json()
        assert data["transitioned"] is True
        assert data["from_state"] == "RESEARCH"
        assert data["to_state"] == "PAPER"
        assert data["operator"] == "testuser"

    def test_allowed_move_persists_to_db(self, client: TestClient, seeded_db: Path) -> None:
        """After a successful transition the DB reflects the new state."""
        payload = {
            "strategy": "adx_trend_pullback",
            "universe": "sp500",
            "new_state": "PAPER",
            "reason": "Testing persistence in DB",
        }
        resp = client.post("/api/strategy-lifecycle/transition", json=payload)
        assert resp.status_code == 200

        conn = sqlite3.connect(str(seeded_db))
        row = conn.execute(
            "SELECT state FROM strategy_lifecycle WHERE strategy=? AND universe=?",
            ("adx_trend_pullback", "sp500"),
        ).fetchone()
        conn.close()
        assert row is not None
        assert row[0] == "PAPER"

    # ── Test 6: disallowed move without force → 400 ───────────────────────────
    def test_disallowed_move_without_force_returns_400(self, client: TestClient) -> None:
        # RESEARCH → LIVE is not in ALLOWED_TRANSITIONS[RESEARCH]
        payload = {
            "strategy": "adx_trend_pullback",
            "universe": "sp500",
            "new_state": "LIVE",
            "reason": "Trying to skip PAPER phase",
            "force": False,
        }
        resp = client.post("/api/strategy-lifecycle/transition", json=payload)
        assert resp.status_code == 400
        detail = resp.json()["detail"]
        assert "LIVE" in detail
        assert "force=true" in detail

    # ── Test 7: disallowed + force=true → 200 with audit warning ──────────────
    def test_force_true_allows_disallowed_move(
        self, client: TestClient, caplog: pytest.LogCaptureFixture
    ) -> None:
        import logging

        payload = {
            "strategy": "adx_trend_pullback",
            "universe": "sp500",
            "new_state": "LIVE",
            "reason": "Emergency force override from dashboard",
            "force": True,
        }
        with caplog.at_level(logging.WARNING):
            resp = client.post("/api/strategy-lifecycle/transition", json=payload)
        assert resp.status_code == 200
        data = resp.json()
        assert data["transitioned"] is True
        assert data["to_state"] == "LIVE"
        # strategy_lifecycle.py logs a WARNING on manual override
        assert any("MANUAL OVERRIDE" in r.message for r in caplog.records), (
            "Expected MANUAL OVERRIDE warning in log — check monitor/strategy_lifecycle.py"
        )

    def test_invalid_state_returns_400(self, client: TestClient) -> None:
        payload = {
            "strategy": "adx_trend_pullback",
            "universe": "sp500",
            "new_state": "UNKNOWN_STATE",
            "reason": "Invalid state test",
        }
        resp = client.post("/api/strategy-lifecycle/transition", json=payload)
        assert resp.status_code == 400
        assert "Invalid state" in resp.json()["detail"]


# ════════════════════════════════════════════════════════════════════════
# 8–9. POST /api/strategy-lifecycle/promote-paper
# ════════════════════════════════════════════════════════════════════════

class TestPostPromotePaper:
    # ── Test 9: gate failures (most PAPER combos won't pass Gate A/B in tests) ─
    def test_returns_gate_breakdown_when_gates_fail(self, client: TestClient) -> None:
        """mean_reversion/sp500 has only 5d in paper + 5 trades → Gates A+B fail."""
        payload = {"strategy": "mean_reversion", "universe": "sp500"}
        resp = client.post("/api/strategy-lifecycle/promote-paper", json=payload)
        assert resp.status_code == 200
        data = resp.json()
        assert data["promoted"] is False
        assert "gates" in data
        assert data["gates"].get("A") == "FAIL"   # only 5d in paper, need 30
        assert data["gates"].get("B") == "FAIL"   # only 5 trades, need 30

    def test_returns_not_in_paper_for_non_paper_combo(self, client: TestClient) -> None:
        """LIVE strategy cannot be promoted via promote-paper — should return promoted=False."""
        payload = {"strategy": "momentum_breakout", "universe": "sp500"}
        resp = client.post("/api/strategy-lifecycle/promote-paper", json=payload)
        assert resp.status_code == 200
        data = resp.json()
        assert data["promoted"] is False
        assert "PAPER" in (data.get("reason") or "")

    # ── Test 8: clean gates → promotion fires ─────────────────────────────────
    def test_clean_gates_trigger_promotion(
        self, seeded_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Seed a combo that passes all gates and verify it promotes."""
        import db.atlas_db as _adb
        monkeypatch.setattr(_adb, "_db_path_override", str(seeded_db))

        conn = sqlite3.connect(str(seeded_db))
        conn.execute("PRAGMA journal_mode=WAL")

        # Seed a PAPER combo entered 35 days ago
        conn.execute(
            "INSERT OR REPLACE INTO strategy_lifecycle "
            "(strategy, universe, state, entered_state_at, paper_start_date, prev_state) "
            "VALUES ('clean_strategy', 'sp500', 'PAPER', ?, ?, 'RESEARCH')",
            (
                (datetime.now(timezone.utc) - timedelta(days=35)).isoformat(),
                (datetime.now(timezone.utc) - timedelta(days=35)).strftime("%Y-%m-%d"),
            ),
        )

        # Seed 35 paper trades with consistent positive returns (Sharpe > 0.3)
        # Using pnl_pct alternating [1.6, -0.4] gives deterministic Sharpe ≈ 0.62
        for i in range(35):
            pnl = 1.6 if i % 2 == 0 else -0.4
            conn.execute(
                "INSERT INTO paper_trades "
                "(ticker, strategy, universe, direction, entry_date, entry_price, shares, "
                " exit_date, exit_price, pnl, pnl_pct, status, superseded) "
                "VALUES (?, 'clean_strategy', 'sp500', 'long', ?, 100.0, 10, ?, 101.0, 10.0, ?, 'closed', 0)",
                (
                    f"CLN{i:03d}",
                    (datetime.now(timezone.utc) - timedelta(days=25 + i % 10)).strftime("%Y-%m-%d"),
                    (datetime.now(timezone.utc) - timedelta(days=2)).strftime("%Y-%m-%d"),
                    pnl,
                ),
            )

        # Seed research_best with Sharpe ≈ 0.65 (close to paper Sharpe → Gate D passes)
        conn.execute(
            "INSERT OR REPLACE INTO research_best "
            "(strategy, universe, regime_state, params, sharpe, trades, max_dd_pct, metric_type) "
            "VALUES ('clean_strategy', 'sp500', NULL, '{}', 0.65, 50, 5.0, 'sharpe')",
        )
        conn.commit()
        conn.close()

        from services.chat_server import app
        from services.auth import check_auth

        app.dependency_overrides[check_auth] = lambda: HTTPBasicCredentials(
            username="testuser", password="testpass"
        )
        try:
            # Suppress Telegram during test
            with patch("utils.telegram.notify"):
                test_client = TestClient(app, raise_server_exceptions=True)
                resp = test_client.post(
                    "/api/strategy-lifecycle/promote-paper",
                    json={"strategy": "clean_strategy", "universe": "sp500"},
                )
        finally:
            app.dependency_overrides.clear()

        assert resp.status_code == 200
        data = resp.json()
        assert data["promoted"] is True, f"Expected promoted=True, got: {data}"
        assert data["gates"]["A"] == "PASS"
        assert data["gates"]["B"] == "PASS"
        assert data["gates"]["C"] == "PASS"
        assert data["research_sharpe"] == pytest.approx(0.65, abs=1e-3)

        # Verify state changed in DB
        conn2 = sqlite3.connect(str(seeded_db))
        row = conn2.execute(
            "SELECT state FROM strategy_lifecycle WHERE strategy=? AND universe=?",
            ("clean_strategy", "sp500"),
        ).fetchone()
        conn2.close()
        assert row is not None and row[0] == "LIVE", f"Expected LIVE, got {row}"


# ════════════════════════════════════════════════════════════════════════
# 10. Auth required
# ════════════════════════════════════════════════════════════════════════

class TestAuthRequired:
    def test_unauthenticated_get_returns_401(self) -> None:
        """Without overriding check_auth dependency, real auth is required."""
        from services.chat_server import app

        # Make sure no override is active
        app.dependency_overrides.clear()
        unauthenticated_client = TestClient(app, raise_server_exceptions=False)
        resp = unauthenticated_client.get("/api/strategy-lifecycle")
        assert resp.status_code == 401

    def test_unauthenticated_post_transition_returns_401(self) -> None:
        from services.chat_server import app

        app.dependency_overrides.clear()
        unauthenticated_client = TestClient(app, raise_server_exceptions=False)
        resp = unauthenticated_client.post(
            "/api/strategy-lifecycle/transition",
            json={
                "strategy": "x",
                "universe": "sp500",
                "new_state": "PAPER",
                "reason": "test",
            },
        )
        assert resp.status_code == 401
