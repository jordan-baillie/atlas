"""
tests/test_doc_staleness.py

Tests for scripts/check_doc_staleness.py.

Scenarios:
  1. Both files fresh (<30d) → exit 0
  2. One file stale (>30d) → exit 1 with filename in output
  3. Both files stale (>30d) → exit 1, both named
  4. One file missing → exit 1 with MISSING in output
  5. --dry-run flag: stale file → prints STALE but still exits 0
"""

from __future__ import annotations

import datetime
import os
import sys
from io import StringIO
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
import check_doc_staleness as cds


# ── Helpers ────────────────────────────────────────────────────────────────────

def _make_file(path: Path, age_days: float) -> Path:
    """Create a file and set its mtime to `age_days` ago."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("content")
    mtime = (datetime.datetime.now() - datetime.timedelta(days=age_days)).timestamp()
    os.utime(path, (mtime, mtime))
    return path


def _run_main(
    tmp_path: Path,
    argv: list[str] | None = None,
    monkeypatch: pytest.MonkeyPatch | None = None,
) -> tuple[int, str]:
    """Run check_doc_staleness.main() and capture stdout + return code."""
    if monkeypatch:
        monkeypatch.setattr(
            cds,
            "MONITORED_FILES",
            [
                tmp_path / "docs" / "KNOWLEDGE_INDEX.md",
                tmp_path / "research" / "brain" / "SUMMARY.md",
            ],
        )

    captured = StringIO()
    original_stdout = sys.stdout
    sys.stdout = captured
    try:
        rc = cds.main(argv or [])
    finally:
        sys.stdout = original_stdout

    return rc, captured.getvalue()


# ── Tests ──────────────────────────────────────────────────────────────────────

class TestBothFresh:
    def test_exits_zero_when_both_fresh(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        _make_file(tmp_path / "docs" / "KNOWLEDGE_INDEX.md", age_days=1)
        _make_file(tmp_path / "research" / "brain" / "SUMMARY.md", age_days=5)

        rc, output = _run_main(tmp_path, monkeypatch=monkeypatch)

        assert rc == 0, f"expected exit 0, got {rc}. Output:\n{output}"
        assert "OK" in output

    def test_output_mentions_both_files(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        _make_file(tmp_path / "docs" / "KNOWLEDGE_INDEX.md", age_days=2)
        _make_file(tmp_path / "research" / "brain" / "SUMMARY.md", age_days=3)

        _, output = _run_main(tmp_path, monkeypatch=monkeypatch)

        assert "KNOWLEDGE_INDEX.md" in output
        assert "SUMMARY.md" in output


class TestStaleFile:
    def test_one_stale_exits_one(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        _make_file(tmp_path / "docs" / "KNOWLEDGE_INDEX.md", age_days=31)
        _make_file(tmp_path / "research" / "brain" / "SUMMARY.md", age_days=5)

        rc, output = _run_main(tmp_path, monkeypatch=monkeypatch)

        assert rc == 1, f"expected exit 1, got {rc}"
        assert "STALE" in output
        assert "KNOWLEDGE_INDEX.md" in output

    def test_stale_output_mentions_age_and_threshold(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        _make_file(tmp_path / "docs" / "KNOWLEDGE_INDEX.md", age_days=35)
        _make_file(tmp_path / "research" / "brain" / "SUMMARY.md", age_days=2)

        _, output = _run_main(tmp_path, monkeypatch=monkeypatch)

        # Should mention the age and threshold
        assert "35d" in output or "STALE" in output

    def test_other_file_ok_still_shown(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        _make_file(tmp_path / "docs" / "KNOWLEDGE_INDEX.md", age_days=40)
        _make_file(tmp_path / "research" / "brain" / "SUMMARY.md", age_days=1)

        _, output = _run_main(tmp_path, monkeypatch=monkeypatch)

        assert "OK" in output          # SUMMARY is fresh
        assert "STALE" in output       # KNOWLEDGE_INDEX is stale

    def test_both_stale_exits_one(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        _make_file(tmp_path / "docs" / "KNOWLEDGE_INDEX.md", age_days=60)
        _make_file(tmp_path / "research" / "brain" / "SUMMARY.md", age_days=45)

        rc, output = _run_main(tmp_path, monkeypatch=monkeypatch)

        assert rc == 1
        assert output.count("STALE") == 2


class TestMissingFile:
    def test_missing_file_exits_one(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """If one monitored file doesn't exist → exit 1 with MISSING message."""
        _make_file(tmp_path / "research" / "brain" / "SUMMARY.md", age_days=1)
        # KNOWLEDGE_INDEX.md intentionally NOT created

        rc, output = _run_main(tmp_path, monkeypatch=monkeypatch)

        assert rc == 1, f"expected exit 1, got {rc}"
        assert "MISSING" in output
        assert "KNOWLEDGE_INDEX.md" in output


class TestDryRunFlag:
    def test_stale_with_dry_run_exits_zero(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """--dry-run: stale files reported but exit code is 0."""
        _make_file(tmp_path / "docs" / "KNOWLEDGE_INDEX.md", age_days=40)
        _make_file(tmp_path / "research" / "brain" / "SUMMARY.md", age_days=1)

        rc, output = _run_main(tmp_path, argv=["--dry-run"], monkeypatch=monkeypatch)

        assert rc == 0, f"--dry-run must exit 0 even when stale; got {rc}"
        assert "STALE" in output, "--dry-run must still report stale status"
