"""
Regression tests for phantom trade row deduplication via uq_trades_natural_key.

Context: commit 1ef93bae fixed the synthesized-exit reconciler that was creating
zombie CRWD rows (R-05a). These tests assert the UNIQUE INDEX correctly rejects
identical-exit-date duplicates and permits same-price/shares trades on different dates.
"""
import sqlite3
import pytest
from db.atlas_db import init_db


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _insert_closed_trade(conn: sqlite3.Connection, *, ticker: str, entry_date: str,
                          exit_date: str, entry_price: float, exit_price: float,
                          shares: float, pnl: float) -> int:
    """Insert a minimal closed trade row and return its rowid."""
    cursor = conn.execute(
        """
        INSERT INTO trades
            (ticker, strategy, universe, status, entry_date, exit_date,
             entry_price, exit_price, shares, pnl, exit_reason)
        VALUES (?, ?, ?, 'closed', ?, ?, ?, ?, ?, ?, 'stop_loss')
        """,
        (ticker, "momentum_breakout", "sp500",
         entry_date, exit_date, entry_price, exit_price, shares, pnl),
    )
    conn.commit()
    return cursor.lastrowid


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestUqTradesNaturalKeyIndex:
    """uq_trades_natural_key enforces no duplicate (ticker, DATE(exit_date), exit_price, shares)
    for closed rows that have a non-NULL exit_date."""

    def test_duplicate_exit_date_raises_integrity_error(self, tmp_path):
        """
        Test 1: Inserting a second closed row with IDENTICAL (ticker, DATE(exit_date),
        exit_price, shares) should raise sqlite3.IntegrityError — the UNIQUE INDEX fires.
        """
        db_file = tmp_path / "test_dedup.db"
        conn = sqlite3.connect(str(db_file))
        conn.execute("PRAGMA foreign_keys = ON")
        init_db(str(db_file))

        # Insert the "real" trade
        _insert_closed_trade(
            conn,
            ticker="CRWD",
            entry_date="2026-05-08T23:46:03",
            exit_date="2026-05-08T23:46:19",
            entry_price=499.55,
            exit_price=494.7089,
            shares=1,
            pnl=-4.8411,
        )

        # Attempt to insert the zombie duplicate (same exit DATE, same exit_price, same shares)
        with pytest.raises(sqlite3.IntegrityError, match="uq_trades_natural_key"):
            conn.execute(
                """
                INSERT INTO trades
                    (ticker, strategy, universe, status, entry_date, exit_date,
                     entry_price, exit_price, shares, pnl, exit_reason)
                VALUES ('CRWD', 'momentum_breakout', 'sp500', 'closed',
                        '2026-05-08T23:46:03', '2026-05-08T23:46:40',
                        499.55, 494.7089, 1, -4.8411, 'stop_loss')
                """,
            )
            conn.commit()

        conn.close()

    def test_different_exit_date_succeeds(self, tmp_path):
        """
        Test 2: A second closed row with the SAME (ticker, exit_price, shares) but
        DIFFERENT exit_date (next day) should be inserted successfully.
        This mirrors the legitimate 'two positions opened/closed on consecutive days'
        scenario (e.g. D mean_reversion ids 92 and 124).
        """
        db_file = tmp_path / "test_dedup_diffdate.db"
        conn = sqlite3.connect(str(db_file))
        conn.execute("PRAGMA foreign_keys = ON")
        init_db(str(db_file))

        # Trade 1: closed May 8
        _insert_closed_trade(
            conn,
            ticker="CRWD",
            entry_date="2026-05-07T22:00:00",
            exit_date="2026-05-08T23:46:19",
            entry_price=499.55,
            exit_price=494.7089,
            shares=1,
            pnl=-4.8411,
        )

        # Trade 2: closed May 9 (next day) — same price/shares, different exit_date
        row_id = _insert_closed_trade(
            conn,
            ticker="CRWD",
            entry_date="2026-05-08T23:50:00",
            exit_date="2026-05-09T09:30:00",
            entry_price=499.55,
            exit_price=494.7089,
            shares=1,
            pnl=-4.8411,
        )

        assert row_id is not None and row_id > 0, "Second trade on different date should insert"

        count = conn.execute(
            "SELECT COUNT(*) FROM trades WHERE ticker='CRWD' AND status='closed'"
        ).fetchone()[0]
        assert count == 2, f"Expected 2 CRWD closed rows, got {count}"

        conn.close()

    def test_uq_trades_natural_key_index_exists(self, tmp_path):
        """
        Test 3 (smoke): The UNIQUE INDEX uq_trades_natural_key must exist in
        the trades table schema so that the dedup guard is always active.
        """
        db_file = tmp_path / "test_index_smoke.db"
        conn = sqlite3.connect(str(db_file))
        init_db(str(db_file))

        index_names = [
            row[1]
            for row in conn.execute("PRAGMA index_list(trades)").fetchall()
        ]

        assert "uq_trades_natural_key" in index_names, (
            f"UNIQUE INDEX uq_trades_natural_key not found in trades indexes: {index_names}"
        )

        # Also verify the index is UNIQUE (not just a plain index)
        # PRAGMA index_list returns: (seq, name, unique, origin, partial)
        all_indexes = conn.execute("PRAGMA index_list(trades)").fetchall()
        uq_row = next((r for r in all_indexes if r[1] == "uq_trades_natural_key"), None)
        assert uq_row is not None, "uq_trades_natural_key not found"
        assert uq_row[2] == 1, "uq_trades_natural_key must be a UNIQUE index (unique col=1)"

        conn.close()
