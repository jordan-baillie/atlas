#!/usr/bin/env python3
"""Atlas nightly risk pre-computation script.

Computes and caches three risk artefacts:
  1. Portfolio VaR/CVaR  -> portfolio_risk table
  2. Regime transition matrix (last 90 days) -> regime_transitions_cache table
  3. Ruin probability (MC simulation)        -> ruin_probability table

Usage::

    python3 scripts/precompute_risk.py --target=all       # default
    python3 scripts/precompute_risk.py --target=risk
    python3 scripts/precompute_risk.py --target=regime
    python3 scripts/precompute_risk.py --target=ruin

Invoked nightly by atlas-risk-precompute.timer (22:30 UTC).
Also triggered on-demand via POST /api/risk/ruin/refresh and when
/api/positions/risk or /api/regime/transitions serve stale cache.
"""
from __future__ import annotations

import argparse
import os
import json
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# Path setup (identical pattern to other Atlas scripts)
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger("precompute_risk")

# Allow tests / CI to point the script at a specific DB without patching code.
_atlas_db_override = os.environ.get("ATLAS_DB_PATH")
if _atlas_db_override:
    import db.atlas_db as _adb_mod
    _adb_mod._db_path_override = _atlas_db_override
    _adb_mod._risk_cache_tables_ensured = False
    logger.info("DB override from ATLAS_DB_PATH: %s", _atlas_db_override)


REGIME_STATES = [
    "bull_risk_on",
    "bull_risk_off",
    "transition_uncertain",
    "bear_risk_off",
    "bear_capitulation",
    "recovery_early",
]


# ---------------------------------------------------------------------------
# Regime transition matrix
# ---------------------------------------------------------------------------

def _compute_regime_transitions(window_days: int = 90) -> dict:
    """Compute regime transition probability matrix from regime_history."""
    from db.atlas_db import get_db

    with get_db() as db:
        rows = db.execute(
            "SELECT date, regime_state FROM regime_history "
            "WHERE date >= date('now', ?) "
            "ORDER BY date ASC",
            (f"-{window_days} days",),
        ).fetchall()

    if not rows:
        logger.warning("regime_transitions: no regime_history rows in last %d days", window_days)
        return {}

    history = [dict(r) for r in rows]

    # Transition counts
    counts: dict = {s: {t: 0 for t in REGIME_STATES} for s in REGIME_STATES}
    from_counts: dict = {s: 0 for s in REGIME_STATES}

    for i in range(len(history) - 1):
        from_s = history[i]["regime_state"]
        to_s = history[i + 1]["regime_state"]
        if from_s in counts and to_s in counts.get(from_s, {}):
            counts[from_s][to_s] += 1
            from_counts[from_s] += 1

    n_obs = sum(from_counts.values())

    # Probabilities
    matrix: dict = {}
    for from_s in REGIME_STATES:
        matrix[from_s] = {}
        total = from_counts[from_s]
        for to_s in REGIME_STATES:
            matrix[from_s][to_s] = (
                round(counts[from_s][to_s] / total * 100, 1) if total > 0 else 0.0
            )

    # Average durations per state
    durations: dict = {s: [] for s in REGIME_STATES}
    if history:
        cur = history[0]["regime_state"]
        run = 1
        for i in range(1, len(history)):
            if history[i]["regime_state"] == cur:
                run += 1
            else:
                if cur in durations:
                    durations[cur].append(run)
                cur = history[i]["regime_state"]
                run = 1
        if cur in durations:
            durations[cur].append(run)

    avg_dur = {}
    for s in REGIME_STATES:
        runs = durations[s]
        if runs:
            avg_dur[s] = {
                "avg_days": round(sum(runs) / len(runs), 1),
                "max_days": max(runs),
                "occurrences": len(runs),
                "total_days": sum(runs),
            }
        else:
            avg_dur[s] = {"avg_days": 0, "max_days": 0, "occurrences": 0, "total_days": 0}

    return {
        "matrix": matrix,
        "durations": avg_dur,
        "states": REGIME_STATES,
        "current_state": history[-1]["regime_state"] if history else None,
        "total_days": len(history),
        "window_days": window_days,
        "n_observations": n_obs,
    }


def run_regime(window_days: int = 90) -> bool:
    """Compute regime transition matrix and write to cache."""
    logger.info("Computing regime transition matrix (window=%d days)...", window_days)
    t0 = time.monotonic()
    try:
        result = _compute_regime_transitions(window_days=window_days)
        if not result:
            logger.warning("regime: no data -- skipping cache write")
            return False

        from db.atlas_db import set_cached_regime_transitions
        set_cached_regime_transitions(
            matrix=result["matrix"],
            window_days=result["window_days"],
            n_obs=result["n_observations"],
        )
        elapsed = time.monotonic() - t0
        logger.info(
            "regime: cached matrix (%d states, %d transitions, %.1fs)",
            len(REGIME_STATES),
            result["n_observations"],
            elapsed,
        )
        return True
    except Exception:
        logger.exception("regime: failed")
        return False


# ---------------------------------------------------------------------------
# Ruin probability
# ---------------------------------------------------------------------------

def run_ruin() -> bool:
    """Compute ruin probability for current portfolio and persist."""
    logger.info("Computing ruin probability...")
    t0 = time.monotonic()
    try:
        from risk.ruin_probability import compute_for_current_portfolio, persist_ruin_probability

        result = compute_for_current_portfolio(floor_pct=0.70)
        status = result.get("status", "unknown")
        if status not in ("ok", "no_positions"):
            logger.warning("ruin: status=%s -- %s", status, result.get("error", ""))
            if status != "no_positions":
                return False

        persist_ruin_probability(result)
        elapsed = time.monotonic() - t0

        if status == "no_positions":
            logger.info("ruin: no open positions -- wrote empty snapshot (%.1fs)", elapsed)
        else:
            h30 = result.get("horizons", {}).get("30d", {})
            logger.info(
                "ruin: 30d P(ruin)=%.2f%%, equity=$%.0f, %d tickers (%.1fs)",
                h30.get("prob_ruin", 0) * 100,
                result.get("current_equity", 0),
                len(result.get("tickers", [])),
                elapsed,
            )
        return True
    except Exception:
        logger.exception("ruin: failed")
        return False


# ---------------------------------------------------------------------------
# Portfolio VaR / CVaR
# ---------------------------------------------------------------------------

def run_risk() -> bool:
    """Compute portfolio VaR/CVaR and persist to portfolio_risk table."""
    logger.info("Computing portfolio VaR/CVaR...")
    t0 = time.monotonic()
    try:
        from db.atlas_db import get_db, get_current_regime, get_latest_equity
        from risk.portfolio_var import compute_portfolio_var_regime_aware, persist_portfolio_var

        # Open positions
        with get_db() as db:
            trade_rows = db.execute(
                "SELECT ticker, shares, entry_price AS current_price "
                "FROM trades WHERE exit_date IS NULL"
            ).fetchall()

        positions = [dict(r) for r in trade_rows]
        if not positions:
            logger.info("risk: no open positions -- skipping VaR compute")
            return True

        # Equity
        equity_row = get_latest_equity()
        equity = (equity_row or {}).get("equity") or 0.0
        if equity <= 0:
            equity = sum(
                float(p["shares"]) * float(p["current_price"]) for p in positions
            )
        if equity <= 0:
            logger.warning("risk: cannot determine equity -- skipping")
            return False

        # Regime
        regime_data = get_current_regime() or {}
        current_regime = (
            regime_data.get("regime_state")
            or regime_data.get("state")
            or "transition_uncertain"
        )

        # VaR compute
        result = compute_portfolio_var_regime_aware(
            positions=positions,
            current_regime=current_regime,
            lookback_days=60,
            n_paths=10_000,
            horizons=(1, 5),
            seed=42,
            equity=equity,
        )

        row_id = persist_portfolio_var(result)
        elapsed = time.monotonic() - t0
        h1 = result.get("horizons", {}).get("1d", {})
        logger.info(
            "risk: VaR(1d,95)=$%.0f method=%s tickers=%d row_id=%s (%.1fs)",
            h1.get("var_95", 0),
            result.get("method", "?"),
            result.get("positions_count", 0),
            row_id,
            elapsed,
        )
        return True
    except Exception:
        logger.exception("risk: failed")
        return False


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="Atlas nightly risk pre-computation")
    parser.add_argument(
        "--target",
        choices=["all", "risk", "regime", "ruin"],
        default="all",
        help="Which artefact(s) to compute (default: all)",
    )
    args = parser.parse_args()

    logger.info("=== precompute_risk.py --target=%s ===", args.target)
    started_at = datetime.now(timezone.utc).timestamp()

    results: dict = {}

    if args.target in ("all", "regime"):
        results["regime"] = run_regime(window_days=90)

    if args.target in ("all", "ruin"):
        results["ruin"] = run_ruin()

    if args.target in ("all", "risk"):
        results["risk"] = run_risk()

    ok = all(results.values()) if results else True
    elapsed = datetime.now(timezone.utc).timestamp() - started_at
    logger.info(
        "=== done in %.1fs | %s ===",
        elapsed,
        " ".join(f"{k}={'OK' if v else 'FAIL'}" for k, v in results.items()),
    )
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
