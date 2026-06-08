#!/usr/bin/env python3
"""Failure instrumentation across all shm cross-OOS battery families.

Answers the "gate-bound vs edge-bound" question (2026-06-08): for every family, pull OOS trade
count, pre-deflation OOS Sharpe, CPCV, DSR, and deployment breadth. If broadly-deployed,
well-powered books still show ~0 edge -> EDGE-bound (no liquid-equity edge at this scale). If
most are NaN-DSR / low-trade -> POWER/gate-bound (need breadth or longer window, not more families).

Usage: python3 scripts/diagnose_family_failures.py
"""
import glob
import json
import math
import statistics as st


def _num(x, n=2):
    if x is None:
        return "  -  "
    if isinstance(x, float) and math.isnan(x):
        return " NaN "
    return f"{x:>5.{n}f}" if isinstance(x, (int, float)) else str(x)


def main() -> int:
    files = glob.glob("backtest/results/search/battery_*_shm.json") + \
        glob.glob("backtest/results/battery_*shm*.json")
    rows, seen = [], set()
    for f in sorted(files):
        try:
            d = json.load(open(f))
        except Exception:
            continue
        strat = d.get("strategy") or f.split("battery_")[-1].replace("_shm.json", "")
        if strat in seen:
            continue
        seen.add(strat)
        ts = d.get("time_split", {})
        oos, is_ = ts.get("out_of_sample", {}), ts.get("in_sample", {})
        b = d.get("cross_oos", {}).get("bundle", {})
        dep = d.get("deployment", {})
        rows.append({
            "strat": strat[:30], "IStr": is_.get("total_trades"), "OOStr": oos.get("total_trades"),
            "OOSsharpe": oos.get("sharpe"), "cpcv": b.get("median_cpcv_sharpe"), "dsr": b.get("dsr"),
            "deptr": dep.get("n_trades"), "peak": dep.get("peak_concurrent"),
            "depPass": dep.get("passed"), "tier": d.get("cross_oos", {}).get("tier", "?"),
        })

    def dsr_key(r):
        v = r["dsr"]
        return v if isinstance(v, (int, float)) and not math.isnan(v) else -9
    print(f"{'strategy':30s} {'IStr':>5} {'OOStr':>5} {'OOSshrp':>7} {'cpcv':>6} {'dsr':>6} "
          f"{'depTr':>5} {'peak':>4} {'depPass':>7} {'tier':>6}")
    print("-" * 100)
    for r in sorted(rows, key=dsr_key, reverse=True):
        print(f"{r['strat']:30s} {_num(r['IStr'],0):>5} {_num(r['OOStr'],0):>5} "
              f"{_num(r['OOSsharpe']):>7} {_num(r['cpcv']):>6} {_num(r['dsr']):>6} "
              f"{_num(r['deptr'],0):>5} {_num(r['peak'],0):>4} {str(r['depPass']):>7} {str(r['tier']):>6}")

    def valid(xs):
        return [x for x in xs if isinstance(x, (int, float)) and not (isinstance(x, float) and math.isnan(x))]
    oostr = valid([r["OOStr"] for r in rows])
    dsrs = [r["dsr"] for r in rows]
    nan_dsr = sum(1 for x in dsrs if x is None or (isinstance(x, float) and math.isnan(x)))
    deppass = sum(1 for r in rows if r["depPass"] is True)
    oss = valid([r["OOSsharpe"] for r in rows])
    print(f"\n=== DIAGNOSTIC ({len(rows)} families) ===")
    print(f"OOS trades: median {st.median(oostr) if oostr else '-'} | >=50: "
          f"{sum(1 for x in oostr if x>=50)}/{len(rows)} | <30: {sum(1 for x in oostr if x<30)}/{len(rows)}")
    print(f"deployment PASSED (broad book): {deppass}/{len(rows)}")
    print(f"NaN/degenerate DSR: {nan_dsr}/{len(rows)}")
    if oss:
        print(f"OOS Sharpe: median {st.median(oss):.3f} | range [{min(oss):.2f}, {max(oss):.2f}]")
    if valid(dsrs):
        print(f"DSR: median {st.median(valid(dsrs)):.3f} | max {max(valid(dsrs)):.3f}")
    print("\nRead: broadly-deployed + well-powered books with ~0 edge => EDGE-bound, not gate-bound.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
