#!/usr/bin/env python3
"""Atlas Optimizer Promote — review and apply optimized strategy weights.

Usage:
    python3 scripts/optimizer_promote.py                      # dry-run: show current vs proposed
    python3 scripts/optimizer_promote.py --market sp500       # explicit market
    python3 scripts/optimizer_promote.py --apply              # apply changes to live config
"""

import argparse
import json
import logging
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from utils.config import get_active_config, save_config_version

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("optimizer_promote")


def _bump_version(version: str) -> str:
    """Bump micro version: 'v3.2' → 'v3.2.1', 'v3.2.1' → 'v3.2.2'."""
    # Strip leading 'v'
    v = version.lstrip("v")
    parts = v.split(".")
    if len(parts) == 2:
        # e.g. "3.2" → "3.2.1"
        return f"v{parts[0]}.{parts[1]}.1"
    elif len(parts) >= 3:
        # e.g. "3.2.1" → "3.2.2"
        try:
            micro = int(parts[2]) + 1
        except ValueError:
            micro = 1
        return f"v{'.'.join(parts[:2])}.{micro}"
    else:
        return f"v{v}.1"


def _display_comparison(current_weights: dict, optimal_weights: dict) -> None:
    """Print a formatted side-by-side comparison table."""
    all_strategies = sorted(
        set(list(current_weights.keys()) + list(optimal_weights.keys()))
    )
    header = f"{'Strategy':<28} {'Current':>9} {'Proposed':>10} {'Delta':>8}"
    sep = "─" * len(header)
    print()
    print(header)
    print(sep)
    for name in all_strategies:
        cur = current_weights.get(name, 0.0)
        prop = optimal_weights.get(name, 0.0)
        delta = prop - cur
        delta_str = f"{delta * 100:+.1f}%" if delta != 0 else "    —"
        print(
            f"{name:<28} {cur * 100:>8.1f}% {prop * 100:>9.1f}%  {delta_str:>7}"
        )
    print()


def _display_portfolio_metrics(metrics: dict) -> None:
    """Print portfolio metrics block."""
    print("Portfolio Metrics:")
    field_map = [
        ("analytic_sharpe",          "Analytic Sharpe"),
        ("simulated_sharpe",         "Simulated Sharpe"),
        ("n_strategies",             "Strategies"),
        ("avg_correlation",          "Avg Correlation"),
        ("portfolio_annual_return",  "Annual Return"),
        ("portfolio_annual_vol",     "Annual Vol"),
        ("portfolio_max_drawdown",   "Max Drawdown"),
    ]
    for key, label in field_map:
        val = metrics.get(key)
        if val is None:
            continue
        if isinstance(val, float):
            print(f"  {label:<26} {val:.4f}")
        else:
            print(f"  {label:<26} {val}")
    print()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Review and apply optimized strategy weights to the live config."
    )
    parser.add_argument("--market", default="sp500", help="Market ID (default: sp500)")
    parser.add_argument(
        "--apply",
        action="store_true",
        default=False,
        help="Apply proposed weights to live config",
    )
    parser.add_argument(
        "--zero-commission",
        action="store_true",
        default=False,
        help="Pass zero_commission=True to PortfolioOptimizer",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=6,
        help="Parallel backtest workers (default: 6)",
    )
    args = parser.parse_args()

    # ── Step 1: Load current config ───────────────────────────────────────────
    logger.info("Loading config for market: %s", args.market)
    config = get_active_config(args.market)

    current_weights = {
        name: float(cfg.get("weight", 0.0))
        for name, cfg in config.get("strategies", {}).items()
        if cfg.get("enabled")
    }
    logger.info("Found %d enabled strategies", len(current_weights))

    # ── Step 2: Run optimizer ──────────────────────────────────────────────────
    logger.info(
        "Running PortfolioOptimizer (workers=%d, zero_commission=%s)...",
        args.workers,
        args.zero_commission,
    )
    from research.portfolio_optimizer import PortfolioOptimizer

    opt = PortfolioOptimizer(
        market=args.market,
        zero_commission=args.zero_commission,
        max_workers=args.workers,
    )
    result = opt.run()

    # ── Step 3: Check for errors ───────────────────────────────────────────────
    if "error" in result:
        logger.error("Optimizer failed: %s", result["error"])
        sys.exit(1)

    optimal_weights: dict = result.get("optimal_weights", {})
    portfolio_metrics: dict = result.get("portfolio_metrics", {})

    if not optimal_weights:
        logger.error("Optimizer returned no weights — aborting.")
        sys.exit(1)

    # ── Step 4: Display comparison ─────────────────────────────────────────────
    _display_comparison(current_weights, optimal_weights)
    _display_portfolio_metrics(portfolio_metrics)

    n_analyzed = result.get("n_strategies_analyzed", "?")
    n_active = result.get("n_strategies_active", "?")
    print(f"Strategies analyzed: {n_analyzed}  |  Active in proposed: {n_active}")
    print()

    # ── Step 5: Dry run exit ───────────────────────────────────────────────────
    if not args.apply:
        print("Dry run — use --apply to update config")
        return

    # ── Step 6: Apply weights ──────────────────────────────────────────────────
    config_path = Path(__file__).resolve().parent.parent / "config" / "active" / f"{args.market}.json"
    backup_path = config_path.with_suffix(".json.bak")

    logger.info("Creating backup: %s", backup_path)
    shutil.copy2(str(config_path), str(backup_path))

    max_open = config.get("risk", {}).get("max_open_positions", 10)
    strategies_cfg = config.get("strategies", {})
    pools_cfg = config.get("allocation", {}).get("pools", {})

    # 6a. Update strategy weights
    for name, cfg in strategies_cfg.items():
        if not cfg.get("enabled"):
            continue
        if name in optimal_weights:
            cfg["weight"] = round(optimal_weights[name], 6)
        # strategies not in optimizer results → keep current weight (no change)

    # 6b–d. Update allocation pool max_positions and weights
    for pool_name in list(pools_cfg.keys()):
        if pool_name == "_other":
            # Keep _other pool unchanged per spec
            continue
        new_weight = optimal_weights.get(pool_name)
        if new_weight is None:
            # pool not in optimizer results → skip
            continue
        pool = pools_cfg[pool_name]
        pool["max_positions"] = max(1, round(new_weight * max_open))
        pool["weight"] = round(new_weight, 6)

    # 6e. Version bump
    current_version = config.get("version", "v1.0")
    new_version = _bump_version(current_version)
    config["version"] = new_version

    # 6f. Save
    logger.info("Saving config as version %s", new_version)
    save_config_version(config, version=new_version, market_id=args.market)

    print(
        f"✅ Config updated to {new_version}. "
        f"Backup: config/active/{args.market}.json.bak"
    )


if __name__ == "__main__":
    main()
