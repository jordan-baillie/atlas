"""Regression tests for portfolio_snapshots.daily_pnl_pct column (#289).

Covers:
1. Migration creates column when absent (idempotent).
2. Migration backfills existing rows correctly, per market_id.
3. record_snapshot() auto-computes daily_pnl_pct on INSERT.
4. NULL cases: first row per market, NULL equity, zero prev_equity.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_PORTFOLIO_SNAPSHOTS_DDL = """
CREATE TABLE IF NOT EXISTS portfolio_snapshots (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp           TEXT    NOT NULL,
    total_equity        REAL,
    cash                REAL,
    positions           TEXT,
    exposure_by_universe TEXT,
    exposure_by_sector  TEXT,
    regime_state        TEXT,
    source              TEXT    DEFAULT 'eod',
    market_id           TEXT    DEFAULT 'sp500'
);
"""


def _make_legacy_db(db_path: Path) -> sqlite3.Connection:
    """Create a portfolio_snapshots table WITHOUT daily_pnl_pct (pre-migration state)."""
    conn = sqlite3.connect(str(db_path))
    conn.execute(_PORTFOLIO_SNAPSHOTS_DDL)
    conn.commit()
    return conn


def _column_exists(conn: sqlite3.Connection, table: str, col: str) -> bool:
    return col in [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]


# ---------------------------------------------------------------------------
# Migration tests
# ---------------------------------------------------------------------------

class TestMigration:
    def test_adds_column_when_absent(self, tmp_path):
        """Migration creates daily_pnl_pct when the column doesn't exist."""
        db_path = tmp_path / "atlas.db"
        conn = _make_legacy_db(db_path)
        assert not _column_exists(conn, "portfolio_snapshots", "daily_pnl_pct")
        conn.close()

        from scripts.migrations import _import_migration
        mod = _import_migration("2026-05-07-add-daily-pnl-pct")
        mod.run(db_path=str(db_path))

        conn2 = sqlite3.connect(str(db_path))
        assert _column_exists(conn2, "portfolio_snapshots", "daily_pnl_pct"), \
            "daily_pnl_pct column should exist after migration"
        conn2.close()

    def test_idempotent_second_run(self, tmp_path):
        """Running migration twice does not error."""
        db_path = tmp_path / "atlas.db"
        _make_legacy_db(db_path).close()

        from scripts.migrations import _import_migration
        mod = _import_migration("2026-05-07-add-daily-pnl-pct")
        mod.run(db_path=str(db_path))
        mod.run(db_path=str(db_path))  # second run should be a no-op

        conn = sqlite3.connect(str(db_path))
        assert _column_exists(conn, "portfolio_snapshots", "daily_pnl_pct")
        conn.close()

    def test_backfill_computes_per_market_pct(self, tmp_path):
        """Backfill correctly computes daily_pnl_pct per market_id."""
        db_path = tmp_path / "atlas.db"
        conn = _make_legacy_db(db_path)
        conn.executemany(
            "INSERT INTO portfolio_snapshots (timestamp, total_equity, market_id) VALUES (?,?,?)",
            [
                ("2026-04-01T00:00:00", 1000.0, "sp500"),
                ("2026-04-02T00:00:00", 1050.0, "sp500"),   # +5%
                ("2026-04-03T00:00:00", 1047.5, "sp500"),   # -0.2381%
                ("2026-04-01T00:00:00", 500.0,  "ALL"),
                ("2026-04-02T00:00:00", 510.0,  "ALL"),     # +2%
            ],
        )
        conn.commit()
        conn.close()

        from scripts.migrations import _import_migration
        mod = _import_migration("2026-05-07-add-daily-pnl-pct")
        mod.run(db_path=str(db_path))

        conn2 = sqlite3.connect(str(db_path))
        conn2.row_factory = sqlite3.Row
        rows = {
            (r["market_id"], r["timestamp"]): r["daily_pnl_pct"]
            for r in conn2.execute(
                "SELECT market_id, timestamp, daily_pnl_pct FROM portfolio_snapshots ORDER BY market_id, timestamp"
            ).fetchall()
        }
        conn2.close()

        # First row per market — no previous → NULL
        assert rows[("ALL", "2026-04-01T00:00:00")] is None
        assert rows[("sp500", "2026-04-01T00:00:00")] is None

        # sp500: +5.0%
        assert abs(rows[("sp500", "2026-04-02T00:00:00")] - 5.0) < 0.01

        # sp500: (1047.5 - 1050) / 1050 * 100 ≈ -0.2381%
        assert abs(rows[("sp500", "2026-04-03T00:00:00")] - (-0.2381)) < 0.01

        # ALL: +2.0%
        assert abs(rows[("ALL", "2026-04-02T00:00:00")] - 2.0) < 0.01

    def test_backfill_returns_row_count(self, tmp_path):
        """run() returns the number of rows that received a non-NULL pnl_pct."""
        db_path = tmp_path / "atlas.db"
        conn = _make_legacy_db(db_path)
        conn.executemany(
            "INSERT INTO portfolio_snapshots (timestamp, total_equity, market_id) VALUES (?,?,?)",
            [
                ("2026-04-01T00:00:00", 1000.0, "sp500"),
                ("2026-04-02T00:00:00", 1100.0, "sp500"),
                ("2026-04-03T00:00:00", 1090.0, "sp500"),
            ],
        )
        conn.commit()
        conn.close()

        from scripts.migrations import _import_migration
        mod = _import_migration("2026-05-07-add-daily-pnl-pct")
        count = mod.run(db_path=str(db_path))
        assert count == 2, f"Expected 2 updated rows (skip first row), got {count}"

    def test_null_equity_rows_skipped(self, tmp_path):
        """Rows with NULL total_equity do not propagate previous equity."""
        db_path = tmp_path / "atlas.db"
        conn = _make_legacy_db(db_path)
        conn.executemany(
            "INSERT INTO portfolio_snapshots (timestamp, total_equity, market_id) VALUES (?,?,?)",
            [
                ("2026-04-01T00:00:00", 1000.0, "sp500"),
                ("2026-04-02T00:00:00", None,    "sp500"),   # NULL equity
                ("2026-04-03T00:00:00", 1100.0, "sp500"),    # prev=1000 (skip NULL)
            ],
        )
        conn.commit()
        conn.close()

        from scripts.migrations import _import_migration
        mod = _import_migration("2026-05-07-add-daily-pnl-pct")
        mod.run(db_path=str(db_path))

        conn2 = sqlite3.connect(str(db_path))
        conn2.row_factory = sqlite3.Row
        rows = conn2.execute(
            "SELECT timestamp, daily_pnl_pct FROM portfolio_snapshots ORDER BY timestamp"
        ).fetchall()
        conn2.close()

        assert rows[0]["daily_pnl_pct"] is None     # first row
        assert rows[1]["daily_pnl_pct"] is None     # NULL equity → no pct
        # row[2]: (1100 - 1000) / 1000 * 100 = 10.0%
        assert abs(rows[2]["daily_pnl_pct"] - 10.0) < 0.01


# ---------------------------------------------------------------------------
# Writer hook tests (record_snapshot)
# ---------------------------------------------------------------------------

class TestWriterHook:
    """Tests that record_snapshot() auto-computes daily_pnl_pct on INSERT."""

    @pytest.fixture()
    def isolated_db(self, tmp_path, monkeypatch):
        """Point atlas_db at an in-memory-style tmp DB with full schema."""
        import db.atlas_db as adb
        db_path = tmp_path / "atlas.db"

        # Create minimal schema
        conn = sqlite3.connect(str(db_path))
        conn.execute(_PORTFOLIO_SNAPSHOTS_DDL)
        # Add the new column (migration already run on prod, but this is a fresh test DB)
        conn.execute(
            "ALTER TABLE portfolio_snapshots ADD COLUMN daily_pnl_pct REAL"
        )
        conn.commit()
        conn.close()

        monkeypatch.setattr(adb, "_db_path_override", str(db_path))
        return db_path

    def test_first_row_pct_is_null(self, isolated_db):
        """First snapshot for a market has daily_pnl_pct=NULL (no previous)."""
        from db import atlas_db as adb
        adb.record_snapshot(
            timestamp="2026-04-01T00:00:00",
            total_equity=1000.0,
            market_id="sp500",
        )
        conn = sqlite3.connect(str(isolated_db))
        row = conn.execute("SELECT daily_pnl_pct FROM portfolio_snapshots").fetchone()
        conn.close()
        assert row[0] is None, "First row should have NULL daily_pnl_pct"

    def test_second_row_computes_pct(self, isolated_db):
        """Second snapshot auto-computes pct change vs previous."""
        from db import atlas_db as adb
        adb.record_snapshot(
            timestamp="2026-04-01T00:00:00",
            total_equity=2000.0,
            market_id="sp500",
        )
        adb.record_snapshot(
            timestamp="2026-04-02T00:00:00",
            total_equity=2100.0,   # +5%
            market_id="sp500",
        )
        conn = sqlite3.connect(str(isolated_db))
        rows = conn.execute(
            "SELECT daily_pnl_pct FROM portfolio_snapshots ORDER BY timestamp"
        ).fetchall()
        conn.close()
        assert rows[0][0] is None        # first row
        assert abs(rows[1][0] - 5.0) < 0.001, f"Expected +5.0%, got {rows[1][0]}"

    def test_markets_are_isolated(self, isolated_db):
        """daily_pnl_pct is computed per market_id, not across markets."""
        from db import atlas_db as adb
        adb.record_snapshot(
            timestamp="2026-04-01T00:00:00",
            total_equity=1000.0,
            market_id="sp500",
        )
        # Different market — should have NULL pct (no prior for THIS market)
        adb.record_snapshot(
            timestamp="2026-04-01T00:00:00",
            total_equity=500.0,
            market_id="ALL",
        )
        conn = sqlite3.connect(str(isolated_db))
        rows = {
            r[0]: r[1]
            for r in conn.execute(
                "SELECT market_id, daily_pnl_pct FROM portfolio_snapshots"
            ).fetchall()
        }
        conn.close()
        assert rows["sp500"] is None
        assert rows["ALL"] is None

    def test_negative_pnl_pct(self, isolated_db):
        """Negative daily_pnl_pct stored correctly."""
        from db import atlas_db as adb
        adb.record_snapshot(
            timestamp="2026-04-01T00:00:00",
            total_equity=1000.0,
            market_id="sp500",
        )
        adb.record_snapshot(
            timestamp="2026-04-02T00:00:00",
            total_equity=950.0,   # -5%
            market_id="sp500",
        )
        conn = sqlite3.connect(str(isolated_db))
        rows = conn.execute(
            "SELECT daily_pnl_pct FROM portfolio_snapshots ORDER BY timestamp DESC LIMIT 1"
        ).fetchone()
        conn.close()
        assert abs(rows[0] - (-5.0)) < 0.001, f"Expected -5.0%, got {rows[0]}"

    def test_null_equity_no_pct(self, isolated_db):
        """If total_equity=None, daily_pnl_pct is NULL (can't compute %)."""
        from db import atlas_db as adb
        adb.record_snapshot(
            timestamp="2026-04-01T00:00:00",
            total_equity=1000.0,
            market_id="sp500",
        )
        adb.record_snapshot(
            timestamp="2026-04-02T00:00:00",
            total_equity=None,
            market_id="sp500",
        )
        conn = sqlite3.connect(str(isolated_db))
        rows = conn.execute(
            "SELECT daily_pnl_pct FROM portfolio_snapshots ORDER BY timestamp DESC LIMIT 1"
        ).fetchone()
        conn.close()
        assert rows[0] is None, "NULL equity → NULL pct"

    def test_zero_prev_equity_no_pct(self, isolated_db):
        """If previous total_equity=0, daily_pnl_pct is NULL (avoid div-by-zero)."""
        from db import atlas_db as adb
        adb.record_snapshot(
            timestamp="2026-04-01T00:00:00",
            total_equity=0.0,
            market_id="sp500",
        )
        adb.record_snapshot(
            timestamp="2026-04-02T00:00:00",
            total_equity=1000.0,
            market_id="sp500",
        )
        conn = sqlite3.connect(str(isolated_db))
        rows = conn.execute(
            "SELECT daily_pnl_pct FROM portfolio_snapshots ORDER BY timestamp DESC LIMIT 1"
        ).fetchone()
        conn.close()
        assert rows[0] is None, "Zero prev equity → NULL pct (no div-by-zero)"


# ---------------------------------------------------------------------------
# Helper used by test_adds_column_when_absent and friends
# ---------------------------------------------------------------------------

# Patch into scripts/migrations/__init__.py if needed
import importlib
import sys


def _import_migration_direct(name: str):
    """Directly load a migration module by filename stem."""
    from pathlib import Path
    mod_path = Path(__file__).resolve().parent.parent / "scripts" / "migrations" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, mod_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# Provide _import_migration via a simple sys.modules approach so tests above work
if "scripts.migrations" not in sys.modules:
    import types
    pkg = types.ModuleType("scripts.migrations")
    pkg._import_migration = _import_migration_direct
    sys.modules["scripts.migrations"] = pkg
else:
    sys.modules["scripts.migrations"]._import_migration = _import_migration_direct
