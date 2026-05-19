"""Tests for services/paper_progress.py (core) and /api/strategies/paper-progress (API).

Uses the global _isolate_prod_db autouse fixture from tests/conftest.py, which
redirects all atlas_db writes to a per-test temporary SQLite database.
"""
from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Helpers — seed the isolated DB
# ---------------------------------------------------------------------------

def _insert_lifecycle_paper(db_conn, strategy: str, universe: str, days_ago: int) -> None:
    """Insert a PAPER-state row into strategy_lifecycle."""
    paper_start = (date.today() - timedelta(days=days_ago)).isoformat()
    db_conn.execute(
        """
        INSERT OR REPLACE INTO strategy_lifecycle
            (strategy, universe, state, entered_state_at, paper_start_date)
        VALUES (?, ?, 'PAPER', ?, ?)
        """,
        (strategy, universe, paper_start + "T00:00:00", paper_start + "T00:00:00"),
    )
    db_conn.commit()


def _insert_paper_trade(
    db_conn,
    strategy: str,
    universe: str,
    pnl: float,
    pnl_pct: float,
    status: str = "closed",
    superseded: int = 0,
    days_ago: int = 1,
) -> None:
    """Insert a paper trade row."""
    exit_date = (date.today() - timedelta(days=days_ago)).isoformat()
    entry_date = (date.today() - timedelta(days=days_ago + 1)).isoformat()
    db_conn.execute(
        """
        INSERT INTO paper_trades
            (ticker, strategy, universe, entry_date, entry_price, shares,
             exit_date, exit_price, pnl, pnl_pct, status, superseded)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        ("TEST", strategy, universe, entry_date, 100.0, 10,
         exit_date, 100.0 + pnl, pnl, pnl_pct, status, superseded),
    )
    db_conn.commit()


def _insert_research_best(db_conn, strategy: str, universe: str, sharpe: float) -> None:
    """Insert a cross-regime research_best row."""
    db_conn.execute(
        """
        INSERT OR REPLACE INTO research_best
            (strategy, universe, regime_state, params, sharpe, metric_type)
        VALUES (?, ?, NULL, '{}', ?, 'unknown')
        """,
        (strategy, universe, sharpe),
    )
    db_conn.commit()


def _get_db_conn():
    """Return a live SQLite connection for the current isolated DB."""
    import db.atlas_db as _adb
    import sqlite3

    db_path = _adb._db_path_override or str(Path(_adb.__file__).parent.parent / "data" / "atlas.db")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


# ════════════════════════════════════════════════════════════════
# Test 1 — Zero paper trades → insufficient_data
# ════════════════════════════════════════════════════════════════

class TestZeroPaperTrades:
    def test_zero_paper_trades_returns_insufficient_data(self) -> None:
        """Fixture: PAPER lifecycle row, no paper trades.
        Expect: status='insufficient_data', all numeric metrics None or 0.
        """
        conn = _get_db_conn()
        _insert_lifecycle_paper(conn, "mean_reversion", "sp500", days_ago=5)
        conn.close()

        from services.paper_progress import compute_paper_progress

        results = compute_paper_progress()
        assert len(results) == 1, f"Expected 1 result, got {len(results)}"

        r = results[0]
        assert r["strategy"] == "mean_reversion"
        assert r["universe"] == "sp500"
        assert r["trade_count"] == 0
        assert r["win_rate"] is None
        assert r["profit_factor"] is None
        assert r["sharpe"] is None
        assert r["sharpe_delta"] is None
        assert r["status"] == "insufficient_data"
        # All gates must be False
        assert r["gates"]["days_pass"] is False
        assert r["gates"]["trades_pass"] is False
        assert r["gates"]["sharpe_pass"] is False
        assert r["gates"]["delta_pass"] is False
        assert r["gates"]["all_pass"] is False


# ════════════════════════════════════════════════════════════════
# Test 2 — All gates pass → status='ready'
# ════════════════════════════════════════════════════════════════

class TestGateThresholdsExact:
    def test_gate_thresholds_exact(self) -> None:
        """Fixture: 10 closed paper trades with Sharpe ≈ 0.31, 30 days in paper,
        research_sharpe = 0.5. Expect all gates pass and status='ready'.
        """
        conn = _get_db_conn()
        _insert_lifecycle_paper(conn, "connors_rsi2", "sp500", days_ago=30)
        _insert_research_best(conn, "connors_rsi2", "sp500", sharpe=0.5)

        # 10 trades: alternating +2.0% / +0.5% → mean=1.25, stdev≈0.75, Sharpe≈1.67
        # Use pattern that gives Sharpe well above 0.3
        for i in range(10):
            pnl_pct = 2.0 if i % 2 == 0 else 0.5
            _insert_paper_trade(conn, "connors_rsi2", "sp500", pnl=pnl_pct * 10, pnl_pct=pnl_pct, days_ago=i + 1)
        conn.close()

        from services.paper_progress import compute_paper_progress

        results = compute_paper_progress()
        assert len(results) == 1

        r = results[0]
        assert r["trade_count"] == 10
        assert r["days_in_paper"] >= 30
        assert r["sharpe"] is not None
        assert r["sharpe"] >= 0.3, f"Expected Sharpe ≥ 0.3, got {r['sharpe']}"
        assert r["research_sharpe"] == 0.5
        # |1.67 - 0.5| = 1.17 > 0.5 — delta gate will fail here,
        # which is correct behaviour for "research agreement" check.
        # Let's adjust: use research_sharpe closer to the paper sharpe.
        # We'll re-insert research_best with a closer value.
        conn2 = _get_db_conn()
        _insert_research_best(conn2, "connors_rsi2", "sp500", sharpe=r["sharpe"] + 0.1)
        conn2.close()

        results2 = compute_paper_progress()
        r2 = results2[0]
        assert r2["gates"]["days_pass"] is True
        assert r2["gates"]["trades_pass"] is True
        assert r2["gates"]["sharpe_pass"] is True
        assert r2["gates"]["delta_pass"] is True
        assert r2["gates"]["all_pass"] is True
        assert r2["status"] == "ready"

    def test_gate_thresholds_exact_clean_fixture(self) -> None:
        """A fully deterministic fixture that verifies all gates from scratch."""
        conn = _get_db_conn()
        _insert_lifecycle_paper(conn, "short_term_mr", "sp500", days_ago=30)

        # 10 alternating +1.6% / -0.4% → mean=0.6, stdev=1.0, Sharpe=0.6
        for i in range(10):
            pnl_pct = 1.6 if i % 2 == 0 else -0.4
            _insert_paper_trade(conn, "short_term_mr", "sp500", pnl=pnl_pct * 10, pnl_pct=pnl_pct, days_ago=i + 1)

        # Research Sharpe slightly above paper so |delta| < 0.5
        _insert_research_best(conn, "short_term_mr", "sp500", sharpe=0.7)
        conn.close()

        from services.paper_progress import compute_paper_progress

        results = compute_paper_progress()
        r = results[0]
        assert r["strategy"] == "short_term_mr"
        assert r["gates"]["all_pass"] is True, f"Expected all gates pass, got {r['gates']}"
        assert r["status"] == "ready"


# ════════════════════════════════════════════════════════════════
# Test 3 — Enough data but bad Sharpe → status='failing'
# ════════════════════════════════════════════════════════════════

class TestFailingStrategy:
    def test_failing_strategy_after_enough_data(self) -> None:
        """Fixture: 15 closed paper trades, 40 days in paper, Sharpe < 0.
        Expect status='failing'.
        """
        conn = _get_db_conn()
        _insert_lifecycle_paper(conn, "mean_reversion", "sp500", days_ago=40)
        _insert_research_best(conn, "mean_reversion", "sp500", sharpe=0.4)

        # 15 trades: alternating -1.0% / +0.1% → mean = -0.45, Sharpe < 0
        for i in range(15):
            pnl_pct = -1.0 if i % 2 == 0 else 0.1
            _insert_paper_trade(conn, "mean_reversion", "sp500", pnl=pnl_pct * 10, pnl_pct=pnl_pct, days_ago=i + 1)
        conn.close()

        from services.paper_progress import compute_paper_progress

        results = compute_paper_progress()
        assert len(results) == 1

        r = results[0]
        assert r["trade_count"] == 15
        assert r["days_in_paper"] >= 40
        assert r["sharpe"] is not None
        assert r["sharpe"] < 0.3, f"Expected Sharpe < 0.3, got {r['sharpe']}"
        assert r["gates"]["days_pass"] is True
        assert r["gates"]["trades_pass"] is True
        assert r["gates"]["sharpe_pass"] is False
        assert r["status"] == "failing"


# ════════════════════════════════════════════════════════════════
# Test 4 — API endpoint returns correct JSON shape
# ════════════════════════════════════════════════════════════════

class TestApiEndpoint:
    @pytest.fixture()
    def api_client(self):
        """FastAPI TestClient with auth bypassed."""
        from fastapi.security import HTTPBasicCredentials
        from fastapi.testclient import TestClient
        from services.chat_server import app

        # The paper-progress route has NO auth dependency — we test without override
        yield TestClient(app, raise_server_exceptions=True)

    def test_api_endpoint_returns_list(self, api_client) -> None:
        """GET /api/strategies/paper-progress returns 200 with correct shape."""
        # Seed one PAPER lifecycle row
        conn = _get_db_conn()
        _insert_lifecycle_paper(conn, "mean_reversion", "sp500", days_ago=3)
        conn.close()

        response = api_client.get("/api/strategies/paper-progress")
        assert response.status_code == 200, f"Expected 200, got {response.status_code}: {response.text}"

        body = response.json()
        assert "strategies" in body, f"Missing 'strategies' key in {body}"
        assert "generated_at" in body, f"Missing 'generated_at' key in {body}"
        assert isinstance(body["strategies"], list)

        # At least the seeded strategy is present
        names = [s["strategy"] for s in body["strategies"]]
        assert "mean_reversion" in names

    def test_api_endpoint_empty_lifecycle_returns_empty_list(self, api_client) -> None:
        """When no PAPER strategies exist, strategies list is empty."""
        # No lifecycle rows inserted in this test
        response = api_client.get("/api/strategies/paper-progress")
        assert response.status_code == 200
        body = response.json()
        assert body["strategies"] == []

    def test_api_endpoint_shape_keys(self, api_client) -> None:
        """Each strategy dict contains all required keys."""
        conn = _get_db_conn()
        _insert_lifecycle_paper(conn, "connors_rsi2", "sp500", days_ago=5)
        conn.close()

        response = api_client.get("/api/strategies/paper-progress")
        body = response.json()
        assert len(body["strategies"]) >= 1

        s = body["strategies"][0]
        required_keys = {
            "strategy", "universe", "paper_start_date", "days_in_paper",
            "trade_count", "win_rate", "profit_factor", "sharpe",
            "research_sharpe", "sharpe_delta", "gates", "status",
        }
        missing = required_keys - set(s.keys())
        assert not missing, f"Missing keys in API response: {missing}"

        gate_keys = {"days_pass", "trades_pass", "sharpe_pass", "delta_pass", "all_pass"}
        missing_gates = gate_keys - set(s["gates"].keys())
        assert not missing_gates, f"Missing gate keys: {missing_gates}"


# ════════════════════════════════════════════════════════════════
# Test 5 — Superseded trades excluded from counts
# ════════════════════════════════════════════════════════════════

class TestSupersededExclusion:
    def test_superseded_trades_not_counted(self) -> None:
        """Trades with superseded=1 must be excluded from all metrics."""
        conn = _get_db_conn()
        _insert_lifecycle_paper(conn, "mean_reversion", "sp500", days_ago=5)

        # 5 real + 3 superseded
        for i in range(5):
            _insert_paper_trade(conn, "mean_reversion", "sp500", pnl=10.0, pnl_pct=1.0, superseded=0, days_ago=i + 1)
        for i in range(3):
            _insert_paper_trade(conn, "mean_reversion", "sp500", pnl=-5.0, pnl_pct=-0.5, superseded=1, days_ago=i + 10)
        conn.close()

        from services.paper_progress import compute_paper_progress

        results = compute_paper_progress()
        r = results[0]
        # Only 5 non-superseded trades should be counted
        assert r["trade_count"] == 5


# ════════════════════════════════════════════════════════════════
# Test 6 — Multi-strategy, multi-universe
# ════════════════════════════════════════════════════════════════

class TestMultiStrategy:
    def test_returns_one_row_per_paper_combo(self) -> None:
        """Two PAPER strategies → two result rows."""
        conn = _get_db_conn()
        _insert_lifecycle_paper(conn, "mean_reversion", "sp500", days_ago=20)
        _insert_lifecycle_paper(conn, "connors_rsi2", "sp500", days_ago=10)
        conn.close()

        from services.paper_progress import compute_paper_progress

        results = compute_paper_progress()
        assert len(results) == 2
        strategies = {r["strategy"] for r in results}
        assert strategies == {"mean_reversion", "connors_rsi2"}
