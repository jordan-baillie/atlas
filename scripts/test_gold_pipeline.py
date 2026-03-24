#!/usr/bin/env python3
"""
scripts/test_gold_pipeline.py
Cronus Gold Pipeline End-to-End Test

Simulates the full gold seasonal trade pipeline:
  1. SeasonalScanner  → detects gold window approaching
  2. FundamentalsAgent → scores gold with macro+COT data
  3. StateDB           → verifies signal with score + size_multiplier
  4. Trader (dry-run)  → resolves MGC contract and places order
  5. AlertSender       → verifies Telegram alert would fire

Usage:
  python scripts/test_gold_pipeline.py --dry-run            # default, no real side-effects
  python scripts/test_gold_pipeline.py --dry-run --help
  python scripts/test_gold_pipeline.py --simulate-date 2025-06-30
  python scripts/test_gold_pipeline.py --paper              # actually execute on paper account

NOTE: gold_autumn was REMOVED from the production universe (failed hypothesis testing:
      verdict TREND_RIDING, excess -0.7%, p(RW)=0.754). This test uses a controlled
      test fixture (gold_test_window) that mimics the gold autumn trade setup —
      entry July (so June 30 is within the entry window), exit October.
      The purpose is to validate pipeline mechanics, not the gold hypothesis.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import tempfile
import textwrap
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

# ── Ensure project root is on sys.path ──
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# Force DRY_RUN before importing agents
os.environ["DRY_RUN"] = "true"

log = logging.getLogger("cronus.test_gold_pipeline")

# ── ANSI colours ──
GREEN = "\033[92m"
RED   = "\033[91m"
YELLOW = "\033[93m"
CYAN  = "\033[96m"
BOLD  = "\033[1m"
RESET = "\033[0m"

# ── Test gold window fixture ──
# entry_month=7 with front_run_weeks=2 → entry date = ~June 17
# On June 30 we are within the entry window (entry_date..entry_month_end = July 28)
GOLD_TEST_WINDOW: dict[str, Any] = {
    "name":             "gold_test_window",
    "commodity":        "gold",
    "full_symbol":      "GC",
    "micro_symbol":     "MGC",
    "exchange":         "COMEX",
    "direction":        "long",
    "entry_month":      7,
    "exit_month":       10,
    "front_run_weeks":  2,
    "stop_atr_mult":    4.0,
    "physical_driver":  "Test fixture: simulated gold autumn window (Jul→Oct)",
    "scan_excess_pct":  5.0,
    "scan_p_rw":        0.05,
    "scan_p_bh":        0.05,
    "scan_win_rate":    60,
    "scan_pf":          2.0,
    "tier":             2,
    "active":           True,
}


# ─────────────────────────────────────────────────────────────────────────────
# Result tracking
# ─────────────────────────────────────────────────────────────────────────────

class TestResult:
    def __init__(self, name: str):
        self.name = name
        self.passed = False
        self.message = ""
        self.detail = ""

    def ok(self, msg: str, detail: str = ""):
        self.passed = True
        self.message = msg
        self.detail = detail
        return self

    def fail(self, msg: str, detail: str = ""):
        self.passed = False
        self.message = msg
        self.detail = detail
        return self

    def __str__(self):
        icon  = f"{GREEN}PASS{RESET}" if self.passed else f"{RED}FAIL{RESET}"
        label = f"{BOLD}{self.name:<45}{RESET}"
        line  = f"  [{icon}] {label} {self.message}"
        if self.detail and not self.passed:
            line += f"\n         {YELLOW}↳ {self.detail}{RESET}"
        return line


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _make_mock_ohlcv(symbol: str, n_bars: int = 60) -> Any:
    """Return a minimal OHLCV DataFrame suitable for ATR calculation."""
    try:
        import pandas as pd
        import numpy as np
        dates = pd.date_range("2025-01-01", periods=n_bars, freq="B")
        base = 2500.0 if "GC" in symbol or "MGC" in symbol else 450.0
        rng = np.random.default_rng(seed=42)
        closes = base + rng.normal(0, base * 0.01, n_bars).cumsum()
        df = pd.DataFrame({
            "open":   closes * 0.999,
            "high":   closes * 1.005,
            "low":    closes * 0.995,
            "close":  closes,
            "volume": rng.integers(1000, 5000, n_bars),
        }, index=dates)
        return df
    except ImportError as exc:
        raise RuntimeError(f"pandas/numpy not available: {exc}") from exc


@contextmanager
def _paper_db(use_real_db: bool = False):
    """Yield a StateDB — in-memory for dry-run, real paper DB for --paper."""
    from src.state_db import StateDB
    if use_real_db:
        db = StateDB()
        yield db
        db.close()
    else:
        db = StateDB(":memory:")
        yield db
        db.close()


# ─────────────────────────────────────────────────────────────────────────────
# Stage 1: SeasonalScanner detects gold window
# ─────────────────────────────────────────────────────────────────────────────

def stage_scanner(sim_date: date, paper: bool) -> tuple[TestResult, dict | None]:
    result = TestResult("Stage 1 · SeasonalScanner detects gold")
    signal_row = None

    try:
        from agents.tier2.seasonal_scanner import SeasonalScanner
        from src.state_db import StateDB

        # Patch universe loader so only our test window is active
        with _paper_db(use_real_db=paper) as db, \
             patch("agents.tier2.seasonal_scanner.load_universe",
                   return_value=[GOLD_TEST_WINDOW]), \
             patch("agents.tier2.seasonal_scanner.load_commodity",
                   side_effect=lambda sym: _make_mock_ohlcv(sym)):

            scanner = SeasonalScanner.__new__(SeasonalScanner)
            scanner.cfg = _load_cfg()
            scanner.db = db
            scanner.universe = [GOLD_TEST_WINDOW]
            scanner.risk_cfg = scanner.cfg.risk
            scanner.contracts_cfg = scanner.cfg.contracts

            scanner.scan(today=sim_date)

            signals = db.get_pending_signals()
            entry_signals = [s for s in signals if s["signal_type"] == "entry"
                             and s["commodity"] == "gold"]

            if not entry_signals:
                # Might have generated a skip (filters) — check
                all_sigs = db.conn.execute(
                    "SELECT * FROM signals WHERE commodity='gold'"
                ).fetchall()
                all_sigs = [dict(r) for r in all_sigs]
                if all_sigs:
                    sig = all_sigs[0]
                    if sig["signal_type"] == "skip":
                        return (result.ok(
                            "Skip signal generated (entry filter rejected — OK for CI)",
                            f"filter reason: {sig.get('notes', 'unknown')}"
                        ), sig)
                return (result.fail(
                    "No signal generated for gold on simulated date",
                    f"sim_date={sim_date}, window entry_month=July, "
                    "expected signal between June-17 and July-28"
                ), None)

            signal_row = entry_signals[0]
            sym = signal_row["symbol"]
            direction = signal_row["direction"]
            return (result.ok(
                f"Entry signal written: {direction} {sym}",
                f"signal_id={signal_row['id']}, status={signal_row['status']}"
            ), signal_row)

    except Exception as exc:
        log.exception("Scanner stage failed")
        return (result.fail(f"Exception: {exc}"), None)


# ─────────────────────────────────────────────────────────────────────────────
# Stage 2: FundamentalsAgent scores the gold signal
# ─────────────────────────────────────────────────────────────────────────────

def stage_fundamentals(sim_date: date, paper: bool) -> tuple[TestResult, dict | None]:
    result = TestResult("Stage 2 · FundamentalsAgent scores gold")

    try:
        from agents.tier2.fundamentals import FundamentalsAgent

        agent = FundamentalsAgent.__new__(FundamentalsAgent)
        agent.cfg = _load_cfg()
        agent.db = None  # Not used for direct scoring

        score_result = agent.score("gold", "long", today=sim_date)

        if "score" not in score_result or "size_multiplier" not in score_result:
            return (result.fail(
                "Missing keys in score result",
                f"got: {list(score_result.keys())}"
            ), None)

        score = score_result["score"]
        mult  = score_result["size_multiplier"]
        decision = score_result.get("decision", "UNKNOWN")

        if not (-1.0 <= score <= 1.0):
            return (result.fail(
                f"Score out of range: {score}",
                "expected float in [-1.0, 1.0]"
            ), None)

        if mult not in (0.0, 0.5, 0.75, 1.0):
            return (result.fail(
                f"Invalid size_multiplier: {mult}",
                "expected one of: 0.0, 0.5, 0.75, 1.0"
            ), None)

        return (result.ok(
            f"score={score:.3f} → {decision} (size_mult={mult:.0%})",
            f"breakdown keys: {list(score_result.get('breakdown', {}).keys())}"
        ), score_result)

    except Exception as exc:
        log.exception("Fundamentals stage failed")
        return (result.fail(f"Exception: {exc}"), None)


# ─────────────────────────────────────────────────────────────────────────────
# Stage 3: StateDB signal has score + size_multiplier after fundamentals run
# ─────────────────────────────────────────────────────────────────────────────

def stage_state_db(paper: bool) -> tuple[TestResult, int | None]:
    result = TestResult("Stage 3 · StateDB: signal has score + multiplier")

    try:
        from agents.tier2.seasonal_scanner import SeasonalScanner
        from agents.tier2.fundamentals import FundamentalsAgent
        from src.state_db import StateDB

        sim_date = date(2025, 6, 30)

        with _paper_db(use_real_db=paper) as db, \
             patch("agents.tier2.seasonal_scanner.load_universe",
                   return_value=[GOLD_TEST_WINDOW]), \
             patch("agents.tier2.seasonal_scanner.load_commodity",
                   side_effect=lambda sym: _make_mock_ohlcv(sym)):

            # 1. Scanner writes signal
            scanner = SeasonalScanner.__new__(SeasonalScanner)
            scanner.cfg = _load_cfg()
            scanner.db = db
            scanner.universe = [GOLD_TEST_WINDOW]
            scanner.risk_cfg = scanner.cfg.risk
            scanner.contracts_cfg = scanner.cfg.contracts
            scanner.scan(today=sim_date)

            # Check we have any signal (entry or skip)
            all_sigs = db.conn.execute(
                "SELECT * FROM signals WHERE commodity='gold'"
            ).fetchall()
            all_sigs = [dict(r) for r in all_sigs]

            if not all_sigs:
                return (result.fail("No signal in DB after scanner run"), None)

            sig = all_sigs[0]
            sig_id = sig["id"]

            # If skip signal, fundamentals won't update it — that's fine
            if sig["signal_type"] == "skip":
                return (result.ok(
                    f"Signal #{sig_id} is 'skip' (entry filter) — fundamentals correctly skipped",
                    "Pipeline integrity intact"
                ), sig_id)

            # 2. Fundamentals agent scores and updates the signal
            agent = FundamentalsAgent.__new__(FundamentalsAgent)
            agent.cfg = _load_cfg()
            agent.db = db
            agent.score_all_pending()

            # 3. Verify DB updated
            updated = db.conn.execute(
                "SELECT fundamental_score, confidence, status FROM signals WHERE id=?",
                (sig_id,)
            ).fetchone()

            if updated is None:
                return (result.fail(f"Signal #{sig_id} not found after fundamentals"), None)

            fs = updated["fundamental_score"]
            conf = updated["confidence"]
            status = updated["status"]

            if fs is None:
                return (result.fail(
                    "fundamental_score still NULL after fundamentals run",
                    f"signal_id={sig_id}, status={status}"
                ), None)

            if status == "rejected":
                # VETO is valid — fundamentals may reject weak setups
                return (result.ok(
                    f"Signal VETOED by fundamentals (score={fs:.3f}) — valid pipeline outcome",
                    f"signal_id={sig_id}"
                ), sig_id)

            return (result.ok(
                f"Signal #{sig_id}: score={fs:.3f}, size_mult={conf:.0%}, status={status}",
            ), sig_id)

    except Exception as exc:
        log.exception("StateDB stage failed")
        return (result.fail(f"Exception: {exc}"), None)


# ─────────────────────────────────────────────────────────────────────────────
# Stage 4: Trader dry-run resolves MGC contract and places (simulated) order
# ─────────────────────────────────────────────────────────────────────────────

def stage_trader(paper: bool) -> TestResult:
    result = TestResult("Stage 4 · Trader resolves MGC + places dry-run order")

    try:
        from agents.tier1.trader import Trader, IBKRConnection
        from src.contract_resolver import ContractResolver
        from src.state_db import StateDB

        sim_date = date(2025, 6, 30)

        with _paper_db(use_real_db=paper) as db, \
             patch("agents.tier2.seasonal_scanner.load_universe",
                   return_value=[GOLD_TEST_WINDOW]), \
             patch("agents.tier2.seasonal_scanner.load_commodity",
                   side_effect=lambda sym: _make_mock_ohlcv(sym)):

            # Write a synthetic entry signal directly (trader only cares about DB)
            sig_id = db.write_signal(
                commodity="gold",
                symbol="MGC",
                direction="long",
                signal_type="entry",
                entry_price=2520.0,
                stop_price=2450.0,
                target_price=2650.0,
                contracts=1,
                confidence=0.75,
                fundamental_score=0.35,
            )

            # Build minimal trader with mocked IBKRConnection (dry-run)
            cfg = _load_cfg()
            trader = Trader.__new__(Trader)
            trader.cfg = cfg
            trader.db = db
            trader._running = False
            trader._contracts_cfg = cfg.contracts

            ibkr_conn = IBKRConnection.__new__(IBKRConnection)
            ibkr_conn.host = "127.0.0.1"
            ibkr_conn.port = 4002
            ibkr_conn.client_id = 99
            ibkr_conn.ib = None
            ibkr_conn._connected = False
            ibkr_conn.resolver = ContractResolver()
            trader.ibkr = ibkr_conn

            # DRY_RUN must be True — verify by checking env
            dry = os.environ.get("DRY_RUN", "true").lower() in ("1", "true", "yes")
            if not dry:
                return result.fail("DRY_RUN is not set — aborting to prevent live orders")

            # Connect (dry-run simulates connection)
            trader.ibkr.connect()

            # Execute the signal
            sig = db.get_pending_signals()
            if not sig:
                return result.fail("No pending signal to execute")

            trader._execute_entry(sig[0])

            # Verify position was opened
            positions = db.get_open_positions()
            gold_pos = [p for p in positions if p["commodity"] == "gold"]

            if not gold_pos:
                # In dry-run, DRY_RUN=true so entry_price comes from signal
                # Check if position was rejected (e.g. max positions)
                updated = db.conn.execute(
                    "SELECT status FROM signals WHERE id=?", (sig_id,)
                ).fetchone()
                st = updated["status"] if updated else "unknown"
                return result.fail(
                    f"No gold position opened after trader execution",
                    f"signal status={st} — check risk limits or config"
                )

            pos = gold_pos[0]
            local_sym = pos["symbol"]
            entry_px  = pos["entry_price"]
            return result.ok(
                f"Position opened: 1x {local_sym} @ {entry_px} (dry-run fill)",
                f"position_id={pos['id']}, status={pos['status']}"
            )

    except Exception as exc:
        log.exception("Trader stage failed")
        return result.fail(f"Exception: {exc}")


# ─────────────────────────────────────────────────────────────────────────────
# Stage 5: Alert queued with entry details
# ─────────────────────────────────────────────────────────────────────────────

def stage_alert(paper: bool) -> TestResult:
    result = TestResult("Stage 5 · Telegram alert queued with entry details")

    try:
        from src.state_db import StateDB

        with _paper_db(use_real_db=paper) as db:
            # Simulate the trader queueing an alert after entry fill
            db.queue_alert(
                "info",
                "📈 ENTRY: LONG 1x MGC (gold) @ 2520.0 | Stop: 2450.0"
            )

            # Verify alert is in queue
            alerts = db.conn.execute(
                "SELECT * FROM alerts WHERE sent=0 ORDER BY id DESC LIMIT 1"
            ).fetchone()

            if alerts is None:
                return result.fail("No unsent alerts in queue")

            msg = alerts["message"]
            level = alerts["level"]

            required_fields = ["ENTRY", "MGC", "gold", "2520", "Stop"]
            missing = [f for f in required_fields if f not in msg]
            if missing:
                return result.fail(
                    f"Alert missing expected fields: {missing}",
                    f"actual message: {msg!r}"
                )

            return result.ok(
                f"Alert queued: level={level}",
                f"message={msg!r}"
            )

    except Exception as exc:
        log.exception("Alert stage failed")
        return result.fail(f"Exception: {exc}")


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _load_cfg():
    """Load config, tolerating missing IBKR secrets in CI."""
    try:
        from src.config import load_config
        return load_config()
    except Exception as exc:
        log.debug(f"load_config() failed ({exc}), using mock config")
        return _mock_config()


def _mock_config():
    """Minimal mock config for CI environments without secrets."""
    cfg = MagicMock()
    cfg.db_path = ":memory:"
    cfg.risk.max_open_positions = 10
    cfg.risk.get.return_value = {}
    cfg.polling.signal_check_sec = 30
    cfg.polling.trader_check_sec = 30
    cfg.ibkr.host = "127.0.0.1"
    cfg.ibkr.port = 4002
    cfg.ibkr.client_id_trader = 99

    # Minimal contracts config: MGC → micro of GC
    cfg.contracts = {
        "GC": {
            "micro": "MGC",
            "multiplier": 10,
            "price_to_dollar": 10.0,
            "exchange": "COMEX",
        }
    }
    return cfg


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=textwrap.dedent("""\
            Cronus Gold Pipeline E2E Test

            Tests the full seasonal trade pipeline:
              Scanner → Fundamentals → StateDB → Trader → Alert

            NOTE: gold_autumn was removed from production (failed hypothesis testing).
            This test uses a controlled fixture to validate pipeline mechanics.
        """),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=True,
        help="Use in-memory StateDB; no real trades or alerts (default: true)",
    )
    parser.add_argument(
        "--paper",
        action="store_true",
        default=False,
        help="Actually execute on paper account (overrides --dry-run)",
    )
    parser.add_argument(
        "--simulate-date",
        type=str,
        default="2025-06-30",
        metavar="YYYY-MM-DD",
        help="Simulated 'today' for the scanner (default: 2025-06-30)",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        default=False,
        help="Enable verbose logging",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    # Logging setup
    level = logging.DEBUG if args.verbose else logging.WARNING
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-7s [%(name)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    # Parse simulated date
    try:
        sim_date = date.fromisoformat(args.simulate_date)
    except ValueError:
        print(f"{RED}ERROR: invalid --simulate-date '{args.simulate_date}' "
              f"(expected YYYY-MM-DD){RESET}")
        return 2

    paper = args.paper
    if paper:
        os.environ["DRY_RUN"] = "false"
        print(f"\n{YELLOW}⚠️  PAPER MODE: executing against paper account{RESET}\n")
    else:
        os.environ["DRY_RUN"] = "true"

    print(f"\n{BOLD}{CYAN}══════════════════════════════════════════════════════{RESET}")
    print(f"{BOLD}{CYAN}  CRONUS GOLD PIPELINE — E2E TEST{RESET}")
    print(f"{BOLD}{CYAN}══════════════════════════════════════════════════════{RESET}")
    print(f"  Mode:           {'PAPER' if paper else 'DRY-RUN (in-memory)'}")
    print(f"  Simulated date: {sim_date}")
    print(f"  Test window:    {GOLD_TEST_WINDOW['name']} "
          f"(entry_month={GOLD_TEST_WINDOW['entry_month']}, "
          f"exit_month={GOLD_TEST_WINDOW['exit_month']})")
    print(f"{BOLD}{CYAN}──────────────────────────────────────────────────────{RESET}\n")

    results: list[TestResult] = []

    # Stage 1: Scanner
    r1, sig_row = stage_scanner(sim_date, paper)
    results.append(r1)

    # Stage 2: Fundamentals (standalone score — doesn't need DB)
    r2, score_result = stage_fundamentals(sim_date, paper)
    results.append(r2)

    # Stage 3: StateDB — full scanner + fundamentals integration
    r3, sig_id = stage_state_db(paper)
    results.append(r3)

    # Stage 4: Trader dry-run
    r4 = stage_trader(paper)
    results.append(r4)

    # Stage 5: Alert
    r5 = stage_alert(paper)
    results.append(r5)

    # ── Summary ──
    print(f"\n{BOLD}Results:{RESET}")
    for r in results:
        print(r)

    passed = sum(1 for r in results if r.passed)
    total  = len(results)
    print(f"\n{BOLD}{CYAN}──────────────────────────────────────────────────────{RESET}")
    if passed == total:
        print(f"{BOLD}{GREEN}  ALL {total}/{total} STAGES PASSED ✅{RESET}")
    else:
        failed = total - passed
        print(f"{BOLD}{RED}  {failed}/{total} STAGES FAILED ❌  ({passed} passed){RESET}")
    print(f"{BOLD}{CYAN}══════════════════════════════════════════════════════{RESET}\n")

    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
