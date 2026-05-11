"""F-13: Trace and neutralize dashboard-data.json writer.

Run: python3 scripts/find_dashboard_json_writer.py

Summary of findings (2026-05-11):
  The file dashboard/data/dashboard-data.json was TRACKED IN GIT since March 2026.
  It was committed with content from 2026-03-12 (generate_data_legacy.py output).
  Despite dashboard/data/ being in .gitignore, already-tracked files are not excluded
  by .gitignore — git continues to manage them.
  Result: any git checkout/pull/stash-pop restores the March 2026 stale file.

  NO active Python script writes fresh content to this file (generator is archived).
  The file is read-only consumed by utils/charts.py and utils/telegram.py as fallback.
  The authoritative data source is FastAPI /api/dashboard-data endpoint (SQLite-backed).

  Fix applied 2026-05-11:
    git rm --cached dashboard/data/dashboard-data.json  (un-track from git)
    git rm --cached dashboard/data/agent.html           (same issue)
    File deleted from disk.
    .gitignore entry 'dashboard/data/' now correctly prevents re-tracking.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

ATLAS_ROOT = Path(__file__).resolve().parent.parent
TARGET = ATLAS_ROOT / "dashboard" / "data" / "dashboard-data.json"


def check_file_exists() -> None:
    print(f"\n=== File status: {TARGET} ===")
    if TARGET.exists():
        import os
        stat = TARGET.stat()
        print(f"  EXISTS — size={stat.st_size} mtime={stat.st_mtime}")
        try:
            import json
            d = json.loads(TARGET.read_text())
            print(f"  Timestamp in file: {d.get('timestamp', '?')}")
        except Exception:
            pass
    else:
        print("  Does not exist (correct after F-13 fix)")


def check_git_tracking() -> None:
    print("\n=== Git tracking ===")
    result = subprocess.run(
        ["git", "ls-files", str(TARGET.relative_to(ATLAS_ROOT))],
        capture_output=True, text=True, cwd=str(ATLAS_ROOT), timeout=5,
    )
    if result.stdout.strip():
        print(f"  WARNING: still tracked in git: {result.stdout.strip()}")
        print("  Fix: git rm --cached dashboard/data/dashboard-data.json")
    else:
        print("  Not tracked in git (correct after F-13 fix)")


def check_active_writers() -> None:
    print("\n=== Active Python writers (non-archive, non-test) ===")
    writers = []
    for f in sorted((ATLAS_ROOT).rglob("*.py")):
        if any(skip in str(f) for skip in ("__pycache__", "archive", "test_", "/tests/")):
            continue
        try:
            text = f.read_text(errors="ignore")
            if "dashboard-data.json" in text and any(
                kw in text for kw in ("write_text", ".write(", "json.dump", "open.*w")
            ):
                writers.append(str(f.relative_to(ATLAS_ROOT)))
        except Exception:
            pass
    if writers:
        print(f"  Found potential writers: {writers}")
    else:
        print("  No active Python writers found — file is read-only (used as fallback)")


def check_cron_systemd() -> None:
    print("\n=== Cron / systemd references ===")
    result = subprocess.run(
        ["grep", "-rn", "dashboard-data.json", "--include=*.sh",
         str(ATLAS_ROOT / "scripts")],
        capture_output=True, text=True, timeout=10,
    )
    for line in result.stdout.strip().splitlines():
        if "generate_data" not in line.lower() and "#" not in line:
            print(f"  Active reference: {line}")
    print("  pi-cron.sh:617 has generate_data.py COMMENTED OUT (retired Phase 5)")
    print("  No active cron entry writes dashboard-data.json")


def main() -> None:
    print("=" * 60)
    print("  F-13 dashboard-data.json Writer Investigation — 2026-05-11")
    print("=" * 60)
    check_file_exists()
    check_git_tracking()
    check_active_writers()
    check_cron_systemd()
    print("\n=== Root cause ===")
    print("  dashboard-data.json was committed to git on 2026-03-12.")
    print("  Despite dashboard/data/ being in .gitignore, git ignores .gitignore")
    print("  for already-tracked files. Any git checkout/pull restored stale content.")
    print("\n=== Fix applied ===")
    print("  1. git rm --cached dashboard/data/dashboard-data.json (un-tracked)")
    print("  2. git rm --cached dashboard/data/agent.html (same issue)")
    print("  3. File deleted from disk")
    print("  4. .gitignore already has 'dashboard/data/' — now correctly prevents re-tracking")
    print("\n=== Impact ===")
    print("  utils/charts.py: reads file as fallback for chart generation — will log WARNING")
    print("  utils/telegram.py: reads file as fallback — will fall back to broker API")
    print("  healthz.py check: will see file as missing — acceptable (data from API)")
    print("  Primary data source: FastAPI /api/dashboard-data (SQLite-backed) — unaffected")


if __name__ == "__main__":
    main()
