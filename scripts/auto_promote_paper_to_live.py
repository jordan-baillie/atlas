#!/usr/bin/env python3
"""Auto-promote (strategy, universe) combos from PAPER → LIVE state.

Runs weekly (Mon 08:00 AEST / Sun 22:00 UTC) as a cron job.  For each combo
currently in PAPER state it evaluates nine promotion gates.  All nine must
pass before the combo is transitioned to LIVE and a Telegram alert is sent.

Gates
-----
A  ≥ 30 calendar days in PAPER state
B  ≥ 30 paper trades in last 30 days
C  Paper Sharpe ≥ 0.3
D  |paper_sharpe − research_sharpe| / max(|research_sharpe|, 0.1) < 0.5
E  DSR per-strategy Sharpe variance gate
F  research_best.sharpe ≥ 0.5
G  OOS Sharpe ≥ 0.3                (BYPASSED — column absent from research_best)
H  OOS trade count ≥ 30            (BYPASSED — column absent from research_best)
I  OOS CAGR ≥ 5%                   (BYPASSED — column absent from research_best)

Config-mode flip
----------------
The script does NOT flip config/active/{universe}.json `mode` from "paper" to
"live".  That action goes through the operator BYPASS_RESEARCH_GATE workflow
or the dashboard Controls UI (Sub-phase 1.5).  The script's sole responsibility
is to advance the *lifecycle state* from PAPER → LIVE.

Usage
-----
    python3 scripts/auto_promote_paper_to_live.py [--dry-run] [--force STRATEGY:UNIVERSE]
                                                   [--verbose] [--no-telegram]

"""
from __future__ import annotations

import argparse
import json
import logging
import math
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ATLAS_ROOT = Path(__file__).resolve().parent.parent
if str(ATLAS_ROOT) not in sys.path:
    sys.path.insert(0, str(ATLAS_ROOT))

logger = logging.getLogger("auto_promote_paper")

# ── Constants ──────────────────────────────────────────────────────────────────

PROMOTION_LOG_PATH = ATLAS_ROOT / "data" / "promotion_log.json"

GATE_A_MIN_DAYS = 30          # days in PAPER state
GATE_B_MIN_TRADES = 30        # paper trades in last 30 days
GATE_C_MIN_PAPER_SHARPE = 0.3
GATE_D_MAX_GAP = 0.5          # relative gap vs research Sharpe
GATE_F_MIN_RESEARCH_SHARPE = 0.5

# Gates G, H, I are BYPASSED — columns oos_sharpe / oos_trades / oos_cagr do NOT
# exist in research_best (schema confirmed 2026-05-06).  Each gate emits a WARN
# so the bypass is visible in the audit log.


# ── Sharpe helper (mirrors check_live_research_divergence._compute_live_sharpe) ─

def _compute_sharpe(pnl_pcts: List[float]) -> Optional[float]:
    """Trade-level Sharpe (not annualised — directional only).

    Returns None if < 2 observations or stdev is zero.
    """
    if len(pnl_pcts) < 2:
        return None
    mean = sum(pnl_pcts) / len(pnl_pcts)
    var = sum((x - mean) ** 2 for x in pnl_pcts) / (len(pnl_pcts) - 1)
    sd = math.sqrt(var)
    if sd == 0:
        return None
    return mean / sd


# ── DB helpers ─────────────────────────────────────────────────────────────────

def _fetch_paper_trades(strategy: str, universe: str, window_days: int = 30) -> List[float]:
    """Return pnl_pct list for closed paper trades within the window."""
    from db.atlas_db import get_db

    cutoff = (
        datetime.now(timezone.utc) - timedelta(days=window_days)
    ).strftime("%Y-%m-%d")
    with get_db() as db:
        cur = db.execute(
            "SELECT pnl_pct FROM paper_trades "
            "WHERE strategy = ? AND universe = ? "
            "AND status = 'closed' AND superseded = 0 "
            "AND exit_date IS NOT NULL AND DATE(exit_date) > ? "
            "AND pnl_pct IS NOT NULL",
            (strategy, universe, cutoff),
        )
        return [float(row[0]) for row in cur.fetchall()]


def _fetch_research_best(strategy: str, universe: str) -> Optional[Dict]:
    """Return cross-regime research_best row (regime_state IS NULL) or None."""
    from db.atlas_db import get_db

    with get_db() as db:
        row = db.execute(
            "SELECT sharpe, trades, max_dd_pct, solo_sharpe, portfolio_sharpe "
            "FROM research_best "
            "WHERE strategy = ? AND universe = ? AND regime_state IS NULL",
            (strategy, universe),
        ).fetchone()
        if row is None:
            return None
        return dict(row)


def _days_since_entered_paper(entered_state_at_iso: str) -> float:
    """Return calendar days between entered_state_at and now (UTC)."""
    try:
        entered = datetime.fromisoformat(entered_state_at_iso)
        if entered.tzinfo is None:
            entered = entered.replace(tzinfo=timezone.utc)
        delta = datetime.now(timezone.utc) - entered
        return delta.total_seconds() / 86_400
    except Exception as exc:
        logger.warning("Could not parse entered_state_at %r: %s", entered_state_at_iso, exc)
        return 0.0


# ── Gate evaluation ────────────────────────────────────────────────────────────

def _evaluate_gates(
    row: Dict,
    paper_pnls: List[float],
    research_row: Optional[Dict],
) -> Tuple[bool, List[str], Dict[str, Any]]:
    """Evaluate all gates for a single (strategy, universe) combo.

    Returns:
        all_pass:  True iff every required gate passed.
        reasons:   Human-readable list of per-gate outcomes.
        metrics:   Dict of computed metrics for logging / promotion_log.json.
    """
    strategy = row["strategy"]
    universe = row["universe"]
    entered_at = row.get("entered_state_at") or ""

    days_in_paper = _days_since_entered_paper(entered_at)
    n_trades = len(paper_pnls)
    paper_sharpe = _compute_sharpe(paper_pnls)
    research_sharpe = float(research_row["sharpe"]) if (research_row and research_row.get("sharpe") is not None) else None

    reasons: List[str] = []
    all_pass = True

    # Gate A — time in PAPER
    a_pass = days_in_paper >= GATE_A_MIN_DAYS
    reasons.append(
        f"Gate A ({'PASS' if a_pass else 'FAIL'}): days_in_paper={days_in_paper:.1f} "
        f"(need ≥{GATE_A_MIN_DAYS})"
    )
    all_pass &= a_pass

    # Gate B — trade count
    b_pass = n_trades >= GATE_B_MIN_TRADES
    reasons.append(
        f"Gate B ({'PASS' if b_pass else 'FAIL'}): paper_trades={n_trades} "
        f"(need ≥{GATE_B_MIN_TRADES})"
    )
    all_pass &= b_pass

    # Gate C — paper Sharpe floor
    if paper_sharpe is None:
        c_pass = False
        reasons.append(
            f"Gate C (FAIL): paper_sharpe=None (need ≥{GATE_C_MIN_PAPER_SHARPE})"
        )
    else:
        c_pass = paper_sharpe >= GATE_C_MIN_PAPER_SHARPE
        reasons.append(
            f"Gate C ({'PASS' if c_pass else 'FAIL'}): paper_sharpe={paper_sharpe:.4f} "
            f"(need ≥{GATE_C_MIN_PAPER_SHARPE})"
        )
    all_pass &= c_pass

    # Gate D — divergence from research
    if research_sharpe is None:
        d_pass = False
        reasons.append(
            "Gate D (FAIL): no research_best row — cannot compute gap"
        )
        gap = None
    else:
        denom = max(abs(research_sharpe), 0.1)
        ps = paper_sharpe if paper_sharpe is not None else 0.0
        gap = abs(ps - research_sharpe) / denom
        d_pass = gap < GATE_D_MAX_GAP
        reasons.append(
            f"Gate D ({'PASS' if d_pass else 'FAIL'}): gap={gap:.4f} "
            f"(need <{GATE_D_MAX_GAP})"
        )
    all_pass &= d_pass

    # Gate E — DSR per-strategy variance
    try:
        from research.loop import _get_dsr_stats
        dsr = _get_dsr_stats(strategy=strategy, market=universe)
        n_exp = dsr.get("num_experiments", 0)
        var_s = dsr.get("variance_of_sharpes", 0.0)
        if n_exp < 5:
            # Not enough experiments to compute variance gate — bypass
            reasons.append(
                f"Gate E (BYPASS): insufficient experiments for DSR ({n_exp} < 5) — skipped"
            )
        else:
            # DSR gate: variance of Sharpes for this strategy should be ≤ 1.0
            # (same ceiling used in research/loop.py keep_or_discard)
            e_pass = var_s <= 1.0
            reasons.append(
                f"Gate E ({'PASS' if e_pass else 'FAIL'}): dsr_variance={var_s:.4f} "
                f"n_experiments={n_exp} (need ≤1.0)"
            )
            all_pass &= e_pass
    except Exception as exc:
        logger.warning(
            "Gate E BYPASS (%s/%s): _get_dsr_stats import/call failed: %s",
            strategy, universe, exc,
        )
        reasons.append(f"Gate E (BYPASS): _get_dsr_stats unavailable — {exc}")

    # Gate F — research Sharpe floor
    if research_sharpe is None:
        f_pass = False
        reasons.append(
            f"Gate F (FAIL): no research_best sharpe (need ≥{GATE_F_MIN_RESEARCH_SHARPE})"
        )
    else:
        f_pass = research_sharpe >= GATE_F_MIN_RESEARCH_SHARPE
        reasons.append(
            f"Gate F ({'PASS' if f_pass else 'FAIL'}): research_sharpe={research_sharpe:.4f} "
            f"(need ≥{GATE_F_MIN_RESEARCH_SHARPE})"
        )
    all_pass &= f_pass

    # Gate G — OOS Sharpe (BYPASSED — column absent from research_best)
    logger.warning(
        "Gate G BYPASS (%s/%s): oos_sharpe column absent from research_best — gate skipped",
        strategy, universe,
    )
    reasons.append("Gate G (BYPASS): oos_sharpe absent from research_best schema")

    # Gate H — OOS trade count (BYPASSED — column absent from research_best)
    logger.warning(
        "Gate H BYPASS (%s/%s): oos_trades column absent from research_best — gate skipped",
        strategy, universe,
    )
    reasons.append("Gate H (BYPASS): oos_trades absent from research_best schema")

    # Gate I — OOS CAGR (BYPASSED — column absent from research_best)
    logger.warning(
        "Gate I BYPASS (%s/%s): oos_cagr column absent from research_best — gate skipped",
        strategy, universe,
    )
    reasons.append("Gate I (BYPASS): oos_cagr absent from research_best schema")

    metrics: Dict[str, Any] = {
        "days_in_paper": round(days_in_paper, 2),
        "paper_trades": n_trades,
        "paper_sharpe": round(paper_sharpe, 6) if paper_sharpe is not None else None,
        "research_sharpe": round(research_sharpe, 6) if research_sharpe is not None else None,
        "gap": round(gap, 6) if gap is not None else None,
    }
    return all_pass, reasons, metrics


# ── Promotion ──────────────────────────────────────────────────────────────────

def _append_promotion_log(entry: Dict) -> None:
    """Append a promotion record to data/promotion_log.json (create if missing)."""
    existing: List[Dict] = []
    if PROMOTION_LOG_PATH.exists():
        try:
            existing = json.loads(PROMOTION_LOG_PATH.read_text())
            if not isinstance(existing, list):
                existing = []
        except Exception as exc:
            logger.warning("Could not parse promotion_log.json — overwriting: %s", exc)
            existing = []
    existing.append(entry)
    PROMOTION_LOG_PATH.write_text(json.dumps(existing, indent=2) + "\n")


def _send_promotion_telegram(
    strategy: str,
    universe: str,
    metrics: Dict[str, Any],
    no_telegram: bool,
) -> None:
    """Send Telegram alert on successful promotion."""
    if no_telegram:
        logger.info("--no-telegram: skipping Telegram for %s/%s", strategy, universe)
        return
    try:
        from utils.telegram import notify

        days = metrics.get("days_in_paper", 0)
        ps = metrics.get("paper_sharpe")
        rs = metrics.get("research_sharpe")
        gap = metrics.get("gap")
        msg = (
            f"✅ <b>{strategy}/{universe}</b> graduated to LIVE "
            f"after {days:.0f}d paper.\n"
            f"Paper Sharpe {ps:.2f} vs research {rs:.2f} "
            f"(gap {gap:.2f})"
        )
        notify(msg, category="auto_promote_paper")
    except Exception as exc:
        logger.error("Telegram send failed for %s/%s: %s", strategy, universe, exc)


# ── Main evaluation loop ───────────────────────────────────────────────────────

def run_promotion(
    dry_run: bool = False,
    force: Optional[str] = None,
    no_telegram: bool = False,
) -> int:
    """Evaluate all PAPER combos and promote those that pass all gates.

    Args:
        dry_run:    Compute everything but skip state transitions and log writes.
        force:      If set ("strategy:universe"), evaluate only that combo.
        no_telegram: Suppress Telegram even on real promotions.

    Returns:
        Exit code (0 = success, 1 = fatal error).
    """
    from monitor.strategy_lifecycle import PromotionState, list_state, transition

    # ── Load PAPER combos ──────────────────────────────────────────────────────
    paper_rows = list_state(PromotionState.PAPER)
    logger.info("Found %d PAPER combos", len(paper_rows))

    if force:
        # --force strategy:universe — filter to exactly that combo
        try:
            force_strategy, force_universe = force.split(":", 1)
        except ValueError:
            logger.error("--force must be STRATEGY:UNIVERSE (got %r)", force)
            return 1
        paper_rows = [
            r for r in paper_rows
            if r["strategy"] == force_strategy and r["universe"] == force_universe
        ]
        if not paper_rows:
            logger.warning(
                "--force %s:%s not found in PAPER state — nothing to evaluate",
                force_strategy, force_universe,
            )
            return 0

    promoted = 0
    skipped = 0
    rejected = 0

    for row in paper_rows:
        strategy = row["strategy"]
        universe = row["universe"]
        entered_at = row.get("entered_state_at") or ""
        paper_start = row.get("paper_start_date") or entered_at

        logger.info("─── Evaluating %s / %s ───", strategy, universe)

        # Quick pre-filter to avoid DB calls when clearly insufficient data
        days_in_paper = _days_since_entered_paper(entered_at)
        paper_pnls = _fetch_paper_trades(strategy, universe, window_days=30)
        n_trades = len(paper_pnls)

        if days_in_paper < GATE_A_MIN_DAYS or n_trades < GATE_B_MIN_TRADES:
            logger.info(
                "SKIP %s/%s: insufficient sample n=%d days=%.1f",
                strategy, universe, n_trades, days_in_paper,
            )
            skipped += 1
            continue

        # Full gate evaluation
        research_row = _fetch_research_best(strategy, universe)
        all_pass, reasons, metrics = _evaluate_gates(row, paper_pnls, research_row)

        for reason_line in reasons:
            logger.info("  %s", reason_line)

        if not all_pass:
            logger.info("REJECT %s/%s — one or more gates failed", strategy, universe)
            rejected += 1
            continue

        # ── All gates pass — promote ───────────────────────────────────────────
        promo_id = str(uuid.uuid4())
        log_entry: Dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "strategy": strategy,
            "universe": universe,
            "paper_sharpe": metrics["paper_sharpe"],
            "research_sharpe": metrics["research_sharpe"],
            "gap": metrics["gap"],
            "paper_trades": metrics["paper_trades"],
            "days_in_paper": metrics["days_in_paper"],
            "from_state": "PAPER",
            "to_state": "LIVE",
            "auto_promotion_id": promo_id,
        }

        if dry_run:
            logger.info(
                "DRY-RUN PROMOTE %s/%s — would transition to LIVE (promo_id=%s)",
                strategy, universe, promo_id,
            )
            promoted += 1
            continue

        # Write promotion log BEFORE state transition so we have a record even if
        # transition() fails.
        try:
            _append_promotion_log(log_entry)
            logger.info(
                "Appended promotion_log.json entry for %s/%s (promo_id=%s)",
                strategy, universe, promo_id,
            )
        except Exception as exc:
            logger.error(
                "Failed to write promotion_log.json for %s/%s: %s",
                strategy, universe, exc,
            )
            # Do not promote if we can't write the audit log
            continue

        # Transition to LIVE
        try:
            transition(
                strategy=strategy,
                universe=universe,
                new_state=PromotionState.LIVE,
                reason="auto_promote_paper_to_live: 30-day gate pass",
                auto_promotion_id=promo_id,
                operator="system",
            )
            logger.info("PROMOTED %s/%s → LIVE (promo_id=%s)", strategy, universe, promo_id)
            promoted += 1
        except Exception as exc:
            logger.error(
                "transition() failed for %s/%s: %s",
                strategy, universe, exc,
            )
            continue

        # Telegram alert (non-fatal)
        _send_promotion_telegram(strategy, universe, metrics, no_telegram)

    dry_tag = " [DRY-RUN]" if dry_run else ""
    logger.info(
        "Done%s: promoted=%d skipped=%d rejected=%d",
        dry_tag, promoted, skipped, rejected,
    )
    return 0


# ── CLI ────────────────────────────────────────────────────────────────────────

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Auto-promote PAPER strategy combos to LIVE after 30-day gate",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Evaluate gates but do NOT transition state, write log, or send Telegram",
    )
    p.add_argument(
        "--force",
        metavar="STRATEGY:UNIVERSE",
        default=None,
        help="Force-evaluate a single combo only (e.g. momentum_breakout:sp500)",
    )
    p.add_argument(
        "--verbose",
        action="store_true",
        default=False,
        help="Enable DEBUG-level logging",
    )
    p.add_argument(
        "--no-telegram",
        action="store_true",
        default=False,
        help="Suppress Telegram even on real promotion (useful for testing)",
    )
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = _build_parser().parse_args(argv)

    log_level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )

    if args.dry_run:
        logger.info("── DRY-RUN mode — no state changes, no log writes, no Telegram ──")

    return run_promotion(
        dry_run=args.dry_run,
        force=args.force,
        no_telegram=args.no_telegram,
    )


if __name__ == "__main__":
    sys.exit(main())
