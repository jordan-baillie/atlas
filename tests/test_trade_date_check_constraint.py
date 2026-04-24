"""
Regression tests for P1-7 + CHECK constraint — trades date consistency.

Verifies that the CHECK (exit_date IS NULL OR exit_date >= entry_date)
constraint in the trades table is enforced at the DB level.
"""
from __future__ import annotations

import sqlite3
import pytest
from db import atlas_db


@pytest.fixture(autouse=True)
def _isolate_db(tmp_path, monkeypatch):
    """Each test gets a fresh isolated DB."""
    db_path = tmp_path / "test_trade_dates.db"
    monkeypatch.setattr(atlas_db, "_db_path_override", str(db_path))
    atlas_db.init_db(str(db_path))
    yield


def _insert_trade(conn, entry_date: str, exit_date: str | None, status: str = "closed") -> int:
    """Helper: raw INSERT into trades, returns lastrowid."""
    cursor = conn.execute(
        """
        INSERT INTO trades
            (ticker, strategy, direction, entry_date, entry_price, shares, exit_date, status)
        VALUES ('TST', 'test_strategy', 'long', ?, 100.0, 10, ?, ?)
        """,
        (entry_date, exit_date, status),
    )
    conn.commit()
    return cursor.lastrowid


class TestTradesCheckConstraint:

    def test_inverted_dates_insert_fails(self, tmp_path):
        """entry > exit → CHECK constraint fails."""
        conn = sqlite3.connect(str(tmp_path / "test_trade_dates.db"))
        with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
            conn.execute(
                """
                INSERT INTO trades
                    (ticker, strategy, direction, entry_date, entry_price, shares,
                     exit_date, status)
                VALUES ('ZZZ', 'x', 'long', '2026-04-20', 100, 1, '2026-04-19', 'closed')
                """
            )
            conn.commit()
        conn.close()

    def test_same_day_entry_exit_succeeds(self, tmp_path):
        """Same-day trade (entry == exit) is allowed."""
        conn = sqlite3.connect(str(tmp_path / "test_trade_dates.db"))
        rowid = _insert_trade(conn, "2026-04-20", "2026-04-20")
        assert rowid > 0
        conn.close()

    def test_null_exit_date_succeeds(self, tmp_path):
        """Open position with exit_date=NULL passes the constraint."""
        conn = sqlite3.connect(str(tmp_path / "test_trade_dates.db"))
        rowid = _insert_trade(conn, "2026-04-20", None, status="open")
        assert rowid > 0
        conn.close()

    def test_normal_closed_trade_succeeds(self, tmp_path):
        """exit > entry is the happy path."""
        conn = sqlite3.connect(str(tmp_path / "test_trade_dates.db"))
        rowid = _insert_trade(conn, "2026-04-15", "2026-04-20")
        assert rowid > 0
        conn.close()

    def test_update_to_inverted_dates_fails(self, tmp_path):
        """UPDATE that creates exit < entry should also be rejected."""
        conn = sqlite3.connect(str(tmp_path / "test_trade_dates.db"))
        rowid = _insert_trade(conn, "2026-04-15", "2026-04-20")
        with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
            conn.execute(
                "UPDATE trades SET exit_date = '2026-04-10' WHERE id = ?", (rowid,)
            )
            conn.commit()
        conn.close()

    def test_schema_has_check_constraint(self, tmp_path):
        """Verify the CHECK clause is present in sqlite_master."""
        conn = sqlite3.connect(str(tmp_path / "test_trade_dates.db"))
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='trades'"
        ).fetchone()
        conn.close()
        assert row is not None, "trades table not found"
        assert "CHECK" in row[0], f"CHECK constraint missing from schema:\n{row[0]}"
        assert "exit_date >= entry_date" in row[0]

    def test_all_indexes_present(self, tmp_path):
        """All 4 expected indexes on trades must survive the migration."""
        conn = sqlite3.connect(str(tmp_path / "test_trade_dates.db"))
        indexes = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='trades'"
            ).fetchall()
        }
        conn.close()
        expected = {
            "idx_trades_status",
            "idx_trades_strategy",
            "idx_trades_dates",
            "idx_trades_unique_open",
        }
        assert expected.issubset(indexes), f"Missing indexes: {expected - indexes}"

    def test_p1_7_regression_swapped_rows_are_valid(self, tmp_path):
        """After the P1-7 data fix the swapped dates must satisfy the constraint."""
        conn = sqlite3.connect(str(tmp_path / "test_trade_dates.db"))
        # Simulate the fixed rows (entry <= exit after swap)
        fixed = [
            ("D",    "2026-03-24", "2026-03-25"),
            ("ECL",  "2026-03-24", "2026-03-25"),
            ("NOC",  "2026-03-24", "2026-03-25"),
            ("CVX",  "2026-03-27", "2026-03-28"),
            ("AMT",  "2026-04-09", "2026-04-10"),
            ("MRVL", "2026-04-10", "2026-04-11"),
        ]
        for ticker, entry, exit_d in fixed:
            conn.execute(
                """
                INSERT INTO trades (ticker, strategy, direction, entry_date, entry_price,
                                    shares, exit_date, status)
                VALUES (?, 'test', 'long', ?, 100, 1, ?, 'closed')
                """,
                (ticker, entry, exit_d),
            )
        conn.commit()
        count = conn.execute("SELECT COUNT(*) FROM trades WHERE exit_date < entry_date").fetchone()[0]
        conn.close()
        assert count == 0, "Swapped rows still violate the constraint"
