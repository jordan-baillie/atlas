"""Single per-market orchestrator (Phase C.3).

Currently runs in SHADOW MODE: logs what it would have done. Cron remains
authoritative until 7+ days of zero-divergence shadow alignment.

Design: docs/phase-c-orchestrator-timer.md
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

ATLAS_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ATLAS_ROOT))

logger = logging.getLogger("atlas.orchestrator")

# ─── Step registry ─────────────────────────────────────────────────────────
# Each step is (name, description, callable). Keep simple — no real DAG yet.

STEPS_PER_MARKET = (
    ("sync_broker_orders", "Refresh broker_orders fill-price oracle"),
    ("reconcile_fills", "Fold fills into trades table"),
    ("sync_protective", "Place/refresh protective stops"),
    ("healthz", "Emit health snapshot"),
)

ACTIVE_MARKETS = ("sp500", "commodity_etfs", "sector_etfs")


def _shadow_log(market: str, step: str, message: str) -> None:
    """Log what the orchestrator WOULD have done (shadow mode)."""
    logger.info("SHADOW market=%s step=%s %s", market, step, message)


def run_cycle(
    markets: tuple[str, ...] = ACTIVE_MARKETS,
    shadow: bool = True,
    now: Optional[datetime] = None,
) -> dict:
    """Run one supervisor cycle. Return summary dict.

    In shadow mode, this only emits log lines. Cron still drives the real work.
    """
    now = now or datetime.now(timezone.utc)
    summary = {
        "started_at": now.isoformat(),
        "shadow": shadow,
        "markets": {},
    }
    for mkt in markets:
        steps_run = []
        for step_name, step_desc in STEPS_PER_MARKET:
            if shadow:
                _shadow_log(mkt, step_name, f"would run: {step_desc}")
                steps_run.append({"step": step_name, "outcome": "shadow"})
            else:
                # NOT YET IMPLEMENTED — real dispatch goes here post-cutover
                raise NotImplementedError(f"Real-mode dispatch for {step_name} not yet wired")
        summary["markets"][mkt] = {"steps": steps_run}
    summary["finished_at"] = datetime.now(timezone.utc).isoformat()
    logger.info("Orchestrator cycle complete: %s", json.dumps(summary, indent=2))
    return summary


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Atlas orchestrator (Phase C.3 — shadow)")
    parser.add_argument("--once", action="store_true", help="Run one cycle and exit")
    parser.add_argument(
        "--shadow",
        action="store_true",
        default=True,
        help="Shadow mode (default): log only, do not invoke real steps",
    )
    parser.add_argument(
        "--no-shadow",
        dest="shadow",
        action="store_false",
        help="Disable shadow mode (NotImplementedError until cutover)",
    )
    parser.add_argument("--market", action="append", help="Limit to specific market(s)")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    markets = tuple(args.market) if args.market else ACTIVE_MARKETS
    summary = run_cycle(markets=markets, shadow=args.shadow)
    return 0 if all("error" not in m for m in summary["markets"].values()) else 1


if __name__ == "__main__":
    sys.exit(main())
