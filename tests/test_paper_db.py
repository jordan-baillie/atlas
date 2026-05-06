"""Tests for paper-trade DB helpers (sub-phase 1.2).

Covers:
  - Schema parity between paper_trades / trades and
    paper_position_protective_orders / position_protective_orders
  - record_paper_trade_entry / record_paper_trade_exit round-trips
  - Isolation guarantees (paper ops don't touch live tables)
  - get_open_paper_trades / get_paper_trades_for_universe
  - paper protective-order helpers
  - Migration idempotency
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Helper — fetch column names for a table from a live connection
# ---------------------------------------------------------------------------

def _columns(conn: sqlite3.Connection, table: str) -> list[str]:
    """Return ordered list of column names for *table*."""
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()  # noqa: S608
    return [r[1] for r in rows]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestPaperTradesSchema:
    """Schema parity: paper_trades mirrors trades + paper_account_id."""

    def test_paper_trades_schema_matches_trades(self):
        """paper_trades must have every column from trades plus paper_account_id."""
        import db.atlas_db as adb

        with adb.get_db() as conn:
            live_cols = _columns(conn, "trades")
            paper_cols = _columns(conn, "paper_trades")

        # paper_trades must contain all live cols
        missing = [c for c in live_cols if c not in paper_cols]
        assert not missing, f"paper_trades is missing live-trades columns: {missing}"

        # paper_trades must also have the extra column
        assert "paper_account_id" in paper_cols, (
            "paper_trades is missing the required paper_account_id column"
        )

        # paper_trades should have exactly one extra column (paper_account_id)
        extra = [c for c in paper_cols if c not in live_cols]
        assert extra == ["paper_account_id"], (
            f"Unexpected extra columns in paper_trades: {extra}"
        )

    def test_paper_position_protective_orders_mirror_schema(self):
        """paper_position_protective_orders must have the same columns as position_protective_orders."""
        import db.atlas_db as adb

        with adb.get_db() as conn:
            live_cols = _columns(conn, "position_protective_orders")
            paper_cols = _columns(conn, "paper_position_protective_orders")

        assert sorted(live_cols) == sorted(paper_cols), (
            f"Column mismatch.\n  live: {sorted(live_cols)}\n  paper: {sorted(paper_cols)}"
        )


class TestRecordPaperTradeEntry:
    """record_paper_trade_entry writes to paper_trades, not trades."""

    def test_record_paper_trade_entry_writes_paper_table(self):
        """Calling the helper should create a row in paper_trades."""
        import db.atlas_db as adb

        new_id = adb.record_paper_trade_entry(
            ticker="AAPL",
            strategy="momentum_vol",
            universe="sp500",
            entry_price=175.0,
            shares=10,
            stop_price=165.0,
            take_profit=190.0,
            confidence=0.8,
            regime_state="bull",
            paper_account_id="PA3TTBLZM6M7",
        )
        assert new_id is not None, "Expected a valid row id, got None"

        with adb.get_db() as conn:
            row = conn.execute(
                "SELECT * FROM paper_trades WHERE id=?", (new_id,)
            ).fetchone()
        assert row is not None, "Row not found in paper_trades after insert"
        assert dict(row)["paper_account_id"] == "PA3TTBLZM6M7"
        assert dict(row)["ticker"] == "AAPL"
        assert dict(row)["status"] == "open"

    def test_record_paper_trade_entry_does_not_touch_trades(self):
        """Paper helper must NOT insert anything into the live trades table."""
        import db.atlas_db as adb

        adb.record_paper_trade_entry(
            ticker="MSFT",
            strategy="momentum_vol",
            universe="sp500",
            entry_price=300.0,
            shares=5,
            stop_price=280.0,
            take_profit=330.0,
            confidence=0.75,
            regime_state="bull",
            paper_account_id="PA3TTBLZM6M7",
        )
        with adb.get_db() as conn:
            live_row = conn.execute(
                "SELECT id FROM trades WHERE ticker='MSFT' AND status='open'"
            ).fetchone()
        assert live_row is None, (
            "record_paper_trade_entry must NOT write to the live trades table"
        )

    def test_record_paper_trade_entry_no_paper_account_id(self):
        """paper_account_id kwarg is optional; must default to NULL."""
        import db.atlas_db as adb

        new_id = adb.record_paper_trade_entry(
            ticker="GOOG",
            strategy="momentum_vol",
            universe="sp500",
            entry_price=120.0,
            shares=3,
            stop_price=112.0,
            take_profit=132.0,
            confidence=0.7,
            regime_state=None,
        )
        assert new_id is not None
        with adb.get_db() as conn:
            row = conn.execute(
                "SELECT paper_account_id FROM paper_trades WHERE id=?", (new_id,)
            ).fetchone()
        assert row is not None
        assert row[0] is None, "paper_account_id should be NULL when not provided"


class TestRecordPaperTradeExit:
    """record_paper_trade_exit closes a paper trade row."""

    def test_record_paper_trade_exit_updates_paper_row(self):
        """Open a paper trade then close it — round trip check."""
        import db.atlas_db as adb

        new_id = adb.record_paper_trade_entry(
            ticker="NVDA",
            strategy="momentum_vol",
            universe="sp500",
            entry_price=500.0,
            shares=2,
            stop_price=470.0,
            take_profit=540.0,
            confidence=0.85,
            regime_state="bull",
            paper_account_id="PA3TTBLZM6M7",
        )
        assert new_id is not None

        adb.record_paper_trade_exit(
            ticker="NVDA",
            strategy="momentum_vol",
            exit_price=530.0,
            exit_reason="take_profit",
        )

        with adb.get_db() as conn:
            row = dict(conn.execute(
                "SELECT * FROM paper_trades WHERE id=?", (new_id,)
            ).fetchone())

        assert row["status"] == "closed"
        assert row["exit_price"] == pytest.approx(530.0)
        assert row["exit_reason"] == "take_profit"
        # pnl = (530 - 500) * 2 = 60
        assert row["pnl"] == pytest.approx(60.0)

    def test_record_paper_trade_exit_does_not_touch_live_trades(self):
        """Exiting a paper trade must NOT affect the live trades table."""
        import db.atlas_db as adb

        # Insert a live trade directly so we can verify it's untouched
        with adb.get_db() as conn:
            conn.execute(
                """INSERT INTO trades
                    (ticker, strategy, universe, direction, entry_date, entry_price,
                     shares, stop_price, status, superseded)
                   VALUES ('META', 'momentum_vol', 'sp500', 'long', '2026-01-01',
                           400.0, 1, 380.0, 'open', 0)"""
            )

        # Open + close a paper trade for the same ticker
        adb.record_paper_trade_entry(
            ticker="META",
            strategy="momentum_vol",
            universe="sp500",
            entry_price=400.0,
            shares=1,
            stop_price=380.0,
            take_profit=430.0,
            confidence=0.7,
            regime_state="bull",
        )
        adb.record_paper_trade_exit(
            ticker="META",
            strategy="momentum_vol",
            exit_price=420.0,
            exit_reason="target",
        )

        # Live trade must still be open
        with adb.get_db() as conn:
            live = dict(conn.execute(
                "SELECT * FROM trades WHERE ticker='META' AND status='open'"
            ).fetchone())
        assert live["status"] == "open", "Live trade should not be affected by paper exit"


class TestGetOpenPaperTrades:
    """get_open_paper_trades and get_paper_trades_for_universe isolation."""

    def test_get_open_paper_trades_returns_paper_rows(self):
        """get_open_paper_trades should return paper trade rows."""
        import db.atlas_db as adb

        adb.record_paper_trade_entry(
            ticker="TSLA",
            strategy="momentum_vol",
            universe="sp500",
            entry_price=200.0,
            shares=4,
            stop_price=188.0,
            take_profit=220.0,
            confidence=0.8,
            regime_state="bull",
        )
        open_paper = adb.get_open_paper_trades()
        tickers = [r["ticker"] for r in open_paper]
        assert "TSLA" in tickers

    def test_get_open_paper_trades_isolates_paper_universe(self):
        """get_open_paper_trades must not return rows from the live trades table."""
        import db.atlas_db as adb

        # Insert a live open trade
        with adb.get_db() as conn:
            conn.execute(
                """INSERT INTO trades
                    (ticker, strategy, universe, direction, entry_date, entry_price,
                     shares, stop_price, status, superseded)
                   VALUES ('AMZN', 'momentum_vol', 'sp500', 'long', '2026-01-10',
                           180.0, 3, 168.0, 'open', 0)"""
            )

        # Insert a paper open trade for the same ticker
        adb.record_paper_trade_entry(
            ticker="AMZN",
            strategy="momentum_vol",
            universe="sp500",
            entry_price=180.0,
            shares=3,
            stop_price=168.0,
            take_profit=198.0,
            confidence=0.8,
            regime_state="bull",
        )

        paper_open = adb.get_open_paper_trades()
        live_open = adb.get_open_positions()

        paper_tickers = [r["ticker"] for r in paper_open]
        live_tickers = [r["ticker"] for r in live_open]

        # Both should see AMZN — but from their own tables
        assert "AMZN" in paper_tickers
        assert "AMZN" in live_tickers

        # Cross-check: live result has NO paper_account_id column (only paper does)
        for r in live_open:
            assert "paper_account_id" not in r, (
                "Live trades row must not have paper_account_id"
            )

    def test_get_paper_trades_for_universe_filters_correctly(self):
        """get_paper_trades_for_universe returns only rows for the given universe."""
        import db.atlas_db as adb

        # Two paper trades in different universes
        adb.record_paper_trade_entry(
            ticker="BHP",
            strategy="momentum_vol",
            universe="asx200",
            entry_price=40.0,
            shares=10,
            stop_price=37.0,
            take_profit=44.0,
            confidence=0.7,
            regime_state=None,
        )
        adb.record_paper_trade_entry(
            ticker="INTC",
            strategy="momentum_vol",
            universe="sp500",
            entry_price=30.0,
            shares=20,
            stop_price=27.0,
            take_profit=33.0,
            confidence=0.6,
            regime_state=None,
        )

        sp500_trades = adb.get_paper_trades_for_universe("sp500")
        asx_trades = adb.get_paper_trades_for_universe("asx200")

        sp500_tickers = [r["ticker"] for r in sp500_trades]
        asx_tickers = [r["ticker"] for r in asx_trades]

        assert "INTC" in sp500_tickers
        assert "BHP" not in sp500_tickers
        assert "BHP" in asx_tickers
        assert "INTC" not in asx_tickers


class TestPaperProtectiveOrders:
    """Paper protective-order helpers operate on paper_position_protective_orders."""

    def test_upsert_and_get_paper_protective_record(self):
        """upsert + get round-trip for a paper protective record."""
        import db.atlas_db as adb

        adb.upsert_paper_protective_record(
            market_id="sp500",
            ticker="ORCL",
            trade_id=1,
            position_qty=5.0,
            stop_order_id="stop-paper-001",
            stop_price=88.0,
            tp_order_id="tp-paper-001",
            tp_price=98.0,
            oco_class="oco",
        )

        rec = adb.get_paper_protective_record("sp500", "ORCL")
        assert rec is not None, "Expected a protective record, got None"
        assert rec["ticker"] == "ORCL"
        assert rec["stop_order_id"] == "stop-paper-001"
        assert rec["status"] == "active"

    def test_paper_protective_record_does_not_touch_live_table(self):
        """Paper protective upsert must not create/modify a live protective record."""
        import db.atlas_db as adb

        adb.upsert_paper_protective_record(
            market_id="sp500",
            ticker="CRM",
            trade_id=None,
            position_qty=2.0,
            stop_order_id="stop-paper-002",
            stop_price=140.0,
        )

        live_rec = adb.get_protective_record("sp500", "CRM")
        assert live_rec is None, (
            "upsert_paper_protective_record must not write to position_protective_orders"
        )

    def test_close_paper_protective_record(self):
        """close_paper_protective_record sets status to 'closed'."""
        import db.atlas_db as adb

        adb.upsert_paper_protective_record(
            market_id="sp500",
            ticker="IBM",
            trade_id=None,
            position_qty=3.0,
            stop_price=120.0,
        )
        adb.close_paper_protective_record("sp500", "IBM")

        rec = adb.get_paper_protective_record("sp500", "IBM")
        assert rec is None, "get_paper_protective_record should return None after close"

    def test_list_active_paper_protective_records(self):
        """list_active_paper_protective_records returns only active paper records."""
        import db.atlas_db as adb

        adb.upsert_paper_protective_record(
            market_id="sp500",
            ticker="UBER",
            trade_id=None,
            position_qty=8.0,
            stop_price=60.0,
        )
        adb.upsert_paper_protective_record(
            market_id="sp500",
            ticker="LYFT",
            trade_id=None,
            position_qty=10.0,
            stop_price=12.0,
        )
        adb.close_paper_protective_record("sp500", "LYFT")

        active = adb.list_active_paper_protective_records("sp500")
        active_tickers = [r["ticker"] for r in active]
        assert "UBER" in active_tickers
        assert "LYFT" not in active_tickers


class TestMigrationIdempotent:
    """Migration script must be safe to run twice (idempotent)."""

    def test_migration_idempotent(self, tmp_path: Path):
        """Running the migration twice on the same DB must not fail or duplicate tables."""
        import importlib.util

        import db.atlas_db as adb

        # Load the migration module from its hyphenated filename
        _mig_path = (
            Path(__file__).resolve().parent.parent
            / "scripts"
            / "migrations"
            / "2026-05-06-paper-trades-schema.py"
        )
        _spec = importlib.util.spec_from_file_location("paper_trades_migration", _mig_path)
        mig = importlib.util.module_from_spec(_spec)  # type: ignore[arg-type]
        _spec.loader.exec_module(mig)  # type: ignore[union-attr]

        db_file = tmp_path / "test_migration.db"

        # Initialise a fresh DB using init_db so all base tables exist
        import db.atlas_db as _adb
        original = _adb._db_path_override
        _adb._db_path_override = str(db_file)
        try:
            adb.init_db()
        finally:
            _adb._db_path_override = original

        # First run
        mig.run(db_path=db_file, dry_run=False)

        # Verify tables exist
        conn = sqlite3.connect(str(db_file))
        try:
            tables = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()}
        finally:
            conn.close()

        assert "paper_trades" in tables
        assert "paper_position_protective_orders" in tables

        # Second run — must not raise
        mig.run(db_path=db_file, dry_run=False)

        # Tables still present after second run
        conn = sqlite3.connect(str(db_file))
        try:
            tables2 = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()}
            # Exactly one paper_trades table (no duplicates)
            pt_count = conn.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='paper_trades'"
            ).fetchone()[0]
        finally:
            conn.close()

        assert "paper_trades" in tables2
        assert "paper_position_protective_orders" in tables2
        assert pt_count == 1, f"Expected exactly 1 paper_trades table, got {pt_count}"
