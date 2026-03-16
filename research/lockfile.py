"""Evaluation lock — ensures backtest engine and data are immutable during a research session.

At session start: compute SHA-256 of all locked files and the data snapshot.
Before each experiment: re-verify hashes. If anything changed, abort the session.

Usage::

    from research.lockfile import (
        LOCKED_FILES, EvaluationLockViolation,
        compute_lock, verify_lock, save_lock, load_lock,
    )

    snapshot_dir = Path("data/snapshots/sp500_v3_unadj_20260310_7yr")
    lock = compute_lock(LOCKED_FILES, snapshot_dir)
    save_lock(lock, session_id="20260316_120000_mean_reversion")

    # Before each experiment:
    ok, changed = verify_lock(lock)
    if not ok:
        raise EvaluationLockViolation("Files changed", changed=changed)
"""

import hashlib
import json
import sys
from pathlib import Path
from typing import List, Tuple

ATLAS_ROOT = Path(__file__).resolve().parent.parent
if str(ATLAS_ROOT) not in sys.path:
    sys.path.insert(0, str(ATLAS_ROOT))

# ─── Locked Files ─────────────────────────────────────────────────────────────

#: Files that must remain immutable for the duration of a research session.
#: Any modification to these files invalidates experiment comparability.
LOCKED_FILES: List[Path] = [
    ATLAS_ROOT / "backtest" / "engine.py",
    ATLAS_ROOT / "backtest" / "metrics.py",
    ATLAS_ROOT / "backtest" / "filters.py",
    ATLAS_ROOT / "backtest" / "enrichment.py",
    ATLAS_ROOT / "backtest" / "pipeline.py",
    ATLAS_ROOT / "backtest" / "vol_scaling.py",
    ATLAS_ROOT / "strategies" / "base.py",
    ATLAS_ROOT / "utils" / "helpers.py",
    ATLAS_ROOT / "utils" / "allocation.py",
    ATLAS_ROOT / "scripts" / "strategy_evaluator.py",
]

#: Directory where session lock files are stored.
LOCKS_DIR = ATLAS_ROOT / "research" / "locks"

# ─── Exception ────────────────────────────────────────────────────────────────


class EvaluationLockViolation(Exception):
    """Raised when locked evaluation files change during a research session.

    Attributes:
        changed: List of file paths that differed from their locked hashes.
    """

    def __init__(self, message: str, changed: List[str] = None):
        super().__init__(message)
        self.changed = changed or []


# ─── Hashing ──────────────────────────────────────────────────────────────────

_CHUNK_SIZE = 65536  # 64 KB — efficient for large parquet files


def _sha256_file(path: Path) -> str:
    """Compute SHA-256 hex digest of a file, reading in 64 KB chunks.

    Returns an empty string if the file is missing or unreadable (which will
    compare as changed against any non-empty hash stored at lock time).
    """
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            while True:
                chunk = f.read(_CHUNK_SIZE)
                if not chunk:
                    break
                h.update(chunk)
        return h.hexdigest()
    except (OSError, IOError):
        return ""  # Missing or unreadable — counts as "changed"


# ─── Public API ───────────────────────────────────────────────────────────────


def compute_lock(locked_files: List[Path], snapshot_dir: Path) -> dict:
    """Compute SHA-256 hashes for locked source files and snapshot parquet files.

    This is called once at session start to establish a baseline fingerprint.

    Args:
        locked_files:  List of Path objects pointing to backtest engine and
                       strategy source files that must remain unchanged.
        snapshot_dir:  Path to the data snapshot directory.  All ``*.parquet``
                       files found recursively are included in the lock.

    Returns:
        Dict mapping ``str(file_path)`` → ``sha256_hex`` for all locked files
        plus every ``.parquet`` file found under ``snapshot_dir``.
    """
    lock: dict = {}

    # Hash all locked source files
    for file_path in locked_files:
        lock[str(file_path)] = _sha256_file(file_path)

    # Hash every parquet file in the snapshot directory
    if snapshot_dir.exists():
        for parquet_path in sorted(snapshot_dir.rglob("*.parquet")):
            lock[str(parquet_path)] = _sha256_file(parquet_path)

    return lock


def verify_lock(lock: dict) -> Tuple[bool, List[str]]:
    """Re-hash each file in *lock* and compare to stored digests.

    Missing files are considered changed (empty hash ≠ stored hash for any file
    that existed at lock time).

    Args:
        lock: Dict returned by :func:`compute_lock` (or loaded via
              :func:`load_lock`).

    Returns:
        ``(True, [])`` if all files match their stored hashes.
        ``(False, [path, ...])`` listing every file whose hash differs.
    """
    changed: List[str] = []

    for file_str, stored_hash in lock.items():
        current_hash = _sha256_file(Path(file_str))
        if current_hash != stored_hash:
            changed.append(file_str)

    return (len(changed) == 0, changed)


def save_lock(lock: dict, session_id: str) -> None:
    """Persist a lock dict to ``research/locks/{session_id}.json``.

    Creates the locks directory if it does not exist.

    Args:
        lock:       Lock dict from :func:`compute_lock`.
        session_id: Unique identifier for this research session (used as
                    the filename).  Typically ``YYYYMMDD_HHMMSS_{strategy}``.
    """
    LOCKS_DIR.mkdir(parents=True, exist_ok=True)
    lock_path = LOCKS_DIR / f"{session_id}.json"
    with open(lock_path, "w") as f:
        json.dump(lock, f, indent=2)


def load_lock(session_id: str) -> dict:
    """Load a previously saved lock from ``research/locks/{session_id}.json``.

    Args:
        session_id: Session identifier used when :func:`save_lock` was called.

    Returns:
        Lock dict mapping ``str(file_path)`` → ``sha256_hex``.

    Raises:
        FileNotFoundError: If the lock file does not exist.
        json.JSONDecodeError: If the lock file is malformed.
    """
    lock_path = LOCKS_DIR / f"{session_id}.json"
    with open(lock_path) as f:
        return json.load(f)
