#!/usr/bin/env python3
"""Atlas-ASX Daily Automation Pipeline.

Runs each trading morning to:
1. Refresh market data for all universe tickers
2. Generate trade plan for today
3. Execute any approved plans from previous days
4. Refresh dashboard data

Usage:
    python scripts/daily_automation.py [--step all|data|plan|execute|dashboard]
"""
import sys
import os
import json
import logging
import subprocess
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

BRISBANE = ZoneInfo("Australia/Brisbane")
from pathlib import Path

# Setup
PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))
os.chdir(str(PROJECT))

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

# Logging
log_dir = PROJECT / "logs"
log_dir.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(log_dir / "automation.log"),
        logging.StreamHandler()
    ]
)
log = logging.getLogger("automation")

def is_trading_day():
    """Check if today is a weekday (ASX trades Mon-Fri)."""
    today = datetime.now(BRISBANE)
    if today.weekday() >= 5:  # Saturday=5, Sunday=6
        log.info(f"Today is {today.strftime('%A')} - not a trading day")
        return False
    return True

def step_refresh_data():
    """Step 1: Refresh market data for all universe tickers."""
    log.info("=" * 60)
    log.info("STEP 1: Refreshing market data")
    log.info("=" * 60)

    try:
        from data.ingest import download_universe, get_asx200_tickers

        # Load universe from processed file or fall back to hardcoded list
        universe_path = PROJECT / "data" / "processed" / "universe.json"
        if universe_path.exists():
            with open(universe_path) as f:
                universe = json.load(f)
            tickers = universe.get("tickers", [])
        else:
            log.warning("No universe file found, using hardcoded ASX list")
            tickers = get_asx200_tickers()

        log.info(f"Refreshing data for {len(tickers)} tickers")
        results = download_universe(tickers, use_cache=True)

        success = len(results)
        failed = len(tickers) - success
        log.info(f"Data refresh complete: {success} success, {failed} failed")
        return True

    except Exception as e:
        log.error(f"Data refresh failed: {e}")
        import traceback
        log.error(traceback.format_exc())
        return False

def step_generate_plan():
    """Step 2: Generate trade plan for today."""
    log.info("=" * 60)
    log.info("STEP 2: Generating trade plan")
    log.info("=" * 60)
    
    try:
        result = subprocess.run(
            [sys.executable, "scripts/cli.py", "plan"],
            capture_output=True, text=True, cwd=str(PROJECT),
            timeout=300
        )
        log.info(result.stdout)
        if result.stderr:
            log.warning(result.stderr[-500:])
        
        if result.returncode == 0:
            log.info("Trade plan generated successfully")
            return True
        else:
            log.error(f"Plan generation failed with code {result.returncode}")
            return False
            
    except subprocess.TimeoutExpired:
        log.error("Plan generation timed out after 5 minutes")
        return False
    except Exception as e:
        log.error(f"Plan generation failed: {e}")
        return False

def step_execute_approved():
    """Step 3: Execute today's approved plan (paper trading mode)."""
    log.info("=" * 60)
    log.info("STEP 3: Executing approved trades")
    log.info("=" * 60)

    try:
        from types import SimpleNamespace
        from utils.config import load_config
        from paper_engine.engine import PaperPortfolio, TradePlanGenerator

        config = load_config(str(PROJECT / "config" / "active_config.json"))
        portfolio = PaperPortfolio(config)
        planner = TradePlanGenerator(portfolio, config)
        approval_required = config.get("trading", {}).get("approval_required", True)

        # Find today's plan
        today = datetime.now(BRISBANE).strftime("%Y-%m-%d")
        plan = planner.load_plan(today)

        if not plan:
            log.info("No plan for today found")
            return True

        status = plan.get("status", "")
        if status == "EXECUTED":
            log.info("Plan already executed today")
            return True

        if status == "PENDING_APPROVAL":
            if approval_required:
                log.info("Plan is pending approval and trading.approval_required=true; skipping execution")
                return True
            plan = planner.approve_plan(today)
            log.info(f"Plan auto-approved at {plan['approved_at']} (approval_required=false)")

        if plan.get("status") != "APPROVED":
            log.info(f"Plan status is {plan.get('status')} - cannot execute")
            return True

        entries = plan.get("proposed_entries", [])
        if not entries:
            log.info("No entries to execute")
            return True

        # Load sector map
        sector_path = PROJECT / "data" / "processed" / "sector_map.json"
        sector_map = {}
        if sector_path.exists():
            with open(sector_path) as f:
                sector_map = json.load(f)

        # Execute each entry
        executed = 0
        for entry in entries:
            if len(portfolio.positions) >= portfolio.max_positions:
                log.warning(f"Max positions ({portfolio.max_positions}) reached - skipping {entry['ticker']}")
                continue

            signal = SimpleNamespace(
                ticker=entry["ticker"],
                strategy=entry["strategy"],
                entry_price=entry["entry_price"],
                stop_price=entry["stop_price"],
                take_profit=entry.get("take_profit"),
                position_size=entry["position_size"],
                confidence=entry["confidence"],
                rationale=entry["rationale"],
                sector=sector_map.get(entry["ticker"], "Unknown"),
            )

            fill = portfolio.execute_entry(signal, entry["entry_price"], today)
            executed += 1
            log.info(f"FILLED: {fill['ticker']} {fill['shares']}@${fill['fill_price']:.2f} cost=${fill['total_cost']:.2f}")

        # Update plan status
        plan["status"] = "EXECUTED"
        plan["executed_at"] = datetime.now(BRISBANE).isoformat()
        planner._save_plan(plan, today)

        log.info(f"Execution complete: {executed}/{len(entries)} trades filled")
        log.info(f"Portfolio: cash=${portfolio.cash:.2f}, positions={len(portfolio.positions)}, equity=${portfolio.equity():.2f}")
        return True

    except Exception as e:
        log.error(f"Execution failed: {e}")
        import traceback
        log.error(traceback.format_exc())
        return False

def step_refresh_dashboard():
    """Step 4: Refresh dashboard data."""
    log.info("=" * 60)
    log.info("STEP 4: Refreshing dashboard data")
    log.info("=" * 60)
    
    try:
        result = subprocess.run(
            [sys.executable, "dashboard/generate_data.py"],
            capture_output=True, text=True, cwd=str(PROJECT),
            timeout=60
        )
        log.info(result.stdout)
        if result.returncode == 0:
            log.info("Dashboard data refreshed")
            return True
        else:
            log.error(f"Dashboard refresh failed: {result.stderr[-300:]}")
            return False
            
    except Exception as e:
        log.error(f"Dashboard refresh failed: {e}")
        return False

def main():
    step = "all"
    if len(sys.argv) > 1 and sys.argv[1] == "--step":
        step = sys.argv[2] if len(sys.argv) > 2 else "all"
    
    log.info(f"\n{'#' * 60}")
    log.info(f"Atlas-ASX Daily Automation - {datetime.now(BRISBANE).isoformat()}")
    log.info(f"Step: {step}")
    log.info(f"{'#' * 60}")
    
    if step in ("all", "data", "plan", "execute") and not is_trading_day():
        log.info("Not a trading day - refreshing dashboard only")
        step_refresh_dashboard()
        return
    
    results = {}
    
    if step in ("all", "data"):
        results["data"] = step_refresh_data()
    
    if step in ("all", "plan"):
        results["plan"] = step_generate_plan()
    
    if step in ("all", "execute"):
        results["execute"] = step_execute_approved()
    
    if step in ("all", "dashboard"):
        results["dashboard"] = step_refresh_dashboard()
    
    # Summary
    log.info(f"\n{'=' * 60}")
    log.info("AUTOMATION SUMMARY")
    for k, v in results.items():
        status = "SUCCESS" if v else "FAILED"
        log.info(f"  {k}: {status}")
    log.info(f"{'=' * 60}")
    
    # Exit with error if any step failed
    if not all(results.values()):
        sys.exit(1)

if __name__ == "__main__":
    main()

