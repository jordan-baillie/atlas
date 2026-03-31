#!/usr/bin/env python3
"""Run Atlas research discovery.

Usage:
    python3 research/discovery/run.py              # Daily (uses rotation)
    python3 research/discovery/run.py --full       # Full sweep all sources
    python3 research/discovery/run.py --source arxiv  # Specific source override
    python3 research/discovery/run.py --status     # Show cumulative stats
    python3 research/discovery/run.py --dry-run    # Show what would run today
"""

import argparse
import json
import logging
import os
import signal
import subprocess
import sys
from pathlib import Path
from datetime import datetime, timezone

DISCOVERY_DIR = Path(__file__).resolve().parent
ATLAS_ROOT = DISCOVERY_DIR.parent.parent
if str(ATLAS_ROOT) not in sys.path:
    sys.path.insert(0, str(ATLAS_ROOT))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("discovery.run")

# PIDs of processes we start so we can clean them up on exit
_started_pids: list = []

# ─── Paths ────────────────────────────────────────────────────────────────────

CUMULATIVE_STATS = DISCOVERY_DIR / "cumulative_stats.json"
PAPERS_DIR = DISCOVERY_DIR / "papers"
SPECS_DIR = DISCOVERY_DIR / "specs"
LOGS_DIR = DISCOVERY_DIR / "logs"


# ─── Signal handler ───────────────────────────────────────────────────────────

def _cleanup_and_exit(signum, frame):
    """Kill any browser / Xvfb we started, then exit cleanly."""
    logger.info("Signal %d received — cleaning up", signum)
    for pid in _started_pids:
        try:
            os.kill(pid, signal.SIGTERM)
            logger.debug("Killed PID %d", pid)
        except (ProcessLookupError, PermissionError):
            pass
    sys.exit(0)


signal.signal(signal.SIGINT, _cleanup_and_exit)
signal.signal(signal.SIGTERM, _cleanup_and_exit)


# ─── Browser / Xvfb helpers ──────────────────────────────────────────────────

def _ensure_xvfb() -> bool:
    """Start Xvfb :99 if not already running. Returns True if now running."""
    result = subprocess.run(["pgrep", "-f", "Xvfb :99"], capture_output=True)
    if result.returncode == 0:
        logger.debug("Xvfb :99 already running")
        return True
    try:
        proc = subprocess.Popen(
            ["Xvfb", ":99", "-screen", "0", "1920x1080x24"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        _started_pids.append(proc.pid)
        logger.info("Started Xvfb :99 (PID %d)", proc.pid)
        return True
    except FileNotFoundError:
        logger.warning("Xvfb not found — computer_use may not work")
        return False


def _ensure_chromium() -> bool:
    """Start Chromium on display :99 if not already running."""
    result = subprocess.run(["pgrep", "-f", "chromium.*:99"], capture_output=True)
    if result.returncode == 0:
        logger.debug("Chromium already running on :99")
        return True
    env = dict(os.environ, DISPLAY=":99")
    try:
        chromium_cmd = ["chromium-browser", "--no-sandbox", "--headless=new",
                        "--disable-gpu", "--remote-debugging-port=9222"]
        # Also try 'chromium' on systems where that's the binary name
        for binary in ("chromium-browser", "chromium", "google-chrome"):
            result = subprocess.run(["which", binary], capture_output=True)
            if result.returncode == 0:
                chromium_cmd[0] = binary
                break
        proc = subprocess.Popen(
            chromium_cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=env,
        )
        _started_pids.append(proc.pid)
        logger.info("Started chromium on :99 (PID %d)", proc.pid)
        return True
    except FileNotFoundError:
        logger.warning("chromium not found — computer_use may not work")
        return False


# ─── Output formatting ────────────────────────────────────────────────────────

def _print_report(report) -> None:
    """Print a DailyReport in a human-readable format."""
    print("\n" + "=" * 60)
    print(f"  Atlas Discovery Report — {report.date}")
    print("=" * 60)
    print(f"  Source:   {report.source} ({report.method})")
    print(f"  Papers found:      {report.papers_found}")
    print(f"  Papers filtered:   {report.papers_filtered}")
    print(f"  Specs extracted:   {report.specs_extracted}")
    print(f"  Strategies generated ({len(report.strategies_generated)}):")
    for name in report.strategies_generated:
        marker = "✅" if name in report.strategies_passed_quickcheck else "❌"
        print(f"    {marker} {name}")
    print(f"  Passed quick-check: {len(report.strategies_passed_quickcheck)}")
    if report.errors:
        print(f"\n  Errors ({len(report.errors)}):")
        for err in report.errors:
            print(f"    ⚠️  {err}")
    print(f"\n  Runtime: {report.runtime_s:.1f}s")
    print("=" * 60 + "\n")


def _show_status() -> None:
    """Print cumulative discovery stats."""
    if not CUMULATIVE_STATS.exists():
        print("No stats yet. Run discovery first.")
        # Create an empty stats file
        CUMULATIVE_STATS.write_text(json.dumps({
            "total_runs": 0, "papers_found": 0, "papers_filtered": 0,
            "specs_extracted": 0, "strategies_generated": 0,
            "strategies_passed_quickcheck": 0, "monthly": {},
        }, indent=2))
        print(f"Created: {CUMULATIVE_STATS}")
        return

    try:
        stats = json.loads(CUMULATIVE_STATS.read_text())
    except (json.JSONDecodeError, OSError) as e:
        print(f"Error reading stats: {e}")
        return

    print("\n" + "=" * 60)
    print("  Atlas Discovery — Cumulative Stats")
    print("=" * 60)
    print(f"  Total runs:            {stats.get('total_runs', 0)}")
    print(f"  Papers found:          {stats.get('papers_found', 0)}")
    print(f"  Papers filtered:       {stats.get('papers_filtered', 0)}")
    print(f"  Specs extracted:       {stats.get('specs_extracted', 0)}")
    print(f"  Strategies generated:  {stats.get('strategies_generated', 0)}")
    print(f"  Passed quick-check:    {stats.get('strategies_passed_quickcheck', 0)}")
    print(f"  Last run:              {stats.get('last_run', 'never')}")

    monthly = stats.get("monthly", {})
    if monthly:
        print("\n  Monthly breakdown:")
        for month in sorted(monthly.keys(), reverse=True)[:6]:
            mo = monthly[month]
            print(
                f"    {month}: {mo.get('runs', 0)} runs | "
                f"{mo.get('papers_found', 0)} papers | "
                f"{mo.get('strategies_generated', 0)} generated | "
                f"{mo.get('strategies_passed', 0)} passed"
            )
    print("=" * 60 + "\n")


def _show_dry_run() -> None:
    """Show what would run today without executing anything."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    print("\n" + "=" * 60)
    print(f"  Atlas Discovery — Dry Run ({today})")
    print("=" * 60)

    source = {"name": "arxiv", "method": "api", "queries": []}
    try:
        from research.discovery.sources import get_today_source, get_queries_for_source
        source = get_today_source()
        if "queries" not in source:
            source["queries"] = get_queries_for_source(source)
    except ImportError:
        print("  ⚠️  sources module not yet available — showing defaults")
    except Exception as e:
        print(f"  ⚠️  Could not load source: {e}")

    print(f"  Source:   {source.get('name', 'arxiv')}")
    print(f"  Method:   {source.get('method', 'api')}")
    queries = source.get("queries", [])
    if queries:
        print(f"  Queries ({len(queries)}):")
        for q in queries:
            print(f"    • {q}")
    else:
        print("  Queries:  (none configured yet)")

    print(f"\n  Would write to:")
    print(f"    papers/  → {PAPERS_DIR}")
    print(f"    specs/   → {SPECS_DIR}")
    print(f"    logs/    → {LOGS_DIR}")
    print(f"    log:     → {DISCOVERY_DIR / 'daily_log.jsonl'}")
    print("=" * 60 + "\n")


# ─── Ensure output directories ────────────────────────────────────────────────

def _ensure_dirs() -> None:
    """Create required output directories if they don't exist."""
    for d in [PAPERS_DIR, SPECS_DIR, LOGS_DIR]:
        d.mkdir(parents=True, exist_ok=True)


# ─── Entry point ──────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Atlas Research Discovery — academic paper to strategy pipeline",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--full", action="store_true",
                        help="Full sweep: iterate through all configured sources")
    parser.add_argument("--source", metavar="NAME",
                        help="Override source for today's run (e.g. arxiv, ssrn, blog_qstrat)")
    parser.add_argument("--status", action="store_true",
                        help="Show cumulative discovery stats and exit")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would run today without executing")
    parser.add_argument("--no-telegram", action="store_true",
                        help="Skip Telegram digest even on success")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="Enable debug logging")
    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    _ensure_dirs()

    # ── --status ──────────────────────────────────────────────────────────
    if args.status:
        _show_status()
        return 0

    # ── --dry-run ─────────────────────────────────────────────────────────
    if args.dry_run:
        _show_dry_run()
        return 0

    # ── Detect whether today needs computer_use (requires Xvfb + Chromium) ─
    needs_browser = False
    if not args.full:
        try:
            from research.discovery.sources import get_today_source
            source = get_today_source()
            if source.get("method") == "computer_use":
                needs_browser = True
        except Exception:
            pass

    if args.source:
        # If user overrides source, check method
        if "ssrn" in args.source or "blog" in args.source:
            needs_browser = True

    if needs_browser:
        logger.info("computer_use day — ensuring Xvfb and Chromium are running")
        _ensure_xvfb()
        _ensure_chromium()

    # ── --full sweep ──────────────────────────────────────────────────────
    if args.full:
        logger.info("Full sweep mode — running all sources")
        from research.discovery.discovery import discover_full
        reports = discover_full()
        for report in reports:
            _print_report(report)
        total_generated = sum(len(r.strategies_generated) for r in reports)
        print(f"Full sweep complete: {len(reports)} sources, {total_generated} strategies generated")
        return 0

    # ── Daily run ─────────────────────────────────────────────────────────
    logger.info("Starting daily discovery run")
    from research.discovery.discovery import discover_daily
    report = discover_daily()
    _print_report(report)

    # Return non-zero if there were errors (but still consider it a soft-fail)
    return 1 if report.errors else 0


if __name__ == "__main__":
    sys.exit(main())
