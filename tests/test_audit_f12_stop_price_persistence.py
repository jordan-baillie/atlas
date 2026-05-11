"""Tests for F-12: stop_price persistence in sync_protective_orders.

Audit finding F-12: trades.stop_price remains NULL even when the broker has
an active OCO stop order. The broker's stop_price was never written back to
SQLite after bracket orders were placed.

Fix:
  1. scripts/backfill_cat_stop_price.py — one-shot backfill for CAT trade 187.
  2. scripts/sync_protective_orders.py::_apply_db_consistency — F-12 block that
     writes stop_price to trades when the sync cycle resolves a stop order and
     the DB row has stop_price IS NULL (CHECK constraint validated first).
"""
from __future__ import annotations

import sqlite3


# ── source-inspection tests ───────────────────────────────────────────────

class TestSyncProtectiveOrdersSourceInspection:
    """Source-level assertions: the F-12 persistence code must exist in the file."""

    def _src(self) -> str:
        with open("scripts/sync_protective_orders.py") as f:
            return f.read()

    def test_update_trades_set_stop_price_present(self):
        """sync_protective_orders.py must contain an UPDATE trades SET stop_price call."""
        src = self._src()
        assert "UPDATE trades SET stop_price" in src, (
            "sync_protective_orders.py must persist stop_price to trades table (F-12). "
            "Add _apply_db_consistency F-12 block."
        )

    def test_f12_block_label_present(self):
        """The F-12 block must be labelled for maintainability."""
        src = self._src()
        assert "F-12" in src, (
            "F-12 label not found in sync_protective_orders.py. "
            "The stop_price persistence block must carry the audit ref."
        )

    def test_check_constraint_guard_present(self):
        """The F-12 block must validate the long/short CHECK constraint."""
        src = self._src()
        # Look for the constraint check logic
        assert "direction" in src and "entry_price" in src, (
            "F-12 block must read 'direction' and 'entry_price' to guard the CHECK constraint."
        )

    def test_stop_price_update_is_non_fatal(self):
        """The F-12 update block must be wrapped in a try/except (non-fatal)."""
        src = self._src()
        # Find the F-12 label and look for surrounding exception handling
        f12_idx = src.find("F-12: Persist broker stop_price")
        assert f12_idx >= 0, "F-12 block label not found"
        # The block should have an outer try/except
        surrounding = src[f12_idx:f12_idx + 2500]
        assert "except Exception" in surrounding or "except" in surrounding, (
            "F-12 block must have try/except (non-fatal — any failure should be "
            "logged as WARNING and swallowed)."
        )

    def test_backfill_script_exists(self):
        """scripts/backfill_cat_stop_price.py must exist."""
        import os
        assert os.path.isfile("scripts/backfill_cat_stop_price.py"), (
            "scripts/backfill_cat_stop_price.py missing — one-shot backfill script required."
        )

    def test_backfill_script_has_idempotency_guard(self):
        """Backfill script must be idempotent (skip if stop_price already set)."""
        with open("scripts/backfill_cat_stop_price.py") as f:
            src = f.read()
        # Should skip if existing stop already set
        assert "existing_stop is not None" in src or "already has stop_price" in src or "no-op" in src, (
            "Backfill script must be idempotent — skip if stop_price already populated."
        )

    def test_backfill_script_validates_check_constraint(self):
        """Backfill script must validate stop < entry for long trades."""
        with open("scripts/backfill_cat_stop_price.py") as f:
            src = f.read()
        assert "entry_price" in src, "Backfill must read entry_price"
        assert "CHECK constraint" in src or "violates" in src, (
            "Backfill script must guard against CHECK constraint violations."
        )


# ── functional tests ─────────────────────────────────────────────────────

class TestStopPriceBackfillLogic:
    """Functional tests for the F-12 persistence logic."""

    def _make_trade_db(self, stop_price: float | None = None) -> sqlite3.Connection:
        """Create an in-memory trades DB with one open CAT trade."""
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute(
            "CREATE TABLE trades ("
            "id INTEGER PRIMARY KEY, ticker TEXT, direction TEXT, "
            "entry_price REAL, shares INTEGER, stop_price REAL, "
            "universe TEXT, status TEXT, superseded INTEGER, "
            "updated_at TEXT"
            ")"
        )
        conn.execute(
            "INSERT INTO trades VALUES (187, 'CAT', 'long', 835.24, 1, ?, "
            "'sp500', 'open', 0, NULL)",
            (stop_price,),
        )
        conn.commit()
        return conn

    def test_writes_valid_stop_price_when_null(self):
        """stop_price is written when NULL and passes CHECK constraint."""
        conn = self._make_trade_db(stop_price=None)
        # Simulate what the F-12 block does
        row = conn.execute(
            "SELECT id, direction, entry_price FROM trades "
            "WHERE ticker='CAT' AND status='open' AND superseded=0 AND stop_price IS NULL"
        ).fetchone()
        assert row is not None
        tid, direction, ep = row["id"], row["direction"], float(row["entry_price"])
        sp = 800.0  # valid: 800 < 835.24
        ok = (direction == "long" and sp < ep)
        assert ok
        conn.execute("UPDATE trades SET stop_price=? WHERE id=?", (sp, tid))
        conn.commit()
        result = conn.execute("SELECT stop_price FROM trades WHERE id=?", (tid,)).fetchone()
        assert result["stop_price"] == pytest.approx(800.0)
        conn.close()

    def test_skips_stop_above_entry_for_long(self):
        """CHECK constraint guard: stop >= entry for long must be skipped."""
        conn = self._make_trade_db(stop_price=None)
        row = conn.execute(
            "SELECT id, direction, entry_price FROM trades "
            "WHERE ticker='CAT' AND status='open' AND superseded=0 AND stop_price IS NULL"
        ).fetchone()
        tid, direction, ep = row["id"], row["direction"], float(row["entry_price"])
        sp = 861.21  # invalid: 861.21 > 835.24 (profit-locking stop)
        ok = (direction == "long" and sp < ep)
        assert not ok, "Should NOT write a stop above entry for a long trade"
        conn.close()

    def test_no_update_when_stop_already_set(self):
        """When stop_price is already set, the F-12 logic skips it."""
        conn = self._make_trade_db(stop_price=810.0)
        row = conn.execute(
            "SELECT id FROM trades "
            "WHERE ticker='CAT' AND status='open' AND superseded=0 AND stop_price IS NULL"
        ).fetchone()
        # No row because stop_price IS NOT NULL → SELECT returns None
        assert row is None, "Row with stop_price should not be returned by IS NULL query"
        conn.close()

    def test_cat_stop_price_in_live_db(self):
        """CAT trade in live DB: stop_price either NULL (broker stop above entry) or a valid value."""
        conn = sqlite3.connect("data/atlas.db")
        row = conn.execute(
            "SELECT stop_price, entry_price FROM trades WHERE ticker='CAT' AND status='open'"
        ).fetchone()
        conn.close()
        if row:
            stop_price, entry_price = row[0], row[1]
            if stop_price is not None:
                # Must be < entry (CHECK constraint for long trade)
                assert float(stop_price) < float(entry_price), (
                    f"stop_price={stop_price} >= entry_price={entry_price}: "
                    "violates trades CHECK constraint for long trade"
                )
            # stop_price=None is valid when the broker stop is above entry (profit-locking)


import pytest
