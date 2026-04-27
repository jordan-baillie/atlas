"""Regression tests: pytest.ini and conftest.py hygiene.

Verifies:
1. No "Unknown config option" warnings on collection.
2. Collection succeeds (returncode 0) and reports tests collected.
3. Tests inside tests/archive/ are NOT collected (collect_ignore_glob working).

These are intentionally subprocess-based so they catch configuration
issues that only manifest during pytest startup — not importable at module
load time.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

PROJECT = Path(__file__).resolve().parent.parent


def _run_collect(extra_args: list[str] | None = None) -> subprocess.CompletedProcess:
    """Run pytest --collect-only and return the result."""
    cmd = [
        sys.executable, "-m", "pytest",
        "--collect-only", "-q",
        "--no-header",
        "--timeout=30",
    ] + (extra_args or [])
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        cwd=str(PROJECT),
        timeout=60,
    )


class TestPytestCollectionZeroWarnings:
    """Verify no 'Unknown config option' warnings fire during collection."""

    def test_no_unknown_config_option_warnings(self):
        result = _run_collect()
        # Warnings appear in stderr
        combined = result.stdout + result.stderr
        unknown_lines = [
            line for line in combined.splitlines()
            if "Unknown config option" in line
        ]
        assert not unknown_lines, (
            "pytest collection emits 'Unknown config option' warnings:\n"
            + "\n".join(unknown_lines)
        )


class TestPytestCollectionNoErrors:
    """Verify collection succeeds and reports a non-zero test count."""

    def test_collection_returns_zero(self):
        result = _run_collect()
        assert result.returncode == 0, (
            f"pytest --collect-only returned rc={result.returncode}.\n"
            f"stdout:\n{result.stdout[-2000:]}\n"
            f"stderr:\n{result.stderr[-1000:]}"
        )

    def test_collection_reports_tests(self):
        result = _run_collect()
        combined = result.stdout + result.stderr
        assert "test" in combined.lower() and "selected" not in combined.lower() or "collected" in combined.lower(), (
            "pytest --collect-only did not report any tests collected.\n"
            f"stdout tail: {result.stdout[-500:]}"
        )

    def test_collection_count_nonzero(self):
        """The collected count must be > 0."""
        result = _run_collect()
        import re
        m = re.search(r"(\d+) test", result.stdout + result.stderr)
        assert m and int(m.group(1)) > 0, (
            "pytest collected 0 tests — something is very wrong.\n"
            f"stdout: {result.stdout[-500:]}"
        )


class TestCollectIgnoreGlobApplied:
    """Verify tests inside tests/archive/ are NOT collected."""

    def test_archive_dir_has_test_files(self):
        """Prerequisite: tests/archive/ has at least one test file."""
        archive = PROJECT / "tests" / "archive"
        test_files = list(archive.glob("test_*.py")) if archive.exists() else []
        if not test_files:
            pytest.skip(
                "tests/archive/ has no test_*.py files — collect_ignore_glob check "
                "is trivially satisfied"
            )

    def test_archive_tests_not_in_collection(self):
        """Tests inside tests/archive/ must NOT appear in collection output."""
        archive = PROJECT / "tests" / "archive"
        if not archive.exists():
            pytest.skip("tests/archive/ does not exist")
        test_files = list(archive.glob("test_*.py"))
        if not test_files:
            pytest.skip("tests/archive/ has no test_*.py files")

        result = _run_collect()
        combined = result.stdout + result.stderr

        # Check each archive test file's name is NOT in the collected list
        archive_names = [f.stem for f in test_files]
        collected_archive = [
            line for line in combined.splitlines()
            if any(name in line for name in archive_names)
            and "tests/archive" in line
        ]
        assert not collected_archive, (
            "tests/archive/ test files ARE being collected (collect_ignore_glob broken):\n"
            + "\n".join(collected_archive)
        )
