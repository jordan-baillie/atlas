#!/usr/bin/env python3
"""
Sweep profit_target_atr_mult for opening_gap and connors_rsi2.
Runs full walk-forward portfolio backtests with different TP multipliers.

Usage:
    cd /root/atlas && python3 scripts/sweep_tp_multiplier.py
"""

import sys
import os
import json
import copy
import time
import warnings
import logging
from datetime import datetime
from pathlib import Path
from concurrent.futures import ProcessPoolExecutor, as_completed

ATLAS_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ATLAS_ROOT))
os.chdir(ATLAS_ROOT)


def run_single_backtest(task):
    """Run one backtest in a subprocess."""
    label, og_tp, cr_tp, config_path = task

    # Subprocess imports
    import sys, os, json, copy, warnings, logging
    from pathlib import Path
    # config_path is like /root/atlas/config/active/sp500.json -> 3 parents up
    atlas = Path(config_path).resolve().parent.parent.parent
    sys.path.insert(0, str(atlas))
    os.chdir(atlas)
    warnings.filterwarnings("ignore")
    logging.disable(logging.CRITICAL)

    import pandas as pd
    from backtest.engine import BacktestEngine
    from universe.builder import get_universe_tickers
    from markets import get_market

    try:
        config = json.loads(Path(config_path).read_text())

        # Set TP multipliers
        config["strategies"]["opening_gap"]["profit_target_atr_mult"] = og_tp
        config["strategies"]["connors_rsi2"]["profit_target_atr_mult"] = cr_tp

        # Load tickers
        market_id = config.get("market", "sp500")
        try:
            tickers = get_universe_tickers(market_id)
        except Exception:
            m = get_market(market_id)
            tickers = m.get_universe_tickers()[:20]

        # Load data from cache
        base_cache = atlas / config["data"]["cache_dir"]
        market_cache = base_cache / market_id
        data = {}
        for ticker in tickers:
            fname = ticker.replace(".", "_") + ".parquet"
            path = market_cache / fname
            if not path.exists():
                path = base_cache / fname
            if path.exists():
                data[ticker] = pd.read_parquet(path)

        if not data:
            return {"label": label, "og_tp": og_tp, "cr_tp": cr_tp, "error": "No data"}

        # Build strategies
        from strategies.momentum_breakout import MomentumBreakout
        from strategies.mean_reversion import MeanReversion
        from strategies.trend_following import TrendFollowing
        from strategies.sector_rotation import SectorRotation
        from strategies.short_term_mr import ShortTermMR
        from strategies.opening_gap import OpeningGap
        from strategies.connors_rsi2 import ConnorsRSI2

        strats = []
        sc = config["strategies"]
        if sc["momentum_breakout"]["enabled"]:
            strats.append(MomentumBreakout(config))
        if sc["mean_reversion"]["enabled"]:
            strats.append(MeanReversion(config))
        if sc["trend_following"]["enabled"]:
            strats.append(TrendFollowing(config))
        if sc.get("sector_rotation", {}).get("enabled", False):
            strats.append(SectorRotation(config))
        if sc.get("short_term_mr", {}).get("enabled", False):
            strats.append(ShortTermMR(config))
        if sc.get("opening_gap", {}).get("enabled", False):
            strats.append(OpeningGap(config))
        if sc.get("connors_rsi2", {}).get("enabled", False):
            strats.append(ConnorsRSI2(config))

        # Run backtest
        engine = BacktestEngine(config, market_id=market_id)
        result = engine.run_walkforward(data, strats)

        m = result.metrics
        trades = result.trades

        # Per-strategy breakdown
        og_trades = [t for t in trades if t.get("strategy") == "opening_gap"]
        cr_trades = [t for t in trades if t.get("strategy") == "connors_rsi2"]

        def strat_stats(trade_list):
            if not trade_list:
                return {"trades": 0, "win_rate": 0, "pf": 0, "avg_ret": 0}
            wins = [t for t in trade_list if t.get("pnl", t.get("profit", 0)) > 0]
            losses = [t for t in trade_list if t.get("pnl", t.get("profit", 0)) <= 0]
            win_rate = len(wins) / len(trade_list) * 100 if trade_list else 0
            gross_wins = sum(t.get("pnl", t.get("profit", 0)) for t in wins)
            gross_losses = abs(sum(t.get("pnl", t.get("profit", 0)) for t in losses))
            pf = gross_wins / gross_losses if gross_losses > 0 else 99.0
            avg_ret = sum(t.get("return_pct", t.get("pnl_pct", 0)) for t in trade_list) / len(trade_list) if trade_list else 0
            return {"trades": len(trade_list), "win_rate": round(win_rate, 1),
                    "pf": round(min(pf, 99), 2), "avg_ret": round(avg_ret * 100 if abs(avg_ret) < 1 else avg_ret, 3)}

        og_s = strat_stats(og_trades)
        cr_s = strat_stats(cr_trades)

        return {
            "label": label,
            "og_tp": og_tp,
            "cr_tp": cr_tp,
            "sharpe": round(m.get("sharpe", 0), 4),
            "cagr_pct": round(m.get("cagr", 0) * 100, 2),
            "max_dd_pct": round(m.get("max_drawdown", 0) * 100, 2),
            "profit_factor": round(m.get("profit_factor", 0), 3),
            "total_trades": m.get("total_trades", len(trades)),
            "win_rate_pct": round(m.get("win_rate", 0) * 100, 1),
            "calmar": round(m.get("calmar", 0), 3),
            "sortino": round(m.get("sortino", 0), 3),
            # Per-strategy
            "og_trades": og_s["trades"], "og_wr": og_s["win_rate"], "og_pf": og_s["pf"],
            "cr_trades": cr_s["trades"], "cr_wr": cr_s["win_rate"], "cr_pf": cr_s["pf"],
        }

    except Exception as e:
        import traceback
        return {"label": label, "og_tp": og_tp, "cr_tp": cr_tp, "error": f"{e}\n{traceback.format_exc()[-300:]}"}


def main():
    config_path = str(ATLAS_ROOT / "config" / "active" / "sp500.json")
    workers = min(6, os.cpu_count() or 4)

    # TP multipliers to sweep
    tp_values = [0.0, 1.0, 1.5, 2.0, 2.5, 3.0, 3.5, 4.0, 5.0]

    tasks = []

    # Phase 1: Sweep opening_gap TP (connors_rsi2 stays at 0)
    for tp in tp_values:
        tasks.append((f"OG={tp:.1f}", tp, 0.0, config_path))

    # Phase 2: Sweep connors_rsi2 TP (opening_gap stays at 0)
    for tp in tp_values[1:]:  # Skip 0 (already in Phase 1 baseline)
        tasks.append((f"CR={tp:.1f}", 0.0, tp, config_path))

    print(f"{'='*90}")
    print(f"  TP Multiplier Sweep — opening_gap + connors_rsi2")
    print(f"  {len(tasks)} backtests, {workers} workers")
    print(f"  OG sweep: {tp_values}")
    print(f"  CR sweep: {tp_values}")
    print(f"{'='*90}\n")

    start = time.time()
    results = []

    with ProcessPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(run_single_backtest, t): t for t in tasks}
        done = 0
        for future in as_completed(futures):
            done += 1
            try:
                r = future.result()
                results.append(r)
                if "error" in r:
                    print(f"  [{done:2d}/{len(tasks)}] {r['label']:>10} — ERROR: {r['error'][:100]}")
                else:
                    print(f"  [{done:2d}/{len(tasks)}] {r['label']:>10} — "
                          f"Sharpe={r['sharpe']:.3f}  CAGR={r['cagr_pct']:+.1f}%  "
                          f"MaxDD={r['max_dd_pct']:.1f}%  PF={r['profit_factor']:.2f}  "
                          f"Trades={r['total_trades']}  WR={r['win_rate_pct']:.1f}%  "
                          f"OG[{r['og_trades']}t/{r['og_wr']:.0f}%]  CR[{r['cr_trades']}t/{r['cr_wr']:.0f}%]")
            except Exception as e:
                done_task = futures[future]
                print(f"  [{done:2d}/{len(tasks)}] {done_task[0]:>10} — CRASH: {e}")

    elapsed = time.time() - start
    print(f"\nCompleted in {elapsed:.0f}s ({elapsed/len(tasks):.1f}s per backtest)\n")

    # Display sorted tables
    og_results = sorted([r for r in results if r["label"].startswith("OG") and "error" not in r],
                        key=lambda r: r["og_tp"])
    cr_results = sorted([r for r in results if r["label"].startswith("CR") and "error" not in r],
                        key=lambda r: r["cr_tp"])

    def print_table(title, data, tp_key):
        print(f"\n{'='*100}")
        print(f"  {title}")
        print(f"{'='*100}")
        print(f"  {'TP':>4}  {'Sharpe':>7}  {'CAGR%':>7}  {'MaxDD%':>7}  {'PF':>6}  {'Sortino':>7}  "
              f"{'Calmar':>7}  {'Trades':>6}  {'WR%':>5}  {'OG_t':>5}  {'OG_WR':>5}  {'OG_PF':>6}  "
              f"{'CR_t':>5}  {'CR_WR':>5}  {'CR_PF':>6}")
        print(f"  {'─'*4}  {'─'*7}  {'─'*7}  {'─'*7}  {'─'*6}  {'─'*7}  "
              f"{'─'*7}  {'─'*6}  {'─'*5}  {'─'*5}  {'─'*5}  {'─'*6}  "
              f"{'─'*5}  {'─'*5}  {'─'*6}")

        if not data:
            print("  (no results)")
            return

        best_sharpe = max(r["sharpe"] for r in data)
        for r in data:
            tp = r[tp_key]
            star = " ★" if r["sharpe"] == best_sharpe else "  "
            print(f"  {tp:4.1f}  {r['sharpe']:7.3f}  {r['cagr_pct']:+7.1f}  {r['max_dd_pct']:7.1f}  "
                  f"{r['profit_factor']:6.2f}  {r['sortino']:7.3f}  {r['calmar']:7.3f}  "
                  f"{r['total_trades']:6d}  {r['win_rate_pct']:5.1f}  "
                  f"{r['og_trades']:5d}  {r['og_wr']:5.1f}  {r['og_pf']:6.2f}  "
                  f"{r['cr_trades']:5d}  {r['cr_wr']:5.1f}  {r['cr_pf']:6.2f}{star}")

        base = [r for r in data if r[tp_key] == 0.0]
        best = max(data, key=lambda r: r["sharpe"])
        if base:
            b = base[0]
            print(f"\n  Baseline (TP=0): Sharpe={b['sharpe']:.3f}, CAGR={b['cagr_pct']:+.1f}%")
            print(f"  Best (TP={best[tp_key]:.1f}x): Sharpe={best['sharpe']:.3f} "
                  f"(Δ={best['sharpe']-b['sharpe']:+.3f}), CAGR={best['cagr_pct']:+.1f}% "
                  f"(Δ={best['cagr_pct']-b['cagr_pct']:+.1f}pp)")

    # Add baseline to CR results
    baseline = [r for r in og_results if r["og_tp"] == 0.0]
    cr_with_base = baseline + cr_results if baseline else cr_results

    print_table("Opening Gap — TP Multiplier Sweep (CR=0)", og_results, "og_tp")
    print_table("Connors RSI2 — TP Multiplier Sweep (OG=0)", cr_with_base, "cr_tp")

    # Save
    out = ATLAS_ROOT / "research" / "results" / f"tp_sweep_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({
        "type": "tp_multiplier_sweep",
        "timestamp": datetime.now().isoformat(),
        "tp_values": tp_values,
        "results": results,
    }, indent=2, default=str))
    print(f"\n  Results saved: {out}\n")


if __name__ == "__main__":
    main()
