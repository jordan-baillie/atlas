"""Regression tests for P1-5: portfolio_snapshots.market_id scoping.

Covers:
1. Schema has market_id column and both indexes
2. Per-market write + ALL aggregate row → queries return correct row per market_id
3. Default 'ALL' reader returns aggregate, not a per-market row
4. Migration is idempotent (run twice → no duplicate columns or errors)
"""
from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def isolated_db(tmp_path, monkeypatch):
    """Spin up a fresh in-memory-backed db using the real schema, isolated from prod."""
    import db.atlas_db as _adb

    db_path = tmp_path / "test_snapshots.db"
    monkeypatch.setattr(_adb, "_db_path_override", str(db_path))

    # Init schema
    from db.atlas_db import get_db
    schema_sql = (Path(__file__).parents[1] / "db" / "schema.sql").read_text()
    with get_db() as conn:
        conn.executescript(schema_sql)

    return db_path


# ---------------------------------------------------------------------------
# Test 1: Schema has market_id column and indexes
# ---------------------------------------------------------------------------

class TestSchema:
    def test_market_id_column_exists(self, isolated_db):
        conn = sqlite3.connect(str(isolated_db))
        cols = {r[1] for r in conn.execute("PRAGMA table_info(portfolio_snapshots)").fetchall()}
        conn.close()
        assert "market_id" in cols, "market_id column missing from portfolio_snapshots"

    def test_market_id_default_value(self, isolated_db):
        conn = sqlite3.connect(str(isolated_db))
        col_info = {
            r[1]: r[4]  # name → default_value
            for r in conn.execute("PRAGMA table_info(portfolio_snapshots)").fetchall()
        }
        conn.close()
        assert col_info.get("market_id") == "'sp500'", (
            f"market_id default should be 'sp500', got {col_info.get('market_id')!r}"
        )

    def test_composite_index_exists(self, isolated_db):
        conn = sqlite3.connect(str(isolated_db))
        indexes = {r[1] for r in conn.execute("PRAGMA index_list(portfolio_snapshots)").fetchall()}
        conn.close()
        assert "idx_portfolio_snapshots_market_ts" in indexes, (
            "Composite index idx_portfolio_snapshots_market_ts missing"
        )

    def test_timestamp_index_exists(self, isolated_db):
        conn = sqlite3.connect(str(isolated_db))
        indexes = {r[1] for r in conn.execute("PRAGMA index_list(portfolio_snapshots)").fetchall()}
        conn.close()
        assert "idx_snapshots_ts" in indexes


# ---------------------------------------------------------------------------
# Test 2: Per-market rows + ALL row — queries return correct row
# ---------------------------------------------------------------------------

class TestMarketScopedReads:
    def test_two_market_rows_plus_all_row(self, isolated_db):
        from db.atlas_db import record_snapshot, record_all_markets_snapshot
        from db.atlas_db import get_latest_snapshot

        ts_base = "2026-04-24T08:00:00"
        # sp500 EOD run writes first
        record_snapshot(
            timestamp=ts_base + ".100000",
            total_equity=5405.86,
            cash=99.48,
            market_id="sp500",
        )
        # commodity_etfs EOD run writes second
        record_snapshot(
            timestamp=ts_base + ".200000",
            total_equity=4966.00,
            cash=99.48,
            market_id="commodity_etfs",
        )
        # ALL aggregate row from broker account
        record_all_markets_snapshot(
            timestamp=ts_base + ".300000",
            broker_equity=5384.17,
            broker_cash=99.48,
        )

        sp500_snap = get_latest_snapshot("sp500")
        assert sp500_snap is not None
        assert sp500_snap["total_equity"] == pytest.approx(5405.86)
        assert sp500_snap["market_id"] == "sp500"

        ce_snap = get_latest_snapshot("commodity_etfs")
        assert ce_snap is not None
        assert ce_snap["total_equity"] == pytest.approx(4966.00)
        assert ce_snap["market_id"] == "commodity_etfs"

        all_snap = get_latest_snapshot("ALL")
        assert all_snap is not None
        assert all_snap["total_equity"] == pytest.approx(5384.17)
        assert all_snap["market_id"] == "ALL"

    def test_get_snapshots_market_filter(self, isolated_db):
        from db.atlas_db import record_snapshot, get_snapshots

        record_snapshot(timestamp="2026-04-23T08:00:00", total_equity=5300.0,
                        cash=100.0, market_id="sp500")
        record_snapshot(timestamp="2026-04-23T08:00:05", total_equity=4900.0,
                        cash=100.0, market_id="commodity_etfs")
        record_snapshot(timestamp="2026-04-24T08:00:00", total_equity=5400.0,
                        cash=100.0, market_id="sp500")
        record_snapshot(timestamp="2026-04-24T08:00:05", total_equity=5000.0,
                        cash=100.0, market_id="commodity_etfs")

        sp500_rows = get_snapshots(market_id="sp500")
        assert all(r["market_id"] == "sp500" for r in sp500_rows)
        assert len(sp500_rows) == 2

        ce_rows = get_snapshots(market_id="commodity_etfs")
        assert all(r["market_id"] == "commodity_etfs" for r in ce_rows)
        assert len(ce_rows) == 2

    def test_get_snapshots_none_market_returns_all(self, isolated_db):
        from db.atlas_db import record_snapshot, get_snapshots

        record_snapshot(timestamp="2026-04-24T08:00:00", total_equity=5400.0,
                        cash=100.0, market_id="sp500")
        record_snapshot(timestamp="2026-04-24T08:00:05", total_equity=5000.0,
                        cash=100.0, market_id="commodity_etfs")

        all_rows = get_snapshots(market_id=None)
        assert len(all_rows) == 2


# ---------------------------------------------------------------------------
# Test 3: Default reader returns ALL row, not per-market
# ---------------------------------------------------------------------------

class TestDefaultReaderReturnsAll:
    def test_get_latest_snapshot_default_is_all(self, isolated_db):
        from db.atlas_db import record_snapshot, record_all_markets_snapshot
        from db.atlas_db import get_latest_snapshot

        ts = "2026-04-24T08:00:00"
        record_snapshot(timestamp=ts + ".100", total_equity=5405.86,
                        cash=99.48, market_id="sp500")
        record_snapshot(timestamp=ts + ".200", total_equity=4966.00,
                        cash=99.48, market_id="commodity_etfs")
        record_all_markets_snapshot(
            timestamp=ts + ".300", broker_equity=5384.17, broker_cash=99.48
        )

        # Default call (no args) should return the ALL row
        snap = get_latest_snapshot()
        assert snap is not None, "get_latest_snapshot() returned None"
        assert snap["market_id"] == "ALL", (
            f"Expected market_id='ALL', got {snap['market_id']!r} — "
            "dashboard is showing per-market equity instead of portfolio total"
        )
        assert snap["total_equity"] == pytest.approx(5384.17), (
            f"Portfolio total should be broker equity 5384.17, got {snap['total_equity']}"
        )

    def test_get_latest_snapshot_per_market_explicit(self, isolated_db):
        from db.atlas_db import record_snapshot, get_latest_snapshot

        record_snapshot(timestamp="2026-04-24T08:00:00.100", total_equity=5405.86,
                        cash=99.48, market_id="sp500")

        snap = get_latest_snapshot("sp500")
        assert snap is not None
        assert snap["market_id"] == "sp500"
        assert snap["total_equity"] == pytest.approx(5405.86)

    def test_get_latest_snapshot_returns_none_when_no_all_row(self, isolated_db):
        from db.atlas_db import record_snapshot, get_latest_snapshot

        # Only per-market rows, no ALL row
        record_snapshot(timestamp="2026-04-24T08:00:00.100", total_equity=5405.86,
                        cash=99.48, market_id="sp500")

        # Default call expects ALL but table has none → returns None (not a crash)
        snap = get_latest_snapshot("ALL")
        assert snap is None


# ---------------------------------------------------------------------------
# Test 4: Migration is idempotent
# ---------------------------------------------------------------------------

class TestMigrationIdempotency:
    def test_run_migration_twice_no_error(self, tmp_path):
        """Running the migration script twice must not raise or create duplicates."""
        import shutil
        import sys
        from pathlib import Path

        scripts_dir = Path(__file__).parents[1] / "scripts" / "migrations"
        migration_mod = (
            scripts_dir / "2026-04-24-add-market-id-to-portfolio-snapshots.py"
        )

        # Minimal sqlite db with portfolio_snapshots (no market_id yet)
        db_path = tmp_path / "test_idempotent.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute("""
            CREATE TABLE portfolio_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                total_equity REAL,
                cash REAL,
                positions TEXT,
                exposure_by_universe TEXT,
                exposure_by_sector TEXT,
                regime_state TEXT,
                source TEXT DEFAULT 'eod'
            )
        """)
        conn.execute(
            "INSERT INTO portfolio_snapshots (timestamp, total_equity, cash, source) "
            "VALUES (?, ?, ?, ?)",
            ("2026-04-20T08:00:00", 5000.0, 100.0, "eod"),
        )
        conn.commit()
        conn.close()

        # Dynamically load and run migration
        import importlib.util
        spec = importlib.util.spec_from_file_location("migration", migration_mod)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        # First run
        mod.run(db_path)

        # Second run — must not raise
        mod.run(db_path)

        # Verify column exists exactly once
        conn = sqlite3.connect(str(db_path))
        cols = [r[1] for r in conn.execute("PRAGMA table_info(portfolio_snapshots)").fetchall()]
        conn.close()
        assert cols.count("market_id") == 1, "market_id column appears more than once after double run"

    def test_backfill_infers_commodity_etfs(self, tmp_path):
        """Migration correctly identifies commodity_etfs rows by position tickers."""
        import json
        import importlib.util
        from pathlib import Path

        migration_mod = (
            Path(__file__).parents[1] / "scripts" / "migrations"
            / "2026-04-24-add-market-id-to-portfolio-snapshots.py"
        )

        db_path = tmp_path / "test_backfill.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute("""
            CREATE TABLE portfolio_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                total_equity REAL,
                cash REAL,
                positions TEXT,
                source TEXT DEFAULT 'eod'
            )
        """)
        # sp500 row — normal stock tickers
        conn.execute(
            "INSERT INTO portfolio_snapshots (timestamp, total_equity, cash, positions) VALUES (?,?,?,?)",
            ("2026-04-24T08:00:00.1", 5400.0, 100.0,
             json.dumps([{"ticker": "AMD"}, {"ticker": "AVGO"}])),
        )
        # commodity_etfs row — includes GLD
        conn.execute(
            "INSERT INTO portfolio_snapshots (timestamp, total_equity, cash, positions) VALUES (?,?,?,?)",
            ("2026-04-24T08:00:00.2", 5000.0, 100.0,
             json.dumps([{"ticker": "GLD"}, {"ticker": "UNG"}])),
        )
        conn.commit()
        conn.close()

        spec = importlib.util.spec_from_file_location("migration", migration_mod)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        mod.run(db_path)

        conn = sqlite3.connect(str(db_path))
        rows = {
            r[0]: r[1]
            for r in conn.execute("SELECT total_equity, market_id FROM portfolio_snapshots").fetchall()
        }
        conn.close()
        assert rows[5400.0] == "sp500"
        assert rows[5000.0] == "commodity_etfs"
