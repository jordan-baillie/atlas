"""Tests for RCA #4D — per-market virtual equity attribution.

Covers:
  1. attribute_equity_pro_rata — 3-market pro-rata sum
  2. attribute_equity_pro_rata — zero-position equal cash split
  3. attribute_equity_pro_rata — single market gets all
  4. market_equity_history upsert idempotency (UNIQUE constraint)
  5. market_equity_history distinct rows per distinct day
  6. /api/market_equity_history endpoint returns chronological order
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def _isolated_db(tmp_path, monkeypatch):
    """Isolated SQLite with market_equity_history schema."""
    import db.atlas_db as _adb
    from db.atlas_db import init_db

    db_file = tmp_path / "test_equity.db"
    monkeypatch.setattr(_adb, "_db_path_override", str(db_file))
    init_db()

    # Ensure market_equity_history exists (migration idempotent)
    with sqlite3.connect(str(db_file)) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS market_equity_history (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                date             TEXT NOT NULL,
                market_id        TEXT NOT NULL,
                allocated_equity REAL NOT NULL,
                position_mv      REAL NOT NULL,
                cash_attributed  REAL NOT NULL,
                broker_equity    REAL NOT NULL,
                broker_cash      REAL NOT NULL,
                snapshot_time    TEXT NOT NULL,
                created_at       TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(date, market_id)
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_meq_date ON market_equity_history(date)"
        )
        conn.commit()

    yield str(db_file)


def _insert_row(db_file: str, date: str, market_id: str, allocated_equity: float,
                position_mv: float, cash_attributed: float,
                broker_equity: float = 5000.0, broker_cash: float = 500.0) -> None:
    with sqlite3.connect(db_file) as conn:
        conn.execute(
            """INSERT OR REPLACE INTO market_equity_history
               (date, market_id, allocated_equity, position_mv, cash_attributed,
                broker_equity, broker_cash, snapshot_time)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (date, market_id, allocated_equity, position_mv, cash_attributed,
             broker_equity, broker_cash, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()


# ---------------------------------------------------------------------------
# 1. Pro-rata sum test (3 markets)
# ---------------------------------------------------------------------------

def test_attribute_pro_rata_with_3_markets_sums_to_total():
    from portfolio.market_equity_attribution import attribute_equity_pro_rata

    broker_equity = 5000.0
    broker_cash = 500.0
    positions_by_market = {
        "sp500":          [{"ticker": "AAPL", "market_value": 1000.0}],
        "commodity_etfs": [{"ticker": "GLD",  "market_value": 2000.0}],
        "sector_etfs":    [{"ticker": "XLK",  "market_value": 1500.0}],
    }

    result = attribute_equity_pro_rata(broker_equity, broker_cash, positions_by_market)

    assert set(result.keys()) == {"sp500", "commodity_etfs", "sector_etfs"}

    total_allocated = sum(v["allocated_equity"] for v in result.values())
    # Allow rounding tolerance of $0.10
    assert abs(total_allocated - broker_equity) < 0.10, (
        f"Expected sum={broker_equity}, got {total_allocated}"
    )

    # sp500 gets 1000/4500 of cash + 1000 MV
    sp_mv = result["sp500"]["position_mv"]
    assert abs(sp_mv - 1000.0) < 0.01

    comm_mv = result["commodity_etfs"]["position_mv"]
    assert abs(comm_mv - 2000.0) < 0.01

    sect_mv = result["sector_etfs"]["position_mv"]
    assert abs(sect_mv - 1500.0) < 0.01


# ---------------------------------------------------------------------------
# 2. Zero-position equal split
# ---------------------------------------------------------------------------

def test_attribute_pro_rata_zero_positions_splits_cash_equally():
    from portfolio.market_equity_attribution import attribute_equity_pro_rata

    broker_equity = 1000.0
    broker_cash = 1000.0
    positions_by_market = {
        "sp500": [],
        "commodity_etfs": [],
        "sector_etfs": [],
    }

    result = attribute_equity_pro_rata(broker_equity, broker_cash, positions_by_market)

    assert len(result) == 3
    for market_id, vals in result.items():
        assert abs(vals["cash_attributed"] - 333.33) < 1.0, (
            f"{market_id}: expected ~333.33, got {vals['cash_attributed']}"
        )
        assert abs(vals["allocated_equity"] - 333.33) < 1.0
        assert vals["position_mv"] == 0.0


# ---------------------------------------------------------------------------
# 3. Single market gets all equity
# ---------------------------------------------------------------------------

def test_attribute_pro_rata_one_market_only():
    from portfolio.market_equity_attribution import attribute_equity_pro_rata

    broker_equity = 5000.0
    broker_cash = 500.0
    positions_by_market = {
        "sp500": [{"ticker": "AAPL", "market_value": 4500.0}],
    }

    result = attribute_equity_pro_rata(broker_equity, broker_cash, positions_by_market)

    assert "sp500" in result
    assert abs(result["sp500"]["position_mv"] - 4500.0) < 0.01
    assert abs(result["sp500"]["cash_attributed"] - 500.0) < 0.01
    assert abs(result["sp500"]["allocated_equity"] - 5000.0) < 0.01


# ---------------------------------------------------------------------------
# 4. Upsert idempotency — UNIQUE(date, market_id)
# ---------------------------------------------------------------------------

def test_market_equity_history_upserts_idempotent(_isolated_db):
    db_file = _isolated_db
    # Insert same row twice — should result in exactly 1 row (INSERT OR REPLACE)
    _insert_row(db_file, "2026-04-29", "sp500", 2500.0, 2000.0, 500.0)
    _insert_row(db_file, "2026-04-29", "sp500", 2600.0, 2100.0, 500.0)  # updated values

    with sqlite3.connect(db_file) as conn:
        rows = conn.execute(
            "SELECT allocated_equity FROM market_equity_history "
            "WHERE date='2026-04-29' AND market_id='sp500'"
        ).fetchall()

    assert len(rows) == 1, f"Expected 1 row, got {len(rows)}"
    # INSERT OR REPLACE — last write wins
    assert abs(rows[0][0] - 2600.0) < 0.01


# ---------------------------------------------------------------------------
# 5. Distinct rows per distinct day
# ---------------------------------------------------------------------------

def test_market_equity_history_distinct_per_day(_isolated_db):
    db_file = _isolated_db
    _insert_row(db_file, "2026-04-28", "sp500", 2400.0, 1900.0, 500.0)
    _insert_row(db_file, "2026-04-29", "sp500", 2500.0, 2000.0, 500.0)

    with sqlite3.connect(db_file) as conn:
        rows = conn.execute(
            "SELECT date FROM market_equity_history WHERE market_id='sp500' ORDER BY date"
        ).fetchall()

    dates = [r[0] for r in rows]
    assert dates == ["2026-04-28", "2026-04-29"], f"Got: {dates}"


# ---------------------------------------------------------------------------
# 6. Endpoint returns history in chronological order
# ---------------------------------------------------------------------------

def test_endpoint_returns_history_in_order(_isolated_db):
    """Call /api/market_equity_history; assert rows are chronological."""
    _insert_row(_isolated_db, "2026-04-27", "sp500",          2300.0, 1800.0, 500.0)
    _insert_row(_isolated_db, "2026-04-27", "commodity_etfs", 1200.0, 900.0,  300.0)
    _insert_row(_isolated_db, "2026-04-28", "sp500",          2400.0, 1900.0, 500.0)
    _insert_row(_isolated_db, "2026-04-29", "sp500",          2500.0, 2000.0, 500.0)

    # Bypass auth so we don't need real credentials in test
    from fastapi.security.http import HTTPBasicCredentials
    from services.chat_server import app, check_auth

    app.dependency_overrides[check_auth] = lambda: HTTPBasicCredentials(
        username="test", password="test"
    )
    try:
        client = TestClient(app, raise_server_exceptions=True)
        resp = client.get("/api/market_equity_history?days=90")
        assert resp.status_code == 200, f"Status {resp.status_code}: {resp.text[:200]}"

        data = resp.json()
        assert "history" in data
        assert "markets" in data

        dates = [r["date"] for r in data["history"]]
        # Dates should be in ascending (chronological) order
        assert dates == sorted(dates), f"Not chronological: {dates}"
        # Should have our inserted rows
        assert len(dates) >= 4
    finally:
        app.dependency_overrides.pop(check_auth, None)
