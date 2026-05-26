#!/usr/bin/env python3
"""Block committing Atlas runtime artifacts.

Atlas live/research jobs generate mutable JSON, DB, plan, cache, backup, and
report files under tracked-looking paths. Those files must remain on disk but
outside git so daily operations do not dirty the working tree.

Default mode checks staged additions/modifications/renames. Use
`--all-tracked` after a hygiene sweep to verify no runtime artifacts remain in
Git's index.
"""

from __future__ import annotations

import argparse
import fnmatch
import subprocess
import sys
from pathlib import Path

# Keep this list intentionally path-specific. Do not use broad patterns such as
# "*backup*" because source files like systemd/atlas-backup.service are durable.
RUNTIME_PATTERNS: tuple[str, ...] = (
    ".pi/**",
    "brokers/state/**",
    "backups-pre-batch-*",
    "backups-pre-batch-LATEST.txt",
    "config/.oos_cache/**",
    "config/active/**/*.bak*",
    "config/active_config_backup_*.json",
    "config/audit_log/**",
    "config/auto_excluded_tickers.json",
    "config/pending_promotions.json",
    "config/promotion_log.json",
    "dashboard/cache/**",
    "dashboard/data/**",
    "data/.sync_*",
    "data/*.db",
    "data/*.db-*",
    "data/*.bak*",
    "data/*_state.json",
    "data/*_verification.json",
    "data/*_comparison_*.json",
    "data/atlas_backup_*.db",
    "data/backups/**",
    "data/audit/**",
    "data/compute_matrix/**",
    "data/contaminated_backtests/**",
    "data/plan_notifications_buffer/**",
    "data/processed/**",
    "data/snapshots/**",
    "data/parity_alert_cooldown.json",
    "data/price_arbiter_alert_throttle.json",
    "data/promotion_log.json",
    "plans/plan_*.json",
    "research/best/**",
    "research/brain/Portfolio/**",
    "research/brain/decisions/promotion_*.md",
    "research/brain/state.json",
    "research/brain/staleness_reset.json",
    "research/brain/strategies/**",
    "research/journal.json",
    "research/queue.json",
    "research/results/**",
    "*.bak",
    "*.bak.*",
    "*.done",
    "*.log",
    "*.json.pre-*",
    "*.db.pre-*",
    "*.py.pre-*",
)

# These tracked files look operational but are source-of-truth code/config/docs.
ALLOWLIST: tuple[str, ...] = (
    "systemd/atlas-backup.service",
    "systemd/atlas-backup.timer",
    "ops/backup-all-projects.sh",
    "scripts/git-hooks/check_no_runtime_artifacts.py",
)


def _git(args: list[str]) -> list[str]:
    result = subprocess.run(["git", *args], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        print(result.stderr.strip(), file=sys.stderr)
        raise SystemExit(result.returncode)
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def _matches(path: str) -> bool:
    normalized = path.replace("\\", "/")
    if normalized in ALLOWLIST:
        return False
    return any(fnmatch.fnmatchcase(normalized, pattern) for pattern in RUNTIME_PATTERNS)


def _staged_paths() -> list[str]:
    # D deletions are allowed — removing runtime artifacts from git is the point.
    return _git(["diff", "--cached", "--name-only", "--diff-filter=ACMR"])


def _tracked_paths() -> list[str]:
    return _git(["ls-files"])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--all-tracked",
        action="store_true",
        help="scan all tracked files instead of only staged additions/modifications",
    )
    args = parser.parse_args()

    if not Path(".git").exists():
        print("check_no_runtime_artifacts.py must be run from the repo root", file=sys.stderr)
        return 2

    paths = _tracked_paths() if args.all_tracked else _staged_paths()
    offenders = sorted(path for path in paths if _matches(path))
    if not offenders:
        return 0

    scope = "tracked" if args.all_tracked else "staged"
    print(f"🚫 Runtime/generated Atlas artifacts are {scope} in git:", file=sys.stderr)
    for path in offenders[:200]:
        print(f"  - {path}", file=sys.stderr)
    if len(offenders) > 200:
        print(f"  ... and {len(offenders) - 200} more", file=sys.stderr)
    print("", file=sys.stderr)
    print("Keep these files on disk but remove them from git:", file=sys.stderr)
    print("  git rm --cached -- <path>  # or add a narrower .gitignore pattern", file=sys.stderr)
    print("", file=sys.stderr)
    print("Emergency override: ALLOW_RUNTIME_ARTIFACTS=1 git commit ...", file=sys.stderr)
    if not args.all_tracked and sys.argv and "ALLOW_RUNTIME_ARTIFACTS" in __import__("os").environ:
        print("ALLOW_RUNTIME_ARTIFACTS is set; allowing commit.", file=sys.stderr)
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
