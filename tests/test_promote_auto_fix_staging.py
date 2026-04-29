"""Tests for scripts/promote_auto_fix_staging.py — 30-min monitoring window."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

# Import module under test
import scripts.promote_auto_fix_staging as promo
from scripts.promote_auto_fix_staging import (
    check_fingerprint_recurrence,
    check_healthchecks_clean,
    find_pending_promotions,
    main,
)


# ── Helpers ─────────────────────────────────────────────────────────────────


def _now_utc(offset_minutes: int = 0) -> str:
    return (
        datetime.now(timezone.utc) + timedelta(minutes=offset_minutes)
    ).strftime("%Y-%m-%dT%H:%M:%S")


def _create_test_db(path: str) -> None:
    """Create minimal schema needed for promote script tests."""
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        PRAGMA journal_mode=WAL;
        PRAGMA foreign_keys=ON;

        CREATE TABLE IF NOT EXISTS fix_attempts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            error_id INTEGER,
            fingerprint TEXT,
            fix_branch TEXT,
            fix_commit_sha TEXT,
            started_ts TEXT NOT NULL,
            finished_ts TEXT,
            status TEXT DEFAULT 'triaged',
            classification TEXT DEFAULT 'ASSIST',
            monitor_outcome TEXT DEFAULT 'pending',
            revert_commit_sha TEXT,
            revert_reason TEXT,
            reverted_ts TEXT,
            notes TEXT
        );

        CREATE TABLE IF NOT EXISTS errors (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fingerprint TEXT,
            first_seen_ts TEXT,
            last_seen_ts TEXT,
            source TEXT DEFAULT 'python_logger',
            level TEXT DEFAULT 'ERROR',
            message TEXT
        );

        CREATE TABLE IF NOT EXISTS fix_audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            attempt_id INTEGER,
            error_id INTEGER,
            ts TEXT DEFAULT (datetime('now')),
            phase TEXT NOT NULL,
            actor TEXT NOT NULL,
            decision TEXT,
            reasoning TEXT,
            result_status TEXT
        );
    """)
    conn.commit()
    conn.close()


def _insert_merged_attempt(
    path: str,
    fingerprint: str = "fp001aaaaaaaaaaaa",
    fix_commit_sha: str = "abc123sha456789a",
    started_offset_min: int = -35,  # 35 min ago = past the 30-min window
    monitor_outcome: str = "pending",
    status: str = "merged",
    classification: str = "AUTO_FIX",
    error_id: int = 1,
) -> int:
    started_ts = _now_utc(started_offset_min)
    conn = sqlite3.connect(path)
    cur = conn.execute(
        """INSERT INTO fix_attempts
           (error_id, fingerprint, fix_branch, fix_commit_sha, started_ts,
            status, classification, monitor_outcome)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (error_id, fingerprint, "auto-fix/test-001", fix_commit_sha,
         started_ts, status, classification, monitor_outcome),
    )
    attempt_id = cur.lastrowid
    conn.commit()
    conn.close()
    return attempt_id


def _insert_error(
    path: str,
    fingerprint: str,
    source: str = "python_logger",
    level: str = "ERROR",
    last_seen_offset_min: int = -10,
    first_seen_offset_min: int = -10,
) -> None:
    conn = sqlite3.connect(path)
    conn.execute(
        """INSERT INTO errors (fingerprint, first_seen_ts, last_seen_ts, source, level, message)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (
            fingerprint,
            _now_utc(first_seen_offset_min),
            _now_utc(last_seen_offset_min),
            source,
            level,
            f"error for {fingerprint}",
        ),
    )
    conn.commit()
    conn.close()


# ── find_pending_promotions tests ───────────────────────────────────────────


def test_find_pending_returns_nothing_when_window_not_elapsed(tmp_path):
    """A row merged only 5 min ago (within window) should NOT be returned."""
    db = str(tmp_path / "test.db")
    _create_test_db(db)
    _insert_merged_attempt(db, started_offset_min=-5)  # only 5 min ago

    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    rows = find_pending_promotions(conn)
    conn.close()
    assert rows == []


def test_find_pending_returns_rows_past_window(tmp_path):
    """A row merged 35 min ago should be returned."""
    db = str(tmp_path / "test.db")
    _create_test_db(db)
    _insert_merged_attempt(db, started_offset_min=-35)

    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    rows = find_pending_promotions(conn)
    conn.close()
    assert len(rows) == 1


def test_find_pending_excludes_clean(tmp_path):
    """monitor_outcome='clean' rows should not be returned."""
    db = str(tmp_path / "test.db")
    _create_test_db(db)
    _insert_merged_attempt(db, started_offset_min=-35, monitor_outcome="clean")

    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    rows = find_pending_promotions(conn)
    conn.close()
    assert rows == []


def test_find_pending_excludes_reverted(tmp_path):
    """monitor_outcome='reverted' rows should not be returned."""
    db = str(tmp_path / "test.db")
    _create_test_db(db)
    _insert_merged_attempt(db, started_offset_min=-35, monitor_outcome="reverted")

    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    rows = find_pending_promotions(conn)
    conn.close()
    assert rows == []


def test_find_pending_excludes_non_auto_fix(tmp_path):
    """classification != AUTO_FIX should not be returned."""
    db = str(tmp_path / "test.db")
    _create_test_db(db)
    _insert_merged_attempt(db, started_offset_min=-35, classification="ASSIST")

    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    rows = find_pending_promotions(conn)
    conn.close()
    assert rows == []


# ── check_fingerprint_recurrence tests ─────────────────────────────────────


def test_check_recurrence_returns_zero_when_none(tmp_path):
    db = str(tmp_path / "test.db")
    _create_test_db(db)

    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    count = check_fingerprint_recurrence(conn, "fp_not_seen", _now_utc(-60))
    conn.close()
    assert count == 0


def test_check_recurrence_returns_count_when_seen(tmp_path):
    db = str(tmp_path / "test.db")
    _create_test_db(db)
    # Insert an error that occurred after our reference time
    _insert_error(db, fingerprint="fp_recurring", last_seen_offset_min=-10)

    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    # Reference time is 30 min ago — the error happened 10 min ago → should count
    count = check_fingerprint_recurrence(conn, "fp_recurring", _now_utc(-30))
    conn.close()
    assert count == 1


def test_check_recurrence_excludes_before_merge(tmp_path):
    db = str(tmp_path / "test.db")
    _create_test_db(db)
    # Error occurred 60 min ago; merge was 30 min ago — should NOT count
    _insert_error(db, fingerprint="fp_old", last_seen_offset_min=-60)

    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    count = check_fingerprint_recurrence(conn, "fp_old", _now_utc(-30))
    conn.close()
    assert count == 0


# ── check_healthchecks_clean tests ──────────────────────────────────────────


def test_healthchecks_clean_returns_true_when_no_critical(tmp_path):
    db = str(tmp_path / "test.db")
    _create_test_db(db)

    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    result = check_healthchecks_clean(conn, _now_utc(-30))
    conn.close()
    assert result is True


def test_healthchecks_clean_returns_false_when_critical_present(tmp_path):
    db = str(tmp_path / "test.db")
    _create_test_db(db)
    # Insert a CRITICAL healthcheck error that occurred after the reference time
    _insert_error(
        db,
        fingerprint="hc_critical_001",
        source="healthcheck",
        level="CRITICAL",
        last_seen_offset_min=-5,
        first_seen_offset_min=-5,
    )

    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    result = check_healthchecks_clean(conn, _now_utc(-30))
    conn.close()
    assert result is False


def test_healthchecks_clean_ignores_error_level(tmp_path):
    """Only CRITICAL level errors trigger the unhealthy flag."""
    db = str(tmp_path / "test.db")
    _create_test_db(db)
    _insert_error(
        db,
        fingerprint="hc_error_001",
        source="healthcheck",
        level="ERROR",  # Not CRITICAL
        last_seen_offset_min=-5,
        first_seen_offset_min=-5,
    )

    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    result = check_healthchecks_clean(conn, _now_utc(-30))
    conn.close()
    assert result is True


# ── main() integration tests ─────────────────────────────────────────────────


def test_main_no_pending_exits_zero(tmp_path):
    """With no pending promotions, main returns 0 and prints empty summary."""
    db = str(tmp_path / "test.db")
    _create_test_db(db)

    rc = main(["--db", db, "--dry-run"])
    assert rc == 0


def test_main_respects_db_argument(tmp_path):
    """--db argument routes to the provided database, not the production one."""
    db = str(tmp_path / "custom.db")
    _create_test_db(db)

    # Should not raise even though prod DB may not exist
    rc = main(["--db", db, "--dry-run"])
    assert rc == 0


def test_main_dry_run_does_not_modify_db(tmp_path):
    """--dry-run must not write any DB changes."""
    db = str(tmp_path / "test.db")
    _create_test_db(db)
    _insert_merged_attempt(db, fingerprint="fp_dryrun", started_offset_min=-35)

    conn = sqlite3.connect(db)
    before_outcome = conn.execute(
        "SELECT monitor_outcome FROM fix_attempts WHERE fingerprint='fp_dryrun'"
    ).fetchone()[0]
    conn.close()
    assert before_outcome == "pending"

    main(["--db", db, "--dry-run"])

    conn = sqlite3.connect(db)
    after_outcome = conn.execute(
        "SELECT monitor_outcome FROM fix_attempts WHERE fingerprint='fp_dryrun'"
    ).fetchone()[0]
    conn.close()
    assert after_outcome == "pending", "dry-run must not modify monitor_outcome"


def test_main_promotes_clean_attempt(tmp_path):
    """A pending attempt with no recurrence + clean healthchecks should be promoted."""
    db = str(tmp_path / "test.db")
    _create_test_db(db)
    _insert_merged_attempt(db, fingerprint="fp_clean_001", started_offset_min=-35)

    with patch.object(promo, "fast_forward_main_to_staging", return_value=(True, "sha_promoted")):
        rc = main(["--db", db])

    assert rc == 0
    conn = sqlite3.connect(db)
    outcome = conn.execute(
        "SELECT monitor_outcome FROM fix_attempts WHERE fingerprint='fp_clean_001'"
    ).fetchone()[0]
    audit_rows = conn.execute(
        "SELECT decision FROM fix_audit_log WHERE decision='PROMOTED_TO_MAIN'"
    ).fetchall()
    conn.close()
    assert outcome == "clean"
    assert len(audit_rows) == 1


def test_main_reverts_on_fingerprint_recurrence(tmp_path):
    """A pending attempt with a recurring fingerprint should be reverted."""
    db = str(tmp_path / "test.db")
    _create_test_db(db)
    fp = "fp_recurring_main"
    _insert_merged_attempt(db, fingerprint=fp, started_offset_min=-35)
    # Recurrence after merge (10 min ago)
    _insert_error(db, fingerprint=fp, last_seen_offset_min=-10)

    with patch.object(
        promo, "revert_promotion", return_value=(True, "revert_sha_001")
    ):
        rc = main(["--db", db])

    assert rc == 0
    conn = sqlite3.connect(db)
    outcome = conn.execute(
        f"SELECT monitor_outcome, revert_reason FROM fix_attempts WHERE fingerprint='{fp}'"
    ).fetchone()
    conn.close()
    assert outcome[0] == "reverted"
    assert "fingerprint recurred" in outcome[1]
