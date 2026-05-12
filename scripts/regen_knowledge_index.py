#!/usr/bin/env python3
"""
scripts/regen_knowledge_index.py — Regenerate docs/KNOWLEDGE_INDEX.md

Builds a fresh Markdown file covering:
  1. Top-level directory descriptions
  2. Key entry-point files
  3. Active strategies (from config/active/sp500.json)
  4. Active markets (config/active/*.json filenames)
  5. Recent git commits (last 20)
  6. Test inventory (count + module hierarchy)

Usage:
  python3 scripts/regen_knowledge_index.py
"""

from __future__ import annotations

import datetime
import json
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
OUTPUT_PATH = PROJECT_ROOT / "docs" / "KNOWLEDGE_INDEX.md"

# ── Directory descriptions ─────────────────────────────────────────────────────
DIR_DESCRIPTIONS: dict[str, str] = {
    "strategies": "Strategy implementations (momentum, mean reversion, trend-following, etc.)",
    "brokers": "Broker adapters — `alpaca/` (live trading), `paper/` (simulated), live_executor.py, live_portfolio.py",
    "data": "Data layer — OHLCV cache (parquet), loaders, atlas.db (SQLite ~100 MB), FRED/Tiingo helpers",
    "research": "Research engine — backtests, parameter sweeps, experiment tracking (research_experiments table)",
    "backtest": "Backtesting framework — engine, metrics, report generation",
    "services": "FastAPI servers — `chat_server.py` (dashboard API + WebSocket), Telegram bot, sub-routers in `api/`",
    "scripts": "Operational scripts — EOD settlement, reconciliation, cron jobs, health checks, migrations",
    "config": "JSON configs — `active/` (per-market live config), `markets.json`, strategy_params",
    "dashboard-ui": "React 19 + Vite frontend for the trading dashboard (Recharts, Tailwind)",
    "monitor": "Intraday monitoring — trailing stops, price tracking, lifecycle management",
    "portfolio": "Portfolio management — allocation pools, rebalancing, position sizing",
    "plans": "Trade plans — pending signals in JSON awaiting execution by `execute_approved.py`",
    "indicators": "Technical indicators library (vol cones, Yang-Zhang vol, etc.)",
    "signals": "Signal generation from strategies",
    "risk": "Risk management — VaR, ruin probability, per-trade sizing, drawdown limits",
    "regime": "Market regime detection (bull/bear/transition) — RegimeModel, distributions",
    "overlay": "Overlay signals (VIX, breadth, macro, alt-data) feeding signal enrichment",
    "tests": "pytest test suite (336 test files, run via `pytest tests/ -x -v --timeout=30`)",
    "logs": "Rotating log files (not committed)",
    "universe": "Stock universe construction and filtering — membership, builder, auto-exclusions",
    "db": "Database utilities — schema.sql, atlas_db.py helpers, migrations/",
    "docs": "Architecture docs, decision records, runbooks, audit reports",
    "journal": "Trade ledger (JSON + SQLite dual-write, deprecated as primary source)",
    "core": "Cross-cutting modules — reconcile, remediation kill-switch",
    "utils": "Shared utilities — Telegram notify, config helpers, atomic writes",
}

# ── Key entry-point files ──────────────────────────────────────────────────────
KEY_FILES: list[tuple[str, str]] = [
    ("services/chat_server.py", "Main dashboard API server (FastAPI) — 210 LOC bootstrap, routes in services/api/"),
    ("services/api/dashboard.py", "Dashboard data endpoint — _build_dashboard_data(), 30s cache, 8 broker RPCs"),
    ("services/api/approvals.py", "Trade plan approve/reject business logic + routes"),
    ("services/api/lifecycle.py", "Strategy lifecycle REST endpoints — list, transition, promote"),
    ("services/api/research.py", "Research dashboard endpoints — experiments, brain, sessions"),
    ("services/api/health.py", "System health endpoint — DB staleness, service status"),
    ("services/telegram_bot.py", "Telegram notification bot — command handlers, alert dispatch"),
    ("brokers/live_executor.py", "Order execution via Alpaca — execute_plan, reconcile fills, place stops"),
    ("brokers/live_portfolio.py", "Live portfolio state management — positions, equity, drawdown, HALT"),
    ("brokers/plan.py", "Trade plan generation — TradePlanGenerator, signal pipelines, risk checks"),
    ("brokers/alpaca/broker.py", "Alpaca broker adapter — get_positions(), place_order(), _broker_call()"),
    ("scripts/eod_settlement.py", "End-of-day stop/TP checking — runs ~22:00 UTC after US close"),
    ("scripts/sync_protective_orders.py", "Protective order sync — stops/TPs/OCO vs broker every 15 min"),
    ("scripts/execute_approved.py", "Execute approved trade plans — runs 23:15 AEST Mon-Fri"),
    ("scripts/reconcile_positions.py", "Reconcile internal state vs broker positions"),
    ("scripts/reconcile_ledger.py", "Reconcile trade ledger fill prices from broker"),
    ("db/atlas_db.py", "SQLite helpers — get_db(), record_trade_exit(), MAE/MFE, batch upserts"),
    ("db/schema.sql", "Canonical DB schema — source of truth for all tables"),
]

# ── Helpers ────────────────────────────────────────────────────────────────────

def _run(cmd: list[str], cwd: Path | None = None) -> str:
    """Run subprocess, return stdout (stripped)."""
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            cwd=str(cwd or PROJECT_ROOT),
            timeout=15,
        )
        return result.stdout.strip()
    except (subprocess.SubprocessError, OSError):
        return "(unavailable)"


def _load_json(path: Path) -> dict | list | None:
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def _active_markets() -> list[str]:
    return sorted(p.stem for p in (PROJECT_ROOT / "config" / "active").glob("*.json"))


def _active_strategies_sp500() -> list[str]:
    cfg = _load_json(PROJECT_ROOT / "config" / "active" / "sp500.json")
    if cfg and isinstance(cfg, dict):
        strats = cfg.get("strategies", {})
        return sorted(strats.keys()) if isinstance(strats, dict) else []
    return []


def _recent_commits(n: int = 20) -> str:
    return _run(["git", "log", f"--oneline", f"-{n}"])


def _test_inventory() -> tuple[int, list[str]]:
    tests_dir = PROJECT_ROOT / "tests"
    all_test_files = sorted(tests_dir.rglob("test_*.py"))
    # Build module groups
    groups: dict[str, int] = {}
    for f in all_test_files:
        rel = f.relative_to(tests_dir)
        parts = rel.parts
        if len(parts) == 1:
            module = "tests/"
        else:
            module = f"tests/{parts[0]}/"
        groups[module] = groups.get(module, 0) + 1
    return len(all_test_files), sorted(groups.items())


def _exists_marker(path: Path) -> str:
    return "✓" if path.exists() else "✗ (missing)"


# ── Build index ────────────────────────────────────────────────────────────────

def build_index() -> str:
    today = datetime.date.today().isoformat()
    lines: list[str] = []

    def h(text: str) -> None:
        lines.append(text)

    # Header
    h("# Atlas Knowledge Index")
    h("")
    h(f"*Auto-regenerated {today}. Re-run via `python3 scripts/regen_knowledge_index.py`.*")
    h("")
    h("*This file is overwritten on each regen — do not edit by hand.*")
    h("")
    h("---")
    h("")

    # 1. Top-level directory map
    h("## 1. Directory Map")
    h("")
    h("| Directory | Purpose |")
    h("|-----------|---------|")
    existing_dirs = sorted(
        d.name for d in PROJECT_ROOT.iterdir()
        if d.is_dir() and not d.name.startswith(".") and d.name in DIR_DESCRIPTIONS
    )
    for name in existing_dirs:
        h(f"| `{name}/` | {DIR_DESCRIPTIONS[name]} |")
    # Also list dirs we have descriptions for that may exist
    for name, desc in sorted(DIR_DESCRIPTIONS.items()):
        if name not in existing_dirs and (PROJECT_ROOT / name).exists():
            h(f"| `{name}/` | {desc} |")
    h("")

    # 2. Key entry-point files
    h("## 2. Key Entry-Point Files")
    h("")
    h("| File | Purpose |")
    h("|------|---------|")
    for path_str, desc in KEY_FILES:
        exists = _exists_marker(PROJECT_ROOT / path_str)
        h(f"| [`{path_str}`]({path_str}) {exists} | {desc} |")
    h("")

    # 3. Active strategies (sp500)
    h("## 3. Active Strategies (sp500)")
    h("")
    strategies = _active_strategies_sp500()
    if strategies:
        for s in strategies:
            h(f"- `{s}`")
    else:
        h("*(could not read config/active/sp500.json)*")
    h("")

    # 4. Active markets
    h("## 4. Active Markets")
    h("")
    markets = _active_markets()
    if markets:
        for m in markets:
            cfg = _load_json(PROJECT_ROOT / "config" / "active" / f"{m}.json")
            mode = "?"
            live_enabled = "?"
            if cfg and isinstance(cfg, dict):
                trading = cfg.get("trading", {})
                mode = trading.get("mode", cfg.get("mode", "?"))
                live_enabled = str(trading.get("live_enabled", cfg.get("live_enabled", "?")))
            h(f"- `{m}` — mode=`{mode}` live_enabled=`{live_enabled}`")
    else:
        h("*(no config/active/*.json found)*")
    h("")

    # 5. Recent commits
    h("## 5. Recent Commits (last 20)")
    h("")
    h("```")
    commits = _recent_commits(20)
    h(commits if commits else "(git unavailable)")
    h("```")
    h("")

    # 6. Test inventory
    h("## 6. Test Inventory")
    h("")
    total, groups = _test_inventory()
    h(f"**Total test files**: {total}")
    h("")
    h("| Module group | Files |")
    h("|-------------|-------|")
    for group, count in groups:
        h(f"| `{group}` | {count} |")
    h("")

    # 7. Quick-reference: key scripts
    h("## 7. Operational Scripts Quick Reference")
    h("")
    h("| Script | Cron / Trigger | Notes |")
    h("|--------|----------------|-------|")
    quick_refs = [
        ("scripts/pi-cron.sh premarket sp500", "19:00 AEST Mon-Fri", "Market analysis + plan generation"),
        ("scripts/pi-cron.sh postclose sp500", "08:00 AEST Tue-Sat", "EOD reconciliation + health report"),
        ("scripts/execute_approved.py -m sp500", "23:15 AEST Mon-Fri", "Execute pending trade plans"),
        ("scripts/sync_protective_orders.py --market sp500", "Every 15 min", "Sync stop/TP/OCO orders"),
        ("scripts/intraday_monitor.py -m sp500", "Every 30 min RTH", "Trailing stop monitoring"),
        ("scripts/eod_settlement.py", "22:04 UTC Mon-Fri", "EOD stop-loss / take-profit check"),
        ("scripts/reconcile_positions.py --market sp500", "09:00 AEST Tue-Sat", "State vs broker reconciliation"),
        ("scripts/reconcile_ledger.py --market sp500", "09:30 AEST Tue-Sat", "Fill-price ledger sync"),
        ("scripts/sync_broker_orders.py", "Every 4h", "Upsert broker_orders cache table"),
        ("scripts/compute_daily_risk.py", "23:00 AEST daily", "VaR, vol cones, ruin probability"),
        ("scripts/cleanup_sediment.py --apply", "04:00 UTC daily", "Delete old incident snapshot files"),
        ("scripts/check_doc_staleness.py", "08:00 AEST daily", "Alert if KNOWLEDGE_INDEX or SUMMARY >30d"),
        ("scripts/check_macro_freshness.py", "09:30 AEST daily", "Check FRED/macro data staleness"),
        ("scripts/check_live_research_divergence.py", "06:30 AEST daily", "Sharpe divergence monitor + rollback"),
    ]
    for script, trigger, notes in quick_refs:
        h(f"| `{script}` | {trigger} | {notes} |")
    h("")

    return "\n".join(lines)


def main() -> None:
    index = build_index()
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(index + "\n")
    size = OUTPUT_PATH.stat().st_size
    print(f"Wrote {OUTPUT_PATH.relative_to(PROJECT_ROOT)}  ({size:,} bytes)")


if __name__ == "__main__":
    main()
