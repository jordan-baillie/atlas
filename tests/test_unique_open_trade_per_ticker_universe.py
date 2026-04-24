"""
Regression tests for P0-1: UNIQUE partial index on trades(ticker, universe)
WHERE status='open'.

Root cause: concurrent reconcile_entry_fills (sp500 + commodity_etfs) both
passed a SELECT dedup check within 11ms and both INSERTed — SELECT→INSERT
was not atomic.  The fix: a UNIQUE partial index at the DB level makes the
INSERT itself atomic, and record_trade_entry catches IntegrityError gracefully.
"""

from __future__ import annotations

from datetime import datetime, timedelta
import logging
import sqlite3

import pytest

from db.atlas_db import get_db, record_trade_entry


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _base_trade(**overrides) -> dict:
    """Return a minimal valid trade dict."""
    defaults = dict(
        ticker="TST",
        universe="sp500",
        strategy="momentum",
        direction="long",
        entry_price=100.0,
        shares=10,
        stop_price=90.0,
        take_profit=None,
        confidence=0.7,
        regime_state=None,
        config_version="v1.0",
    )
    defaults.update(overrides)
    return defaults


# ---------------------------------------------------------------------------
# Test 1: second INSERT with same (ticker, universe) while first is open raises
#         IntegrityError at the SQLite level.
# ---------------------------------------------------------------------------

class TestUniqueIndexEnforcement:
    def test_direct_insert_same_ticker_universe_raises(self):
        """Raw INSERT into trades (bypassing record_trade_entry) raises IntegrityError."""
        with get_db() as db:
            db.execute(
                "INSERT INTO trades (ticker, strategy, universe, direction, "
                "entry_date, entry_price, shares, stop_price, status) "
                "VALUES ('ABC','strat','sp500','long','2026-01-01',100,1,90,'open')"
            )
            with pytest.raises(sqlite3.IntegrityError):
                db.execute(
                    "INSERT INTO trades (ticker, strategy, universe, direction, "
                    "entry_date, entry_price, shares, stop_price, status) "
                    "VALUES ('ABC','strat2','sp500','long','2026-01-02',101,1,91,'open')"
                )

    def test_record_trade_entry_blocks_duplicate(self):
        """record_trade_entry returns None (not None=id) on the second call."""
        t = _base_trade(ticker="DUP")
        id1 = record_trade_entry(**t)
        id2 = record_trade_entry(**t)

        assert id1 is not None, "First insert should succeed and return a row id"
        assert isinstance(id1, int)
        assert id2 is None, "Second insert for same open (ticker, universe) should be blocked (None)"

    def test_record_trade_entry_second_call_logs_warning(self, caplog):
        """record_trade_entry emits a WARNING (not an exception) on duplicate."""
        t = _base_trade(ticker="WARNTEST")
        record_trade_entry(**t)
        with caplog.at_level(logging.WARNING):
            result = record_trade_entry(**t)
        assert result is None
        assert any(
            "duplicate open trade blocked" in r.message and "WARNTEST" in r.message
            for r in caplog.records
        ), f"Expected warning not found. Records: {[r.message for r in caplog.records]}"


# ---------------------------------------------------------------------------
# Test 2: same ticker but DIFFERENT universe IS allowed (index is partial and
#         covers (ticker, universe) together — not just ticker).
# ---------------------------------------------------------------------------

class TestDifferentUniverseAllowed:
    def test_same_ticker_different_universe(self):
        """Two open trades with same ticker but different universe are allowed."""
        t1 = _base_trade(ticker="CROSS", universe="sp500")
        t2 = _base_trade(ticker="CROSS", universe="commodity_etfs")

        id1 = record_trade_entry(**t1)
        id2 = record_trade_entry(**t2)

        assert id1 is not None, "sp500 insert should succeed"
        assert id2 is not None, "commodity_etfs insert should succeed — different universe"
        assert id1 != id2

    def test_same_ticker_different_universe_raw_sql(self):
        """Raw SQL also allows cross-universe open trades."""
        with get_db() as db:
            db.execute(
                "INSERT INTO trades (ticker, strategy, universe, direction, "
                "entry_date, entry_price, shares, stop_price, status) "
                "VALUES ('XUNI','s','sp500','long','2026-01-01',10,1,9,'open')"
            )
            # This should NOT raise
            db.execute(
                "INSERT INTO trades (ticker, strategy, universe, direction, "
                "entry_date, entry_price, shares, stop_price, status) "
                "VALUES ('XUNI','s','asx','long','2026-01-01',10,1,9,'open')"
            )


# ---------------------------------------------------------------------------
# Test 3: after closing the first, a new open CAN be inserted.
# ---------------------------------------------------------------------------

class TestReopenAfterClose:
    def test_can_reopen_after_close(self):
        """Once trade is closed, inserting a new open for same (ticker, universe) succeeds."""
        t = _base_trade(ticker="REOPEN", universe="sp500")

        id1 = record_trade_entry(**t)
        assert id1 is not None

        # Close it
        with get_db() as db:
            row = db.execute("SELECT entry_date FROM trades WHERE id=?", (id1,)).fetchone()
            entry_dt = row['entry_date'][:10]  # YYYY-MM-DD (strip any time part)
            # Exit one day after entry
            exit_dt = (datetime.fromisoformat(entry_dt) + timedelta(days=1)).strftime('%Y-%m-%d')
            db.execute(
                "UPDATE trades SET status='closed', exit_date=?, "
                "exit_price=110.0, pnl=100.0 WHERE id=?",
                (exit_dt, id1),
            )

        # Now a new open should succeed
        id2 = record_trade_entry(**t)
        assert id2 is not None, "New open after close should be allowed"
        assert id2 != id1

    def test_closed_rows_dont_block_raw_sql(self):
        """Partial index only covers status='open'; closed rows do not conflict."""
        with get_db() as db:
            db.execute(
                "INSERT INTO trades (ticker, strategy, universe, direction, "
                "entry_date, entry_price, shares, stop_price, status) "
                "VALUES ('CLSD','s','sp500','long','2026-01-01',10,1,9,'closed')"
            )
            # Same ticker/universe but both closed — should not conflict
            db.execute(
                "INSERT INTO trades (ticker, strategy, universe, direction, "
                "entry_date, entry_price, shares, stop_price, status) "
                "VALUES ('CLSD','s','sp500','long','2026-01-02',10,1,9,'closed')"
            )
            # And an open one now is also fine (no existing open)
            db.execute(
                "INSERT INTO trades (ticker, strategy, universe, direction, "
                "entry_date, entry_price, shares, stop_price, status) "
                "VALUES ('CLSD','s','sp500','long','2026-01-03',10,1,9,'open')"
            )


# ---------------------------------------------------------------------------
# Test 4: record_trade_entry graceful handling — calling path must not crash.
# ---------------------------------------------------------------------------

class TestGracefulConflictHandling:
    def test_caller_path_does_not_raise(self):
        """Simulate a caller that does NOT guard for None — must not crash."""
        t = _base_trade(ticker="GRACE", universe="sp500")
        record_trade_entry(**t)

        # Second call — simulates concurrent process
        try:
            result = record_trade_entry(**t)
        except Exception as exc:  # noqa: BLE001
            pytest.fail(f"record_trade_entry raised unexpectedly on dupe: {exc}")

        assert result is None

    def test_successful_insert_returns_int_id(self):
        """Verify the success path returns an integer row id (not just None)."""
        t = _base_trade(ticker="IDCHECK", universe="sp500")
        row_id = record_trade_entry(**t)
        assert isinstance(row_id, int), f"Expected int, got {type(row_id)}: {row_id}"
        assert row_id > 0

    def test_universe_null_does_not_conflict_with_named_universe(self):
        """If universe is NULL and another is named 'sp500', they should not conflict
        (SQLite UNIQUE treats NULL != NULL)."""
        with get_db() as db:
            db.execute(
                "INSERT INTO trades (ticker, strategy, universe, direction, "
                "entry_date, entry_price, shares, stop_price, status) "
                "VALUES ('NULLU','s',NULL,'long','2026-01-01',10,1,9,'open')"
            )
            # A named-universe open should be fine
            db.execute(
                "INSERT INTO trades (ticker, strategy, universe, direction, "
                "entry_date, entry_price, shares, stop_price, status) "
                "VALUES ('NULLU','s','sp500','long','2026-01-01',10,1,9,'open')"
            )
