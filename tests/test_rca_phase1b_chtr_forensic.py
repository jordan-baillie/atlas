"""
tests/test_rca_phase1b_chtr_forensic.py
────────────────────────────────────────
Unit tests for CHTR forensic (RCA Phase 1B):
  1. classify_fills() correctly separates buy/sell fills from a mocked
     Alpaca FILL activities response
  2. compute_pnl() returns correct values given fill prices
  3. Migration idempotency — applying twice is a no-op
  4. Migration DRY-RUN makes no DB changes
  5. APPLY marks phantom row superseded and canonical row verified
"""
from __future__ import annotations

import sqlite3
from unittest.mock import MagicMock, patch
from pathlib import Path
import sys
import importlib

import pytest

# ── path bootstrap ──────────────────────────────────────────────────────────
ATLAS_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ATLAS_ROOT))


# ── helpers ──────────────────────────────────────────────────────────────────

def _mock_chtr_fills_one_roundtrip() -> list[dict]:
    """Mocked Alpaca FILL response for CHTR — ONE round-trip (Case A)."""
    return [
        {
            "id": "20260421093001718::eab2445b",
            "activity_type": "FILL",
            "transaction_time": "2026-04-21T13:30:01.718Z",
            "type": "fill",
            "price": "243.93",
            "qty": "1",
            "side": "buy",
            "symbol": "CHTR",
            "leaves_qty": "0",
            "order_id": "7ee0a69c-7989-4472-8048-8992c0c92203",
            "cum_qty": "1",
            "order_status": "filled",
        },
        {
            "id": "20260423132819011::23699a46",
            "activity_type": "FILL",
            "transaction_time": "2026-04-23T17:28:19.011Z",
            "type": "fill",
            "price": "241.8368",
            "qty": "1",
            "side": "sell",
            "symbol": "CHTR",
            "leaves_qty": "0",
            "order_id": "50dc1ec0-550c-4e8e-83b7-84cf66a4ae3a",
            "cum_qty": "1",
            "order_status": "filled",
        },
    ]


def _mock_chtr_fills_two_roundtrips() -> list[dict]:
    """Mocked FILL response for CHTR — TWO round-trips (Case B scenario)."""
    return [
        {"price": "240.00", "qty": "1", "side": "buy",  "symbol": "CHTR",
         "transaction_time": "2026-04-21T13:30:00Z", "order_id": "aaa"},
        {"price": "242.00", "qty": "1", "side": "sell", "symbol": "CHTR",
         "transaction_time": "2026-04-22T15:00:00Z", "order_id": "bbb"},
        {"price": "241.00", "qty": "1", "side": "buy",  "symbol": "CHTR",
         "transaction_time": "2026-04-24T13:30:00Z", "order_id": "ccc"},
        {"price": "239.00", "qty": "1", "side": "sell", "symbol": "CHTR",
         "transaction_time": "2026-04-25T15:00:00Z", "order_id": "ddd"},
    ]


# ── import helpers from forensic script ─────────────────────────────────────

def _load_forensic_module():
    spec = importlib.util.spec_from_file_location(
        "forensic_chtr_fills",
        ATLAS_ROOT / "scripts" / "forensic_chtr_fills.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ── Tests: classify_fills ─────────────────────────────────────────────────────

class TestClassifyFills:
    """Tests for forensic_chtr_fills.classify_fills()."""

    def setup_method(self):
        self.mod = _load_forensic_module()

    def test_one_roundtrip_buy_sell(self):
        fills = _mock_chtr_fills_one_roundtrip()
        result = self.mod.classify_fills(fills)
        assert len(result["buys"]) == 1
        assert len(result["sells"]) == 1

    def test_one_roundtrip_counts(self):
        fills = _mock_chtr_fills_one_roundtrip()
        result = self.mod.classify_fills(fills)
        assert result["total_bought_qty"] == 1.0
        assert result["total_sold_qty"] == 1.0
        assert result["round_trips"] == 1

    def test_one_roundtrip_buy_price(self):
        fills = _mock_chtr_fills_one_roundtrip()
        result = self.mod.classify_fills(fills)
        assert abs(result["buys"][0]["price"] - 243.93) < 1e-6

    def test_one_roundtrip_sell_price(self):
        fills = _mock_chtr_fills_one_roundtrip()
        result = self.mod.classify_fills(fills)
        assert abs(result["sells"][0]["price"] - 241.8368) < 1e-6

    def test_two_roundtrips_count(self):
        fills = _mock_chtr_fills_two_roundtrips()
        result = self.mod.classify_fills(fills)
        assert result["round_trips"] == 2
        assert len(result["buys"]) == 2
        assert len(result["sells"]) == 2

    def test_empty_fills_returns_zero_roundtrips(self):
        result = self.mod.classify_fills([])
        assert result["round_trips"] == 0
        assert result["buys"] == []
        assert result["sells"] == []

    def test_only_buys_no_roundtrip(self):
        fills = [{"price": "100.00", "qty": "1", "side": "buy",
                  "symbol": "CHTR", "transaction_time": "t1", "order_id": "x"}]
        result = self.mod.classify_fills(fills)
        assert result["round_trips"] == 0
        assert len(result["buys"]) == 1
        assert len(result["sells"]) == 0

    def test_buy_timestamps_preserved(self):
        fills = _mock_chtr_fills_one_roundtrip()
        result = self.mod.classify_fills(fills)
        assert "2026-04-21" in result["buys"][0]["transaction_time"]

    def test_sell_timestamps_preserved(self):
        fills = _mock_chtr_fills_one_roundtrip()
        result = self.mod.classify_fills(fills)
        assert "2026-04-23" in result["sells"][0]["transaction_time"]

    def test_order_ids_preserved(self):
        fills = _mock_chtr_fills_one_roundtrip()
        result = self.mod.classify_fills(fills)
        assert "7ee0a69c" in result["buys"][0]["order_id"]
        assert "50dc1ec0" in result["sells"][0]["order_id"]


# ── Tests: compute_pnl ────────────────────────────────────────────────────────

class TestComputePnl:
    """Tests for forensic_chtr_fills.compute_pnl()."""

    def setup_method(self):
        self.mod = _load_forensic_module()

    def test_chtr_actual_pnl(self):
        """Confirmed actual: entry=243.93 exit=241.8368 qty=1 → pnl=-2.0932."""
        pnl, pnl_pct = self.mod.compute_pnl(243.93, 241.8368, 1)
        assert abs(pnl - (-2.0932)) < 1e-3

    def test_chtr_actual_pnl_pct(self):
        pnl, pnl_pct = self.mod.compute_pnl(243.93, 241.8368, 1)
        # ≈ -0.858%
        assert abs(pnl_pct - (-0.8581)) < 0.01

    def test_profit_case(self):
        pnl, pnl_pct = self.mod.compute_pnl(100.0, 110.0, 2)
        assert abs(pnl - 20.0) < 1e-6
        assert abs(pnl_pct - 10.0) < 1e-6

    def test_multi_share_scaling(self):
        pnl, _ = self.mod.compute_pnl(50.0, 45.0, 5)
        assert abs(pnl - (-25.0)) < 1e-6

    def test_breakeven(self):
        pnl, pnl_pct = self.mod.compute_pnl(100.0, 100.0, 1)
        assert pnl == 0.0
        assert pnl_pct == 0.0


# ── Tests: migration script ───────────────────────────────────────────────────

def _build_test_db(tmp_path: Path) -> str:
    """Create a minimal trades table with rows 172 and 184."""
    db_path = str(tmp_path / "test_atlas.db")
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE trades (
            id              INTEGER PRIMARY KEY,
            ticker          TEXT NOT NULL,
            strategy        TEXT NOT NULL,
            universe        TEXT,
            entry_date      TEXT NOT NULL,
            exit_date       TEXT,
            entry_price     REAL NOT NULL,
            exit_price      REAL,
            shares          INTEGER NOT NULL,
            pnl             REAL,
            pnl_pct         REAL,
            exit_reason     TEXT,
            status          TEXT DEFAULT 'closed',
            superseded      INTEGER NOT NULL DEFAULT 0,
            created_at      TEXT DEFAULT (datetime('now')),
            updated_at      TEXT DEFAULT (datetime('now'))
        )
    """)
    conn.execute("""
        INSERT INTO trades (id, ticker, strategy, entry_date, exit_date,
            entry_price, exit_price, shares, pnl, pnl_pct, exit_reason, superseded)
        VALUES
            (172, 'CHTR', 'momentum_breakout', '2026-04-21T13:30:01', '2026-04-23T17:28:19',
             243.93, 241.8368, 1, -2.0932, -0.8581, 'reconcile_fill', 0),
            (184, 'CHTR', 'reconciled', '2026-04-24T10:57:38', '2026-04-25T08:00:49',
             243.93, 241.8368, 1, -2.0932, -0.8581, 'reconcile_fill', 0)
    """)
    conn.commit()
    conn.close()
    return db_path


def _load_migration_module():
    spec = importlib.util.spec_from_file_location(
        "migration_chtr_rca_1b",
        ATLAS_ROOT / "scripts" / "migrations" / "2026-04-29-chtr-forensic-correction-rca-1b.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestMigrationDryRun:
    """DRY-RUN makes no DB changes."""

    def test_dry_run_no_db_changes(self, tmp_path, monkeypatch):
        db_path = _build_test_db(tmp_path)
        mod = _load_migration_module()

        # Redirect get_db to use our test DB
        import contextlib
        @contextlib.contextmanager
        def mock_get_db():
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            try:
                yield conn
            finally:
                conn.close()

        monkeypatch.setattr(mod, "get_db", mock_get_db)
        mod.apply_migration(dry_run=True)

        # Verify nothing changed
        conn = sqlite3.connect(db_path)
        row172 = conn.execute("SELECT superseded, exit_reason FROM trades WHERE id=172").fetchone()
        row184 = conn.execute("SELECT superseded, exit_reason FROM trades WHERE id=184").fetchone()
        conn.close()

        assert row172[0] == 0, "DRY-RUN should not modify row 172"
        assert row184[0] == 0, "DRY-RUN should not modify row 184"
        assert row172[1] == "reconcile_fill", "DRY-RUN should not change exit_reason"
        assert row184[1] == "reconcile_fill", "DRY-RUN should not change exit_reason"


class TestMigrationApply:
    """APPLY makes correct changes."""

    def _apply(self, db_path: str, mod):
        import contextlib
        @contextlib.contextmanager
        def mock_get_db():
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            try:
                yield conn
            finally:
                conn.close()

        import db.atlas_db as atlas_db_mod
        import unittest.mock
        with unittest.mock.patch.object(mod, "get_db", mock_get_db):
            mod.apply_migration(dry_run=False)

        return sqlite3.connect(db_path)

    def test_phantom_row_superseded(self, tmp_path):
        db_path = _build_test_db(tmp_path)
        mod = _load_migration_module()
        conn = self._apply(db_path, mod)
        row = conn.execute("SELECT superseded FROM trades WHERE id=184").fetchone()
        conn.close()
        assert row[0] == 1, "Row 184 must be marked superseded=1"

    def test_phantom_row_exit_reason_updated(self, tmp_path):
        db_path = _build_test_db(tmp_path)
        mod = _load_migration_module()
        conn = self._apply(db_path, mod)
        row = conn.execute("SELECT exit_reason FROM trades WHERE id=184").fetchone()
        conn.close()
        assert "corrected_phase1b" in (row[0] or "")

    def test_canonical_row_not_superseded(self, tmp_path):
        db_path = _build_test_db(tmp_path)
        mod = _load_migration_module()
        conn = self._apply(db_path, mod)
        row = conn.execute("SELECT superseded FROM trades WHERE id=172").fetchone()
        conn.close()
        assert row[0] == 0, "Row 172 (canonical) must remain superseded=0"

    def test_canonical_exit_reason_verified(self, tmp_path):
        db_path = _build_test_db(tmp_path)
        mod = _load_migration_module()
        conn = self._apply(db_path, mod)
        row = conn.execute("SELECT exit_reason FROM trades WHERE id=172").fetchone()
        conn.close()
        assert "verified_phase1b" in (row[0] or "")

    def test_canonical_prices_unchanged(self, tmp_path):
        """Row 172 prices must not be modified — Alpaca confirms they were correct."""
        db_path = _build_test_db(tmp_path)
        mod = _load_migration_module()
        conn = self._apply(db_path, mod)
        row = conn.execute("SELECT entry_price, exit_price, pnl FROM trades WHERE id=172").fetchone()
        conn.close()
        assert abs(row[0] - 243.93) < 1e-6
        assert abs(row[1] - 241.8368) < 1e-6
        assert abs(row[2] - (-2.0932)) < 1e-3

    def test_dollar_correction_zero(self, tmp_path):
        """Confirm no PnL change — prices were already correct in canonical row."""
        db_path = _build_test_db(tmp_path)
        mod = _load_migration_module()
        conn = self._apply(db_path, mod)
        before = -2.0932
        row = conn.execute("SELECT pnl FROM trades WHERE id=172").fetchone()
        conn.close()
        assert abs(row[0] - before) < 1e-3, f"No PnL correction expected, got {row[0]}"


class TestMigrationIdempotency:
    """Re-running the migration is a no-op."""

    def _apply(self, db_path: str, mod):
        import contextlib
        @contextlib.contextmanager
        def mock_get_db():
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            try:
                yield conn
            finally:
                conn.close()

        import unittest.mock
        with unittest.mock.patch.object(mod, "get_db", mock_get_db):
            mod.apply_migration(dry_run=False)

    def test_double_apply_is_noop(self, tmp_path):
        db_path = _build_test_db(tmp_path)
        mod = _load_migration_module()
        self._apply(db_path, mod)
        self._apply(db_path, mod)  # second run should be no-op

        conn = sqlite3.connect(db_path)
        row = conn.execute("SELECT superseded, exit_reason FROM trades WHERE id=184").fetchone()
        conn.close()
        # exit_reason should contain exactly one 'corrected_phase1b', not duplicated
        assert row[0] == 1
        assert (row[1] or "").count("corrected_phase1b") == 1, \
            f"Suffix should appear once, got: {row[1]}"
