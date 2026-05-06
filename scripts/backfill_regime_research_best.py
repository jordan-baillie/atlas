#!/usr/bin/env python3
"""Backfill per-regime entries in research_best from research_experiments.

For each (strategy, universe, regime_state) with >= MIN_EXPERIMENTS experiments
that have sharpe IS NOT NULL and trades >= 30, finds the highest-Sharpe row and
writes it to research_best as a per-regime entry.

Skips combos with < MIN_EXPERIMENTS to avoid promoting underpowered results.
Cross-regime (NULL) rows already in research_best are untouched — this script
only writes per-regime rows.

Per audit 2026-05-06 Recommendation 5.

Usage:
    python3 scripts/backfill_regime_research_best.py --dry-run
    python3 scripts/backfill_regime_research_best.py --apply
"""
import argparse
import ast
import json
import logging
import sys
from pathlib import Path

ATLAS_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ATLAS_ROOT))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger("backfill_regime")

MIN_EXPERIMENTS = 30   # minimum experiments per (strategy, universe, regime) to be eligible
MIN_TRADES = 30        # minimum trades in a single experiment row to be eligible


def _parse_params(raw: str | None) -> dict:
    """Parse params_changed string into a dict.

    research_experiments.params_changed can be either JSON or a Python-style
    'key=val, key2=val2' kwargs string (with nested dict literals).
    Tries JSON first, then falls back to ast.parse keyword-argument parsing.
    """
    if not raw or not raw.strip():
        return {}
    # Try JSON first
    try:
        result = json.loads(raw)
        if isinstance(result, dict):
            return result
    except (json.JSONDecodeError, TypeError):
        pass
    # Try Python kwargs-style: "key=val, key2=val2"
    try:
        tree = ast.parse(f"_f({raw})", mode="eval")
        call = tree.body  # type: ignore[attr-defined]
        return {kw.arg: ast.literal_eval(kw.value) for kw in call.keywords}
    except Exception:
        return {}


def backfill(apply: bool) -> int:
    from db.atlas_db import get_db, upsert_research_best

    with get_db() as db:
        # Verify schema
        cols = [r[1] for r in db.execute("PRAGMA table_info(research_experiments)").fetchall()]
        if "regime_state" not in cols:
            logger.error("research_experiments has no regime_state column — abort")
            return 1

        rb_cols = [r[1] for r in db.execute("PRAGMA table_info(research_best)").fetchall()]
        if "regime_state" not in rb_cols:
            logger.error("research_best has no regime_state column — run migration first")
            return 1

        # universe column is named 'universe' (not 'market') in research_experiments
        # research_experiments stores swept params in 'params_changed' column (not 'params')
        params_col = "params_changed" if "params_changed" in cols else (
            "params" if "params" in cols else None
        )
        if params_col is None:
            logger.error("research_experiments has no params_changed/params column — abort")
            return 1

        universe_col = "universe" if "universe" in cols else "market"

        # Aggregate: per (strategy, universe, regime_state) with MIN_EXPERIMENTS qualifying rows
        agg_rows = db.execute(f"""
            SELECT strategy,
                   {universe_col} AS universe,
                   regime_state,
                   COUNT(*) AS n
            FROM research_experiments
            WHERE regime_state IS NOT NULL
              AND sharpe IS NOT NULL
              AND trades >= ?
            GROUP BY strategy, {universe_col}, regime_state
            HAVING n >= ?
            ORDER BY strategy, {universe_col}, regime_state
        """, (MIN_TRADES, MIN_EXPERIMENTS)).fetchall()

        logger.info(
            "[backfill] %d eligible (strategy, universe, regime) combos found "
            "(MIN_EXPERIMENTS=%d, MIN_TRADES=%d)",
            len(agg_rows), MIN_EXPERIMENTS, MIN_TRADES,
        )

        written = 0
        skipped = 0
        for agg in agg_rows:
            strat = agg["strategy"]
            uni = agg["universe"]
            regime = agg["regime_state"]
            n_exp = agg["n"]

            # Pull the single best row for this combo
            best = db.execute(f"""
                SELECT {params_col} AS params, sharpe, trades, max_dd_pct
                FROM research_experiments
                WHERE strategy=?
                  AND {universe_col}=?
                  AND regime_state=?
                  AND sharpe IS NOT NULL
                  AND trades >= ?
                ORDER BY sharpe DESC
                LIMIT 1
            """, (strat, uni, regime, MIN_TRADES)).fetchone()

            if not best:
                skipped += 1
                continue

            try:
                raw_params = best["params"]
                params = _parse_params(raw_params)
                # params=={} is valid: means "default config, no param changes"
                if params is None:
                    logger.warning("  null params for %s/%s/%s — skipping", strat, uni, regime)
                    skipped += 1
                    continue
            except Exception as exc:
                logger.warning("  params parse error for %s/%s/%s: %s", strat, uni, regime, exc)
                skipped += 1
                continue

            logger.info(
                "[backfill] %s / %s / %s  Sharpe=%.4f  trades=%d  n_exp=%d",
                strat, uni, regime,
                float(best["sharpe"]),
                int(best["trades"]),
                n_exp,
            )

            if apply:
                try:
                    upsert_research_best(
                        strategy=strat,
                        universe=uni,
                        params=params,
                        sharpe=float(best["sharpe"]),
                        trades=int(best["trades"]),
                        max_dd_pct=float(best["max_dd_pct"] or 0.0),
                        regime_state=regime,
                    )
                    written += 1
                except Exception as exc:
                    logger.error("  upsert failed for %s/%s/%s: %s", strat, uni, regime, exc)
                    skipped += 1
            else:
                written += 1   # count as "would write" in dry-run

    action = "wrote" if apply else "would write"
    logger.info("[backfill] %s %d per-regime rows, skipped %d", action, written, skipped)
    return 0


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Backfill per-regime research_best rows")
    p.add_argument("--apply", action="store_true", help="Actually write to DB (default: dry-run)")
    p.add_argument("--dry-run", action="store_true", help="Print what would be written (default)")
    args = p.parse_args()
    if not args.apply and not args.dry_run:
        args.dry_run = True
    sys.exit(backfill(apply=args.apply))
