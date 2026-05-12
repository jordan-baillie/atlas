#!/usr/bin/env python3
"""
scripts/check_doc_staleness.py — Check staleness of auto-regenerated docs

Monitored files:
  docs/KNOWLEDGE_INDEX.md
  research/brain/SUMMARY.md

Exit codes:
  0 — all files fresh (mtime ≤ STALE_DAYS)
  1 — at least one file is stale or missing

Usage:
  python3 scripts/check_doc_staleness.py            # check and exit
  python3 scripts/check_doc_staleness.py --dry-run  # print status, exit 0 always
"""

from __future__ import annotations

import argparse
import datetime
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent

STALE_DAYS: int = 30

MONITORED_FILES: list[Path] = [
    PROJECT_ROOT / "docs" / "KNOWLEDGE_INDEX.md",
    PROJECT_ROOT / "research" / "brain" / "SUMMARY.md",
]


def _display_name(path: Path) -> str:
    """Return a relative path for display; fall back to filename if not under PROJECT_ROOT."""
    try:
        return str(path.relative_to(PROJECT_ROOT))
    except ValueError:
        # In tests path may be in tmp_path
        return path.name


def check_file(path: Path, now: datetime.datetime) -> tuple[bool, str]:
    """
    Returns (is_ok, message).
    is_ok=True if file exists and is fresh.
    """
    name = _display_name(path)

    if not path.exists():
        return False, f"MISSING: {name}"

    mtime = datetime.datetime.fromtimestamp(path.stat().st_mtime, tz=datetime.timezone.utc)
    age_days = (now - mtime).days

    if age_days > STALE_DAYS:
        return False, f"STALE: {name} (age {age_days}d, threshold {STALE_DAYS}d)"
    return True, f"OK: {name} (age {age_days}d)"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check auto-regen doc staleness")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Print status without exiting 1 on stale files",
    )
    args = parser.parse_args(argv)

    now = datetime.datetime.now(tz=datetime.timezone.utc)
    all_ok = True

    for monitored in MONITORED_FILES:
        is_ok, message = check_file(monitored, now)
        print(message)
        if not is_ok:
            all_ok = False

    if all_ok:
        print("\nAll monitored docs are fresh.")
        return 0
    else:
        print(f"\nOne or more docs require regeneration.")
        print("  Run: python3 scripts/regen_knowledge_index.py")
        print("  Run: python3 scripts/regen_brain_summary.py")
        if args.dry_run:
            return 0
        return 1


if __name__ == "__main__":
    sys.exit(main())
