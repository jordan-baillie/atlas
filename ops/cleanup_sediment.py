#!/usr/bin/env python3
"""
scripts/cleanup_sediment.py — Operational sediment retention / cleanup

Glob patterns scanned:
  data/atlas.db.bak*       (DB backups labelled bak)
  data/atlas.db.pre-*      (DB pre-operation snapshots)
  data/*.json.bak.*        (JSON backups)
  data/*.json.pre-*        (JSON pre-operation snapshots)

Retention policy per pattern group:
  - Preserve the most-recent 3 files regardless of age (safety net).
  - Delete any remaining file whose mtime is > RETENTION_DAYS old.
  - Files < RETENTION_DAYS old but outside top-3: keep (active recovery window).

Usage:
  python3 scripts/cleanup_sediment.py --dry-run   # default: show what would be deleted
  python3 scripts/cleanup_sediment.py --apply     # actually delete

Writes audit JSON to data/audit/sediment_cleanup_<ISO_ts>.json.
Exits 0 always (cron-friendly).
"""

from __future__ import annotations

import argparse
import datetime
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

# ── Constants ──────────────────────────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).parent.parent
AUDIT_DIR = PROJECT_ROOT / "data" / "audit"

# Each tuple: (human_label, glob_pattern_relative_to_project)
PATTERNS: list[tuple[str, str]] = [
    ("db_bak", "data/atlas.db.bak*"),
    ("db_pre", "data/atlas.db.pre-*"),
    ("json_bak", "data/*.json.bak.*"),
    ("json_pre", "data/*.json.pre-*"),
]

# Preserve this many most-recent files per pattern group regardless of age
TOP_K: int = 3

# Delete files older than this many days (outside the top-K set)
RETENTION_DAYS: int = 14

logger = logging.getLogger(__name__)


# ── Helpers ────────────────────────────────────────────────────────────────────

def _file_record(path: Path) -> dict[str, Any]:
    """Build a dict describing a single file."""
    stat = path.stat()
    return {
        "path": str(path.relative_to(PROJECT_ROOT)),
        "size_bytes": stat.st_size,
        "mtime": datetime.datetime.fromtimestamp(stat.st_mtime, tz=datetime.timezone.utc).isoformat(),
    }


def scan_pattern_group(
    label: str,
    pattern: str,
    cutoff: datetime.datetime,
    dry_run: bool,
) -> tuple[list[dict], list[dict], list[dict], list[dict]]:
    """
    Scan one glob pattern group.

    Returns four lists: (would_delete, top3_preserved, recent_preserved, all_files)
    """
    root = PROJECT_ROOT
    all_paths = sorted(root.glob(pattern), key=lambda p: p.stat().st_mtime, reverse=True)

    top3: list[Path] = all_paths[:TOP_K]
    rest: list[Path] = all_paths[TOP_K:]

    would_delete: list[dict] = []
    top3_records: list[dict] = []
    recent_records: list[dict] = []
    all_records: list[dict] = [_file_record(p) for p in all_paths]

    for p in top3:
        top3_records.append(_file_record(p))

    for p in rest:
        mtime_dt = datetime.datetime.fromtimestamp(p.stat().st_mtime, tz=datetime.timezone.utc)
        if mtime_dt < cutoff:
            # Old AND outside top-3 → candidate for deletion
            rec = _file_record(p)
            would_delete.append(rec)
            if not dry_run:
                try:
                    p.unlink()
                    logger.info("Deleted %s (%d bytes)", rec["path"], rec["size_bytes"])
                except OSError as e:
                    logger.warning("Could not delete %s: %s", rec["path"], e)
        else:
            # Recent but outside top-3 → keep (active recovery window)
            recent_records.append(_file_record(p))

    return would_delete, top3_records, recent_records, all_records


# ── Main ───────────────────────────────────────────────────────────────────────

def run(dry_run: bool = True) -> dict[str, Any]:
    """Execute sediment cleanup and return the audit payload."""
    now_utc = datetime.datetime.now(tz=datetime.timezone.utc)
    cutoff = now_utc - datetime.timedelta(days=RETENTION_DAYS)

    all_deleted: list[dict] = []
    all_top3: list[dict] = []
    all_recent: list[dict] = []
    total_examined = 0

    for label, pattern in PATTERNS:
        deleted, top3, recent, all_files = scan_pattern_group(label, pattern, cutoff, dry_run)
        all_deleted.extend(deleted)
        all_top3.extend(top3)
        all_recent.extend(recent)
        total_examined += len(all_files)

    audit: dict[str, Any] = {
        "timestamp": now_utc.isoformat(),
        "dry_run": dry_run,
        "retention_days": RETENTION_DAYS,
        "top_k": TOP_K,
        "patterns_scanned": [pat for _, pat in PATTERNS],
        "files_examined": total_examined,
        "files_deleted": all_deleted,
        "files_preserved_top3": all_top3,
        "files_preserved_recent": all_recent,
    }

    # Write audit JSON
    AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    ts_str = now_utc.strftime("%Y-%m-%dT%H%M%SZ")
    audit_path = AUDIT_DIR / f"sediment_cleanup_{ts_str}.json"
    try:
        audit_path.write_text(json.dumps(audit, indent=2))
    except OSError as e:
        logger.warning("Could not write audit JSON: %s", e)

    return audit


def _print_summary(audit: dict[str, Any]) -> None:
    """Print a human-readable summary."""
    mode = "DRY-RUN" if audit["dry_run"] else "APPLIED"
    print(f"\n=== Sediment Cleanup [{mode}] ===")
    print(f"  Timestamp    : {audit['timestamp']}")
    print(f"  Files scanned: {audit['files_examined']}")
    print(f"  Top-3 kept   : {len(audit['files_preserved_top3'])}")
    print(f"  Recent kept  : {len(audit['files_preserved_recent'])}")

    if audit["files_deleted"]:
        action = "Would delete" if audit["dry_run"] else "Deleted"
        total_bytes = sum(f["size_bytes"] for f in audit["files_deleted"])
        print(f"  {action}     : {len(audit['files_deleted'])} files "
              f"({total_bytes / 1_048_576:.1f} MB)")
        for f in audit["files_deleted"]:
            print(f"    - {f['path']}  ({f['size_bytes']:,} bytes, mtime={f['mtime']})")
    else:
        print("  Nothing to delete — state is clean.")

    print()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Operational sediment cleanup")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--dry-run", action="store_true", default=True,
                       help="Print what would be deleted without deleting (default)")
    group.add_argument("--apply", action="store_true", default=False,
                       help="Actually delete eligible files")
    args = parser.parse_args(argv)

    dry_run = not args.apply
    audit = run(dry_run=dry_run)
    _print_summary(audit)

    # Confirm audit location
    if audit.get("files_examined", 0) >= 0:
        ts_str = audit["timestamp"].replace(":", "").replace("-", "")[:15] + "Z"
        print(f"  Audit JSON   : data/audit/sediment_cleanup_{ts_str}.json")

    sys.exit(0)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    main()
