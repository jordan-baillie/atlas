#!/usr/bin/env python3
"""Regime performance report — postclose cron script.

Joins trades × regime_history to compute per-(strategy, universe, regime_state)
performance metrics, then writes a Markdown report.

Usage:
  python3 scripts/regime_performance_report.py
  python3 scripts/regime_performance_report.py --days 90 --output-dir reports/
"""

from __future__ import annotations

import argparse
import logging
import math
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

# ── Path bootstrap ─────────────────────────────────────────────────────────────
_HERE = Path(__file__).resolve()
_ATLAS_ROOT = _HERE.parent.parent
sys.path.insert(0, str(_ATLAS_ROOT))

logger = logging.getLogger("regime_performance_report")


# ── DB path resolution ─────────────────────────────────────────────────────────

def _resolve_db_path() -> Path:
    import os
    env_override = os.environ.get("ATLAS_DB_PATH")
    if env_override:
        return Path(env_override)
    try:
        from db import atlas_db
        override = getattr(atlas_db, "_db_path_override", None)
        if override:
            return Path(override)
    except Exception:
        pass
    return _ATLAS_ROOT / "data" / "atlas.db"


# ── Sharpe calculation ─────────────────────────────────────────────────────────

def _sharpe(pnl_list: list[float]) -> float | None:
    """Annualised Sharpe from a list of per-trade PnL values. Returns None if <10 trades."""
    n = len(pnl_list)
    if n < 10:
        return None
    mean = sum(pnl_list) / n
    variance = sum((x - mean) ** 2 for x in pnl_list) / n
    std = math.sqrt(variance)
    if std == 0:
        return None
    # Assume ~252 trading days, trades annualise by sqrt(252/n) approximately
    return (mean / std) * math.sqrt(252)


# ── Core report logic ──────────────────────────────────────────────────────────

def build_report(db_path: Path, days: int) -> str:
    """Query DB and return Markdown report string."""
    conn = sqlite3.connect(str(db_path), timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row

    try:
        today = datetime.now(tz=timezone.utc)
        date_str = today.strftime("%Y-%m-%d")

        # ── Total closed trades in window ──────────────────────────────────────
        total_closed = conn.execute("""
            SELECT COUNT(*) FROM trades
            WHERE status = 'closed'
              AND exit_date >= date('now', ?)
        """, (f"-{days} days",)).fetchone()[0]

        if total_closed == 0:
            return f"# Regime Performance Report — {date_str}\n\nNo closed trades in last {days} days.\n"

        # ── Trades with regime_state ───────────────────────────────────────────
        # Join via regime_history on entry_date; fall back to regime_at_entry column
        tagged_rows = conn.execute("""
            SELECT
                t.id,
                t.strategy,
                t.universe,
                COALESCE(rh.regime_state, t.regime_at_entry) AS regime_state,
                t.pnl,
                t.pnl_pct,
                CASE WHEN t.pnl > 0 THEN 1 ELSE 0 END AS is_win
            FROM trades t
            LEFT JOIN regime_history rh
                ON rh.date = DATE(t.entry_date)
            WHERE t.status = 'closed'
              AND t.exit_date >= date('now', ?)
        """, (f"-{days} days",)).fetchall()

        tagged_count = sum(1 for r in tagged_rows if r["regime_state"] is not None)
        coverage_pct = (tagged_count / total_closed * 100) if total_closed else 0.0

        # ── Group by (strategy, universe, regime_state) ────────────────────────
        # Regime states with <5 total trades across ALL (strategy, universe) are skipped
        regime_totals: dict[str, int] = defaultdict(int)
        for row in tagged_rows:
            rs = row["regime_state"] or "untagged"
            regime_totals[rs] += 1

        # Keep only regimes with >= 5 trades total
        valid_regimes = {rs for rs, cnt in regime_totals.items() if cnt >= 5}

        groups: dict[tuple[str, str, str], list[dict]] = defaultdict(list)
        for row in tagged_rows:
            rs = row["regime_state"] or "untagged"
            if rs not in valid_regimes:
                continue
            key = (row["strategy"] or "unknown", row["universe"] or "unknown", rs)
            groups[key].append({
                "pnl": row["pnl"] or 0.0,
                "pnl_pct": row["pnl_pct"] or 0.0,
                "is_win": row["is_win"],
            })

        # ── Build Markdown ─────────────────────────────────────────────────────
        lines: list[str] = [
            f"# Regime Performance Report — {date_str}",
            f"Coverage: {coverage_pct:.0f}% of last-{days}-day trades tagged with regime "
            f"({tagged_count}/{total_closed})\n",
            "## By Strategy × Regime",
            "",
            "| Strategy | Universe | Regime | Trades | WinRate | AvgR | TotalPnL | Sharpe |",
            "|----------|----------|--------|--------|---------|------|----------|--------|",
        ]

        # Sort: strategy, universe, regime_state
        for key in sorted(groups.keys()):
            strategy, universe, regime = key
            trades_data = groups[key]
            n = len(trades_data)
            win_rate = sum(d["is_win"] for d in trades_data) / n * 100
            avg_r = sum(d["pnl_pct"] for d in trades_data) / n
            total_pnl = sum(d["pnl"] for d in trades_data)
            sharpe_val = _sharpe([d["pnl_pct"] for d in trades_data])
            sharpe_str = f"{sharpe_val:.2f}" if sharpe_val is not None else "n/a (<10)"
            lines.append(
                f"| {strategy} | {universe} | {regime} | {n} "
                f"| {win_rate:.0f}% | {avg_r:+.2f}% | ${total_pnl:+.2f} | {sharpe_str} |"
            )

        lines.append("")

        # ── Regime summary section ─────────────────────────────────────────────
        lines.append("## Regime Coverage Summary")
        lines.append("")
        lines.append("| Regime State | Total Trades | Included in Report |")
        lines.append("|-------------|--------------|-------------------|")
        for rs, cnt in sorted(regime_totals.items()):
            included = "yes" if rs in valid_regimes else f"no (<5 trades)"
            lines.append(f"| {rs} | {cnt} | {included} |")

        lines.append("")
        lines.append(f"*Generated {today.isoformat()} UTC | window={days}d*")

        return "\n".join(lines) + "\n"

    finally:
        conn.close()


# ── CLI ────────────────────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--days", type=int, default=90,
        help="Lookback window in days (default: 90).",
    )
    parser.add_argument(
        "--output-dir", type=Path, default=Path("reports"),
        help="Directory to write Markdown report (default: reports/).",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        stream=sys.stdout,
    )

    db_path = _resolve_db_path()
    logger.info("DB: %s | window: %d days", db_path, args.days)

    report = build_report(db_path, args.days)

    output_dir = _ATLAS_ROOT / args.output_dir if not args.output_dir.is_absolute() else args.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)

    date_str = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
    out_path = output_dir / f"regime_performance_{date_str}.md"
    out_path.write_text(report, encoding="utf-8")

    logger.info("Report written: %s", out_path)
    print(report)


if __name__ == "__main__":
    main()
