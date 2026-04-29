#!/usr/bin/env python3
"""
scripts/backtest_overlay_phase3a.py

Backtest comparing Shadow (current) vs Enforce (hypothetical) overlay sizing
over the last 7 trading days for the sp500 universe.

Scenario A (Shadow / current):  positions sized as actually executed.
Scenario B (Enforce / hypo):    positions sized using overlay's sizing_override.

Usage:
    python3 scripts/backtest_overlay_phase3a.py
    python3 scripts/backtest_overlay_phase3a.py --window 14  # expand window
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path

# ── Project path ────────────────────────────────────────────────────────────
_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))


# ── Trading days helper ──────────────────────────────────────────────────────
_WEEKDAYS = {0, 1, 2, 3, 4}  # Mon-Fri


def _last_n_trading_days(n: int, reference: date | None = None) -> list[date]:
    """Return the last *n* (Mon-Fri) calendar days up to and including *reference*."""
    ref = reference or date.today()
    out: list[date] = []
    d = ref
    while len(out) < n:
        if d.weekday() in _WEEKDAYS:
            out.append(d)
        d -= timedelta(days=1)
    return sorted(out)


# ── DB helpers ───────────────────────────────────────────────────────────────

def _fetch_overlay_decisions(conn: sqlite3.Connection, from_date: str, to_date: str) -> dict:
    """Return {date_str: {action, sizing_override, id}} for the window.

    When a date has multiple decisions (e.g. two runs), takes the tighten-priority row
    (sizing_override NOT NULL wins over NULL; among those, latest timestamp wins).
    """
    rows = conn.execute(
        """
        SELECT date(timestamp) as td, action, sizing_override, id, timestamp
        FROM overlay_decisions
        WHERE date(timestamp) BETWEEN ? AND ?
        ORDER BY td, sizing_override DESC NULLS LAST, timestamp DESC
        """,
        (from_date, to_date),
    ).fetchall()

    best: dict = {}
    for row in rows:
        td = row["td"]
        if td not in best:
            best[td] = dict(row)
    return best


def _fetch_sp500_trades(
    conn: sqlite3.Connection, from_date: str, to_date: str
) -> list[dict]:
    """Return all sp500 non-superseded trades entered in the window."""
    rows = conn.execute(
        """
        SELECT id, ticker, universe, strategy, entry_date, entry_price,
               exit_date, exit_price, pnl, shares, status
        FROM trades
        WHERE universe = 'sp500'
          AND superseded = 0
          AND date(entry_date) BETWEEN ? AND ?
        ORDER BY entry_date
        """,
        (from_date, to_date),
    ).fetchall()
    return [dict(r) for r in rows]


# ── Core backtest logic ──────────────────────────────────────────────────────

def run_backtest(window_days: int = 7, db_path: Path | None = None) -> dict:
    """
    Run the overlay shadow-vs-enforce backtest.

    Returns a result dict with per-trade rows and aggregate metrics.
    """
    db_path = db_path or (_ROOT / "data" / "atlas.db")
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    trading_days = _last_n_trading_days(window_days)
    from_date = str(trading_days[0])
    to_date = str(trading_days[-1])

    decisions = _fetch_overlay_decisions(conn, from_date, to_date)
    trades = _fetch_sp500_trades(conn, from_date, to_date)

    rows: list[dict] = []
    for t in trades:
        trade_date = t["entry_date"][:10]  # YYYY-MM-DD (UTC)
        dec = decisions.get(trade_date, {})
        action = dec.get("action", "no_decision")
        sizing = dec.get("sizing_override")  # None if no_change or no decision

        actual_pnl: float | None = t["pnl"]
        shares: int = t["shares"] or 1
        entry_price: float = t["entry_price"] or 0.0
        exit_price: float | None = t["exit_price"]
        status = t["status"] or "open"

        # Only process closed trades for P&L comparison
        if status != "closed" or exit_price is None:
            rows.append({
                "id": t["id"],
                "ticker": t["ticker"],
                "strategy": t["strategy"],
                "trade_date": trade_date,
                "shares": shares,
                "entry_price": entry_price,
                "exit_price": None,
                "actual_pnl": None,
                "overlay_action": action,
                "sizing_override": sizing,
                "hypo_shares": None,
                "hypo_pnl": None,
                "delta": None,
                "note": "open — excluded from P&L comparison",
            })
            continue

        # Reconciled strategy entries are data-cleanup artifacts — skip them
        if t["strategy"] == "reconciled":
            rows.append({
                "id": t["id"],
                "ticker": t["ticker"],
                "strategy": t["strategy"],
                "trade_date": trade_date,
                "shares": shares,
                "entry_price": entry_price,
                "exit_price": exit_price,
                "actual_pnl": actual_pnl,
                "overlay_action": action,
                "sizing_override": sizing,
                "hypo_shares": shares,
                "hypo_pnl": actual_pnl,
                "delta": 0.0,
                "note": "reconciled — overlay not applied to reconciliation entries",
            })
            continue

        # Verify pnl matches arithmetic (use computed value for consistency)
        computed_pnl = round((exit_price - entry_price) * shares, 4)

        if sizing is not None and action == "tighten":
            # Enforce scenario: apply multiplier (same truncation as live_executor)
            hypo_shares = int(shares * sizing)
            if hypo_shares <= 0:
                # Trade would have been blocked entirely
                hypo_pnl = 0.0
                note = f"would_be_BLOCKED (int({shares}×{sizing})={hypo_shares})"
            else:
                hypo_pnl = round((exit_price - entry_price) * hypo_shares, 4)
                note = f"reduced {shares}→{hypo_shares} shares (×{sizing})"
        else:
            # No overlay tightening — hypothetical = actual
            hypo_shares = shares
            hypo_pnl = computed_pnl
            note = f"no_tighten ({action}) — hypothetical = actual"

        delta = round((hypo_pnl if hypo_pnl is not None else 0.0) - computed_pnl, 4)

        rows.append({
            "id": t["id"],
            "ticker": t["ticker"],
            "strategy": t["strategy"],
            "trade_date": trade_date,
            "shares": shares,
            "entry_price": entry_price,
            "exit_price": exit_price,
            "actual_pnl": computed_pnl,
            "overlay_action": action,
            "sizing_override": sizing,
            "hypo_shares": hypo_shares,
            "hypo_pnl": hypo_pnl,
            "delta": delta,
            "note": note,
        })

    conn.close()

    # ── Aggregate ─────────────────────────────────────────────────────────
    closed_rows = [r for r in rows if r["actual_pnl"] is not None and r["strategy"] != "reconciled"]
    tighten_rows = [r for r in closed_rows if r["overlay_action"] == "tighten"]

    actual_cumulative = round(sum(r["actual_pnl"] for r in closed_rows), 4)
    hypo_cumulative = round(sum(r["hypo_pnl"] for r in closed_rows), 4)  # type: ignore[arg-type]
    cumulative_delta = round(hypo_cumulative - actual_cumulative, 4)

    all_deltas = sorted(r["delta"] for r in closed_rows if r["delta"] is not None)
    tighten_deltas = sorted(r["delta"] for r in tighten_rows if r["delta"] is not None)

    def _median(lst: list[float]) -> float | None:
        if not lst:
            return None
        n = len(lst)
        mid = n // 2
        return lst[mid] if n % 2 else (lst[mid - 1] + lst[mid]) / 2

    median_delta_all = _median(all_deltas)
    median_delta_tighten = _median(tighten_deltas)

    # ── Decision ──────────────────────────────────────────────────────────
    # FLIP if cumulative delta >= 0.01 OR median per-trade delta >= 0.0
    flip = (
        (cumulative_delta is not None and cumulative_delta >= 0.01)
        or (median_delta_all is not None and median_delta_all >= 0.0)
    )
    decision = "FLIP" if flip else "NO_FLIP"

    return {
        "window_days": window_days,
        "trading_days": [str(d) for d in trading_days],
        "from_date": from_date,
        "to_date": to_date,
        "n_trades_total": len(rows),
        "n_trades_closed": len(closed_rows),
        "n_trades_open": len(rows) - len(closed_rows),
        "n_trades_tighten_affected": len(tighten_rows),
        "actual_cumulative_pnl": actual_cumulative,
        "hypo_cumulative_pnl": hypo_cumulative,
        "cumulative_delta": cumulative_delta,
        "median_delta_all_trades": median_delta_all,
        "median_delta_tighten_trades": median_delta_tighten,
        "decision": decision,
        "flip_criteria": {
            "cumulative_delta_ge_001": (cumulative_delta is not None and cumulative_delta >= 0.01),
            "median_delta_ge_0": (median_delta_all is not None and median_delta_all >= 0.0),
        },
        "overlay_decisions_by_date": {
            td: {"action": d.get("action"), "sizing_override": d.get("sizing_override")}
            for td, d in sorted(decisions.items())
        },
        "rows": rows,
    }


# ── CLI ──────────────────────────────────────────────────────────────────────

def _fmt_pnl(v: float | None) -> str:
    if v is None:
        return "N/A"
    sign = "+" if v >= 0 else ""
    return f"{sign}${v:.2f}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Overlay Phase 3A backtest")
    parser.add_argument("--window", type=int, default=7, help="Trading days to look back")
    parser.add_argument("--json", action="store_true", help="Output raw JSON")
    args = parser.parse_args(argv)

    result = run_backtest(window_days=args.window)

    if args.json:
        print(json.dumps(result, indent=2, default=str))
        return 0

    print(f"\n{'='*70}")
    print("Overlay Phase 3A Backtest — Shadow vs Enforce (sp500)")
    print(f"{'='*70}")
    print(f"Window:  {result['from_date']} → {result['to_date']} ({result['window_days']} trading days)")
    print(f"Trades:  {result['n_trades_total']} total, {result['n_trades_closed']} closed, {result['n_trades_open']} open")
    print(f"Overlay tighten affected (closed): {result['n_trades_tighten_affected']}")
    print()

    print("Overlay decisions by date:")
    for td, d in result["overlay_decisions_by_date"].items():
        so = d["sizing_override"]
        so_str = f" sizing={so}" if so else ""
        print(f"  {td}: {d['action']}{so_str}")
    print()

    print(f"{'ID':>4}  {'Ticker':<6} {'Date':<10} {'Shr':>3} {'Entry':>8} {'Exit':>8}  "
          f"{'ActPnL':>8} {'Overlay':<12} {'HypoShr':>7} {'HypoPnL':>8} {'Delta':>8}")
    print("-" * 100)
    for r in result["rows"]:
        if r["actual_pnl"] is None:
            continue  # skip open trades in table
        print(
            f"{r['id']:>4}  {r['ticker']:<6} {r['trade_date']:<10} {r['shares']:>3} "
            f"${r['entry_price']:>7.2f} ${r['exit_price']:>7.2f}  "
            f"{_fmt_pnl(r['actual_pnl']):>8}  "
            f"{r['overlay_action']+'('+str(r['sizing_override'])+')' if r['sizing_override'] else r['overlay_action']:<12}  "
            f"{str(r['hypo_shares']):>7} {_fmt_pnl(r['hypo_pnl']):>8} {_fmt_pnl(r['delta']):>8}"
        )
    print("-" * 100)

    print()
    print(f"SCENARIO A (Shadow/actual) cumulative P&L:     {_fmt_pnl(result['actual_cumulative_pnl'])}")
    print(f"SCENARIO B (Enforce/hypo) cumulative P&L:      {_fmt_pnl(result['hypo_cumulative_pnl'])}")
    print(f"Delta (Y - X):                                  {_fmt_pnl(result['cumulative_delta'])}")
    print(f"Median per-trade delta (all):                   {_fmt_pnl(result['median_delta_all_trades'])}")
    print(f"Median per-trade delta (tighten-affected):      {_fmt_pnl(result['median_delta_tighten_trades'])}")
    print()
    print("FLIP criteria:")
    crit = result["flip_criteria"]
    print(f"  cumulative_delta >= $0.01:  {'✅ PASS' if crit['cumulative_delta_ge_001'] else '❌ FAIL'}")
    print(f"  median_delta >= $0.00:      {'✅ PASS' if crit['median_delta_ge_0'] else '❌ FAIL'}")
    print()
    verdict = result["decision"]
    print(f"{'🟢 DECISION: ' + verdict if verdict == 'FLIP' else '🔴 DECISION: ' + verdict}")
    print(f"{'='*70}\n")

    return 0


if __name__ == "__main__":
    sys.exit(main())
