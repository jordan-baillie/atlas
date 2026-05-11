"""Test F-03 audit fix: /api/lifecycle returns JSON (not HTML SPA fallback).

Verifies that both /api/strategy-lifecycle (original) and /api/lifecycle (alias)
return valid JSON responses, and that the static SPA catch-all no longer intercepts
the /api/lifecycle path.
"""
from __future__ import annotations

import json
import os
import sqlite3

import pytest
from fastapi.testclient import TestClient


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def client_and_auth():
    """Create a TestClient and load real auth credentials."""
    from services.chat_server import app  # noqa: PLC0415

    secrets_path = os.path.expanduser("~/.atlas-secrets.json")
    with open(secrets_path) as f:
        secrets = json.load(f)
    auth = (secrets["dashboard_user"], secrets["dashboard_pass"])
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c, auth


@pytest.fixture(scope="module")
def lifecycle_row():
    """Return (strategy, universe) from strategy_lifecycle table, or None if empty."""
    db_path = os.path.join(os.path.dirname(__file__), "..", "data", "atlas.db")
    try:
        conn = sqlite3.connect(db_path)
        row = conn.execute(
            "SELECT strategy, universe FROM strategy_lifecycle LIMIT 1"
        ).fetchone()
        conn.close()
        return row  # (strategy, universe) or None
    except Exception:
        return None


# ── Core F-03 tests ───────────────────────────────────────────────────────────

def test_lifecycle_alias_returns_json(client_and_auth):
    """F-03: /api/lifecycle must return valid JSON with a 'rows' key (not HTML)."""
    client, auth = client_and_auth
    resp = client.get("/api/lifecycle", auth=auth)
    assert resp.status_code == 200, (
        f"Expected 200, got {resp.status_code}. "
        f"Body (first 300 chars): {resp.text[:300]}"
    )
    # Must be parseable as JSON — if this raises, the endpoint returned HTML
    data = resp.json()
    assert "rows" in data, f"Missing 'rows' key in response: {list(data.keys())}"
    assert isinstance(data["rows"], list), f"'rows' must be a list, got {type(data['rows'])}"


def test_strategy_lifecycle_original_still_works(client_and_auth):
    """F-03: /api/strategy-lifecycle original path must remain functional."""
    client, auth = client_and_auth
    resp = client.get("/api/strategy-lifecycle", auth=auth)
    assert resp.status_code == 200, (
        f"Expected 200, got {resp.status_code}. Body: {resp.text[:300]}"
    )
    data = resp.json()
    assert "rows" in data
    assert isinstance(data["rows"], list)


def test_both_paths_return_same_data(client_and_auth):
    """F-03: /api/lifecycle and /api/strategy-lifecycle must return identical data."""
    client, auth = client_and_auth
    resp_orig = client.get("/api/strategy-lifecycle", auth=auth)
    resp_alias = client.get("/api/lifecycle", auth=auth)
    assert resp_orig.status_code == 200
    assert resp_alias.status_code == 200
    # row counts and strategy names should match
    orig_rows = resp_orig.json()["rows"]
    alias_rows = resp_alias.json()["rows"]
    assert len(orig_rows) == len(alias_rows), (
        f"Row count mismatch: orig={len(orig_rows)} alias={len(alias_rows)}"
    )
    # Compare strategy/universe pairs (order may differ but sets should match)
    orig_pairs = {(r["strategy"], r["universe"]) for r in orig_rows}
    alias_pairs = {(r["strategy"], r["universe"]) for r in alias_rows}
    assert orig_pairs == alias_pairs, (
        f"Strategy/universe pairs differ:\n  orig:  {orig_pairs}\n  alias: {alias_pairs}"
    )


def test_lifecycle_alias_not_html(client_and_auth):
    """F-03: /api/lifecycle must NOT return HTML (was previously SPA catch-all)."""
    client, auth = client_and_auth
    resp = client.get("/api/lifecycle", auth=auth)
    content_type = resp.headers.get("content-type", "")
    assert "html" not in content_type.lower(), (
        f"Content-Type is HTML: {content_type!r} — SPA catch-all still intercepting"
    )
    body_start = resp.text[:50].lower()
    assert not body_start.startswith("<!doctype"), (
        "Response body starts with HTML doctype — SPA catch-all still active"
    )
    assert not body_start.startswith("<html"), (
        "Response body starts with <html> — SPA catch-all still active"
    )


def test_lifecycle_history_alias(client_and_auth, lifecycle_row):
    """F-03: /api/lifecycle/{strategy}/{universe}/history returns valid JSON."""
    client, auth = client_and_auth
    if lifecycle_row is None:
        pytest.skip("No rows in strategy_lifecycle table — cannot test history endpoint")
    strategy, universe = lifecycle_row
    resp = client.get(f"/api/lifecycle/{strategy}/{universe}/history", auth=auth)
    assert resp.status_code == 200, (
        f"history alias returned {resp.status_code}: {resp.text[:300]}"
    )
    data = resp.json()
    assert "history" in data, f"Expected 'history' key, got: {list(data.keys())}"
    assert isinstance(data["history"], list)


def test_lifecycle_history_original_still_works(client_and_auth, lifecycle_row):
    """F-03: /api/strategy-lifecycle/{strategy}/{universe}/history still functional."""
    client, auth = client_and_auth
    if lifecycle_row is None:
        pytest.skip("No rows in strategy_lifecycle table — cannot test history endpoint")
    strategy, universe = lifecycle_row
    resp = client.get(
        f"/api/strategy-lifecycle/{strategy}/{universe}/history", auth=auth
    )
    assert resp.status_code == 200, (
        f"history original returned {resp.status_code}: {resp.text[:300]}"
    )
    data = resp.json()
    assert "history" in data
    assert isinstance(data["history"], list)


def test_lifecycle_history_alias_and_original_match(client_and_auth, lifecycle_row):
    """F-03: Both history paths return identical payloads."""
    client, auth = client_and_auth
    if lifecycle_row is None:
        pytest.skip("No rows in strategy_lifecycle table")
    strategy, universe = lifecycle_row
    resp_orig = client.get(
        f"/api/strategy-lifecycle/{strategy}/{universe}/history", auth=auth
    )
    resp_alias = client.get(
        f"/api/lifecycle/{strategy}/{universe}/history", auth=auth
    )
    assert resp_orig.status_code == 200
    assert resp_alias.status_code == 200
    assert resp_orig.json() == resp_alias.json(), (
        "History payloads differ between alias and original path"
    )


def test_lifecycle_requires_auth(client_and_auth):
    """Security: /api/lifecycle must reject unauthenticated requests."""
    client, _auth = client_and_auth
    resp = client.get("/api/lifecycle")  # no auth
    assert resp.status_code in (401, 403), (
        f"Expected 401/403 without auth, got {resp.status_code}"
    )


def test_strategy_lifecycle_requires_auth(client_and_auth):
    """Security: /api/strategy-lifecycle must reject unauthenticated requests."""
    client, _auth = client_and_auth
    resp = client.get("/api/strategy-lifecycle")  # no auth
    assert resp.status_code in (401, 403), (
        f"Expected 401/403 without auth, got {resp.status_code}"
    )
