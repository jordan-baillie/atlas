"""Atlas Research Brain — Execution Intelligence

Reads live execution telemetry (logs/live_executions.jsonl, logs/portfolio_snapshots.jsonl)
and trade history from SQLite (primary, Issue 4 migration) with journal/trade_ledger.json
as a read-only fallback only.  Writes structured analysis to the research brain.

Designed to be called periodically (daily/weekly) by the director or cron,
NOT during live order execution.

Output files:
    research/brain/execution/slippage.md       — slippage analysis by strategy/ticker
    research/brain/execution/fill_quality.md   — MOO fill quality vs backtest assumptions
    research/brain/execution/portfolio_track.md — live equity curve vs backtest projection
    research/brain/execution/stop_analysis.md  — protective stop effectiveness
    research/brain/execution/weekly_review.md  — weekly execution quality summary
"""

import json
import logging
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Optional

logger = logging.getLogger("atlas.brain.execution")

ATLAS_ROOT = Path(__file__).resolve().parent.parent.parent
EXECUTION_LOG = ATLAS_ROOT / "logs" / "live_executions.jsonl"
# LEGACY — used only as fallback if SQLite is unavailable (Issue 4 migration)
TRADE_LEDGER = ATLAS_ROOT / "journal" / "trade_ledger.json"
PORTFOLIO_SNAPSHOTS = ATLAS_ROOT / "logs" / "portfolio_snapshots.jsonl"
BRAIN_DIR = ATLAS_ROOT / "research" / "brain" / "execution"


def _read_jsonl(path: Path) -> list:
    """Read JSONL file, skip malformed lines."""
    entries = []
    if not path.exists():
        return entries
    for line in path.read_text().strip().split("\n"):
        if not line.strip():
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return entries


def _read_json(path: Path) -> list:
    """Read JSON array file."""
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, Exception):
        return []


def _write_brain_md(filename: str, content: str):
    """Write a markdown file to the brain execution directory."""
    BRAIN_DIR.mkdir(parents=True, exist_ok=True)
    path = BRAIN_DIR / filename
    path.write_text(content)
    logger.info("Brain execution doc updated: %s", path)


def analyze_slippage(days: int = 30) -> dict:
    """Analyze slippage from live execution log.

    Returns dict with summary stats and writes to brain/execution/slippage.md
    """
    entries = _read_jsonl(EXECUTION_LOG)
    cutoff = (datetime.now() - timedelta(days=days)).isoformat()

    fills = [e for e in entries
             if e.get("event") in ("live_entry", "live_exit")
             and e.get("timestamp", "") >= cutoff
             and e.get("fill_price", 0) > 0
             and e.get("planned_price", 0) > 0]

    if not fills:
        _write_brain_md("slippage.md", f"# Slippage Analysis\n\n_No fills in last {days} days._\n\nUpdated: {datetime.now().isoformat()}\n")
        return {"total_fills": 0}

    # Calculate slippage stats
    buy_slips = []
    sell_slips = []
    by_strategy = {}

    for f in fills:
        planned = f["planned_price"]
        actual = f["fill_price"]
        slip_bps = (actual - planned) / planned * 10000
        side = f.get("side", "BUY")
        strategy = f.get("strategy", "unknown")

        entry = {"ticker": f.get("ticker"), "slip_bps": round(slip_bps, 1),
                 "planned": planned, "actual": actual, "side": side}

        if side == "BUY":
            buy_slips.append(slip_bps)
        else:
            sell_slips.append(slip_bps)

        if strategy not in by_strategy:
            by_strategy[strategy] = []
        by_strategy[strategy].append(slip_bps)

    def _stats(values):
        if not values:
            return {"count": 0}
        return {
            "count": len(values),
            "mean_bps": round(sum(values) / len(values), 1),
            "max_bps": round(max(values), 1),
            "min_bps": round(min(values), 1),
            "total_cost_bps": round(sum(values), 1),
        }

    result = {
        "total_fills": len(fills),
        "period_days": days,
        "buy_slippage": _stats(buy_slips),
        "sell_slippage": _stats(sell_slips),
        "all_slippage": _stats(buy_slips + sell_slips),
        "by_strategy": {s: _stats(v) for s, v in by_strategy.items()},
        "config_assumption_bps": 5.0,  # 0.0005 = 5 bps from config
    }

    # Write brain markdown
    lines = [
        "# Slippage Analysis",
        "",
        f"Period: last {days} days | Total fills: {len(fills)}",
        f"Updated: {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        "",
        "## Summary",
        "",
        f"| Metric | Buy | Sell | All |",
        f"|--------|-----|------|-----|",
        f"| Count | {result['buy_slippage'].get('count',0)} | {result['sell_slippage'].get('count',0)} | {result['all_slippage'].get('count',0)} |",
        f"| Mean (bps) | {result['buy_slippage'].get('mean_bps','—')} | {result['sell_slippage'].get('mean_bps','—')} | {result['all_slippage'].get('mean_bps','—')} |",
        f"| Max (bps) | {result['buy_slippage'].get('max_bps','—')} | {result['sell_slippage'].get('max_bps','—')} | {result['all_slippage'].get('max_bps','—')} |",
        "",
        f"Config assumes: {result['config_assumption_bps']} bps slippage",
        "",
        "## By Strategy",
        "",
    ]
    for strat, stats in result["by_strategy"].items():
        lines.append(f"- **{strat}**: {stats.get('count',0)} fills, mean {stats.get('mean_bps','—')} bps")

    lines.extend(["", "## Implications", "",
        "If actual slippage consistently exceeds 5 bps, increase `fees.slippage_pct` in config.",
        "If slippage is lower, backtest results are conservative (good).",
    ])

    _write_brain_md("slippage.md", "\n".join(lines) + "\n")
    return result


def analyze_fill_quality(days: int = 30) -> dict:
    """Analyze market-on-open fill quality.

    Compares fill prices to the actual market open price for that day.
    Writes to brain/execution/fill_quality.md
    """
    entries = _read_jsonl(EXECUTION_LOG)
    cutoff = (datetime.now() - timedelta(days=days)).isoformat()

    fills = [e for e in entries
             if e.get("event") in ("live_entry", "live_exit")
             and e.get("timestamp", "") >= cutoff
             and e.get("fill_price", 0) > 0]

    if not fills:
        _write_brain_md("fill_quality.md", f"# Fill Quality Analysis\n\n_No fills in last {days} days._\n\nUpdated: {datetime.now().isoformat()}\n")
        return {"total_fills": 0}

    # Analyze timing: submit_time to fill_time
    timed_fills = []
    for f in fills:
        submit = f.get("submit_time", "")
        fill = f.get("fill_time", "")
        if submit and fill and fill != "None" and fill != "":
            try:
                st = datetime.fromisoformat(submit.replace("Z", "+00:00"))
                ft = datetime.fromisoformat(fill.replace("Z", "+00:00"))
                delta_s = (ft - st).total_seconds()
                timed_fills.append({
                    "ticker": f.get("ticker"),
                    "delta_s": delta_s,
                    "side": f.get("side"),
                })
            except (ValueError, TypeError):
                pass

    result = {
        "total_fills": len(fills),
        "timed_fills": len(timed_fills),
        "avg_fill_time_s": round(sum(t["delta_s"] for t in timed_fills) / len(timed_fills), 1) if timed_fills else None,
        "max_fill_time_s": round(max(t["delta_s"] for t in timed_fills), 1) if timed_fills else None,
    }

    # Spread analysis
    spreads = [f.get("spread", {}) for f in fills if f.get("spread")]
    if spreads:
        spread_bps = [s.get("spread_bps", 0) for s in spreads if s.get("spread_bps")]
        if spread_bps:
            result["avg_spread_bps"] = round(sum(spread_bps) / len(spread_bps), 1)
            result["max_spread_bps"] = round(max(spread_bps), 1)

    lines = [
        "# Fill Quality Analysis",
        "",
        f"Period: last {days} days | Total fills: {len(fills)}",
        f"Updated: {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        "",
        "## Fill Timing",
        "",
        f"- Fills with timing data: {len(timed_fills)}",
        f"- Average time to fill: {result.get('avg_fill_time_s', '—')}s",
        f"- Max time to fill: {result.get('max_fill_time_s', '—')}s",
        "",
        "## Bid-Ask Spread at Order Time",
        "",
        f"- Average spread: {result.get('avg_spread_bps', '—')} bps",
        f"- Max spread: {result.get('max_spread_bps', '—')} bps",
        "",
        "## Backtest Assumption Check",
        "",
        "The backtest assumes instant fill at market open price.",
        "If avg fill time > 30s or spread > 10 bps, consider adjusting assumptions.",
    ]

    _write_brain_md("fill_quality.md", "\n".join(lines) + "\n")
    return result


def analyze_stops(days: int = 30) -> dict:
    """Analyze protective stop effectiveness.

    Writes to brain/execution/stop_analysis.md
    """
    entries = _read_jsonl(EXECUTION_LOG)
    cutoff = (datetime.now() - timedelta(days=days)).isoformat()

    stops = [e for e in entries
             if e.get("timestamp", "") >= cutoff
             and e.get("stop_expected_price", 0) > 0
             and e.get("stop_fill_price", 0) > 0]

    if not stops:
        _write_brain_md("stop_analysis.md", f"# Stop Analysis\n\n_No stop fills in last {days} days._\n\nUpdated: {datetime.now().isoformat()}\n")
        return {"total_stops": 0}

    slippages = []
    for s in stops:
        expected = s["stop_expected_price"]
        actual = s["stop_fill_price"]
        slip_bps = (expected - actual) / expected * 10000  # positive = worse fill
        slippages.append({
            "ticker": s.get("ticker"),
            "expected": expected,
            "actual": actual,
            "slip_bps": round(slip_bps, 1),
        })

    slip_values = [s["slip_bps"] for s in slippages]
    result = {
        "total_stops": len(stops),
        "avg_stop_slippage_bps": round(sum(slip_values) / len(slip_values), 1),
        "max_stop_slippage_bps": round(max(slip_values), 1),
        "worst_fills": sorted(slippages, key=lambda x: x["slip_bps"], reverse=True)[:5],
    }

    lines = [
        "# Protective Stop Analysis",
        "",
        f"Period: last {days} days | Stop fills: {len(stops)}",
        f"Updated: {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        "",
        f"- Average stop slippage: {result['avg_stop_slippage_bps']} bps",
        f"- Max stop slippage: {result['max_stop_slippage_bps']} bps",
        "",
        "## Worst Fills",
        "",
    ]
    for w in result["worst_fills"]:
        lines.append(f"- {w['ticker']}: expected ${w['expected']:.2f}, filled ${w['actual']:.2f} ({w['slip_bps']:+.1f} bps)")

    lines.extend(["", "## Implications", "",
        "Stop slippage > 20 bps suggests using stop-limit orders instead of stop-market.",
        "Large gaps through stop levels indicate overnight risk not captured by ATR stops.",
    ])

    _write_brain_md("stop_analysis.md", "\n".join(lines) + "\n")
    return result


def analyze_portfolio_track(days: int = 30) -> dict:
    """Compare live equity curve to backtest projection.

    Writes to brain/execution/portfolio_track.md
    """
    snapshots = _read_jsonl(PORTFOLIO_SNAPSHOTS)
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")

    recent = [s for s in snapshots if s.get("date", "") >= cutoff]

    if not recent:
        _write_brain_md("portfolio_track.md", f"# Portfolio Tracking\n\n_No snapshots in last {days} days._\n\nUpdated: {datetime.now().isoformat()}\n")
        return {"snapshots": 0}

    equities = [(s["date"], s.get("equity", 0)) for s in recent]
    start_eq = equities[0][1] if equities else 0
    end_eq = equities[-1][1] if equities else 0
    live_return = (end_eq - start_eq) / start_eq * 100 if start_eq > 0 else 0

    result = {
        "snapshots": len(recent),
        "start_equity": start_eq,
        "end_equity": end_eq,
        "live_return_pct": round(live_return, 2),
        "period_days": days,
    }

    lines = [
        "# Portfolio Tracking — Live vs Backtest",
        "",
        f"Period: last {days} days | Snapshots: {len(recent)}",
        f"Updated: {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        "",
        f"## Live Performance",
        "",
        f"- Start equity: ${start_eq:,.2f}",
        f"- Current equity: ${end_eq:,.2f}",
        f"- Live return: {live_return:+.2f}%",
        "",
        "## Daily Snapshots",
        "",
        "| Date | Equity | Positions | Daily PnL |",
        "|------|--------|-----------|-----------|",
    ]
    for s in recent[-14:]:  # last 14 days
        lines.append(f"| {s['date']} | ${s.get('equity',0):,.2f} | {s.get('num_positions',0)} | ${s.get('daily_pnl',0):,.2f} |")

    lines.extend(["", "## Backtest Comparison", "",
        "Backtest CAGR: 15.7% (config metadata)",
        f"Live return (annualized): {live_return * 365 / max(days, 1):+.1f}%",
        "",
        "Divergence > 5% annualized suggests model assumptions need updating.",
    ])

    _write_brain_md("portfolio_track.md", "\n".join(lines) + "\n")
    return result


def weekly_review() -> dict:
    """Generate weekly execution quality review.

    Calls all analysis functions and writes a combined summary.
    Writes to brain/execution/weekly_review.md
    """
    slip = analyze_slippage(days=7)
    fills = analyze_fill_quality(days=7)
    stops = analyze_stops(days=7)
    port = analyze_portfolio_track(days=7)

    # Read recent trade activity from SQLite (source of truth — Issue 4 migration).
    # Falls back to JSON ledger if SQLite is unreachable so we don't break during cutover.
    cutoff = (datetime.now() - timedelta(days=7)).isoformat()
    recent_trade_count = 0
    try:
        from db.atlas_db import get_db
        with get_db() as _db:
            # Count both new entries and exits recorded this week
            recent_trade_count = _db.execute(
                """
                SELECT COUNT(*) FROM (
                    SELECT id FROM trades WHERE created_at >= :cutoff
                    UNION
                    SELECT id FROM trades WHERE status = 'closed' AND updated_at >= :cutoff
                )
                """,
                {"cutoff": cutoff},
            ).fetchone()[0]
    except Exception as _e:
        logger.warning(
            "SQLite read failed in weekly_review; falling back to JSON ledger: %s", _e
        )
        _fallback = _read_json(TRADE_LEDGER)
        recent_trade_count = len(
            [t for t in _fallback if t.get("recorded_at", "") >= cutoff]
        )

    result = {
        "week_ending": date.today().isoformat(),
        "slippage": slip,
        "fill_quality": fills,
        "stop_analysis": stops,
        "portfolio": port,
        "trades_this_week": recent_trade_count,
    }

    lines = [
        "# Weekly Execution Review",
        "",
        f"Week ending: {date.today().isoformat()}",
        f"Updated: {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        "",
        "## Summary",
        "",
        f"- Fills: {slip.get('total_fills', 0)}",
        f"- Avg slippage: {slip.get('all_slippage', {}).get('mean_bps', '—')} bps (config: 5 bps)",
        f"- Avg fill time: {fills.get('avg_fill_time_s', '—')}s",
        f"- Stop fills: {stops.get('total_stops', 0)}",
        f"- Portfolio return: {port.get('live_return_pct', '—')}%",
        f"- Trades closed: {recent_trade_count}",
        "",
        "## Action Items",
        "",
        "- [ ] Review slippage — adjust config if actual > 2x assumed",
        "- [ ] Review stop fills — switch to stop-limit if slippage > 20 bps",
        "- [ ] Compare live return to backtest projection",
        "",
        "See individual reports: slippage.md, fill_quality.md, stop_analysis.md, portfolio_track.md",
    ]

    _write_brain_md("weekly_review.md", "\n".join(lines) + "\n")
    return result
