"""
tests/test_cleanup_sediment.py

Tests for scripts/cleanup_sediment.py sediment retention logic.

Scenarios:
  1. dry-run: no files deleted from disk, audit JSON has correct counts + preview list
  2. apply: old-outside-top3 deleted; top-3 preserved; recent preserved
  3. idempotency: re-running on cleaned state is a no-op
  4. edge case: group with fewer than 3 files preserves all (even if old)
  5. edge case: empty pattern groups → no crash
"""

from __future__ import annotations

import datetime
import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "ops"))
import cleanup_sediment as cs


# ── Helpers ────────────────────────────────────────────────────────────────────

def _make_file(path: Path, age_days: float, size: int = 512) -> Path:
    """Create a file and backdate its mtime to `age_days` ago."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    mtime = (datetime.datetime.now() - datetime.timedelta(days=age_days)).timestamp()
    os.utime(path, (mtime, mtime))
    return path


# ── Fixture ────────────────────────────────────────────────────────────────────

@pytest.fixture
def sediment_tree(tmp_path: Path):
    """
    data/:
      live_sp500.json.pre-r1   (1d)  ← top-1
      live_sp500.json.pre-r2   (2d)  ← top-2
      live_sp500.json.pre-r3   (3d)  ← top-3
      live_sp500.json.pre-old1 (20d) ← outside top-3, old  → DELETE
      live_sp500.json.pre-old2 (25d) ← outside top-3, old  → DELETE
      live_sp500.json.bak.1      (5d)  ← top-1 (only one in bak group)

    data/:
      atlas.db.bak.new1   (1d)  ← top-1
      atlas.db.bak.new2   (2d)  ← top-2  (only 2 in db.bak group → both preserved)
    """
    bs = tmp_path / "data"
    _make_file(bs / "live_sp500.json.pre-r1", age_days=1)
    _make_file(bs / "live_sp500.json.pre-r2", age_days=2)
    _make_file(bs / "live_sp500.json.pre-r3", age_days=3)
    _make_file(bs / "live_sp500.json.pre-old1", age_days=20)
    _make_file(bs / "live_sp500.json.pre-old2", age_days=25)
    _make_file(bs / "live_sp500.json.bak.1", age_days=5)

    data = tmp_path / "data"
    _make_file(data / "atlas.db.bak.new1", age_days=1, size=1024 * 1024)
    _make_file(data / "atlas.db.bak.new2", age_days=2, size=1024 * 1024)

    return tmp_path


# ── Dry-run tests ──────────────────────────────────────────────────────────────

class TestDryRun:
    def test_no_files_removed_from_disk(
        self, sediment_tree: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """Dry-run must NOT delete any files from disk."""
        monkeypatch.setattr(cs, "PROJECT_ROOT", sediment_tree)
        monkeypatch.setattr(cs, "AUDIT_DIR", sediment_tree / "data" / "audit")

        cs.run(dry_run=True)

        bs = sediment_tree / "data"
        assert (bs / "live_sp500.json.pre-old1").exists(), "dry-run: old1 must not be removed"
        assert (bs / "live_sp500.json.pre-old2").exists(), "dry-run: old2 must not be removed"

    def test_audit_json_has_preview_of_deletions(
        self, sediment_tree: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """In dry-run, files_deleted in audit shows WOULD-BE deletions as a preview."""
        monkeypatch.setattr(cs, "PROJECT_ROOT", sediment_tree)
        monkeypatch.setattr(cs, "AUDIT_DIR", sediment_tree / "data" / "audit")

        audit = cs.run(dry_run=True)

        assert audit["dry_run"] is True
        # Both old files should appear in preview
        deleted_paths = [f["path"] for f in audit["files_deleted"]]
        assert any("pre-old1" in p for p in deleted_paths), "old1 must appear in dry-run preview"
        assert any("pre-old2" in p for p in deleted_paths), "old2 must appear in dry-run preview"

    def test_audit_examined_and_preserved_counts(
        self, sediment_tree: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """Audit JSON counts: 8 examined, 6 in top-3, 0 recent, 2 would-delete."""
        monkeypatch.setattr(cs, "PROJECT_ROOT", sediment_tree)
        monkeypatch.setattr(cs, "AUDIT_DIR", sediment_tree / "data" / "audit")

        audit = cs.run(dry_run=True)

        # 5 pre + 1 bak + 2 db.bak = 8 files
        assert audit["files_examined"] == 8
        # top-3: r1+r2+r3 (pre), bak (bak), new1+new2 (db.bak) = 6
        assert len(audit["files_preserved_top3"]) == 6
        # 2 would-be deletions (old1, old2)
        assert len(audit["files_deleted"]) == 2


# ── Apply tests ────────────────────────────────────────────────────────────────

class TestApply:
    def test_old_outside_top3_deleted(
        self, sediment_tree: Path, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setattr(cs, "PROJECT_ROOT", sediment_tree)
        monkeypatch.setattr(cs, "AUDIT_DIR", sediment_tree / "data" / "audit")

        audit = cs.run(dry_run=False)

        bs = sediment_tree / "data"
        assert not (bs / "live_sp500.json.pre-old1").exists(), "old1 must be deleted"
        assert not (bs / "live_sp500.json.pre-old2").exists(), "old2 must be deleted"
        assert len(audit["files_deleted"]) == 2

    def test_top3_preserved_regardless_of_age(
        self, sediment_tree: Path, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setattr(cs, "PROJECT_ROOT", sediment_tree)
        monkeypatch.setattr(cs, "AUDIT_DIR", sediment_tree / "data" / "audit")
        cs.run(dry_run=False)

        bs = sediment_tree / "data"
        assert (bs / "live_sp500.json.pre-r1").exists()
        assert (bs / "live_sp500.json.pre-r2").exists()
        assert (bs / "live_sp500.json.pre-r3").exists()

    def test_recent_outside_top3_not_deleted(
        self, sediment_tree: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """A 4th file that is recent (10d) is outside top-3 but within 14d window → kept."""
        bs = sediment_tree / "data"
        _make_file(bs / "live_sp500.json.pre-mid", age_days=10)
        # pre group now: r1(1d)=T1, r2(2d)=T2, r3(3d)=T3, mid(10d)=recent-not-top3
        # old1(20d) + old2(25d) still deleted

        monkeypatch.setattr(cs, "PROJECT_ROOT", sediment_tree)
        monkeypatch.setattr(cs, "AUDIT_DIR", sediment_tree / "data" / "audit")
        audit = cs.run(dry_run=False)

        assert (bs / "live_sp500.json.pre-mid").exists(), "recent-but-outside-top3 must be kept"
        assert not (bs / "live_sp500.json.pre-old1").exists()
        assert not (bs / "live_sp500.json.pre-old2").exists()
        assert len(audit["files_preserved_recent"]) >= 1


# ── Idempotency test ───────────────────────────────────────────────────────────

class TestIdempotency:
    def test_second_apply_is_noop(
        self, sediment_tree: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """Running --apply twice: second run deletes nothing new."""
        monkeypatch.setattr(cs, "PROJECT_ROOT", sediment_tree)
        monkeypatch.setattr(cs, "AUDIT_DIR", sediment_tree / "data" / "audit")

        audit1 = cs.run(dry_run=False)
        audit2 = cs.run(dry_run=False)

        assert len(audit2["files_deleted"]) == 0, (
            f"second apply must delete nothing; got {audit2['files_deleted']}"
        )
        # First run may have deleted old files
        assert len(audit1["files_deleted"]) >= len(audit2["files_deleted"])


# ── Edge case tests ────────────────────────────────────────────────────────────

class TestEdgeCases:
    def test_fewer_than_3_files_all_preserved(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """Group with only 1 file (very old) must be preserved via top-3 rule."""
        bs = tmp_path / "data"
        _make_file(bs / "live_sp500.json.pre-only-one", age_days=60)

        monkeypatch.setattr(cs, "PROJECT_ROOT", tmp_path)
        monkeypatch.setattr(cs, "AUDIT_DIR", tmp_path / "data" / "audit")
        audit = cs.run(dry_run=False)

        assert (bs / "live_sp500.json.pre-only-one").exists(), (
            "sole file in group must be preserved regardless of age"
        )
        assert len(audit["files_deleted"]) == 0

    def test_empty_patterns_no_crash(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """No matching files in any pattern → zero examined, zero deleted, no crash."""
        monkeypatch.setattr(cs, "PROJECT_ROOT", tmp_path)
        monkeypatch.setattr(cs, "AUDIT_DIR", tmp_path / "data" / "audit")
        audit = cs.run(dry_run=False)

        assert audit["files_examined"] == 0
        assert audit["files_deleted"] == []
        assert audit["files_preserved_top3"] == []
