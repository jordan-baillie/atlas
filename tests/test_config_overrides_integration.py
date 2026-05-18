"""Integration smoke tests for config override lifecycle (Commit 5).

Tests the full flip lifecycle: API write → get_active_config read → verify state.
All tests use _isolate_prod_db autouse + migration applied via fixture.
Uses FastAPI TestClient with mocked auth.
"""
from __future__ import annotations

import importlib.util
import sys
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
    import db.atlas_db as _adb
    db_path = _adb._db_path_override or str(_adb.DB_PATH)
    _load_migration()._run(apply=True, db_path=Path(db_path))
    from utils.config import clear_config_cache
    clear_config_cache()
    yield
    clear_config_cache()


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr("services.auth._get_credentials", lambda: _AUTH)
    from fastapi.testclient import TestClient
    from services.chat_server import app
    return TestClient(app, raise_server_exceptions=False)


def _post_universe(client, market_id: str, state: str, reason: str = None, **extra) -> dict:
    """Helper: POST a universe state override."""
    from utils.config import clear_config_cache
    clear_config_cache()
    body = {
        "state": state,
        "reason": reason or f"Integration test override for {market_id} to {state}",
        "i_understand": True,
    }
    body.update(extra)
    r = client.post(f"/api/admin/universe/{market_id}/state", auth=_AUTH, json=body)
    clear_config_cache()
    return r


def _post_strategy(client, market_id: str, strategy: str, state: str, reason: str = None) -> dict:
    from utils.config import clear_config_cache
    clear_config_cache()
    body = {
        "state": state,
        "reason": reason or f"Integration test: {market_id}.{strategy} to {state}",
        "i_understand": True,
    }
    r = client.post(f"/api/admin/strategy/{market_id}/{strategy}/state", auth=_AUTH, json=body)
    clear_config_cache()
    return r


def _revert(client, override_id: int, reason: str = "Reverting override for integration test") -> dict:
    from utils.config import clear_config_cache
    r = client.post(
        f"/api/admin/override/{override_id}/revert",
        auth=_AUTH,
        json={"reason": reason},
    )
    clear_config_cache()
    return r


# ── Tests ─────────────────────────────────────────────────────────────────────

def test_full_flip_via_api_to_config_read(client):
    """POST disable on commodity_etfs → get_active_config reflects disabled state."""
    # commodity_etfs raw config: mode=passive, live_enabled=True → effective=passive
    r = _post_universe(client, "commodity_etfs", "disabled")
    assert r.status_code == 200

    from utils.config import get_active_config
    cfg = get_active_config("commodity_etfs")
    assert cfg.get("trading", {}).get("live_enabled") is False
    assert cfg.get("trading", {}).get("mode") == "passive"
    assert cfg.get("_overrides_applied") is True


def test_revert_falls_back_to_json(client):
    """POST disable → revert → get_active_config returns raw JSON values."""
    r = _post_universe(client, "commodity_etfs", "disabled")
    assert r.status_code == 200
    override_id = r.json()["override_id"]

    r2 = _revert(client, override_id)
    assert r2.status_code == 200

    from utils.config import get_active_config, get_raw_config
    cfg = get_active_config("commodity_etfs")
    raw = get_raw_config("commodity_etfs")
    # After revert, effective should match raw
    assert cfg.get("trading", {}).get("live_enabled") == raw.get("trading", {}).get("live_enabled")
    assert cfg.get("trading", {}).get("mode") == raw.get("trading", {}).get("mode")
    assert "_overrides_applied" not in cfg


def test_supersede_chain(client):
    """POST disable → POST passive → only one active row; prior marked superseded."""
    # commodity_etfs is passive → disable
    r1 = _post_universe(client, "commodity_etfs", "disabled")
    assert r1.status_code == 200

    # disable → passive (supersedes)
    r2 = _post_universe(client, "commodity_etfs", "passive")
    assert r2.status_code == 200

    from db.atlas_db import get_db
    with get_db() as conn:
        active = conn.execute(
            "SELECT COUNT(*) AS n FROM config_overrides WHERE key='commodity_etfs' AND active=1"
        ).fetchone()["n"]
        superseded = conn.execute(
            "SELECT COUNT(*) AS n FROM config_overrides "
            "WHERE key='commodity_etfs' AND active=0 AND ended_reason='superseded'"
        ).fetchone()["n"]
    assert active == 1
    assert superseded == 1


def test_audit_trail_complete_flow(client):
    """POST → revert → audit log has 2 events (create + revert) with reason + actor."""
    r = _post_universe(
        client, "commodity_etfs", "disabled",
        reason="Integration audit trail test: disabling commodity_etfs market"
    )
    assert r.status_code == 200
    override_id = r.json()["override_id"]

    _revert(client, override_id, reason="Integration audit trail test: reverting disable")

    r_audit = client.get(
        f"/api/admin/override-audit?scope=universe&key=commodity_etfs",
        auth=_AUTH,
    )
    assert r_audit.status_code == 200
    events = r_audit.json()["audit"]
    actions = [e["action"] for e in events]
    assert "create" in actions
    assert "revert" in actions
    # Verify actor format and reason present
    for e in events:
        assert e["actor"].startswith("human:")
        assert e["reason"]


def test_strategy_override_affects_get_active_config(client):
    """POST disable momentum_breakout on sp500 → cfg['strategies']['momentum_breakout']['enabled'] == False."""
    # sp500 has momentum_breakout enabled in raw config
    # (connors_rsi2 was decommissioned 2026-05-18 per #340 — no longer a valid enabled-strategy anchor)
    from utils.config import get_raw_config
    raw = get_raw_config("sp500")
    assert raw.get("strategies", {}).get("momentum_breakout", {}).get("enabled") is True

    r = _post_strategy(
        client, "sp500", "momentum_breakout", "disabled",
        reason="Integration test: disabling momentum_breakout on sp500"
    )
    assert r.status_code == 200

    from utils.config import get_active_config
    cfg = get_active_config("sp500")
    assert cfg["strategies"]["momentum_breakout"]["enabled"] is False
    assert cfg.get("_overrides_applied") is True


def test_apply_overrides_false_bypasses(client):
    """get_active_config(market_id, apply_overrides=False) returns raw even with active override."""
    r = _post_strategy(
        client, "sp500", "momentum_breakout", "disabled",
        reason="Integration test: verifying bypass with apply_overrides=False"
    )
    assert r.status_code == 200

    from utils.config import get_active_config
    raw = get_active_config("sp500", apply_overrides=False)
    # Raw should still show momentum_breakout as enabled (the original JSON config)
    # (connors_rsi2 was decommissioned 2026-05-18 per #340 — no longer a valid anchor here)
    assert raw["strategies"]["momentum_breakout"]["enabled"] is True
    assert "_overrides_applied" not in raw


def test_expired_override_falls_back_via_get_active_config(client):
    """Override with expires_at in past is ignored — get_active_config returns raw."""
    from db.atlas_db import get_db
    from utils.config import get_active_config, clear_config_cache

    # Insert an already-expired override directly
    with get_db() as conn:
        conn.execute(
            "INSERT INTO config_overrides "
            "(scope, key, state, created_by, active, expires_at) "
            "VALUES ('universe', 'commodity_etfs', 'disabled', 'test', 1, '2000-01-01T00:00:00')"
        )
    clear_config_cache()

    cfg = get_active_config("commodity_etfs")
    raw_cfg = __import__("json").load(open(PROJECT / "config/active/commodity_etfs.json"))
    # Expired override should be ignored — effective should match raw
    assert cfg.get("trading", {}).get("live_enabled") == raw_cfg.get("trading", {}).get("live_enabled")
    # _overrides_applied should NOT be set (override was treated as inactive)
    assert "_overrides_applied" not in cfg
