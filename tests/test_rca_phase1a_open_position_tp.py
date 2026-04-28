"""
Regression guard for RCA Phase 1A: every open position with a stop order
MUST also have a take_profit price and tp_order_id set.

Background:
  GLD (trade_id=135), XLI (trade_id=185), XLY (trade_id=167) were
  TP-naked for 5+ days — their trailing stops were cancelled and
  replaced with OCO (stop + limit TP) on 2026-04-29.

  This test prevents a future regression where a position is opened
  with a stop but no matching TP.

Test scope:
  - Reads from the ISOLATED test DB (via conftest._isolate_prod_db fixture)
  - Seeds open positions with/without TP to verify the contract
  - Does NOT touch the production DB
"""
from __future__ import annotations

import sqlite3
from datetime import date, datetime, timezone

import pytest


# ── Helpers ────────────────────────────────────────────────────────────

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _insert_open_trade(
    conn: sqlite3.Connection,
    *,
    trade_id: int,
    ticker: str,
    stop_order_id: str = "stop-oid-001",
    take_profit: float | None = 200.0,
    tp_order_id: str = "tp-oid-001",
    stop_price: float = 95.0,
) -> None:
    """Insert a minimal open trade row."""
    conn.execute(
        """
        INSERT INTO trades (
            id, ticker, strategy, universe, direction,
            entry_date, entry_price, shares,
            stop_price, take_profit,
            status, stop_order_id, tp_order_id,
            created_at, updated_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            trade_id, ticker, "momentum_breakout", "sp500", "long",
            str(date.today()), 100.0, 5,
            stop_price, take_profit,
            "open", stop_order_id, tp_order_id,
            _now_iso(), _now_iso(),
        ),
    )
    conn.commit()


def _open_trades_missing_tp(conn: sqlite3.Connection) -> list[dict]:
    """
    Return open trades that have a stop_order_id but are missing
    take_profit or tp_order_id.

    This is the production invariant we enforce.
    """
    rows = conn.execute(
        """
        SELECT id, ticker, universe, strategy, stop_order_id, take_profit, tp_order_id
        FROM trades
        WHERE status = 'open'
          AND stop_order_id IS NOT NULL
          AND stop_order_id != ''
          AND (
                take_profit IS NULL
             OR tp_order_id IS NULL
             OR tp_order_id = ''
          )
        """,
    ).fetchall()
    return [dict(r) for r in rows]


# ── Tests ───────────────────────────────────────────────────────────────

class TestOpenPositionTPInvariant:
    """Every open trade with stop_order_id must have take_profit + tp_order_id."""

    def test_fully_bracketed_trade_passes(self, tmp_path):
        """A trade with both stop and TP set should pass the invariant."""
        from db.atlas_db import init_db
        db_path = str(tmp_path / "test.db")

        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        # Minimal schema
        init_db(db_path)

        _insert_open_trade(
            conn,
            trade_id=1,
            ticker="AAPL",
            stop_order_id="stop-001",
            take_profit=175.0,
            tp_order_id="tp-001",
        )

        violations = _open_trades_missing_tp(conn)
        assert violations == [], f"Expected no violations, got: {violations}"
        conn.close()

    def test_tp_naked_trade_is_detected(self, tmp_path):
        """A trade with stop_order_id but NULL take_profit should be caught."""
        from db.atlas_db import init_db
        db_path = str(tmp_path / "test.db")

        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        init_db(db_path)

        _insert_open_trade(
            conn,
            trade_id=2,
            ticker="MSFT",
            stop_order_id="stop-002",
            take_profit=None,        # ← TP-naked
            tp_order_id="",
        )

        violations = _open_trades_missing_tp(conn)
        assert len(violations) == 1
        assert violations[0]["ticker"] == "MSFT"
        conn.close()

    def test_tp_set_but_no_order_id_is_detected(self, tmp_path):
        """A trade with take_profit price but empty tp_order_id should be caught."""
        from db.atlas_db import init_db
        db_path = str(tmp_path / "test.db")

        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        init_db(db_path)

        _insert_open_trade(
            conn,
            trade_id=3,
            ticker="NVDA",
            stop_order_id="stop-003",
            take_profit=500.0,       # price set
            tp_order_id="",          # ← order ID missing
        )

        violations = _open_trades_missing_tp(conn)
        assert len(violations) == 1
        assert violations[0]["ticker"] == "NVDA"
        conn.close()

    def test_no_stop_order_id_is_exempt(self, tmp_path):
        """A trade without a stop_order_id is exempt (entry-only, not yet protected)."""
        from db.atlas_db import init_db
        db_path = str(tmp_path / "test.db")

        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        init_db(db_path)

        _insert_open_trade(
            conn,
            trade_id=4,
            ticker="AMD",
            stop_order_id="",   # no stop placed yet
            take_profit=None,   # also no TP
            tp_order_id="",
        )

        violations = _open_trades_missing_tp(conn)
        assert violations == [], "No-stop trades should not trigger TP violation"
        conn.close()

    def test_closed_trades_are_exempt(self, tmp_path):
        """Closed trades do not need tp_order_id."""
        from db.atlas_db import init_db
        db_path = str(tmp_path / "test.db")

        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        init_db(db_path)

        # Insert a closed trade with stop but no TP
        conn.execute(
            """
            INSERT INTO trades (
                id, ticker, strategy, universe, direction,
                entry_date, entry_price, shares, stop_price,
                exit_date, exit_price, status,
                stop_order_id, tp_order_id, created_at, updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                5, "GOOGL", "momentum_breakout", "sp500", "long",
                "2026-01-10", 150.0, 3, 140.0,
                "2026-01-20", 155.0, "closed",
                "stop-closed-001", "", _now_iso(), _now_iso(),
            ),
        )
        conn.commit()

        violations = _open_trades_missing_tp(conn)
        assert violations == [], "Closed trades should not be flagged"
        conn.close()

    def test_multiple_violations_all_reported(self, tmp_path):
        """When multiple open trades are TP-naked, all are reported."""
        from db.atlas_db import init_db
        db_path = str(tmp_path / "test.db")

        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        init_db(db_path)

        for i, ticker in enumerate(["GLD", "XLI", "XLY"], start=10):
            _insert_open_trade(
                conn,
                trade_id=i,
                ticker=ticker,
                stop_order_id=f"stop-{i:03d}",
                take_profit=None,   # all TP-naked
                tp_order_id="",
            )

        violations = _open_trades_missing_tp(conn)
        assert len(violations) == 3
        violating_tickers = {v["ticker"] for v in violations}
        assert violating_tickers == {"GLD", "XLI", "XLY"}
        conn.close()


class TestPhase1ASpecificPositions:
    """
    Guard that the specific 3 positions from Phase 1A RCA are correctly
    bracketed.  These read from the REAL production DB via the global
    _isolate_prod_db fixture.
    """

    def test_gld_is_fully_bracketed(self):
        """trade_id=135 (GLD) must have take_profit and tp_order_id."""
        import sqlite3 as _sq
        from db import atlas_db as _adb

        db_path = _adb._db_path_override or str(_adb._default_db_path())
        conn = _sq.connect(db_path)
        conn.row_factory = _sq.Row
        row = conn.execute(
            "SELECT take_profit, tp_order_id, stop_order_id, status "
            "FROM trades WHERE id=135"
        ).fetchone()
        conn.close()

        if row is None:
            pytest.skip("trade_id=135 not in DB (isolated test DB)")
        if row["status"] != "open":
            pytest.skip("trade_id=135 is closed — no longer relevant")

        assert row["take_profit"] is not None, "GLD (id=135) missing take_profit"
        assert row["tp_order_id"], "GLD (id=135) missing tp_order_id"
        assert row["stop_order_id"], "GLD (id=135) missing stop_order_id"

    def test_xli_is_fully_bracketed(self):
        """trade_id=185 (XLI) must have take_profit and tp_order_id."""
        import sqlite3 as _sq
        from db import atlas_db as _adb

        db_path = _adb._db_path_override or str(_adb._default_db_path())
        conn = _sq.connect(db_path)
        conn.row_factory = _sq.Row
        row = conn.execute(
            "SELECT take_profit, tp_order_id, stop_order_id, status "
            "FROM trades WHERE id=185"
        ).fetchone()
        conn.close()

        if row is None:
            pytest.skip("trade_id=185 not in DB (isolated test DB)")
        if row["status"] != "open":
            pytest.skip("trade_id=185 is closed")

        assert row["take_profit"] is not None, "XLI (id=185) missing take_profit"
        assert row["tp_order_id"], "XLI (id=185) missing tp_order_id"

    def test_xly_is_fully_bracketed(self):
        """trade_id=167 (XLY) must have take_profit and tp_order_id."""
        import sqlite3 as _sq
        from db import atlas_db as _adb

        db_path = _adb._db_path_override or str(_adb._default_db_path())
        conn = _sq.connect(db_path)
        conn.row_factory = _sq.Row
        row = conn.execute(
            "SELECT take_profit, tp_order_id, stop_order_id, status "
            "FROM trades WHERE id=167"
        ).fetchone()
        conn.close()

        if row is None:
            pytest.skip("trade_id=167 not in DB (isolated test DB)")
        if row["status"] != "open":
            pytest.skip("trade_id=167 is closed")

        assert row["take_profit"] is not None, "XLY (id=167) missing take_profit"
        assert row["tp_order_id"], "XLY (id=167) missing tp_order_id"
