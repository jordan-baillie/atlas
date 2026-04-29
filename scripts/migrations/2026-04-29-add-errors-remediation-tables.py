#!/usr/bin/env python3
"""
Migration: 2026-04-29-add-errors-remediation-tables.py

Creates 3 tables for the auto-error-remediation Phase 0 system:

  errors          — Dedup'd error stream (populated by SQLiteErrorWriter,
                    journald tailer, cron capture, healthcheck, etc.)
  fix_attempts    — State-machine row per fix attempt (one error → many attempts)
  fix_audit_log   — Immutable append-only audit trail protected by two triggers
                    that block UPDATE and DELETE.

Total objects created:
  • 3 tables
  • 13 indexes  (6 on errors, 3 on fix_attempts, 4 on fix_audit_log)
  • 2 triggers   (fix_audit_log_no_update, fix_audit_log_no_delete)

Usage:
    python3 scripts/migrations/2026-04-29-add-errors-remediation-tables.py          # dry-run
    python3 scripts/migrations/2026-04-29-add-errors-remediation-tables.py --apply
    python3 scripts/migrations/2026-04-29-add-errors-remediation-tables.py --apply --db /tmp/test.db
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

CREATE_ERRORS_SQL = """\
CREATE TABLE IF NOT EXISTS errors (
  id                   INTEGER PRIMARY KEY AUTOINCREMENT,
  fingerprint          TEXT    NOT NULL,
  first_seen_ts        TEXT    NOT NULL,
  last_seen_ts         TEXT    NOT NULL,
  occurrence_count     INTEGER NOT NULL DEFAULT 1,
  ts                   TEXT    NOT NULL,
  source               TEXT    NOT NULL CHECK(source IN ('python_logger','journald','cron','healthcheck','telegram_alert','manual','backfill')),
  service              TEXT,
  level                TEXT    NOT NULL CHECK(level IN ('WARNING','ERROR','CRITICAL')),
  logger_name          TEXT,
  message              TEXT    NOT NULL,
  exc_type             TEXT,
  exc_message          TEXT,
  traceback            TEXT,
  file_path            TEXT,
  line_number          INTEGER,
  function_name        TEXT,
  pid                  INTEGER,
  hostname             TEXT,
  context_json         TEXT,
  market_hours         INTEGER NOT NULL DEFAULT 0 CHECK(market_hours IN (0,1)),
  halt_active          INTEGER NOT NULL DEFAULT 0 CHECK(halt_active IN (0,1)),
  git_sha              TEXT,
  classification       TEXT    NOT NULL DEFAULT 'UNCLASSIFIED' CHECK(classification IN ('AUTO_FIX','ASSIST','ESCALATE','IGNORE','UNCLASSIFIED','ESCALATE_DEFERRED','IGNORE_PENDING_CLEAR')),
  triage_reason        TEXT,
  tier                 INTEGER NOT NULL DEFAULT 99 CHECK(tier IN (0,1,2,99)),
  remediation_status   TEXT    NOT NULL DEFAULT 'NEW' CHECK(remediation_status IN ('NEW','TRIAGED','IN_FLIGHT','FIXED','REVERTED','ESCALATED','IGNORED','SUPPRESSED')),
  remediation_attempts INTEGER NOT NULL DEFAULT 0,
  last_attempt_at      TEXT,
  fixed_by_attempt_id  INTEGER REFERENCES fix_attempts(id),
  resolved_at          TEXT,
  created_at           TEXT    NOT NULL DEFAULT (datetime('now'))
)"""

CREATE_FIX_ATTEMPTS_SQL = """\
CREATE TABLE IF NOT EXISTS fix_attempts (
  id                   INTEGER PRIMARY KEY AUTOINCREMENT,
  error_id             INTEGER NOT NULL REFERENCES errors(id),
  fingerprint          TEXT    NOT NULL,
  started_ts           TEXT    NOT NULL,
  finished_ts          TEXT,
  status               TEXT    NOT NULL DEFAULT 'triaged'
    CHECK(status IN ('triaged','reproducing','diagnosing','fixing','verifying','reviewing','merged','reverted','failed','escalated','blocked','aborted')),
  classification       TEXT    NOT NULL CHECK(classification IN ('AUTO_FIX','ASSIST','ESCALATE','IGNORE')),
  triage_model         TEXT,
  triage_reason        TEXT,
  triage_tokens        INTEGER,
  diagnosis_model      TEXT,
  diagnosis_summary    TEXT,
  diagnosis_tokens     INTEGER,
  fix_model            TEXT,
  fix_branch           TEXT,
  fix_commit_sha       TEXT,
  fix_diff_lines       INTEGER,
  fix_tokens           INTEGER,
  review_model         TEXT,
  review_verdict       TEXT CHECK(review_verdict IS NULL OR review_verdict IN ('APPROVE','REJECT')),
  review_confidence    REAL CHECK(review_confidence IS NULL OR (review_confidence >= 0.0 AND review_confidence <= 1.0)),
  review_reason        TEXT,
  review_tokens        INTEGER,
  test_results_json    TEXT,
  gates_passed_json    TEXT,
  gates_failed_json    TEXT,
  blocked_by_gate      TEXT,
  revert_commit_sha    TEXT,
  revert_reason        TEXT,
  reverted_ts          TEXT,
  monitor_outcome      TEXT CHECK(monitor_outcome IS NULL OR monitor_outcome IN ('clean','reverted','pending')),
  total_wall_seconds   REAL,
  notes                TEXT,
  CHECK(NOT (status IN ('merged','reverted','failed','escalated','blocked','aborted','triaged') AND finished_ts IS NULL AND status != 'triaged' AND monitor_outcome != 'pending'))
)"""

CREATE_FIX_AUDIT_LOG_SQL = """\
CREATE TABLE IF NOT EXISTS fix_audit_log (
  id                   INTEGER PRIMARY KEY AUTOINCREMENT,
  attempt_id           INTEGER REFERENCES fix_attempts(id),
  error_id             INTEGER REFERENCES errors(id),
  ts                   TEXT    NOT NULL DEFAULT (datetime('now')),
  phase                TEXT    NOT NULL
    CHECK(phase IN ('capture','triage','reproduce','diagnose','fix','verify','review','gate_check','merge','monitor','revert','halt','resume','config_change','graduation','demotion','manual')),
  actor                TEXT    NOT NULL,
  model                TEXT,
  decision             TEXT,
  reasoning            TEXT,
  diff                 TEXT,
  payload_json         TEXT,
  duration_sec         REAL,
  tokens_in            INTEGER,
  tokens_out           INTEGER,
  cost_usd             REAL DEFAULT 0,
  result_status        TEXT CHECK(result_status IS NULL OR result_status IN ('success','blocked','error','timeout','aborted')),
  blocked_by_gate      TEXT,
  notes                TEXT
)"""

# ── DDL: indexes ──────────────────────────────────────────────────────────────

# errors: 6 indexes
IDX_ERRORS_FINGERPRINT    = "CREATE UNIQUE INDEX IF NOT EXISTS idx_errors_fingerprint ON errors(fingerprint)"
IDX_ERRORS_CLASSIFICATION = "CREATE INDEX IF NOT EXISTS idx_errors_classification ON errors(classification, remediation_status)"
IDX_ERRORS_LAST_SEEN      = "CREATE INDEX IF NOT EXISTS idx_errors_last_seen ON errors(last_seen_ts)"
IDX_ERRORS_SERVICE        = "CREATE INDEX IF NOT EXISTS idx_errors_service ON errors(service)"
IDX_ERRORS_SOURCE         = "CREATE INDEX IF NOT EXISTS idx_errors_source ON errors(source, level)"
IDX_ERRORS_SEVERITY_PEND  = "CREATE INDEX IF NOT EXISTS idx_errors_severity_pend ON errors(classification, fixed_by_attempt_id) WHERE fixed_by_attempt_id IS NULL"

# fix_attempts: 3 indexes
IDX_FIX_ATTEMPTS_STATUS      = "CREATE INDEX IF NOT EXISTS idx_fix_attempts_status ON fix_attempts(status, started_ts)"
IDX_FIX_ATTEMPTS_FINGERPRINT = "CREATE INDEX IF NOT EXISTS idx_fix_attempts_fingerprint ON fix_attempts(fingerprint, started_ts)"
IDX_FIX_ATTEMPTS_ERROR_ID    = "CREATE INDEX IF NOT EXISTS idx_fix_attempts_error_id ON fix_attempts(error_id)"

# fix_audit_log: 4 indexes
IDX_AUDIT_ATTEMPT_ID = "CREATE INDEX IF NOT EXISTS idx_audit_attempt_id ON fix_audit_log(attempt_id)"
IDX_AUDIT_ERROR_ID   = "CREATE INDEX IF NOT EXISTS idx_audit_error_id ON fix_audit_log(error_id)"
IDX_AUDIT_TS         = "CREATE INDEX IF NOT EXISTS idx_audit_ts ON fix_audit_log(ts)"
IDX_AUDIT_PHASE      = "CREATE INDEX IF NOT EXISTS idx_audit_phase ON fix_audit_log(phase, actor)"

# ── DDL: triggers ─────────────────────────────────────────────────────────────

CREATE_TRIGGER_NO_UPDATE = """\
CREATE TRIGGER IF NOT EXISTS fix_audit_log_no_update
BEFORE UPDATE ON fix_audit_log
BEGIN
  SELECT RAISE(ABORT, 'fix_audit_log is immutable (append-only)');
END"""

CREATE_TRIGGER_NO_DELETE = """\
CREATE TRIGGER IF NOT EXISTS fix_audit_log_no_delete
BEFORE DELETE ON fix_audit_log
BEGIN
  SELECT RAISE(ABORT, 'fix_audit_log is immutable (append-only)');
END"""

# ── Ordered DDL: (label, sql) ─────────────────────────────────────────────────
# fix_attempts first so errors.fixed_by_attempt_id FK is cleaner at runtime.

ALL_DDL: list[tuple[str, str]] = [
    ("Create fix_attempts table",           CREATE_FIX_ATTEMPTS_SQL),
    ("Create errors table",                 CREATE_ERRORS_SQL),
    ("Create fix_audit_log table",          CREATE_FIX_AUDIT_LOG_SQL),
    # errors: 6 indexes
    ("Index idx_errors_fingerprint",        IDX_ERRORS_FINGERPRINT),
    ("Index idx_errors_classification",     IDX_ERRORS_CLASSIFICATION),
    ("Index idx_errors_last_seen",          IDX_ERRORS_LAST_SEEN),
    ("Index idx_errors_service",            IDX_ERRORS_SERVICE),
    ("Index idx_errors_source",             IDX_ERRORS_SOURCE),
    ("Index idx_errors_severity_pend",      IDX_ERRORS_SEVERITY_PEND),
    # fix_attempts: 3 indexes
    ("Index idx_fix_attempts_status",       IDX_FIX_ATTEMPTS_STATUS),
    ("Index idx_fix_attempts_fingerprint",  IDX_FIX_ATTEMPTS_FINGERPRINT),
    ("Index idx_fix_attempts_error_id",     IDX_FIX_ATTEMPTS_ERROR_ID),
    # fix_audit_log: 4 indexes
    ("Index idx_audit_attempt_id",          IDX_AUDIT_ATTEMPT_ID),
    ("Index idx_audit_error_id",            IDX_AUDIT_ERROR_ID),
    ("Index idx_audit_ts",                  IDX_AUDIT_TS),
    ("Index idx_audit_phase",               IDX_AUDIT_PHASE),
    # triggers: 2
    ("Trigger fix_audit_log_no_update",     CREATE_TRIGGER_NO_UPDATE),
    ("Trigger fix_audit_log_no_delete",     CREATE_TRIGGER_NO_DELETE),
]

# ── Expected objects (for post-apply verification) ────────────────────────────

EXPECTED_TABLES: list[str] = [
    "errors",
    "fix_attempts",
    "fix_audit_log",
]

EXPECTED_INDEXES: list[str] = [
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

EXPECTED_TRIGGERS: list[str] = [
    "fix_audit_log_no_update",
    "fix_audit_log_no_delete",
]


# ── Helpers ───────────────────────────────────────────────────────────────────

def _backup_db(db_path: Path) -> Path:
    """Copy db_path to a timestamped backup alongside the original."""
    iso = datetime.now().strftime("%Y-%m-%dT%H%M%S")
    backup_name = f"{db_path.name}.backup-errors-remediation-{iso}"
    backup_path = db_path.parent / backup_name
    shutil.copy2(str(db_path), str(backup_path))
    return backup_path


def _verify(db_path: Path) -> tuple[bool, str]:
    """
    Verify all expected tables, indexes, and triggers exist in sqlite_master.

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

    return ok, "\n".join(lines)


def _test_trigger_enforcement(db_path: Path) -> tuple[bool, str]:
    """
    Functional test: insert a probe row, then confirm UPDATE and DELETE
    are both blocked by IntegrityError from the immutability triggers.

    Uses a raw sqlite3 connection (not get_db) so transaction control
    is explicit and trigger semantics are clear.

    Returns (ok: bool, report: str).
    """
    lines: list[str] = []
    ok = True

    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        # Insert probe row — attempt_id/error_id are nullable, no FKs needed.
        conn.execute(
            "INSERT INTO fix_audit_log (attempt_id, error_id, phase, actor) "
            "VALUES (NULL, NULL, 'manual', 'migration_verify')"
        )
        conn.commit()
        probe_id = conn.execute(
            "SELECT id FROM fix_audit_log WHERE actor='migration_verify' "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()[0]

        # ── UPDATE must be blocked ────────────────────────────────────────
        update_blocked = False
        update_msg = ""
        try:
            conn.execute(
                "UPDATE fix_audit_log SET actor='tampered' WHERE id=?",
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
                "DELETE FROM fix_audit_log WHERE id=?", (probe_id,)
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
    """
    Execute or dry-run the migration.

    Parameters
    ----------
    apply   : True = execute DDL; False = print DDL only (dry-run).
    db_path : Path to the SQLite database file.

    Returns
    -------
    Exit code: 0 = success/dry-run-ok, 1 = error.
    """
    # If the DB doesn't exist and we're applying, create it.
    if not db_path.exists():
        if apply:
            logger.info("DB not found at %s — creating new file.", db_path)
            db_path.parent.mkdir(parents=True, exist_ok=True)
            sqlite3.connect(str(db_path)).close()
        else:
            logger.error("DB not found at %s", db_path)
            return 1

    mode_label = "APPLY" if apply else "DRY-RUN"
    print(f"Migration: 2026-04-29-add-errors-remediation-tables")
    print(f"Mode:      {mode_label}")
    print(f"DB:        {db_path}")
    print(
        f"Objects:   {len(EXPECTED_TABLES)} tables, "
        f"{len(EXPECTED_INDEXES)} indexes, "
        f"{len(EXPECTED_TRIGGERS)} triggers"
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
            "immutability triggers confirmed.\n"
        )
        logger.info(
            "Migration complete: %d tables, %d indexes, %d triggers",
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
            "Create errors, fix_attempts, fix_audit_log tables for "
            "the auto-error-remediation Phase 0 system (idempotent)."
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
    setup_logging("migrate_errors_remediation")
    return _run(apply=args.apply, db_path=args.db)


if __name__ == "__main__":
    sys.exit(main())
