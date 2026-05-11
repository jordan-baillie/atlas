"""
tests/test_audit_f05_asx_equity.py
====================================
Regression tests for audit finding F-05: ASX universe equity not surfaced.

Before the fix (commit 40c45bd9, 2026-05-11):
  - /api/system/health/universes ASX entry had equity=null
  - /api/admin/universes ASX entry had current_equity=null
  - market_equity_history had no 'asx' row

After the fix:
  - market_equity_history has a 2026-05-11 ASX row: broker_equity=$2,681.65
  - Both health/universes and admin/universes expose asx.equity=$2,681.65
  - Controls tab Universes section shows "$2,681.65" (Moomoo passive holdings)

These tests guard against:
  1. The ASX row being deleted from market_equity_history
  2. health/universes and admin/universes losing the asx equity field

Note on DB isolation: market_equity_history was added via migration script
(2026-04-29), NOT in db/schema.sql, so it is absent from isolated test DBs.
API integration tests seed the table inline before calling TestClient.

Audit ref: F-05.
"""
from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = PROJECT_ROOT / "data" / "atlas.db"

# The canonical ASX equity value seeded on 2026-05-11
_ASX_EQUITY = 2681.65
_TOLERANCE_PCT = 0.01   # allow 1% drift from live-market AUD fluctuations
_ASX_SEED_DATE = "2026-05-11"

# market_equity_history DDL (not in schema.sql — added via migration 2026-04-29)
_MARKET_EQUITY_HISTORY_DDL = """
    CREATE TABLE IF NOT EXISTS market_equity_history (
        date TEXT NOT NULL,
        market_id TEXT NOT NULL,
        broker_equity REAL DEFAULT 0,
        allocated_equity REAL DEFAULT 0,
        position_mv REAL DEFAULT 0,
        cash_attributed REAL DEFAULT 0,
        broker_cash REAL DEFAULT 0,
        snapshot_time TEXT,
        PRIMARY KEY (date, market_id)
    )
"""


def _get_auth() -> tuple[str, str]:
    """Load dashboard credentials from ~/.atlas-secrets.json.

    Skips the test if the file is absent (CI / isolated environment).
    """
    secrets_path = os.path.expanduser("~/.atlas-secrets.json")
    if not Path(secrets_path).exists():
        pytest.skip("~/.atlas-secrets.json not present — skipping integration test")
    with open(secrets_path) as f:
        secrets = json.load(f)
    return secrets["dashboard_user"], secrets["dashboard_pass"]


def _require_db() -> None:
    """Skip if atlas.db is absent."""
    if not DB_PATH.exists():
        pytest.skip("data/atlas.db not present — skipping read-only prod DB check")


def _seed_market_equity_history() -> None:
    """Create market_equity_history in the isolated test DB and insert ASX + sp500 rows.

    Called by API integration tests that need equity data available during
    the TestClient request (which reads from the same isolated DB).
    """
    import db.atlas_db as _adb
    with _adb.get_db() as db:
        db.execute(_MARKET_EQUITY_HISTORY_DDL)
        db.execute(
            "INSERT OR REPLACE INTO market_equity_history VALUES (?,?,?,?,?,?,?,?)",
            ("2026-05-11", "asx", _ASX_EQUITY, _ASX_EQUITY, _ASX_EQUITY, 0.0, 0.0, None),
        )
        db.execute(
            "INSERT OR REPLACE INTO market_equity_history VALUES (?,?,?,?,?,?,?,?)",
            ("2026-05-11", "sp500", 5211.19, 5211.19, 897.45, 4313.32, 4313.32, None),
        )
        db.commit()


# ── Test 1: health/universes exposes ASX equity ────────────────────────────────

def test_asx_universe_in_health_response() -> None:
    """F-05: GET /api/system/health/universes must include asx with equity > 0.

    The ASX universe is passive (no live trading) but its equity ($2,681.65
    from Moomoo AU account) must be visible for monitoring purposes.

    Seeds the isolated test DB with market_equity_history data before calling
    TestClient (the table is not in schema.sql).
    """
    auth = _get_auth()
    _seed_market_equity_history()

    from fastapi.testclient import TestClient
    from services.chat_server import app

    client = TestClient(app)
    resp = client.get("/api/system/health/universes", auth=auth)
    assert resp.status_code == 200, (
        f"/api/system/health/universes returned HTTP {resp.status_code}: {resp.text[:200]}"
    )

    data = resp.json()
    universes = data.get("universes", [])
    assert isinstance(universes, list), (
        "health/universes response must contain a 'universes' list"
    )

    asx = next((u for u in universes if u.get("market_id") == "asx"), None)
    assert asx is not None, (
        "F-05 regression: 'asx' universe missing from /api/system/health/universes. "
        "ASX entry was absent before fix (no config/active/asx.json discovered or "
        "market_equity_history row missing)."
    )

    equity = asx.get("equity")
    assert equity is not None, (
        "F-05 regression: asx.equity is null in /api/system/health/universes. "
        "Before the fix, equity was null because market_equity_history had no ASX row."
    )
    assert equity > 0, (
        f"F-05 regression: asx.equity = {equity} (must be > 0). "
        f"Expected ≈ {_ASX_EQUITY} (Moomoo AU account balance seeded 2026-05-11)."
    )

    # Soft check: value should match what was seeded
    lower = _ASX_EQUITY * (1 - _TOLERANCE_PCT)
    upper = _ASX_EQUITY * (1 + _TOLERANCE_PCT)
    assert lower <= equity <= upper, (
        f"F-05 equity drift: asx.equity = {equity:.2f}, expected {_ASX_EQUITY:.2f} ±1% "
        f"(range [{lower:.2f}, {upper:.2f}])."
    )


# ── Test 2: admin/universes exposes ASX current_equity ────────────────────────

def test_asx_universe_in_admin_response() -> None:
    """F-05: GET /api/admin/universes must include asx.current_equity > 0.

    The admin endpoint provides the same market_equity_history lookup for
    all universes; ASX should appear with its seeded equity value.

    Seeds the isolated test DB with market_equity_history data before calling
    TestClient.
    """
    auth = _get_auth()
    _seed_market_equity_history()

    from fastapi.testclient import TestClient
    from services.chat_server import app

    client = TestClient(app)
    resp = client.get("/api/admin/universes", auth=auth)
    assert resp.status_code == 200, (
        f"/api/admin/universes returned HTTP {resp.status_code}: {resp.text[:200]}"
    )

    data = resp.json()
    universes = data.get("universes", [])
    assert isinstance(universes, list)

    asx = next((u for u in universes if u.get("market_id") == "asx"), None)
    assert asx is not None, (
        "F-05 regression: 'asx' universe missing from /api/admin/universes. "
        "Check _list_market_ids() in services/api/admin.py — it must discover asx "
        "from config/active/asx.json."
    )

    current_equity = asx.get("current_equity")
    assert current_equity is not None, (
        "F-05 regression: asx.current_equity is null in /api/admin/universes. "
        "Before the fix, this was null because market_equity_history had no ASX row."
    )
    assert current_equity > 0, (
        f"F-05 regression: asx.current_equity = {current_equity} (must be > 0)."
    )

    # Soft check: matches seeded value
    lower = _ASX_EQUITY * (1 - _TOLERANCE_PCT)
    upper = _ASX_EQUITY * (1 + _TOLERANCE_PCT)
    assert lower <= current_equity <= upper, (
        f"F-05: asx.current_equity = {current_equity:.2f}, expected {_ASX_EQUITY:.2f} ±1%."
    )


# ── Test 3: market_equity_history DB row for ASX ──────────────────────────────

@pytest.mark.no_isolate_prod_db
def test_market_equity_history_has_asx_row() -> None:
    """F-05: market_equity_history must have at least one asx row dated 2026-05-11.

    The fix (commit 40c45bd9) seeded this row manually.  If it is deleted or
    the seed script is re-run with the wrong value, this test will catch it.

    Reads directly from the production atlas.db (bypasses DB isolation) via
    sqlite3 — read-only, does not write to the DB.
    """
    _require_db()

    with sqlite3.connect(str(DB_PATH)) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT date, broker_equity, allocated_equity FROM market_equity_history "
            "WHERE market_id = 'asx' AND date >= ? "
            "ORDER BY date DESC LIMIT 1",
            (_ASX_SEED_DATE,),
        ).fetchone()

    assert row is not None, (
        f"F-05 regression: no market_equity_history row for market_id='asx' "
        f"with date >= '{_ASX_SEED_DATE}'. "
        f"Run scripts/seed_asx_equity.py (or equivalent) to restore the seed."
    )

    broker_equity = float(row["broker_equity"])
    allocated_equity = float(row["allocated_equity"])

    assert broker_equity > 0, (
        f"F-05: market_equity_history.asx.broker_equity = {broker_equity} (must be > 0)"
    )
    assert allocated_equity > 0, (
        f"F-05: market_equity_history.asx.allocated_equity = {allocated_equity} (must be > 0)"
    )

    # Value sanity check (within 1%)
    lower = _ASX_EQUITY * (1 - _TOLERANCE_PCT)
    upper = _ASX_EQUITY * (1 + _TOLERANCE_PCT)
    assert lower <= broker_equity <= upper, (
        f"F-05 value drift: market_equity_history.asx.broker_equity = {broker_equity:.2f}, "
        f"expected {_ASX_EQUITY:.2f} ±1%. Update _ASX_EQUITY if value changed legitimately."
    )
