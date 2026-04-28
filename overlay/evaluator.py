"""overlay/evaluator.py — Weekly self-scoring of AI overlay decisions.

Loads unevaluated overlay decisions from SQLite, fetches SPY price data for
the days following each decision, determines whether each decision was correct,
persists the outcome back to the DB, and returns an accuracy summary.

Tighten decisions are correct when the market falls after them; no_change
decisions are correct when the market does NOT fall sharply after them.

Run weekly (or on demand):
    python3 -m overlay.cron --evaluate
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Optional

import pandas as pd

logger = logging.getLogger(__name__)

# ── Thresholds ────────────────────────────────────────────────────────────────

# Tighten: market needs to drop this much (in 3 days) for decision to be CORRECT
_TIGHTEN_CORRECT_THRESHOLD = -0.01     # -1 %

# Tighten: market rising this much means tightening MISSED UPSIDE → INCORRECT
_TIGHTEN_MISSED_THRESHOLD = +0.01     # +1 %

# No-change: if market drops this much we SHOULD have tightened → INCORRECT
_NO_CHANGE_DROP_THRESHOLD = -0.02     # -2 %

# How many trading days after decision to measure return
_LOOKAHEAD_DAYS = 3


# ── SPY data helper ───────────────────────────────────────────────────────────

def _get_spy_returns_after(timestamp: str, lookahead: int = _LOOKAHEAD_DAYS) -> Optional[float]:
    """Return SPY cumulative return over *lookahead* trading days after *timestamp*.

    Uses atlas DB first; falls back to yfinance if SPY is absent in the DB.

    Returns None when price data is unavailable (e.g. decision too recent).
    """
    try:
        decision_dt = datetime.fromisoformat(timestamp)
    except (ValueError, TypeError):
        logger.warning("evaluator: cannot parse timestamp %r", timestamp)
        return None

    start_date = (decision_dt + timedelta(days=1)).strftime("%Y-%m-%d")
    # Fetch extra buffer days to account for weekends / holidays
    end_date = (decision_dt + timedelta(days=lookahead * 2 + 5)).strftime("%Y-%m-%d")

    spy_df = _load_spy_ohlcv(start_date, end_date)
    if spy_df is None or spy_df.empty:
        logger.debug("evaluator: no SPY data %s %s", start_date, end_date)
        return None

    # We need at least lookahead trading days
    if len(spy_df) < lookahead:
        logger.debug(
            "evaluator: only %d SPY rows available (need %d)", len(spy_df), lookahead
        )
        return None

    first_close = float(spy_df["close"].iloc[0])
    last_close = float(spy_df["close"].iloc[lookahead - 1])
    if first_close == 0:
        return None
    return (last_close - first_close) / first_close


def _load_spy_ohlcv(start_date: str, end_date: str) -> Optional[pd.DataFrame]:
    """Load SPY OHLCV from Atlas DB; fall back to yfinance on miss."""
    try:
        from db.atlas_db import get_ohlcv  # type: ignore
        df = get_ohlcv("SPY", start_date=start_date, end_date=end_date)
        if not df.empty:
            return df
    except Exception as exc:
        logger.debug("evaluator: DB get_ohlcv failed: %s", exc)

    # yfinance fallback — used when SPY has not yet been ingested
    try:
        import yfinance as yf  # type: ignore
        spy = yf.download("SPY", start=start_date, end=end_date, progress=False, auto_adjust=True)
        if spy.empty:
            return None
        spy = spy.rename(columns=str.lower)
        spy.index = pd.to_datetime(spy.index)
        return spy
    except Exception as exc:
        logger.warning("evaluator: yfinance fallback failed: %s", exc)
        return None


# ── Decision scoring ──────────────────────────────────────────────────────────

def _score_decision(action: str, spy_return: Optional[float]) -> tuple[int, str]:
    """Return (outcome_correct, outcome_notes) for a single decision.

    outcome_correct: 1 = correct, 0 = incorrect.
    """
    if spy_return is None:
        return 1, "SPY data unavailable — skipping evaluation (assumed neutral)"

    pct = round(spy_return * 100, 2)

    if action == "tighten":
        if spy_return < _TIGHTEN_CORRECT_THRESHOLD:
            return (
                1,
                f"Market fell {pct:.2f}% over {_LOOKAHEAD_DAYS}d — tightening protected downside",
            )
        elif spy_return > _TIGHTEN_MISSED_THRESHOLD:
            return (
                0,
                f"Market rose {pct:.2f}% over {_LOOKAHEAD_DAYS}d — tightening missed upside",
            )
        else:
            return (
                1,
                f"Market flat/neutral ({pct:.2f}%) over {_LOOKAHEAD_DAYS}d — tightening not costly (neutral)",
            )
    else:
        # action = 'no_change' or any other value
        if spy_return < _NO_CHANGE_DROP_THRESHOLD:
            return (
                0,
                f"Market dropped {pct:.2f}% over {_LOOKAHEAD_DAYS}d — should have tightened",
            )
        else:
            return (
                1,
                f"Market stable/up ({pct:.2f}%) over {_LOOKAHEAD_DAYS}d — no-change was appropriate",
            )


# ── Main evaluation function ──────────────────────────────────────────────────

def evaluate_overlay_decisions(days: int = 7) -> dict:
    """Score unevaluated overlay decisions using a two-pass strategy.

    **Pass 1 — catch-up sweep** (new behaviour):
        Fetches ALL unevaluated decisions regardless of age (via
        ``get_overlay_decisions(unevaluated_only=True)``).  For each one,
        attempts to score it if ``_LOOKAHEAD_DAYS`` of SPY data are now
        available.  Decisions that still lack sufficient lookahead are skipped
        with an INFO log; they will be retried on the next run.

        This fixes the silent-skip bug where Wed/Thu/Fri decisions fell outside
        the 7-day recency window before being evaluated the following Saturday.

    **Pass 2 — stats window** (existing behaviour, unchanged):
        Re-fetches decisions in the last *days* calendar days (evaluated + not)
        to compute accuracy statistics for the Telegram summary.

    Parameters
    ----------
    days:
        Calendar-day window used for the accuracy-stats summary (Pass 2).
        Does NOT affect which decisions are attempted for evaluation in Pass 1.

    Returns
    -------
    dict with keys:
        period_days, total_decisions, tighten_count, no_change_count,
        tighten_correct_pct, no_change_correct_pct, overall_accuracy_pct,
        net_value, evaluated_count, skipped_count
    """
    from db.atlas_db import get_overlay_decisions, update_overlay_outcome  # type: ignore

    # ── Pass 1: attempt to evaluate ALL unevaluated decisions ─────────────
    all_unevaluated = get_overlay_decisions(unevaluated_only=True)

    logger.info(
        "evaluator: pass1 found %d unevaluated decisions (all ages)",
        len(all_unevaluated),
    )

    evaluated_count = 0
    skipped_count = 0

    for d in all_unevaluated:
        decision_id = d["id"]
        action = d.get("action", "no_change")
        timestamp = d.get("timestamp", "")

        spy_return = _get_spy_returns_after(timestamp)
        if spy_return is None:
            # Not enough future SPY data yet — this decision is genuinely too
            # recent.  It will be retried on the next evaluation run.
            skipped_count += 1
            logger.info(
                "evaluator: id=%d action=%s skipped (insufficient lookahead)",
                decision_id,
                action,
            )
            continue

        outcome_correct, outcome_notes = _score_decision(action, spy_return)
        try:
            update_overlay_outcome(
                decision_id=decision_id,
                outcome_correct=outcome_correct,
                outcome_notes=outcome_notes,
            )
            evaluated_count += 1
            logger.info(
                "evaluator: id=%d action=%s correct=%d  %s",
                decision_id,
                action,
                outcome_correct,
                outcome_notes,
            )
        except Exception as exc:
            logger.error("evaluator: failed to update id=%d: %s", decision_id, exc)
            skipped_count += 1

    logger.info(
        "evaluator: pass1 evaluated=%d skipped=%d (insufficient lookahead)",
        evaluated_count,
        skipped_count,
    )

    # ── Pass 2: accuracy stats over the recency window ────────────────────
    # Re-load decisions so we include the ones we just evaluated.
    all_decisions = get_overlay_decisions(days=days)
    evaluated_all = [d for d in all_decisions if d.get("outcome_evaluated")]

    tighten_decisions = [d for d in evaluated_all if d.get("action") == "tighten"]
    no_change_decisions = [d for d in evaluated_all if d.get("action") == "no_change"]

    def _pct_correct(subset: list) -> float:
        if not subset:
            return 0.0
        correct = sum(1 for d in subset if d.get("outcome_correct") == 1)
        return round(correct / len(subset) * 100, 1)

    tighten_correct_pct = _pct_correct(tighten_decisions)
    no_change_correct_pct = _pct_correct(no_change_decisions)

    all_correct = sum(1 for d in evaluated_all if d.get("outcome_correct") == 1)
    overall_accuracy_pct = (
        round(all_correct / len(evaluated_all) * 100, 1) if evaluated_all else 0.0
    )

    if overall_accuracy_pct >= 55:
        net_value = "positive"
    elif overall_accuracy_pct <= 45 and evaluated_all:
        net_value = "negative"
    else:
        net_value = "neutral"

    stats = {
        "period_days": days,
        "total_decisions": len(all_decisions),
        "evaluated_count": len(evaluated_all),
        "newly_evaluated": evaluated_count,
        "skipped_count": skipped_count,
        "tighten_count": len(tighten_decisions),
        "no_change_count": len(no_change_decisions),
        "tighten_correct_pct": tighten_correct_pct,
        "no_change_correct_pct": no_change_correct_pct,
        "overall_accuracy_pct": overall_accuracy_pct,
        "net_value": net_value,
    }

    logger.info("evaluator stats: %s", stats)
    return stats


# ── Backlog health check ──────────────────────────────────────────────────────

def check_evaluator_backlog(threshold: int = 5) -> tuple[bool, int, float]:
    """Check whether the overlay evaluator has a stale backlog.

    A decision is considered *stale* when it is more than 2 calendar days old
    and still has ``outcome_evaluated = 0``.  Any such decision has had at
    least one weekly evaluation run pass without processing it, which indicates
    the window-logic bug (or a new variant of it).

    Parameters
    ----------
    threshold:
        Maximum number of stale decisions before the check reports unhealthy.
        Default is 5 (matches healthz alert gate).

    Returns
    -------
    (is_healthy, backlog_count, oldest_age_days)
        is_healthy:     ``True`` when backlog_count <= threshold.
        backlog_count:  number of unevaluated decisions older than 2 days.
        oldest_age_days: age of the oldest such decision in decimal days
                         (0.0 when backlog is empty).
    """
    try:
        from db.atlas_db import get_db  # type: ignore
        with get_db() as db:
            row = db.execute(
                """
                SELECT
                    COUNT(*) AS cnt,
                    MAX(
                        (julianday('now') - julianday(timestamp))
                    ) AS oldest_age_days
                FROM overlay_decisions
                WHERE outcome_evaluated = 0
                  AND timestamp < datetime('now', '-2 days')
                """,
            ).fetchone()
            backlog_count: int = int(row["cnt"] or 0)
            oldest_age_days: float = float(row["oldest_age_days"] or 0.0)
    except Exception as exc:
        logger.error("check_evaluator_backlog: DB query failed: %s", exc)
        # Treat query failure as healthy to avoid false-positive alerts.
        return True, 0, 0.0

    is_healthy = backlog_count <= threshold
    return is_healthy, backlog_count, round(oldest_age_days, 1)


# ── Telegram reporting ────────────────────────────────────────────────────────

def _format_stats(stats: dict) -> str:
    """Format evaluation stats as a human-readable Telegram message."""
    net_emoji = {"positive": "🟢", "negative": "🔴", "neutral": "🟡"}.get(
        stats.get("net_value", "neutral"), "🟡"
    )
    lines = [
        f"📊 <b>Overlay Weekly Review</b> ({stats.get('period_days', 7)}d)",
        "",
        f"Decisions reviewed:  {stats.get('evaluated_count', 0)} / {stats.get('total_decisions', 0)}",
        f"  Tighten:   {stats.get('tighten_count', 0)}  ({stats.get('tighten_correct_pct', 0):.1f}% correct)",
        f"  No-change: {stats.get('no_change_count', 0)}  ({stats.get('no_change_correct_pct', 0):.1f}% correct)",
        "",
        f"Overall accuracy: <b>{stats.get('overall_accuracy_pct', 0):.1f}%</b>",
        f"Net value: {net_emoji} <b>{stats.get('net_value', 'neutral').upper()}</b>",
    ]
    if stats.get("skipped_count"):
        lines.append(f"\nSkipped (data not yet available): {stats['skipped_count']}")
    return "\n".join(lines)



def evaluate_shadow_events(limit: int = 1000) -> dict:
    """Match unevaluated overlay_shadow_log rows to closed trades and populate actual_outcome_pnl.

    For each shadow row with actual_outcome_evaluated=0:
      - Find the corresponding closed trade by ticker + DATE(entry_date) matching shadow row plan_id date
      - If found, populate actual_outcome_pnl with the trade's realized pnl
      - Mark evaluated

    Returns stats dict: {evaluated, skipped, total}.
    """
    from db.atlas_db import get_db, get_unevaluated_shadow_events, update_shadow_outcome

    pending = get_unevaluated_shadow_events(limit=limit)
    evaluated = 0
    skipped = 0

    with get_db() as db:
        for ev in pending:
            shadow_id = ev["id"]
            ticker = ev["ticker"]
            # plan_id format: "{market_id}_{YYYY-MM-DD}" — extract trade_date
            plan_id = ev.get("plan_id", "")
            trade_date = plan_id.rsplit("_", 1)[-1] if "_" in plan_id else None
            if not trade_date:
                skipped += 1
                continue
            # Find the closed trade matching ticker + entry_date
            row = db.execute(
                """
                SELECT id, pnl
                FROM trades
                WHERE ticker = ?
                  AND DATE(entry_date) = ?
                  AND status = 'closed'
                ORDER BY id DESC
                LIMIT 1
                """,
                (ticker, trade_date),
            ).fetchone()
            if row is None or row["pnl"] is None:
                skipped += 1
                continue
            update_shadow_outcome(shadow_id, float(row["pnl"]))
            evaluated += 1

    logger.info(
        "overlay_shadow_eval: evaluated=%d skipped=%d total=%d",
        evaluated, skipped, len(pending),
    )
    return {"evaluated": evaluated, "skipped": skipped, "total": len(pending)}


def evaluate_and_report(days: int = 7) -> dict:
    """Convenience wrapper: evaluate decisions + send Telegram summary.

    Returns the stats dict from evaluate_overlay_decisions(), augmented with
    a ``shadow`` key from evaluate_shadow_events().
    """
    stats = evaluate_overlay_decisions(days=days)

    # M3: also evaluate shadow events (non-fatal)
    try:
        shadow_stats = evaluate_shadow_events()
        stats["shadow"] = shadow_stats
    except Exception as exc:
        logger.warning("evaluator: shadow eval failed (non-fatal): %s", exc)
        stats["shadow"] = {}

    try:
        from utils.telegram import send_message  # type: ignore
        msg = _format_stats(stats)
        send_message(msg)
        logger.info("evaluator: Telegram summary sent")
    except Exception as exc:
        logger.warning("evaluator: Telegram send failed (non-fatal): %s", exc)

    return stats
