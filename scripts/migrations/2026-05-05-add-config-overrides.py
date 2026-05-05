#!/usr/bin/env python3
"""
Migration: 2026-05-05-add-config-overrides.py

Creates 2 tables for the dashboard universe/strategy toggle system:

  config_overrides      — Active DB-resident overrides layered on top of
                          config/active/*.json at read-time. One active row per
                          (scope, key); historical rows kept with active=0.
  config_override_audit — Immutable append-only audit log of every override
                          mutation event. Protected by two triggers that block
                          UPDATE and DELETE (mirrors fix_audit_log pattern).

Total objects created:
  • 2 tables
  • 5 indexes  (3 on config_overrides, 2 on config_override_audit)
  • 2 triggers  (config_override_audit_no_update, config_override_audit_no_delete)
  • 1 schema_version row (version 29)

Usage:
    python3 scripts/migrations/2026-05-05-add-config-overrides.py          # dry-run
    python3 scripts/migrations/2026-05-05-add-config-overrides.py --apply
    python3 scripts/migrations/2026-05-05-add-config-overrides.py --apply --db /tmp/test.db
"""
from __future__ import annotations

import argparse
import logging
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ATLAS_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ATLAS_ROOT))

from db.atlas_db import get_db  # noqa: E402
from utils.logging_config import setup_logging  # noqa: E402

DB_PATH = ATLAS_ROOT / "data" / "atlas.db"

logger = logging.getLogger(__name__)

# ── DDL: tables ───────────────────────────────────────────────────────────────

CREATE_CONFIG_OVERRIDES_SQL = """\
CREATE TABLE IF NOT EXISTS config_overrides (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  scope        TEXT    NOT NULL CHECK(scope IN ('universe','strategy')),
  key          TEXT    NOT NULL,
  state        TEXT    NOT NULL,
  reason       TEXT,
  created_by   TEXT    NOT NULL,
  created_at   TEXT    NOT NULL DEFAULT (datetime('now')),
  expires_at   TEXT,
  prev_state   TEXT,
  active       INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
  ended_at     TEXT,
  ended_reason TEXT CHECK(ended_reason IN ('reverted','expired','superseded') OR ended_reason IS NULL)
)"""

CREATE_CONFIG_OVERRIDE_AUDIT_SQL = """\
CREATE TABLE IF NOT EXISTS config_override_audit (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  ts           TEXT NOT NULL DEFAULT (datetime('now')),
  override_id  INTEGER REFERENCES config_overrides(id),
  scope        TEXT NOT NULL,
  key          TEXT NOT NULL,
  action       TEXT NOT NULL CHECK(action IN ('create','revert','expire','supersede')),
  from_state   TEXT,
  to_state     TEXT,
  reason       TEXT,
  actor        TEXT NOT NULL,
  source       TEXT NOT NULL CHECK(source IN ('dashboard','cli','telegram','sweep')),
  remote_ip    TEXT,
  payload_json TEXT
)"""

# ── DDL: indexes ──────────────────────────────────────────────────────────────

# config_overrides: 3 indexes
IDX_CONFIG_OVERRIDES_ACTIVE = (
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_config_overrides_active "
    "ON config_overrides(scope, key) WHERE active = 1"
)
IDX_CONFIG_OVERRIDES_EXPIRES = (
    "CREATE INDEX IF NOT EXISTS idx_config_overrides_expires "
    "ON config_overrides(expires_at) WHERE active = 1 AND expires_at IS NOT NULL"
)
IDX_CONFIG_OVERRIDES_LOOKUP = (
    "CREATE INDEX IF NOT EXISTS idx_config_overrides_lookup "
    "ON config_overrides(scope, key, active)"
)

# config_override_audit: 2 indexes
IDX_AUDIT_TS = (
    "CREATE INDEX IF NOT EXISTS idx_config_override_audit_ts "
    "ON config_override_audit(ts DESC)"
)
IDX_AUDIT_KEY = (
    "CREATE INDEX IF NOT EXISTS idx_config_override_audit_key "
    "ON config_override_audit(scope, key, ts DESC)"
)

# ── DDL: triggers ─────────────────────────────────────────────────────────────

CREATE_TRIGGER_NO_UPDATE = """\
CREATE TRIGGER IF NOT EXISTS config_override_audit_no_update
  BEFORE UPDATE ON config_override_audit
  BEGIN SELECT RAISE(ABORT, 'config_override_audit is immutable (append-only)'); END"""

CREATE_TRIGGER_NO_DELETE = """\
CREATE TRIGGER IF NOT EXISTS config_override_audit_no_delete
  BEFORE DELETE ON config_override_audit
  BEGIN SELECT RAISE(ABORT, 'config_override_audit is immutable (append-only)'); END"""

# ── DDL: schema_version bump ──────────────────────────────────────────────────

SCHEMA_VERSION_SQL = (
    "INSERT OR IGNORE INTO schema_version (version, applied_at) "
    "VALUES (29, datetime('now'))"
)

# ── Ordered DDL: (label, sql) ─────────────────────────────────────────────────

ALL_DDL: list[tuple[str, str]] = [
    ("Create config_overrides table",                   CREATE_CONFIG_OVERRIDES_SQL),
    ("Create config_override_audit table",              CREATE_CONFIG_OVERRIDE_AUDIT_SQL),
    ("Index uq_config_overrides_active",                IDX_CONFIG_OVERRIDES_ACTIVE),
    ("Index idx_config_overrides_expires",              IDX_CONFIG_OVERRIDES_EXPIRES),
    ("Index idx_config_overrides_lookup",               IDX_CONFIG_OVERRIDES_LOOKUP),
    ("Index idx_config_override_audit_ts",              IDX_AUDIT_TS),
    ("Index idx_config_override_audit_key",             IDX_AUDIT_KEY),
    ("Trigger config_override_audit_no_update",         CREATE_TRIGGER_NO_UPDATE),
    ("Trigger config_override_audit_no_delete",         CREATE_TRIGGER_NO_DELETE),
    ("Bump schema_version to 29",                       SCHEMA_VERSION_SQL),
]

# ── Expected objects (for post-apply verification) ────────────────────────────

EXPECTED_TABLES: list[str] = [
    "config_overrides",
    "config_override_audit",
]

EXPECTED_INDEXES: list[str] = [
    "uq_config_overrides_active",
    "idx_config_overrides_expires",
    "idx_config_overrides_lookup",
    "idx_config_override_audit_ts",
    "idx_config_override_audit_key",
]

EXPECTED_TRIGGERS: list[str] = [
    "config_override_audit_no_update",
    "config_override_audit_no_delete",
]


# ── Helpers ───────────────────────────────────────────────────────────────────

def _backup_db(db_path: Path) -> Path:
    """Copy db_path to a timestamped backup alongside the original."""
    iso = datetime.now().strftime("%Y-%m-%dT%H%M%S")
    backup_name = f"{db_path.name}.backup-config-overrides-{iso}"
    backup_path = db_path.parent / backup_name
    shutil.copy2(str(db_path), str(backup_path))
    return backup_path


def _verify(db_path: Path) -> tuple[bool, str]:
    """Verify all expected tables, indexes, triggers, and schema_version exist.

    Returns (ok: bool, report: str).
    """
    lines: list[str] = []
    ok = True

    with get_db(str(db_path)) as conn:
        existing_tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        existing_indexes = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            ).fetchall()
        }
        existing_triggers = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger'"
            ).fetchall()
        }
        version_rows = conn.execute(
            "SELECT version FROM schema_version WHERE version >= 29 LIMIT 1"
        ).fetchall()

    lines.append("\n=== Tables ===")
    for t in EXPECTED_TABLES:
        found = t in existing_tables
        lines.append(f"  {'✓' if found else '✗ MISSING'}  {t}")
        if not found:
            ok = False

    lines.append(f"\n=== Indexes ({len(EXPECTED_INDEXES)} expected) ===")
    found_count = 0
    for idx in EXPECTED_INDEXES:
        found = idx in existing_indexes
        lines.append(f"  {'✓' if found else '✗ MISSING'}  {idx}")
        if found:
            found_count += 1
        else:
            ok = False
    lines.append(f"  Total: {found_count}/{len(EXPECTED_INDEXES)}")

    lines.append(f"\n=== Triggers ({len(EXPECTED_TRIGGERS)} expected) ===")
    for trig in EXPECTED_TRIGGERS:
        found = trig in existing_triggers
        lines.append(f"  {'✓' if found else '✗ MISSING'}  {trig}")
        if not found:
            ok = False

    lines.append("\n=== schema_version ===")
    if version_rows:
        lines.append(f"  ✓  version >= 29 found: {version_rows[0][0]}")
    else:
        lines.append("  ✗ MISSING  version >= 29 not found")
        ok = False

    return ok, "\n".join(lines)


def _test_trigger_enforcement(db_path: Path) -> tuple[bool, str]:
    """Functional test: insert a probe row into config_override_audit, then
    confirm UPDATE and DELETE are both blocked by the immutability triggers.

    Uses a raw sqlite3 connection (not get_db) so transaction control
    is explicit and trigger semantics are clear.

    Returns (ok: bool, report: str).
    """
    lines: list[str] = []
    ok = True

    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        # Insert probe row — override_id is nullable, no FK needed.
        conn.execute(
            "INSERT INTO config_override_audit "
            "(scope, key, action, actor, source) "
            "VALUES ('universe', 'test', 'create', 'migration_verify', 'cli')"
        )
        conn.commit()
        probe_id = conn.execute(
            "SELECT id FROM config_override_audit WHERE actor='migration_verify' "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()[0]

        # ── UPDATE must be blocked ────────────────────────────────────────
        update_blocked = False
        update_msg = ""
        try:
            conn.execute(
                "UPDATE config_override_audit SET actor='tampered' WHERE id=?",
                (probe_id,),
            )
            conn.commit()
        except sqlite3.IntegrityError as exc:
            update_blocked = True
            update_msg = str(exc)
        except Exception as exc:
            update_msg = f"{type(exc).__name__}: {exc}"

        if update_blocked:
            lines.append(f"  ✓  UPDATE blocked by trigger: {update_msg}")
        else:
            lines.append("  ✗  UPDATE NOT blocked — trigger missing or broken!")
            ok = False

        # ── DELETE must be blocked ────────────────────────────────────────
        delete_blocked = False
        delete_msg = ""
        try:
            conn.execute(
                "DELETE FROM config_override_audit WHERE id=?", (probe_id,)
            )
            conn.commit()
        except sqlite3.IntegrityError as exc:
            delete_blocked = True
            delete_msg = str(exc)
        except Exception as exc:
            delete_msg = f"{type(exc).__name__}: {exc}"

        if delete_blocked:
            lines.append(f"  ✓  DELETE blocked by trigger: {delete_msg}")
        else:
            lines.append("  ✗  DELETE NOT blocked — trigger missing or broken!")
            ok = False

    finally:
        conn.close()

    return ok, "\n".join(lines)


# ── Core logic ────────────────────────────────────────────────────────────────

def _run(apply: bool, db_path: Path) -> int:
    """Execute or dry-run the migration.

    Parameters
    ----------
    apply   : True = execute DDL; False = print DDL only (dry-run).
    db_path : Path to the SQLite database file.

    Returns
    -------
    Exit code: 0 = success/dry-run-ok, 1 = error.
    """
    if not db_path.exists():
        if apply:
            logger.info("DB not found at %s — creating new file.", db_path)
            db_path.parent.mkdir(parents=True, exist_ok=True)
            sqlite3.connect(str(db_path)).close()
        else:
            logger.error("DB not found at %s", db_path)
            return 1

    mode_label = "APPLY" if apply else "DRY-RUN"
    print(f"Migration: 2026-05-05-add-config-overrides")
    print(f"Mode:      {mode_label}")
    print(f"DB:        {db_path}")
    print(
        f"Objects:   {len(EXPECTED_TABLES)} tables, "
        f"{len(EXPECTED_INDEXES)} indexes, "
        f"{len(EXPECTED_TRIGGERS)} triggers, "
        f"schema_version=29"
    )

    # ── Backup ────────────────────────────────────────────────────────────
    if apply:
        try:
            backup_path = _backup_db(db_path)
            logger.info("Backup: %s", backup_path)
            print(f"\nBackup:    {backup_path}")
        except Exception as exc:
            logger.error("Failed to create backup: %s", exc)
            return 1

    # ── Dry-run: print SQL and exit ───────────────────────────────────────
    if not apply:
        print("\n=== DDL (dry-run — not applied) ===")
        for label, sql in ALL_DDL:
            print(f"\n-- {label}")
            print(sql + ";")
        print("\n--- Dry-run complete. Pass --apply to execute.")
        return 0

    # ── Apply DDL ─────────────────────────────────────────────────────────
    print("\n=== Applying DDL ===")
    try:
        with get_db(str(db_path)) as conn:
            for label, sql in ALL_DDL:
                print(f"  {label} ...")
                conn.execute(sql)
        logger.info("All DDL statements executed and committed.")
    except Exception as exc:
        logger.error("Migration FAILED during DDL apply: %s", exc)
        return 1

    # ── Verify schema objects ─────────────────────────────────────────────
    print("\n=== Verification: schema objects ===")
    schema_ok, schema_report = _verify(db_path)
    print(schema_report)

    # ── Verify trigger enforcement ────────────────────────────────────────
    print("\n=== Verification: trigger enforcement ===")
    trigger_ok, trigger_report = _test_trigger_enforcement(db_path)
    print(trigger_report)

    if schema_ok and trigger_ok:
        print(
            "\n✅ Migration COMPLETE — "
            f"{len(EXPECTED_TABLES)} tables, "
            f"{len(EXPECTED_INDEXES)} indexes, "
            f"{len(EXPECTED_TRIGGERS)} triggers verified; "
            "immutability triggers confirmed; schema_version=29.\n"
        )
        logger.info(
            "Migration complete: %d tables, %d indexes, %d triggers, schema_version=29",
            len(EXPECTED_TABLES),
            len(EXPECTED_INDEXES),
            len(EXPECTED_TRIGGERS),
        )
        return 0
    else:
        print("\n❌ Migration INCOMPLETE — see verification failures above.\n")
        logger.error("Migration verification failed — see output above.")
        return 1


# ── Entry point ───────────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Create config_overrides + config_override_audit tables for "
            "the dashboard universe/strategy toggle system (idempotent)."
        )
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        default=False,
        help="Execute the migration (default: dry-run, prints SQL only).",
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=DB_PATH,
        help=f"Path to atlas.db (default: {DB_PATH})",
    )
    args = parser.parse_args(argv)
    setup_logging("migrate_config_overrides")
    return _run(apply=args.apply, db_path=args.db)


if __name__ == "__main__":
    sys.exit(main())
