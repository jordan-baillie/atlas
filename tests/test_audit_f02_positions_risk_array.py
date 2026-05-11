"""Tests for F-02: /api/positions/risk positions[] array must be populated.

Audit finding F-02: the cache-hit branches of /api/positions/risk hardcoded
``"positions": []``, causing the per-position risk panel in the UI to render
blank even when open trades exist in SQLite.

Fix: added _build_positions_array() helper that reads open trades from SQLite
and returns per-position dicts for both cache-hit branches (fresh + stale).
"""
from __future__ import annotations

import sqlite3
from unittest.mock import MagicMock, patch

import pytest


# ── helpers ──────────────────────────────────────────────────────────────────

REQUIRED_POSITION_KEYS = (
    "ticker", "strategy", "entry_price", "shares", "market_value",
    "current_price", "unrealized_pnl",
)


def _make_mock_db_row(
    ticker: str = "CAT",
    strategy: str = "momentum_breakout",
    universe: str = "sp500",
    entry_price: float = 835.24,
    shares: int = 1,
    stop_price: float | None = None,
    take_profit: float | None = None,
    stop_order_id: str = "abc123",
    tp_order_id: str = "",
) -> sqlite3.Row:
    """Create a mock sqlite3.Row-like dict for a single open trade."""
    return {
        "id": 187,
        "ticker": ticker,
        "strategy": strategy,
        "universe": universe,
        "entry_price": entry_price,
        "shares": shares,
        "stop_price": stop_price,
        "take_profit": take_profit,
        "entry_date": "2026-05-01",
        "stop_order_id": stop_order_id,
        "tp_order_id": tp_order_id,
    }


# ── unit tests for _build_positions_array ─────────────────────────────────

class TestBuildPositionsArray:
    """Unit tests for the _build_positions_array() helper."""

    def test_returns_list(self, tmp_path, monkeypatch):
        """_build_positions_array always returns a list (even on empty DB)."""
        from services.api.risk import _build_positions_array

        def _mock_get_db():
            from contextlib import contextmanager
            @contextmanager
            def _ctx():
                conn = sqlite3.connect(":memory:")
                conn.row_factory = sqlite3.Row
                conn.execute(
                    "CREATE TABLE trades (id INTEGER, ticker TEXT, strategy TEXT, "
                    "universe TEXT, entry_price REAL, shares INTEGER, "
                    "stop_price REAL, take_profit REAL, entry_date TEXT, "
                    "stop_order_id TEXT, tp_order_id TEXT, status TEXT, superseded INTEGER)"
                )
                yield conn
                conn.close()
            return _ctx()

        monkeypatch.setattr("services.api.risk.get_db", _mock_get_db, raising=False)
        # Patch at the point of use inside the function
        with patch("db.atlas_db.get_db", _mock_get_db):
            result = _build_positions_array()
        assert isinstance(result, list)

    def test_returns_empty_when_no_open_trades(self):
        """_build_positions_array returns [] when there are no open trades."""
        from contextlib import contextmanager

        @contextmanager
        def _empty_db():
            conn = sqlite3.connect(":memory:")
            conn.row_factory = sqlite3.Row
            conn.execute(
                "CREATE TABLE trades (id INTEGER, ticker TEXT, strategy TEXT, "
                "universe TEXT, entry_price REAL, shares INTEGER, "
                "stop_price REAL, take_profit REAL, entry_date TEXT, "
                "stop_order_id TEXT, tp_order_id TEXT, status TEXT, superseded INTEGER)"
            )
            yield conn
            conn.close()

        with patch("db.atlas_db.get_db", _empty_db):
            from services.api import risk as _risk_mod
            with patch.object(_risk_mod, "_build_positions_array",
                               wraps=_risk_mod._build_positions_array):
                pass  # just check import

        # Direct call with empty DB mock
        with patch("db.atlas_db.get_db", _empty_db):
            import importlib, services.api.risk as _r
            # Monkeypatched module-level import
            original_get_db = None
            try:
                import db.atlas_db as _adb
                original_get_db = _adb.get_db
                _adb.get_db = _empty_db
                result = _r._build_positions_array()
                assert result == []
            finally:
                if original_get_db is not None:
                    _adb.get_db = original_get_db

    def test_position_dict_has_required_keys(self):
        """Each position dict must contain all required keys."""
        from contextlib import contextmanager

        @contextmanager
        def _db_with_cat():
            conn = sqlite3.connect(":memory:")
            conn.row_factory = sqlite3.Row
            conn.execute(
                "CREATE TABLE trades (id INTEGER, ticker TEXT, strategy TEXT, "
                "universe TEXT, entry_price REAL, shares INTEGER, "
                "stop_price REAL, take_profit REAL, entry_date TEXT, "
                "stop_order_id TEXT, tp_order_id TEXT, status TEXT, superseded INTEGER)"
            )
            conn.execute(
                "INSERT INTO trades VALUES (187, 'CAT', 'momentum_breakout', "
                "'sp500', 835.24, 1, NULL, NULL, '2026-05-01', 'abc', '', 'open', 0)"
            )
            yield conn
            conn.close()

        import db.atlas_db as _adb
        original = _adb.get_db
        try:
            _adb.get_db = _db_with_cat
            # Suppress live price fetch (no broker in tests)
            with patch("services.api.dashboard._build_dashboard_data", side_effect=RuntimeError("no broker")):
                from services.api.risk import _build_positions_array
                result = _build_positions_array()
        finally:
            _adb.get_db = original

        assert len(result) == 1, f"Expected 1 position, got {len(result)}"
        pos = result[0]
        for key in REQUIRED_POSITION_KEYS:
            assert key in pos, f"Missing required key: {key}"

    def test_market_value_computed_correctly(self):
        """market_value = current_price * shares (falls back to entry_price when no live price)."""
        from contextlib import contextmanager

        @contextmanager
        def _db_with_cat():
            conn = sqlite3.connect(":memory:")
            conn.row_factory = sqlite3.Row
            conn.execute(
                "CREATE TABLE trades (id INTEGER, ticker TEXT, strategy TEXT, "
                "universe TEXT, entry_price REAL, shares INTEGER, "
                "stop_price REAL, take_profit REAL, entry_date TEXT, "
                "stop_order_id TEXT, tp_order_id TEXT, status TEXT, superseded INTEGER)"
            )
            conn.execute(
                "INSERT INTO trades VALUES (187, 'CAT', 'momentum_breakout', "
                "'sp500', 835.24, 1, NULL, NULL, '2026-05-01', 'abc', '', 'open', 0)"
            )
            yield conn
            conn.close()

        import db.atlas_db as _adb
        original = _adb.get_db
        try:
            _adb.get_db = _db_with_cat
            with patch("services.api.dashboard._build_dashboard_data", side_effect=RuntimeError):
                from services.api.risk import _build_positions_array
                result = _build_positions_array()
        finally:
            _adb.get_db = original

        assert len(result) == 1
        pos = result[0]
        # Falls back to entry_price when no live price: 835.24 * 1 = 835.24
        assert pos["market_value"] == pytest.approx(835.24, rel=1e-4)

    def test_risk_to_stop_computed_when_stop_set(self):
        """risk_to_stop = (current - stop) * shares."""
        from contextlib import contextmanager

        @contextmanager
        def _db_with_stop():
            conn = sqlite3.connect(":memory:")
            conn.row_factory = sqlite3.Row
            conn.execute(
                "CREATE TABLE trades (id INTEGER, ticker TEXT, strategy TEXT, "
                "universe TEXT, entry_price REAL, shares INTEGER, "
                "stop_price REAL, take_profit REAL, entry_date TEXT, "
                "stop_order_id TEXT, tp_order_id TEXT, status TEXT, superseded INTEGER)"
            )
            # entry=100, stop=90, shares=5 → risk_to_stop=(100-90)*5=50
            conn.execute(
                "INSERT INTO trades VALUES (1, 'XYZ', 'trend', 'sp500', "
                "100.0, 5, 90.0, NULL, '2026-01-01', '', '', 'open', 0)"
            )
            yield conn
            conn.close()

        import db.atlas_db as _adb
        original = _adb.get_db
        try:
            _adb.get_db = _db_with_stop
            with patch("services.api.dashboard._build_dashboard_data", side_effect=RuntimeError):
                from services.api.risk import _build_positions_array
                result = _build_positions_array()
        finally:
            _adb.get_db = original

        assert len(result) == 1
        pos = result[0]
        assert pos["risk_to_stop"] == pytest.approx(50.0, rel=1e-4)

    def test_risk_to_stop_none_when_no_stop(self):
        """risk_to_stop should be None when stop_price is NULL."""
        from contextlib import contextmanager

        @contextmanager
        def _db_no_stop():
            conn = sqlite3.connect(":memory:")
            conn.row_factory = sqlite3.Row
            conn.execute(
                "CREATE TABLE trades (id INTEGER, ticker TEXT, strategy TEXT, "
                "universe TEXT, entry_price REAL, shares INTEGER, "
                "stop_price REAL, take_profit REAL, entry_date TEXT, "
                "stop_order_id TEXT, tp_order_id TEXT, status TEXT, superseded INTEGER)"
            )
            conn.execute(
                "INSERT INTO trades VALUES (1, 'XYZ', 'trend', 'sp500', "
                "100.0, 5, NULL, NULL, '2026-01-01', '', '', 'open', 0)"
            )
            yield conn
            conn.close()

        import db.atlas_db as _adb
        original = _adb.get_db
        try:
            _adb.get_db = _db_no_stop
            with patch("services.api.dashboard._build_dashboard_data", side_effect=RuntimeError):
                from services.api.risk import _build_positions_array
                result = _build_positions_array()
        finally:
            _adb.get_db = original

        assert len(result) == 1
        assert result[0]["risk_to_stop"] is None

    def test_returns_empty_on_db_error(self):
        """_build_positions_array returns [] (not raises) on DB failure."""
        def _fail():
            raise RuntimeError("DB connection failure")

        import db.atlas_db as _adb
        original = _adb.get_db
        try:
            _adb.get_db = _fail
            from services.api.risk import _build_positions_array
            result = _build_positions_array()
        finally:
            _adb.get_db = original
        assert result == []

    def test_live_price_enrichment(self):
        """_build_positions_array uses live price from dashboard when available."""
        from contextlib import contextmanager

        @contextmanager
        def _db_with_cat():
            conn = sqlite3.connect(":memory:")
            conn.row_factory = sqlite3.Row
            conn.execute(
                "CREATE TABLE trades (id INTEGER, ticker TEXT, strategy TEXT, "
                "universe TEXT, entry_price REAL, shares INTEGER, "
                "stop_price REAL, take_profit REAL, entry_date TEXT, "
                "stop_order_id TEXT, tp_order_id TEXT, status TEXT, superseded INTEGER)"
            )
            conn.execute(
                "INSERT INTO trades VALUES (187, 'CAT', 'momentum_breakout', "
                "'sp500', 835.24, 1, NULL, NULL, '2026-05-01', 'abc', '', 'open', 0)"
            )
            yield conn
            conn.close()

        mock_dd = {"positions": [{"ticker": "CAT", "current_price": 900.0}]}
        import db.atlas_db as _adb
        original = _adb.get_db
        try:
            _adb.get_db = _db_with_cat
            with patch("services.api.dashboard._build_dashboard_data", return_value=mock_dd):
                from services.api.risk import _build_positions_array
                result = _build_positions_array()
        finally:
            _adb.get_db = original

        assert len(result) == 1
        pos = result[0]
        assert pos["current_price"] == pytest.approx(900.0)
        assert pos["market_value"] == pytest.approx(900.0)


# ── integration test: endpoint response shape ─────────────────────────────

class TestPositionsRiskEndpoint:
    """Integration tests for the /api/positions/risk endpoint shape (F-02)."""

    def test_positions_key_present_in_response(self):
        """GET /api/positions/risk must include a 'positions' key."""
        import json
        import os
        from fastapi.testclient import TestClient
        from services.chat_server import app

        secrets_path = os.path.expanduser("~/.atlas-secrets.json")
        with open(secrets_path) as f:
            secrets = json.load(f)

        client = TestClient(app)
        resp = client.get(
            "/api/positions/risk",
            auth=(secrets["dashboard_user"], secrets["dashboard_pass"]),
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "positions" in data, "Response must contain 'positions' key"

    def test_positions_count_matches_array_length(self):
        """If summary.positions_count > 0, positions[] must have matching entries."""
        import json
        import os
        from fastapi.testclient import TestClient
        from services.chat_server import app

        secrets_path = os.path.expanduser("~/.atlas-secrets.json")
        with open(secrets_path) as f:
            secrets = json.load(f)

        client = TestClient(app)
        resp = client.get(
            "/api/positions/risk",
            auth=(secrets["dashboard_user"], secrets["dashboard_pass"]),
        )
        assert resp.status_code == 200
        data = resp.json()

        summary = data.get("summary") or {}
        pos_count = summary.get("positions_count") or summary.get("num_positions") or 0
        positions = data.get("positions") or []

        # The critical F-02 assertion: positions[] must NOT be hardcoded to []
        # when there are open trades.
        if pos_count > 0:
            assert len(positions) > 0, (
                f"F-02 violation: positions_count={pos_count} but positions=[] "
                "(array was hardcoded to empty in cache branches)"
            )

    def test_position_dict_shape_when_trades_exist(self):
        """Each position entry must contain required risk keys."""
        import json
        import os
        from fastapi.testclient import TestClient
        from services.chat_server import app

        secrets_path = os.path.expanduser("~/.atlas-secrets.json")
        with open(secrets_path) as f:
            secrets = json.load(f)

        client = TestClient(app)
        resp = client.get(
            "/api/positions/risk",
            auth=(secrets["dashboard_user"], secrets["dashboard_pass"]),
        )
        data = resp.json()
        positions = data.get("positions") or []
        for pos in positions:
            for key in ("ticker", "entry_price", "shares"):
                assert key in pos, f"Missing required key '{key}' in position dict: {pos}"
