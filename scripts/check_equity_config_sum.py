#!/usr/bin/env python3
"""Equity-sum config guard — RCA latent #6 closure.

Asserts: Σ(active_configs.risk.starting_equity) ≤ broker.equity × 1.05

This is a KEEP-LOUD guard.  Config drift (per-market equity claims exceed
real broker capital) causes inflated position sizes, wrong drawdown percentages,
and incorrect risk limit evaluations.

Exit codes:
    0  — constraint satisfied (or broker equity unavailable — UNKNOWN)
    1  — constraint VIOLATED — Telegram alert sent
    2  — unexpected error

Usage:
    python3 scripts/check_equity_config_sum.py
    python3 scripts/check_equity_config_sum.py --dry-run
    python3 scripts/check_equity_config_sum.py --tolerance 0.10
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from scripts.health_check import check_equity_config_sum  # type: ignore


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--tolerance",
        type=float,
        default=0.05,
        help="Fractional overage allowed (default 0.05 = 5%%)",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Print Telegram alert text without sending it",
    )
    p.add_argument(
        "--db-path",
        default=None,
        help="Override DB path (default: data/atlas.db)",
    )
    p.add_argument(
        "--config-dir",
        default=None,
        help="Override config dir (default: config/active)",
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    db_path = Path(args.db_path) if args.db_path else None
    config_dir = Path(args.config_dir) if args.config_dir else None

    try:
        ok, info = check_equity_config_sum(
            config_dir=config_dir,
            db_path=db_path,
            tolerance=args.tolerance,
            dry_run=args.dry_run,
        )
    except Exception as exc:
        print(f"ERROR: check_equity_config_sum raised unexpectedly: {exc}", file=sys.stderr)
        return 2

    # Always print summary regardless of status
    status = info.get("status", "UNKNOWN")
    equity_sum = info.get("equity_sum", 0.0)
    broker_equity = info.get("broker_equity")
    active_markets = info.get("active_markets", {})

    print(f"=== Equity Config Sum Guard (RCA latent #6) ===")
    print(f"Status: {status}")
    print(f"Σ(starting_equity): ${equity_sum:,.2f}")
    if broker_equity is not None:
        limit = info.get("limit", broker_equity * (1 + args.tolerance))
        print(f"Broker equity (last snapshot): ${broker_equity:,.2f}")
        print(f"Limit ({1+args.tolerance:.0%}): ${limit:,.2f}")
        if not ok:
            print(f"Exceeded by: ${info.get('violated_by', 0.0):,.2f}  ← VIOLATION")
    else:
        print("Broker equity: unavailable (market_equity_history empty)")

    print("\nActive markets:")
    for market, value in sorted(active_markets.items()):
        print(f"  {market}: ${value:,.2f}")

    if status == "VIOLATION":
        print(
            "\n⚠️  VIOLATION detected — Telegram alert sent "
            "(use --dry-run to suppress)."
        )
        return 1

    print(f"\n✓ Constraint satisfied.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
