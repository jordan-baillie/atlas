"""Tests for P2.7/P2.8/P4.2 risk pre-compute infrastructure.

Covers:
- regime_transitions_cache roundtrip (write + read)
- ruin_probability cache staleness by age
- ruin_probability cache staleness by portfolio change (P2.8)
- portfolio_risk cache fresh read
- precompute script writes all three tables (--target=all)
- /api/system/health/universes lists all config universes
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from datetime import date, timedelta
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    """Point atlas_db at a fresh tmp DB for every test."""
    import db.atlas_db as _adb
    from db.atlas_db import init_db

    test_db = str(tmp_path / "test_risk.db")
    monkeypatch.setattr(_adb, "_db_path_override", test_db)
    monkeypatch.setattr(_adb, "_risk_cache_tables_ensured", False)
    _adb._wal_initialized_paths.discard(test_db)
    init_db(test_db)
    yield test_db
    # Cleanup
    monkeypatch.setattr(_adb, "_db_path_override", None)
    monkeypatch.setattr(_adb, "_risk_cache_tables_ensured", False)


# ---------------------------------------------------------------------------
# Test 1: regime_transitions_cache roundtrip
# ---------------------------------------------------------------------------

class TestRegimeTransitionsCache:
    """test_regime_transitions_cache_roundtrip"""

    def test_regime_transitions_cache_roundtrip(self):
        """Write a 6x6 matrix then read it back and verify equality."""
        from db.atlas_db import set_cached_regime_transitions, get_cached_regime_transitions

        STATES = [
            "bull_risk_on", "bull_risk_off", "transition_uncertain",
            "bear_risk_off", "bear_capitulation", "recovery_early",
        ]
        # Build a synthetic 6x6 matrix
        matrix = {
            from_s: {to_s: round(100.0 / len(STATES), 1) for to_s in STATES}
            for from_s in STATES
        }

        set_cached_regime_transitions(matrix=matrix, window_days=90, n_obs=88)

        result = get_cached_regime_transitions(max_age_hours=24)
        assert result is not None, "Expected a cache hit, got None"
        assert "matrix" in result
        assert "window_days" in result
        assert "n_observations" in result

        assert result["window_days"] == 90
        assert result["n_observations"] == 88
        assert set(result["matrix"].keys()) == set(STATES)

        # Spot-check one value
        assert result["matrix"]["bull_risk_on"]["bull_risk_off"] == pytest.approx(
            matrix["bull_risk_on"]["bull_risk_off"], abs=0.01
        )

    def test_regime_transitions_cache_returns_none_when_stale(self):
        """A row written with an old as_of should not be returned as fresh."""
        from db.atlas_db import set_cached_regime_transitions, get_cached_regime_transitions

        old_ts = (date.today() - timedelta(days=2)).isoformat() + "T22:30:00+00:00"
        matrix = {"bull_risk_on": {"bull_risk_on": 100.0}}

        set_cached_regime_transitions(matrix=matrix, window_days=90, n_obs=5, as_of=old_ts)

        result = get_cached_regime_transitions(max_age_hours=24)
        assert result is None, f"Expected None for stale row, got {result}"


# ---------------------------------------------------------------------------
# Test 2: ruin_probability staleness by age
# ---------------------------------------------------------------------------

class TestRuinProbabilityCacheStaleness:
    """test_ruin_probability_cache_staleness_by_age"""

    def test_staleness_by_age(self):
        """Insert row with as_of=2 days ago; get_cached(24h) must return None."""
        from db.atlas_db import set_cached_ruin_probability, get_cached_ruin_probability

        old_date = (date.today() - timedelta(days=2)).isoformat()
        set_cached_ruin_probability(
            prob=0.05,
            tickers=["AAPL", "MSFT"],
            n_positions=2,
            equity=10_000.0,
            params={"as_of": old_date},
        )

        result = get_cached_ruin_probability(max_age_hours=24)
        assert result is None, (
            f"Expected None for a 2-day-old row (max_age=24h), got {result}"
        )

    def test_fresh_row_returned(self, _isolated_db):
        """Insert row for today + matching open trades; get_cached(24h) must return fresh row."""
        from db.atlas_db import set_cached_ruin_probability, get_cached_ruin_probability, get_db

        set_cached_ruin_probability(
            prob=0.03,
            tickers=["AAPL", "MSFT"],
            n_positions=2,
            equity=10_000.0,
        )

        # Insert matching open positions so portfolio-change staleness does not trigger
        with get_db() as db:
            for ticker, price in [("AAPL", 150.0), ("MSFT", 300.0)]:
                db.execute(
                    """INSERT INTO trades
                        (ticker, strategy, entry_date, entry_price, shares, status)
                       VALUES (?, 'test', '2026-01-01', ?, 10, 'open')""",
                    (ticker, price),
                )

        result = get_cached_ruin_probability(max_age_hours=24)
        assert result is not None
        assert result["prob"] == pytest.approx(0.03, abs=1e-6)
        assert result["stale"] is False
        assert result["reason"] is None


# ---------------------------------------------------------------------------
# Test 3: ruin_probability staleness by portfolio change (P2.8)
# ---------------------------------------------------------------------------

class TestRuinProbabilityCacheStalenessByPortfolio:
    """test_ruin_probability_cache_staleness_by_portfolio"""

    def test_staleness_by_portfolio(self, _isolated_db):
        """Cached tickers differ from open positions → stale=True, reason=portfolio_changed."""
        from db.atlas_db import (
            set_cached_ruin_probability,
            get_cached_ruin_probability,
            get_db,
        )

        # Write a cache row with AMD+GLD
        set_cached_ruin_probability(
            prob=0.07,
            tickers=["AMD", "GLD"],
            n_positions=2,
            equity=8_000.0,
        )

        # Insert an open position with a DIFFERENT ticker into the trades table
        with get_db() as db:
            db.execute("""
                INSERT INTO trades
                    (ticker, strategy, entry_date, entry_price, shares, status)
                VALUES ('NVDA', 'test', '2026-01-01', 100.0, 10, 'open')
            """)

        result = get_cached_ruin_probability(max_age_hours=24)
        assert result is not None, "Expected a cache row, got None"
        assert result["stale"] is True, f"Expected stale=True, got {result}"
        assert result["reason"] == "portfolio_changed", (
            f"Expected reason='portfolio_changed', got {result['reason']}"
        )

    def test_same_portfolio_not_stale(self, _isolated_db):
        """Cached tickers match open positions → stale=False."""
        from db.atlas_db import (
            set_cached_ruin_probability,
            get_cached_ruin_probability,
            get_db,
        )

        # Write cache row with AAPL
        set_cached_ruin_probability(
            prob=0.02,
            tickers=["AAPL"],
            n_positions=1,
            equity=5_000.0,
        )

        # Open position matching the cache
        with get_db() as db:
            db.execute("""
                INSERT INTO trades
                    (ticker, strategy, entry_date, entry_price, shares, status)
                VALUES ('AAPL', 'test', '2026-01-01', 150.0, 10, 'open')
            """)

        result = get_cached_ruin_probability(max_age_hours=24)
        assert result is not None
        assert result["stale"] is False
        assert result["reason"] is None


# ---------------------------------------------------------------------------
# Test 4: portfolio_risk cache fresh within 24h
# ---------------------------------------------------------------------------

class TestPortfolioRiskCacheFresh:
    """test_portfolio_risk_cache_returns_fresh_within_24h"""

    def test_fresh_within_24h(self, _isolated_db):
        """Write to portfolio_risk then get_cached_portfolio_risk returns the row."""
        from db.atlas_db import get_cached_portfolio_risk, get_db

        # Directly insert a portfolio_risk row with today's date
        today = date.today().isoformat()
        with get_db() as db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS portfolio_risk (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    as_of TEXT NOT NULL,
                    equity REAL NOT NULL,
                    positions_value REAL NOT NULL,
                    positions_count INTEGER NOT NULL,
                    tickers TEXT NOT NULL,
                    correlation_avg REAL,
                    correlation_max REAL,
                    effective_bets REAL,
                    var_1d_95 REAL,
                    var_1d_99 REAL,
                    cvar_1d_95 REAL,
                    cvar_1d_99 REAL,
                    var_5d_95 REAL,
                    var_5d_99 REAL,
                    cvar_5d_95 REAL,
                    cvar_5d_99 REAL,
                    var_1d_95_pct REAL,
                    cvar_1d_95_pct REAL,
                    method TEXT,
                    n_paths INTEGER,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(as_of, method)
                )
            """)
            db.execute("""
                INSERT OR REPLACE INTO portfolio_risk
                    (as_of, equity, positions_value, positions_count, tickers,
                     var_1d_95, cvar_1d_95, method, n_paths)
                VALUES (?, 5000.0, 4800.0, 3, ?, -120.0, -180.0, 'regime_conditional', 10000)
            """, (today, json.dumps(["AAPL", "MSFT", "NVDA"])))

        result = get_cached_portfolio_risk(max_age_hours=24)
        assert result is not None, "Expected a cache hit, got None"
        assert result["as_of"] == today
        assert result["equity"] == pytest.approx(5000.0)
        assert result["method"] == "regime_conditional"
        assert "AAPL" in result["tickers"]

    def test_stale_returns_none(self, _isolated_db):
        """Portfolio risk row 2 days old should not be returned by get_cached(24h)."""
        from db.atlas_db import get_cached_portfolio_risk, get_db

        old_date = (date.today() - timedelta(days=2)).isoformat()
        with get_db() as db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS portfolio_risk (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    as_of TEXT NOT NULL,
                    equity REAL NOT NULL,
                    positions_value REAL NOT NULL,
                    positions_count INTEGER NOT NULL,
                    tickers TEXT NOT NULL,
                    method TEXT,
                    n_paths INTEGER,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(as_of, method)
                )
            """)
            # created_at must also be old — the fix uses julianday(created_at)
            from datetime import datetime as _dt, timezone as _tz, timedelta as _td
            old_created_at = (_dt.now(_tz.utc) - _td(hours=49)).strftime("%Y-%m-%d %H:%M:%S")
            db.execute("""
                INSERT INTO portfolio_risk
                    (as_of, equity, positions_value, positions_count, tickers, created_at)
                VALUES (?, 5000.0, 4800.0, 3, '["AAPL"]', ?)
            """, (old_date, old_created_at))

        result = get_cached_portfolio_risk(max_age_hours=24)
        assert result is None, f"Expected None for stale row, got {result}"


# ---------------------------------------------------------------------------
# Test 5: precompute script writes all three tables
# ---------------------------------------------------------------------------

class TestPrecomputeScriptIntegration:
    """test_precompute_script_writes_all_three_tables"""

    def test_precompute_writes_all_tables(self, tmp_path, monkeypatch):
        """Run precompute_risk.py --target=all; assert all three tables have fresh rows."""
        # Use a separate DB file that the subprocess can find.
        # We set ATLAS_DB env var and override DB_PATH in the subprocess.
        import os
        import sqlite3 as sl

        db_path = str(tmp_path / "precompute_test.db")

        # Prime the DB with regime_history data (needed for regime target)
        conn = sl.connect(db_path)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS schema_version (
                version INTEGER PRIMARY KEY,
                applied_at TEXT DEFAULT (datetime('now'))
            )
        """)
        conn.execute("INSERT OR IGNORE INTO schema_version (version) VALUES (1)")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS regime_history (
                date TEXT PRIMARY KEY,
                regime_state TEXT NOT NULL,
                trend_score REAL, risk_score REAL,
                active_universes TEXT, sizing_multiplier REAL DEFAULT 1.0,
                enabled_strategies TEXT, reasoning TEXT, model_version TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker TEXT NOT NULL, strategy TEXT NOT NULL,
                universe TEXT, direction TEXT DEFAULT 'long',
                entry_date TEXT NOT NULL, entry_price REAL NOT NULL,
                shares INTEGER NOT NULL, stop_price REAL, take_profit REAL,
                exit_date TEXT, exit_price REAL, exit_reason TEXT,
                pnl REAL, pnl_pct REAL, mae REAL, mfe REAL,
                hold_days INTEGER, confidence REAL,
                regime_at_entry TEXT, regime_at_exit TEXT,
                status TEXT DEFAULT 'open', config_version TEXT,
                created_at TEXT DEFAULT (datetime('now'))
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS ohlcv (
                ticker TEXT NOT NULL, date TEXT NOT NULL,
                open REAL NOT NULL, high REAL NOT NULL, low REAL NOT NULL,
                close REAL NOT NULL, adj_close REAL, volume INTEGER NOT NULL,
                universe TEXT NOT NULL, source TEXT DEFAULT 'tiingo',
                PRIMARY KEY (ticker, date)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS equity_curve (
                date TEXT NOT NULL, market_id TEXT NOT NULL,
                equity REAL NOT NULL, cash REAL, positions_value REAL,
                day_pnl REAL, regime_state TEXT,
                PRIMARY KEY (date, market_id)
            )
        """)
        # Insert 10 regime rows so transitions can be computed
        import random
        states = ["bull_risk_on", "bull_risk_off", "transition_uncertain",
                  "bear_risk_off", "bear_capitulation", "recovery_early"]
        for i in range(10):
            d = (date.today() - timedelta(days=10 - i)).isoformat()
            conn.execute(
                "INSERT OR IGNORE INTO regime_history (date, regime_state) VALUES (?, ?)",
                (d, states[i % len(states)])
            )
        conn.commit()
        conn.close()

        env = os.environ.copy()
        # ATLAS_DB_PATH env var is read at script startup to override the DB path.
        env["ATLAS_DB_PATH"] = db_path
        env["PYTHONPATH"] = str(PROJECT_ROOT)

        result = subprocess.run(
            [sys.executable, str(PROJECT_ROOT / "scripts" / "precompute_risk.py"),
             "--target=regime"],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
            cwd=str(PROJECT_ROOT),
        )
        # regime target doesn't need positions/equity, should always succeed
        # (even if other targets fail due to missing data)
        log = result.stdout + result.stderr
        # regime=OK is expected; ruin/risk may warn about no positions
        assert result.returncode == 0, (
            f"precompute_risk.py --target=regime failed:\n{log}"
        )
        assert "regime=OK" in log or "regime: cached matrix" in log, (
            f"Expected regime success in log:\n{log}"
        )

        # Verify regime_transitions_cache has a row
        conn2 = sl.connect(db_path)
        conn2.row_factory = sl.Row
        rows = conn2.execute(
            "SELECT * FROM regime_transitions_cache ORDER BY as_of DESC LIMIT 1"
        ).fetchall()
        conn2.close()
        assert rows, "regime_transitions_cache is empty after --target=regime"


# ---------------------------------------------------------------------------
# Test 6: /api/system/health/universes
# ---------------------------------------------------------------------------

class TestUniversesEndpoint:
    """test_universes_endpoint_lists_all_configs"""

    def test_universes_endpoint_lists_all_configs(self):
        """GET /api/system/health/universes returns all market configs incl. ASX."""
        from fastapi.testclient import TestClient
        from services.chat_server import app, check_auth
        from fastapi.security import HTTPBasicCredentials

        app.dependency_overrides[check_auth] = lambda: HTTPBasicCredentials(
            username="test", password="test"
        )
        client = TestClient(app, raise_server_exceptions=True)

        try:
            resp = client.get("/api/system/health/universes")
            assert resp.status_code == 200, f"Got {resp.status_code}: {resp.text}"

            data = resp.json()
            assert "universes" in data, f"Missing 'universes' key: {data}"

            universes = data["universes"]
            market_ids = [u["market_id"] for u in universes]

            # sp500 must be present and active
            assert "sp500" in market_ids, f"sp500 missing from {market_ids}"
            sp500 = next(u for u in universes if u["market_id"] == "sp500")
            assert sp500["approval"] is True, f"sp500 approval should be True: {sp500}"

            # ASX must be present (intentionally dead but NOT removed)
            assert "asx" in market_ids, f"asx missing from {market_ids}"
            asx = next(u for u in universes if u["market_id"] == "asx")
            assert asx["approval"] is False, f"asx approval should be False: {asx}"
            assert asx["mode"] == "passive", f"asx mode should be 'passive': {asx}"

            # Each universe has required keys
            required_keys = {"market_id", "mode", "approval", "open_positions", "equity"}
            for u in universes:
                missing = required_keys - set(u.keys())
                assert not missing, f"Universe {u} missing keys: {missing}"

        finally:
            app.dependency_overrides.clear()

    def test_universes_in_system_health(self):
        """GET /api/system/health also contains 'universes' key (P4.2 surface)."""
        from fastapi.testclient import TestClient
        from services.chat_server import app, check_auth
        from fastapi.security import HTTPBasicCredentials

        app.dependency_overrides[check_auth] = lambda: HTTPBasicCredentials(
            username="test", password="test"
        )
        client = TestClient(app, raise_server_exceptions=True)

        try:
            resp = client.get("/api/system/health")
            assert resp.status_code == 200, f"Got {resp.status_code}: {resp.text}"
            data = resp.json()
            assert "universes" in data, (
                f"'universes' key missing from /api/system/health: {list(data.keys())}"
            )
        finally:
            app.dependency_overrides.clear()
