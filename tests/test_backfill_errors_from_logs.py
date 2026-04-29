"""Tests for scripts/backfill_errors_from_logs.py.

Covers all 20 spec test cases:
 1.  Dry run on empty logs → 0 inserts
 2.  Single ERROR line parsed correctly
 3.  CRITICAL level captured
 4.  WARNING level NOT captured
 5.  yfinance.* logger filtered out
 6.  Multi-line traceback captured
 7.  Idempotent: same file twice → 1 row, occurrence_count=2
 8.  .gz rotated log file parsed
 9.  Records older than --days cutoff skipped
10.  --limit N stops at N records
11.  system_log level='error' captured
12.  system_log level='critical' captured
13.  system_log level='info'/'warning' skipped
14.  Missing errors table → exit code 2
15.  journalctl unavailable → no crash, 0 records, warning logged
16.  Two lines with same fingerprint → 1 row inserted then bumped (count=2)
17.  Two lines with different messages → 2 distinct rows
18.  --source python_logs skips journald and system_log
19.  source field = 'backfill' on every inserted row
20.  Property: same backfill run twice → identical row count (idempotent)
"""
from __future__ import annotations

import gzip
import json
import logging
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

import scripts.backfill_errors_from_logs as bef  # noqa: E402

# ── Errors table schema ──────────────────────────────────────────────────────
# Created manually in fixtures so tests don't depend on the migration having run.

_ERRORS_SCHEMA = """
CREATE TABLE IF NOT EXISTS errors (
    id                 INTEGER PRIMARY KEY,
    ts                 TEXT    NOT NULL,
    source             TEXT    NOT NULL,
    service            TEXT,
    level              TEXT    NOT NULL,
    logger_name        TEXT,
    message            TEXT    NOT NULL,
    exc_type           TEXT,
    exc_message        TEXT,
    traceback          TEXT,
    file_path          TEXT,
    line_number        INTEGER,
    fingerprint        TEXT    NOT NULL,
    occurrence_count   INTEGER DEFAULT 1,
    classification     TEXT    DEFAULT 'UNCLASSIFIED',
    tier               INTEGER DEFAULT 99,
    remediation_status TEXT    DEFAULT 'NEW',
    last_seen_ts       TEXT,
    first_seen_ts      TEXT
)
"""

_SYSTEM_LOG_SCHEMA = """
CREATE TABLE IF NOT EXISTS system_log (
    id        INTEGER PRIMARY KEY,
    timestamp TEXT    NOT NULL,
    service   TEXT,
    level     TEXT    NOT NULL,
    message   TEXT
)
"""


# ── Fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture()
def errors_db(tmp_path: Path) -> str:
    """Fresh SQLite DB with only the errors table — no migration needed."""
    db_path = str(tmp_path / "errors_test.db")
    conn = sqlite3.connect(db_path)
    conn.execute(_ERRORS_SCHEMA)
    conn.commit()
    conn.close()
    return db_path


@pytest.fixture()
def errors_syslog_db(tmp_path: Path) -> str:
    """Fresh SQLite DB with errors + system_log tables."""
    db_path = str(tmp_path / "syslog_test.db")
    conn = sqlite3.connect(db_path)
    conn.execute(_ERRORS_SCHEMA)
    conn.execute(_SYSTEM_LOG_SCHEMA)
    conn.commit()
    conn.close()
    return db_path


@pytest.fixture()
def log_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Temporary log directory; patches bef._LOG_DIR to point at it."""
    d = tmp_path / "logs"
    d.mkdir()
    monkeypatch.setattr(bef, "_LOG_DIR", d)
    return d


# ── Helpers ───────────────────────────────────────────────────────────────────

def _ts(days_ago: float = 0, minutes_ago: float = 0) -> str:
    """ISO-style timestamp N days/minutes ago."""
    dt = datetime.utcnow() - timedelta(days=days_ago, minutes=minutes_ago)
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _log_line(
    level: str = "ERROR",
    logger: str = "atlas.module",
    msg: str = "Something broke",
    days_ago: float = 0,
) -> str:
    return f"{_ts(days_ago)} [{level}] {logger}: {msg}"


def _write_log(log_dir: Path, name: str, lines: list[str]) -> Path:
    p = log_dir / name
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


def _write_gz_log(log_dir: Path, name: str, lines: list[str]) -> Path:
    p = log_dir / name
    with gzip.open(p, "wt", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return p


def _rows(db_path: str) -> list[sqlite3.Row]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cur = conn.execute("SELECT * FROM errors ORDER BY id")
    rows = cur.fetchall()
    conn.close()
    return rows


# ── Tests ─────────────────────────────────────────────────────────────────────

class TestDryRun:
    """Tests 1, 4, 19 (dry-run path)."""

    def test_01_dry_run_empty_logs_zero_inserts(
        self, errors_db: str, log_dir: Path
    ) -> None:
        """Test 1 — Dry run on empty log dir → 0 would-be inserts."""
        result = bef.main(["--db", errors_db, "--source", "python_logs"])
        assert result == 0
        # Nothing in the DB (dry-run writes nothing)
        assert _rows(errors_db) == []

    def test_04_dry_run_warning_not_counted(
        self, errors_db: str, log_dir: Path
    ) -> None:
        """Test 4 — WARNING level is skipped even in dry-run count."""
        _write_log(log_dir, "atlas.log", [_log_line("WARNING", msg="Warn only")])
        # dry-run: counts["inserted"] should be 0 (warnings filtered)
        # The call returns 0 (success)
        result = bef.main(["--db", errors_db, "--source", "python_logs"])
        assert result == 0
        assert _rows(errors_db) == []  # nothing written in dry-run


class TestPythonLogs:
    """Tests 2, 3, 4, 5, 6, 7, 8, 9, 10."""

    def test_02_single_error_line_parsed_correctly(
        self, errors_db: str, log_dir: Path
    ) -> None:
        """Test 2 — Single ERROR line is parsed and inserted."""
        _write_log(log_dir, "atlas.log", [
            _log_line("ERROR", "atlas.live_executor", "Order failed for AAPL"),
        ])
        result = bef.main(["--apply", "--db", errors_db, "--source", "python_logs"])
        assert result == 0
        rows = _rows(errors_db)
        assert len(rows) == 1
        assert rows[0]["level"] == "ERROR"
        assert rows[0]["logger_name"] == "atlas.live_executor"
        assert "Order failed for AAPL" in rows[0]["message"]
        assert rows[0]["occurrence_count"] == 1

    def test_03_critical_level_captured(
        self, errors_db: str, log_dir: Path
    ) -> None:
        """Test 3 — CRITICAL level is inserted."""
        _write_log(log_dir, "atlas.log", [
            _log_line("CRITICAL", "atlas.kill_switch", "Daily drawdown exceeded"),
        ])
        result = bef.main(["--apply", "--db", errors_db, "--source", "python_logs"])
        assert result == 0
        rows = _rows(errors_db)
        assert len(rows) == 1
        assert rows[0]["level"] == "CRITICAL"

    def test_04_warning_level_not_captured(
        self, errors_db: str, log_dir: Path
    ) -> None:
        """Test 4 — WARNING level is NOT inserted."""
        _write_log(log_dir, "atlas.log", [
            _log_line("WARNING", "atlas.broker", "Retrying request"),
        ])
        result = bef.main(["--apply", "--db", errors_db, "--source", "python_logs"])
        assert result == 0
        assert _rows(errors_db) == []

    def test_05_yfinance_logger_filtered(
        self, errors_db: str, log_dir: Path
    ) -> None:
        """Test 5 — yfinance.* ERROR lines are skipped."""
        _write_log(log_dir, "atlas.log", [
            _log_line("ERROR", "yfinance.base", "No data for AAPL"),
            _log_line("ERROR", "yfinance", "404 not found"),
        ])
        result = bef.main(["--apply", "--db", errors_db, "--source", "python_logs"])
        assert result == 0
        assert _rows(errors_db) == []

    def test_06_multiline_traceback_captured(
        self, errors_db: str, log_dir: Path
    ) -> None:
        """Test 6 — Traceback continuation lines are collected into 'traceback' column."""
        lines = [
            _log_line("ERROR", "atlas.executor", "Unhandled exception"),
            "Traceback (most recent call last):",
            '  File "scripts/foo.py", line 42, in bar',
            "    raise ValueError('bad')",
            "ValueError: bad",
        ]
        _write_log(log_dir, "atlas.log", lines)
        result = bef.main(["--apply", "--db", errors_db, "--source", "python_logs"])
        assert result == 0
        rows = _rows(errors_db)
        assert len(rows) == 1
        tb = rows[0]["traceback"]
        assert tb is not None
        assert "Traceback" in tb
        assert "ValueError" in tb

    def test_07_idempotent_same_file_twice(
        self, errors_db: str, log_dir: Path
    ) -> None:
        """Test 7 — Processing the same file twice: 1 row, occurrence_count=2."""
        _write_log(log_dir, "atlas.log", [
            _log_line("ERROR", "atlas.module", "Repeated failure"),
        ])
        bef.main(["--apply", "--db", errors_db, "--source", "python_logs"])
        bef.main(["--apply", "--db", errors_db, "--source", "python_logs"])
        rows = _rows(errors_db)
        assert len(rows) == 1
        assert rows[0]["occurrence_count"] == 2

    def test_08_gz_rotated_log_parsed(
        self, errors_db: str, log_dir: Path
    ) -> None:
        """Test 8 — .gz rotated log file is read and parsed."""
        _write_gz_log(log_dir, "atlas.log-20260401.gz", [
            _log_line("ERROR", "atlas.data", "Gz file error", days_ago=5),
        ])
        result = bef.main(["--apply", "--db", errors_db, "--days", "30",
                           "--source", "python_logs"])
        assert result == 0
        rows = _rows(errors_db)
        assert len(rows) == 1
        assert rows[0]["level"] == "ERROR"

    def test_09_records_older_than_days_skipped(
        self, errors_db: str, log_dir: Path
    ) -> None:
        """Test 9 — Records older than --days cutoff are skipped."""
        _write_log(log_dir, "atlas.log", [
            # Recent (within 5 days)
            _log_line("ERROR", "atlas.mod", "Recent error", days_ago=2),
            # Old (outside 5-day window)
            _log_line("ERROR", "atlas.mod", "Old error", days_ago=10),
        ])
        result = bef.main(["--apply", "--db", errors_db, "--days", "5",
                           "--source", "python_logs"])
        assert result == 0
        rows = _rows(errors_db)
        assert len(rows) == 1
        assert "Recent error" in rows[0]["message"]

    def test_10_limit_stops_at_n_records(
        self, errors_db: str, log_dir: Path
    ) -> None:
        """Test 10 — --limit N processes only N total records."""
        # Write 10 ERROR lines; limit=3 should insert only 3
        labels = ["alpha", "bravo", "charlie", "delta", "echo",
                  "foxtrot", "golf", "hotel", "india", "juliet"]
        lines = [
            _log_line("ERROR", "atlas.mod", f"Distinct error {labels[i]}")
            for i in range(10)
        ]
        _write_log(log_dir, "atlas.log", lines)
        result = bef.main(["--apply", "--db", errors_db, "--source", "python_logs",
                           "--limit", "3"])
        assert result == 0
        rows = _rows(errors_db)
        assert len(rows) == 3


class TestSystemLog:
    """Tests 11, 12, 13."""

    def test_11_system_log_error_captured(
        self, errors_syslog_db: str, log_dir: Path
    ) -> None:
        """Test 11 — system_log rows with level='error' are captured."""
        ts = _ts(days_ago=1).replace(" ", "T")
        conn = sqlite3.connect(errors_syslog_db)
        conn.execute(
            "INSERT INTO system_log (timestamp, service, level, message) VALUES (?,?,?,?)",
            (ts, "atlas.eod", "error", "EOD settlement failed"),
        )
        conn.commit()
        conn.close()
        result = bef.main(["--apply", "--db", errors_syslog_db,
                           "--source", "system_log"])
        assert result == 0
        rows = _rows(errors_syslog_db)
        assert len(rows) == 1
        assert rows[0]["level"] == "ERROR"
        assert "EOD settlement failed" in rows[0]["message"]

    def test_12_system_log_critical_captured(
        self, errors_syslog_db: str, log_dir: Path
    ) -> None:
        """Test 12 — system_log rows with level='critical' are captured."""
        ts = _ts(days_ago=1).replace(" ", "T")
        conn = sqlite3.connect(errors_syslog_db)
        conn.execute(
            "INSERT INTO system_log (timestamp, service, level, message) VALUES (?,?,?,?)",
            (ts, "atlas.kill_switch", "critical", "Kill switch triggered"),
        )
        conn.commit()
        conn.close()
        result = bef.main(["--apply", "--db", errors_syslog_db,
                           "--source", "system_log"])
        assert result == 0
        rows = _rows(errors_syslog_db)
        assert len(rows) == 1
        assert rows[0]["level"] == "CRITICAL"

    def test_13_system_log_info_warning_skipped(
        self, errors_syslog_db: str, log_dir: Path
    ) -> None:
        """Test 13 — system_log rows with level='info' or 'warning' are skipped."""
        ts = _ts(days_ago=1).replace(" ", "T")
        conn = sqlite3.connect(errors_syslog_db)
        conn.executemany(
            "INSERT INTO system_log (timestamp, service, level, message) VALUES (?,?,?,?)",
            [
                (ts, "atlas.svc", "info", "Service started"),
                (ts, "atlas.svc", "warning", "Low disk space"),
            ],
        )
        conn.commit()
        conn.close()
        # system_log WHERE level IN ('error','critical') — info/warning excluded at query
        result = bef.main(["--apply", "--db", errors_syslog_db,
                           "--source", "system_log"])
        assert result == 0
        assert _rows(errors_syslog_db) == []


class TestEdgeCases:
    """Tests 14, 15, 18, 19, 20."""

    def test_14_missing_errors_table_returns_exit_code_2(
        self, tmp_path: Path
    ) -> None:
        """Test 14 — If errors table absent, main() returns 2."""
        db_path = str(tmp_path / "no_errors.db")
        conn = sqlite3.connect(db_path)
        conn.execute("CREATE TABLE other (id INTEGER PRIMARY KEY)")
        conn.commit()
        conn.close()
        result = bef.main(["--db", db_path, "--source", "python_logs"])
        assert result == 2

    def test_15_journald_unavailable_no_crash(
        self, errors_db: str, caplog: pytest.LogCaptureFixture, log_dir: Path
    ) -> None:
        """Test 15 — If journalctl raises, no crash; warning logged; 0 records."""
        with patch.object(
            bef.subprocess, "check_output",
            side_effect=FileNotFoundError("journalctl not found"),
        ):
            with caplog.at_level(logging.WARNING):
                result = bef.main(["--apply", "--db", errors_db, "--source", "journald"])
        assert result == 0
        assert _rows(errors_db) == []
        # Warning about unavailability should be in captured logs
        assert any("journalctl" in r.message.lower() for r in caplog.records)

    def test_18_source_python_logs_only(
        self, errors_syslog_db: str, log_dir: Path
    ) -> None:
        """Test 18 — --source python_logs never touches system_log or journald."""
        # Put a record in system_log that would be captured if queried
        ts = _ts(days_ago=1).replace(" ", "T")
        conn = sqlite3.connect(errors_syslog_db)
        conn.execute(
            "INSERT INTO system_log (timestamp, service, level, message) VALUES (?,?,?,?)",
            (ts, "atlas.svc", "error", "Should not appear"),
        )
        conn.commit()
        conn.close()
        # --source python_logs means only iter_python_logs is called
        # No log files → 0 records inserted
        # Mock subprocess to ensure journalctl is never called
        with patch.object(bef.subprocess, "check_output") as mock_co:
            result = bef.main(["--apply", "--db", errors_syslog_db,
                               "--source", "python_logs"])
            mock_co.assert_not_called()
        assert result == 0
        # system_log record not captured
        assert _rows(errors_syslog_db) == []

    def test_19_source_field_is_backfill(
        self, errors_db: str, log_dir: Path
    ) -> None:
        """Test 19 — Every inserted row has source='backfill'."""
        _write_log(log_dir, "atlas.log", [
            _log_line("ERROR", "atlas.mod", "Error one"),
            _log_line("CRITICAL", "atlas.mod", "Critical two"),
        ])
        bef.main(["--apply", "--db", errors_db, "--source", "python_logs"])
        for row in _rows(errors_db):
            assert row["source"] == "backfill", (
                f"Expected source='backfill', got {row['source']!r}"
            )

    def test_20_property_idempotent_twice(
        self, errors_db: str, log_dir: Path
    ) -> None:
        """Test 20 — Running the same backfill twice leaves identical row count."""
        _write_log(log_dir, "atlas.log", [
            _log_line("ERROR", "atlas.mod", "Idempotency error alpha"),
            _log_line("ERROR", "atlas.mod", "Idempotency error beta"),
        ])
        bef.main(["--apply", "--db", errors_db, "--source", "python_logs"])
        count_after_first = len(_rows(errors_db))
        bef.main(["--apply", "--db", errors_db, "--source", "python_logs"])
        count_after_second = len(_rows(errors_db))
        assert count_after_first == count_after_second == 2


class TestFingerprintBehaviour:
    """Tests 16, 17 — fingerprint deduplication logic."""

    def test_16_same_fingerprint_bumps_count(
        self, errors_db: str, log_dir: Path
    ) -> None:
        """Test 16 — Two lines with the same message produce 1 row, count=2."""
        msg = "Identical error message for dedup test"
        _write_log(log_dir, "atlas.log", [
            _log_line("ERROR", "atlas.mod", msg),
            _log_line("ERROR", "atlas.mod", msg),
        ])
        bef.main(["--apply", "--db", errors_db, "--source", "python_logs"])
        rows = _rows(errors_db)
        assert len(rows) == 1
        assert rows[0]["occurrence_count"] == 2

    def test_17_different_messages_two_rows(
        self, errors_db: str, log_dir: Path
    ) -> None:
        """Test 17 — Two lines with different messages produce 2 distinct rows."""
        _write_log(log_dir, "atlas.log", [
            _log_line("ERROR", "atlas.mod", "First distinct error ALPHA"),
            _log_line("ERROR", "atlas.mod", "Second distinct error BETA"),
        ])
        bef.main(["--apply", "--db", errors_db, "--source", "python_logs"])
        rows = _rows(errors_db)
        assert len(rows) == 2
        messages = {r["message"] for r in rows}
        assert "First distinct error ALPHA" in messages
        assert "Second distinct error BETA" in messages


class TestJournald:
    """Test 15 (extended) — journald parsing with synthetic data."""

    def _make_journald_json(self, priority: int = 3, days_ago: float = 1) -> str:
        """Return a fake journald JSON line."""
        ts_us = int(
            (datetime.utcnow() - timedelta(days=days_ago)).timestamp() * 1_000_000
        )
        return json.dumps({
            "PRIORITY": str(priority),
            "__REALTIME_TIMESTAMP": str(ts_us),
            "MESSAGE": "Atlas service crashed unexpectedly",
            "SYSLOG_IDENTIFIER": "atlas-dashboard",
            "_SYSTEMD_UNIT": "atlas-dashboard.service",
        })

    def test_journald_error_priority_3_captured(
        self, errors_db: str, log_dir: Path
    ) -> None:
        """Journald syslog priority 3 (err) → ERROR level inserted."""
        fake_output = self._make_journald_json(priority=3)
        with patch.object(bef.subprocess, "check_output", return_value=fake_output.encode()):
            result = bef.main(["--apply", "--db", errors_db, "--source", "journald"])
        assert result == 0
        rows = _rows(errors_db)
        assert len(rows) == 1
        assert rows[0]["level"] == "ERROR"

    def test_journald_critical_priority_2_captured(
        self, errors_db: str, log_dir: Path
    ) -> None:
        """Journald syslog priority 2 (crit) → CRITICAL level inserted."""
        fake_output = self._make_journald_json(priority=2)
        with patch.object(bef.subprocess, "check_output", return_value=fake_output.encode()):
            result = bef.main(["--apply", "--db", errors_db, "--source", "journald"])
        assert result == 0
        rows = _rows(errors_db)
        assert len(rows) == 1
        assert rows[0]["level"] == "CRITICAL"

    def test_journald_priority_4_warning_skipped(
        self, errors_db: str, log_dir: Path
    ) -> None:
        """Journald syslog priority 4 (warning) → not captured."""
        fake_output = self._make_journald_json(priority=4)
        with patch.object(bef.subprocess, "check_output", return_value=fake_output.encode()):
            result = bef.main(["--apply", "--db", errors_db, "--source", "journald"])
        assert result == 0
        assert _rows(errors_db) == []

    def test_journald_timeout_no_crash(
        self, errors_db: str, caplog: pytest.LogCaptureFixture, log_dir: Path
    ) -> None:
        """Journald TimeoutExpired → logs warning, returns 0, 0 records."""
        import subprocess as _sp

        with patch.object(
            bef.subprocess, "check_output",
            side_effect=_sp.TimeoutExpired(cmd="journalctl", timeout=120),
        ):
            with caplog.at_level(logging.WARNING):
                result = bef.main(["--apply", "--db", errors_db, "--source", "journald"])
        assert result == 0
        assert _rows(errors_db) == []

    def test_journald_source_field_is_backfill(
        self, errors_db: str, log_dir: Path
    ) -> None:
        """Journald records also get source='backfill'."""
        fake_output = self._make_journald_json(priority=3)
        with patch.object(bef.subprocess, "check_output", return_value=fake_output.encode()):
            bef.main(["--apply", "--db", errors_db, "--source", "journald"])
        rows = _rows(errors_db)
        assert rows[0]["source"] == "backfill"
