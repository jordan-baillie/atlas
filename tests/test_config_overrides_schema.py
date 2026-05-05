"""Tests for config_overrides + config_override_audit schema (migration 2026-05-05).

Uses the autouse _isolate_prod_db fixture from conftest.py — each test gets
a fresh isolated DB with init_db() applied. The migration is run at the start
of each test against the isolated DB.
"""
from __future__ import annotations

import importlib.util
import sqlite3
import sys
from pathlib import Path

import pytest

# Ensure project root is on path
PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

MIGRATION_PATH = PROJECT / "scripts" / "migrations" / "2026-05-05-add-config-overrides.py"


def _load_migration():
    """Load the migration module by file path (name starts with digit — not importable normally)."""
    spec = importlib.util.spec_from_file_location("migration_config_overrides", MIGRATION_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _apply_migration(db_path: str) -> None:
    """Apply the config-overrides migration to a test DB path."""
    mod = _load_migration()
    rc = mod._run(apply=True, db_path=Path(db_path))
    assert rc == 0, f"Migration failed with exit code {rc}"


@pytest.fixture(autouse=True)
def _migration_applied():
    """Ensure migration is applied to the isolated test DB."""
    import db.atlas_db as _adb
    db_path = _adb._db_path_override or str(_adb.DB_PATH)
    _apply_migration(db_path)


# ── Tests ─────────────────────────────────────────────────────────────────────

def test_schema_version_29():
    """schema_version table must have a row >= 29."""
    from db.atlas_db import get_db
    with get_db() as conn:
        row = conn.execute(
            "SELECT MAX(version) AS v FROM schema_version"
        ).fetchone()
    assert row["v"] >= 29, f"schema_version={row['v']}, expected >= 29"


def test_tables_exist():
    """Both config_overrides and config_override_audit must be in sqlite_master."""
    from db.atlas_db import get_db
    with get_db() as conn:
        names = {
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
    assert "config_overrides" in names
    assert "config_override_audit" in names


def test_unique_active_index():
    """Inserting two active=1 rows for same (scope, key) must raise IntegrityError."""
    from db.atlas_db import get_db
    with get_db() as conn:
        conn.execute(
            "INSERT INTO config_overrides (scope, key, state, created_by) "
            "VALUES ('universe', 'sp500', 'passive', 'test')"
        )
    # Second row with active=1 for same scope+key must fail
    with pytest.raises(sqlite3.IntegrityError):
        with get_db() as conn:
            conn.execute(
                "INSERT INTO config_overrides (scope, key, state, created_by) "
                "VALUES ('universe', 'sp500', 'disabled', 'test')"
            )


def test_audit_no_update_trigger():
    """UPDATE on config_override_audit must raise IntegrityError."""
    import db.atlas_db as _adb
    db_path = _adb._db_path_override or str(_adb.DB_PATH)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO config_override_audit (scope, key, action, actor, source) "
            "VALUES ('universe', 'asx', 'create', 'test', 'cli')"
        )
        conn.commit()
        row_id = conn.execute(
            "SELECT id FROM config_override_audit ORDER BY id DESC LIMIT 1"
        ).fetchone()[0]

        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "UPDATE config_override_audit SET actor='tampered' WHERE id=?",
                (row_id,),
            )
            conn.commit()
    finally:
        conn.close()


def test_audit_no_delete_trigger():
    """DELETE on config_override_audit must raise IntegrityError."""
    import db.atlas_db as _adb
    db_path = _adb._db_path_override or str(_adb.DB_PATH)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO config_override_audit (scope, key, action, actor, source) "
            "VALUES ('universe', 'asx', 'create', 'test', 'cli')"
        )
        conn.commit()
        row_id = conn.execute(
            "SELECT id FROM config_override_audit ORDER BY id DESC LIMIT 1"
        ).fetchone()[0]

        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "DELETE FROM config_override_audit WHERE id=?", (row_id,)
            )
            conn.commit()
    finally:
        conn.close()


def test_check_constraints_scope():
    """Invalid scope value must be rejected by CHECK constraint."""
    from db.atlas_db import get_db
    with pytest.raises(sqlite3.IntegrityError):
        with get_db() as conn:
            conn.execute(
                "INSERT INTO config_overrides (scope, key, state, created_by) "
                "VALUES ('invalid_scope', 'sp500', 'live', 'test')"
            )


def test_check_constraints_action():
    """Invalid action value on audit table must be rejected by CHECK constraint."""
    from db.atlas_db import get_db
    with pytest.raises(sqlite3.IntegrityError):
        with get_db() as conn:
            conn.execute(
                "INSERT INTO config_override_audit (scope, key, action, actor, source) "
                "VALUES ('universe', 'sp500', 'invalid_action', 'test', 'cli')"
            )


def test_check_constraints_ended_reason():
    """Invalid ended_reason must be rejected by CHECK constraint."""
    from db.atlas_db import get_db
    with pytest.raises(sqlite3.IntegrityError):
        with get_db() as conn:
            conn.execute(
                "INSERT INTO config_overrides "
                "(scope, key, state, created_by, active, ended_reason) "
                "VALUES ('universe', 'sp500', 'passive', 'test', 0, 'invalid_reason')"
            )
