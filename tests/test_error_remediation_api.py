"""Tests for /api/error_remediation/* endpoints — Phase 1.

Uses a per-test isolated SQLite DB with the errors_remediation schema.
Patches services.api.error_remediation._DB_PATH and _cache to avoid
touching the production atlas.db and to ensure cache-isolation.
"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.security import HTTPBasicCredentials
from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# Schema helpers
# ---------------------------------------------------------------------------

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS errors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fingerprint TEXT UNIQUE NOT NULL,
    first_seen_ts TEXT NOT NULL DEFAULT (datetime('now')),
    last_seen_ts TEXT NOT NULL DEFAULT (datetime('now')),
    occurrence_count INTEGER DEFAULT 1,
    ts TEXT NOT NULL DEFAULT (datetime('now')),
    source TEXT NOT NULL DEFAULT 'python_logger',
    service TEXT DEFAULT NULL,
    level TEXT NOT NULL DEFAULT 'ERROR',
    logger_name TEXT DEFAULT NULL,
    message TEXT NOT NULL DEFAULT '',
    exc_type TEXT DEFAULT NULL,
    exc_message TEXT DEFAULT NULL,
    traceback TEXT DEFAULT NULL,
    file_path TEXT DEFAULT NULL,
    line_number INTEGER DEFAULT NULL,
    function_name TEXT DEFAULT NULL,
    pid INTEGER DEFAULT NULL,
    hostname TEXT DEFAULT NULL,
    context_json TEXT DEFAULT NULL,
    market_hours INTEGER DEFAULT 0,
    halt_active INTEGER DEFAULT 0,
    git_sha TEXT DEFAULT NULL,
    classification TEXT DEFAULT 'UNCLASSIFIED',
    triage_reason TEXT DEFAULT NULL,
    tier INTEGER DEFAULT 99,
    remediation_status TEXT DEFAULT 'NEW',
    remediation_attempts INTEGER DEFAULT 0,
    last_attempt_at TEXT DEFAULT NULL,
    fixed_by_attempt_id INTEGER DEFAULT NULL,
    resolved_at TEXT DEFAULT NULL,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS fix_attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    error_id INTEGER NOT NULL,
    fingerprint TEXT NOT NULL,
    started_ts TEXT NOT NULL DEFAULT (datetime('now')),
    finished_ts TEXT DEFAULT NULL,
    status TEXT DEFAULT 'triaged',
    classification TEXT DEFAULT NULL,
    triage_model TEXT DEFAULT NULL,
    triage_reason TEXT DEFAULT NULL,
    triage_tokens INTEGER DEFAULT NULL,
    fix_branch TEXT DEFAULT NULL,
    fix_commit_sha TEXT DEFAULT NULL,
    fix_diff_lines INTEGER DEFAULT NULL,
    review_verdict TEXT DEFAULT NULL,
    review_confidence REAL DEFAULT NULL,
    blocked_by_gate TEXT DEFAULT NULL,
    monitor_outcome TEXT DEFAULT NULL,
    total_wall_seconds REAL DEFAULT NULL,
    notes TEXT DEFAULT NULL
);

CREATE TABLE IF NOT EXISTS fix_audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    attempt_id INTEGER DEFAULT NULL,
    error_id INTEGER DEFAULT NULL,
    ts TEXT DEFAULT (datetime('now')),
    phase TEXT NOT NULL DEFAULT 'capture',
    actor TEXT NOT NULL DEFAULT 'system',
    model TEXT DEFAULT NULL,
    decision TEXT DEFAULT NULL,
    reasoning TEXT DEFAULT NULL,
    diff TEXT DEFAULT NULL,
    payload_json TEXT DEFAULT NULL,
    duration_sec REAL DEFAULT NULL,
    tokens_in INTEGER DEFAULT NULL,
    tokens_out INTEGER DEFAULT NULL,
    cost_usd REAL DEFAULT 0,
    result_status TEXT DEFAULT NULL,
    blocked_by_gate TEXT DEFAULT NULL,
    notes TEXT DEFAULT NULL
);
"""


def _create_db(path: Path) -> None:
    """Create the errors_remediation schema at *path*."""
    conn = sqlite3.connect(str(path))
    conn.executescript(_SCHEMA_SQL)
    conn.commit()
    conn.close()


def _insert_error(
    db_path: Path,
    fingerprint: str = "abc123",
    classification: str = "UNCLASSIFIED",
    remediation_status: str = "NEW",
    occurrence_count: int = 1,
    service: str | None = "atlas-dashboard",
    level: str = "ERROR",
    message: str = "Test error",
    last_seen_ts: str | None = None,
    tier: int = 99,
) -> int:
    conn = sqlite3.connect(str(db_path))
    ts = last_seen_ts or "2026-04-29T12:00:00"
    cur = conn.execute(
        """INSERT INTO errors
           (fingerprint, classification, remediation_status, occurrence_count,
            service, level, message, ts, last_seen_ts, tier)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (fingerprint, classification, remediation_status, occurrence_count,
         service, level, message, ts, ts, tier),
    )
    conn.commit()
    row_id = cur.lastrowid
    conn.close()
    return row_id  # type: ignore[return-value]


def _insert_attempt(db_path: Path, error_id: int, status: str = "triaged") -> None:
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "INSERT INTO fix_attempts (error_id, fingerprint, status) VALUES (?, ?, ?)",
        (error_id, "abc123", status),
    )
    conn.commit()
    conn.close()


def _insert_audit(db_path: Path) -> None:
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "INSERT INTO fix_audit_log (phase, actor) VALUES (?, ?)",
        ("capture", "classifier"),
    )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def isolated_db(tmp_path: Path) -> Path:
    """Isolated SQLite DB with empty errors_remediation tables."""
    db = tmp_path / "test_atlas.db"
    _create_db(db)
    return db


@pytest.fixture()
def client(isolated_db: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """TestClient with DB patched + auth bypassed + cache cleared."""
    import services.api.error_remediation as rem
    monkeypatch.setattr(rem, "_DB_PATH", isolated_db)
    monkeypatch.setattr(rem, "_cache", {})

    from services.auth import check_auth
    app = FastAPI()
    app.dependency_overrides[check_auth] = lambda: HTTPBasicCredentials(
        username="test", password="test"
    )
    app.include_router(rem.router)
    return TestClient(app, raise_server_exceptions=True)


@pytest.fixture()
def unauth_client(isolated_db: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """TestClient with NO auth override — real HTTPBasic fires."""
    import services.api.error_remediation as rem
    monkeypatch.setattr(rem, "_DB_PATH", isolated_db)
    monkeypatch.setattr(rem, "_cache", {})

    app = FastAPI()
    app.include_router(rem.router)
    return TestClient(app, raise_server_exceptions=False)


# ---------------------------------------------------------------------------
# Tests: /summary
# ---------------------------------------------------------------------------

class TestSummaryEndpoint:
    def test_summary_returns_200_with_auth(self, client: TestClient) -> None:
        resp = client.get("/api/error_remediation/summary")
        assert resp.status_code == 200, resp.text

    def test_summary_returns_401_without_auth(self, unauth_client: TestClient) -> None:
        resp = unauth_client.get("/api/error_remediation/summary")
        assert resp.status_code == 401, f"Expected 401, got {resp.status_code}"

    def test_summary_errors_total_nonnegative(self, client: TestClient, isolated_db: Path) -> None:
        _insert_error(isolated_db, fingerprint="fp1")
        _insert_error(isolated_db, fingerprint="fp2")
        resp = client.get("/api/error_remediation/summary")
        data = resp.json()
        assert data["errors_total"] >= 0
        assert data["errors_total"] == 2

    def test_summary_returns_by_classification_dict(self, client: TestClient, isolated_db: Path) -> None:
        _insert_error(isolated_db, fingerprint="fp_uc", classification="UNCLASSIFIED")
        _insert_error(isolated_db, fingerprint="fp_af", classification="AUTO_FIX")
        resp = client.get("/api/error_remediation/summary")
        data = resp.json()
        assert isinstance(data["by_classification"], dict)
        assert "UNCLASSIFIED" in data["by_classification"]
        assert "AUTO_FIX" in data["by_classification"]

    def test_summary_phase_fields(self, client: TestClient) -> None:
        resp = client.get("/api/error_remediation/summary")
        data = resp.json()
        assert data["phase"] == 1
        assert data["phase_3_enabled"] is False
        assert data["dry_run"] is True

    def test_summary_attempts_total_zero_in_phase1(self, client: TestClient) -> None:
        resp = client.get("/api/error_remediation/summary")
        data = resp.json()
        assert data["attempts_total"] == 0


# ---------------------------------------------------------------------------
# Tests: /timeseries
# ---------------------------------------------------------------------------

class TestTimeseriesEndpoint:
    def test_timeseries_returns_200(self, client: TestClient) -> None:
        resp = client.get("/api/error_remediation/timeseries?hours=24")
        assert resp.status_code == 200, resp.text

    def test_timeseries_clamps_hours_lower_bound(self, client: TestClient) -> None:
        # hours=0 should be rejected (ge=1)
        resp = client.get("/api/error_remediation/timeseries?hours=0")
        assert resp.status_code == 422

    def test_timeseries_clamps_hours_upper_bound(self, client: TestClient) -> None:
        # hours=169 should be rejected (le=168)
        resp = client.get("/api/error_remediation/timeseries?hours=169")
        assert resp.status_code == 422

    def test_timeseries_returns_buckets_array(self, client: TestClient) -> None:
        resp = client.get("/api/error_remediation/timeseries?hours=24")
        data = resp.json()
        assert "buckets" in data
        assert isinstance(data["buckets"], list)

    def test_timeseries_buckets_have_expected_keys(self, client: TestClient, isolated_db: Path) -> None:
        import datetime
        recent_ts = (datetime.datetime.utcnow() - datetime.timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S")
        _insert_error(isolated_db, fingerprint="ts_fp", last_seen_ts=recent_ts)
        resp = client.get("/api/error_remediation/timeseries?hours=24")
        data = resp.json()
        if data["buckets"]:
            bucket = data["buckets"][0]
            assert "hour" in bucket
            assert "errors" in bucket
            assert "occurrences" in bucket


# ---------------------------------------------------------------------------
# Tests: /fingerprints
# ---------------------------------------------------------------------------

class TestFingerprintsEndpoint:
    def test_fingerprints_returns_top_n_with_limit(self, client: TestClient, isolated_db: Path) -> None:
        import datetime
        recent_ts = (datetime.datetime.utcnow() - datetime.timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S")
        for i in range(15):
            _insert_error(isolated_db, fingerprint=f"fp{i:04d}", last_seen_ts=recent_ts, occurrence_count=i + 1)
        resp = client.get("/api/error_remediation/fingerprints?hours=24&limit=10")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["fingerprints"]) <= 10

    def test_fingerprints_rows_have_required_fields(self, client: TestClient, isolated_db: Path) -> None:
        import datetime
        recent_ts = (datetime.datetime.utcnow() - datetime.timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S")
        _insert_error(isolated_db, fingerprint="fp_check", message="boom", service="atlas-dashboard",
                      last_seen_ts=recent_ts)
        resp = client.get("/api/error_remediation/fingerprints?hours=24&limit=10")
        data = resp.json()
        assert data["fingerprints"], "Expected at least 1 row"
        row = data["fingerprints"][0]
        for field in ("fingerprint", "occurrence_count", "message", "service"):
            assert field in row, f"Missing field: {field}"

    def test_fingerprints_ordered_by_occurrence_count_desc(self, client: TestClient, isolated_db: Path) -> None:
        import datetime
        recent_ts = (datetime.datetime.utcnow() - datetime.timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S")
        _insert_error(isolated_db, fingerprint="rare", occurrence_count=1, last_seen_ts=recent_ts)
        _insert_error(isolated_db, fingerprint="common", occurrence_count=99, last_seen_ts=recent_ts)
        resp = client.get("/api/error_remediation/fingerprints?hours=24&limit=10")
        data = resp.json()
        fps = [r["fingerprint"] for r in data["fingerprints"]]
        assert fps[0] == "common", f"Expected 'common' first, got {fps}"


# ---------------------------------------------------------------------------
# Tests: /attempts
# ---------------------------------------------------------------------------

class TestAttemptsEndpoint:
    def test_attempts_returns_200(self, client: TestClient) -> None:
        resp = client.get("/api/error_remediation/attempts")
        assert resp.status_code == 200, resp.text

    def test_attempts_empty_in_phase1(self, client: TestClient) -> None:
        resp = client.get("/api/error_remediation/attempts")
        data = resp.json()
        assert data["attempts"] == []
        assert data["limit"] == 50

    def test_attempts_status_filter_returns_matched_rows(self, client: TestClient, isolated_db: Path) -> None:
        error_id = _insert_error(isolated_db, fingerprint="fp_attempt")
        _insert_attempt(isolated_db, error_id, status="triaged")
        _insert_attempt(isolated_db, error_id, status="merged")
        resp = client.get("/api/error_remediation/attempts?status=triaged")
        data = resp.json()
        assert data["status_filter"] == "triaged"
        assert all(a["status"] == "triaged" for a in data["attempts"])
        assert len(data["attempts"]) == 1


# ---------------------------------------------------------------------------
# Tests: /health
# ---------------------------------------------------------------------------

class TestHealthEndpoint:
    def test_health_returns_200(self, client: TestClient) -> None:
        resp = client.get("/api/error_remediation/health")
        assert resp.status_code == 200, resp.text

    def test_health_ok_true_when_backlog_ok_no_halt(self, client: TestClient) -> None:
        resp = client.get("/api/error_remediation/health")
        data = resp.json()
        # Empty DB → backlog=0, no HALT files → ok=True
        assert data["classifier_backlog_ok"] is True
        assert data["halt_active"] is False
        assert data["ok"] is True

    def test_health_halt_active_when_halt_file_exists(
        self, isolated_db: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Create a HALT file under a tmp PROJECT_ROOT and verify halt_active=True."""
        import services.api.error_remediation as rem

        # Set up fake PROJECT_ROOT with a HALT file
        fake_root = tmp_path / "fake_atlas"
        fake_data_dir = fake_root / "data"
        fake_data_dir.mkdir(parents=True)
        halt_file = fake_data_dir / "HALT"
        halt_file.touch()

        monkeypatch.setattr(rem, "_DB_PATH", isolated_db)
        monkeypatch.setattr(rem, "_cache", {})
        monkeypatch.setattr(rem, "PROJECT_ROOT", fake_root)

        from services.auth import check_auth
        app = FastAPI()
        app.dependency_overrides[check_auth] = lambda: HTTPBasicCredentials(
            username="test", password="test"
        )
        app.include_router(rem.router)
        client = TestClient(app)

        resp = client.get("/api/error_remediation/health")
        data = resp.json()
        assert data["halt_active"] is True
        assert data["ok"] is False

    def test_health_phase_fields_correct(self, client: TestClient) -> None:
        resp = client.get("/api/error_remediation/health")
        data = resp.json()
        assert data["phase"] == 1
        assert data["phase_3_enabled"] is False


# ---------------------------------------------------------------------------
# Test: caching
# ---------------------------------------------------------------------------

class TestCaching:
    def test_two_consecutive_summary_calls_hit_cache(
        self, isolated_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Second /summary call within TTL window must not re-open the DB."""
        import services.api.error_remediation as rem
        monkeypatch.setattr(rem, "_DB_PATH", isolated_db)
        monkeypatch.setattr(rem, "_cache", {})

        call_count = 0
        original_get_conn = rem._get_conn

        def counting_get_conn():
            nonlocal call_count
            call_count += 1
            return original_get_conn()

        monkeypatch.setattr(rem, "_get_conn", counting_get_conn)

        from services.auth import check_auth
        app = FastAPI()
        app.dependency_overrides[check_auth] = lambda: HTTPBasicCredentials(
            username="test", password="test"
        )
        app.include_router(rem.router)
        tc = TestClient(app)

        r1 = tc.get("/api/error_remediation/summary")
        r2 = tc.get("/api/error_remediation/summary")
        assert r1.status_code == 200
        assert r2.status_code == 200
        # DB should have been hit exactly once; second call served from _cache
        assert call_count == 1, f"Expected 1 DB call, got {call_count}"


# ---------------------------------------------------------------------------
# Test: phase 3 disabled
# ---------------------------------------------------------------------------

class TestPhase3Disabled:
    def test_phase3_disabled_in_summary(self, client: TestClient) -> None:
        resp = client.get("/api/error_remediation/summary")
        data = resp.json()
        assert data["phase_3_enabled"] is False, (
            "Phase 3 must be disabled in Phase 1 deployments"
        )

    def test_phase3_disabled_in_health(self, client: TestClient) -> None:
        resp = client.get("/api/error_remediation/health")
        data = resp.json()
        assert data["phase_3_enabled"] is False
