"""Regression tests for Fix #8: N+1 DB query elimination.

Site 1: risk/stop_probability._fetch_vols_from_cones_batch — verifies
        that analyze_all_open_positions fetches all vol data in ONE query
        rather than N per-ticker queries.

Site 2: services/api/health._build_universes_list — verifies that for
        N universes the open-position COUNT code path uses ONE SQL query
        (not N separate connections).
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))


# ═══════════════════════════════════════════════════════════════
# Site 1 — risk/stop_probability batch vol fetch
# ═══════════════════════════════════════════════════════════════

class TestBatchVolFetch:
    """_fetch_vols_from_cones_batch returns all tickers in one query."""

    def _setup_vol_db(self, tmp_path, tickers: list[str]) -> Path:
        db_path = tmp_path / "sp.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("""
            CREATE TABLE vol_cones (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker TEXT NOT NULL,
                as_of TEXT NOT NULL,
                horizon INTEGER NOT NULL,
                current_vol REAL,
                UNIQUE(ticker, as_of, horizon)
            )
        """)
        for t in tickers:
            conn.execute(
                "INSERT INTO vol_cones (ticker, as_of, horizon, current_vol) VALUES (?,?,?,?)",
                (t, "2026-05-01", 20, 0.35),
            )
        conn.commit()
        conn.close()
        return db_path

    def test_batch_returns_all_tickers(self, tmp_path, monkeypatch):
        """_fetch_vols_from_cones_batch returns vol for every ticker."""
        import db.atlas_db as adb
        db_path = self._setup_vol_db(tmp_path, ["AAPL", "MSFT", "NVDA"])
        monkeypatch.setattr(adb, "_db_path_override", str(db_path))
        monkeypatch.setattr(adb, "_wal_initialized_paths", set())

        from risk.stop_probability import _fetch_vols_from_cones_batch
        result = _fetch_vols_from_cones_batch(["AAPL", "MSFT", "NVDA"])

        assert result["AAPL"] == pytest.approx(0.35)
        assert result["MSFT"] == pytest.approx(0.35)
        assert result["NVDA"] == pytest.approx(0.35)

    def test_batch_missing_ticker_returns_none(self, tmp_path, monkeypatch):
        """Tickers not in vol_cones get None (not KeyError)."""
        import db.atlas_db as adb
        db_path = self._setup_vol_db(tmp_path, ["AAPL"])
        monkeypatch.setattr(adb, "_db_path_override", str(db_path))
        monkeypatch.setattr(adb, "_wal_initialized_paths", set())

        from risk.stop_probability import _fetch_vols_from_cones_batch
        result = _fetch_vols_from_cones_batch(["AAPL", "UNKNOWN_TICKER"])

        assert result["AAPL"] == pytest.approx(0.35)
        assert result["UNKNOWN_TICKER"] is None

    def test_batch_empty_input_returns_empty(self):
        """Empty ticker list returns empty dict without hitting DB."""
        from risk.stop_probability import _fetch_vols_from_cones_batch
        assert _fetch_vols_from_cones_batch([]) == {}

    def test_batch_uses_most_recent_as_of(self, tmp_path, monkeypatch):
        """Batch returns the MAX(as_of) row, not an older row."""
        import db.atlas_db as adb
        db_path = tmp_path / "sp_multi.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("""
            CREATE TABLE vol_cones (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker TEXT, as_of TEXT, horizon INTEGER, current_vol REAL,
                UNIQUE(ticker, as_of, horizon)
            )
        """)
        conn.execute("INSERT INTO vol_cones VALUES (NULL,'AAPL','2026-04-30',20,0.20)")
        conn.execute("INSERT INTO vol_cones VALUES (NULL,'AAPL','2026-05-01',20,0.40)")
        conn.commit()
        conn.close()
        monkeypatch.setattr(adb, "_db_path_override", str(db_path))
        monkeypatch.setattr(adb, "_wal_initialized_paths", set())

        from risk.stop_probability import _fetch_vols_from_cones_batch
        result = _fetch_vols_from_cones_batch(["AAPL"])
        assert result["AAPL"] == pytest.approx(0.40), (
            "Should return most-recent row (0.40), not older row (0.20)"
        )

    def test_analyze_all_positions_calls_batch_once(self, tmp_path, monkeypatch):
        """analyze_all_open_positions calls _fetch_vols_from_cones_batch exactly once."""
        import db.atlas_db as adb
        import risk.stop_probability as sp

        db_path = tmp_path / "sp_allpos.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("""
            CREATE TABLE trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker TEXT, shares INTEGER,
                entry_price REAL, stop_price REAL,
                strategy TEXT, exit_date TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE vol_cones (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker TEXT, as_of TEXT, horizon INTEGER, current_vol REAL,
                UNIQUE(ticker, as_of, horizon)
            )
        """)
        for t in ["AAPL", "MSFT", "NVDA"]:
            conn.execute(
                "INSERT INTO trades (ticker, shares, entry_price, stop_price, strategy) "
                "VALUES (?, 10, 100.0, 90.0, 'test')", (t,),
            )
            conn.execute(
                "INSERT INTO vol_cones (ticker, as_of, horizon, current_vol) VALUES (?,?,20,0.30)",
                (t, "2026-05-01"),
            )
        conn.commit()
        conn.close()

        monkeypatch.setattr(adb, "_db_path_override", str(db_path))
        monkeypatch.setattr(adb, "_wal_initialized_paths", set())

        call_count = {"n": 0}
        original_batch = sp._fetch_vols_from_cones_batch

        def _counting_batch(tickers):
            call_count["n"] += 1
            return original_batch(tickers)

        monkeypatch.setattr(sp, "_fetch_vols_from_cones_batch", _counting_batch)

        results = sp.analyze_all_open_positions()
        assert len(results) == 3
        assert call_count["n"] == 1, (
            f"Expected exactly 1 batch call for 3 positions, got {call_count['n']}. "
            "N+1 regression detected."
        )


# ═══════════════════════════════════════════════════════════════
# Site 2 — services/api/health._build_universes_list
# ═══════════════════════════════════════════════════════════════

class TestUniversesListBatch:
    """_build_universes_list uses one SQL query for N universes."""

    def _make_cfg_dir(self, tmp_path: Path, n: int) -> Path:
        cfg_dir = tmp_path / "config" / "active"
        cfg_dir.mkdir(parents=True)
        for i in range(n):
            market = f"universe_{i}"
            (cfg_dir / f"{market}.json").write_text(json.dumps({
                "market": market,
                "trading": {"mode": "passive", "live_enabled": False},
                "risk": {"starting_equity": 5000},
            }))
        return cfg_dir

    def _make_trades_db(self, tmp_path: Path) -> Path:
        db_path = tmp_path / "health.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute("CREATE TABLE trades (id INT, universe TEXT, exit_date TEXT)")
        conn.commit()
        conn.close()
        return db_path

    def test_returns_all_non_regime_entries(self, tmp_path, monkeypatch):
        """Returns one entry per config file, excluding regime.json."""
        import db.atlas_db as adb
        cfg_dir = self._make_cfg_dir(tmp_path, 3)
        (cfg_dir / "regime.json").write_text(json.dumps({"weights": {}}))
        db_path = self._make_trades_db(tmp_path)
        monkeypatch.setattr(adb, "_db_path_override", str(db_path))
        monkeypatch.setattr(adb, "_wal_initialized_paths", set())
        monkeypatch.setattr(adb, "get_latest_equity", lambda market_id: None)

        from services.api.health import _build_universes_list
        with patch("services.api.health.Path") as MockPath:
            mock_glob_results = sorted(cfg_dir.glob("*.json"))

            def _path_se(p_str):
                if "config/active" in str(p_str):
                    m = MagicMock()
                    m.glob.return_value = iter(mock_glob_results)
                    return m
                return Path(p_str)

            MockPath.side_effect = _path_se
            result = _build_universes_list()

        assert len(result) == 3  # 4 files - 1 regime = 3
        market_ids = {r["market_id"] for r in result}
        assert "universe_0" in market_ids
        assert "universe_1" in market_ids
        assert "universe_2" in market_ids
        assert "regime" not in market_ids

    def test_single_db_connection_for_multiple_universes(self, tmp_path, monkeypatch):
        """For 3 universes, at most 1 DB connection is opened for position counts."""
        import db.atlas_db as adb
        cfg_dir = self._make_cfg_dir(tmp_path, 3)
        db_path = self._make_trades_db(tmp_path)
        monkeypatch.setattr(adb, "_db_path_override", str(db_path))
        monkeypatch.setattr(adb, "_wal_initialized_paths", set())
        monkeypatch.setattr(adb, "get_latest_equity", lambda market_id: None)

        connection_opens = {"n": 0}
        real_get_db = adb.get_db

        from contextlib import contextmanager

        @contextmanager
        def _counting_get_db(*args, **kwargs):
            connection_opens["n"] += 1
            with real_get_db(*args, **kwargs) as conn:
                yield conn

        monkeypatch.setattr(adb, "get_db", _counting_get_db)

        from services.api.health import _build_universes_list
        with patch("services.api.health.Path") as MockPath:
            mock_glob_results = sorted(cfg_dir.glob("*.json"))

            def _path_se(p_str):
                if "config/active" in str(p_str):
                    m = MagicMock()
                    m.glob.return_value = iter(mock_glob_results)
                    return m
                return Path(p_str)

            MockPath.side_effect = _path_se
            _build_universes_list()

        assert connection_opens["n"] <= 1, (
            f"Expected ≤1 DB connection for position counts across 3 universes, "
            f"got {connection_opens['n']}. N+1 regression in _build_universes_list."
        )

    def test_open_positions_reflected_in_result(self, tmp_path, monkeypatch):
        """Open position counts from DB are returned in the result dict."""
        import db.atlas_db as adb
        cfg_dir = self._make_cfg_dir(tmp_path, 2)
        db_path = tmp_path / "trades.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute("CREATE TABLE trades (id INT, universe TEXT, exit_date TEXT)")
        # 2 open positions for universe_0, 0 for universe_1
        conn.execute("INSERT INTO trades VALUES (1, 'universe_0', NULL)")
        conn.execute("INSERT INTO trades VALUES (2, 'universe_0', NULL)")
        conn.commit()
        conn.close()
        monkeypatch.setattr(adb, "_db_path_override", str(db_path))
        monkeypatch.setattr(adb, "_wal_initialized_paths", set())
        monkeypatch.setattr(adb, "get_latest_equity", lambda market_id: None)

        from services.api.health import _build_universes_list
        with patch("services.api.health.Path") as MockPath:
            mock_glob_results = sorted(cfg_dir.glob("*.json"))

            def _path_se(p_str):
                if "config/active" in str(p_str):
                    m = MagicMock()
                    m.glob.return_value = iter(mock_glob_results)
                    return m
                return Path(p_str)

            MockPath.side_effect = _path_se
            result = _build_universes_list()

        by_id = {r["market_id"]: r for r in result}
        assert by_id["universe_0"]["open_positions"] == 2
        assert by_id["universe_1"]["open_positions"] == 0
