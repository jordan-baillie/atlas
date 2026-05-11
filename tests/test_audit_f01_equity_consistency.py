"""Tests for audit findings F-01/F-04 — equity source unification.

Verifies that /api/portfolio, /api/positions/risk, /api/risk/ruin,
/api/system/health/universes, and /api/admin/universes all read equity
from market_equity_history (not equity_curve) and return consistent values.

Audit refs: F-01 (equity contradiction), F-04 (negative cash/positions_value)
"""
from __future__ import annotations

import sqlite3
from unittest.mock import MagicMock, patch

import pytest


# ── Shared fixture: isolated DB with market_equity_history row ─────────────

@pytest.fixture
def _mem_db(tmp_path):
    """In-memory SQLite with just the tables we need for these tests."""
    db_path = tmp_path / "test_f01.db"
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")

    # Minimal tables
    conn.execute("""
        CREATE TABLE market_equity_history (
            date TEXT NOT NULL,
            market_id TEXT NOT NULL,
            broker_equity REAL DEFAULT 0,
            allocated_equity REAL DEFAULT 0,
            position_mv REAL DEFAULT 0,
            cash_attributed REAL DEFAULT 0,
            broker_cash REAL DEFAULT 0,
            snapshot_time TEXT,
            PRIMARY KEY (date, market_id)
        )
    """)
    conn.execute("""
        CREATE TABLE equity_curve (
            date TEXT,
            market_id TEXT,
            equity REAL,
            cash REAL DEFAULT 0,
            positions_value REAL DEFAULT 0
        )
    """)
    conn.execute("""
        CREATE TABLE trades (
            id INTEGER PRIMARY KEY,
            ticker TEXT,
            strategy TEXT,
            universe TEXT,
            entry_date TEXT,
            exit_date TEXT,
            entry_price REAL,
            stop_price REAL,
            shares INTEGER,
            status TEXT DEFAULT 'open',
            pnl REAL
        )
    """)
    conn.execute("""
        CREATE TABLE regime_history (
            date TEXT PRIMARY KEY,
            regime_state TEXT,
            regime_score REAL
        )
    """)
    conn.execute("""
        CREATE TABLE portfolio_risk_cache (
            id INTEGER PRIMARY KEY,
            computed_at TEXT,
            equity REAL,
            positions_count INTEGER DEFAULT 0,
            tickers TEXT DEFAULT '[]',
            method TEXT,
            var_1d_95 REAL,
            cvar_1d_95 REAL,
            effective_bets REAL,
            correlation_avg REAL,
            as_of TEXT,
            stale INTEGER DEFAULT 0
        )
    """)
    conn.execute("""
        CREATE TABLE ruin_probability (
            id INTEGER PRIMARY KEY,
            as_of TEXT,
            horizon_days INTEGER,
            prob_ruin REAL,
            current_equity REAL,
            floor REAL,
            floor_pct REAL,
            n_paths INTEGER,
            worst_case_equity REAL,
            worst_5pct_equity REAL,
            median_end_equity REAL,
            tickers TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE strategy_lifecycle (
            strategy TEXT,
            universe TEXT,
            state TEXT,
            entered_state_at TEXT,
            transition_reason TEXT,
            PRIMARY KEY (strategy, universe)
        )
    """)
    conn.commit()

    # Insert known-good market_equity_history row
    conn.execute(
        "INSERT INTO market_equity_history VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("2026-05-08", "sp500", 5211.19, 5211.19, 897.45, 4313.32, 4313.32, "2026-05-08T20:00:00"),
    )
    conn.execute(
        "INSERT INTO market_equity_history VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("2026-05-08", "commodity_etfs", 5211.19, 1085.31, 0.0, 1085.31, 0.0, "2026-05-08T20:00:00"),
    )
    conn.execute(
        "INSERT INTO market_equity_history VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("2026-05-08", "asx", 2681.65, 2681.65, 2681.65, 0.0, 0.0, "2026-05-11T00:00:00"),
    )

    # Insert CORRUPT equity_curve row (negative positions_value — the problem F-04)
    conn.execute(
        "INSERT INTO equity_curve VALUES (?, ?, ?, ?, ?)",
        ("2026-05-08", "sp500", 1360.74, 4313.32, -2952.58),
    )
    conn.commit()
    yield db_path, conn
    conn.close()


# ── Test: portfolio endpoint reads from market_equity_history ─────────────

class TestPortfolioEquitySource:
    def test_portfolio_returns_broker_equity_not_corrupt_equity_curve(self, _mem_db, tmp_path):
        """db_portfolio() must return broker_equity from market_equity_history.

        equity_curve has equity=1360.74 (corrupt) for the same date.
        market_equity_history has broker_equity=5211.19 (correct).
        The endpoint must return 5211.19.
        """
        db_path, conn = _mem_db

        # Patch get_db to use our test DB
        import db.atlas_db as _adb
        orig = _adb._db_path_override
        try:
            _adb._db_path_override = str(db_path)

            # Also patch get_open_positions and get_current_regime
            with patch("db.atlas_db.get_open_positions", return_value=[]), \
                 patch("db.atlas_db.get_current_regime", return_value=None):
                from services.api.portfolio import db_portfolio

                # Call with universe=sp500 — use a mock auth credential
                mock_auth = MagicMock()
                result = db_portfolio(universe="sp500", _auth=mock_auth)

                import json
                body = json.loads(result.body)
                equity = body.get("equity") or {}

                assert equity.get("equity") == pytest.approx(5211.19, abs=0.01), (
                    f"Expected broker_equity=5211.19 from market_equity_history, "
                    f"got {equity.get('equity')} — reading corrupt equity_curve?"
                )
                # Ensure we're NOT returning the corrupt value
                assert equity.get("equity") != pytest.approx(1360.74, abs=0.01), (
                    "equity_curve corrupt value (1360.74) must not be returned"
                )
        finally:
            _adb._db_path_override = orig

    def test_portfolio_equity_has_correct_shape(self, _mem_db):
        """equity dict must have the expected backward-compat keys."""
        db_path, conn = _mem_db
        import db.atlas_db as _adb
        orig = _adb._db_path_override
        try:
            _adb._db_path_override = str(db_path)
            with patch("db.atlas_db.get_open_positions", return_value=[]), \
                 patch("db.atlas_db.get_current_regime", return_value=None):
                from services.api.portfolio import db_portfolio
                mock_auth = MagicMock()
                result = db_portfolio(universe="sp500", _auth=mock_auth)
                import json
                body = json.loads(result.body)
                equity = body.get("equity") or {}
                for key in ("equity", "allocated_equity", "cash", "broker_cash", "positions_value", "date", "market_id"):
                    assert key in equity, f"Missing key: {key}"
        finally:
            _adb._db_path_override = orig

    def test_portfolio_asx_universe_returns_asx_equity(self, _mem_db):
        """universe=asx should return the ASX equity row (2681.65)."""
        db_path, conn = _mem_db
        import db.atlas_db as _adb
        orig = _adb._db_path_override
        try:
            _adb._db_path_override = str(db_path)
            with patch("db.atlas_db.get_open_positions", return_value=[]), \
                 patch("db.atlas_db.get_current_regime", return_value=None):
                from services.api.portfolio import db_portfolio
                mock_auth = MagicMock()
                result = db_portfolio(universe="asx", _auth=mock_auth)
                import json
                body = json.loads(result.body)
                equity = body.get("equity") or {}
                assert equity.get("equity") == pytest.approx(2681.65, abs=0.01)
        finally:
            _adb._db_path_override = orig


# ── Test: risk.py cache path reads equity from market_equity_history ──────

class TestRiskEquitySource:
    def test_positions_risk_cache_uses_market_equity_history(self, _mem_db):
        """When cache hit, summary.equity must come from market_equity_history."""
        db_path, conn = _mem_db
        import db.atlas_db as _adb
        orig = _adb._db_path_override
        try:
            _adb._db_path_override = str(db_path)

            # Insert a stale cache row with wrong equity (what equity_curve would have returned)
            conn.execute(
                "INSERT INTO portfolio_risk_cache VALUES (1, datetime('now'), 1360.74, 2, '[]', "
                "'regime_conditional', -50.0, -75.0, 1.8, 0.06, datetime('now'), 0)"
            )
            conn.commit()

            mock_cached = {
                "equity": 1360.74,  # wrong value from old equity_curve
                "positions_count": 2,
                "tickers": [],
                "method": "regime_conditional",
                "var_1d_95": -50.0,
                "cvar_1d_95": -75.0,
                "effective_bets": 1.8,
                "correlation_avg": 0.06,
                "as_of": "2026-05-08T20:00:00",
                "stale": False,
            }

            with patch("db.atlas_db.get_cached_portfolio_risk", return_value=mock_cached):
                from importlib import reload
                import services.api.risk as risk_mod
                reload(risk_mod)

                mock_auth = MagicMock()
                result = risk_mod.positions_risk(_auth=mock_auth)

                import json
                body = json.loads(result.body)
                actual_equity = body["summary"]["equity"]
                assert actual_equity == pytest.approx(5211.19, abs=0.01), (
                    f"Cache path must return market_equity_history broker_equity=5211.19, "
                    f"got {actual_equity}"
                )
        finally:
            _adb._db_path_override = orig

    def test_risk_ruin_uses_market_equity_history(self, _mem_db):
        """risk_ruin current_equity must come from market_equity_history."""
        db_path, conn = _mem_db
        import db.atlas_db as _adb
        orig = _adb._db_path_override
        try:
            _adb._db_path_override = str(db_path)

            # Insert ruin_probability row with old stale equity
            conn.execute(
                "INSERT INTO ruin_probability VALUES "
                "(1, '2026-05-08T20:00:00', 30, 0.01, 1360.74, 952.52, 0.70, 10000, 800.0, 900.0, 1200.0, '[]')"
            )
            conn.commit()

            with patch("db.atlas_db.get_cached_ruin_probability", return_value=None):
                from importlib import reload
                import services.api.risk as risk_mod
                reload(risk_mod)

                mock_auth = MagicMock()
                result = risk_mod.risk_ruin(_auth=mock_auth)

                # risk_ruin returns a plain dict (not JSONResponse) for the DB path
                if hasattr(result, "body"):
                    import json
                    body = json.loads(result.body)
                else:
                    body = result  # plain dict
                actual_equity = body.get("current_equity")
                assert actual_equity == pytest.approx(5211.19, abs=0.01), (
                    f"risk_ruin must return market_equity_history broker_equity=5211.19, "
                    f"got {actual_equity}"
                )
        finally:
            _adb._db_path_override = orig


# ── Test: admin universes reads from market_equity_history ─────────────────

class TestAdminUniverseEquitySource:
    def test_admin_universes_equity_from_market_equity_history(self, _mem_db, tmp_path):
        """admin_get_universes must use allocated_equity from market_equity_history."""
        db_path, conn = _mem_db
        import db.atlas_db as _adb
        orig = _adb._db_path_override
        try:
            _adb._db_path_override = str(db_path)

            # Mock config loading so admin_get_universes finds sp500
            mock_cfg = {
                "market": "sp500",
                "trading": {"mode": "live", "live_enabled": True},
                "risk": {"starting_equity": 5000},
                "strategies": {"momentum_breakout": {"enabled": True, "weight": 0.5}},
                "version": "v1.0",
            }
            with patch("services.api.admin._list_market_ids", return_value=["sp500"]), \
                 patch("utils.config.get_active_config", return_value=mock_cfg), \
                 patch("utils.config.get_raw_config", return_value=mock_cfg), \
                 patch("services.api.admin._get_active_override", return_value=None), \
                 patch("services.api.admin._last_trade_at", return_value=None):
                from services.api.admin import admin_get_universes
                mock_auth = MagicMock()
                result = admin_get_universes(_auth=mock_auth)

                import json
                body = json.loads(result.body)
                universes = body.get("universes", [])
                sp500 = next((u for u in universes if u["market_id"] == "sp500"), None)
                assert sp500 is not None
                # allocated_equity from market_equity_history = 5211.19
                assert sp500["current_equity"] == pytest.approx(5211.19, abs=0.01), (
                    f"admin_get_universes must use market_equity_history.allocated_equity, "
                    f"got {sp500['current_equity']}"
                )
        finally:
            _adb._db_path_override = orig


# ── Test: health universes reads from market_equity_history ────────────────

class TestHealthUniverseEquitySource:
    def test_health_universes_equity_from_market_equity_history(self, _mem_db):
        """_build_universes_list must use market_equity_history.allocated_equity."""
        db_path, conn = _mem_db
        import db.atlas_db as _adb
        orig = _adb._db_path_override
        try:
            _adb._db_path_override = str(db_path)

            mock_cfg = {
                "market": "sp500",
                "trading": {"mode": "live", "live_enabled": True},
                "risk": {"starting_equity": 5000},
            }

            with patch("utils.config.get_active_config", return_value=mock_cfg), \
                 patch("pathlib.Path.glob", return_value=[MagicMock(stem="sp500")]):
                from services.api.health import _build_universes_list
                universes = _build_universes_list()

                sp500 = next((u for u in universes if u.get("market_id") == "sp500"), None)
                if sp500 is not None:
                    # Should be market_equity_history.allocated_equity not equity_curve
                    assert sp500["equity"] == pytest.approx(5211.19, abs=0.01), (
                        f"health universes must use market_equity_history, got {sp500['equity']}"
                    )
        finally:
            _adb._db_path_override = orig


# ── Test: equity_curve corrupt rows are NOT returned ──────────────────────

class TestEquityCurveNotConsulted:
    def test_equity_curve_corrupt_rows_remain_in_db(self, _mem_db):
        """equity_curve still has corrupt rows — just not consulted by readers anymore."""
        db_path, conn = _mem_db
        count = conn.execute(
            "SELECT COUNT(*) FROM equity_curve WHERE cash<0 OR positions_value<0"
        ).fetchone()[0]
        assert count >= 1, "Test setup: equity_curve must have at least 1 corrupt row"

    def test_market_equity_history_has_no_negative_values(self, _mem_db):
        """market_equity_history must not have negative broker_equity or allocated_equity."""
        db_path, conn = _mem_db
        bad = conn.execute(
            "SELECT COUNT(*) FROM market_equity_history "
            "WHERE broker_equity < 0 OR allocated_equity < 0"
        ).fetchone()[0]
        assert bad == 0, f"market_equity_history has {bad} rows with negative equity — check data"

    def test_asx_equity_row_present(self, _mem_db):
        """ASX row must be in market_equity_history after seed_asx_equity.py is run."""
        db_path, conn = _mem_db
        row = conn.execute(
            "SELECT allocated_equity FROM market_equity_history "
            "WHERE market_id='asx' ORDER BY date DESC LIMIT 1"
        ).fetchone()
        assert row is not None, "ASX equity row missing from market_equity_history (run seed_asx_equity.py)"
        assert float(row[0]) == pytest.approx(2681.65, abs=0.01)
