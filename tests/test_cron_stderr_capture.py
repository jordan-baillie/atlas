"""Tests for cron_stderr_capture.sh + cron_stderr_to_errors.py.

Coverage:
  1.  Wrapper exits 0 transparently when wrapped command exits 0 (no DB write)
  2.  Wrapper preserves wrapped exit code (exit 17 → wrapper exits 17)
  3.  Wrapper inserts a row when wrapped command exits non-zero
  4.  Inserted row has source='cron'
  5.  Inserted row has service=<job_name>
  6.  Inserted row has level='ERROR' for exit code 1
  7.  Inserted row has level='CRITICAL' for exit codes 137, 139, and 134
  8.  Inserted row has message containing exit code
  9.  Inserted row has message containing tail of stderr
  10. context_json contains exit_code
  11. Idempotent: same failing command twice → 1 row, occurrence_count=2
  12. Different exit codes → different fingerprints (2 rows)
  13. Stderr is also passed through (parent process still sees it)
  14. Missing errors table → no crash, Python script exits 0
  15. --stderr-file pointing to nonexistent path → no crash, row inserted
  16. Long stderr (10000 chars) → message truncated to ≤8000 in message column
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

# ── Bootstrap ─────────────────────────────────────────────────────────────────
PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

import db.atlas_db as _adb  # noqa: E402

WRAPPER = str(PROJECT / "scripts" / "cron_stderr_capture.sh")
CAPTURE_PY = str(PROJECT / "scripts" / "cron_stderr_to_errors.py")


# ══════════════════════════════════════════════════════════════════════════════
# Shared helpers
# ══════════════════════════════════════════════════════════════════════════════

_ERRORS_DDL = """
CREATE TABLE IF NOT EXISTS errors (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    fingerprint         TEXT    NOT NULL UNIQUE,
    first_seen_ts       TEXT    NOT NULL,
    last_seen_ts        TEXT    NOT NULL,
    occurrence_count    INTEGER NOT NULL DEFAULT 1,
    ts                  TEXT    NOT NULL,
    source              TEXT    NOT NULL,
    service             TEXT,
    level               TEXT    NOT NULL,
    logger_name         TEXT,
    message             TEXT,
    classification      TEXT    DEFAULT 'UNCLASSIFIED',
    tier                INTEGER DEFAULT 99,
    remediation_status  TEXT    DEFAULT 'NEW',
    context_json        TEXT
)
"""


def _make_db_with_errors(tmp_path: Path) -> str:
    """Create a SQLite DB at *tmp_path/test.db* that has the errors table."""
    db_path = str(tmp_path / "test.db")
    conn = sqlite3.connect(db_path)
    conn.execute(_ERRORS_DDL)
    conn.commit()
    conn.close()
    return db_path


def _fetch_errors(db_path: str) -> list[dict]:
    """Fetch all rows from the errors table in *db_path*."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM errors").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def _run_capture_py(
    *,
    db_path: str,
    job: str = "test_job",
    exit_code: int = 1,
    stderr_file: str | None = None,
    command: str = "sh -c exit 1",
) -> subprocess.CompletedProcess:
    """Run cron_stderr_to_errors.py as a subprocess with a controlled DB.

    Always passes --db so the script writes to the test DB, never prod.
    """
    args = [
        sys.executable,
        CAPTURE_PY,
        "--job", job,
        "--exit-code", str(exit_code),
        "--stderr-file", stderr_file or "/dev/null",
        "--command", command,
        "--db", db_path,
    ]
    env = {**os.environ, "ATLAS_SQLITE_ERROR_WRITER": "0"}
    return subprocess.run(
        args, capture_output=True, text=True, env=env, timeout=15
    )


# ══════════════════════════════════════════════════════════════════════════════
# Tests: bash wrapper behaviour (exit code + stderr passthrough)
# ══════════════════════════════════════════════════════════════════════════════

class TestWrapperExitCode:
    """The wrapper must be transparent with respect to the wrapped exit code."""

    def _run(self, *cmd, job: str = "t") -> subprocess.CompletedProcess:
        env = {**os.environ, "ATLAS_PROJECT": str(PROJECT)}
        return subprocess.run(
            [WRAPPER, job, *cmd],
            capture_output=True,
            text=True,
            env=env,
            timeout=15,
        )

    def test_exit_0_transparent(self):
        """Test 1: wrapper exits 0 when wrapped command exits 0."""
        result = self._run("sh", "-c", "echo hello")
        assert result.returncode == 0, f"Expected 0, got {result.returncode}"

    def test_exit_code_17_preserved(self):
        """Test 2: wrapper returns the exact exit code of the wrapped command."""
        result = self._run("sh", "-c", "exit 17")
        assert result.returncode == 17, f"Expected 17, got {result.returncode}"

    def test_exit_code_1_preserved(self):
        """Extra: exit code 1 propagated correctly."""
        result = self._run("sh", "-c", "exit 1")
        assert result.returncode == 1

    def test_stderr_passthrough(self):
        """Test 13: stderr from wrapped command passes through to the parent."""
        result = self._run("sh", "-c", "echo fake_error_passthrough >&2; exit 1")
        assert "fake_error_passthrough" in result.stderr, (
            f"Expected stderr passthrough; got: {result.stderr!r}"
        )

    def test_missing_args_exits_2(self):
        """Wrapper prints usage and exits 2 when called without enough args."""
        result = subprocess.run(
            [WRAPPER],
            capture_output=True,
            text=True,
            env={**os.environ, "ATLAS_PROJECT": str(PROJECT)},
            timeout=10,
        )
        assert result.returncode == 2


# ══════════════════════════════════════════════════════════════════════════════
# Tests: Python script — cron_stderr_to_errors.py
# ══════════════════════════════════════════════════════════════════════════════

class TestCaptureScript:
    """Unit tests for cron_stderr_to_errors.py called directly as a subprocess.

    Each test calls the Python script with --db pointing at a fresh tmp SQLite
    file so there is zero risk of touching production data.
    """

    @pytest.fixture()
    def db(self, tmp_path: Path) -> str:
        return _make_db_with_errors(tmp_path)

    @pytest.fixture()
    def stderr_file(self, tmp_path: Path) -> Path:
        f = tmp_path / "stderr.log"
        f.write_text("something went wrong\nanother error line\n")
        return f

    # ── convenience wrapper ────────────────────────────────────────────────────

    def _run(
        self, db: str, *, job: str = "test_job", exit_code: int = 1,
        stderr_file: str | None = None, command: str = "sh -c exit 1",
    ) -> subprocess.CompletedProcess:
        return _run_capture_py(
            db_path=db, job=job, exit_code=exit_code,
            stderr_file=stderr_file, command=command,
        )

    # ── tests ─────────────────────────────────────────────────────────────────

    def test_inserts_row_on_nonzero_exit(self, db: str, stderr_file: Path):
        """Test 3: a row is inserted when exit code is non-zero."""
        result = self._run(db, stderr_file=str(stderr_file))
        assert result.returncode == 0, f"Script crashed: {result.stderr}"
        rows = _fetch_errors(db)
        assert len(rows) == 1, f"Expected 1 row; got {len(rows)}"

    def test_source_is_cron(self, db: str, stderr_file: Path):
        """Test 4: source column is 'cron'."""
        self._run(db, stderr_file=str(stderr_file))
        rows = _fetch_errors(db)
        assert rows[0]["source"] == "cron"

    def test_service_is_job_name(self, db: str, stderr_file: Path):
        """Test 5: service column matches the --job argument."""
        self._run(db, job="my_special_job", stderr_file=str(stderr_file))
        rows = _fetch_errors(db)
        assert rows[0]["service"] == "my_special_job"

    def test_level_error_for_exit_1(self, db: str, stderr_file: Path):
        """Test 6: level='ERROR' for a normal non-zero exit."""
        self._run(db, exit_code=1, stderr_file=str(stderr_file))
        rows = _fetch_errors(db)
        assert rows[0]["level"] == "ERROR"

    def test_level_critical_for_exit_137(self, db: str, stderr_file: Path):
        """Test 7a: level='CRITICAL' for SIGKILL (exit 137)."""
        self._run(db, exit_code=137, stderr_file=str(stderr_file))
        rows = _fetch_errors(db)
        assert rows[0]["level"] == "CRITICAL"

    def test_level_critical_for_exit_139(self, db: str, stderr_file: Path):
        """Test 7b: level='CRITICAL' for SIGSEGV (exit 139)."""
        self._run(db, exit_code=139, stderr_file=str(stderr_file))
        rows = _fetch_errors(db)
        assert rows[0]["level"] == "CRITICAL"

    def test_level_critical_for_exit_134(self, db: str, stderr_file: Path):
        """Test 7c: level='CRITICAL' for SIGABRT (exit 134)."""
        self._run(db, exit_code=134, stderr_file=str(stderr_file))
        rows = _fetch_errors(db)
        assert rows[0]["level"] == "CRITICAL"

    def test_message_contains_exit_code(self, db: str, stderr_file: Path):
        """Test 8: message contains the numeric exit code."""
        self._run(db, exit_code=42, stderr_file=str(stderr_file))
        rows = _fetch_errors(db)
        assert "42" in rows[0]["message"]

    def test_message_contains_stderr_tail(self, db: str, stderr_file: Path):
        """Test 9: message contains lines from the stderr file."""
        self._run(db, stderr_file=str(stderr_file))
        rows = _fetch_errors(db)
        msg = rows[0]["message"]
        assert "something went wrong" in msg or "another error line" in msg

    def test_context_json_has_exit_code(self, db: str, stderr_file: Path):
        """Test 10: context_json is valid JSON containing exit_code."""
        self._run(db, exit_code=5, stderr_file=str(stderr_file))
        rows = _fetch_errors(db)
        ctx = json.loads(rows[0]["context_json"])
        assert ctx["exit_code"] == 5

    def test_idempotent_bumps_count(self, db: str, stderr_file: Path):
        """Test 11: running twice with identical args → 1 row, count=2."""
        for _ in range(2):
            self._run(db, job="idempotent_job", exit_code=1,
                      stderr_file=str(stderr_file))
        rows = _fetch_errors(db)
        assert len(rows) == 1, f"Expected 1 row, got {len(rows)}"
        assert rows[0]["occurrence_count"] == 2, (
            f"Expected count=2, got {rows[0]['occurrence_count']}"
        )

    def test_different_exit_codes_different_fingerprints(
        self, db: str, stderr_file: Path
    ):
        """Test 12: exit code 1 vs exit code 2 → 2 distinct rows."""
        self._run(db, job="same_job", exit_code=1, stderr_file=str(stderr_file))
        self._run(db, job="same_job", exit_code=2, stderr_file=str(stderr_file))
        rows = _fetch_errors(db)
        assert len(rows) == 2, f"Expected 2 rows (different FPs), got {len(rows)}"
        fps = {r["fingerprint"] for r in rows}
        assert len(fps) == 2, "Fingerprints must differ by exit code"

    def test_missing_stderr_file_no_crash(self, db: str):
        """Test 15: --stderr-file pointing to nonexistent path → no crash, row inserted."""
        result = _run_capture_py(
            db_path=db,
            job="missing_stderr",
            exit_code=1,
            stderr_file="/tmp/this_does_not_exist_atlas_test_xyz.log",
        )
        assert result.returncode == 0, (
            f"Expected exit 0 on missing file; got {result.returncode}\nstderr={result.stderr}"
        )
        rows = _fetch_errors(db)
        # Row still inserted — message just lacks stderr content
        assert len(rows) == 1

    def test_long_stderr_truncated_to_8000(self, db: str, tmp_path: Path):
        """Test 16: message stored in DB is at most 8000 chars."""
        big_file = tmp_path / "big_stderr.log"
        # Write lines so _last_lines returns content (blank lines are filtered)
        big_file.write_text("\n".join(["x" * 200] * 60))
        _run_capture_py(
            db_path=db, job="big_job", exit_code=1, stderr_file=str(big_file)
        )
        rows = _fetch_errors(db)
        assert len(rows[0]["message"]) <= 8000, (
            f"Message length {len(rows[0]['message'])} exceeds 8000"
        )

    def test_missing_errors_table_no_crash(self, tmp_path: Path):
        """Test 14: errors table missing → Python script exits 0 gracefully."""
        db_path = str(tmp_path / "no_errors_table.db")
        conn = sqlite3.connect(db_path)
        conn.execute("CREATE TABLE IF NOT EXISTS dummy (id INTEGER PRIMARY KEY)")
        conn.commit()
        conn.close()

        result = _run_capture_py(
            db_path=db_path, job="no_table_job", exit_code=1,
        )
        assert result.returncode == 0, (
            f"Expected exit 0 on missing table; got {result.returncode}\nstderr={result.stderr}"
        )


# ══════════════════════════════════════════════════════════════════════════════
# Tests: End-to-end — bash wrapper calls Python script, which writes to DB
# ══════════════════════════════════════════════════════════════════════════════

class TestWrapperEndToEnd:
    """End-to-end tests: bash wrapper discovers and runs the Python script.

    To inspect the DB we need to redirect the Python script to a test DB.
    We achieve this by building a fake ATLAS_PROJECT tree that contains a
    thin shim in place of cron_stderr_to_errors.py.  The shim prepends
    ``--db <test.db>`` before forwarding all other args to the real script.
    """

    @pytest.fixture()
    def db(self, tmp_path: Path) -> str:
        return _make_db_with_errors(tmp_path)

    def _build_shim(self, tmp_path: Path, db_path: str) -> str:
        """Create a fake project tree with a shim and return the fake PROJECT root."""
        # The wrapper expects:  $ATLAS_PROJECT/scripts/cron_stderr_to_errors.py
        scripts_dir = tmp_path / "atlas_fake" / "scripts"
        scripts_dir.mkdir(parents=True, exist_ok=True)
        shim = scripts_dir / "cron_stderr_to_errors.py"
        shim.write_text(
            f'''#!/usr/bin/env python3
import subprocess, sys
real = {CAPTURE_PY!r}
db   = {db_path!r}
args = [sys.executable, real, "--db", db] + sys.argv[1:]
sys.exit(subprocess.run(args).returncode)
'''
        )
        shim.chmod(0o755)
        fake_project = tmp_path / "atlas_fake"
        return str(fake_project)

    def _run(
        self, db: str, job: str, cmd: list[str], tmp_path: Path
    ) -> subprocess.CompletedProcess:
        fake_project = self._build_shim(tmp_path, db)
        env = {**os.environ, "ATLAS_PROJECT": fake_project}
        return subprocess.run(
            [WRAPPER, job, *cmd],
            capture_output=True,
            text=True,
            env=env,
            timeout=20,
        )

    def test_e2e_exit_0_no_db_write(self, db: str, tmp_path: Path):
        """Test 1 (E2E): wrapper exits 0 AND no DB row for a passing command."""
        result = self._run(db, "ok_job", ["sh", "-c", "echo ok"], tmp_path)
        assert result.returncode == 0
        rows = _fetch_errors(db)
        assert len(rows) == 0, f"Expected 0 rows on success; got {rows}"

    def test_e2e_nonzero_inserts_row(self, db: str, tmp_path: Path):
        """Test 3 (E2E): bash wrapper calls Python on non-zero exit → DB row."""
        result = self._run(
            db, "fail_job", ["sh", "-c", "echo boom >&2; exit 3"], tmp_path
        )
        assert result.returncode == 3
        rows = _fetch_errors(db)
        assert len(rows) == 1, f"Expected 1 row; got {len(rows)}"
        assert rows[0]["source"] == "cron"
        assert rows[0]["service"] == "fail_job"

    def test_e2e_stderr_passthrough(self, db: str, tmp_path: Path):
        """Test 13 (E2E): stderr is visible to the parent even through the wrapper."""
        result = self._run(
            db, "pass_job",
            ["sh", "-c", "echo visible_error >&2; exit 1"],
            tmp_path,
        )
        assert "visible_error" in result.stderr, (
            f"Expected passthrough stderr; got: {result.stderr!r}"
        )
