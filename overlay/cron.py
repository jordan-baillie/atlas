"""overlay/cron.py — Daily cron entry point for the AI overlay module.

Called from pi-cron.sh or a systemd timer every market day before plan
generation.  Orchestrates the overlay engine, logs the decision, and
(optionally) surfaces it to plan.py integration.

Usage
-----
    # Log-only mode (default — safe, no plan impact)
    python3 -m overlay.cron

    # Active mode — decision returned for plan.py wiring
    python3 -m overlay.cron --mode active

    # Weekly self-evaluation
    python3 -m overlay.cron --evaluate

    # Evaluate last N days
    python3 -m overlay.cron --evaluate --days 14
"""
from __future__ import annotations

import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Ensure project root is importable
_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


# ── Daily runner ──────────────────────────────────────────────────────────────

def run_daily_overlay(mode: str = "log_only") -> Optional[dict]:
    """Run the AI overlay engine and record the decision.

    Parameters
    ----------
    mode : 'log_only' | 'active'
        * ``log_only`` — execute the overlay pipeline and write the decision to
          the DB, but do NOT feed the result into plan generation.  Safe during
          the 2-week validation window (Phase 4 spec).
        * ``active``   — return the decision dict so that plan.py can apply it
          when building today's trade plan.

    Returns
    -------
    dict | None
        The overlay decision dict when ``mode='active'``.
        None when ``mode='log_only'``.
    """
    logger.info("overlay cron: starting daily run (mode=%s)", mode)
    start_ts = datetime.now().isoformat()

    try:
        from overlay.engine import run_overlay  # type: ignore
    except ImportError as exc:
        logger.error(
            "overlay cron: cannot import overlay.engine — is it deployed? (%s)", exc
        )
        # Fail gracefully so cron does not abort the rest of the daily pipeline
        return None

    try:
        decision = run_overlay(mode=mode)
    except Exception as exc:
        logger.error("overlay cron: run_overlay raised %s: %s", type(exc).__name__, exc)
        return None

    # Always log the result
    action = ("tighten" if decision.adjust else "no_change") if decision else "error"
    sizing = decision.sizing_multiplier_override if decision else None
    logger.info(
        "overlay cron: decision action=%s sizing_override=%s (elapsed %s)",
        action,
        sizing,
        _elapsed(start_ts),
    )

    if mode == "log_only":
        logger.info("overlay cron: log_only mode — decision recorded, not applied to plan")
        return None

    # mode == 'active'
    logger.info("overlay cron: active mode — returning decision for plan integration")
    return decision


# ── Helpers ──────────────────────────────────────────────────────────────────

def _elapsed(start_iso: str) -> str:
    """Return a human-readable elapsed time string."""
    try:
        delta = datetime.now() - datetime.fromisoformat(start_iso)
        secs = delta.total_seconds()
        return f"{secs:.1f}s"
    except Exception:
        return "?"


# ── CLI entry point ───────────────────────────────────────────────────────────

def _main() -> None:
    """Parse CLI arguments and dispatch to the appropriate function."""
    import argparse

    from utils.logging_config import setup_logging  # type: ignore
    from db.atlas_db import init_db  # type: ignore

    setup_logging()
    init_db()  # Ensure schema is current (idempotent)

    parser = argparse.ArgumentParser(
        description="Atlas AI overlay daily runner / weekly evaluator",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--mode",
        default="log_only",
        choices=["log_only", "active"],
        help="log_only: record decision only; active: feed into plan generation",
    )
    parser.add_argument(
        "--evaluate",
        action="store_true",
        help="Run weekly self-evaluation instead of the daily overlay",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=7,
        help="Number of days to evaluate (used with --evaluate)",
    )
    args = parser.parse_args()

    if args.evaluate:
        from overlay.evaluator import evaluate_and_report  # type: ignore

        stats = evaluate_and_report(days=args.days)
        print("\n=== Overlay Evaluation Results ===")
        for key, value in stats.items():
            print(f"  {key}: {value}")
        print()
    else:
        decision = run_daily_overlay(mode=args.mode)
        if decision:
            print("\n=== Overlay Decision (active mode) ===")
            import json
            print(json.dumps(decision, indent=2, default=str))
            print()
        else:
            print("Overlay run complete (log_only or no decision returned).")


if __name__ == "__main__":
    _main()
