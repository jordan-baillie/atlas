"""
Tests for trade dedup / superseded mechanism.

Covers:
1. Migration idempotency (superseded column add + index changes)
2. Dup cluster detection and canonical-row selection (lowest id)
3. Unique index rejects duplicate active-closed rows
4. Unique index allows legitimate re-entries (different pnl or exit_date)
5. record_trade_exit dedup guard: second close logs WARN + marks superseded=1
6. P&L aggregation excludes superseded rows via get_closed_trades
7. Legitimate re-open with different pnl succeeds (both active)
8. (bonus) test_migration_marks_known_dup_clusters — seeded prod-like data

All tests use _isolate_prod_db (autouse) — never touches data/atlas.db.
"""
from __future__ import annotations

import importlib.util
import logging
import sqlite3
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from db.atlas_db import get_db, get_closed_trades, performance_summary
import db.atlas_db as _adb


# ---------------------------------------------------------------------------
# Load migration module (hyphenated filename → importlib)
# ---------------------------------------------------------------------------

def _load_migration():
    mig_path = (
        PROJECT_ROOT / "scripts" / "migrations" / "2026-04-28-trade-dedup-superseded.py"
    )
    spec = importlib.util.spec_from_file_location("_mig_dedup", str(mig_path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_TRADES_DDL = """
CREATE TABLE IF NOT EXISTS trades (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker          TEXT    NOT NULL,
    strategy        TEXT    NOT NULL,
    universe        TEXT,
    direction       TEXT    DEFAULT 'long',
    entry_date      TEXT    NOT NULL,
    entry_price     REAL    NOT NULL,
    shares          INTEGER NOT NULL,
    stop_price      REAL,
    take_profit     REAL,
    exit_date       TEXT,
    exit_price      REAL,
    exit_reason     TEXT,
    pnl             REAL,
    pnl_pct         REAL,
    mae             REAL,
    mfe             REAL,
    hold_days       INTEGER,
    confidence      REAL,
    regime_at_entry TEXT,
    regime_at_exit  TEXT,
    status          TEXT    DEFAULT 'open',
    config_version  TEXT,
    created_at      TEXT    DEFAULT (datetime('now')),
    updated_at      TEXT    DEFAULT (datetime('now')),
    stop_order_id   TEXT    DEFAULT '',
    tp_order_id     TEXT    DEFAULT ''
);
"""


def _raw_conn(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), timeout=30, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row
    return conn


def _create_pre_migration_db(path: Path) -> sqlite3.Connection:
    """
    Create a minimal DB with the trades table but WITHOUT the new unique index.
    This simulates the pre-migration state so we can insert dup rows freely.
    """
    conn = _raw_conn(path)
    conn.executescript(_TRADES_DDL)
    # Add superseded column manually (without the unique index that blocks dups)
    conn.execute("BEGIN")
    conn.execute(
        "ALTER TABLE trades ADD COLUMN superseded INTEGER NOT NULL DEFAULT 0 "
        "CHECK (superseded IN (0,1))"
    )
    conn.execute("COMMIT")
    return conn


def _create_migrated_db(path: Path):
    """Create a fresh DB from schema.sql (with new index) and run migration."""
    mig = _load_migration()
    conn = mig._connect(path)
    schema = (PROJECT_ROOT / "db" / "schema.sql").read_text()
    conn.executescript(schema)
    mig.apply_migration(conn)
    return conn, mig


def _insert_closed_raw(
    conn: sqlite3.Connection,
    ticker: str,
    strategy: str,
    entry_date: str,
    exit_date: str,
    pnl: float,
    superseded: int = 0,
) -> int:
    conn.execute("BEGIN")
    cur = conn.execute(
        "INSERT INTO trades (ticker, strategy, universe, direction, "
        "entry_date, exit_date, entry_price, shares, stop_price, pnl, "
        "status, superseded) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,'closed',?)",
        (
            ticker, strategy, "sp500", "long",
            entry_date, exit_date, 100.0, 10, 90.0, pnl, superseded,
        ),
    )
    conn.execute("COMMIT")
    return cur.lastrowid


# ---------------------------------------------------------------------------
# 1. test_migration_adds_superseded_column_idempotent
# ---------------------------------------------------------------------------

class TestMigrationIdempotent:
    def test_migration_adds_superseded_column_idempotent(self, tmp_path: Path) -> None:
        """Running the migration twice must succeed with no errors."""
        mig = _load_migration()

        # Start with pre-migration DB (no superseded col, old index style)
        db_path = tmp_path / "test_mig.db"
        conn = mig._connect(db_path)
        schema = (PROJECT_ROOT / "db" / "schema.sql").read_text()
        conn.executescript(schema)

        # schema.sql now has the new index; apply_migration should be idempotent
        mig.apply_migration(conn)

        cols = [r[1] for r in conn.execute("PRAGMA table_info(trades)").fetchall()]
        assert "superseded" in cols
        assert mig._has_index(conn, "uq_trades_active_closed")
        assert not mig._has_index(conn, "idx_trades_no_dup_closed")

        # Second run — must not raise
        mig.apply_migration(conn)

        cols2 = [r[1] for r in conn.execute("PRAGMA table_info(trades)").fetchall()]
        assert "superseded" in cols2
        assert mig._has_index(conn, "uq_trades_active_closed")
        conn.close()


# ---------------------------------------------------------------------------
# 2. test_migration_marks_known_dup_clusters
# ---------------------------------------------------------------------------

class TestMigrationDupClusters:
    def test_migration_marks_known_dup_clusters(self, tmp_path: Path) -> None:
        """Migration's _find_dup_clusters detects correct clusters in seeded data."""
        mig = _load_migration()

        # Use pre-migration DB so we can insert dup rows freely (no unique index)
        conn = _create_pre_migration_db(tmp_path / "test_clusters.db")

        # Seed MRVL cluster: same (ticker, strategy, exit_date, pnl)
        id_mrvl_c  = _insert_closed_raw(conn, "MRVL", "momentum_breakout", "2026-04-02", "2026-04-07", 17.57)
        id_mrvl_d  = _insert_closed_raw(conn, "MRVL", "momentum_breakout", "2026-04-03", "2026-04-07", 17.57)

        # Seed AMT cluster: pnl rounds to same cent
        id_amt_c   = _insert_closed_raw(conn, "AMT", "mean_reversion", "2026-03-31", "2026-04-10", 49.2152)
        id_amt_d   = _insert_closed_raw(conn, "AMT", "mean_reversion", "2026-04-09", "2026-04-10", 49.22)

        # Seed SLV three-way cluster
        id_slv_c   = _insert_closed_raw(conn, "SLV", "momentum_breakout", "2026-04-16", "2026-04-22", -5.60)
        id_slv_d1  = _insert_closed_raw(conn, "SLV", "momentum_breakout", "2026-04-21", "2026-04-22", -5.60)
        id_slv_d2  = _insert_closed_raw(conn, "SLV", "momentum_breakout", "2026-04-22", "2026-04-22", -5.60)

        clusters = mig._find_dup_clusters(conn)
        assert len(clusters) == 3, f"Expected 3 clusters, got {clusters}"

        mrvl_c = next(c for c in clusters if c["ticker"] == "MRVL")
        amt_c  = next(c for c in clusters if c["ticker"] == "AMT")
        slv_c  = next(c for c in clusters if c["ticker"] == "SLV")

        assert mrvl_c["canonical_id"] == id_mrvl_c
        assert id_mrvl_d in mrvl_c["superseded_ids"]

        assert amt_c["canonical_id"] == id_amt_c
        assert id_amt_d in amt_c["superseded_ids"]

        assert slv_c["canonical_id"] == id_slv_c
        assert id_slv_d1 in slv_c["superseded_ids"]
        assert id_slv_d2 in slv_c["superseded_ids"]

        # Mark them as superseded=1 and verify
        for c in clusters:
            ph = ",".join("?" * len(c["superseded_ids"]))
            conn.execute("BEGIN")
            conn.execute(
                f"UPDATE trades SET superseded=1 WHERE id IN ({ph})",
                c["superseded_ids"],
            )
            conn.execute("COMMIT")

        for c in clusters:
            all_ids = [c["canonical_id"]] + c["superseded_ids"]
            ph = ",".join("?" * len(all_ids))
            rows = conn.execute(
                f"SELECT id, superseded FROM trades WHERE id IN ({ph})", all_ids
            ).fetchall()
            active = [r for r in rows if r["superseded"] == 0]
            assert len(active) == 1 and active[0]["id"] == c["canonical_id"]

        conn.close()


# ---------------------------------------------------------------------------
# 3. test_migration_keeps_earliest_id
# ---------------------------------------------------------------------------

class TestMigrationKeepsEarliestId:
    def test_migration_keeps_earliest_id(self, tmp_path: Path) -> None:
        """Canonical row = lowest id regardless of entry_date."""
        mig = _load_migration()
        conn = _create_pre_migration_db(tmp_path / "test_earliest.db")

        id1 = _insert_closed_raw(conn, "TSLA", "momentum_breakout", "2026-02-01", "2026-02-10", 50.0)
        id2 = _insert_closed_raw(conn, "TSLA", "momentum_breakout", "2026-02-03", "2026-02-10", 50.0)
        assert id1 < id2

        clusters = mig._find_dup_clusters(conn)
        assert len(clusters) == 1
        assert clusters[0]["canonical_id"] == id1
        assert id2 in clusters[0]["superseded_ids"]

        conn.close()


# ---------------------------------------------------------------------------
# 4. test_unique_index_rejects_duplicate_close
# ---------------------------------------------------------------------------

class TestUniqueIndexRejectsDup:
    def test_unique_index_rejects_duplicate_close(self, tmp_path: Path) -> None:
        """After migration, inserting a matching active-closed row raises IntegrityError."""
        conn, _ = _create_migrated_db(tmp_path / "test_uidx.db")

        _insert_closed_raw(conn, "GLD", "momentum_breakout", "2026-03-01", "2026-03-10", -20.0)

        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("BEGIN")
            conn.execute(
                "INSERT INTO trades (ticker, strategy, universe, direction, "
                "entry_date, exit_date, entry_price, shares, stop_price, pnl, "
                "status, superseded) "
                "VALUES ('GLD','momentum_breakout','commodity_etfs','long',"
                "'2026-03-05','2026-03-10',100,10,90,-20.0,'closed',0)"
            )
            conn.execute("COMMIT")
        conn.execute("ROLLBACK")
        conn.close()


# ---------------------------------------------------------------------------
# 5. test_unique_index_allows_legitimate_reentry_with_different_pnl
# ---------------------------------------------------------------------------

class TestUniqueIndexAllowsDifferentPnl:
    def test_allows_different_pnl(self, tmp_path: Path) -> None:
        conn, _ = _create_migrated_db(tmp_path / "test_diff_pnl.db")
        _insert_closed_raw(conn, "AAPL", "momentum_breakout", "2026-01-10", "2026-01-20", +50.0)
        _insert_closed_raw(conn, "AAPL", "momentum_breakout", "2026-03-10", "2026-03-20", +75.0)
        n = conn.execute(
            "SELECT COUNT(*) FROM trades WHERE ticker='AAPL' AND superseded=0"
        ).fetchone()[0]
        assert n == 2
        conn.close()

    def test_allows_different_exit_date(self, tmp_path: Path) -> None:
        conn, _ = _create_migrated_db(tmp_path / "test_diff_date.db")
        _insert_closed_raw(conn, "MSFT", "connors_rsi2", "2026-01-01", "2026-01-05", +20.0)
        _insert_closed_raw(conn, "MSFT", "connors_rsi2", "2026-02-01", "2026-02-05", +20.0)
        n = conn.execute(
            "SELECT COUNT(*) FROM trades WHERE ticker='MSFT' AND superseded=0"
        ).fetchone()[0]
        assert n == 2
        conn.close()


# ---------------------------------------------------------------------------
# 6. test_writer_guard_logs_and_skips
# ---------------------------------------------------------------------------

class TestWriterGuard:
    def test_writer_guard_logs_and_skips(self, caplog: pytest.LogCaptureFixture) -> None:
        """record_trade_exit dedup guard: second close logs WARN and marks superseded=1."""
        from db.atlas_db import record_trade_entry, record_trade_exit

        # First cycle: open + close (active)
        record_trade_entry(
            ticker="NVDA", strategy="momentum_breakout", universe="sp500",
            entry_price=800.0, shares=5, stop_price=720.0,
            take_profit=None, confidence=0.8,
            regime_state="bull_risk_on", direction="long",
        )
        record_trade_exit(
            ticker="NVDA", strategy="momentum_breakout",
            exit_price=850.0,      # pnl = (850-800)*5 = 250.0
            exit_reason="trailing_stop",
        )

        first_closed = get_closed_trades()
        nvda_first = [t for t in first_closed if t["ticker"] == "NVDA"]
        assert len(nvda_first) == 1
        assert nvda_first[0]["superseded"] == 0
        first_pnl = nvda_first[0]["pnl"]   # 250.0

        # Second cycle: open again (allowed since first is closed)
        record_trade_entry(
            ticker="NVDA", strategy="momentum_breakout", universe="sp500",
            entry_price=810.0, shares=5, stop_price=729.0,
            take_profit=None, confidence=0.8,
            regime_state="bull_risk_on", direction="long",
        )
        # Compute exit_price that yields same pnl: (exit - 810)*5 = 250 → exit=860
        dup_exit_price = 810.0 + first_pnl / 5.0

        with caplog.at_level(logging.WARNING, logger="db.atlas_db"):
            record_trade_exit(
                ticker="NVDA", strategy="momentum_breakout",
                exit_price=dup_exit_price,
                exit_reason="trailing_stop",
            )

        with get_db() as db:
            all_nvda = db.execute(
                "SELECT id, superseded, pnl FROM trades "
                "WHERE ticker='NVDA' AND status='closed' ORDER BY id"
            ).fetchall()

        assert len(all_nvda) == 2, f"Expected 2 rows, got {all_nvda}"
        actives = [r for r in all_nvda if r["superseded"] == 0]
        sups    = [r for r in all_nvda if r["superseded"] == 1]
        assert len(actives) == 1, f"Expected 1 active, got {actives}"
        assert len(sups)    == 1, f"Expected 1 superseded, got {sups}"

        dup_warns = [r for r in caplog.records if "dedup hit" in r.message]
        assert len(dup_warns) >= 1, (
            f"Expected 'dedup hit' WARN, got: {[r.message for r in caplog.records]}"
        )


# ---------------------------------------------------------------------------
# 7. test_pnl_aggregation_excludes_superseded
# ---------------------------------------------------------------------------

class TestPnlAggregation:
    def test_get_closed_trades_excludes_superseded(self) -> None:
        """get_closed_trades() returns only superseded=0 rows."""
        with get_db() as db:
            db.execute("BEGIN")
            db.execute(
                "INSERT INTO trades (ticker, strategy, universe, direction, "
                "entry_date, exit_date, entry_price, shares, stop_price, pnl, "
                "status, superseded) "
                "VALUES ('DEDUP_A','mean_reversion','sp500','long',"
                "'2026-01-01','2026-01-05',100,10,90,+50.0,'closed',0)"
            )
            db.execute(
                "INSERT INTO trades (ticker, strategy, universe, direction, "
                "entry_date, exit_date, entry_price, shares, stop_price, pnl, "
                "status, superseded) "
                "VALUES ('DEDUP_A','mean_reversion','sp500','long',"
                "'2026-01-02','2026-01-05',100,10,90,+50.0,'closed',1)"
            )
            db.execute("COMMIT")

        closed = get_closed_trades()
        rows = [t for t in closed if t["ticker"] == "DEDUP_A"]
        assert len(rows) == 1, f"Expected 1 active row, got {rows}"
        assert rows[0]["superseded"] == 0
        assert rows[0]["pnl"] == 50.0

    def test_pnl_not_double_counted(self) -> None:
        """P&L sum must not include superseded=1 rows."""
        with get_db() as db:
            db.execute("BEGIN")
            db.execute(
                "INSERT INTO trades (ticker, strategy, universe, direction, "
                "entry_date, exit_date, entry_price, shares, stop_price, pnl, "
                "status, superseded) "
                "VALUES ('DEDUP_B','momentum_breakout','sp500','long',"
                "'2026-02-01','2026-02-08',100,3,90,+90.0,'closed',0)"
            )
            db.execute(
                "INSERT INTO trades (ticker, strategy, universe, direction, "
                "entry_date, exit_date, entry_price, shares, stop_price, pnl, "
                "status, superseded) "
                "VALUES ('DEDUP_B','momentum_breakout','sp500','long',"
                "'2026-02-02','2026-02-08',100,3,90,+90.0,'closed',1)"
            )
            db.execute("COMMIT")

        closed = get_closed_trades()
        dedup_b = [t for t in closed if t["ticker"] == "DEDUP_B"]
        total_pnl = sum(t["pnl"] for t in dedup_b)
        assert total_pnl == 90.0, f"Expected 90.0 (no double-count), got {total_pnl}"


# ---------------------------------------------------------------------------
# 8. test_legitimate_reopen_allowed (bonus)
# ---------------------------------------------------------------------------

class TestLegitimateReopen:
    def test_reopen_different_pnl_both_active(self) -> None:
        """Different exit_price → different pnl: both closes are active."""
        from db.atlas_db import record_trade_entry, record_trade_exit

        record_trade_entry(
            ticker="COST", strategy="momentum_breakout", universe="sp500",
            entry_price=900.0, shares=1, stop_price=810.0,
            take_profit=None, confidence=0.6, regime_state=None, direction="long",
        )
        record_trade_exit(
            ticker="COST", strategy="momentum_breakout",
            exit_price=950.0,  # pnl = 50.0
            exit_reason="trailing_stop",
        )

        record_trade_entry(
            ticker="COST", strategy="momentum_breakout", universe="sp500",
            entry_price=920.0, shares=1, stop_price=828.0,
            take_profit=None, confidence=0.6, regime_state=None, direction="long",
        )
        # Different pnl: (990-920)*1 = 70 ≠ 50 → NOT a dup
        record_trade_exit(
            ticker="COST", strategy="momentum_breakout",
            exit_price=990.0,  # pnl = 70.0
            exit_reason="trailing_stop",
        )

        closed = get_closed_trades()
        cost = [t for t in closed if t["ticker"] == "COST"]
        active = [t for t in cost if t["superseded"] == 0]
        assert len(active) == 2, f"Expected 2 active COST trades, got {active}"
