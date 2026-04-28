#!/usr/bin/env python3
"""Report on overlay shadow events: what would have happened, what actually did.

Usage:
  python3 -m scripts.overlay_shadow_report --days 30
  python3 -m scripts.overlay_shadow_report --ticker AAPL
  python3 -m scripts.overlay_shadow_report --strategy mean_reversion
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Dict, List, Optional

# Ensure project root on path when run as a script
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


def build_report(
    days: int = 30,
    strategy: Optional[str] = None,
    ticker: Optional[str] = None,
) -> dict:
    """Build a shadow evaluation report dict.

    Parameters
    ----------
    days:     Look-back window in calendar days.
    strategy: Optional strategy filter (joins against trades table).
    ticker:   Optional ticker filter.

    Returns
    -------
    dict with keys: days, strategy_filter, ticker_filter, n_events,
        total_would_be_dollar_diff, evaluated_count, tighten_count,
        tighten_winrate, sim_pnl_with_overlay, sim_pnl_original,
        sim_pnl_delta, verdict, events_by_day.
    """
    from db.atlas_db import get_shadow_events, get_db  # type: ignore

    rows = get_shadow_events(days=days, ticker=ticker)

    # Optional strategy filter via JOIN with trades
    if strategy:
        with get_db() as db:
            keep_ids: set[int] = set()
            for r in rows:
                plan_id = r.get("plan_id", "")
                trade_date = plan_id.rsplit("_", 1)[-1] if "_" in plan_id else None
                if not trade_date:
                    continue
                row = db.execute(
                    "SELECT 1 FROM trades WHERE ticker=? AND DATE(entry_date)=?"
                    " AND strategy=? LIMIT 1",
                    (r["ticker"], trade_date, strategy),
                ).fetchone()
                if row:
                    keep_ids.add(r["id"])
            rows = [r for r in rows if r["id"] in keep_ids]

    if not rows:
        return {
            "days": days,
            "strategy_filter": strategy,
            "ticker_filter": ticker,
            "n_events": 0,
            "total_would_be_dollar_diff": 0.0,
            "evaluated_count": 0,
            "tighten_count": 0,
            "tighten_winrate": None,
            "sim_pnl_with_overlay": 0.0,
            "sim_pnl_original": 0.0,
            "sim_pnl_delta": 0.0,
            "verdict": "insufficient data",
            "events_by_day": {},
        }

    # Aggregate
    n_events = len(rows)
    total_diff = sum(float(r.get("would_be_dollar_diff") or 0.0) for r in rows)
    evaluated = [r for r in rows if r.get("actual_outcome_evaluated")]
    n_eval = len(evaluated)

    # Per-day breakdown
    by_day: Dict[str, dict] = {}
    for r in rows:
        day = (r.get("created_at") or "")[:10]
        by_day.setdefault(day, {"n": 0, "would_diff_$": 0.0})
        by_day[day]["n"] += 1
        by_day[day]["would_diff_$"] += float(r.get("would_be_dollar_diff") or 0.0)

    # Tighten win rate: of evaluated rows where multiplier < 1.0 (would have reduced),
    # how many had ORIGINAL trade pnl < 0 (i.e. tightening would have helped)?
    tighten_rows = [r for r in evaluated if (r.get("sizing_multiplier") or 1.0) < 1.0]
    tighten_helpful = sum(
        1 for r in tighten_rows
        if (r.get("actual_outcome_pnl") or 0.0) < 0.0
    )
    tighten_winrate = (tighten_helpful / len(tighten_rows)) if tighten_rows else None

    # Simulated impact: for evaluated rows with multiplier M, overlay would have held
    # position_size * M instead of position_size. Realized pnl scales with M.
    sim_pnl_overlay = sum(
        (r.get("actual_outcome_pnl") or 0.0) * (r.get("sizing_multiplier") or 1.0)
        for r in evaluated
    )
    sim_pnl_original = sum(r.get("actual_outcome_pnl") or 0.0 for r in evaluated)
    sim_delta = sim_pnl_overlay - sim_pnl_original

    # Verdict
    if n_eval < 10:
        verdict = "insufficient data"
    elif sim_delta > 0:
        verdict = "overlay is helping"
    elif sim_delta < 0:
        verdict = "overlay is hurting"
    else:
        verdict = "overlay is neutral"

    return {
        "days": days,
        "strategy_filter": strategy,
        "ticker_filter": ticker,
        "n_events": n_events,
        "total_would_be_dollar_diff": round(total_diff, 2),
        "evaluated_count": n_eval,
        "tighten_count": len(tighten_rows),
        "tighten_winrate": round(tighten_winrate, 4) if tighten_winrate is not None else None,
        "sim_pnl_with_overlay": round(sim_pnl_overlay, 2),
        "sim_pnl_original": round(sim_pnl_original, 2),
        "sim_pnl_delta": round(sim_delta, 2),
        "verdict": verdict,
        "events_by_day": {
            k: {"n": v["n"], "would_diff_$": round(v["would_diff_$"], 2)}
            for k, v in sorted(by_day.items())
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Report on overlay shadow events.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--days", type=int, default=30, help="Look-back window in days")
    parser.add_argument("--strategy", type=str, default=None, help="Filter by strategy")
    parser.add_argument("--ticker", type=str, default=None, help="Filter by ticker")
    args = parser.parse_args(argv)

    report = build_report(days=args.days, strategy=args.strategy, ticker=args.ticker)
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
