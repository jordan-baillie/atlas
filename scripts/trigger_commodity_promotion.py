#!/usr/bin/env python3
"""One-shot manual trigger for commodity_etfs/momentum_breakout promotion.

Uses the auto_promote pipeline — goes through the full gate flow (cooldown,
regression, sanity, OOS) and queues a Telegram APPROVE/REJECT message.

Does NOT bypass any gate.  Does NOT auto-apply.  This ONLY queues the
Telegram approval request.

Usage:
    python3 scripts/trigger_commodity_promotion.py          # dry-run (default)
    python3 scripts/trigger_commodity_promotion.py --apply  # fire auto_promote

NOTE on _run_promotion_sweep coverage (D4 investigation):
    autoresearch_nightly.py's _run_promotion_sweep() accepts any market/universe
    and does NOT hardcode sp500.  HOWEVER, the cron entry in pi-cron.sh
    (line 611) is COMMENTED OUT and defaults to --market sp500 --universe sp500.
    There is NO separate cron entry for commodity_etfs.  To add nightly coverage
    you would need a separate cron line such as:
        python3 research/autoresearch_nightly.py \\
            --market commodity_etfs --universe commodity_etfs \\
            --strategies momentum_breakout --hours 4 --workers 1 --notify
    This is a confirmed blocker for automated commodity_etfs promotion via the
    nightly sweep.  This manual trigger script is the workaround.

auto_promote() signature (from research/promoter.py):
    auto_promote(
        strategy: str,
        improved_params: dict,
        initial_sharpe: float,   # ← spec called this "baseline_sharpe"
        final_sharpe: float,     # ← spec called this "improved_sharpe"
        improvements: list,
        market: str = "sp500",
    ) -> dict
"""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
from pathlib import Path

ATLAS_ROOT = Path(__file__).resolve().parent.parent
if str(ATLAS_ROOT) not in sys.path:
    sys.path.insert(0, str(ATLAS_ROOT))

TARGET_STRATEGY = "momentum_breakout"
TARGET_UNIVERSE = "commodity_etfs"
TARGET_MARKET = "commodity_etfs"

DB_PATH = ATLAS_ROOT / "data" / "atlas.db"

logger = logging.getLogger(__name__)


# ─── Logging ──────────────────────────────────────────────────────────────────

def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [trigger_promo] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


# ─── Research best lookup ─────────────────────────────────────────────────────

def _load_research_row(db_path: Path, strategy: str, universe: str) -> dict | None:
    """Load a single (strategy, universe) row from research_best."""
    with sqlite3.connect(str(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.execute(
            "SELECT strategy, universe, params, sharpe, trades, max_dd_pct, updated_at "
            "FROM research_best WHERE strategy = ? AND universe = ?",
            (strategy, universe),
        )
        row = cursor.fetchone()
        return dict(row) if row else None


def _parse_params(row: dict) -> dict:
    """Parse params JSON from research_best row."""
    params_raw = row.get("params") or "{}"
    try:
        result = json.loads(params_raw)
        return result if isinstance(result, dict) else {}
    except (json.JSONDecodeError, TypeError):
        logger.warning("Could not parse params: %r", str(params_raw)[:80])
        return {}


# ─── Config baseline lookup ───────────────────────────────────────────────────

def _load_config_baseline_sharpe(market: str, strategy: str) -> float | None:
    """Return the Sharpe baseline for a strategy from the active config.

    Returns None if the config has no sharpe baseline for this strategy.
    """
    try:
        from utils.config import get_active_config  # noqa: PLC0415
        config = get_active_config(market)
    except Exception as exc:
        logger.warning("Could not load active config for %s: %s", market, exc)
        return None

    # Check baselines section
    baselines = config.get("baselines") or {}
    if strategy in baselines:
        bs = baselines[strategy]
        if isinstance(bs, dict) and "sharpe" in bs:
            return float(bs["sharpe"])
        if isinstance(bs, (int, float)):
            return float(bs)

    # Check strategy entry
    strat_cfg = (config.get("strategies") or {}).get(strategy)
    if isinstance(strat_cfg, dict) and "sharpe" in strat_cfg:
        return float(strat_cfg["sharpe"])

    return None


# ─── Core trigger ─────────────────────────────────────────────────────────────

def _dry_run_report(
    research_row: dict | None,
    config_baseline: float | None,
) -> None:
    """Print what would happen without firing auto_promote."""
    print("=" * 65)
    print(f"  DRY-RUN: trigger_commodity_promotion.py")
    print(f"  Target: {TARGET_STRATEGY}/{TARGET_UNIVERSE} → market={TARGET_MARKET}")
    print("=" * 65)

    if research_row is None:
        print(
            f"\n  ❌ No row found in research_best for "
            f"strategy={TARGET_STRATEGY}, universe={TARGET_UNIVERSE}"
        )
        print("  Cannot trigger promotion — run research for commodity_etfs first.")
        return

    params = _parse_params(research_row)
    sharpe_research = float(research_row.get("sharpe") or 0.0)
    trades = int(research_row.get("trades") or 0)
    updated_at = research_row.get("updated_at", "unknown")

    print(f"\n  Research best row:")
    print(f"    Sharpe:     {sharpe_research:.4f}")
    print(f"    Trades:     {trades}")
    print(f"    Updated at: {updated_at}")
    print(f"    Params:     {json.dumps(params, indent=6)}")

    if config_baseline is not None:
        delta = sharpe_research - config_baseline
        print(f"\n  Config baseline Sharpe: {config_baseline:.4f}")
        print(f"  Delta:                  {delta:+.4f}")
        if delta >= 0.05:
            print("  ✅ Delta >= 0.05 — would pass client gate and call auto_promote()")
        else:
            print("  ⚠️  Delta < 0.05 — would be blocked by client gate")
    else:
        print("\n  Config baseline Sharpe: (none found)")
        print(
            "  Will use research_best.sharpe as initial_sharpe=0.0 "
            "(conservative: all delta goes to final_sharpe)"
        )
        print("  ✅ Would call auto_promote() regardless (no baseline gate)")

    print("\n  Gates that auto_promote() will apply:")
    print("    Gate 1: 24h cooldown per strategy")
    print("    Gate 2: Regression check (candidate vs active portfolio backtest)")
    print("    Gate 3: Sanity bounds (Sharpe>0, CAGR>0, ≥20 trades)")
    print("    Gate 4: OOS validation (time-split + perturbation robustness)")
    print("\n  Re-run with --apply to fire the promotion pipeline.")
    print("=" * 65)


def trigger_promotion(db_path: Path = DB_PATH, apply: bool = False) -> int:
    """Load research_best row and trigger auto_promote for commodity_etfs.

    Args:
        db_path: Path to atlas.db.
        apply:   If False (default), dry-run only.

    Returns:
        Exit code (0 = success / queued, 1 = error / blocked).
    """
    research_row = _load_research_row(db_path, TARGET_STRATEGY, TARGET_UNIVERSE)
    config_baseline = _load_config_baseline_sharpe(TARGET_MARKET, TARGET_STRATEGY)

    if not apply:
        _dry_run_report(research_row, config_baseline)
        return 0

    # ── Apply mode ────────────────────────────────────────────────────────────
    if research_row is None:
        logger.error(
            "No row found in research_best for strategy=%s universe=%s — cannot promote",
            TARGET_STRATEGY,
            TARGET_UNIVERSE,
        )
        return 1

    params = _parse_params(research_row)
    sharpe_research = float(research_row.get("sharpe") or 0.0)
    updated_at = research_row.get("updated_at", "unknown")

    # Determine initial_sharpe (baseline):
    #   If config has a sharpe baseline, use it.
    #   Otherwise default to 0.0 so all improvement shows as delta.
    if config_baseline is not None:
        initial_sharpe = config_baseline
        delta = sharpe_research - config_baseline
    else:
        initial_sharpe = 0.0
        delta = sharpe_research

    logger.info(
        "Triggering promotion: strategy=%s market=%s sharpe=%.4f delta=+%.4f",
        TARGET_STRATEGY,
        TARGET_MARKET,
        sharpe_research,
        delta,
    )

    # Client-side gate — mirror autoresearch_nightly's 0.05 gate
    if delta < 0.05 and config_baseline is not None:
        logger.error(
            "Delta %.4f below client gate 0.05 — promotion not triggered. "
            "Ensure research_best has a better Sharpe than the config baseline.",
            delta,
        )
        print(f"[trigger] BLOCKED: delta={delta:+.4f} is below client gate 0.05")
        return 1

    improvements = [
        f"commodity_etfs promotion trigger: Sharpe {initial_sharpe:.4f} → {sharpe_research:.4f}",
        f"(research_best updated {updated_at})",
    ]

    try:
        from research.promoter import auto_promote  # noqa: PLC0415
    except ImportError as exc:
        logger.error("Cannot import research.promoter: %s", exc)
        return 1

    try:
        result = auto_promote(
            strategy=TARGET_STRATEGY,
            improved_params=params,
            initial_sharpe=float(initial_sharpe),
            final_sharpe=float(sharpe_research),
            improvements=improvements,
            market=TARGET_MARKET,
        )
    except Exception as exc:
        logger.error("auto_promote() raised an exception: %s", exc)
        return 1

    # Report outcome
    pending = result.get("pending", False)
    promoted = result.get("promoted", False)
    reason = result.get("reason", "no reason given")
    pending_id = result.get("pending_id", "")

    if pending:
        print(
            f"[trigger] ✅ ALL GATES PASSED — Telegram approval queued\n"
            f"  pending_id: {pending_id}\n"
            f"  reason:     {reason}"
        )
        return 0
    elif promoted:
        print(
            f"[trigger] ✅ PROMOTED (auto-approved)\n"
            f"  version: {result.get('version')}\n"
            f"  reason:  {reason}"
        )
        return 0
    else:
        print(
            f"[trigger] ⚠️  BLOCKED\n"
            f"  reason: {reason}"
        )
        return 1


# ─── CLI ─────────────────────────────────────────────────────────────────────

def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Manual promotion trigger for commodity_etfs/momentum_breakout. "
            "Dry-run by default — use --apply to fire auto_promote()."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python3 scripts/trigger_commodity_promotion.py           # dry-run\n"
            "  python3 scripts/trigger_commodity_promotion.py --apply   # trigger\n"
        ),
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        default=False,
        help="Actually fire auto_promote() (default: dry-run only)",
    )
    parser.add_argument(
        "--db",
        default=str(DB_PATH),
        help=f"SQLite DB path (default: {DB_PATH})",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    _setup_logging()

    db_path = Path(args.db)
    if not db_path.exists():
        logger.error("Database not found: %s", db_path)
        return 1

    return trigger_promotion(db_path=db_path, apply=args.apply)


if __name__ == "__main__":
    sys.exit(main())
