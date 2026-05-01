"""Tests for get_cached_portfolio_risk freshness logic (Fix #1).

Verifies that the cache staleness check uses ``created_at`` (full timestamp)
rather than ``as_of`` (date-only column) to avoid the off-by-day false-stale
bug where rows written near end-of-day appeared stale the following day.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone, timedelta

import pytest


# ── helpers ──────────────────────────────────────────────────────────────────

def _make_isolated_db(tmp_path):
    """Return a path to a fresh, schema-initialised temp SQLite DB."""
    db_path = tmp_path / "test_portfolio_risk.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE portfolio_risk (
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
    conn.commit()
    conn.close()
    return db_path


def _insert_risk_row(db_path, as_of: str, created_at: str, method: str = "test"):
    """Insert one portfolio_risk row with explicit as_of and created_at."""
    conn = sqlite3.connect(str(db_path))
    conn.execute("""
        INSERT INTO portfolio_risk
            (as_of, equity, positions_value, positions_count, tickers,
             method, n_paths, created_at)
        VALUES (?, 5000.0, 4800.0, 2, ?, ?, 1000, ?)
    """, (as_of, json.dumps(["AAPL", "MSFT"]), method, created_at))
    conn.commit()
    conn.close()


# ── tests ─────────────────────────────────────────────────────────────────────

class TestPortfolioRiskCacheCreatedAt:
    """Freshness check uses created_at, not as_of."""

    def test_fresh_row_written_5min_ago(self, tmp_path, monkeypatch):
        """Row with as_of=yesterday, created_at=5 min ago -> FRESH (under 24h)."""
        db_path = _make_isolated_db(tmp_path)
        import db.atlas_db as adb
        monkeypatch.setattr(adb, "_db_path_override", str(db_path))
        monkeypatch.setattr(adb, "_risk_cache_tables_ensured", True)

        today = datetime.now(timezone.utc)
        as_of = (today - timedelta(days=1)).strftime("%Y-%m-%d")  # yesterday
        created_at = (today - timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S")

        _insert_risk_row(db_path, as_of=as_of, created_at=created_at)

        result = adb.get_cached_portfolio_risk(max_age_hours=24)
        assert result is not None, (
            "Row with created_at=5min ago should be fresh (got None). "
            "Bug: using as_of would show ~23h+ stale."
        )
        assert result["equity"] == pytest.approx(5000.0)

    def test_stale_row_written_25h_ago(self, tmp_path, monkeypatch):
        """Row with as_of=yesterday, created_at=25h ago -> STALE."""
        db_path = _make_isolated_db(tmp_path)
        import db.atlas_db as adb
        monkeypatch.setattr(adb, "_db_path_override", str(db_path))
        monkeypatch.setattr(adb, "_risk_cache_tables_ensured", True)

        today = datetime.now(timezone.utc)
        as_of = (today - timedelta(days=1)).strftime("%Y-%m-%d")
        created_at = (today - timedelta(hours=25)).strftime("%Y-%m-%d %H:%M:%S")

        _insert_risk_row(db_path, as_of=as_of, created_at=created_at)

        result = adb.get_cached_portfolio_risk(max_age_hours=24)
        assert result is None, (
            f"Row with created_at=25h ago should be stale (got {result})."
        )

    def test_edge_as_of_today_but_created_at_23h_ago_is_fresh(self, tmp_path, monkeypatch):
        """Row with as_of=today, created_at=23h ago -> FRESH (still under 24h)."""
        db_path = _make_isolated_db(tmp_path)
        import db.atlas_db as adb
        monkeypatch.setattr(adb, "_db_path_override", str(db_path))
        monkeypatch.setattr(adb, "_risk_cache_tables_ensured", True)

        today = datetime.now(timezone.utc)
        as_of = today.strftime("%Y-%m-%d")  # today
        created_at = (today - timedelta(hours=23)).strftime("%Y-%m-%d %H:%M:%S")

        _insert_risk_row(db_path, as_of=as_of, created_at=created_at)

        result = adb.get_cached_portfolio_risk(max_age_hours=24)
        assert result is not None, (
            "Row with created_at=23h ago should be fresh under 24h window."
        )

    def test_no_rows_returns_none(self, tmp_path, monkeypatch):
        """Empty table -> None."""
        db_path = _make_isolated_db(tmp_path)
        import db.atlas_db as adb
        monkeypatch.setattr(adb, "_db_path_override", str(db_path))
        monkeypatch.setattr(adb, "_risk_cache_tables_ensured", True)

        result = adb.get_cached_portfolio_risk(max_age_hours=24)
        assert result is None

    def test_key_bug_scenario_end_of_day_write(self, tmp_path, monkeypatch):
        """The specific off-by-day bug: as_of=yesterday written at 11pm -> fresh today.

        With the old julianday(as_of) check:
            julianday(now) - julianday('2026-04-30') ~27h for a 3am 2026-05-01 'now'
            -> STALE (wrong!)

        With the fix julianday(created_at):
            julianday(now) - julianday('2026-04-30 23:00:00') ~4h
            -> FRESH (correct!)
        """
        db_path = _make_isolated_db(tmp_path)
        import db.atlas_db as adb
        monkeypatch.setattr(adb, "_db_path_override", str(db_path))
        monkeypatch.setattr(adb, "_risk_cache_tables_ensured", True)

        # Simulate: script ran yesterday at 11pm, as_of=yesterday, created_at=4h ago
        today = datetime.now(timezone.utc)
        as_of = (today - timedelta(hours=4)).strftime("%Y-%m-%d")  # may be yesterday
        created_at = (today - timedelta(hours=4)).strftime("%Y-%m-%d %H:%M:%S")  # 4h ago

        _insert_risk_row(db_path, as_of=as_of, created_at=created_at)

        result = adb.get_cached_portfolio_risk(max_age_hours=24)
        assert result is not None, (
            "Row written 4h ago should be fresh regardless of as_of date."
        )

    def test_tickers_deserialised_to_list(self, tmp_path, monkeypatch):
        """Returned dict has tickers as a Python list, not raw JSON string."""
        db_path = _make_isolated_db(tmp_path)
        import db.atlas_db as adb
        monkeypatch.setattr(adb, "_db_path_override", str(db_path))
        monkeypatch.setattr(adb, "_risk_cache_tables_ensured", True)

        now = datetime.now(timezone.utc)
        as_of = now.strftime("%Y-%m-%d")
        created_at = now.strftime("%Y-%m-%d %H:%M:%S")
        _insert_risk_row(db_path, as_of=as_of, created_at=created_at)

        result = adb.get_cached_portfolio_risk(max_age_hours=24)
        assert result is not None
        assert isinstance(result["tickers"], list)
        assert "AAPL" in result["tickers"]
