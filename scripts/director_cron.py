#!/usr/bin/env python3
"""Atlas Director Cron — Automated Queue Management and Portfolio Review

A standalone Python script (NOT an LLM agent) that:
1. Reads vault state: queue depth, recent journal results, coverage map
2. Generates new experiments when queue is running dry (< 5 pending)
3. Runs portfolio optimizer weekly (checks last run date)
4. Sends daily digest via Telegram
5. Writes heartbeat to /tmp/director-heartbeat.json

Design: Deterministic logic only. All LLM-guided strategy decisions
remain in scripts/principal.py. This script is purely mechanical maintenance.

Deployment (systemd timer — runs at 08:00 and 20:00 AEST daily):
    # Install service + timer from systemd/ directory:
    cp systemd/atlas-director.service /etc/systemd/system/
    cp systemd/atlas-director.timer /etc/systemd/system/
    systemctl daemon-reload
    systemctl enable --now atlas-director.timer

Usage:
    python3 scripts/director_cron.py              # Normal run
    python3 scripts/director_cron.py --dry-run    # Show what would happen
    python3 scripts/director_cron.py --force-portfolio  # Force portfolio optimizer
    python3 scripts/director_cron.py --force-discovery  # Force experiment generation
"""

import argparse
import json
import logging
import os
import subprocess
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from utils.logging_config import setup_logging

logger = setup_logging("director_cron")

# ── Constants ────────────────────────────────────────────────────────────────

HEARTBEAT_PATH       = Path("/tmp/director-heartbeat.json")
QUEUE_PATH           = PROJECT_ROOT / "research" / "queue.json"
JOURNAL_PATH         = PROJECT_ROOT / "research" / "journal.json"
LAST_PORTFOLIO_RUN   = PROJECT_ROOT / "research" / "vault" / ".last_portfolio_optimizer_run"
STATE_PATH           = PROJECT_ROOT / "research" / "vault" / ".director_state.json"

# Thresholds
MIN_QUEUE_DEPTH      = 5    # Generate more experiments when below this
PORTFOLIO_OPT_DAYS   = 7    # Run portfolio optimizer at most once per week
MAX_EXPERIMENTS_GEN  = 10   # Max experiments to generate per run


# ── Heartbeat ────────────────────────────────────────────────────────────────

def _write_heartbeat(
    status: str = "running",
    phase: str = "reviewing",
    queue_depth: int = 0,
    experiments_queued: int = 0,
    portfolio_sharpe: float = 0.0,
    coverage_pct: int = 0,
    activity: str = "reviewing",
    detail: str = "",
) -> None:
    """Write director state to heartbeat file (atomic)."""
    hb = {
        "timestamp": datetime.now(timezone.utc).astimezone().isoformat(),
        "status": status,
        "phase": phase,
        "queue_depth": queue_depth,
        "experiments_queued": experiments_queued,
        "portfolio_sharpe": portfolio_sharpe,
        "coverage_pct": coverage_pct,
        "activity": activity,
        "detail": detail,
    }
    tmp = HEARTBEAT_PATH.with_suffix(".tmp")
    try:
        with open(tmp, "w") as f:
            json.dump(hb, f, indent=2, default=str)
        os.replace(tmp, HEARTBEAT_PATH)
        logger.debug("Heartbeat written: phase=%s detail=%s", phase, detail)
    except Exception as e:
        logger.warning("Heartbeat write failed: %s", e)


# ── State persistence ────────────────────────────────────────────────────────

def _load_state() -> dict:
    """Load persistent director state (last run times etc.)."""
    if not STATE_PATH.exists():
        return {}
    try:
        with open(STATE_PATH) as f:
            return json.load(f)
    except Exception:
        return {}


def _save_state(state: dict) -> None:
    """Persist director state."""
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".tmp")
    try:
        with open(tmp, "w") as f:
            json.dump(state, f, indent=2, default=str)
        os.replace(tmp, STATE_PATH)
    except Exception as e:
        logger.warning("State save failed: %s", e)


# ── Queue analysis ────────────────────────────────────────────────────────────

def _read_json(path: Path, default=None):
    """Safely read a JSON file."""
    if default is None:
        default = []
    if not path.exists():
        return default
    try:
        with open(path) as f:
            return json.load(f)
    except Exception as e:
        logger.warning("Could not read %s: %s", path, e)
        return default


def get_queue_stats() -> dict:
    """Analyse queue.json and return summary statistics."""
    queue = _read_json(QUEUE_PATH, [])
    if not isinstance(queue, list):
        queue = []

    total      = len(queue)
    queued     = sum(1 for e in queue if e.get("status") == "queued")
    running    = sum(1 for e in queue if e.get("status") in ("running", "claimed"))
    passed     = sum(1 for e in queue if e.get("status") == "passed")
    failed     = sum(1 for e in queue if e.get("status") == "failed")
    by_priority: dict[str, int] = {}
    for e in queue:
        if e.get("status") == "queued":
            p = e.get("priority", "P5")
            by_priority[p] = by_priority.get(p, 0) + 1

    return {
        "total": total,
        "queued": queued,
        "running": running,
        "passed": passed,
        "failed": failed,
        "by_priority": by_priority,
    }


def get_recent_journal_stats(days: int = 7) -> dict:
    """Read last N days of journal and compute pass rate, key findings."""
    journal = _read_json(JOURNAL_PATH, [])
    if not isinstance(journal, list):
        journal = []

    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    recent = []
    for entry in journal:
        ts_str = entry.get("timestamp", "")
        if not ts_str:
            continue
        try:
            ts = datetime.fromisoformat(ts_str)
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            if ts >= cutoff:
                recent.append(entry)
        except (ValueError, TypeError):
            continue

    n = len(recent)
    passes   = sum(1 for e in recent if e.get("verdict") == "pass")
    fails    = sum(1 for e in recent if e.get("verdict") in ("fail", "failed"))
    partials = sum(1 for e in recent if e.get("verdict") == "partial")

    # Best Sharpe found recently
    best_sharpe = 0.0
    best_strategy = None
    for entry in recent:
        metrics = entry.get("key_metrics") or {}
        sh = metrics.get("sharpe") or metrics.get("combined_sharpe", 0)
        if sh and sh > best_sharpe:
            best_sharpe = sh
            best_strategy = entry.get("strategy")

    return {
        "days": days,
        "total": n,
        "passes": passes,
        "fails": fails,
        "partials": partials,
        "pass_rate": round(passes / n * 100, 1) if n > 0 else 0.0,
        "best_sharpe": round(best_sharpe, 3),
        "best_strategy": best_strategy,
    }


def get_coverage_stats() -> dict:
    """Compute strategy coverage across lifecycle stages."""
    from research.discovery import STRATEGY_UNIVERSE
    total_strategies = len(STRATEGY_UNIVERSE)
    status_counts: dict[str, int] = {}
    for info in STRATEGY_UNIVERSE.values():
        s = info.get("status", "unknown")
        status_counts[s] = status_counts.get(s, 0) + 1

    active    = status_counts.get("active", 0)
    dormant   = status_counts.get("dormant", 0)
    tested    = sum(v for k, v in status_counts.items()
                    if k in ("active", "dormant", "screening", "solo",
                              "optimize", "combined", "oos"))
    pct = round(tested / total_strategies * 100) if total_strategies else 0
    return {
        "total": total_strategies,
        "active": active,
        "dormant": dormant,
        "tested": tested,
        "coverage_pct": pct,
        "by_status": status_counts,
    }


# ── Experiment generation ────────────────────────────────────────────────────

def generate_more_experiments(max_count: int = MAX_EXPERIMENTS_GEN,
                               dry_run: bool = False) -> int:
    """Generate new experiments via discovery.py when queue is running dry.

    Returns number of experiments added to queue.
    """
    try:
        from research.discovery import queue_discovery_batch
    except ImportError as e:
        logger.error("Cannot import discovery module: %s", e)
        return 0

    if dry_run:
        logger.info("[dry-run] Would generate up to %d new experiments", max_count)
        return 0

    try:
        added = queue_discovery_batch(max_count=max_count)
        logger.info("Generated %d new experiments via discovery", added)
        return added
    except Exception as e:
        logger.error("Experiment generation failed: %s", e)
        return 0


# ── Portfolio optimizer ──────────────────────────────────────────────────────

def _days_since_portfolio_run(state: dict) -> Optional[float]:
    """Return days since last portfolio optimizer run, or None if never run."""
    last_str = state.get("last_portfolio_run")
    if not last_str:
        # Also check the sentinel file
        if LAST_PORTFOLIO_RUN.exists():
            try:
                mtime = LAST_PORTFOLIO_RUN.stat().st_mtime
                delta = (datetime.now(timezone.utc).timestamp() - mtime) / 86400
                return round(delta, 1)
            except Exception:
                pass
        return None
    try:
        last = datetime.fromisoformat(last_str)
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - last).days
    except (ValueError, TypeError):
        return None


def run_portfolio_optimizer(dry_run: bool = False, equity: float = 25000) -> bool:
    """Run portfolio_optimizer.py --vault. Returns True on success."""
    cmd = [
        sys.executable,
        str(PROJECT_ROOT / "research" / "portfolio_optimizer.py"),
        "--vault",
        "--zero-commission",
        "--equity", str(equity),
    ]
    logger.info("Running: %s", " ".join(cmd))

    if dry_run:
        logger.info("[dry-run] Would run: %s", " ".join(cmd))
        return True

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=3600,  # 1 hour max
            cwd=str(PROJECT_ROOT),
        )
        if proc.returncode == 0:
            logger.info("Portfolio optimizer completed successfully")
            # Update sentinel file
            LAST_PORTFOLIO_RUN.parent.mkdir(parents=True, exist_ok=True)
            LAST_PORTFOLIO_RUN.write_text(datetime.now(timezone.utc).isoformat())
            return True
        else:
            logger.error(
                "Portfolio optimizer failed (rc=%d): %s",
                proc.returncode,
                proc.stderr[-500:] if proc.stderr else "",
            )
            return False
    except subprocess.TimeoutExpired:
        logger.error("Portfolio optimizer timed out after 1 hour")
        return False
    except Exception as e:
        logger.error("Portfolio optimizer error: %s", e)
        return False


# ── Telegram digest ──────────────────────────────────────────────────────────

def send_digest(queue_stats: dict, journal_stats: dict, coverage: dict,
                experiments_generated: int = 0, portfolio_ran: bool = False,
                dry_run: bool = False) -> None:
    """Send daily director digest via Telegram."""
    if dry_run:
        logger.info("[dry-run] Would send Telegram digest")
        return

    try:
        from utils.telegram import send_message

        q   = queue_stats
        j   = journal_stats
        cov = coverage

        pass_rate_str = f"{j['pass_rate']:.0f}%" if j.get("total", 0) > 0 else "—"
        best_str = ""
        if j.get("best_strategy"):
            best_str = (
                f"\n📊 Best: <code>{j['best_strategy']}</code> "
                f"Sharpe={j['best_sharpe']:.3f}"
            )

        gen_str = ""
        if experiments_generated > 0:
            gen_str = f"\n🔄 Generated {experiments_generated} new experiments"

        port_str = ""
        if portfolio_ran:
            port_str = "\n🏦 Portfolio optimizer completed"

        msg = (
            f"🎬 <b>Director Daily Digest</b>\n"
            f"\n"
            f"📋 <b>Queue</b>: {q['queued']} pending "
            f"(P1={q['by_priority'].get('P1', 0)}, "
            f"P2={q['by_priority'].get('P2', 0)}, "
            f"P3={q['by_priority'].get('P3', 0)})\n"
            f"   Total: {q['total']} | Passed: {q['passed']} | Failed: {q['failed']}\n"
            f"\n"
            f"🔬 <b>Research ({j['days']}d)</b>: {j['total']} experiments\n"
            f"   Pass: {j['passes']} | Fail: {j['fails']} | Rate: {pass_rate_str}"
            f"{best_str}\n"
            f"\n"
            f"🗺 <b>Coverage</b>: {cov['coverage_pct']}% "
            f"({cov['active']} active, {cov['dormant']} dormant / {cov['total']} total)"
            f"{gen_str}{port_str}"
        )
        send_message(msg)
        logger.info("Telegram digest sent")
    except Exception as e:
        logger.debug("Telegram digest failed (non-critical): %s", e)


# ── Main ─────────────────────────────────────────────────────────────────────

def parse_args():
    parser = argparse.ArgumentParser(description="Atlas Director Cron")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would happen without making changes")
    parser.add_argument("--force-portfolio", action="store_true",
                        help="Force portfolio optimizer run regardless of schedule")
    parser.add_argument("--force-discovery", action="store_true",
                        help="Force experiment generation regardless of queue depth")
    parser.add_argument("--no-telegram", action="store_true",
                        help="Skip Telegram notification")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    dry_run = args.dry_run

    if dry_run:
        logger.info("=== Director Cron [DRY-RUN] ===")
    else:
        logger.info("=== Director Cron starting ===")

    _write_heartbeat(
        status="running",
        phase="reviewing",
        activity="reviewing",
        detail="Starting director cron",
    )

    # ── 1. Gather state ──────────────────────────────────────────
    state = _load_state()

    _write_heartbeat(
        status="running",
        phase="reviewing",
        activity="reviewing",
        detail="Reading queue and journal",
    )

    queue_stats   = get_queue_stats()
    journal_stats = get_recent_journal_stats(days=7)

    try:
        coverage = get_coverage_stats()
    except Exception as e:
        logger.warning("Coverage stats failed: %s", e)
        coverage = {"total": 0, "active": 0, "dormant": 0, "tested": 0,
                    "coverage_pct": 0, "by_status": {}}

    _write_heartbeat(
        status="running",
        phase="reviewing",
        queue_depth=queue_stats["queued"],
        coverage_pct=coverage.get("coverage_pct", 0),
        activity="reviewing",
        detail=(
            f"Queue: {queue_stats['queued']} pending | "
            f"Coverage: {coverage.get('coverage_pct', 0)}% | "
            f"Pass rate (7d): {journal_stats['pass_rate']:.0f}%"
        ),
    )

    logger.info(
        "State: queue=%d pending, coverage=%d%%, pass_rate=%s%%",
        queue_stats["queued"],
        coverage.get("coverage_pct", 0),
        journal_stats["pass_rate"],
    )

    # ── 2. Generate experiments if queue is running dry ──────────
    experiments_generated = 0
    should_generate = (
        args.force_discovery
        or queue_stats["queued"] < MIN_QUEUE_DEPTH
    )

    if should_generate:
        reason = (
            "forced" if args.force_discovery
            else f"queue depth {queue_stats['queued']} < {MIN_QUEUE_DEPTH}"
        )
        logger.info("Generating experiments (%s)", reason)

        _write_heartbeat(
            status="running",
            phase="queuing",
            queue_depth=queue_stats["queued"],
            activity="typing",
            detail=f"Queue running dry ({queue_stats['queued']} pending) — generating",
        )

        experiments_generated = generate_more_experiments(
            max_count=MAX_EXPERIMENTS_GEN,
            dry_run=dry_run,
        )

        if experiments_generated > 0:
            queue_stats["queued"] += experiments_generated
            logger.info("Added %d experiments to queue", experiments_generated)
    else:
        logger.info(
            "Queue healthy (%d pending) — skipping generation",
            queue_stats["queued"]
        )

    # ── 3. Portfolio optimizer (weekly) ─────────────────────────
    portfolio_ran = False
    days_since = _days_since_portfolio_run(state)
    should_run_portfolio = (
        args.force_portfolio
        or days_since is None
        or days_since >= PORTFOLIO_OPT_DAYS
    )

    if should_run_portfolio:
        reason = (
            "forced" if args.force_portfolio
            else f"last run {days_since} days ago" if days_since is not None
            else "never run"
        )
        logger.info("Running portfolio optimizer (%s)", reason)

        _write_heartbeat(
            status="running",
            phase="portfolio",
            queue_depth=queue_stats["queued"],
            activity="typing",
            detail=f"Running portfolio optimizer ({reason})",
        )

        portfolio_ran = run_portfolio_optimizer(dry_run=dry_run)

        if portfolio_ran and not dry_run:
            state["last_portfolio_run"] = datetime.now(timezone.utc).isoformat()
            _save_state(state)
    else:
        logger.info(
            "Portfolio optimizer: skipped (ran %.1f days ago, threshold=%d days)",
            days_since, PORTFOLIO_OPT_DAYS,
        )

    # ── 4. Telegram digest ───────────────────────────────────────
    if not args.no_telegram:
        _write_heartbeat(
            status="running",
            phase="reporting",
            queue_depth=queue_stats["queued"],
            activity="writing",
            detail="Sending Telegram digest",
        )
        send_digest(
            queue_stats=queue_stats,
            journal_stats=journal_stats,
            coverage=coverage,
            experiments_generated=experiments_generated,
            portfolio_ran=portfolio_ran,
            dry_run=dry_run,
        )

    # ── 5. Final heartbeat ───────────────────────────────────────
    _write_heartbeat(
        status="idle",
        phase="idle",
        queue_depth=queue_stats["queued"],
        experiments_queued=experiments_generated,
        coverage_pct=coverage.get("coverage_pct", 0),
        activity="reviewing",
        detail=(
            f"Done: {experiments_generated} generated, "
            f"portfolio={'ran' if portfolio_ran else 'skipped'}, "
            f"queue={queue_stats['queued']} pending"
        ),
    )

    logger.info("=== Director Cron complete ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
