#!/usr/bin/env python3
"""Allocation Pool Comparison Backtest.

Compares:
  Scenario A: Current behavior (no allocation pools, max_positions=15)
  Scenario B: Per-strategy hard-pool caps of 5 each (max_positions=15)

Writes results to journal/allocation_research.md
"""
import sys
import os
import json
import copy
import logging
from pathlib import Path
from datetime import datetime

# Ensure project root is on path
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# When running in a swarm worktree, data may live in the main repo's cache.
# We transparently fall back to /root/atlas/data/cache if the worktree cache is empty.
MAIN_REPO_CACHE = Path("/root/atlas/data/cache")

from utils.config import get_active_config
from backtest.engine import BacktestEngine
from scripts.cli import get_strategies, load_data

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("allocation_comparison")


def run_scenario(label: str, config: dict, data: dict) -> dict:
    """Run a single backtest scenario and return metrics + strategy breakdown."""
    print(f"\n  Running: {label} ...")
    strategies = get_strategies(config)
    strat_names = [s.name for s in strategies]
    print(f"  Strategies: {strat_names}")
    alloc = config.get("allocation", {})
    if alloc.get("enabled"):
        print(f"  Allocation: {alloc.get('mode')}, pools={alloc.get('pools')}")
    else:
        print(f"  Allocation: disabled (global cap only)")

    engine = BacktestEngine(config, market_id="sp500")
    result = engine.run_walkforward(data, strategies)

    metrics = result.metrics
    trades = result.trades

    # Per-strategy breakdown
    by_strategy: dict[str, dict] = {}
    for t in trades:
        s = t.get("strategy", "unknown")
        if s not in by_strategy:
            by_strategy[s] = {"trades": 0, "pnl": 0.0, "wins": 0, "losses": 0}
        by_strategy[s]["trades"] += 1
        pnl = t.get("pnl", 0.0)
        by_strategy[s]["pnl"] += pnl
        if pnl >= 0:
            by_strategy[s]["wins"] += 1
        else:
            by_strategy[s]["losses"] += 1

    for s, d in by_strategy.items():
        n = d["trades"]
        d["win_rate"] = round(d["wins"] / n * 100, 1) if n > 0 else 0.0
        d["pnl"] = round(d["pnl"], 2)
        d["pct_of_trades"] = round(n / len(trades) * 100, 1) if trades else 0.0

    return {
        "label": label,
        "metrics": metrics,
        "total_trades": len(trades),
        "by_strategy": dict(sorted(by_strategy.items(), key=lambda x: -x[1]["trades"])),
    }


def format_results_md(results: list[dict], bench: dict) -> str:
    """Format comparison results as Markdown."""
    lines = []
    lines.append("# Allocation Pool Research — SP500 Comparison")
    lines.append(f"\n*Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}*\n")
    lines.append("## Summary\n")

    # Table header
    cols = ["Scenario", "Sharpe", "CAGR %", "MaxDD %", "Trades", "Win Rate %", "Profit Factor"]
    lines.append("| " + " | ".join(cols) + " |")
    lines.append("| " + " | ".join(["---"] * len(cols)) + " |")

    for r in results:
        m = r["metrics"]
        row = [
            r["label"],
            f"{m.get('sharpe', 0):.3f}",
            f"{m.get('cagr', 0)*100:+.2f}",
            f"{m.get('max_drawdown', 0)*100:.2f}",
            str(r["total_trades"]),
            f"{m.get('win_rate', 0)*100:.1f}",
            f"{m.get('profit_factor', 0):.2f}",
        ]
        lines.append("| " + " | ".join(row) + " |")

    # Benchmark
    if bench:
        row = [
            "Benchmark (SPY B&H)",
            f"{bench.get('sharpe', 0):.3f}",
            f"{bench.get('cagr', 0)*100:+.2f}",
            f"{bench.get('max_drawdown', 0)*100:.2f}",
            "N/A", "N/A", "N/A",
        ]
        lines.append("| " + " | ".join(row) + " |")

    lines.append("")

    # Per-strategy breakdown for each scenario
    for r in results:
        lines.append(f"\n## {r['label']} — Per-Strategy Breakdown\n")
        lines.append("| Strategy | Trades | % of Total | PnL $ | Win Rate % |")
        lines.append("| --- | --- | --- | --- | --- |")
        for strat, d in r["by_strategy"].items():
            lines.append(
                f"| {strat} | {d['trades']} | {d['pct_of_trades']}% | "
                f"${d['pnl']:,.2f} | {d['win_rate']}% |"
            )

    lines.append("")
    lines.append("\n## Configuration Details\n")
    for r in results:
        lines.append(f"### {r['label']}")
        m = r["metrics"]
        lines.append(f"- Sharpe: {m.get('sharpe', 0):.4f}")
        lines.append(f"- CAGR: {m.get('cagr', 0)*100:+.4f}%")
        lines.append(f"- Max Drawdown: {m.get('max_drawdown', 0)*100:.4f}%")
        lines.append(f"- Sortino: {m.get('sortino', 0):.4f}")
        lines.append(f"- Profit Factor: {m.get('profit_factor', 0):.4f}")
        lines.append(f"- Expectancy R: {m.get('expectancy_r', 0):+.4f}")
        lines.append(f"- Total Trades: {r['total_trades']}")
        lines.append(f"- Win Rate: {m.get('win_rate', 0)*100:.2f}%")
        lines.append("")

    lines.append("\n## Methodology\n")
    lines.append("Walk-forward backtest, 3-year SP500 data, train=252/test=63/step=21.")
    lines.append("Scenario A uses current production config (no allocation).")
    lines.append("Scenario B adds `allocation.enabled=true` with hard_pool, 5 slots per strategy.")
    lines.append("")
    lines.append("**Allocation config for Scenario B:**")
    lines.append("```json")
    lines.append(json.dumps({
        "enabled": True,
        "mode": "hard_pool",
        "pools": {
            "trend_following": {"max_positions": 5},
            "mean_reversion": {"max_positions": 5},
            "opening_gap": {"max_positions": 5},
            "_other": {"max_positions": 2},
        }
    }, indent=2))
    lines.append("```")
    lines.append("")

    return "\n".join(lines)


def main():
    market_id = "sp500"
    # Limit tickers for faster comparison (top liquid SP500 names)
    COMPARISON_TICKERS = [
        "AAPL", "MSFT", "AMZN", "NVDA", "GOOGL", "META", "TSLA", "AMD",
        "JPM", "BAC", "WFC", "GS", "MS", "C", "V", "MA",
        "UNH", "JNJ", "PFE", "ABT", "MRK", "ABBV", "LLY", "MDT",
        "XOM", "CVX", "COP", "SLB", "HAL", "EOG", "VLO", "MPC",
        "COST", "WMT", "HD", "LOW", "TGT", "TJX",
        "BA", "LMT", "RTX", "NOC", "GD",
        "ALB", "NEM", "FCX", "F", "GM",
        "NFLX", "DIS", "CMCSA",
        "DHI", "LEN", "DHR", "BMY", "GILD", "WBD",
    ]
    print("=" * 60)
    print("ALLOCATION POOL COMPARISON BACKTEST")
    print("=" * 60)

    # Load config + data
    config = get_active_config(market_id)
    print(f"\nConfig version: {config.get('version')}")
    print(f"Max positions: {config['risk']['max_open_positions']}")
    print(f"Enabled strategies: {[k for k, v in config['strategies'].items() if v.get('enabled')]}")

    import pandas as pd
    print(f"Loading data for {len(COMPARISON_TICKERS)} tickers (fast subset)...")
    data = {}
    market_cache = MAIN_REPO_CACHE / market_id
    local_cache = PROJECT_ROOT / config["data"]["cache_dir"] / market_id
    for ticker in COMPARISON_TICKERS:
        fname = ticker.replace(".", "_") + ".parquet"
        for cache_dir in [local_cache, market_cache]:
            path = cache_dir / fname
            if path.exists():
                data[ticker] = pd.read_parquet(path)
                break
    if not data:
        print("ERROR: No cached data. Run 'python3 scripts/cli.py ingest --market sp500' first.")
        sys.exit(1)
    print(f"Data loaded: {len(data)} tickers")

    # ── Scenario A: no allocation ─────────────────────────────
    config_a = copy.deepcopy(config)
    config_a["allocation"] = {"enabled": False}
    result_a = run_scenario("A: No Allocation (current)", config_a, data)

    # ── Scenario B: hard-pool, 5 per strategy ────────────────
    config_b = copy.deepcopy(config)
    config_b["allocation"] = {
        "enabled": True,
        "mode": "hard_pool",
        "overflow_enabled": True,
        "pools": {
            "trend_following":  {"max_positions": 5},
            "mean_reversion":   {"max_positions": 5},
            "opening_gap":      {"max_positions": 5},
            "_other":           {"max_positions": 2},
        }
    }
    result_b = run_scenario("B: Hard Pool (5 per strategy)", config_b, data)

    # ── Scenario C: soft-pool, 5 per strategy + 2 overflow ───
    config_c = copy.deepcopy(config)
    config_c["allocation"] = {
        "enabled": True,
        "mode": "soft_pool",
        "overflow_enabled": True,
        "pools": {
            "trend_following":  {"max_positions": 5},
            "mean_reversion":   {"max_positions": 5},
            "opening_gap":      {"max_positions": 5},
            "_other":           {"max_positions": 3},
        }
    }
    result_c = run_scenario("C: Soft Pool (5 + 3 overflow)", config_c, data)

    # Collect benchmark from one of the results
    from backtest.engine import BacktestEngine
    bench = result_a["metrics"].get("benchmark_metrics", {})
    # Try to get benchmark from the result object
    results = [result_a, result_b, result_c]

    # Print summary
    print("\n" + "=" * 60)
    print("RESULTS SUMMARY")
    print("=" * 60)
    fmt = "{:<35} {:>8} {:>8} {:>8} {:>8}"
    print(fmt.format("Scenario", "Sharpe", "CAGR%", "MaxDD%", "Trades"))
    print("-" * 60)
    for r in results:
        m = r["metrics"]
        print(fmt.format(
            r["label"][:35],
            f"{m.get('sharpe', 0):.3f}",
            f"{m.get('cagr', 0)*100:+.1f}%",
            f"{m.get('max_drawdown', 0)*100:.1f}%",
            str(r["total_trades"]),
        ))

    print("\nPer-strategy trade counts:")
    all_strategies = set()
    for r in results:
        all_strategies.update(r["by_strategy"].keys())

    fmt2 = "{:<25} " + " {:>20}" * len(results)
    hdr_row = [f"{r['label'][:18]} (trades)" for r in results]
    print(fmt2.format("Strategy", *hdr_row))
    print("-" * (25 + 21 * len(results)))
    for s in sorted(all_strategies):
        vals = []
        for r in results:
            d = r["by_strategy"].get(s, {})
            if d:
                vals.append(f"{d['trades']} ({d['pct_of_trades']}%)")
            else:
                vals.append("-")
        print(fmt2.format(s[:25], *vals))

    # Write markdown report
    # Use benchmark from results if captured
    md = format_results_md(results, bench)
    journal_dir = PROJECT_ROOT / "journal"
    journal_dir.mkdir(exist_ok=True)
    out_path = journal_dir / "allocation_research.md"
    with open(out_path, "w") as f:
        f.write(md)
    print(f"\nReport written: {out_path}")

    # Also save raw JSON
    raw_path = journal_dir / "allocation_research.json"
    with open(raw_path, "w") as f:
        json.dump(
            [
                {
                    "label": r["label"],
                    "metrics": {k: float(v) if isinstance(v, (int, float)) else v
                                for k, v in r["metrics"].items()
                                if not isinstance(v, dict)},
                    "total_trades": r["total_trades"],
                    "by_strategy": r["by_strategy"],
                }
                for r in results
            ],
            f, indent=2, default=str,
        )
    print(f"Raw data: {raw_path}")


if __name__ == "__main__":
    main()
