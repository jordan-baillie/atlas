"""Paper-trading progress reporter.

Computes per-strategy promotion gate metrics for all strategies currently
in PAPER lifecycle state.  Designed to return safe, zero-crash output even
when paper_trades is completely empty (status='insufficient_data').

Public API
----------
compute_paper_progress() -> list[dict]
    Returns one dict per (strategy, universe) PAPER combo.

Keys returned per dict
----------------------
strategy, universe, paper_start_date, days_in_paper, trade_count,
win_rate, profit_factor, sharpe, research_sharpe, sharpe_delta,
gates: {days_pass, trades_pass, sharpe_pass, delta_pass, all_pass},
status: 'ready' | 'progressing' | 'failing' | 'insufficient_data'
"""
from __future__ import annotations

import logging
import math
import statistics
from datetime import date, datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

# ── Promotion thresholds (documented bar — NOT the stricter auto-promote gates)
DAYS_THRESHOLD = 30
TRADES_THRESHOLD = 10
SHARPE_THRESHOLD = 0.3
DELTA_THRESHOLD = 0.5


def _days_since(date_str: str | None) -> int:
    """Return calendar days elapsed since an ISO-8601 date/datetime string."""
    if not date_str:
        return 0
    try:
        # Handle both 'YYYY-MM-DD' and 'YYYY-MM-DDTHH:MM:SS' formats
        dt_str = date_str.split("T")[0]
        start = date.fromisoformat(dt_str)
        return (date.today() - start).days
    except (ValueError, TypeError):
        logger.warning("Cannot parse date string: %r", date_str)
        return 0


def _safe_sharpe(pnl_pcts: list[float]) -> float | None:
    """Per-trade Sharpe: mean(pnl_pct) / stdev(pnl_pct).

    Returns None if fewer than 2 observations (stdev undefined).
    Returns None if stdev is zero (degenerate series).
    """
    if len(pnl_pcts) < 2:
        return None
    try:
        mu = statistics.mean(pnl_pcts)
        sd = statistics.stdev(pnl_pcts)
        if sd == 0.0:
            return None
        return mu / sd
    except statistics.StatisticsError:
        return None


def _profit_factor(pnl_list: list[float]) -> float | None:
    """Gross profit / |gross loss|.  Clamped to 999.99 when no losses exist.
    Returns None if the list is empty.
    """
    if not pnl_list:
        return None
    gross_profit = sum(p for p in pnl_list if p > 0)
    gross_loss = abs(sum(p for p in pnl_list if p < 0))
    if gross_loss == 0:
        return 999.99 if gross_profit > 0 else 1.0
    return min(round(gross_profit / gross_loss, 4), 999.99)


def _determine_status(
    days_pass: bool,
    trades_pass: bool,
    sharpe_pass: bool,
    delta_pass: bool,
    all_pass: bool,
    trade_count: int,
    sharpe: float | None,
) -> str:
    if all_pass:
        return "ready"
    if days_pass and trades_pass and not sharpe_pass:
        # Enough data has accumulated — Sharpe consistently below threshold
        return "failing"
    if days_pass or trades_pass:
        return "progressing"
    return "insufficient_data"


def compute_paper_progress() -> list[dict[str, Any]]:
    """Return progress metrics for all PAPER-state (strategy, universe) combos.

    Gracefully handles empty paper_trades — all metric fields will be None / 0
    and status will be 'insufficient_data'.
    """
    from db.atlas_db import get_db

    results: list[dict[str, Any]] = []

    with get_db() as db:
        # 1. Fetch all PAPER lifecycle rows
        lifecycle_rows = db.execute(
            """
            SELECT strategy, universe, entered_state_at, paper_start_date
            FROM strategy_lifecycle
            WHERE state = 'PAPER'
            ORDER BY strategy, universe
            """
        ).fetchall()

        if not lifecycle_rows:
            return []

        for row in lifecycle_rows:
            strategy = row["strategy"]
            universe = row["universe"]
            # Use paper_start_date when available, fall back to entered_state_at
            start_date_str = row["paper_start_date"] or row["entered_state_at"]
            days_in_paper = _days_since(start_date_str)

            # 2. Fetch closed, non-superseded paper trades
            trades_rows = db.execute(
                """
                SELECT pnl, pnl_pct
                FROM paper_trades
                WHERE strategy = ?
                  AND universe = ?
                  AND status = 'closed'
                  AND superseded = 0
                """,
                (strategy, universe),
            ).fetchall()

            trade_count = len(trades_rows)
            pnl_list = [r["pnl"] for r in trades_rows if r["pnl"] is not None]
            pnl_pcts = [r["pnl_pct"] for r in trades_rows if r["pnl_pct"] is not None]

            # 3. Derived metrics
            win_rate: float | None = None
            profit_factor_val: float | None = None
            sharpe: float | None = None

            if trade_count > 0:
                wins = sum(1 for p in pnl_list if p > 0)
                win_rate = round(wins / trade_count, 4) if trade_count > 0 else None
                profit_factor_val = _profit_factor(pnl_list) if pnl_list else None

            if len(pnl_pcts) >= 2:
                sharpe = _safe_sharpe(pnl_pcts)
                if sharpe is not None:
                    sharpe = round(sharpe, 4)

            # 4. Research Sharpe — cross-regime row (regime_state IS NULL)
            rb_row = db.execute(
                """
                SELECT sharpe
                FROM research_best
                WHERE strategy = ?
                  AND universe = ?
                  AND regime_state IS NULL
                ORDER BY updated_at DESC
                LIMIT 1
                """,
                (strategy, universe),
            ).fetchone()

            research_sharpe: float | None = None
            if rb_row and rb_row["sharpe"] is not None:
                try:
                    research_sharpe = float(rb_row["sharpe"])
                except (TypeError, ValueError):
                    research_sharpe = None

            # 5. Sharpe delta
            sharpe_delta: float | None = None
            if sharpe is not None and research_sharpe is not None:
                sharpe_delta = round(sharpe - research_sharpe, 4)

            # 6. Gates
            days_pass = days_in_paper >= DAYS_THRESHOLD
            trades_pass = trade_count >= TRADES_THRESHOLD
            sharpe_pass = sharpe is not None and sharpe >= SHARPE_THRESHOLD
            delta_pass = (
                sharpe is not None
                and research_sharpe is not None
                and abs(sharpe - research_sharpe) < DELTA_THRESHOLD
            )
            all_pass = days_pass and trades_pass and sharpe_pass and delta_pass

            # 7. Status
            status = _determine_status(
                days_pass, trades_pass, sharpe_pass, delta_pass, all_pass,
                trade_count, sharpe,
            )

            results.append(
                {
                    "strategy": strategy,
                    "universe": universe,
                    "paper_start_date": start_date_str,
                    "days_in_paper": days_in_paper,
                    "trade_count": trade_count,
                    "win_rate": win_rate,
                    "profit_factor": profit_factor_val,
                    "sharpe": sharpe,
                    "research_sharpe": research_sharpe,
                    "sharpe_delta": sharpe_delta,
                    "gates": {
                        "days_pass": days_pass,
                        "trades_pass": trades_pass,
                        "sharpe_pass": sharpe_pass,
                        "delta_pass": delta_pass,
                        "all_pass": all_pass,
                    },
                    "status": status,
                }
            )

    return results
