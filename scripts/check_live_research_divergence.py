#!/usr/bin/env python3
"""Daily live-vs-research Sharpe divergence monitor.

For each (universe, strategy) in research_best, compute the live trade-level
Sharpe over the last 30 days and compare to research_best.sharpe. Alert via
Telegram if the gap exceeds threshold.

Sub-phase 1.4 extension: consecutive-day breach tracking with auto-rollback.
  - PAPER state: gap > threshold for 5+ consecutive days → PAPER → RESEARCH
  - LIVE state:  gap > threshold for 5+ consecutive days → Telegram escalation
    alert + health state demoted to WATCH via force_to_watch (Item 3).
    Sub-phase 1.4 + Item 3: LIVE breach demotes health state to WATCH via
    force_to_watch + Telegram alert. Promotion state remains LIVE (operator
    action via Controls tab still required for full rollback).

Per audit 2026-05-06 Recommendation 4.

Usage:
    python3 scripts/check_live_research_divergence.py
    python3 scripts/check_live_research_divergence.py --dry-run-telegram
    python3 scripts/check_live_research_divergence.py --window-days 60 --gap-threshold 0.3
    python3 scripts/check_live_research_divergence.py --no-rollback
    python3 scripts/check_live_research_divergence.py --state-file /tmp/test_state.json
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

ATLAS_ROOT = Path(__file__).resolve().parent.parent
if str(ATLAS_ROOT) not in sys.path:
    sys.path.insert(0, str(ATLAS_ROOT))

logger = logging.getLogger("divergence")

DEFAULT_WINDOW_DAYS = 30
DEFAULT_GAP_THRESHOLD = 0.5  # alert if research_sharpe - live_sharpe > 0.5
MIN_TRADES_FOR_LIVE_SHARPE = 5

# ── Sub-phase 1.4 constants ───────────────────────────────────────────────────

DEFAULT_STATE_FILE = ATLAS_ROOT / "data" / "divergence_state.json"
ROLLBACK_CONSECUTIVE_DAYS = 5  # consecutive breach days before rollback fires
PROMOTION_LOG_PATH = ATLAS_ROOT / "data" / "promotion_log.json"


# ── Core computation (original, unchanged) ────────────────────────────────────


def _compute_live_sharpe(pnl_pcts: List[float]) -> Optional[float]:
    """Trade-level Sharpe (NOT annualised — directional only).

    Returns None if fewer than 2 trades or stdev is zero.
    """
    if len(pnl_pcts) < 2:
        return None
    mean = sum(pnl_pcts) / len(pnl_pcts)
    var = sum((x - mean) ** 2 for x in pnl_pcts) / (len(pnl_pcts) - 1)
    sd = math.sqrt(var)
    if sd == 0:
        return None
    return mean / sd


def _fetch_research_best_rows() -> List[Dict]:
    """Return list of row dicts from research_best where sharpe IS NOT NULL."""
    from db.atlas_db import get_db

    with get_db() as db:
        cur = db.execute(
            "SELECT universe, strategy, sharpe, trades, updated_at "
            "FROM research_best "
            "WHERE sharpe IS NOT NULL"
        )
        return [dict(r) for r in cur.fetchall()]


def _fetch_live_trades(universe: str, strategy: str, window_days: int) -> List[float]:
    """Return list of pnl_pct for closed, non-superseded trades within window."""
    from db.atlas_db import get_db

    cutoff = (
        datetime.now(timezone.utc) - timedelta(days=window_days)
    ).strftime("%Y-%m-%d")
    with get_db() as db:
        cur = db.execute(
            "SELECT pnl_pct FROM trades "
            "WHERE universe = ? AND strategy = ? "
            "AND status = 'closed' AND COALESCE(superseded, 0) = 0 "
            "AND exit_date IS NOT NULL "
            "AND DATE(exit_date) > ? "
            "AND pnl_pct IS NOT NULL",
            (universe, strategy, cutoff),
        )
        return [float(row[0]) for row in cur.fetchall()]


def compute_divergences(
    window_days: int = DEFAULT_WINDOW_DAYS,
    gap_threshold: float = DEFAULT_GAP_THRESHOLD,
) -> List[Dict]:
    """Return list of divergence records sorted by gap descending.

    Each record: {universe, strategy, research_sharpe, live_sharpe, gap,
                  live_trades, trust_score, severity}

    Only records with >= MIN_TRADES_FOR_LIVE_SHARPE live trades are included.
    """
    out: List[Dict] = []
    for r in _fetch_research_best_rows():
        universe = r["universe"]
        strategy = r["strategy"]
        research_sharpe = float(r["sharpe"]) if r["sharpe"] is not None else 0.0

        pnls = _fetch_live_trades(universe, strategy, window_days)
        n = len(pnls)
        if n < MIN_TRADES_FOR_LIVE_SHARPE:
            continue  # insufficient live data

        live_sharpe = _compute_live_sharpe(pnls)
        if live_sharpe is None:
            continue

        gap = research_sharpe - live_sharpe

        logger.info(
            "reading: universe=%s strategy=%s research_sharpe=%.4f live_sharpe=%.4f gap=%+.4f n_trades=%d",
            universe, strategy, research_sharpe, live_sharpe, gap, n,
        )

        # Trust score: live / research, clamped [0, 2].
        # Undefined when research_sharpe <= 0 (would invert or divide-by-zero).
        if research_sharpe > 0:
            trust: Optional[float] = max(0.0, min(2.0, live_sharpe / research_sharpe))
        else:
            trust = None

        if gap >= gap_threshold or (research_sharpe > 0 and live_sharpe < 0):
            severity = "🔴" if (gap >= 1.0 or live_sharpe < -1.0) else "🟡"
        else:
            severity = "🟢"

        out.append(
            {
                "universe": universe,
                "strategy": strategy,
                "research_sharpe": research_sharpe,
                "live_sharpe": live_sharpe,
                "gap": gap,
                "live_trades": n,
                "trust_score": trust,
                "severity": severity,
            }
        )

    out.sort(key=lambda d: d["gap"], reverse=True)
    return out


def format_telegram(divergences: List[Dict], gap_threshold: float) -> str:
    """Build Telegram HTML message. Alerting rows listed first."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    alerting = [d for d in divergences if d["severity"] in ("🔴", "🟡")]
    healthy = [d for d in divergences if d["severity"] == "🟢"]

    lines = [f"⚠️ <b>Research-Live Divergence ({today})</b>"]
    if alerting:
        lines.append(f"Threshold: gap > {gap_threshold:.2f}")
        lines.append("")
        lines.append("<b>Divergent strategies:</b>")
        for d in alerting:
            trust_str = (
                f"trust {d['trust_score']:.2f}"
                if d["trust_score"] is not None
                else "trust n/a"
            )
            lines.append(
                f"{d['severity']} {d['universe']}/{d['strategy']}: "
                f"research {d['research_sharpe']:+.2f}, "
                f"live {d['live_sharpe']:+.2f} "
                f"(gap {d['gap']:+.2f}, {trust_str}, n={d['live_trades']})"
            )
    else:
        lines.append(
            "✅ No divergence alerts. "
            "All live strategies tracking research within threshold."
        )

    if healthy:
        lines.append("")
        lines.append(
            f"<i>Healthy: {len(healthy)} strategies (gap &lt; {gap_threshold:.2f})</i>"
        )

    return "\n".join(lines)


# ── State management (Sub-phase 1.4) ─────────────────────────────────────────


def _load_state(state_file: Path) -> Dict[str, Any]:
    """Load divergence state from JSON file. Returns empty dict if missing/corrupt."""
    if not state_file.exists():
        return {}
    try:
        data = json.loads(state_file.read_text())
        if not isinstance(data, dict):
            logger.warning("divergence_state.json has unexpected type — resetting")
            return {}
        return data
    except Exception as exc:
        logger.warning("Could not load divergence state from %s: %s", state_file, exc)
        return {}


def _save_state_atomic(state: Dict[str, Any], state_file: Path) -> None:
    """Save state to JSON file atomically (temp file + rename)."""
    state_file.parent.mkdir(parents=True, exist_ok=True)
    tmp = state_file.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2) + "\n")
    os.replace(str(tmp), str(state_file))


def _compute_updated_entry(
    entry: Dict[str, Any],
    is_breach: bool,
    today_str: str,
    yesterday_str: str,
) -> Dict[str, Any]:
    """Return updated per-combo state entry (mutates in place, also returns).

    Streak increment rules:
      - Breach today AND last_check_date == yesterday AND last_breach_date == yesterday
        → unbroken consecutive streak → increment
      - Breach today but streak was broken (first day, or skipped a day, or
        yesterday was clean) → new streak of 1
      - Not a breach today → reset streak to 0 (spec: gap below threshold → reset)
      - Skipped >1 day → even if breach today, reset streak to 1 (broken window)
    """
    last_check = entry.get("last_check_date")
    last_breach = entry.get("last_breach_date")
    current_streak = int(entry.get("consecutive_breach_days", 0))

    if is_breach:
        if last_check == yesterday_str and last_breach == yesterday_str:
            # Unbroken consecutive streak from yesterday
            new_streak = current_streak + 1
        else:
            # Streak broken (first breach, skipped day, or yesterday was clean)
            new_streak = 1
        entry["consecutive_breach_days"] = new_streak
        entry["last_breach_date"] = today_str
    else:
        # Not a breach today — reset
        entry["consecutive_breach_days"] = 0

    entry["last_check_date"] = today_str
    return entry


def _append_rollback_log(log_entry: Dict[str, Any]) -> None:
    """Append a rollback/escalation record to data/promotion_log.json (reuse 1.3 log)."""
    existing: List[Dict] = []
    if PROMOTION_LOG_PATH.exists():
        try:
            existing = json.loads(PROMOTION_LOG_PATH.read_text())
            if not isinstance(existing, list):
                existing = []
        except Exception as exc:
            logger.warning("Could not parse promotion_log.json — overwriting: %s", exc)
            existing = []
    existing.append(log_entry)
    PROMOTION_LOG_PATH.write_text(json.dumps(existing, indent=2) + "\n")


def _send_telegram_alert(
    message: str,
    category: str,
    dry_run_telegram: bool,
    no_telegram: bool,
) -> None:
    """Send or print a Telegram notification (non-fatal)."""
    if dry_run_telegram:
        print(f"\n[DRY RUN] Telegram ({category}):\n{message}")
        return
    if no_telegram:
        return
    try:
        from utils.telegram import notify

        notify(message, category=category)
    except Exception as exc:
        logger.error("Telegram send failed (category=%s): %s", category, exc)


def process_rollbacks(
    divergences: List[Dict],
    state: Dict[str, Any],
    gap_threshold: float,
    today_str: str,
    no_rollback: bool,
    dry_run_telegram: bool,
    no_telegram: bool,
) -> List[str]:
    """Update per-combo breach counters and fire rollbacks/escalations as needed.

    Iterates over ``divergences`` (all combos with sufficient live data).
    Mutates ``state`` in place (per-combo entries updated).

    Returns list of alert message strings that were sent/printed.

    LIVE state handling:
        Sub-phase 1.4 + Item 3: LIVE breach for ROLLBACK_CONSECUTIVE_DAYS also
        demotes the health state to WATCH via StrategyLifecycleManager.force_to_watch.
        Promotion state stays LIVE (operator action via Controls tab still required
        for full rollback). Telegram escalation alert also fires.
    """
    yesterday_str = (
        datetime.strptime(today_str, "%Y-%m-%d").date() - timedelta(days=1)
    ).isoformat()

    alerts: List[str] = []

    # Dedup: research_best can have multiple rows per (strategy, universe) when
    # regime_state rows exist (Sub-phase Rec 5).  Process each unique key once,
    # taking the worst-case (highest gap) entry — divergences is already sorted
    # by gap descending so we just skip keys we've already seen.
    seen_keys: set = set()

    # Import lifecycle module once (module-level reference for test mocking)
    try:
        import monitor.strategy_lifecycle as _slm

        _PromotionState = _slm.PromotionState
        _lifecycle_available = True
    except Exception as exc:
        logger.warning("monitor.strategy_lifecycle unavailable: %s", exc)
        _slm = None  # type: ignore[assignment]
        _PromotionState = None
        _lifecycle_available = False

    for d in divergences:
        strategy = d["strategy"]
        universe = d["universe"]
        research_sharpe = d["research_sharpe"]
        gap_val = d["gap"]

        # Only track combos with meaningful research Sharpe (spec requirement)
        if research_sharpe <= 0:
            continue

        key = f"{strategy}:{universe}"
        if key in seen_keys:
            # Skip duplicate regime_state rows for the same strategy/universe.
            # First entry already has the worst-case gap (list is sorted desc).
            continue
        seen_keys.add(key)
        entry = state.get(key, {})

        # Determine breach status for today
        is_breach = gap_val > gap_threshold

        # Get current promotion state
        promo_state = None
        if _lifecycle_available:
            try:
                promo_state = _slm.get_state(strategy, universe)
            except Exception as exc:
                logger.warning(
                    "Could not get promotion state for %s/%s: %s",
                    strategy, universe, exc,
                )

        entry["current_state"] = promo_state.value if promo_state else "UNKNOWN"

        # Update consecutive breach counter
        entry = _compute_updated_entry(entry, is_breach, today_str, yesterday_str)
        state[key] = entry

        n_days = entry["consecutive_breach_days"]

        # ── Rollback / escalation gate ─────────────────────────────────────────
        if n_days < ROLLBACK_CONSECUTIVE_DAYS or not is_breach or no_rollback:
            continue

        # n_days >= 5, is_breach, no_rollback=False
        if not _lifecycle_available or _PromotionState is None:
            logger.warning(
                "Rollback threshold reached for %s/%s but lifecycle module "
                "unavailable — sending alert only.",
                strategy, universe,
            )
            alert = (
                f"⚠️ <b>{strategy}/{universe}</b> divergence threshold reached "
                f"({n_days} days, gap {gap_val:.2f}) but lifecycle module "
                f"unavailable. Manual intervention required."
            )
            alerts.append(alert)
            _send_telegram_alert(
                alert, "divergence_rollback", dry_run_telegram, no_telegram
            )
            continue

        if promo_state == _PromotionState.PAPER:
            # ── PAPER → RESEARCH auto-rollback ────────────────────────────────
            rollback_id = str(uuid.uuid4())
            reason = (
                f"auto_rollback: gap > {gap_threshold} for "
                f"{n_days} consecutive days"
            )
            try:
                _slm.transition(
                    strategy=strategy,
                    universe=universe,
                    new_state=_PromotionState.RESEARCH,
                    reason=reason,
                    auto_promotion_id=rollback_id,
                    operator="system",
                )
                logger.info(
                    "Auto-rolled back %s/%s PAPER → RESEARCH "
                    "(gap=%.2f, consecutive_days=%d)",
                    strategy, universe, gap_val, n_days,
                )
            except Exception as exc:
                logger.error(
                    "Failed to rollback %s/%s: %s", strategy, universe, exc
                )
                continue

            # Reset streak after successful rollback
            entry["consecutive_breach_days"] = 0

            # Audit log (reuse promotion_log.json from Sub-phase 1.3)
            log_entry: Dict[str, Any] = {
                "ts": datetime.now(timezone.utc).isoformat(),
                "strategy": strategy,
                "universe": universe,
                "from_state": "PAPER",
                "to_state": "RESEARCH",
                "gap": round(gap_val, 4),
                "consecutive_breach_days": n_days,
                "auto_promotion_id": rollback_id,
                "reason": reason,
                "note": "Paper positions left for manual review.",
            }
            try:
                _append_rollback_log(log_entry)
            except Exception as exc:
                logger.warning("Could not write rollback log: %s", exc)

            alert = (
                f"⚠️ <b>{strategy}/{universe}</b> auto-rolled back "
                f"PAPER → RESEARCH (gap {gap_val:.2f} for {n_days} days).\n"
                f"Paper positions left for manual review."
            )
            alerts.append(alert)
            _send_telegram_alert(
                alert, "divergence_rollback", dry_run_telegram, no_telegram
            )

        elif promo_state == _PromotionState.LIVE:
            # ── LIVE: escalation alert + health-state demotion to WATCH ─────────
            # Sub-phase 1.4 + Item 3: Telegram alert fires AND health state is
            # demoted to WATCH via force_to_watch. Promotion state stays LIVE.
            alert = (
                f"⚠️ <b>{strategy}/{universe}</b> LIVE divergence persistent "
                f"({n_days} days, gap {gap_val:.2f}). "
                f"Health → WATCH recommended. "
                f"Review at Controls tab. Live positions UNTOUCHED."
            )
            alerts.append(alert)
            _send_telegram_alert(
                alert, "divergence_escalation", dry_run_telegram, no_telegram
            )
            logger.warning(
                "LIVE divergence escalation for %s/%s: gap=%.2f for %d days. "
                "Health state demoted to WATCH via force_to_watch.",
                strategy, universe, gap_val, n_days,
            )

            # Auto-demote LIVE → WATCH on health state machine
            try:
                from monitor.lifecycle import StrategyLifecycleManager
                from utils.config import get_active_config
                _cfg = get_active_config(universe)
                _lcm = StrategyLifecycleManager(_cfg, market_id=universe)
                _watch_reason = (
                    f"divergence_breach: gap > {gap_threshold} for {n_days} consecutive days "
                    f"(live_sharpe vs research_sharpe)"
                )
                _transitioned = _lcm.force_to_watch(strategy, _watch_reason)
                if _transitioned:
                    logger.info(
                        "force_to_watch: %s health state demoted to WATCH (universe=%s)",
                        strategy, universe,
                    )
                    # Append rollback log entry too (mirroring the PAPER → RESEARCH path)
                    log_entry: Dict[str, Any] = {
                        "ts": datetime.now(timezone.utc).isoformat(),
                        "strategy": strategy,
                        "universe": universe,
                        "from_state": "LIVE",
                        "to_state": "LIVE",  # promotion state stays LIVE
                        "health_state_to": "WATCH",
                        "gap": round(gap_val, 4),
                        "consecutive_breach_days": n_days,
                        "auto_promotion_id": str(uuid.uuid4()),
                        "reason": _watch_reason,
                        "note": "Promotion state remains LIVE; health state demoted to WATCH for operator review.",
                    }
                    try:
                        _append_rollback_log(log_entry)
                    except Exception as exc:
                        logger.warning("Could not write rollback log: %s", exc)
                else:
                    logger.info(
                        "force_to_watch: %s already in WATCH-or-worse, no health transition",
                        strategy,
                    )
            except Exception as exc:
                logger.error("force_to_watch failed for %s/%s: %s", strategy, universe, exc)

    return alerts


# ── Main orchestration (Sub-phase 1.4 wiring) ─────────────────────────────────


def run_divergence_check(
    window_days: int = DEFAULT_WINDOW_DAYS,
    gap_threshold: float = DEFAULT_GAP_THRESHOLD,
    state_file: Path = DEFAULT_STATE_FILE,
    no_rollback: bool = False,
    dry_run_telegram: bool = False,
    no_telegram: bool = False,
    today: Optional[str] = None,
    force_rerun: bool = False,
) -> int:
    """Full divergence check cycle. Factored out of main() for testability.

    Returns 0 on success, 1 on Telegram send error.
    """
    today_str = today or datetime.now(timezone.utc).date().isoformat()

    # Load persisted breach-tracking state
    state = _load_state(state_file)

    # Idempotency: exit early if script already ran today
    if state.get("_last_run_date") == today_str and not force_rerun:
        logger.info("Already checked today (%s) — skipping", today_str)
        return 0

    # ── Compute divergences (original logic, unchanged) ───────────────────────
    divergences = compute_divergences(window_days, gap_threshold)

    # Summary log — one line per run (always, regardless of Telegram send).
    n_breaches = sum(1 for d in divergences if d["severity"] != "🟢")
    try:
        n_total = len(_fetch_research_best_rows())
    except Exception:
        n_total = len(divergences)
    n_skipped = max(0, n_total - len(divergences))
    logger.info(
        "summary: %d strategies checked, %d breaches (gap > %.2f), %d no-trades-in-window",
        len(divergences), n_breaches, gap_threshold, n_skipped,
    )

    msg = format_telegram(divergences, gap_threshold)

    print(msg)
    n_alerting = sum(1 for d in divergences if d["severity"] != "🟢")
    print(
        f"\n[summary] {len(divergences)} (universe, strategy) combos checked, "
        f"{n_alerting} alerting"
    )

    # ── Sub-phase 1.4: update state, fire rollbacks ───────────────────────────
    rollback_alerts = process_rollbacks(
        divergences=divergences,
        state=state,
        gap_threshold=gap_threshold,
        today_str=today_str,
        no_rollback=no_rollback,
        dry_run_telegram=dry_run_telegram,
        no_telegram=no_telegram,
    )

    if rollback_alerts:
        print(f"\n[rollbacks] {len(rollback_alerts)} action(s) taken.")

    # Mark run date for idempotency (must be last before save)
    state["_last_run_date"] = today_str

    # Atomic save
    try:
        _save_state_atomic(state, state_file)
        logger.debug("Saved divergence state to %s", state_file)
    except Exception as exc:
        logger.error("Failed to save divergence state: %s", exc)

    # ── Telegram divergence digest (original behaviour) ───────────────────────
    if dry_run_telegram:
        print("\n[DRY RUN] Divergence digest Telegram skipped.")
        return 0

    if no_telegram:
        return 0

    has_alerts = any(d["severity"] != "🟢" for d in divergences)
    if has_alerts:
        try:
            from utils.telegram import notify

            notify(msg, category="research_divergence")
        except Exception as exc:
            logger.error("Telegram send failed: %s", exc)
            return 1

    # Heartbeat record (non-fatal)
    try:
        from db.atlas_db import record_heartbeat

        record_heartbeat("check_live_research_divergence", status="ok")
    except Exception as exc:
        logger.warning("Heartbeat record failed: %s", exc)

    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--window-days",
        type=int,
        default=DEFAULT_WINDOW_DAYS,
        help="Lookback window for live trades (default: 30)",
    )
    parser.add_argument(
        "--gap-threshold",
        type=float,
        default=DEFAULT_GAP_THRESHOLD,
        help="Alert if research_sharpe - live_sharpe > threshold (default: 0.5)",
    )
    parser.add_argument(
        "--dry-run-telegram",
        action="store_true",
        help="Print message instead of sending to Telegram",
    )
    parser.add_argument(
        "--no-telegram",
        action="store_true",
        help="Skip Telegram entirely (for CI / cron health checks)",
    )
    parser.add_argument(
        "--state-file",
        type=Path,
        default=DEFAULT_STATE_FILE,
        help="Path to divergence state JSON (default: data/divergence_state.json)",
    )
    parser.add_argument(
        "--no-rollback",
        action="store_true",
        help="Disable auto-rollback even on breach (useful for staging)",
    )
    parser.add_argument(
        "--force-rerun",
        action="store_true",
        help="Bypass the once-per-day idempotency gate (for manual verification)",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
    )

    # Ensure the named logger always writes to the dedicated log file,
    # even when the script is invoked directly (not via the cron redirect).
    # propagate=False prevents duplicate entries when cron also redirects stderr.
    if not logger.handlers:
        _log_file = ATLAS_ROOT / "logs" / "live_research_divergence.log"
        _log_file.parent.mkdir(exist_ok=True)
        _fh = logging.FileHandler(str(_log_file))
        _fh.setFormatter(
            logging.Formatter("%(asctime)s [%(name)s] %(levelname)s %(message)s")
        )
        logger.addHandler(_fh)
        logger.setLevel(logging.INFO)
        logger.propagate = False  # avoid double-write when cron redirects stderr

    return run_divergence_check(
        window_days=args.window_days,
        gap_threshold=args.gap_threshold,
        state_file=args.state_file,
        no_rollback=args.no_rollback,
        dry_run_telegram=args.dry_run_telegram,
        no_telegram=args.no_telegram,
        force_rerun=args.force_rerun,
    )


if __name__ == "__main__":
    sys.exit(main())
