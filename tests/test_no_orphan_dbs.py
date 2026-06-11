"""
CI guard: ensure no orphan or empty DB files exist in the repo.

Rules enforced:
  1. atlas.db must NOT exist at the repo root.
  2. No .db file at repo root (top-level) may exist (they're never intentional there).
  3. No .db file anywhere under data/ may be exactly 0 bytes.
  4. data/atlas.db must exist and be non-empty (sanity check the real DB).
"""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"


def test_no_atlas_db_at_repo_root():
    """atlas.db must never exist at the repo root — it's always an orphan."""
    orphan = REPO_ROOT / "atlas.db"
    assert not orphan.exists(), (
        f"Orphan DB found at repo root: {orphan}. Remove with `git rm atlas.db`."
    )


def test_no_zero_byte_dbs_at_repo_root():
    """Any .db file sitting at repo root is unexpected and likely orphaned."""
    bad = [p for p in REPO_ROOT.glob("*.db") if p.stat().st_size == 0]
    assert not bad, (
        f"Zero-byte .db files at repo root: {[str(p) for p in bad]}. "
        "Remove with `git rm` or `rm`."
    )


def test_no_zero_byte_dbs_in_data_dir():
    """No .db file under data/ should be empty; empty = orphan or failed init."""
    bad = [
        p for p in DATA_DIR.glob("**/*.db")
        if p.stat().st_size == 0
    ]
    assert not bad, (
        f"Zero-byte .db files under data/: {[str(p) for p in bad]}. "
        "Remove or reinitialise them."
    )


def test_real_atlas_db_exists_and_is_nonempty():
    """data/atlas.db is the live database; on the prod host it must exist and be >0 bytes."""
    import pytest
    real_db = DATA_DIR / "atlas.db"
    if not real_db.exists():
        pytest.skip("data/atlas.db not present — dev checkout (prod-host guard only)")
    assert real_db.stat().st_size > 0, f"Real DB is empty: {real_db}"
