#!/usr/bin/env python3
"""
Lightweight index over backtest results + research experiments.

Gives one-liner summaries and filtered lookups without needing a database.

Usage:
    python -m backtest.index build                  # rebuild index
    python -m backtest.index ls                     # list all results
    python -m backtest.index ls oos_validation      # filter by type
    python -m backtest.index best                   # top results by sharpe
    python -m backtest.index best cagr              # top results by cagr
    python -m backtest.index experiments            # list research experiments
    python -m backtest.index compare FILE1 FILE2    # side-by-side diff
"""

import json
import os
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RESULTS_DIR = ROOT / "backtest" / "results"
EXPERIMENTS_DIR = ROOT / "research" / "experiments"
INDEX_PATH = RESULTS_DIR / "index.json"

# Max age before auto-rebuild (seconds)
INDEX_MAX_AGE = 3600


# ── Classification ─────────────────────────────────────────────────

def classify_result(data: dict, filename: str) -> str:
    if "validation_type" in data or "overall_pass" in data:
        return "oos_validation"
    if "baseline_combined" in data:
        return "reoptimization"
    if "per_strategy" in data and "metrics" in data:
        return "optimized_summary"
    if filename.startswith("backtest_2"):
        return "backtest"
    if filename.startswith("fee_"):
        return "fee_analysis"
    if "equity_curve" in data:
        return "equity_curve"
    if "phase" in data:
        return "phase_report"
    return "other"


# ── Metric extraction ──────────────────────────────────────────────

def _flat_metrics(m: dict) -> dict:
    """Normalise a metrics dict to standard keys."""
    return {
        "cagr": m.get("cagr") or m.get("cagr_pct"),
        "sharpe": m.get("sharpe"),
        "sortino": m.get("sortino"),
        "max_dd": m.get("max_drawdown") or m.get("max_drawdown_pct") or m.get("max_dd"),
        "win_rate": m.get("win_rate") or m.get("win_rate_pct"),
        "profit_factor": m.get("profit_factor") or m.get("pf"),
        "total_trades": m.get("total_trades") or m.get("trades"),
        "total_pnl": m.get("total_pnl"),
        "avg_hold_days": m.get("avg_hold_days"),
    }


def extract_metrics(data: dict, rtype: str) -> dict | None:
    if rtype == "backtest" and "metrics" in data:
        return _flat_metrics(data["metrics"])

    if rtype == "oos_validation":
        # Two shapes: flat (time_split.oos_metrics) or nested (test1_time_period_split.oos_metrics)
        oos = (data.get("time_split", {}).get("oos_metrics")
               or data.get("test1_time_period_split", {}).get("oos_metrics")
               or {})
        m = _flat_metrics(oos)
        m["overall_pass"] = data.get("overall_pass",
                                     data.get("summary", {}).get("overall_pass"))
        return m

    if rtype == "reoptimization":
        b = data.get("baseline_combined", {})
        m = _flat_metrics(b)
        m["market"] = data.get("market_id")
        m["n_tickers"] = data.get("n_tickers")
        m["strategies"] = data.get("active_strategies")
        return m

    if rtype == "optimized_summary" and "metrics" in data:
        m = _flat_metrics(data["metrics"])
        m["per_strategy"] = {
            k: {"pnl": v.get("pnl"), "trades": v.get("count"), "wr": v.get("win_rate")}
            for k, v in data.get("per_strategy", {}).items()
        }
        return m

    return None


def extract_experiment(data: dict) -> dict:
    """Summarise a research experiment (eval-* or exp-* file)."""
    outputs = data.get("outputs") or {}
    is_eval = "solo" in data and not outputs

    # Determine the "best" metrics to surface
    if is_eval:
        # eval files have solo metrics at top level
        solo = data.get("solo", {})
        sharpe = solo.get("sharpe")
        cagr = solo.get("cagr_pct")
        trades = solo.get("total_trades")
        pnl = solo.get("total_pnl")
    elif "optimized" in outputs:
        # optimization result
        opt = outputs["optimized"]
        sharpe = opt.get("sharpe")
        cagr = opt.get("cagr_pct")
        trades = opt.get("total_trades")
        pnl = opt.get("total_pnl")
    elif "combined" in outputs:
        # combination result
        comb = outputs["combined"]
        sharpe = comb.get("sharpe")
        cagr = comb.get("cagr_pct")
        trades = comb.get("total_trades")
        pnl = comb.get("total_pnl")
    elif "solo" in outputs:
        # solo eval wrapped in exp
        solo = outputs["solo"]
        sharpe = solo.get("sharpe")
        cagr = solo.get("cagr_pct")
        trades = solo.get("total_trades")
        pnl = solo.get("total_pnl")
    elif "error" in outputs:
        sharpe = cagr = trades = pnl = None
    else:
        sharpe = outputs.get("sharpe") or outputs.get("best_sharpe")
        cagr = outputs.get("cagr") or outputs.get("best_cagr")
        trades = outputs.get("total_trades") or outputs.get("best_trades")
        pnl = outputs.get("total_pnl") or outputs.get("best_pnl")

    # Strategy name from various locations
    strategy = (data.get("strategy")
                or data.get("queue_entry", {}).get("strategy_name")
                or outputs.get("strategy"))

    # Market
    market = (data.get("market")
              or data.get("queue_entry", {}).get("market")
              or outputs.get("market"))

    # Determine mode
    qe = data.get("queue_entry", {})
    mode = data.get("mode") or qe.get("method") or qe.get("category", "")

    return {
        "experiment_id": data.get("id") or data.get("experiment_id"),
        "strategy": strategy,
        "market": market,
        "mode": mode,
        "verdict": data.get("verdict"),
        "promoted": data.get("promoted", False),
        "sharpe": sharpe,
        "cagr": cagr,
        "trades": trades,
        "pnl": pnl,
        "has_error": "error" in outputs if isinstance(outputs, dict) else False,
        "runtime_s": (data.get("metadata", {}).get("runtime_s")
                      or data.get("runtime_s")),
        "timestamp": (data.get("metadata", {}).get("finished_at")
                      or data.get("timestamp")),
    }


# ── Index build ────────────────────────────────────────────────────

def build_index() -> dict:
    index = {
        "built_at": datetime.now().isoformat(),
        "results": [],
        "experiments": [],
    }

    # Backtest results
    for fp in sorted(RESULTS_DIR.glob("*.json")):
        if fp.name == "index.json":
            continue
        try:
            data = json.loads(fp.read_text())
        except Exception:
            continue
        rtype = classify_result(data, fp.name)
        index["results"].append({
            "file": fp.name,
            "type": rtype,
            "timestamp": data.get("timestamp"),
            "config_version": data.get("config_version") or data.get("version"),
            "metrics": extract_metrics(data, rtype),
        })

    # Research experiments
    for fp in sorted(EXPERIMENTS_DIR.glob("*.json")):
        try:
            data = json.loads(fp.read_text())
        except Exception:
            continue
        entry = extract_experiment(data)
        entry["file"] = fp.name
        index["experiments"].append(entry)

    INDEX_PATH.write_text(json.dumps(index, indent=2, default=str))
    return index


def load_or_build() -> dict:
    if INDEX_PATH.exists():
        age = datetime.now().timestamp() - INDEX_PATH.stat().st_mtime
        if age < INDEX_MAX_AGE:
            return json.loads(INDEX_PATH.read_text())
    return build_index()


# ── Display helpers ────────────────────────────────────────────────

def fmt(val, width=8):
    if val is None:
        return "-".center(width)
    if isinstance(val, bool):
        return ("✓" if val else "✗").center(width)
    if isinstance(val, float):
        return f"{val:>{width}.4f}"
    if isinstance(val, int):
        return str(val).rjust(width)
    return str(val).rjust(width)


METRIC_KEYS = ["sharpe", "cagr", "max_dd", "win_rate", "profit_factor", "total_trades", "total_pnl"]

# Higher is better except max_dd
HIGHER_IS_BETTER = {"sharpe", "cagr", "win_rate", "profit_factor", "total_trades", "total_pnl",
                    "sortino", "avg_hold_days"}


def print_results_table(results, title="Backtest Results"):
    print(f"\n{'─' * 95}")
    print(f"  {title}")
    print(f"{'─' * 95}")
    header = (f"  {'File':<42} {'Type':<18} "
              f"{'Sharpe':>8} {'CAGR':>8} {'MaxDD':>8} {'PF':>6} {'Trades':>7}")
    print(header)
    print(f"  {'─' * 40}  {'─' * 16}  {'─' * 6}  {'─' * 6}  {'─' * 6} {'─' * 5} {'─' * 6}")

    for r in results:
        m = r.get("metrics") or {}
        sharpe = m.get("sharpe")
        cagr = m.get("cagr")
        dd = m.get("max_dd")
        pf = m.get("profit_factor")
        trades = m.get("total_trades")

        # For OOS, show pass/fail suffix
        suffix = ""
        if r["type"] == "oos_validation" and m.get("overall_pass") is not None:
            suffix = " ✓" if m["overall_pass"] else " ✗"

        print(f"  {r['file']:<42} {r['type']:<18} "
              f"{fmt(sharpe)} {fmt(cagr)} {fmt(dd)} {fmt(pf, 6)} {fmt(trades, 7)}{suffix}")

    print(f"  {'─' * 93}")
    print(f"  {len(results)} result(s)")


def print_experiments_table(experiments, title="Research Experiments"):
    print(f"\n{'─' * 110}")
    print(f"  {title}")
    print(f"{'─' * 110}")
    header = (f"  {'ID':<28} {'Strategy':<18} {'Mode':<14} "
              f"{'Verdict':>7} {'Sharpe':>8} {'CAGR':>8} {'PnL':>9} {'Trades':>7} {'Prom':>5}")
    print(header)
    print(f"  {'─' * 26}  {'─' * 16}  {'─' * 12}  "
          f"{'─' * 5}  {'─' * 6}  {'─' * 6}  {'─' * 7}  {'─' * 5}  {'─' * 3}")

    for e in experiments:
        eid = (e.get("experiment_id") or "?")[:28]
        strat = (e.get("strategy") or "?")[:18]
        mode = (e.get("mode") or "?")[:14]
        verdict = e.get("verdict") or ("err" if e.get("has_error") else "-")
        prom = "✓" if e.get("promoted") else ""

        print(f"  {eid:<28} {strat:<18} {mode:<14} "
              f"{verdict:>7} {fmt(e.get('sharpe'))} {fmt(e.get('cagr'))} "
              f"{fmt(e.get('pnl'), 9)} {fmt(e.get('trades'), 7)} {prom:>5}")

    print(f"  {'─' * 108}")
    passed = sum(1 for e in experiments if e.get("verdict") == "pass")
    failed = sum(1 for e in experiments if e.get("verdict") == "fail")
    promoted = sum(1 for e in experiments if e.get("promoted"))
    print(f"  {len(experiments)} experiment(s)  |  {passed} pass  {failed} fail  |  {promoted} promoted")


# ── CLI commands ───────────────────────────────────────────────────

def cmd_build(_args):
    idx = build_index()
    n_r = len(idx["results"])
    n_e = len(idx["experiments"])
    print(f"Index built: {n_r} results, {n_e} experiments → {INDEX_PATH}")


def cmd_ls(args):
    index = load_or_build()
    results = index["results"]
    if args:
        rtype = args[0]
        results = [r for r in results if rtype in r["type"]]
    print_results_table(results)


def cmd_experiments(args):
    index = load_or_build()
    exps = index["experiments"]
    if args:
        filt = args[0].lower()
        exps = [e for e in exps
                if filt in (e.get("verdict") or "")
                or filt in (e.get("strategy") or "")
                or filt in (e.get("market") or "")]
    print_experiments_table(exps)


def cmd_best(args):
    index = load_or_build()
    metric = args[0] if args else "sharpe"
    reverse = metric in HIGHER_IS_BETTER

    scored = []
    for r in index["results"]:
        m = r.get("metrics") or {}
        val = m.get(metric)
        if val is not None and isinstance(val, (int, float)):
            scored.append((val, r))
    scored.sort(key=lambda x: x[0], reverse=reverse)

    direction = "↓" if not reverse else "↑"
    print_results_table(
        [r for _, r in scored[:10]],
        f"Top 10 by {metric} ({direction} = better)"
    )


def cmd_compare(args):
    if len(args) < 2:
        print("Usage: python -m backtest.index compare <file1> <file2>")
        sys.exit(1)

    rows = []
    for fname in args[:2]:
        # Try exact name, then with .json suffix
        fp = RESULTS_DIR / fname
        if not fp.exists():
            fp = RESULTS_DIR / (fname + ".json")
        if not fp.exists():
            # Try experiments dir
            fp = EXPERIMENTS_DIR / fname
            if not fp.exists():
                fp = EXPERIMENTS_DIR / (fname + ".json")
        if not fp.exists():
            print(f"Not found: {fname}")
            sys.exit(1)

        data = json.loads(fp.read_text())
        rtype = classify_result(data, fp.name)
        metrics = extract_metrics(data, rtype) or {}

        # If it's an experiment, use experiment metrics instead
        if not metrics and fp.parent == EXPERIMENTS_DIR:
            exp = extract_experiment(data)
            metrics = {
                "sharpe": exp.get("sharpe"),
                "cagr": exp.get("cagr"),
                "trades": exp.get("trades"),
                "pnl": exp.get("pnl"),
            }

        rows.append({"file": fp.name, "type": rtype, "metrics": metrics})

    # Collect all keys
    all_keys = set()
    for r in rows:
        all_keys.update(r["metrics"].keys())
    # Filter out non-numeric / structural keys
    skip = {"strategies", "per_strategy", "market", "n_tickers", "overall_pass"}
    all_keys = sorted(k for k in all_keys if k not in skip)

    f1, f2 = rows[0]["file"], rows[1]["file"]
    print(f"\n{'─' * 70}")
    print(f"  Compare: {f1}  vs  {f2}")
    print(f"{'─' * 70}")
    print(f"  {'Metric':<20} {'File 1':>14} {'File 2':>14} {'Delta':>14}")
    print(f"  {'─' * 18}  {'─' * 12}  {'─' * 12}  {'─' * 12}")

    for k in all_keys:
        v1 = rows[0]["metrics"].get(k)
        v2 = rows[1]["metrics"].get(k)
        delta = ""
        if isinstance(v1, (int, float)) and isinstance(v2, (int, float)):
            d = v2 - v1
            sign = "+" if d > 0 else ""
            if isinstance(d, float):
                delta = f"{sign}{d:.4f}"
            else:
                delta = f"{sign}{d}"
        print(f"  {k:<20} {fmt(v1, 14)} {fmt(v2, 14)} {delta:>14}")

    # Show pass/fail for OOS
    for i, r in enumerate(rows):
        op = r["metrics"].get("overall_pass")
        if op is not None:
            print(f"\n  File {i+1} OOS: {'PASS ✓' if op else 'FAIL ✗'}")


COMMANDS = {
    "build": cmd_build,
    "ls": cmd_ls,
    "best": cmd_best,
    "compare": cmd_compare,
    "experiments": cmd_experiments,
    "exp": cmd_experiments,
}


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "ls"
    if cmd in ("-h", "--help"):
        print(__doc__)
        sys.exit(0)
    if cmd not in COMMANDS:
        print(f"Unknown command: {cmd}")
        print(f"Available: {', '.join(COMMANDS)}")
        sys.exit(1)
    COMMANDS[cmd](sys.argv[2:])


if __name__ == "__main__":
    main()
