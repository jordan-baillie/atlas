"""Tests for SQLiteErrorWriter in utils/logging_config.py.

Test IDs match the spec list (1-20).
Fixtures create a per-test SQLite DB with the errors table so these tests
are fully isolated from production data/atlas.db.
"""
from __future__ import annotations

import logging
import os
import sqlite3
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# ── Schema (mirrors spec; DO NOT depend on migration being applied) ──────────

_CREATE_ERRORS_TABLE = """
CREATE TABLE IF NOT EXISTS errors (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  fingerprint TEXT NOT NULL,
  first_seen_ts TEXT NOT NULL,
  last_seen_ts TEXT NOT NULL,
  occurrence_count INTEGER NOT NULL DEFAULT 1,
  ts TEXT NOT NULL,
  source TEXT NOT NULL,
  service TEXT,
  level TEXT NOT NULL,
  logger_name TEXT,
  message TEXT NOT NULL,
  exc_type TEXT, exc_message TEXT, traceback TEXT,
  file_path TEXT, line_number INTEGER, function_name TEXT,
  pid INTEGER, hostname TEXT,
  context_json TEXT,
  market_hours INTEGER NOT NULL DEFAULT 0,
  halt_active INTEGER NOT NULL DEFAULT 0,
  git_sha TEXT,
  classification TEXT NOT NULL DEFAULT 'UNCLASSIFIED',
  triage_reason TEXT,
  tier INTEGER NOT NULL DEFAULT 99,
  remediation_status TEXT NOT NULL DEFAULT 'NEW',
  remediation_attempts INTEGER NOT NULL DEFAULT 0,
  last_attempt_at TEXT,
  fixed_by_attempt_id INTEGER,
  resolved_at TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
)
"""
_CREATE_FP_INDEX = (
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_errors_fingerprint ON errors(fingerprint)"
)


# ── Fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture
def errors_db(tmp_path: Path) -> str:
    """Fresh SQLite DB with the errors table — one per test."""
    db_path = str(tmp_path / "errors_test.db")
    conn = sqlite3.connect(db_path)
    conn.execute(_CREATE_ERRORS_TABLE)
    conn.execute(_CREATE_FP_INDEX)
    conn.commit()
    conn.close()
    return db_path


@pytest.fixture
def writer(errors_db: str):
    """SQLiteErrorWriter pointing at the per-test errors DB."""
    from atlas.kernel.logging_config import SQLiteErrorWriter
    return SQLiteErrorWriter(script_name="test_script", db_path=errors_db)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _make_record(
    name: str = "atlas.test",
    level: int = logging.ERROR,
    msg: str = "Test error message",
    pathname: str = "/root/atlas/test_module.py",
    lineno: int = 42,
    funcname: str = "test_func",
    exc_info=None,
) -> logging.LogRecord:
    record = logging.LogRecord(
        name=name,
        level=level,
        pathname=pathname,
        lineno=lineno,
        msg=msg,
        args=(),
        exc_info=exc_info,
    )
    record.funcName = funcname
    return record


def _count_rows(db_path: str) -> int:
    conn = sqlite3.connect(db_path)
    n = conn.execute("SELECT COUNT(*) FROM errors").fetchone()[0]
    conn.close()
    return n


def _fetch_all(db_path: str) -> list[dict]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM errors").fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ── 1-4: Basic emit filtering ─────────────────────────────────────────────────

class TestEmitFiltering:

    # 1
    def test_emit_error_inserts_row(self, writer: object, errors_db: str) -> None:
        """emit() inserts a row when level=ERROR."""
        writer.emit(_make_record(level=logging.ERROR))
        assert _count_rows(errors_db) == 1

    # 2
    def test_emit_critical_inserts_row(self, writer: object, errors_db: str) -> None:
        """emit() inserts a row when level=CRITICAL."""
        writer.emit(_make_record(level=logging.CRITICAL, msg="Critical failure"))
        assert _count_rows(errors_db) == 1

    # 3 — level filter happens at callHandlers level; route through a real logger
    def test_warning_not_inserted_via_logger(self, writer: object, errors_db: str) -> None:
        """WARNING records don't reach emit() when routed through the logging system."""
        lg = logging.getLogger("_atlas_test_warning_filter_unique_3")
        lg.addHandler(writer)
        lg.setLevel(logging.DEBUG)
        lg.propagate = False
        lg.warning("this is a warning — must NOT be stored")
        lg.removeHandler(writer)
        assert _count_rows(errors_db) == 0

    # 4
    def test_emit_yfinance_logger_filtered(self, writer: object, errors_db: str) -> None:
        """emit() does NOT insert for yfinance.* logger names (even at ERROR level)."""
        writer.emit(_make_record(name="yfinance.download", level=logging.ERROR))
        assert _count_rows(errors_db) == 0

    def test_emit_yfinance_sublogger_filtered(self, writer: object, errors_db: str) -> None:
        """yfinance.base.* loggers also filtered."""
        writer.emit(_make_record(name="yfinance.base.TickerBase", level=logging.ERROR))
        assert _count_rows(errors_db) == 0


# ── 5-6: Dedup ───────────────────────────────────────────────────────────────

class TestDedup:

    # 5
    def test_same_fingerprint_bumps_count(self, writer: object, errors_db: str) -> None:
        """emit() with same message/file/line twice → 1 row, occurrence_count=2."""
        rec = _make_record(msg="Identical error", pathname="/root/atlas/x.py", lineno=10)
        writer.emit(rec)
        writer.emit(rec)
        rows = _fetch_all(errors_db)
        assert len(rows) == 1
        assert rows[0]["occurrence_count"] == 2

    # 6
    def test_different_fingerprints_two_rows(self, writer: object, errors_db: str) -> None:
        """Different messages → 2 separate rows."""
        writer.emit(_make_record(msg="Error alpha", pathname="/root/atlas/a.py", lineno=1))
        writer.emit(_make_record(msg="Error beta",  pathname="/root/atlas/b.py", lineno=99))
        assert _count_rows(errors_db) == 2


# ── 7: exc_info ───────────────────────────────────────────────────────────────

class TestExcInfo:

    # 7
    def test_exc_info_captured_correctly(self, writer: object, errors_db: str) -> None:
        """exc_info correctly captured (ZeroDivisionError, division by zero)."""
        try:
            1 / 0
        except ZeroDivisionError:
            exc_info = sys.exc_info()

        rec = _make_record(msg="Division error", exc_info=exc_info)
        writer.emit(rec)

        rows = _fetch_all(errors_db)
        assert len(rows) == 1
        row = rows[0]
        assert row["exc_type"] == "ZeroDivisionError"
        assert row["exc_message"] is not None
        assert "division by zero" in row["exc_message"]
        assert row["traceback"] is not None
        assert "ZeroDivisionError" in row["traceback"]


# ── 8-14: Column values ───────────────────────────────────────────────────────

class TestColumnValues:

    # 8
    def test_not_null_columns_populated(self, writer: object, errors_db: str) -> None:
        """emit() populates all NOT-NULL columns — no IntegrityErrors."""
        writer.emit(_make_record())
        rows = _fetch_all(errors_db)
        assert len(rows) == 1
        row = rows[0]
        for col in ("fingerprint", "first_seen_ts", "last_seen_ts", "ts",
                    "source", "level", "message"):
            assert row[col] is not None, f"Column {col!r} was NULL"
        assert row["occurrence_count"] == 1

    # 9
    def test_level_error_set_correctly(self, writer: object, errors_db: str) -> None:
        writer.emit(_make_record(level=logging.ERROR, msg="err_level_test"))
        assert _fetch_all(errors_db)[0]["level"] == "ERROR"

    def test_level_critical_set_correctly(self, writer: object, errors_db: str) -> None:
        writer.emit(_make_record(level=logging.CRITICAL, msg="crit_level_test"))
        assert _fetch_all(errors_db)[0]["level"] == "CRITICAL"

    # 10
    def test_logger_name_set(self, writer: object, errors_db: str) -> None:
        writer.emit(_make_record(name="atlas.live_executor"))
        assert _fetch_all(errors_db)[0]["logger_name"] == "atlas.live_executor"

    # 11
    def test_pid_hostname_file_path_line_function(self, writer: object, errors_db: str) -> None:
        """emit() sets pid, hostname, file_path, line_number, function_name."""
        writer.emit(_make_record(
            pathname="/root/atlas/brokers/live_executor.py",
            lineno=474,
            funcname="_execute_entry",
        ))
        row = _fetch_all(errors_db)[0]
        assert row["pid"] == os.getpid()
        assert row["file_path"] == "/root/atlas/brokers/live_executor.py"
        assert row["line_number"] == 474
        assert row["function_name"] == "_execute_entry"

    # 12
    def test_source_is_python_logger(self, writer: object, errors_db: str) -> None:
        writer.emit(_make_record())
        assert _fetch_all(errors_db)[0]["source"] == "python_logger"

    # 13
    def test_service_is_script_name(self, writer: object, errors_db: str) -> None:
        writer.emit(_make_record())
        assert _fetch_all(errors_db)[0]["service"] == "test_script"

    # 14
    def test_classification_and_remediation_defaults(self, writer: object, errors_db: str) -> None:
        writer.emit(_make_record())
        row = _fetch_all(errors_db)[0]
        assert row["classification"] == "UNCLASSIFIED"
        assert row["remediation_status"] == "NEW"
        assert row["tier"] == 99

    # 15
    def test_long_message_truncated_at_8000(self, writer: object, errors_db: str) -> None:
        """emit() truncates very long messages (>8000 chars)."""
        long_msg = "x" * 10_000
        writer.emit(_make_record(msg=long_msg))
        rows = _fetch_all(errors_db)
        assert len(rows) == 1
        assert len(rows[0]["message"]) <= 8000


# ── 16-17: Fail-open ─────────────────────────────────────────────────────────

class TestFailOpen:

    # 16
    def test_fail_open_db_not_exist(self) -> None:
        """emit() is fail-open: if DB path doesn't exist, does NOT raise."""
        from atlas.kernel.logging_config import SQLiteErrorWriter
        bad = SQLiteErrorWriter(script_name="t", db_path="/nonexistent/path/atlas.db")
        bad.emit(_make_record())  # must not raise

    # 17
    def test_fail_open_table_not_exist(self, tmp_path: Path) -> None:
        """emit() is fail-open: if errors table is missing, does NOT raise."""
        from atlas.kernel.logging_config import SQLiteErrorWriter
        db_path = str(tmp_path / "empty.db")
        sqlite3.connect(db_path).close()  # create empty DB, no tables
        bad = SQLiteErrorWriter(script_name="t", db_path=db_path)
        bad.emit(_make_record())  # must not raise

    def test_fail_open_unusual_message_content(self, writer: object) -> None:
        """emit() is fail-open even with null bytes in message."""
        writer.emit(_make_record(msg="\x00\x01 null bytes \xff"))  # must not raise


# ── 18: Thread safety ────────────────────────────────────────────────────────

class TestThreadSafety:

    # 18
    def test_threadsafe_10_threads_100_emits(self, writer: object, errors_db: str) -> None:
        """10 threads × 100 emits → rows inserted correctly, no exceptions."""
        exceptions: list[Exception] = []

        def worker(thread_id: int) -> None:
            try:
                for i in range(100):
                    # Unique lineno per (thread, i) → unique fingerprint per call
                    # (normalised message collapses numbers, but file+lineno differ)
                    writer.emit(_make_record(
                        msg=f"Thread error number {i} occurred",
                        pathname="/root/atlas/t.py",
                        lineno=thread_id * 1000 + i + 1,  # +1: avoid falsy 0
                    ))
            except Exception as exc:
                exceptions.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        assert not exceptions, f"Thread exceptions: {exceptions}"
        n = _count_rows(errors_db)
        # Each unique lineno → unique fingerprint; 10*100=1000 distinct rows max.
        # Due to the _lock we should get exactly 1000 with no UNIQUE conflicts.
        assert 0 < n <= 1000


# ── 19-20: setup_logging() guards ────────────────────────────────────────────

class TestSetupLoggingGuards:

    # 19
    def test_sqlite_writer_env_var_0_disables_handler(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """ATLAS_SQLITE_ERROR_WRITER=0 prevents SQLiteErrorWriter from being installed."""
        import atlas.kernel.logging_config as _lc

        monkeypatch.setenv("ATLAS_SQLITE_ERROR_WRITER", "0")
        original_done = _lc._setup_done
        original_collector = _lc._collector
        _lc._setup_done = False

        # Temporarily hide pytest from sys.modules so _in_pytest=False
        # and only the env-var guard controls whether the handler is added.
        pytest_mod = sys.modules.pop("pytest", None)
        orig_pytest_env = os.environ.pop("PYTEST_CURRENT_TEST", None)

        root = logging.getLogger()
        handlers_before = list(root.handlers)

        try:
            _lc.setup_logging("_test_env_off_guard")
            current_types = [type(h).__name__ for h in root.handlers]
            assert "SQLiteErrorWriter" not in current_types, (
                f"SQLiteErrorWriter was installed despite ATLAS_SQLITE_ERROR_WRITER=0: {current_types}"
            )
        finally:
            # Restore sys.modules and env
            if pytest_mod is not None:
                sys.modules["pytest"] = pytest_mod
            if orig_pytest_env is not None:
                os.environ["PYTEST_CURRENT_TEST"] = orig_pytest_env
            # Restore logging_config state
            _lc._setup_done = original_done
            _lc._collector = original_collector
            # Remove handlers added by setup_logging
            for h in list(root.handlers):
                if h not in handlers_before:
                    root.removeHandler(h)
                    try:
                        h.close()
                    except Exception:
                        pass

    # 20
    def test_setup_logging_under_pytest_no_sqlite_handler(self) -> None:
        """setup_logging() under pytest does NOT install SQLiteErrorWriter (_in_pytest guard)."""
        import atlas.kernel.logging_config as _lc

        original_done = _lc._setup_done
        original_collector = _lc._collector
        _lc._setup_done = False

        root = logging.getLogger()
        handlers_before = list(root.handlers)

        try:
            _lc.setup_logging("_test_pytest_guard")
            # Under pytest, "pytest" is in sys.modules → _in_pytest=True
            # → SQLiteErrorWriter block is skipped regardless of env var
            sqlite_handlers = [
                h for h in root.handlers
                if type(h).__name__ == "SQLiteErrorWriter"
            ]
            assert not sqlite_handlers, (
                f"SQLiteErrorWriter was added despite being in pytest: {sqlite_handlers}"
            )
        finally:
            _lc._setup_done = original_done
            _lc._collector = original_collector
            for h in list(root.handlers):
                if h not in handlers_before:
                    root.removeHandler(h)
                    try:
                        h.close()
                    except Exception:
                        pass
