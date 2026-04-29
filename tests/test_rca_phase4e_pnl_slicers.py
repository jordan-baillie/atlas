#!/usr/bin/env python3
"""Tests for RCA Phase 4E — P&L endpoint slicers.

Verifies:
- /api/trades accepts market_id, strategy, sector query params
- Each filter narrows results correctly
- Sector filter uses JOIN against signals table
- Combined filters intersect (AND logic)
- /api/pnl_filter_options returns distinct values from trades + signals
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest
from fastapi.security import HTTPBasicCredentials
from fastapi.testclient import TestClient

# ── Project path ─────────────────────────────────────────────────────────────
ATLAS_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ATLAS_ROOT))


# ════════════════════════════════════════════════════════════════
# Fixtures
# ════════════════════════════════════════════════════════════════

@pytest.fixture()
def isolated_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Full Atlas-schema DB with isolation, seeded with test trades + signals."""
    import db.atlas_db as _adb

    db_path = tmp_path / "atlas_4e.db"
    monkeypatch.setattr(_adb, "_db_path_override", str(db_path))
    _adb.init_db()

    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")

    # ── Seed closed trades ────────────────────────────────────────────────────
    trades = [
        # (ticker, strategy, universe, entry_date, exit_date, entry_price, shares, pnl)
        ("AAPL", "momentum_breakout", "sp500",         "2026-01-05", "2026-01-15", 150.0, 10,  200.0),
        ("MSFT", "momentum_breakout", "sp500",         "2026-01-06", "2026-01-16", 300.0, 5,   100.0),
        ("GLD",  "trend_following",   "commodity_etfs","2026-01-07", "2026-01-17", 180.0, 8,   -50.0),
        ("SLV",  "trend_following",   "commodity_etfs","2026-01-08", "2026-01-18", 22.0,  50,   30.0),
        ("XLK",  "sector_rotation",   "sector_etfs",   "2026-01-09", "2026-01-19", 170.0, 10,   80.0),
        ("XLV",  "sector_rotation",   "sector_etfs",   "2026-01-10", "2026-01-20", 130.0, 12,  -20.0),
    ]
    for t in trades:
        conn.execute(
            """INSERT INTO trades
               (ticker, strategy, universe, entry_date, exit_date, entry_price, shares,
                pnl, status, direction, superseded)
               VALUES (?,?,?,?,?,?,?,?,'closed','long',0)""",
            t,
        )

    # ── Seed signals with sector ──────────────────────────────────────────────
    # sector → tickers mapping
    signals = [
        # (ticker, strategy, universe, sector, action)
        ("AAPL", "momentum_breakout", "sp500",         "Technology", "accepted"),
        ("MSFT", "momentum_breakout", "sp500",         "Technology", "accepted"),
        ("GLD",  "trend_following",   "commodity_etfs","Commodities","accepted"),
        ("SLV",  "trend_following",   "commodity_etfs","Commodities","accepted"),
        ("XLK",  "sector_rotation",   "sector_etfs",   "Technology", "accepted"),
        ("XLV",  "sector_rotation",   "sector_etfs",   "Healthcare", "accepted"),
    ]
    for s in signals:
        conn.execute(
            """INSERT INTO signals
               (timestamp, ticker, strategy, universe, sector, action,
                entry_price, stop_price, position_size, position_value, risk_amount, confidence)
               VALUES (datetime('now'),?,?,?,?,?,100.0,90.0,10,1000.0,100.0,0.8)""",
            s,
        )

    conn.commit()
    conn.close()
    yield db_path


@pytest.fixture()
def client(isolated_db: Path) -> TestClient:
    """FastAPI TestClient with auth bypassed."""
    from services.chat_server import app, check_auth
    app.dependency_overrides[check_auth] = lambda: HTTPBasicCredentials(
        username="test", password="test"
    )
    yield TestClient(app, raise_server_exceptions=True)
    app.dependency_overrides.clear()


# ════════════════════════════════════════════════════════════════
# Tests — /api/trades slicers
# ════════════════════════════════════════════════════════════════

class TestGetTradesNoFilter:
    def test_get_trades_no_filter_returns_all(self, client: TestClient) -> None:
        """Baseline: no filter → all 6 closed trades returned."""
        resp = client.get("/api/trades")
        assert resp.status_code == 200
        data = resp.json()
        assert "trades" in data
        assert "count" in data
        assert data["count"] == 6
        assert len(data["trades"]) == 6


class TestGetTradesMarketFilter:
    def test_get_trades_market_filter_returns_only_that_market(
        self, client: TestClient
    ) -> None:
        """market_id=sp500 → only the 2 sp500 trades (AAPL, MSFT)."""
        resp = client.get("/api/trades?market_id=sp500")
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] == 2
        tickers = {t["ticker"] for t in data["trades"]}
        assert tickers == {"AAPL", "MSFT"}

    def test_get_trades_universe_alias_works(self, client: TestClient) -> None:
        """universe= param still works as it did before (backward compat)."""
        resp = client.get("/api/trades?universe=commodity_etfs")
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] == 2
        tickers = {t["ticker"] for t in data["trades"]}
        assert tickers == {"GLD", "SLV"}

    def test_get_trades_sector_etfs_market(self, client: TestClient) -> None:
        """market_id=sector_etfs → only XLK and XLV."""
        resp = client.get("/api/trades?market_id=sector_etfs")
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] == 2
        tickers = {t["ticker"] for t in data["trades"]}
        assert tickers == {"XLK", "XLV"}


class TestGetTradesStrategyFilter:
    def test_get_trades_strategy_filter_returns_only_that_strategy(
        self, client: TestClient
    ) -> None:
        """strategy=momentum_breakout → only AAPL + MSFT."""
        resp = client.get("/api/trades?strategy=momentum_breakout")
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] == 2
        strategies = {t["strategy"] for t in data["trades"]}
        assert strategies == {"momentum_breakout"}

    def test_get_trades_trend_following_strategy(self, client: TestClient) -> None:
        """strategy=trend_following → GLD + SLV."""
        resp = client.get("/api/trades?strategy=trend_following")
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] == 2
        tickers = {t["ticker"] for t in data["trades"]}
        assert tickers == {"GLD", "SLV"}

    def test_get_trades_unknown_strategy_returns_empty(self, client: TestClient) -> None:
        """strategy=nonexistent → 0 results, not an error."""
        resp = client.get("/api/trades?strategy=nonexistent_strat")
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] == 0
        assert data["trades"] == []


class TestGetTradesSectorFilter:
    def test_get_trades_sector_filter_joins_signals_table(
        self, client: TestClient
    ) -> None:
        """sector=Technology → AAPL, MSFT, XLK (JOIN against signals.sector)."""
        resp = client.get("/api/trades?sector=Technology")
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] == 3
        tickers = {t["ticker"] for t in data["trades"]}
        assert tickers == {"AAPL", "MSFT", "XLK"}

    def test_get_trades_sector_healthcare(self, client: TestClient) -> None:
        """sector=Healthcare → only XLV."""
        resp = client.get("/api/trades?sector=Healthcare")
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] == 1
        assert data["trades"][0]["ticker"] == "XLV"

    def test_get_trades_sector_commodities(self, client: TestClient) -> None:
        """sector=Commodities → GLD + SLV."""
        resp = client.get("/api/trades?sector=Commodities")
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] == 2
        tickers = {t["ticker"] for t in data["trades"]}
        assert tickers == {"GLD", "SLV"}

    def test_get_trades_unknown_sector_returns_empty(self, client: TestClient) -> None:
        """sector=Energy → 0 results (no signals tagged Energy)."""
        resp = client.get("/api/trades?sector=Energy")
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] == 0


class TestGetTradesCombinedFilters:
    def test_get_trades_combined_filters_intersect(self, client: TestClient) -> None:
        """market_id=sp500 + strategy=momentum_breakout → AAPL + MSFT only."""
        resp = client.get("/api/trades?market_id=sp500&strategy=momentum_breakout")
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] == 2
        tickers = {t["ticker"] for t in data["trades"]}
        assert tickers == {"AAPL", "MSFT"}

    def test_get_trades_market_plus_sector_intersect(self, client: TestClient) -> None:
        """market_id=sp500 + sector=Technology → AAPL + MSFT (sp500 Tech)."""
        resp = client.get("/api/trades?market_id=sp500&sector=Technology")
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] == 2
        tickers = {t["ticker"] for t in data["trades"]}
        assert tickers == {"AAPL", "MSFT"}

    def test_get_trades_market_mismatch_returns_empty(self, client: TestClient) -> None:
        """market_id=sp500 + sector=Commodities → 0 (GLD/SLV are commodity_etfs)."""
        resp = client.get("/api/trades?market_id=sp500&sector=Commodities")
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] == 0

    def test_get_trades_limit_param_respected(self, client: TestClient) -> None:
        """limit=2 → at most 2 rows even when more match."""
        resp = client.get("/api/trades?limit=2")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["trades"]) <= 2


# ════════════════════════════════════════════════════════════════
# Tests — /api/pnl_filter_options
# ════════════════════════════════════════════════════════════════

class TestPnlFilterOptions:
    def test_pnl_filter_options_returns_distinct_values(
        self, client: TestClient
    ) -> None:
        """Endpoint returns 3 sorted lists: markets, strategies, sectors."""
        resp = client.get("/api/pnl_filter_options")
        assert resp.status_code == 200
        data = resp.json()

        assert "markets" in data
        assert "strategies" in data
        assert "sectors" in data

        # markets — distinct universe values from closed trades
        assert set(data["markets"]) == {"sp500", "commodity_etfs", "sector_etfs"}

        # strategies — distinct strategy values from closed trades
        assert set(data["strategies"]) == {
            "momentum_breakout",
            "trend_following",
            "sector_rotation",
        }

        # sectors — from signals table (Technology appears in sp500 + sector_etfs)
        assert set(data["sectors"]) == {"Technology", "Commodities", "Healthcare"}

    def test_pnl_filter_options_markets_sorted(self, client: TestClient) -> None:
        """Markets list is alphabetically sorted."""
        resp = client.get("/api/pnl_filter_options")
        assert resp.status_code == 200
        data = resp.json()
        assert data["markets"] == sorted(data["markets"])

    def test_pnl_filter_options_strategies_sorted(self, client: TestClient) -> None:
        """Strategies list is alphabetically sorted."""
        resp = client.get("/api/pnl_filter_options")
        assert resp.status_code == 200
        data = resp.json()
        assert data["strategies"] == sorted(data["strategies"])

    def test_pnl_filter_options_sectors_sorted(self, client: TestClient) -> None:
        """Sectors list is alphabetically sorted."""
        resp = client.get("/api/pnl_filter_options")
        assert resp.status_code == 200
        data = resp.json()
        assert data["sectors"] == sorted(data["sectors"])

    def test_pnl_filter_options_empty_db(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Returns empty lists gracefully when DB has no trades/signals."""
        import db.atlas_db as _adb
        from services.chat_server import app, check_auth

        db_path = tmp_path / "empty_4e.db"
        monkeypatch.setattr(_adb, "_db_path_override", str(db_path))
        _adb.init_db()

        app.dependency_overrides[check_auth] = lambda: HTTPBasicCredentials(
            username="test", password="test"
        )
        try:
            c = TestClient(app, raise_server_exceptions=True)
            resp = c.get("/api/pnl_filter_options")
            assert resp.status_code == 200
            data = resp.json()
            assert data["markets"] == []
            assert data["strategies"] == []
            assert data["sectors"] == []
        finally:
            app.dependency_overrides.clear()
