"""Tests for the admin API endpoints (Commit 3).

Uses FastAPI TestClient + autouse _isolate_prod_db fixture (fresh DB per test).
Auth is mocked via monkeypatch on services.auth._get_credentials.

Market states in raw config:
  sp500:          live   (mode=live,    live_enabled=True)
  commodity_etfs: passive (mode=passive, live_enabled=True)
  sector_etfs:    passive (mode=passive, live_enabled=True)
  asx:            disabled (mode=passive, live_enabled=False)
  crypto/gold/defensive/treasury: disabled
"""
from __future__ import annotations

import importlib.util
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

MIGRATION_PATH = PROJECT / "scripts" / "migrations" / "2026-05-05-add-config-overrides.py"
_AUTH = ("testuser", "testpass")


def _load_migration():
    spec = importlib.util.spec_from_file_location("migration_config_overrides", MIGRATION_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(autouse=True)
def _apply_migration():
    """Apply migration and clear config cache before each test."""
    import db.atlas_db as _adb
    db_path = _adb._db_path_override or str(_adb.DB_PATH)
    _load_migration()._run(apply=True, db_path=Path(db_path))
    from utils.config import clear_config_cache
    clear_config_cache()
    yield
    clear_config_cache()


@pytest.fixture
def client(monkeypatch):
    """FastAPI TestClient backed by the full chat_server app with mocked auth."""
    monkeypatch.setattr("services.auth._get_credentials", lambda: _AUTH)
    from fastapi.testclient import TestClient
    from services.chat_server import app
    return TestClient(app, raise_server_exceptions=False)


def _insert_open_trade(market_id: str, strategy: str = "momentum_breakout") -> None:
    """Insert a fake open trade for testing position guards.

    Uses actual trades table schema: direction/shares (not side/qty).
    """
    from db.atlas_db import get_db
    with get_db() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO trades "
            "(ticker, universe, strategy, entry_date, entry_price, shares, direction) "
            "VALUES ('FAKE', ?, ?, date('now'), 100.0, 1.0, 'long')",
            (market_id, strategy),
        )


# ── GET /api/admin/universes ──────────────────────────────────────────────────

def test_get_universes_no_overrides(client):
    """All 8 universes returned; with empty override table, effective == config."""
    r = client.get("/api/admin/universes", auth=_AUTH)
    assert r.status_code == 200
    data = r.json()
    assert "universes" in data
    assert len(data["universes"]) == 8
    for u in data["universes"]:
        assert u["effective_state"] == u["config_state"]


def test_get_universes_with_override(client):
    """Active override changes effective_state but not config_state."""
    from db.atlas_db import get_db
    from utils.config import clear_config_cache
    # commodity_etfs is passive; add a 'live' override
    with get_db() as conn:
        conn.execute(
            "INSERT INTO config_overrides (scope, key, state, created_by) "
            "VALUES ('universe', 'commodity_etfs', 'live', 'test')"
        )
    clear_config_cache()

    r = client.get("/api/admin/universes", auth=_AUTH)
    assert r.status_code == 200
    ce = next(u for u in r.json()["universes"] if u["market_id"] == "commodity_etfs")
    assert ce["effective_state"] == "live"
    assert ce["config_state"] == "passive"  # raw config
    assert ce["override"] is not None


# ── POST /api/admin/universe — position guard ────────────────────────────────

def test_post_universe_disabled_blocked_by_open_positions(client):
    """Attempting to disable with open positions returns 400."""
    _insert_open_trade("commodity_etfs")
    r = client.post(
        "/api/admin/universe/commodity_etfs/state",
        auth=_AUTH,
        json={
            "state": "disabled",
            "reason": "Testing the open-position guard mechanism here",
            "i_understand": True,
        },
    )
    assert r.status_code == 400
    assert "open position" in r.json()["detail"].lower()


def test_post_universe_disabled_succeeds_when_no_positions(client):
    """No open positions → disable succeeds (commodity_etfs is currently passive)."""
    r = client.post(
        "/api/admin/universe/commodity_etfs/state",
        auth=_AUTH,
        json={
            "state": "disabled",
            "reason": "Testing disable on commodity_etfs with no positions",
            "i_understand": True,
        },
    )
    assert r.status_code == 200
    assert r.json()["to_state"] == "disabled"


def test_post_universe_passive_succeeds(client):
    """Passive-type state change succeeds with open positions (no disabled-guard).

    sector_etfs is passive; requesting 'live' is a valid transition.
    sector_etfs is NOT production (live_enabled=True but mode=passive → effective=passive),
    so no confirm_token required. Open positions don't block non-disabled transitions.
    """
    _insert_open_trade("sector_etfs")
    r = client.post(
        "/api/admin/universe/sector_etfs/state",
        auth=_AUTH,
        json={
            "state": "live",
            "reason": "Testing live transition from passive with open positions present",
            "i_understand": True,
        },
    )
    assert r.status_code == 200


# ── POST /api/admin/universe — same-state, validation ────────────────────────

def test_post_universe_same_state_rejected(client):
    """Requesting a state identical to current effective → 409 Conflict."""
    # sp500 is live; requesting live → 409
    r = client.post(
        "/api/admin/universe/sp500/state",
        auth=_AUTH,
        json={
            "state": "live",
            "reason": "Requesting live on already-live universe",
            "confirm_token": "sp500",
            "i_understand": True,
        },
    )
    assert r.status_code == 409


def test_post_universe_invalid_state(client):
    """Invalid state value → 422 Pydantic validation error."""
    r = client.post(
        "/api/admin/universe/commodity_etfs/state",
        auth=_AUTH,
        json={
            "state": "bogus",
            "reason": "Testing invalid state value rejection here",
            "i_understand": True,
        },
    )
    assert r.status_code == 422


def test_post_universe_short_reason_rejected(client):
    """Reason shorter than 10 chars → 422."""
    r = client.post(
        "/api/admin/universe/commodity_etfs/state",
        auth=_AUTH,
        json={
            "state": "disabled",
            "reason": "short",
            "i_understand": True,
        },
    )
    assert r.status_code == 422


def test_post_universe_no_i_understand_rejected(client):
    """i_understand=False → 400."""
    r = client.post(
        "/api/admin/universe/commodity_etfs/state",
        auth=_AUTH,
        json={
            "state": "disabled",
            "reason": "Testing i_understand guard mechanism here",
            "i_understand": False,
        },
    )
    assert r.status_code == 400
    assert "i_understand" in r.json()["detail"].lower()


# ── POST /api/admin/universe — production confirm token ──────────────────────

def test_post_universe_production_requires_confirm_token(client):
    """sp500 (production) requires confirm_token matching market_id."""
    # Missing confirm_token
    r_no_token = client.post(
        "/api/admin/universe/sp500/state",
        auth=_AUTH,
        json={
            "state": "passive",
            "reason": "Testing production universe confirm token requirement",
            "i_understand": True,
        },
    )
    assert r_no_token.status_code == 400
    assert "confirm_token" in r_no_token.json()["detail"].lower()

    # Correct confirm_token
    r_with_token = client.post(
        "/api/admin/universe/sp500/state",
        auth=_AUTH,
        json={
            "state": "passive",
            "reason": "Testing production universe confirm token requirement",
            "confirm_token": "sp500",
            "i_understand": True,
        },
    )
    assert r_with_token.status_code == 200


# ── POST /api/admin/universe — supersede chain ───────────────────────────────

def test_post_universe_supersedes_prior_active(client):
    """Two POSTs: second supersedes the first; only one active row remains."""
    # commodity_etfs is passive → disable it
    r1 = client.post(
        "/api/admin/universe/commodity_etfs/state",
        auth=_AUTH,
        json={
            "state": "disabled",
            "reason": "First override — disabling for test supersede flow",
            "i_understand": True,
        },
    )
    assert r1.status_code == 200

    # Now request passive (supersedes disabled)
    from utils.config import clear_config_cache
    clear_config_cache()
    r2 = client.post(
        "/api/admin/universe/commodity_etfs/state",
        auth=_AUTH,
        json={
            "state": "passive",
            "reason": "Second override — superseding the disabled state now",
            "i_understand": True,
        },
    )
    assert r2.status_code == 200

    from db.atlas_db import get_db
    with get_db() as conn:
        active_count = conn.execute(
            "SELECT COUNT(*) AS n FROM config_overrides WHERE scope='universe' "
            "AND key='commodity_etfs' AND active=1"
        ).fetchone()["n"]
        total_count = conn.execute(
            "SELECT COUNT(*) AS n FROM config_overrides WHERE scope='universe' "
            "AND key='commodity_etfs'"
        ).fetchone()["n"]
        audit_count = conn.execute(
            "SELECT COUNT(*) AS n FROM config_override_audit WHERE key='commodity_etfs'"
        ).fetchone()["n"]

    assert active_count == 1
    assert total_count == 2
    assert audit_count >= 3  # create + supersede + create


# ── POST /api/admin/strategy ─────────────────────────────────────────────────

def test_post_strategy_unknown_returns_404(client):
    """Unknown strategy → 404."""
    r = client.post(
        "/api/admin/strategy/sp500/nonexistent_strategy/state",
        auth=_AUTH,
        json={
            "state": "disabled",
            "reason": "Testing unknown strategy 404 response here",
            "i_understand": True,
        },
    )
    assert r.status_code == 404


def test_post_strategy_disable_succeeds(client):
    """Disabling an enabled strategy returns 200 and correct to_state."""
    r = client.post(
        "/api/admin/strategy/sp500/momentum_breakout/state",
        auth=_AUTH,
        json={
            "state": "disabled",
            "reason": "Testing strategy disable endpoint in unit test context",
            "i_understand": True,
        },
    )
    assert r.status_code == 200
    assert r.json()["to_state"] == "disabled"


# ── POST /api/admin/override/{id}/revert ─────────────────────────────────────

def test_revert_marks_inactive_and_writes_audit(client):
    """Revert soft-deletes the override and writes audit row."""
    r = client.post(
        "/api/admin/universe/commodity_etfs/state",
        auth=_AUTH,
        json={
            "state": "disabled",
            "reason": "Creating override to test revert functionality here",
            "i_understand": True,
        },
    )
    assert r.status_code == 200
    override_id = r.json()["override_id"]

    r2 = client.post(
        f"/api/admin/override/{override_id}/revert",
        auth=_AUTH,
        json={"reason": "Reverting the test override now, no longer needed"},
    )
    assert r2.status_code == 200
    assert r2.json()["reverted_override_id"] == override_id

    from db.atlas_db import get_db
    with get_db() as conn:
        row = conn.execute(
            "SELECT active, ended_reason FROM config_overrides WHERE id=?",
            (override_id,),
        ).fetchone()
        assert row["active"] == 0
        assert row["ended_reason"] == "reverted"
        audit = conn.execute(
            "SELECT action FROM config_override_audit WHERE override_id=? ORDER BY id",
            (override_id,),
        ).fetchall()
    actions = [a["action"] for a in audit]
    assert "create" in actions
    assert "revert" in actions


def test_revert_unknown_id_404(client):
    """Reverting non-existent override → 404."""
    r = client.post(
        "/api/admin/override/99999/revert",
        auth=_AUTH,
        json={"reason": "Reverting a non-existent override to test 404 handling"},
    )
    assert r.status_code == 404


# ── GET /api/admin/override-audit ────────────────────────────────────────────

def test_audit_log_filters_by_scope_and_key(client):
    """scope= and key= query params filter the audit log correctly."""
    from utils.config import clear_config_cache
    client.post("/api/admin/universe/commodity_etfs/state", auth=_AUTH, json={
        "state": "disabled",
        "reason": "Audit filter test: commodity_etfs override first one here",
        "i_understand": True,
    })
    clear_config_cache()
    client.post("/api/admin/strategy/sp500/momentum_breakout/state", auth=_AUTH, json={
        "state": "disabled",
        "reason": "Audit filter test: strategy override second one here",
        "i_understand": True,
    })

    r = client.get("/api/admin/override-audit?scope=universe&key=commodity_etfs", auth=_AUTH)
    assert r.status_code == 200
    audit = r.json()["audit"]
    assert len(audit) > 0
    assert all(a["scope"] == "universe" and a["key"] == "commodity_etfs" for a in audit)


def test_audit_log_respects_limit(client):
    """limit= query param caps the result count."""
    r = client.get("/api/admin/override-audit?limit=5", auth=_AUTH)
    assert r.status_code == 200
    assert len(r.json()["audit"]) <= 5


# ── Expiry and TTL tests ──────────────────────────────────────────────────────

def test_default_ttl_is_30_days(client):
    """Omitted expires_at defaults to now+30d."""
    r = client.post(
        "/api/admin/universe/commodity_etfs/state",
        auth=_AUTH,
        json={
            "state": "disabled",
            "reason": "Testing default 30-day TTL on omitted expires_at field",
            "i_understand": True,
        },
    )
    assert r.status_code == 200
    expires_at = r.json()["expires_at"]
    assert expires_at is not None
    dt = datetime.fromisoformat(expires_at)
    now = datetime.utcnow()
    assert abs((dt - now).total_seconds() - 30 * 86400) < 120


def test_explicit_null_expires_at_is_permanent(client):
    """Explicit null expires_at means permanent (NULL in DB)."""
    r = client.post(
        "/api/admin/universe/commodity_etfs/state",
        auth=_AUTH,
        content=json.dumps({
            "state": "disabled",
            "reason": "Testing permanent override via explicit null expires_at",
            "expires_at": None,
            "i_understand": True,
        }),
        headers={"Content-Type": "application/json"},
    )
    assert r.status_code == 200
    assert r.json()["expires_at"] is None

    from db.atlas_db import get_db
    with get_db() as conn:
        row = conn.execute(
            "SELECT expires_at FROM config_overrides WHERE active=1 "
            "AND key='commodity_etfs'"
        ).fetchone()
    assert row["expires_at"] is None


def test_cache_cleared_after_write(client):
    """After a POST write, get_active_config immediately reflects the override."""
    from utils.config import get_active_config, clear_config_cache
    clear_config_cache()

    # commodity_etfs is passive (live_enabled=True); disable it → live_enabled=False
    r = client.post(
        "/api/admin/universe/commodity_etfs/state",
        auth=_AUTH,
        json={
            "state": "disabled",
            "reason": "Cache invalidation test for the override write path here",
            "i_understand": True,
        },
    )
    assert r.status_code == 200

    # Cache was cleared by the handler — next read should show live_enabled=False
    cfg_after = get_active_config("commodity_etfs")
    assert cfg_after.get("trading", {}).get("live_enabled") is False


def test_unauth_returns_401(client):
    """No auth header → 401 on all endpoints."""
    r = client.get("/api/admin/universes")
    assert r.status_code == 401

    r2 = client.post("/api/admin/universe/commodity_etfs/state", json={
        "state": "passive",
        "reason": "Unauthorized attempt for test purposes",
        "i_understand": True,
    })
    assert r2.status_code == 401
