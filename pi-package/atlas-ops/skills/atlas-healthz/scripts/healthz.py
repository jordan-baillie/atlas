#!/usr/bin/env python3
"""Atlas System Health Check — comprehensive audit of all subsystems.

Checks: infrastructure, data, config, broker, portfolio, cron, research,
logging, disk, and backtest performance. Returns structured JSON with
per-check verdicts (ok/warn/fail) and actionable messages.

Usage:
    python3 scripts/healthz.py                 # all checks
    python3 scripts/healthz.py --section infra # single section
    python3 scripts/healthz.py --json          # raw JSON output
    python3 scripts/healthz.py --market sp500  # market override

Exit codes: 0 = all ok, 1 = warnings only, 2 = failures present
"""
import sys
import os
import json
import socket
import subprocess
import argparse
from datetime import datetime, timedelta
from pathlib import Path
from collections import Counter

# Resolve project root: prefer ATLAS_ROOT env, then walk up to find config/active/
_default = Path(__file__).resolve()
for _p in [Path(os.environ.get("ATLAS_ROOT", "")),
           Path("/root/atlas"),
           _default.parent.parent.parent.parent.parent,
           _default.parent.parent.parent.parent]:
    if (_p / "config" / "active").is_dir():
        PROJECT = _p
        break
else:
    PROJECT = Path("/root/atlas")  # last resort
sys.path.insert(0, str(PROJECT))

# ── Helpers ────────────────────────────────────────────────────

def _age_hours(path: Path) -> float:
    """Hours since file was last modified."""
    if not path.exists():
        return float("inf")
    return (datetime.now().timestamp() - path.stat().st_mtime) / 3600

def _file_size_mb(path: Path) -> float:
    if not path.exists():
        return 0
    return path.stat().st_size / (1024 * 1024)

def _load_json(path: Path):
    if not path.exists():
        return None
    with open(path) as f:
        return json.load(f)

def _count_lines(path: Path) -> int:
    if not path.exists():
        return 0
    with open(path, "rb") as f:
        return sum(1 for _ in f)

def _check(ok: bool, warn_msg: str = "", fail_msg: str = ""):
    """Return (verdict, message) tuple."""
    if ok:
        return ("ok", "")
    if fail_msg:
        return ("fail", fail_msg)
    return ("warn", warn_msg)


# ── Check sections ────────────────────────────────────────────

def check_infra(project: Path) -> list:
    """Infrastructure: services, ports, systemd units."""
    results = []

    # Alpaca broker (no gateway process needed — REST API only)
    cfg_path = project / "config" / "active" / "sp500.json"
    broker_name = ""
    if cfg_path.exists():
        try:
            import json as _json
            broker_name = _json.load(open(cfg_path)).get("trading", {}).get("broker", "")
        except Exception:
            pass
    if broker_name == "alpaca":
        results.append({"check": "alpaca_api", "verdict": "ok", "message": "Broker is Alpaca (REST API, no gateway needed)"})

    # (IBKR broker removed — no gateway check needed)

    # Telegram bot service
    try:
        r = subprocess.run(["systemctl", "is-active", "atlas-telegram-bot"], capture_output=True, text=True, timeout=5)
        active = r.stdout.strip() == "active"
        v, m = _check(active, fail_msg="atlas-telegram-bot service is not running")
        results.append({"check": "telegram_bot", "verdict": v, "message": m or "Service active"})
    except Exception as e:
        results.append({"check": "telegram_bot", "verdict": "warn", "message": f"Could not check: {e}"})

    # Dashboard service
    try:
        r = subprocess.run(["systemctl", "is-active", "atlas-dashboard"], capture_output=True, text=True, timeout=5)
        active = r.stdout.strip() == "active"
        v, m = _check(active, warn_msg="atlas-dashboard service is not running")
        results.append({"check": "dashboard_service", "verdict": v, "message": m or "Service active"})
    except Exception as e:
        results.append({"check": "dashboard_service", "verdict": "warn", "message": f"Could not check: {e}"})

    # Secrets file
    secrets_path = Path.home() / ".atlas-secrets.json"
    if secrets_path.exists():
        secrets = _load_json(secrets_path)
        has_telegram = bool(secrets.get("telegram_bot_token")) and bool(secrets.get("telegram_chat_id"))
        has_alpaca = bool(secrets.get("ALPACA_API_KEY") and secrets.get("ALPACA_SECRET_KEY"))
        results.append({"check": "secrets_telegram", "verdict": "ok" if has_telegram else "fail",
                        "message": "Telegram credentials present" if has_telegram else "Missing telegram_bot_token or telegram_chat_id in ~/.atlas-secrets.json"})
        results.append({"check": "secrets_broker", "verdict": "ok" if has_alpaca else "warn",
                        "message": "Alpaca credentials present" if has_alpaca else "Missing ALPACA_API_KEY or ALPACA_SECRET_KEY in ~/.atlas-secrets.json"})
    else:
        results.append({"check": "secrets_file", "verdict": "fail", "message": "~/.atlas-secrets.json not found"})

    return results


def check_data(project: Path, market_id: str) -> list:
    """Data: cache freshness, ticker coverage, cache integrity."""
    results = []
    cache_dir = project / "data" / "cache" / market_id

    if not cache_dir.exists():
        results.append({"check": "cache_dir", "verdict": "fail", "message": f"Cache directory missing: {cache_dir}"})
        return results

    parquets = list(cache_dir.glob("*.parquet"))
    results.append({"check": "cache_files", "verdict": "ok" if len(parquets) > 50 else "warn",
                    "message": f"{len(parquets)} parquet files in {market_id}/ cache"})

    # Freshness: newest file age
    if parquets:
        newest = max(p.stat().st_mtime for p in parquets)
        age_h = (datetime.now().timestamp() - newest) / 3600
        fresh = age_h < 48  # 48h tolerance (weekends)
        results.append({"check": "cache_freshness", "verdict": "ok" if fresh else "warn",
                        "message": f"Newest cache file is {age_h:.1f}h old" + ("" if fresh else " — may need ingest")})

    # Check for empty parquets
    import pandas as pd
    empty_count = 0
    tiny_count = 0
    for p in parquets[:50]:  # sample
        try:
            df = pd.read_parquet(p)
            if len(df) == 0:
                empty_count += 1
            elif len(df) < 100:
                tiny_count += 1
        except Exception:
            empty_count += 1
    if empty_count:
        results.append({"check": "cache_empty_files", "verdict": "warn", "message": f"{empty_count} empty/corrupt parquet files in sample of 50"})
    if tiny_count:
        results.append({"check": "cache_short_history", "verdict": "warn", "message": f"{tiny_count} files with <100 rows in sample of 50"})

    # Universe file
    uni_path = project / "data" / "processed" / f"universe_{market_id}.json"
    if uni_path.exists():
        uni = _load_json(uni_path)
        results.append({"check": "universe", "verdict": "ok", "message": f"Universe has {len(uni)} tickers"})
    else:
        results.append({"check": "universe", "verdict": "warn", "message": f"No universe file for {market_id}"})

    return results


def check_config(project: Path, market_id: str) -> list:
    """Config: active config validity, version, strategy state."""
    results = []
    cfg_path = project / "config" / "active" / f"{market_id}.json"

    if not cfg_path.exists():
        results.append({"check": "config_file", "verdict": "fail", "message": f"Active config missing: {cfg_path.name}"})
        return results

    cfg = _load_json(cfg_path)
    results.append({"check": "config_version", "verdict": "ok", "message": f"Version: {cfg.get('version', '?')}"})

    # Required sections
    for section in ["risk", "fees", "strategies", "backtest", "data", "trading"]:
        present = section in cfg
        results.append({"check": f"config_{section}", "verdict": "ok" if present else "fail",
                        "message": f"Section '{section}' " + ("present" if present else "MISSING")})

    # Enabled strategies
    strats = cfg.get("strategies", {})
    enabled = [s for s, v in strats.items() if v.get("enabled", False)]
    disabled = [s for s, v in strats.items() if not v.get("enabled", True)]
    results.append({"check": "strategies_enabled", "verdict": "ok" if enabled else "warn",
                    "message": f"Enabled: {', '.join(enabled) or 'NONE'}"})
    if disabled:
        results.append({"check": "strategies_disabled", "verdict": "ok",
                        "message": f"Disabled: {', '.join(disabled)}"})

    # Risk params sanity
    risk = cfg.get("risk", {})
    eq = risk.get("starting_equity", 0)
    max_pos = risk.get("max_open_positions", 0)
    max_risk = risk.get("max_risk_per_trade_pct", 0)
    results.append({"check": "risk_equity", "verdict": "ok" if eq > 0 else "fail",
                    "message": f"Starting equity: ${eq:,.0f}"})
    results.append({"check": "risk_positions", "verdict": "ok",
                    "message": f"Max positions: {max_pos}, risk/trade: {max_risk:.1%}"})

    # Trading mode
    trading = cfg.get("trading", {})
    mode = trading.get("mode", "live")
    broker = trading.get("broker", "alpaca")
    live = trading.get("live_enabled", False)
    dry = trading.get("live_safety", {}).get("dry_run_first", True)
    results.append({"check": "trading_mode", "verdict": "ok",
                    "message": f"Mode: {mode}, Broker: {broker}, Live: {live}, DryRun: {dry}"})

    # Optimization metadata
    meta = cfg.get("_optimization_metadata", {})
    if meta:
        opt_date = meta.get("optimized_at", "?")[:10]
        sharpe = meta.get("optimized_sharpe", "?")
        results.append({"check": "optimization", "verdict": "ok",
                        "message": f"Last optimized: {opt_date}, Sharpe: {sharpe}"})

    return results


def check_broker(project: Path, market_id: str) -> list:
    """Broker: connection, positions, account info."""
    results = []
    cfg_path = project / "config" / "active" / f"{market_id}.json"
    if not cfg_path.exists():
        results.append({"check": "broker_config", "verdict": "fail", "message": "No config"})
        return results

    cfg = _load_json(cfg_path)
    broker_name = cfg.get("trading", {}).get("broker", "alpaca")
    if broker_name not in ("alpaca",):
        results.append({"check": "broker", "verdict": "ok", "message": "No valid broker configured"})
        return results

    try:
        from brokers.registry import get_broker
        broker = get_broker(market_id, cfg)
        if not broker.connect():
            results.append({"check": "broker_connect", "verdict": "fail", "message": f"Broker ({broker_name}) connection failed"})
            return results

        results.append({"check": "broker_connect", "verdict": "ok", "message": f"Connected to {broker_name}"})

        info = broker.get_account_info()
        results.append({"check": "broker_account", "verdict": "ok",
                        "message": f"Equity: ${info.equity:,.2f}, Cash: ${info.cash:,.2f}, Positions: {info.num_positions}"})

        positions = broker.get_positions()
        if positions:
            tickers = [p.ticker for p in positions]
            results.append({"check": "broker_positions", "verdict": "ok",
                            "message": f"{len(positions)} positions: {', '.join(tickers[:10])}"})

        orders = broker.get_open_orders()
        if orders:
            results.append({"check": "broker_orders", "verdict": "warn",
                            "message": f"{len(orders)} open orders pending"})
        else:
            results.append({"check": "broker_orders", "verdict": "ok", "message": "No open orders"})

        broker.disconnect()
    except Exception as e:
        results.append({"check": "broker_error", "verdict": "fail", "message": f"Broker check failed: {e}"})

    return results


def check_portfolio(project: Path, market_id: str) -> list:
    """Portfolio: live state, equity history, closed trades, consistency."""
    results = []
    state_path = project / "brokers" / "state" / f"live_{market_id}.json"

    if not state_path.exists():
        results.append({"check": "live_state", "verdict": "warn", "message": f"No live state file for {market_id}"})
        return results

    state = _load_json(state_path)
    eq_hist = state.get("equity_history", [])
    trades = state.get("closed_trades", [])
    halted = state.get("halted", False)

    results.append({"check": "equity_history", "verdict": "ok" if eq_hist else "warn",
                    "message": f"{len(eq_hist)} equity snapshots"})
    results.append({"check": "closed_trades", "verdict": "ok",
                    "message": f"{len(trades)} closed trades recorded"})

    if halted:
        results.append({"check": "halt_status", "verdict": "fail",
                        "message": f"HALTED: {state.get('halt_reason', '?')}"})
    else:
        results.append({"check": "halt_status", "verdict": "ok", "message": "Not halted"})

    # Check equity consistency
    if eq_hist:
        latest = eq_hist[-1]
        eq = latest.get("equity", 0)
        starting = state.get("daily_high_water", 0) or eq
        results.append({"check": "latest_equity", "verdict": "ok",
                        "message": f"Latest equity: ${eq:,.2f} ({latest.get('date', '?')}), {latest.get('num_positions', 0)} positions"})

    # Trade quality summary
    if trades:
        pnls = [t.get("pnl", 0) for t in trades]
        winners = [p for p in pnls if p > 0]
        win_rate = len(winners) / len(pnls) * 100 if pnls else 0
        total_pnl = sum(pnls)
        results.append({"check": "trade_quality", "verdict": "ok",
                        "message": f"Win rate: {win_rate:.0f}%, Total P&L: ${total_pnl:+,.2f}, Avg: ${sum(pnls)/len(pnls):+,.2f}"})

    # Plans
    plans_dir = project / "plans"
    if plans_dir.exists():
        plans = sorted(plans_dir.glob("plan_*.json"))
        if plans:
            latest_plan = _load_json(plans[-1])
            status = latest_plan.get("status", "?") if latest_plan else "?"
            plan_date = plans[-1].stem.replace("plan_", "")
            results.append({"check": "latest_plan", "verdict": "ok",
                            "message": f"Latest plan: {plan_date} ({status})"})

    return results


def check_cron(project: Path) -> list:
    """Cron: schedule, recent runs, failures."""
    results = []

    # Check crontab
    try:
        r = subprocess.run(["crontab", "-l"], capture_output=True, text=True, timeout=5)
        lines = [l for l in r.stdout.split("\n") if l.strip() and not l.startswith("#")]
        atlas_jobs = [l for l in lines if "atlas" in l.lower()]
        results.append({"check": "crontab", "verdict": "ok" if atlas_jobs else "warn",
                        "message": f"{len(atlas_jobs)} Atlas cron jobs installed"})

        # Check for key jobs
        has_premarket = any("premarket" in l for l in atlas_jobs)
        has_postclose = any("postclose" in l for l in atlas_jobs)
        has_research = any("research" in l for l in atlas_jobs)
        has_dashboard = any("dashboard" in l or "refresh" in l for l in atlas_jobs)
        has_maintenance = any("maintenance" in l for l in atlas_jobs)

        for job, present in [("premarket", has_premarket), ("postclose", has_postclose),
                             ("research", has_research), ("dashboard", has_dashboard),
                             ("maintenance", has_maintenance)]:
            results.append({"check": f"cron_{job}", "verdict": "ok" if present else "warn",
                            "message": f"{job}: {'scheduled' if present else 'NOT scheduled'}"})
    except Exception as e:
        results.append({"check": "crontab", "verdict": "warn", "message": f"Could not read crontab: {e}"})

    # Recent cron logs
    logs_dir = project / "logs"
    cron_logs = sorted(logs_dir.glob("pi-cron-*"), key=lambda p: p.stat().st_mtime, reverse=True)
    if cron_logs:
        recent = cron_logs[0]
        age_h = _age_hours(recent)
        results.append({"check": "cron_last_run", "verdict": "ok" if age_h < 30 else "warn",
                        "message": f"Last cron log: {recent.name} ({age_h:.1f}h ago)"})
    else:
        results.append({"check": "cron_last_run", "verdict": "warn", "message": "No cron logs found"})

    # Recovery logs (sign of past failures)
    recover_logs = list(logs_dir.glob("recover_*.log"))
    recent_recoveries = [l for l in recover_logs if _age_hours(l) < 72]
    if recent_recoveries:
        results.append({"check": "recent_recoveries", "verdict": "warn",
                        "message": f"{len(recent_recoveries)} auto-recovery runs in last 72h"})

    return results


def check_research(project: Path) -> list:
    """Research: queue state, journal, experiment results."""
    results = []
    queue_path = project / "research" / "queue.json"
    journal_path = project / "research" / "journal.json"

    if not queue_path.exists():
        results.append({"check": "research_queue", "verdict": "warn", "message": "No research queue"})
        return results

    queue = _load_json(queue_path) or []
    statuses = Counter(e.get("status", "?") for e in queue)
    results.append({"check": "research_queue", "verdict": "ok",
                    "message": f"{len(queue)} experiments: {dict(statuses)}"})

    queued = statuses.get("queued", 0)
    if queued > 0:
        results.append({"check": "research_pending", "verdict": "ok",
                        "message": f"{queued} experiments waiting to run"})

    if journal_path.exists():
        journal = _load_json(journal_path) or []
        verdicts = Counter(e.get("verdict", "?") for e in journal)
        results.append({"check": "research_journal", "verdict": "ok",
                        "message": f"{len(journal)} journal entries: {dict(verdicts)}"})

    # Experiment files
    exp_dir = project / "research" / "experiments"
    if exp_dir.exists():
        exps = list(exp_dir.glob("exp-*.json"))
        results.append({"check": "research_experiments", "verdict": "ok",
                        "message": f"{len(exps)} experiment result files"})

    return results


def check_logging(project: Path) -> list:
    """Logging: data stores, journal sizes, execution log."""
    results = []

    # Decision journal
    dj_path = project / "journal" / "decision_journal.json"
    if dj_path.exists():
        dj = _load_json(dj_path) or []
        results.append({"check": "decision_journal", "verdict": "ok",
                        "message": f"{len(dj)} signal entries"})
        # Check field completeness of latest entry
        if dj:
            last = dj[-1]
            expected = {"timestamp", "ticker", "strategy", "confidence", "features", "action", "market_id"}
            missing = expected - set(last.keys())
            has_market = bool(last.get("market_id"))
            if missing:
                results.append({"check": "dj_fields", "verdict": "warn",
                                "message": f"Latest entry missing: {missing}"})
            elif not has_market:
                results.append({"check": "dj_market_id", "verdict": "warn",
                                "message": "Latest entry has empty market_id — old format"})
    else:
        results.append({"check": "decision_journal", "verdict": "warn", "message": "No decision journal"})

    # Trade ledger
    tl_path = project / "journal" / "trade_ledger.json"
    if tl_path.exists():
        tl = _load_json(tl_path) or []
        results.append({"check": "trade_ledger", "verdict": "ok", "message": f"{len(tl)} ledger entries"})

    # Live execution journal
    ej_path = project / "logs" / "live_executions.jsonl"
    if ej_path.exists():
        lines = _count_lines(ej_path)
        size = _file_size_mb(ej_path)
        results.append({"check": "execution_journal", "verdict": "ok",
                        "message": f"{lines} events, {size:.2f} MB"})
    else:
        results.append({"check": "execution_journal", "verdict": "warn", "message": "No execution journal"})

    # EOD summaries
    eod_dir = project / "logs"
    eod_files = list(eod_dir.glob("eod_summary_*.json"))
    results.append({"check": "eod_summaries", "verdict": "ok" if eod_files else "warn",
                    "message": f"{len(eod_files)} EOD summary files"})

    # Dashboard data
    dash_path = project / "dashboard" / "data" / "dashboard-data.json"
    if dash_path.exists():
        age_h = _age_hours(dash_path)
        results.append({"check": "dashboard_data", "verdict": "ok" if age_h < 24 else "warn",
                        "message": f"Dashboard data is {age_h:.1f}h old"})
    else:
        results.append({"check": "dashboard_data", "verdict": "warn", "message": "No dashboard data file"})

    return results


def check_disk(project: Path) -> list:
    """Disk: project size, log sizes, cache sizes, cleanup needs."""
    results = []

    # Project total
    try:
        r = subprocess.run(["du", "-sh", str(project), "--exclude=.git"], capture_output=True, text=True, timeout=10)
        size = r.stdout.split()[0] if r.stdout else "?"
        results.append({"check": "project_size", "verdict": "ok", "message": f"Project size: {size}"})
    except Exception:
        pass

    # Large logs
    logs_dir = project / "logs"
    if logs_dir.exists():
        large_logs = []
        for f in logs_dir.iterdir():
            if f.is_file() and _file_size_mb(f) > 5:
                large_logs.append(f"{f.name} ({_file_size_mb(f):.1f}MB)")
        if large_logs:
            results.append({"check": "large_logs", "verdict": "warn",
                            "message": f"Large log files: {', '.join(large_logs)}"})

    # atlas.log size
    atlas_log = project / "atlas.log"
    if atlas_log.exists():
        lines = _count_lines(atlas_log)
        size = _file_size_mb(atlas_log)
        results.append({"check": "atlas_log", "verdict": "ok" if size < 10 else "warn",
                        "message": f"atlas.log: {lines} lines, {size:.1f}MB"})

    # __pycache__
    pycache = list(project.rglob("__pycache__"))
    if len(pycache) > 5:
        results.append({"check": "pycache", "verdict": "warn",
                        "message": f"{len(pycache)} __pycache__ dirs — run weekly_maintenance.sh"})

    # Disk free
    try:
        import shutil
        total, used, free = shutil.disk_usage("/")
        free_gb = free / (1024**3)
        results.append({"check": "disk_free", "verdict": "ok" if free_gb > 5 else ("warn" if free_gb > 1 else "fail"),
                        "message": f"{free_gb:.1f} GB free"})
    except Exception:
        pass

    return results


def check_backtest(project: Path, market_id: str) -> list:
    """Backtest: optimization metadata vs baseline, OOS status."""
    results = []
    cfg_path = project / "config" / "active" / f"{market_id}.json"
    if not cfg_path.exists():
        return results

    cfg = _load_json(cfg_path)
    meta = cfg.get("_optimization_metadata", {})

    if not meta:
        results.append({"check": "optimization_meta", "verdict": "warn",
                        "message": "No optimization metadata in config — never optimized?"})
        return results

    sharpe = meta.get("optimized_sharpe", 0)
    cagr = meta.get("optimized_cagr", 0)
    oos_ratio = meta.get("oos_sharpe_ratio", 0)
    perturb_neg = meta.get("perturbation_negative_trials", -1)
    window_pct = meta.get("window_profitable_pct", 0)

    # Sharpe quality
    if sharpe >= 0.8:
        results.append({"check": "sharpe", "verdict": "ok", "message": f"Optimized Sharpe: {sharpe:.3f}"})
    elif sharpe >= 0.3:
        results.append({"check": "sharpe", "verdict": "warn", "message": f"Optimized Sharpe: {sharpe:.3f} (moderate)"})
    else:
        results.append({"check": "sharpe", "verdict": "fail", "message": f"Optimized Sharpe: {sharpe:.3f} (weak)"})

    # CAGR
    results.append({"check": "cagr", "verdict": "ok" if cagr > 0.05 else "warn",
                    "message": f"CAGR: {cagr*100:.1f}%"})

    # OOS validation
    if oos_ratio > 0:
        results.append({"check": "oos_ratio", "verdict": "ok" if oos_ratio >= 0.7 else "warn",
                        "message": f"OOS Sharpe ratio: {oos_ratio:.2f} (OOS/IS)"})
    if perturb_neg >= 0:
        results.append({"check": "perturbation", "verdict": "ok" if perturb_neg == 0 else "warn",
                        "message": f"Perturbation: {perturb_neg}/10 negative CAGR trials"})
    if window_pct > 0:
        results.append({"check": "walk_forward", "verdict": "ok" if window_pct >= 60 else "warn",
                        "message": f"Walk-forward: {window_pct:.0f}% profitable windows"})

    return results


# ── Main ──────────────────────────────────────────────────────

SECTIONS = {
    "infra": ("Infrastructure", check_infra),
    "data": ("Data & Cache", check_data),
    "config": ("Configuration", check_config),
    "broker": ("Broker", check_broker),
    "portfolio": ("Portfolio & Trades", check_portfolio),
    "cron": ("Cron & Automation", check_cron),
    "research": ("Research Pipeline", check_research),
    "logging": ("Logging & Observability", check_logging),
    "disk": ("Disk & Housekeeping", check_disk),
    "backtest": ("Backtest Performance", check_backtest),
}


def run_healthcheck(project: Path, market_id: str = "sp500", sections: list = None) -> dict:
    """Run all health checks and return structured results."""
    report = {
        "timestamp": datetime.now().isoformat(),
        "market_id": market_id,
        "project": str(project),
        "sections": {},
        "summary": {"ok": 0, "warn": 0, "fail": 0},
    }

    for key, (name, fn) in SECTIONS.items():
        if sections and key not in sections:
            continue
        try:
            # Some checks need market_id, some don't
            import inspect
            params = inspect.signature(fn).parameters
            if "market_id" in params:
                checks = fn(project, market_id)
            else:
                checks = fn(project)
        except Exception as e:
            checks = [{"check": f"{key}_error", "verdict": "fail", "message": f"Section crashed: {e}"}]

        report["sections"][key] = {"name": name, "checks": checks}
        for c in checks:
            v = c.get("verdict", "ok")
            if v in report["summary"]:
                report["summary"][v] += 1

    total = sum(report["summary"].values())
    report["summary"]["total"] = total
    report["summary"]["overall"] = (
        "healthy" if report["summary"]["fail"] == 0 and report["summary"]["warn"] == 0
        else "degraded" if report["summary"]["fail"] == 0
        else "unhealthy"
    )

    return report


def format_report(report: dict) -> str:
    """Format report as human-readable text."""
    lines = []
    s = report["summary"]
    icon = {"healthy": "✅", "degraded": "⚠️", "unhealthy": "❌"}[s["overall"]]
    lines.append(f"\n{icon} ATLAS HEALTH CHECK — {s['overall'].upper()}")
    lines.append(f"   Market: {report['market_id']}  |  {s['ok']} ok  {s['warn']} warn  {s['fail']} fail")
    lines.append(f"   {report['timestamp'][:19]}")
    lines.append("")

    verdict_icon = {"ok": "✅", "warn": "⚠️", "fail": "❌"}
    for key, section in report["sections"].items():
        lines.append(f"── {section['name']} ──")
        for c in section["checks"]:
            v = c["verdict"]
            lines.append(f"  {verdict_icon[v]} {c['check']}: {c['message']}")
        lines.append("")

    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="Atlas System Health Check")
    parser.add_argument("--market", "-m", default="sp500", help="Market ID")
    parser.add_argument("--section", "-s", help="Run single section: " + ", ".join(SECTIONS.keys()))
    parser.add_argument("--json", action="store_true", help="Output raw JSON")
    parser.add_argument("--project", default=None, help="Project root override")
    args = parser.parse_args()

    # In JSON mode, suppress stdout noise from SDKs to keep JSON output clean.
    if args.json:
        import io, logging as _logging
        # Redirect stdout during health check run, capture it
        _real_stdout = sys.stdout
        sys.stdout = io.StringIO()

    project = Path(args.project) if args.project else PROJECT
    sections = [args.section] if args.section else None

    report = run_healthcheck(project, args.market, sections)

    if args.json:
        # Restore real stdout and print clean JSON
        sys.stdout = _real_stdout
        print(json.dumps(report, indent=2))
    else:
        print(format_report(report))

    # Exit code
    if report["summary"]["fail"] > 0:
        sys.exit(2)
    elif report["summary"]["warn"] > 0:
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    main()
