"""
tests/test_trades_check_constraints.py

Verifies the CHECK constraints added by
  scripts/migrations/2026-04-29-trades-check-constraints.py

Unit tests use an in-memory SQLite DB with the fully-constrained schema.
The idempotency test runs the migration script against a tmp file-based DB.
"""
from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path

import pytest

# ── Constrained schema (mirrors migration DDL) ────────────────────────────────

_CREATE_CONSTRAINED = """\
CREATE TABLE trades (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker          TEXT    NOT NULL,
    strategy        TEXT    NOT NULL DEFAULT 'test_strat',
    universe        TEXT,
    direction       TEXT    DEFAULT 'long',
    entry_date      TEXT    NOT NULL DEFAULT '2026-01-01',
    entry_price     REAL    NOT NULL DEFAULT 100.0,
    shares          INTEGER NOT NULL DEFAULT 1,
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
    tp_order_id     TEXT    DEFAULT '',
    superseded      INTEGER NOT NULL DEFAULT 0,
    CHECK (superseded IN (0, 1)),
    CHECK (exit_date IS NULL OR exit_date >= entry_date),
    CHECK (
        stop_price IS NULL
        OR (direction = 'long'  AND stop_price < entry_price)
        OR (direction = 'short' AND stop_price > entry_price)
    ),
    CHECK (
        status != 'closed'
        OR (exit_price IS NOT NULL AND exit_date IS NOT NULL)
    ),
    CHECK (
        status != 'open'
        OR (entry_price IS NOT NULL AND entry_price > 0
            AND shares IS NOT NULL AND shares > 0)
    ),
    CHECK (status IN ('open', 'closed', 'cancelled', 'pending'))
)"""

# Schema WITHOUT Phase B.1 constraints (to test migration FROM this state)
_CREATE_UNCONSTRAINED = """\
CREATE TABLE trades (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker          TEXT    NOT NULL,
    strategy        TEXT    NOT NULL DEFAULT 'test_strat',
    universe        TEXT,
    direction       TEXT    DEFAULT 'long',
    entry_date      TEXT    NOT NULL DEFAULT '2026-01-01',
    entry_price     REAL    NOT NULL DEFAULT 100.0,
    shares          INTEGER NOT NULL DEFAULT 1,
    stop_price      REAL,
    take_profit     REAL,
    exit_date       TEXT,
    exit_price      REAL,
    exit_reason     TEXT,
    pnl             REAL, pnl_pct REAL, mae REAL, mfe REAL,
    hold_days       INTEGER, confidence REAL,
    regime_at_entry TEXT, regime_at_exit TEXT,
    status          TEXT    DEFAULT 'open',
    config_version  TEXT,
    created_at      TEXT    DEFAULT (datetime('now')),
    updated_at      TEXT    DEFAULT (datetime('now')),
    stop_order_id   TEXT    DEFAULT '',
    tp_order_id     TEXT    DEFAULT '',
    superseded      INTEGER NOT NULL DEFAULT 0,
    CHECK (superseded IN (0, 1)),
    CHECK (exit_date IS NULL OR exit_date >= entry_date),
    CHECK (
        stop_price IS NULL
        OR (direction = 'long'  AND stop_price < entry_price)
        OR (direction = 'short' AND stop_price > entry_price)
    )
)"""


@pytest.fixture()
def db() -> sqlite3.Connection:
    """In-memory DB with the Phase B.1 constrained trades schema."""
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute(_CREATE_CONSTRAINED)
    return conn


def _insert(conn: sqlite3.Connection, **kwargs) -> None:
    """Insert a trade row using sensible defaults for required fields."""
    row = dict(
        ticker="TST",
        strategy="test_strat",
        direction="long",
        entry_date="2026-01-01",
        entry_price=100.0,
        shares=1,
        status="open",
    )
    row.update(kwargs)
    # Filter out None-valued keys so SQL defaults apply
    row_clean = {k: v for k, v in row.items() if v is not None or k in ("stop_price", "exit_price", "exit_date")}
    cols = ", ".join(row_clean.keys())
    placeholders = ", ".join("?" for _ in row_clean)
    conn.execute(f"INSERT INTO trades ({cols}) VALUES ({placeholders})", list(row_clean.values()))
    conn.commit()


# ── C1: closed trades must have exit_price AND exit_date ─────────────────────

class TestClosedTradeConstraint:
    def test_invalid_closed_no_exit_price_rejected(self, db: sqlite3.Connection) -> None:
        """C1: closed trade missing exit_price is rejected."""
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                "INSERT INTO trades (ticker, status, entry_price, shares, entry_date, exit_date) "
                "VALUES ('TST', 'closed', 100.0, 1, '2026-01-01', '2026-01-02')"
            )

    def test_valid_closed_passes(self, db: sqlite3.Connection) -> None:
        """C1: closed trade with both exit fields is accepted."""
        db.execute(
            "INSERT INTO trades (ticker, status, entry_price, shares, entry_date, exit_date, exit_price) "
            "VALUES ('TST', 'closed', 100.0, 1, '2026-01-01', '2026-01-02', 105.0)"
        )
        db.commit()
        assert db.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == 1


# ── C2: open trades must have valid entry fields ──────────────────────────────

class TestOpenTradeConstraint:
    def test_invalid_open_no_entry_price_rejected(self, db: sqlite3.Connection) -> None:
        """C2: open trade with entry_price=0 is rejected."""
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                "INSERT INTO trades (ticker, status, entry_price, shares, entry_date) "
                "VALUES ('TST', 'open', 0.0, 1, '2026-01-01')"
            )

    def test_invalid_open_zero_shares_rejected(self, db: sqlite3.Connection) -> None:
        """C2: open trade with shares=0 is rejected."""
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                "INSERT INTO trades (ticker, status, entry_price, shares, entry_date) "
                "VALUES ('TST', 'open', 100.0, 0, '2026-01-01')"
            )

    def test_valid_open_passes(self, db: sqlite3.Connection) -> None:
        """C2: open trade with valid entry fields is accepted."""
        db.execute(
            "INSERT INTO trades (ticker, status, entry_price, shares, entry_date) "
            "VALUES ('TST', 'open', 100.0, 5, '2026-01-01')"
        )
        db.commit()
        assert db.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == 1


# ── Pre-existing stop_price constraint ───────────────────────────────────────

class TestStopPriceConstraint:
    def test_invalid_stop_above_entry_rejected(self, db: sqlite3.Connection) -> None:
        """Existing constraint: long with stop_price >= entry_price is rejected."""
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                "INSERT INTO trades (ticker, status, direction, entry_price, shares, stop_price, entry_date) "
                "VALUES ('TST', 'open', 'long', 100.0, 1, 110.0, '2026-01-01')"
            )

    def test_valid_stop_below_entry_passes(self, db: sqlite3.Connection) -> None:
        """Existing constraint: long with stop_price < entry_price is accepted."""
        db.execute(
            "INSERT INTO trades (ticker, status, direction, entry_price, shares, stop_price, entry_date) "
            "VALUES ('TST', 'open', 'long', 100.0, 1, 90.0, '2026-01-01')"
        )
        db.commit()
        assert db.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == 1


# ── Pre-existing exit_date constraint ─────────────────────────────────────────

class TestDateConstraint:
    def test_invalid_exit_before_entry_rejected(self, db: sqlite3.Connection) -> None:
        """Existing constraint: exit_date < entry_date is rejected."""
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                "INSERT INTO trades "
                "(ticker, status, entry_price, shares, entry_date, exit_date, exit_price) "
                "VALUES ('TST', 'closed', 100.0, 1, '2026-01-10', '2026-01-05', 105.0)"
            )


# ── C5: status domain ─────────────────────────────────────────────────────────

class TestStatusDomainConstraint:
    def test_invalid_status_rejected(self, db: sqlite3.Connection) -> None:
        """C5: unrecognised status is rejected."""
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                "INSERT INTO trades (ticker, status, entry_price, shares, entry_date) "
                "VALUES ('TST', 'INVALID_STATUS', 100.0, 1, '2026-01-01')"
            )

    def test_all_valid_statuses_pass(self, db: sqlite3.Connection) -> None:
        """C5: all four canonical status values are accepted."""
        db.execute(
            "INSERT INTO trades (ticker, status, entry_price, shares, entry_date) "
            "VALUES ('T1', 'open', 100.0, 1, '2026-01-01')"
        )
        db.execute(
            "INSERT INTO trades (ticker, status, entry_price, shares, entry_date) "
            "VALUES ('T2', 'pending', 100.0, 1, '2026-01-01')"
        )
        db.execute(
            "INSERT INTO trades (ticker, status, entry_price, shares, entry_date) "
            "VALUES ('T3', 'cancelled', 100.0, 1, '2026-01-01')"
        )
        db.execute(
            "INSERT INTO trades "
            "(ticker, status, entry_price, shares, entry_date, exit_date, exit_price) "
            "VALUES ('T4', 'closed', 100.0, 1, '2026-01-01', '2026-01-02', 105.0)"
        )
        db.commit()
        assert db.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == 4


# ── Idempotency test ──────────────────────────────────────────────────────────

class TestMigrationIdempotency:
    def _load_migration(self):
        """Dynamically load the migration module."""
        spec_path = (
            Path(__file__).resolve().parents[1]
            / "scripts"
            / "migrations"
            / "2026-04-29-trades-check-constraints.py"
        )
        spec = importlib.util.spec_from_file_location("migration_check", str(spec_path))
        mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
        return mod

    def _make_tmp_db(self, tmp_path: Path) -> Path:
        """Create a minimal tmp DB with the OLD (unconstrained) trades schema."""
        db_path = tmp_path / "test_atlas.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(_CREATE_UNCONSTRAINED)
        # Insert a couple of clean rows
        conn.execute(
            "INSERT INTO trades (ticker, strategy, entry_date, entry_price, shares, status) "
            "VALUES ('AAPL', 'test', '2026-01-01', 150.0, 2, 'open')"
        )
        conn.execute(
            "INSERT INTO trades (ticker, strategy, entry_date, entry_price, shares, status, exit_date, exit_price) "
            "VALUES ('MSFT', 'test', '2026-01-01', 200.0, 1, 'closed', '2026-01-05', 210.0)"
        )
        conn.commit()
        conn.close()
        return db_path

    def test_migration_idempotent(self, tmp_path: Path, monkeypatch) -> None:
        """Applying the migration twice returns 0 both times and row count is unchanged."""
        mod = self._load_migration()
        db_path = self._make_tmp_db(tmp_path)

        # Point the migration at our tmp DB
        monkeypatch.setattr(mod, "DB_PATH", db_path)

        # First apply
        rc1 = mod._run(apply=True, allow_fix=False)
        assert rc1 == 0, "First migration apply failed"

        # Verify row count preserved
        conn = sqlite3.connect(str(db_path))
        count = conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
        conn.close()
        assert count == 2, f"Expected 2 rows after migration, got {count}"

        # Second apply — must be a no-op (idempotent)
        rc2 = mod._run(apply=True, allow_fix=False)
        assert rc2 == 0, "Second migration apply failed (should be no-op)"

        # Row count still preserved
        conn = sqlite3.connect(str(db_path))
        count2 = conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
        conn.close()
        assert count2 == 2, f"Row count changed after idempotent run: {count2}"
