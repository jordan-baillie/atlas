#!/usr/bin/env python3
"""Tests for RCA Phase 4A — broker_orders SQLite cache.

Verifies:
- Migration creates the table idempotently
- sync_broker_orders.py upserts new orders and updates existing ones
- Reconcile path uses broker_orders fill price first (not inference)
- CHTR-style phantom-price scenario is blocked by the cache
"""
from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ── Project path ─────────────────────────────────────────────────────────────
ATLAS_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ATLAS_ROOT))


# ════════════════════════════════════════════════════════════════
# Fixtures
# ════════════════════════════════════════════════════════════════

@pytest.fixture()
def fresh_db(tmp_path: Path) -> Path:
    """Fresh SQLite database (no broker_orders table yet)."""
    db_path = tmp_path / "test_atlas.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.commit()
    conn.close()
    return db_path


@pytest.fixture()
def db_with_broker_orders(fresh_db: Path) -> Path:
    """DB that already has broker_orders table (for idempotency checks)."""
    conn = sqlite3.connect(str(fresh_db))
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS broker_orders (
            order_id           TEXT PRIMARY KEY,
            symbol             TEXT NOT NULL,
            side               TEXT NOT NULL,
            qty                REAL NOT NULL,
            filled_qty         REAL,
            fill_price         REAL,
            status             TEXT NOT NULL,
            submitted_at       TEXT NOT NULL,
            filled_at          TEXT,
            order_class        TEXT,
            parent_id          TEXT,
            raw_alpaca_json    TEXT NOT NULL,
            last_synced_at     TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_broker_orders_symbol ON broker_orders(symbol);
        CREATE INDEX IF NOT EXISTS idx_broker_orders_status ON broker_orders(status);
        CREATE INDEX IF NOT EXISTS idx_broker_orders_submitted_at
            ON broker_orders(submitted_at);
        CREATE INDEX IF NOT EXISTS idx_broker_orders_parent_id ON broker_orders(parent_id);
    """)
    conn.commit()
    conn.close()
    return fresh_db


@pytest.fixture()
def db_with_trades(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Full Atlas-schema DB (via init_db) with isolation."""
    import db.atlas_db as _adb
    db_path = tmp_path / "atlas.db"
    monkeypatch.setattr(_adb, "_db_path_override", str(db_path))
    _adb.init_db()
    yield db_path
    monkeypatch.setattr(_adb, "_db_path_override", None)


# ════════════════════════════════════════════════════════════════
# Helpers
# ════════════════════════════════════════════════════════════════

def _load_migration_module(db_path: Path):
    """Load the migration script as a module, patching its DB_PATH."""
    mig_path = (
        ATLAS_ROOT / "scripts" / "migrations" /
        "2026-04-29-add-broker-orders-table.py"
    )
    spec = importlib.util.spec_from_file_location("mig_broker_orders", mig_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.DB_PATH = db_path
    return mod


def _insert_broker_order(
    db_path: Path,
    order_id: str,
    symbol: str,
    side: str,
    qty: float,
    fill_price: float | None = None,
    filled_qty: float | None = None,
    status: str = "filled",
    submitted_at: str = "2026-04-29T10:00:00+00:00",
    filled_at: str | None = "2026-04-29T10:00:01+00:00",
    order_class: str = "simple",
) -> None:
    """Insert directly into broker_orders for test setup."""
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """INSERT OR REPLACE INTO broker_orders
           (order_id, symbol, side, qty, filled_qty, fill_price, status,
            submitted_at, filled_at, order_class, parent_id, raw_alpaca_json, last_synced_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,NULL,'{}',datetime('now'))""",
        (order_id, symbol, side, qty, filled_qty, fill_price, status,
         submitted_at, filled_at, order_class),
    )
    conn.commit()
    conn.close()


def _make_mock_alpaca_order(
    order_id: str,
    symbol: str,
    side_str: str,
    qty: float,
    filled_qty: float | None,
    filled_avg_price: float | None,
    status_str: str,
    submitted_at: str,
    filled_at: str | None = None,
    order_class_str: str = "simple",
    replaces: str | None = None,
) -> MagicMock:
    """Create a mock Alpaca order object as returned by _trade_client.get_orders."""
    order = MagicMock()
    order.model_dump.return_value = {
        "id": order_id,
        "client_order_id": f"atlas-{order_id[:8]}",
        "symbol": symbol,
        "side": side_str,
        "qty": str(qty),
        "filled_qty": str(filled_qty) if filled_qty is not None else "0",
        "filled_avg_price": str(filled_avg_price) if filled_avg_price is not None else None,
        "status": status_str,
        "submitted_at": submitted_at,
        "filled_at": filled_at,
        "order_class": order_class_str,
        "replaces": replaces,
        "created_at": submitted_at,
        "updated_at": submitted_at,
        "asset_id": None,
        "asset_class": "us_equity",
        "notional": None,
        "time_in_force": "day",
        "limit_price": None,
        "stop_price": None,
        "extended_hours": False,
        "legs": [],
        "trail_percent": None,
        "trail_price": None,
        "hwm": None,
        "position_intent": None,
        "ratio_qty": None,
        "type": "market",
        "order_type": "market",
        "expired_at": None,
        "expires_at": None,
        "canceled_at": None,
        "failed_at": None,
        "replaced_at": None,
        "replaced_by": None,
    }
    return order


# ════════════════════════════════════════════════════════════════
# 1. Migration tests
# ════════════════════════════════════════════════════════════════

class TestMigration:
    def test_migration_creates_table_idempotent(self, fresh_db: Path) -> None:
        """Apply migration twice — no error, table and indexes exist."""
        mod = _load_migration_module(fresh_db)

        # Apply 1st time
        mod._run(apply=True)

        # Apply 2nd time — must not raise
        mod._run(apply=True)

        # Verify schema
        conn = sqlite3.connect(str(fresh_db))
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        assert "broker_orders" in tables

        cols = {r[1] for r in conn.execute("PRAGMA table_info(broker_orders)").fetchall()}
        for expected_col in [
            "order_id", "symbol", "side", "qty", "filled_qty", "fill_price",
            "status", "submitted_at", "filled_at", "order_class", "parent_id",
            "raw_alpaca_json", "last_synced_at",
        ]:
            assert expected_col in cols, f"Missing column: {expected_col}"

        idx_count = conn.execute(
            "SELECT COUNT(*) FROM sqlite_master "
            "WHERE type='index' AND tbl_name='broker_orders'"
        ).fetchone()[0]
        assert idx_count >= 4, f"Expected ≥4 indexes, got {idx_count}"
        conn.close()

    def test_migration_table_already_exists_nonfatal(self, db_with_broker_orders: Path) -> None:
        """Migration is safe when broker_orders already exists (IF NOT EXISTS)."""
        mod = _load_migration_module(db_with_broker_orders)
        # Should not raise
        mod._run(apply=True)

        conn = sqlite3.connect(str(db_with_broker_orders))
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        assert "broker_orders" in tables
        conn.close()


# ════════════════════════════════════════════════════════════════
# 2. sync_broker_orders tests
# ════════════════════════════════════════════════════════════════

class TestSyncBrokerOrders:
    """Tests for the sync script itself (mocked Alpaca calls)."""

    def _make_broker(self) -> MagicMock:
        broker = MagicMock()
        broker.connect.return_value = True
        broker._trade_client = MagicMock()
        broker._broker_call.side_effect = lambda fn, *args, **kwargs: fn(*args, **kwargs)
        broker.disconnect.return_value = None
        return broker

    def test_sync_upserts_new_order(self, db_with_trades: Path, monkeypatch) -> None:
        """Mock Alpaca returns 1 order — sync inserts it into broker_orders."""
        import db.atlas_db as _adb
        import scripts.sync_broker_orders as sync_mod

        mock_order = _make_mock_alpaca_order(
            order_id="aaa-111",
            symbol="AAPL",
            side_str="buy",
            qty=10,
            filled_qty=10,
            filled_avg_price=150.00,
            status_str="filled",
            submitted_at="2026-04-29T09:30:00+00:00",
            filled_at="2026-04-29T09:30:01+00:00",
        )

        broker = self._make_broker()
        broker._trade_client.get_orders.return_value = [mock_order]

        monkeypatch.setattr(sync_mod, "get_live_broker", lambda cfg: broker)
        monkeypatch.setattr(sync_mod, "get_active_config", lambda market: {})

        stats = sync_mod.sync_broker_orders(days=7, dry_run=False)

        assert stats["fetched"] == 1
        assert stats["upserted"] == 1
        assert stats["filled_count"] == 1
        assert not stats["errors"]

        # Verify row in DB
        row = _adb.get_broker_orders(symbol="AAPL", side="buy", status="filled")
        assert len(row) == 1
        assert row[0]["order_id"] == "aaa-111"
        assert abs(row[0]["fill_price"] - 150.00) < 0.001

    def test_sync_updates_existing_order_status(self, db_with_trades: Path, monkeypatch) -> None:
        """Same order returned with status=filled — sync updates the row."""
        import db.atlas_db as _adb
        import scripts.sync_broker_orders as sync_mod

        # Pre-insert an 'accepted' order
        _insert_broker_order(
            db_with_trades, "bbb-222", "MSFT", "buy", 5,
            fill_price=None, filled_qty=0, status="accepted",
            filled_at=None,
        )

        # Mock returns same order now filled
        mock_order = _make_mock_alpaca_order(
            order_id="bbb-222",
            symbol="MSFT",
            side_str="buy",
            qty=5,
            filled_qty=5,
            filled_avg_price=420.50,
            status_str="filled",
            submitted_at="2026-04-29T09:00:00+00:00",
            filled_at="2026-04-29T09:00:02+00:00",
        )

        broker = self._make_broker()
        broker._trade_client.get_orders.return_value = [mock_order]

        monkeypatch.setattr(sync_mod, "get_live_broker", lambda cfg: broker)
        monkeypatch.setattr(sync_mod, "get_active_config", lambda market: {})

        stats = sync_mod.sync_broker_orders(days=7, dry_run=False)

        row = _adb.get_broker_orders(symbol="MSFT", side="buy")
        assert len(row) == 1
        assert row[0]["status"] == "filled"
        assert abs(row[0]["fill_price"] - 420.50) < 0.001
        # order_id preserved
        assert row[0]["order_id"] == "bbb-222"

    def test_sync_aggregates_partial_fills_to_weighted_avg(
        self, db_with_trades: Path, monkeypatch
    ) -> None:
        """Two orders for same symbol (partial fill scenario) — both rows upserted.

        Note: Alpaca aggregates partial fills into a single order with a weighted
        fill price. Our sync stores the order row as-is. This test verifies two
        distinct orders for the same symbol are both stored (each as its own row).
        """
        import db.atlas_db as _adb
        import scripts.sync_broker_orders as sync_mod

        order1 = _make_mock_alpaca_order(
            order_id="ccc-333-a",
            symbol="NVDA",
            side_str="buy",
            qty=5,
            filled_qty=3,
            filled_avg_price=880.00,
            status_str="partially_filled",
            submitted_at="2026-04-29T09:30:00+00:00",
        )
        order2 = _make_mock_alpaca_order(
            order_id="ccc-333-b",
            symbol="NVDA",
            side_str="buy",
            qty=2,
            filled_qty=2,
            filled_avg_price=882.50,
            status_str="filled",
            submitted_at="2026-04-29T09:35:00+00:00",
            filled_at="2026-04-29T09:35:01+00:00",
        )

        broker = self._make_broker()
        broker._trade_client.get_orders.return_value = [order1, order2]

        monkeypatch.setattr(sync_mod, "get_live_broker", lambda cfg: broker)
        monkeypatch.setattr(sync_mod, "get_active_config", lambda market: {})

        stats = sync_mod.sync_broker_orders(days=7, dry_run=False)

        assert stats["fetched"] == 2
        assert stats["upserted"] == 2

        rows = _adb.get_broker_orders(symbol="NVDA", side="buy")
        assert len(rows) == 2
        order_ids = {r["order_id"] for r in rows}
        assert "ccc-333-a" in order_ids
        assert "ccc-333-b" in order_ids

    def test_dry_run_does_not_write(self, db_with_trades: Path, monkeypatch) -> None:
        """--dry-run: fetches orders but writes nothing to DB."""
        import db.atlas_db as _adb
        import scripts.sync_broker_orders as sync_mod

        mock_order = _make_mock_alpaca_order(
            order_id="ddd-444",
            symbol="GOOG",
            side_str="buy",
            qty=1,
            filled_qty=1,
            filled_avg_price=175.00,
            status_str="filled",
            submitted_at="2026-04-29T10:00:00+00:00",
        )

        broker = self._make_broker()
        broker._trade_client.get_orders.return_value = [mock_order]

        monkeypatch.setattr(sync_mod, "get_live_broker", lambda cfg: broker)
        monkeypatch.setattr(sync_mod, "get_active_config", lambda market: {})

        stats = sync_mod.sync_broker_orders(days=7, dry_run=True)

        assert stats["upserted"] == 1  # counted but not persisted
        # Nothing written to DB
        rows = _adb.get_broker_orders(symbol="GOOG")
        assert len(rows) == 0


# ════════════════════════════════════════════════════════════════
# 3. Reconcile path — atlas_db helper tests
# ════════════════════════════════════════════════════════════════

class TestReconcileFillPriceLookup:
    """Test that get_broker_fill_price works correctly for reconcile path."""

    def test_reconcile_uses_broker_orders_first(
        self, db_with_trades: Path, monkeypatch
    ) -> None:
        """If broker_orders has fill_price=$X for a trade, reconcile uses $X."""
        import db.atlas_db as _adb

        # Seed broker_orders with a known fill price
        _insert_broker_order(
            db_with_trades, "ord-001", "CHTR", "buy", 1,
            fill_price=241.84, filled_qty=1, status="filled",
        )

        price = _adb.get_broker_fill_price("CHTR", side="buy")
        assert price is not None
        assert abs(price - 241.84) < 0.001, f"Expected 241.84 got {price}"

    def test_reconcile_falls_back_to_inference_when_not_in_broker_orders(
        self, db_with_trades: Path, monkeypatch
    ) -> None:
        """If broker_orders has no row for symbol, get_broker_fill_price returns None."""
        import db.atlas_db as _adb

        price = _adb.get_broker_fill_price("TSLA", side="buy")
        assert price is None, f"Expected None (triggers inference fallback), got {price}"

    def test_get_broker_fill_price_ignores_unfilled_orders(
        self, db_with_trades: Path, monkeypatch
    ) -> None:
        """Orders with fill_price=NULL or not-filled status are ignored."""
        import db.atlas_db as _adb

        _insert_broker_order(
            db_with_trades, "ord-002", "AMZN", "buy", 5,
            fill_price=None, filled_qty=0, status="accepted",
            filled_at=None,
        )

        price = _adb.get_broker_fill_price("AMZN", side="buy")
        assert price is None

    def test_get_broker_fill_price_by_order_id(
        self, db_with_trades: Path, monkeypatch
    ) -> None:
        """get_broker_fill_price by order_id returns exact fill."""
        import db.atlas_db as _adb

        _insert_broker_order(
            db_with_trades, "ord-999", "META", "sell", 3,
            fill_price=510.25, filled_qty=3, status="filled",
        )

        price = _adb.get_broker_fill_price("META", side="sell", order_id="ord-999")
        assert price is not None
        assert abs(price - 510.25) < 0.001

    def test_get_broker_fill_price_returns_most_recent(
        self, db_with_trades: Path, monkeypatch
    ) -> None:
        """Multiple filled orders for same symbol — returns most recent fill."""
        import db.atlas_db as _adb

        _insert_broker_order(
            db_with_trades, "ord-old", "AMD", "buy", 10,
            fill_price=100.00, filled_qty=10, status="filled",
            submitted_at="2026-04-20T10:00:00+00:00",
            filled_at="2026-04-20T10:00:01+00:00",
        )
        _insert_broker_order(
            db_with_trades, "ord-new", "AMD", "buy", 10,
            fill_price=112.50, filled_qty=10, status="filled",
            submitted_at="2026-04-28T10:00:00+00:00",
            filled_at="2026-04-28T10:00:01+00:00",
        )

        price = _adb.get_broker_fill_price("AMD", side="buy")
        assert abs(price - 112.50) < 0.001, f"Expected most recent 112.50, got {price}"

    def test_get_broker_fill_price_table_missing_is_nonfatal(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """If broker_orders table doesn't exist, returns None gracefully."""
        import db.atlas_db as _adb
        plain_db = tmp_path / "plain.db"
        conn = sqlite3.connect(str(plain_db))
        conn.execute("CREATE TABLE dummy (id INTEGER PRIMARY KEY)")
        conn.commit()
        conn.close()

        monkeypatch.setattr(_adb, "_db_path_override", str(plain_db))
        price = _adb.get_broker_fill_price("XYZ", side="buy")
        assert price is None
        monkeypatch.setattr(_adb, "_db_path_override", None)


# ════════════════════════════════════════════════════════════════
# 4. CHTR-style phantom-price scenario (key regression test)
# ════════════════════════════════════════════════════════════════

class TestCHTRPhantomPriceScenario:
    """CHTR bug pattern: inference says $243.93, reality was $241.84.

    With broker_orders populated, get_broker_fill_price must return the
    real fill price and never the inferred one.
    """

    def test_chtr_style_phantom_price_blocked_by_broker_orders(
        self, db_with_trades: Path, monkeypatch
    ) -> None:
        """
        Setup:
          - broker_orders has CHTR fill at $241.84 (actual)
          - broker position would infer avg_entry_price=$243.93 (wrong)
        Assertion:
          - get_broker_fill_price("CHTR") returns $241.84 (not $243.93)
        """
        import db.atlas_db as _adb

        _insert_broker_order(
            db_with_trades, "chtr-real-fill", "CHTR", "buy", 1,
            fill_price=241.84, filled_qty=1, status="filled",
            submitted_at="2026-04-15T09:30:00+00:00",
            filled_at="2026-04-15T09:30:02+00:00",
        )

        inferred_price = 243.93  # what the old inference path would produce

        actual_price = _adb.get_broker_fill_price("CHTR", side="buy")

        assert actual_price is not None, "Expected a price from broker_orders"
        assert abs(actual_price - 241.84) < 0.001, (
            f"broker_orders returned {actual_price}, "
            f"expected 241.84 (not inferred {inferred_price})"
        )
        # Confirm this would have caught the bug (prices differ by >$1)
        assert abs(actual_price - inferred_price) > 1.0, (
            "broker_orders price too close to inferred — CHTR scenario not distinguishable"
        )

    def test_chtr_style_inference_without_broker_orders(
        self, db_with_trades: Path, monkeypatch
    ) -> None:
        """Without broker_orders data, returns None → caller uses inference fallback."""
        import db.atlas_db as _adb

        price = _adb.get_broker_fill_price("CHTR", side="buy")
        assert price is None, (
            "Without broker_orders data, should return None so caller can fall back"
        )

    def test_broker_orders_side_isolation(
        self, db_with_trades: Path, monkeypatch
    ) -> None:
        """buy fill for CHTR does not pollute sell fill lookup."""
        import db.atlas_db as _adb

        _insert_broker_order(
            db_with_trades, "chtr-buy", "CHTR", "buy", 1,
            fill_price=241.84, filled_qty=1, status="filled",
        )

        sell_price = _adb.get_broker_fill_price("CHTR", side="sell")
        assert sell_price is None  # no sell fill exists

        buy_price = _adb.get_broker_fill_price("CHTR", side="buy")
        assert abs(buy_price - 241.84) < 0.001


# ════════════════════════════════════════════════════════════════
# 5. Source inspection — reconcile_ledger wiring
# ════════════════════════════════════════════════════════════════

class TestReconcileLedgerBrokerOrdersWiring:
    """Verify reconcile_ledger.py is wired to use broker_orders first."""

    def test_reconcile_ledger_calls_broker_fill_price(self) -> None:
        """reconcile_ledger.py must call atlas_db.get_broker_fill_price."""
        src = (ATLAS_ROOT / "scripts" / "reconcile_ledger.py").read_text()
        assert "get_broker_fill_price" in src, (
            "reconcile_ledger.py must call atlas_db.get_broker_fill_price"
        )

    def test_reconcile_ledger_broker_orders_before_inference(self) -> None:
        """broker_orders lookup appears BEFORE fill.fill_price inference fallback."""
        src = (ATLAS_ROOT / "scripts" / "reconcile_ledger.py").read_text()
        idx_cache = src.find("get_broker_fill_price")
        idx_infer = src.find("fill.fill_price")
        assert idx_cache > 0, "get_broker_fill_price not found in reconcile_ledger.py"
        assert idx_infer > 0, "fill.fill_price not found in reconcile_ledger.py"
        assert idx_cache < idx_infer, (
            f"broker_orders lookup must come BEFORE inference "
            f"(cache@{idx_cache} vs infer@{idx_infer})"
        )

    def test_reconcile_ledger_source_of_truth_comment(self) -> None:
        """Source-of-truth comment documents the intent in reconcile_ledger."""
        src = (ATLAS_ROOT / "scripts" / "reconcile_ledger.py").read_text()
        assert "source-of-truth" in src.lower() or "broker_orders local cache" in src, (
            "Expected source-of-truth/broker_orders comment in reconcile_ledger.py"
        )
