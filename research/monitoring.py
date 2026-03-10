#!/usr/bin/env python3
"""Atlas Research Monitoring — digests, dashboards, health checks, maintenance.

Provides:
1. Daily digest (Telegram): experiments run, passes/fails, key findings, queue depth
2. Weekly summary (Telegram): coverage map, best strategies, growth stats
3. Dashboard.md: auto-refreshed after each experiment batch
4. Health checks: data staleness, disk space, memory, daemon status
5. Coverage Map: strategy × lifecycle stage matrix
6. Log rotation: archive old logs/experiments
"""

import sys
from pathlib import Path
ATLAS_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ATLAS_ROOT))

import json
import logging
import os
import shutil
import gzip
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Any, Optional

logger = logging.getLogger("monitoring")

VAULT_ROOT = ATLAS_ROOT / "research" / "vault"
JOURNAL_PATH = ATLAS_ROOT / "research" / "journal.json"
QUEUE_PATH = ATLAS_ROOT / "research" / "queue.json"
HEARTBEAT_PATH = Path("/tmp/research-daemon-heartbeat.json")
LOG_DIR = ATLAS_ROOT / "logs"
EXPERIMENTS_DIR = ATLAS_ROOT / "research" / "experiments"
DATA_DIR = ATLAS_ROOT / "data" / "snapshots"


# ─── Helpers ────────────────────────────────────────────────────────────────

def _read_json(path: Path) -> Any:
    """Safely read a JSON file; return empty list/dict on error."""
    if not path.exists():
        return [] if path.suffix == ".json" else {}
    try:
        with open(path) as f:
            return json.load(f)
    except Exception as e:
        logger.warning("Failed to read %s: %s", path, e)
        return []


def _read_heartbeat() -> Optional[dict]:
    """Read daemon heartbeat file."""
    if not HEARTBEAT_PATH.exists():
        return None
    try:
        with open(HEARTBEAT_PATH) as f:
            return json.load(f)
    except Exception:
        return None


def _get_daemon_status() -> tuple[str, str]:
    """Return (status_label, uptime_str) from heartbeat."""
    hb = _read_heartbeat()
    if hb is None:
        return "⛔ offline", "—"

    ts_str = hb.get("timestamp") or hb.get("ts") or ""
    if not ts_str:
        return "⚠️ no timestamp", "—"

    try:
        ts = datetime.fromisoformat(ts_str)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        age = datetime.now(timezone.utc) - ts
        age_min = age.total_seconds() / 60
        if age_min < 5:
            status = "✅ running"
        elif age_min < 30:
            status = "⚠️ stale"
        else:
            status = "⛔ dead"

        started = hb.get("started") or hb.get("start_time") or ""
        if started:
            try:
                s_ts = datetime.fromisoformat(started)
                if s_ts.tzinfo is None:
                    s_ts = s_ts.replace(tzinfo=timezone.utc)
                uptime_h = (datetime.now(timezone.utc) - s_ts).total_seconds() / 3600
                uptime_str = f"{uptime_h:.1f}h"
            except Exception:
                uptime_str = "—"
        else:
            uptime_str = "—"

        return status, uptime_str
    except Exception:
        return "⚠️ unknown", "—"


def _count_hypotheses() -> dict:
    """Count hypotheses by status from vault/Hypotheses/ directory."""
    hyp_dir = VAULT_ROOT / "Hypotheses"
    counts = defaultdict(int)
    if not hyp_dir.exists():
        return {}
    for md_file in hyp_dir.glob("*.md"):
        try:
            content = md_file.read_text()
            for line in content.splitlines():
                line = line.strip()
                if line.startswith("status:"):
                    status = line.split(":", 1)[1].strip().strip('"').strip("'")
                    counts[status] += 1
                    break
        except Exception:
            pass
    return dict(counts)


def _get_data_age_hours() -> float:
    """Return age in hours of the newest snapshot directory."""
    if not DATA_DIR.exists():
        return 999.0
    entries = list(DATA_DIR.iterdir())
    if not entries:
        return 999.0
    newest = max(entries, key=lambda p: p.stat().st_mtime)
    age = datetime.now().timestamp() - newest.stat().st_mtime
    return age / 3600


def _get_disk_free_gb() -> float:
    """Return free disk space in GB."""
    try:
        usage = shutil.disk_usage(str(ATLAS_ROOT))
        return usage.free / (1024 ** 3)
    except Exception:
        return 0.0


def _get_memory_info() -> tuple[float, float]:
    """Return (used_gb, total_gb) from /proc/meminfo."""
    try:
        meminfo = Path("/proc/meminfo").read_text()
        values = {}
        for line in meminfo.splitlines():
            parts = line.split()
            if len(parts) >= 2:
                key = parts[0].rstrip(":")
                val = int(parts[1])  # kB
                values[key] = val
        total_kb = values.get("MemTotal", 0)
        avail_kb = values.get("MemAvailable", values.get("MemFree", 0))
        used_kb = total_kb - avail_kb
        return used_kb / (1024 ** 2), total_kb / (1024 ** 2)
    except Exception:
        return 0.0, 0.0


def _queue_stats(entries: list) -> dict:
    """Summarise queue entries by status and priority."""
    stats = {
        "queued": 0, "running": 0, "claimed": 0, "failed": 0, "done": 0,
        "P1": 0, "P2": 0, "P3": 0, "P4": 0,
    }
    for e in entries:
        st = e.get("status", "queued")
        if st in stats:
            stats[st] += 1
        p = e.get("priority", "P3")
        if p in stats:
            stats[p] += 1
    return stats


def _get_lifecycle_stage(journal_entries: list, strategy: str) -> tuple[str, str]:
    """
    Determine the highest lifecycle stage reached for a strategy from journal.

    Lifecycle order: screen → quick → solo → optimize → combined → oos → promote

    Returns (stage_label, icon).
    """
    STAGE_ORDER = ["screen", "quick", "solo", "optimize", "combined", "oos", "promote"]
    STAGE_ICON = {
        "pass": "✅",
        "fail": "❌",
        "partial": "⚠️",
        "running": "🔄",
        None: "—",
    }

    # Map experiment categories/types to stages
    CATEGORY_TO_STAGE = {
        "screening": "screen",
        "screen": "screen",
        "quick": "quick",
        "quick_screen": "quick",
        "solo": "solo",
        "optimize": "optimize",
        "optimization": "optimize",
        "combined": "combined",
        "oos": "oos",
        "out_of_sample": "oos",
        "promote": "promote",
        "promoted": "promote",
    }

    # Filter entries for this strategy
    strat_entries = [e for e in journal_entries if e.get("strategy") == strategy]

    if not strat_entries:
        return "—", "—"

    # Find highest stage
    best_stage_idx = -1
    best_verdict = None

    for entry in strat_entries:
        category = entry.get("category", "").lower()
        exp_id = entry.get("experiment_id", "").lower()
        verdict = entry.get("verdict")

        # Infer stage from category or experiment_id
        stage = CATEGORY_TO_STAGE.get(category)
        if not stage:
            for kw, s in CATEGORY_TO_STAGE.items():
                if kw in exp_id:
                    stage = s
                    break

        # Default solo if unclassifiable but has data
        if not stage:
            stage = "solo"

        idx = STAGE_ORDER.index(stage) if stage in STAGE_ORDER else 0
        if idx > best_stage_idx:
            best_stage_idx = idx
            best_verdict = verdict

    if best_stage_idx < 0:
        return "—", "—"

    stage_name = STAGE_ORDER[best_stage_idx]
    icon = STAGE_ICON.get(best_verdict, "—")
    return stage_name, icon


def _build_strategy_leaderboard(journal_entries: list, top_n: int = 10) -> list:
    """Build leaderboard: top strategies by best Sharpe."""
    best_by_strategy = {}
    for entry in journal_entries:
        strat = entry.get("strategy") or "unknown"
        if strat == "unknown":
            continue
        metrics = entry.get("key_metrics") or {}
        sharpe = metrics.get("sharpe", metrics.get("sharpe_ratio", None))
        if sharpe is None:
            continue
        if strat not in best_by_strategy or sharpe > best_by_strategy[strat]["sharpe"]:
            best_by_strategy[strat] = {
                "strategy": strat,
                "sharpe": sharpe,
                "win_rate": metrics.get("win_rate_pct", metrics.get("win_rate", 0)),
                "trades": metrics.get("total_trades", 0),
                "verdict": entry.get("verdict", "—"),
            }

    sorted_lb = sorted(best_by_strategy.values(), key=lambda x: x["sharpe"], reverse=True)
    return sorted_lb[:top_n]


# ─── Public API ─────────────────────────────────────────────────────────────

def send_daily_digest(date_str: str = None) -> bool:
    """Send daily research digest via Telegram.

    Pulls data from:
    - journal.json: today's experiments (filter by timestamp)
    - queue.json: queue depth
    - heartbeat: daemon status
    - vault/Hypotheses/: hypothesis status counts

    Returns True if sent successfully.
    """
    from utils.telegram import send_message

    if date_str is None:
        date_str = datetime.now().strftime("%Y-%m-%d")

    journal = _read_json(JOURNAL_PATH)
    today_entries = [e for e in journal if e.get("timestamp", "").startswith(date_str)]

    # Verdict counts
    passes = sum(1 for e in today_entries if e.get("verdict") == "pass")
    fails = sum(1 for e in today_entries if e.get("verdict") == "fail")
    partials = sum(1 for e in today_entries if e.get("verdict") == "partial")
    total = len(today_entries)

    # Best Sharpe today
    best_sharpe = None
    best_strat = "—"
    for e in today_entries:
        m = e.get("key_metrics") or {}
        s = m.get("sharpe", m.get("sharpe_ratio"))
        if s is not None:
            if best_sharpe is None or s > best_sharpe:
                best_sharpe = s
                best_strat = e.get("strategy") or "unknown"

    # Average win rate today
    win_rates = [
        (e.get("key_metrics") or {}).get("win_rate_pct", 0)
        for e in today_entries
        if (e.get("key_metrics") or {}).get("win_rate_pct") is not None
    ]
    avg_wr = sum(win_rates) / len(win_rates) if win_rates else 0.0

    # Queue
    queue = _read_json(QUEUE_PATH)
    if not isinstance(queue, list):
        queue = []
    qstats = _queue_stats(queue)

    # Daemon status
    daemon_status, uptime = _get_daemon_status()

    # Data age
    data_age = _get_data_age_hours()

    # Disk / memory
    disk_free = _get_disk_free_gb()
    mem_used, mem_total = _get_memory_info()

    # Hypotheses
    hyp = _count_hypotheses()
    hyp_open = hyp.get("proposed", 0) + hyp.get("open", 0)
    hyp_testing = hyp.get("testing", 0) + hyp.get("in_progress", 0)
    hyp_confirmed = hyp.get("confirmed", 0)
    hyp_rejected = hyp.get("rejected", 0)

    # Key findings (top 3 passing strategies with best Sharpe)
    findings = []
    passing = sorted(
        [e for e in today_entries if e.get("verdict") in ("pass", "partial")],
        key=lambda e: (e.get("key_metrics") or {}).get("sharpe", -999),
        reverse=True,
    )
    for e in passing[:3]:
        strat = e.get("strategy", "?")
        m = e.get("key_metrics") or {}
        s = m.get("sharpe", m.get("sharpe_ratio"))
        wr = m.get("win_rate_pct", 0)
        icon = "✅" if e.get("verdict") == "pass" else "⚠️"
        line = f"{icon} {strat}"
        if s is not None:
            line += f" — Sharpe {s:.2f}"
        if wr:
            line += f", WR {wr:.0f}%"
        findings.append(line)
    if not findings:
        findings.append("No passing experiments today")

    sharpe_str = f"{best_sharpe:.2f}" if best_sharpe is not None else "—"

    lines = [
        f"🔬 <b>Atlas Research Digest — {date_str}</b>",
        "",
        "📊 <b>Today's Results</b>",
        f"Experiments: {total} ({passes}✅ {fails}❌ {partials}⚠️)",
        f"Best Sharpe: {best_strat} ({sharpe_str})",
        f"Avg Win Rate: {avg_wr:.0f}%",
        "",
        "📋 <b>Queue Status</b>",
        f"Queued: {qstats['queued']} | Running: {qstats['running']} | Claimed: {qstats['claimed']}",
        "",
        "🔍 <b>Key Findings</b>",
    ] + [f"• {f}" for f in findings] + [
        "",
        "🧠 <b>Hypotheses</b>",
        f"Open: {hyp_open} | Testing: {hyp_testing} | Confirmed: {hyp_confirmed} | Rejected: {hyp_rejected}",
        "",
        "⚙️ <b>System</b>",
        f"Daemon: {daemon_status} | Uptime: {uptime} | Data age: {data_age:.1f}h",
        f"Disk: {disk_free:.1f}GB free | Memory: {mem_used:.1f}/{mem_total:.1f}GB",
    ]

    return send_message("\n".join(lines))


def send_weekly_summary() -> bool:
    """Send weekly research summary (Sunday digest).

    Returns True if sent.
    """
    from utils.telegram import send_message
    from research.discovery import STRATEGY_UNIVERSE

    now = datetime.now(timezone.utc)
    week_start = now - timedelta(days=7)
    week_start_str = week_start.strftime("%Y-%m-%d")
    week_end_str = now.strftime("%Y-%m-%d")

    journal = _read_json(JOURNAL_PATH)
    if not isinstance(journal, list):
        journal = []

    week_entries = [
        e for e in journal
        if e.get("timestamp", "") >= week_start_str
    ]

    total = len(week_entries)
    passes = sum(1 for e in week_entries if e.get("verdict") == "pass")
    pass_pct = (passes / total * 100) if total else 0.0

    # Leaderboard (top 5 by best Sharpe this week)
    lb = _build_strategy_leaderboard(week_entries, top_n=5)

    # Coverage stats
    all_strats = set(STRATEGY_UNIVERSE.keys())
    tested_strats = set(e.get("strategy") for e in journal if e.get("strategy"))
    new_this_week = set(e.get("strategy") for e in week_entries if e.get("strategy")) - \
                    set(e.get("strategy") for e in journal
                        if e.get("timestamp", "") < week_start_str and e.get("strategy"))

    # Lifecycle breakdown (from full journal)
    stage_counts = defaultdict(int)
    for strat in tested_strats:
        stage, _ = _get_lifecycle_stage(journal, strat)
        stage_counts[stage] += 1

    # Promotion pipeline
    oos_ready = []
    opt_ready = []
    for strat in tested_strats:
        stage, icon = _get_lifecycle_stage(journal, strat)
        if stage == "oos" and icon == "✅":
            oos_ready.append(strat)
        elif stage in ("optimize", "combined") and icon == "✅":
            opt_ready.append(strat)

    best_strat_name = lb[0]["strategy"] if lb else "—"
    best_sharpe_val = lb[0]["sharpe"] if lb else 0.0

    # Build leaderboard section
    lb_lines = []
    for i, row in enumerate(lb, 1):
        wr = row.get("win_rate", 0) or 0
        lb_lines.append(
            f"{i}. {row['strategy']}: Sharpe {row['sharpe']:.2f}, "
            f"WR {wr:.0f}%, {row['trades']} trades"
        )

    stage_breakdown = " | ".join(
        f"{stage}: {cnt}" for stage, cnt in sorted(stage_counts.items())
        if stage != "—"
    ) or "none"

    lines = [
        "📊 <b>Atlas Weekly Research Summary</b>",
        f"Week of {week_start_str} — {week_end_str}",
        "",
        "📈 <b>Activity</b>",
        f"Total experiments: {total}",
        f"Pass rate: {pass_pct:.0f}%",
        f"Best strategy: {best_strat_name} (Sharpe {best_sharpe_val:.2f})",
        "",
        "🗺️ <b>Coverage</b>",
        f"Strategies tested: {len(tested_strats)}/{len(all_strats)}",
        f"New this week: {', '.join(sorted(new_this_week)) or 'none'}",
        f"Lifecycle stages: {stage_breakdown}",
        "",
        "🏆 <b>Strategy Leaderboard</b>",
    ] + lb_lines + [
        "",
        "📌 <b>Promotion Pipeline</b>",
        f"OOS ready: {', '.join(oos_ready) or 'none'}",
        f"Optimization ready: {', '.join(opt_ready) or 'none'}",
    ]

    return send_message("\n".join(lines))


def generate_dashboard() -> Path:
    """Generate/update Dashboard.md in vault root.

    Returns path to Dashboard.md.
    """
    from research.discovery import STRATEGY_UNIVERSE

    journal = _read_json(JOURNAL_PATH)
    if not isinstance(journal, list):
        journal = []

    queue_entries = _read_json(QUEUE_PATH)
    if not isinstance(queue_entries, list):
        queue_entries = []

    today_str = datetime.now().strftime("%Y-%m-%d")
    now_iso = datetime.now(timezone.utc).isoformat()

    # Today's stats
    today_entries = [e for e in journal if e.get("timestamp", "").startswith(today_str)]
    today_total = len(today_entries)
    today_pass = sum(1 for e in today_entries if e.get("verdict") == "pass")
    today_fail = sum(1 for e in today_entries if e.get("verdict") == "fail")
    today_partial = sum(1 for e in today_entries if e.get("verdict") == "partial")

    # Best Sharpe today
    best_today_sharpe = None
    best_today_strat = "—"
    for e in today_entries:
        m = e.get("key_metrics") or {}
        s = m.get("sharpe", m.get("sharpe_ratio"))
        if s is not None:
            if best_today_sharpe is None or s > best_today_sharpe:
                best_today_sharpe = s
                best_today_strat = e.get("strategy") or "unknown"
    best_today_str = f"{best_today_strat} ({best_today_sharpe:.2f})" if best_today_sharpe is not None else "—"

    # Queue stats
    qstats = _queue_stats(queue_entries)

    # Daemon status
    daemon_status, _ = _get_daemon_status()

    # All-time stats
    all_total = len(journal)
    all_pass = sum(1 for e in journal if e.get("verdict") == "pass")
    overall_pct = (all_pass / all_total * 100) if all_total else 0.0
    tested_strats = len(set(e.get("strategy") for e in journal if e.get("strategy")))

    # Hypotheses
    hyp = _count_hypotheses()
    hyp_open = hyp.get("proposed", 0) + hyp.get("open", 0)
    hyp_confirmed = hyp.get("confirmed", 0)
    hyp_rejected = hyp.get("rejected", 0)

    # Days active (from first journal entry to now)
    if journal:
        first_ts = journal[0].get("timestamp", today_str)[:10]
        try:
            first_date = datetime.strptime(first_ts, "%Y-%m-%d")
            days_active = (datetime.now() - first_date).days + 1
        except ValueError:
            days_active = 1
    else:
        days_active = 0

    # Leaderboard (top 10 all-time by best Sharpe)
    lb = _build_strategy_leaderboard(journal, top_n=10)

    # Lifecycle stage for each strategy in leaderboard
    stage_map = {}
    for row in lb:
        strat = row["strategy"]
        stage, _ = _get_lifecycle_stage(journal, strat)
        stage_map[strat] = stage

    # Recent experiments (last 10)
    recent = sorted(journal, key=lambda e: e.get("timestamp", ""), reverse=True)[:10]

    # Build Markdown
    lines = [
        "---",
        "tags: [meta, dashboard]",
        f"updated: {now_iso}",
        "---",
        "",
        "# Research Dashboard",
        "",
        f"## 📊 Today ({today_str})",
        "| Metric | Value |",
        "|--------|-------|",
        f"| Experiments | {today_total} |",
        f"| Pass/Fail/Partial | {today_pass}/{today_fail}/{today_partial} |",
        f"| Best Sharpe | {best_today_str} |",
        f"| Queue depth | {qstats['queued'] + qstats['running'] + qstats['claimed']} |",
        f"| Daemon status | {daemon_status} |",
        "",
        "## 📈 All-Time Stats",
        "| Metric | Value |",
        "|--------|-------|",
        f"| Total experiments | {all_total} |",
        f"| Overall pass rate | {overall_pct:.1f}% |",
        f"| Strategies tested | {tested_strats} |",
        f"| Hypotheses | {hyp_open} open / {hyp_confirmed} confirmed / {hyp_rejected} rejected |",
        f"| Days active | {days_active} |",
        "",
        "## 🏆 Strategy Leaderboard",
        "| # | Strategy | Best Sharpe | Win Rate | Trades | Stage |",
        "|---|----------|-------------|----------|--------|-------|",
    ]
    for i, row in enumerate(lb, 1):
        wr = row.get("win_rate", 0) or 0
        stage = stage_map.get(row["strategy"], "—")
        lines.append(
            f"| {i} | {row['strategy']} | {row['sharpe']:.2f} | "
            f"{wr:.0f}% | {row['trades']} | {stage} |"
        )

    lines += [
        "",
        "## 📋 Queue",
        "| Priority | Count |",
        "|----------|-------|",
        f"| P1 Critical | {qstats['P1']} |",
        f"| P2 High | {qstats['P2']} |",
        f"| P3 Medium | {qstats['P3']} |",
        f"| P4 Low | {qstats['P4']} |",
        "",
        "## 🔄 Recent Experiments (last 10)",
        "| Experiment | Strategy | Verdict | Sharpe | Date |",
        "|------------|----------|---------|--------|------|",
    ]
    for e in recent:
        exp_id = e.get("experiment_id", "?")
        strat = e.get("strategy", "?")
        verdict = e.get("verdict", "?")
        m = e.get("key_metrics") or {}
        s = m.get("sharpe", m.get("sharpe_ratio"))
        sharpe_str = f"{s:.2f}" if s is not None else "—"
        ts = e.get("timestamp", "")[:10]
        lines.append(f"| {exp_id} | {strat} | {verdict} | {sharpe_str} | {ts} |")

    content = "\n".join(lines) + "\n"
    dash_path = VAULT_ROOT / "Dashboard.md"
    VAULT_ROOT.mkdir(parents=True, exist_ok=True)
    dash_path.write_text(content)
    logger.info("Dashboard written to %s", dash_path)
    return dash_path


def generate_coverage_map() -> Path:
    """Generate Meta/Coverage Map.md — strategy × lifecycle matrix.

    Returns path to Coverage Map.md.
    """
    from research.discovery import STRATEGY_UNIVERSE

    journal = _read_json(JOURNAL_PATH)
    if not isinstance(journal, list):
        journal = []

    now_iso = datetime.now(timezone.utc).isoformat()

    LIFECYCLE_STAGES = ["screen", "quick", "solo", "optimize", "combined", "oos", "promote"]
    STAGE_LABEL = {
        "screen": "Screen", "quick": "Quick", "solo": "Solo",
        "optimize": "Optimize", "combined": "Combined", "oos": "OOS",
        "promote": "Promote",
    }

    CATEGORY_TO_STAGE = {
        "screening": "screen",
        "screen": "screen",
        "quick": "quick",
        "quick_screen": "quick",
        "solo": "solo",
        "optimize": "optimize",
        "optimization": "optimize",
        "combined": "combined",
        "oos": "oos",
        "out_of_sample": "oos",
        "promote": "promote",
        "promoted": "promote",
        "dormant": "solo",
        "filter": "combined",
    }

    VERDICT_ICON = {
        "pass": "✅",
        "fail": "❌",
        "partial": "⚠️",
        "running": "🔄",
    }

    def _cell(strat_entries: list, stage: str) -> str:
        """Return icon for a (strategy, stage) cell."""
        stage_entries = []
        for e in strat_entries:
            cat = e.get("category", "").lower()
            exp_id = e.get("experiment_id", "").lower()
            mapped = CATEGORY_TO_STAGE.get(cat)
            if not mapped:
                for kw, s in CATEGORY_TO_STAGE.items():
                    if kw in exp_id:
                        mapped = s
                        break
            if not mapped:
                mapped = "solo"
            if mapped == stage:
                stage_entries.append(e)
        if not stage_entries:
            return "—"
        # Pick the best verdict among entries for this stage
        verdicts = [e.get("verdict") for e in stage_entries]
        if "pass" in verdicts:
            return "✅"
        if "partial" in verdicts:
            return "⚠️"
        if "running" in verdicts:
            return "🔄"
        if "fail" in verdicts:
            return "❌"
        return "—"

    # Group journal entries by strategy
    by_strategy = defaultdict(list)
    for e in journal:
        strat = e.get("strategy")
        if strat:
            by_strategy[strat].append(e)

    # Summary counters
    count_screened = 0
    count_solo = 0
    count_optimized = 0
    count_oos = 0
    count_promoted = 0
    total_strats = len(STRATEGY_UNIVERSE)

    # Build table rows
    header = "| Strategy | Type | " + " | ".join(STAGE_LABEL[s] for s in LIFECYCLE_STAGES) + " |"
    separator = "|----------|------|" + "--------|" * len(LIFECYCLE_STAGES)

    rows = [header, separator]

    for strat_name, strat_info in sorted(STRATEGY_UNIVERSE.items()):
        strat_type = strat_info.get("type", "—")
        entries = by_strategy.get(strat_name, [])

        cells = [_cell(entries, stage) for stage in LIFECYCLE_STAGES]

        # Count stages reached
        if cells[0] != "—":  # screen
            count_screened += 1
        if cells[2] != "—":  # solo
            count_solo += 1
        if cells[3] != "—":  # optimize
            count_optimized += 1
        if cells[5] != "—":  # oos
            count_oos += 1
        if cells[6] != "—":  # promote
            count_promoted += 1

        row = f"| {strat_name} | {strat_type} | " + " | ".join(cells) + " |"
        rows.append(row)

    content = "\n".join([
        "---",
        "tags: [meta, coverage]",
        f"updated: {now_iso}",
        "---",
        "",
        "# Strategy Coverage Map",
        "",
        *rows,
        "",
        "Legend: ✅ = passed, ❌ = failed, ⚠️ = partial, 🔄 = in progress, — = not reached",
        "",
        "## Summary",
        f"- Total strategies: {total_strats}",
        f"- Screened: {count_screened}",
        f"- Solo tested: {count_solo}",
        f"- Optimized: {count_optimized}",
        f"- OOS validated: {count_oos}",
        f"- Promoted: {count_promoted}",
        "",
    ])

    cov_path = VAULT_ROOT / "Meta" / "Coverage Map.md"
    cov_path.parent.mkdir(parents=True, exist_ok=True)
    cov_path.write_text(content)
    logger.info("Coverage map written to %s", cov_path)
    return cov_path


def run_health_checks() -> List[Dict[str, Any]]:
    """Run all health checks, return list of issues.

    Checks:
    1. Data freshness: newest snapshot age (warn >24h, critical >48h)
    2. Disk space: warn <20GB, critical <5GB
    3. Memory: warn >80%, critical >95%
    4. Daemon: check heartbeat file (warn if >5min stale, critical if missing)
    5. Queue: warn if empty, warn if >100 queued (backlog)
    6. Journal: check for write errors (empty entries, missing fields)

    Returns list of result dicts with keys: check, status, message, value.
    """
    results = []

    # 1. Data freshness
    data_age_h = _get_data_age_hours()
    if data_age_h >= 48:
        data_status = "critical"
        data_msg = f"Data is {data_age_h:.1f}h old (critical: >48h)"
    elif data_age_h >= 24:
        data_status = "warning"
        data_msg = f"Data is {data_age_h:.1f}h old (warn: >24h)"
    else:
        data_status = "ok"
        data_msg = f"Data is {data_age_h:.1f}h old"
    results.append({
        "check": "data_freshness",
        "status": data_status,
        "message": data_msg,
        "value": round(data_age_h, 2),
    })

    # 2. Disk space
    disk_free_gb = _get_disk_free_gb()
    if disk_free_gb < 5:
        disk_status = "critical"
        disk_msg = f"Only {disk_free_gb:.1f}GB free (critical: <5GB)"
    elif disk_free_gb < 20:
        disk_status = "warning"
        disk_msg = f"{disk_free_gb:.1f}GB free (warn: <20GB)"
    else:
        disk_status = "ok"
        disk_msg = f"{disk_free_gb:.1f}GB free"
    results.append({
        "check": "disk_space",
        "status": disk_status,
        "message": disk_msg,
        "value": round(disk_free_gb, 2),
    })

    # 3. Memory
    mem_used, mem_total = _get_memory_info()
    mem_pct = (mem_used / mem_total * 100) if mem_total else 0.0
    if mem_pct >= 95:
        mem_status = "critical"
        mem_msg = f"Memory {mem_pct:.0f}% used ({mem_used:.1f}/{mem_total:.1f}GB, critical: >95%)"
    elif mem_pct >= 80:
        mem_status = "warning"
        mem_msg = f"Memory {mem_pct:.0f}% used ({mem_used:.1f}/{mem_total:.1f}GB, warn: >80%)"
    else:
        mem_status = "ok"
        mem_msg = f"Memory {mem_pct:.0f}% used ({mem_used:.1f}/{mem_total:.1f}GB)"
    results.append({
        "check": "memory",
        "status": mem_status,
        "message": mem_msg,
        "value": round(mem_pct, 1),
    })

    # 4. Daemon heartbeat
    hb = _read_heartbeat()
    if hb is None:
        hb_status = "critical"
        hb_msg = "Heartbeat file missing — daemon not running"
        hb_age = None
    else:
        ts_str = hb.get("timestamp") or hb.get("ts") or ""
        if not ts_str:
            hb_status = "warning"
            hb_msg = "Heartbeat file exists but has no timestamp"
            hb_age = None
        else:
            try:
                ts = datetime.fromisoformat(ts_str)
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                age_min = (datetime.now(timezone.utc) - ts).total_seconds() / 60
                hb_age = round(age_min, 1)
                if age_min >= 30:
                    hb_status = "critical"
                    hb_msg = f"Daemon heartbeat {age_min:.0f}min stale (critical: >30min)"
                elif age_min >= 5:
                    hb_status = "warning"
                    hb_msg = f"Daemon heartbeat {age_min:.0f}min stale (warn: >5min)"
                else:
                    hb_status = "ok"
                    hb_msg = f"Daemon heartbeat {age_min:.1f}min ago"
            except Exception:
                hb_status = "warning"
                hb_msg = f"Heartbeat timestamp parse error: {ts_str!r}"
                hb_age = None
    results.append({
        "check": "daemon_heartbeat",
        "status": hb_status,
        "message": hb_msg,
        "value": hb_age,
    })

    # 5. Queue health
    queue = _read_json(QUEUE_PATH)
    if not isinstance(queue, list):
        queue = []
    qstats = _queue_stats(queue)
    pending = qstats["queued"] + qstats["claimed"]
    if pending == 0:
        q_status = "warning"
        q_msg = "Queue is empty — no experiments pending"
    elif pending > 100:
        q_status = "warning"
        q_msg = f"Queue backlog: {pending} pending entries (warn: >100)"
    else:
        q_status = "ok"
        q_msg = f"Queue has {pending} pending entries"
    results.append({
        "check": "queue_health",
        "status": q_status,
        "message": q_msg,
        "value": pending,
    })

    # 6. Journal integrity
    journal = _read_json(JOURNAL_PATH)
    if not isinstance(journal, list):
        j_status = "critical"
        j_msg = "journal.json is not a list — possible corruption"
    else:
        missing_fields = sum(
            1 for e in journal
            if not e.get("experiment_id") or not e.get("timestamp") or not e.get("verdict")
        )
        if missing_fields > 0:
            j_status = "warning"
            j_msg = f"{missing_fields}/{len(journal)} journal entries missing required fields"
        else:
            j_status = "ok"
            j_msg = f"Journal has {len(journal)} entries, all valid"
    results.append({
        "check": "journal_integrity",
        "status": j_status if isinstance(journal, list) else "critical",
        "message": j_msg,
        "value": len(journal) if isinstance(journal, list) else 0,
    })

    return results


def rotate_logs(max_age_days: int = 7, compress_after_days: int = 30) -> Dict[str, int]:
    """Rotate old log files and compress old experiment JSON.

    1. Daemon logs in logs/ older than max_age_days → archive to logs/archive/
    2. Experiment JSON in research/experiments/ older than compress_after_days → gzip
    3. Clean up empty directories

    Returns: {"archived": n, "compressed": n, "cleaned": n}
    """
    archived = 0
    compressed = 0
    cleaned = 0
    now_ts = datetime.now().timestamp()

    # 1. Archive old logs
    if LOG_DIR.exists():
        archive_dir = LOG_DIR / "archive"
        archive_dir.mkdir(exist_ok=True)
        cutoff_archive = now_ts - max_age_days * 86400
        for log_file in LOG_DIR.iterdir():
            if log_file.is_file() and log_file.suffix in (".log", ".txt"):
                if log_file.stat().st_mtime < cutoff_archive:
                    dest = archive_dir / log_file.name
                    try:
                        shutil.move(str(log_file), str(dest))
                        archived += 1
                        logger.debug("Archived log: %s → %s", log_file.name, dest)
                    except Exception as e:
                        logger.warning("Failed to archive %s: %s", log_file, e)

    # 2. Compress old experiment JSON files
    if EXPERIMENTS_DIR.exists():
        cutoff_compress = now_ts - compress_after_days * 86400
        for json_file in EXPERIMENTS_DIR.rglob("*.json"):
            if json_file.stat().st_mtime < cutoff_compress:
                gz_path = json_file.with_suffix(".json.gz")
                if gz_path.exists():
                    continue  # already compressed
                try:
                    with open(json_file, "rb") as f_in:
                        with gzip.open(gz_path, "wb") as f_out:
                            shutil.copyfileobj(f_in, f_out)
                    json_file.unlink()
                    compressed += 1
                    logger.debug("Compressed: %s", json_file.name)
                except Exception as e:
                    logger.warning("Failed to compress %s: %s", json_file, e)

    # 3. Remove empty directories (only under logs/ and experiments/)
    for base_dir in [LOG_DIR, EXPERIMENTS_DIR]:
        if not base_dir.exists():
            continue
        for dirpath in sorted(base_dir.rglob("*"), reverse=True):
            if dirpath.is_dir() and dirpath != base_dir:
                try:
                    if not any(dirpath.iterdir()):
                        dirpath.rmdir()
                        cleaned += 1
                        logger.debug("Removed empty dir: %s", dirpath)
                except Exception:
                    pass

    logger.info("Log rotation: archived=%d, compressed=%d, cleaned=%d",
                archived, compressed, cleaned)
    return {"archived": archived, "compressed": compressed, "cleaned": cleaned}


# ─── CLI ────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    parser = argparse.ArgumentParser(description="Atlas Research Monitoring")
    sub = parser.add_subparsers(dest="cmd")

    sub.add_parser("daily-digest", help="Send daily Telegram digest")
    sub.add_parser("weekly-summary", help="Send weekly Telegram summary")
    sub.add_parser("dashboard", help="Generate vault Dashboard.md")
    sub.add_parser("coverage-map", help="Generate Meta/Coverage Map.md")
    sub.add_parser("health-checks", help="Run health checks")
    rot = sub.add_parser("rotate-logs", help="Rotate and compress old logs")
    rot.add_argument("--max-age-days", type=int, default=7)
    rot.add_argument("--compress-after-days", type=int, default=30)

    args = parser.parse_args()

    if args.cmd == "daily-digest":
        ok = send_daily_digest()
        print("Sent" if ok else "Failed")
    elif args.cmd == "weekly-summary":
        ok = send_weekly_summary()
        print("Sent" if ok else "Failed")
    elif args.cmd == "dashboard":
        p = generate_dashboard()
        print(f"Dashboard: {p}")
    elif args.cmd == "coverage-map":
        p = generate_coverage_map()
        print(f"Coverage map: {p}")
    elif args.cmd == "health-checks":
        checks = run_health_checks()
        for c in checks:
            icon = {"ok": "✅", "warning": "⚠️", "critical": "❌"}.get(c["status"], "?")
            print(f"{icon} {c['check']}: {c['message']}")
    elif args.cmd == "rotate-logs":
        result = rotate_logs(
            max_age_days=args.max_age_days,
            compress_after_days=args.compress_after_days,
        )
        print(f"archived={result['archived']}, compressed={result['compressed']}, "
              f"cleaned={result['cleaned']}")
    else:
        parser.print_help()
