"""
Tests for the errors-remediation migration:
  scripts/migrations/2026-04-29-add-errors-remediation-tables.py

Verifies:
  • All 3 tables created
  • All 13 indexes created
  • Both immutability triggers created and enforced
  • Migration is idempotent (run twice → no error, no data loss)
  • All CHECK constraints reject invalid values
  • UNIQUE constraint on errors.fingerprint enforced
  • Foreign key constraints enforced
  • Backup file created on --apply

Run:
    cd /root/atlas && python3 -m pytest tests/test_errors_remediation_schema.py -v --timeout=30
"""
from __future__ import annotations

import importlib.util
import sqlite3
import sys
from pathlib import Path

import pytest

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

# ── Load migration module (filename has hyphens) ──────────────────────────────

MIGRATION_PATH = (
    PROJECT
    / "scripts"
    / "migrations"
    / "2026-04-29-add-errors-remediation-tables.py"
)


def _load_migration():
    """Import the migration module via importlib (hyphenated filename)."""
    spec = importlib.util.spec_from_file_location("migration_errors", MIGRATION_PATH)
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


# Cache to avoid re-loading per test
_MIGRATION_MOD = None


def _migration():
    global _MIGRATION_MOD
    if _MIGRATION_MOD is None:
        _MIGRATION_MOD = _load_migration()
    return _MIGRATION_MOD


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def db_path(tmp_path) -> Path:
    """Path to a fresh SQLite file for each test."""
    return tmp_path / "test_errors_remediation.db"


@pytest.fixture
def applied_db(db_path) -> str:
    """Fresh DB with the errors-remediation migration already applied."""
    mod = _migration()
    ret = mod._run(apply=True, db_path=db_path)
    assert ret == 0, f"Migration _run returned {ret}"
    return str(db_path)


def _conn(db_path_str: str) -> sqlite3.Connection:
    """Open a WAL+FK connection for constraint testing."""
    conn = sqlite3.connect(db_path_str)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _insert_error(conn: sqlite3.Connection, fingerprint: str = "abc123def456abcd") -> int:
    """Insert a minimal valid errors row. Returns new id."""
    cur = conn.execute(
        """
        INSERT INTO errors
            (fingerprint, first_seen_ts, last_seen_ts, ts, source, level, message)
        VALUES (?, datetime('now'), datetime('now'), datetime('now'),
                'python_logger', 'ERROR', 'test error message')
        """,
        (fingerprint,),
    )
    conn.commit()
    return cur.lastrowid


def _insert_fix_attempt(conn: sqlite3.Connection, error_id: int) -> int:
    """Insert a minimal valid fix_attempts row. Returns new id."""
    cur = conn.execute(
        """
        INSERT INTO fix_attempts (error_id, fingerprint, started_ts, classification)
        VALUES (?, 'abc123def456abcd', datetime('now'), 'AUTO_FIX')
        """,
        (error_id,),
    )
    conn.commit()
    return cur.lastrowid


# ══════════════════════════════════════════════════════════════════════════════
# 1. Tables created
# ══════════════════════════════════════════════════════════════════════════════

class TestTablesCreated:
    def test_all_three_tables_exist(self, applied_db):
        """All 3 tables are present after migration."""
        conn = _conn(applied_db)
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        conn.close()
        assert "errors" in tables
        assert "fix_attempts" in tables
        assert "fix_audit_log" in tables

    def test_all_tables_visible_via_sqlite_master(self, applied_db):
        """SELECT name FROM sqlite_master WHERE type='table' returns all 3."""
        conn = _conn(applied_db)
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        conn.close()
        missing = {"errors", "fix_attempts", "fix_audit_log"} - tables
        assert not missing, f"Tables missing from sqlite_master: {missing}"


# ══════════════════════════════════════════════════════════════════════════════
# 2. All 13 indexes created
# ══════════════════════════════════════════════════════════════════════════════

class TestIndexesCreated:
    EXPECTED_INDEXES = [
        "idx_errors_fingerprint",
        "idx_errors_classification",
        "idx_errors_last_seen",
        "idx_errors_service",
        "idx_errors_source",
        "idx_errors_severity_pend",
        "idx_fix_attempts_status",
        "idx_fix_attempts_fingerprint",
        "idx_fix_attempts_error_id",
        "idx_audit_attempt_id",
        "idx_audit_error_id",
        "idx_audit_ts",
        "idx_audit_phase",
    ]

    def test_all_13_indexes_exist(self, applied_db):
        """All 13 expected indexes are present in sqlite_master."""
        conn = _conn(applied_db)
        existing = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
        ).fetchall()}
        conn.close()
        missing = [idx for idx in self.EXPECTED_INDEXES if idx not in existing]
        assert not missing, f"Indexes missing: {missing}"

    def test_index_count_is_exactly_13(self, applied_db):
        """Exactly 13 named (non-auto) indexes exist on the 3 tables."""
        conn = _conn(applied_db)
        count = conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='index' AND name LIKE 'idx_%'"
        ).fetchone()[0]
        conn.close()
        assert count == 13, f"Expected 13 indexes, found {count}"


# ══════════════════════════════════════════════════════════════════════════════
# 3. Both triggers created
# ══════════════════════════════════════════════════════════════════════════════

class TestTriggersCreated:
    def test_both_triggers_exist(self, applied_db):
        """fix_audit_log_no_update and fix_audit_log_no_delete triggers exist."""
        conn = _conn(applied_db)
        triggers = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger'"
        ).fetchall()}
        conn.close()
        assert "fix_audit_log_no_update" in triggers
        assert "fix_audit_log_no_delete" in triggers


# ══════════════════════════════════════════════════════════════════════════════
# 4. Idempotency
# ══════════════════════════════════════════════════════════════════════════════

class TestIdempotency:
    def test_schema_idempotent_run_twice(self, db_path):
        """Running migration twice on empty DB returns 0 both times."""
        mod = _migration()
        assert mod._run(apply=True, db_path=db_path) == 0
        assert mod._run(apply=True, db_path=db_path) == 0

    def test_idempotent_with_existing_data(self, applied_db, db_path):
        """Re-running --apply on a populated DB does not lose rows."""
        conn = _conn(applied_db)
        _insert_error(conn, fingerprint="unique_fp_idempotent")
        conn.close()

        # Re-run migration on same DB
        mod = _migration()
        ret = mod._run(apply=True, db_path=Path(applied_db))
        assert ret == 0

        conn = _conn(applied_db)
        count = conn.execute("SELECT COUNT(*) FROM errors").fetchone()[0]
        conn.close()
        assert count == 1, "Row was lost during second migration run"


# ══════════════════════════════════════════════════════════════════════════════
# 5–11. CHECK constraints on errors table
# ══════════════════════════════════════════════════════════════════════════════

class TestErrorsCheckConstraints:
    def _base_insert(self, conn, **overrides):
        """Build a valid INSERT, apply overrides, execute."""
        vals = {
            "fingerprint": "fp_check_test_001",
            "first_seen_ts": "datetime('now')",
            "last_seen_ts":  "datetime('now')",
            "ts":            "datetime('now')",
            "source":        "'python_logger'",
            "level":         "'ERROR'",
            "message":       "'test'",
        }
        vals.update({k: f"'{v}'" if isinstance(v, str) else str(v)
                     for k, v in overrides.items()})
        cols = ", ".join(vals.keys())
        placeholders = ", ".join(vals.values())
        conn.execute(f"INSERT INTO errors ({cols}) VALUES ({placeholders})")

    def test_source_rejects_invalid(self, applied_db):
        """errors.source CHECK rejects 'invalid_source'."""
        conn = _conn(applied_db)
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO errors (fingerprint, first_seen_ts, last_seen_ts, ts, "
                "source, level, message) VALUES (?,datetime('now'),datetime('now'),"
                "datetime('now'),?,?,'test')",
                ("fp_src", "invalid_source", "ERROR"),
            )
        conn.close()

    def test_level_rejects_invalid(self, applied_db):
        """errors.level CHECK rejects 'DEBUG'."""
        conn = _conn(applied_db)
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO errors (fingerprint, first_seen_ts, last_seen_ts, ts, "
                "source, level, message) VALUES (?,datetime('now'),datetime('now'),"
                "datetime('now'),?,?,?)",
                ("fp_lvl", "python_logger", "DEBUG", "test"),
            )
        conn.close()

    def test_classification_rejects_invalid(self, applied_db):
        """errors.classification CHECK rejects 'BOGUS'."""
        conn = _conn(applied_db)
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO errors (fingerprint, first_seen_ts, last_seen_ts, ts, "
                "source, level, message, classification) "
                "VALUES (?,datetime('now'),datetime('now'),datetime('now'),"
                "?,?,?,?)",
                ("fp_cls", "python_logger", "ERROR", "test", "BOGUS"),
            )
        conn.close()

    def test_market_hours_rejects_invalid(self, applied_db):
        """errors.market_hours CHECK rejects value 2."""
        conn = _conn(applied_db)
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO errors (fingerprint, first_seen_ts, last_seen_ts, ts, "
                "source, level, message, market_hours) "
                "VALUES (?,datetime('now'),datetime('now'),datetime('now'),"
                "?,?,?,?)",
                ("fp_mh", "python_logger", "ERROR", "test", 2),
            )
        conn.close()

    def test_halt_active_rejects_invalid(self, applied_db):
        """errors.halt_active CHECK rejects value 5."""
        conn = _conn(applied_db)
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO errors (fingerprint, first_seen_ts, last_seen_ts, ts, "
                "source, level, message, halt_active) "
                "VALUES (?,datetime('now'),datetime('now'),datetime('now'),"
                "?,?,?,?)",
                ("fp_ha", "python_logger", "ERROR", "test", 5),
            )
        conn.close()

    def test_tier_rejects_invalid(self, applied_db):
        """errors.tier CHECK rejects value 3 (only 0,1,2,99 allowed)."""
        conn = _conn(applied_db)
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO errors (fingerprint, first_seen_ts, last_seen_ts, ts, "
                "source, level, message, tier) "
                "VALUES (?,datetime('now'),datetime('now'),datetime('now'),"
                "?,?,?,?)",
                ("fp_tier", "python_logger", "ERROR", "test", 3),
            )
        conn.close()

    def test_remediation_status_rejects_invalid(self, applied_db):
        """errors.remediation_status CHECK rejects 'PENDING'."""
        conn = _conn(applied_db)
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO errors (fingerprint, first_seen_ts, last_seen_ts, ts, "
                "source, level, message, remediation_status) "
                "VALUES (?,datetime('now'),datetime('now'),datetime('now'),"
                "?,?,?,?)",
                ("fp_rs", "python_logger", "ERROR", "test", "PENDING"),
            )
        conn.close()


# ══════════════════════════════════════════════════════════════════════════════
# 12. UNIQUE constraint on errors.fingerprint
# ══════════════════════════════════════════════════════════════════════════════

class TestErrorsUniqueFingerprint:
    def test_duplicate_fingerprint_rejected(self, applied_db):
        """Inserting two rows with the same fingerprint raises IntegrityError."""
        conn = _conn(applied_db)
        conn.execute(
            "INSERT INTO errors (fingerprint, first_seen_ts, last_seen_ts, ts, "
            "source, level, message) VALUES (?,datetime('now'),datetime('now'),"
            "datetime('now'),?,?,?)",
            ("dup_fp", "python_logger", "ERROR", "first"),
        )
        conn.commit()
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO errors (fingerprint, first_seen_ts, last_seen_ts, ts, "
                "source, level, message) VALUES (?,datetime('now'),datetime('now'),"
                "datetime('now'),?,?,?)",
                ("dup_fp", "python_logger", "ERROR", "second"),
            )
        conn.close()


# ══════════════════════════════════════════════════════════════════════════════
# 13–17. CHECK constraints on fix_attempts
# ══════════════════════════════════════════════════════════════════════════════

class TestFixAttemptsCheckConstraints:
    def _make_error(self, applied_db: str) -> tuple[sqlite3.Connection, int]:
        conn = _conn(applied_db)
        eid = _insert_error(conn, fingerprint="fa_check_fp")
        return conn, eid

    def test_status_rejects_invalid(self, applied_db):
        """fix_attempts.status CHECK rejects 'sleeping'."""
        conn, eid = self._make_error(applied_db)
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO fix_attempts (error_id, fingerprint, started_ts, "
                "classification, status) VALUES (?,?,datetime('now'),?,?)",
                (eid, "fa_check_fp", "AUTO_FIX", "sleeping"),
            )
        conn.close()

    def test_classification_rejects_invalid(self, applied_db):
        """fix_attempts.classification CHECK rejects 'MAYBE'."""
        conn, eid = self._make_error(applied_db)
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO fix_attempts (error_id, fingerprint, started_ts, "
                "classification) VALUES (?,?,datetime('now'),?)",
                (eid, "fa_check_fp", "MAYBE"),
            )
        conn.close()

    def test_review_confidence_rejects_negative(self, applied_db):
        """fix_attempts.review_confidence CHECK rejects -0.1."""
        conn, eid = self._make_error(applied_db)
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO fix_attempts (error_id, fingerprint, started_ts, "
                "classification, review_confidence) VALUES (?,?,datetime('now'),?,?)",
                (eid, "fa_check_fp", "AUTO_FIX", -0.1),
            )
        conn.close()

    def test_review_confidence_rejects_above_one(self, applied_db):
        """fix_attempts.review_confidence CHECK rejects 1.01."""
        conn, eid = self._make_error(applied_db)
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO fix_attempts (error_id, fingerprint, started_ts, "
                "classification, review_confidence) VALUES (?,?,datetime('now'),?,?)",
                (eid, "fa_check_fp", "AUTO_FIX", 1.01),
            )
        conn.close()

    def test_review_verdict_allows_null(self, applied_db):
        """fix_attempts.review_verdict = NULL is allowed (not yet reviewed)."""
        conn, eid = self._make_error(applied_db)
        conn.execute(
            "INSERT INTO fix_attempts (error_id, fingerprint, started_ts, "
            "classification, review_verdict) VALUES (?,?,datetime('now'),?,NULL)",
            (eid, "fa_check_fp", "AUTO_FIX"),
        )
        conn.commit()
        conn.close()

    def test_review_verdict_rejects_invalid(self, applied_db):
        """fix_attempts.review_verdict CHECK rejects 'MAYBE'."""
        conn, eid = self._make_error(applied_db)
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO fix_attempts (error_id, fingerprint, started_ts, "
                "classification, review_verdict) VALUES (?,?,datetime('now'),?,?)",
                (eid, "fa_check_fp", "AUTO_FIX", "MAYBE"),
            )
        conn.close()


# ══════════════════════════════════════════════════════════════════════════════
# 18–19. CHECK constraints on fix_audit_log
# ══════════════════════════════════════════════════════════════════════════════

class TestFixAuditLogCheckConstraints:
    def test_phase_rejects_invalid(self, applied_db):
        """fix_audit_log.phase CHECK rejects 'sleeping'."""
        conn = _conn(applied_db)
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO fix_audit_log (phase, actor) VALUES (?,?)",
                ("sleeping", "classifier"),
            )
        conn.close()

    def test_result_status_allows_null(self, applied_db):
        """fix_audit_log.result_status = NULL is permitted."""
        conn = _conn(applied_db)
        conn.execute(
            "INSERT INTO fix_audit_log (phase, actor, result_status) VALUES (?,?,NULL)",
            ("triage", "classifier"),
        )
        conn.commit()
        conn.close()


# ══════════════════════════════════════════════════════════════════════════════
# 20–22. fix_audit_log immutability (INSERT OK, UPDATE/DELETE blocked)
# ══════════════════════════════════════════════════════════════════════════════

class TestAuditLogImmutability:
    def _insert_probe(self, applied_db: str) -> tuple[sqlite3.Connection, int]:
        """Insert a probe row and return (conn, probe_id)."""
        conn = _conn(applied_db)
        cur = conn.execute(
            "INSERT INTO fix_audit_log (phase, actor) VALUES (?,?)",
            ("manual", "test_probe"),
        )
        conn.commit()
        return conn, cur.lastrowid

    def test_insert_succeeds(self, applied_db):
        """fix_audit_log INSERT is allowed (count increases by 1)."""
        conn = _conn(applied_db)
        before = conn.execute("SELECT COUNT(*) FROM fix_audit_log").fetchone()[0]
        conn.execute(
            "INSERT INTO fix_audit_log (phase, actor) VALUES (?,?)",
            ("triage", "classifier"),
        )
        conn.commit()
        after = conn.execute("SELECT COUNT(*) FROM fix_audit_log").fetchone()[0]
        conn.close()
        assert after == before + 1, f"Expected count to increase by 1: {before} -> {after}"

    def test_update_blocked_by_trigger(self, applied_db):
        """fix_audit_log UPDATE raises sqlite3.IntegrityError (immutable trigger)."""
        conn, probe_id = self._insert_probe(applied_db)
        with pytest.raises(sqlite3.IntegrityError) as exc_info:
            conn.execute(
                "UPDATE fix_audit_log SET actor='tampered' WHERE id=?",
                (probe_id,),
            )
        assert "immutable" in str(exc_info.value).lower()
        conn.close()

    def test_delete_blocked_by_trigger(self, applied_db):
        """fix_audit_log DELETE raises sqlite3.IntegrityError (immutable trigger)."""
        conn, probe_id = self._insert_probe(applied_db)
        with pytest.raises(sqlite3.IntegrityError) as exc_info:
            conn.execute(
                "DELETE FROM fix_audit_log WHERE id=?",
                (probe_id,),
            )
        assert "immutable" in str(exc_info.value).lower()
        conn.close()


# ══════════════════════════════════════════════════════════════════════════════
# 23–24. Foreign key constraints
# ══════════════════════════════════════════════════════════════════════════════

class TestForeignKeys:
    def test_fix_attempts_error_id_fk_enforced(self, applied_db):
        """fix_attempts.error_id → errors.id: bad error_id raises IntegrityError."""
        conn = _conn(applied_db)
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO fix_attempts (error_id, fingerprint, started_ts, "
                "classification) VALUES (?,?,datetime('now'),?)",
                (99999, "fp_fk_test", "AUTO_FIX"),  # error_id 99999 does not exist
            )
        conn.close()

    def test_fix_audit_log_attempt_id_fk_enforced(self, applied_db):
        """fix_audit_log.attempt_id → fix_attempts.id: bad attempt_id raises IntegrityError."""
        conn = _conn(applied_db)
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO fix_audit_log (attempt_id, phase, actor) VALUES (?,?,?)",
                (99999, "triage", "classifier"),  # attempt_id 99999 does not exist
            )
        conn.close()


# ══════════════════════════════════════════════════════════════════════════════
# 26. Backup file created on --apply
# ══════════════════════════════════════════════════════════════════════════════

class TestBackup:
    def test_backup_file_created_on_apply(self, tmp_path):
        """--apply creates a backup file named atlas.db.backup-errors-remediation-*."""
        db_path = tmp_path / "atlas.db"
        # Create empty DB first
        sqlite3.connect(str(db_path)).close()

        mod = _migration()
        ret = mod._run(apply=True, db_path=db_path)
        assert ret == 0

        backups = list(tmp_path.glob("atlas.db.backup-errors-remediation-*"))
        assert len(backups) >= 1, (
            f"Expected backup file, found none in {tmp_path}. "
            f"Files present: {list(tmp_path.iterdir())}"
        )
        # Backup file must exist (size may be 0 for a freshly-created source DB)
        assert backups[0].exists()

    def test_backup_not_created_on_dry_run(self, tmp_path):
        """Dry-run does NOT create a backup file."""
        db_path = tmp_path / "atlas.db"
        sqlite3.connect(str(db_path)).close()

        mod = _migration()
        ret = mod._run(apply=False, db_path=db_path)
        assert ret == 0

        backups = list(tmp_path.glob("atlas.db.backup-errors-remediation-*"))
        assert len(backups) == 0, f"Backup unexpectedly created on dry-run: {backups}"
