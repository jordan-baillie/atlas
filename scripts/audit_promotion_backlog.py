#!/usr/bin/env python3
"""Read-only audit of the research_experiments promotion backlog.

Queries research_experiments WHERE status='kept' AND created_at >= '2026-04-13',
compares against research_best to classify each (strategy, universe) group:

    - already_superseded  : research_best sharpe >= best_kept_sharpe (no gain)
    - fail_client_gate    : best_kept_sharpe - current_best_sharpe < 0.05
    - promote_eligible    : delta >= 0.05 AND final_sharpe > 0 AND params exist

Outputs a markdown table to stdout and a summary line.

IMPORTANT: This script performs NO DB writes and does NOT call auto_promote().
"""

import sys
from pathlib import Path

ATLAS_ROOT = Path(__file__).resolve().parent.parent
if str(ATLAS_ROOT) not in sys.path:
    sys.path.insert(0, str(ATLAS_ROOT))

import json
import sqlite3
from typing import Optional


# ─── Helpers ─────────────────────────────────────────────────────────────────


def _get_research_best_sharpe(strategy: str, universe: str, conn: sqlite3.Connection) -> Optional[float]:
    """Return best solo/standalone Sharpe for (strategy, universe), or None.

    M2 2026-04-28: uses COALESCE(solo_sharpe, sharpe) so solo-strategy Sharpe
    is preferred over legacy whole-portfolio Sharpe.
    """
    row = conn.execute(
        "SELECT COALESCE(solo_sharpe, sharpe) AS best_sharpe, solo_sharpe, metric_type "
        "FROM research_best WHERE strategy=? AND universe=?",
        (strategy, universe),
    ).fetchone()
    if not row or row[0] is None:
        return None
    return float(row[0])


def _get_research_best_meta(strategy: str, universe: str, conn: sqlite3.Connection) -> dict:
    """Return metric metadata for a research_best row (for legacy annotation)."""
    row = conn.execute(
        "SELECT solo_sharpe, metric_type FROM research_best WHERE strategy=? AND universe=?",
        (strategy, universe),
    ).fetchone()
    if not row:
        return {"solo_sharpe": None, "metric_type": None}
    return {
        "solo_sharpe": row[0],
        "metric_type": row[1],
    }


def _has_best_params(strategy: str, universe: str, conn: sqlite3.Connection) -> bool:
    """True if research_best has a non-empty params entry for (strategy, universe)."""
    row = conn.execute(
        "SELECT params FROM research_best WHERE strategy=? AND universe=?",
        (strategy, universe),
    ).fetchone()
    if not row or not row[0]:
        return False
    try:
        p = json.loads(row[0]) if isinstance(row[0], str) else row[0]
        return bool(p)
    except (json.JSONDecodeError, TypeError):
        return False


def _has_json_params(strategy: str, universe: str) -> bool:
    """True if research/best/{strategy}_{universe}.json or {strategy}.json exists."""
    best_dir = ATLAS_ROOT / "research" / "best"
    return (best_dir / f"{strategy}_{universe}.json").exists() or (
        best_dir / f"{strategy}.json"
    ).exists()


# ─── Main ─────────────────────────────────────────────────────────────────────


def main() -> None:
    db_path = ATLAS_ROOT / "data" / "atlas.db"
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    # ── M2 2026-04-28: warn about legacy rows before rendering table ──────────
    n_legacy = conn.execute(
        "SELECT COUNT(*) FROM research_best WHERE solo_sharpe IS NULL"
    ).fetchone()
    n_legacy_count = n_legacy[0] if n_legacy else 0
    n_legacy_type = conn.execute(
        "SELECT COUNT(*) FROM research_best WHERE metric_type = 'legacy_portfolio'"
    ).fetchone()
    n_legacy_type_count = n_legacy_type[0] if n_legacy_type else 0
    if n_legacy_count > 0:
        import sys as _sys
        print(
            f"⚠️  WARN: {n_legacy_count} rows still have no solo_sharpe "
            f"({n_legacy_type_count} marked legacy_portfolio). "
            f"Run migration 2026-04-28-research-best-solo-sharpe.py --apply "
            f"or trigger a re-sweep to refresh.",
            file=_sys.stderr,
        )

    # Aggregate kept experiments by (strategy, universe).
    # FILTER: exclude description='baseline' and params_changed IS NULL — these
    # are whole-portfolio regression-check baselines (gate 2 of auto_promote),
    # NOT individual strategy parameter improvements. Mixing them creates
    # apples-to-oranges deltas (#B3 canary RCA, 2026-04-28).
    rows = conn.execute(
        """
        SELECT
            strategy,
            universe,
            COUNT(*)            AS n_kept,
            MAX(sharpe)         AS best_kept_sharpe,
            MIN(sharpe)         AS worst_kept_sharpe
        FROM research_experiments
        WHERE status = 'kept'
          AND created_at >= '2026-04-13'
          AND description != 'baseline'
          AND params_changed IS NOT NULL
        GROUP BY strategy, universe
        ORDER BY strategy, universe
        """,
    ).fetchall()

    if not rows:
        print("No 'kept' experiments found since 2026-04-13.")
        return

    # ── Classification ────────────────────────────────────────────────────────
    table_rows = []
    n_promote_eligible = 0
    n_fail_gate = 0
    n_already_superseded = 0
    n_no_params = 0

    for row in rows:
        strategy = row["strategy"]
        universe = row["universe"]
        n_kept = row["n_kept"]
        best_kept = row["best_kept_sharpe"] or 0.0
        current_best = _get_research_best_sharpe(strategy, universe, conn)
        has_params = _has_best_params(strategy, universe, conn) or _has_json_params(strategy, universe)

        delta = best_kept - (current_best or 0.0)
        # M2 2026-04-28: annotate if current_best fell back to legacy portfolio sharpe
        meta = _get_research_best_meta(strategy, universe, conn)
        _is_legacy = (
            meta.get("solo_sharpe") is None
            and meta.get("metric_type") in ("legacy_portfolio", None, "unknown")
            and current_best is not None
        )
        _legacy_tag = " [LEGACY-PORTFOLIO]" if _is_legacy else ""
        current_best_str = (
            f"{current_best:.4f}{_legacy_tag}" if current_best is not None else "N/A"
        )
        delta_str = f"{delta:+.4f}"

        # Classify
        if current_best is not None and current_best >= best_kept:
            classification = "no"
            reason = "already superseded in research_best"
            n_already_superseded += 1
        elif not has_params:
            classification = "no"
            reason = "no params in research_best or JSON fallback"
            n_no_params += 1
        elif best_kept <= 0:
            classification = "no"
            reason = "best_kept_sharpe <= 0 (safety guard)"
            n_fail_gate += 1
        elif delta < 0.05:
            classification = "no"
            reason = f"delta_sharpe={delta:+.4f} < 0.05 client gate"
            n_fail_gate += 1
        else:
            classification = "YES"
            reason = ""
            n_promote_eligible += 1

        table_rows.append(
            (strategy, universe, n_kept, f"{best_kept:.4f}", current_best_str, delta_str, classification, reason)
        )

    conn.close()

    # ── Print markdown table ──────────────────────────────────────────────────
    headers = [
        "strategy", "universe", "n_kept",
        "best_kept_sharpe", "current_best_sharpe", "delta",
        "would_promote", "reason_if_no",
    ]
    # Column widths
    col_w = [max(len(h), max(len(str(r[i])) for r in table_rows)) for i, h in enumerate(headers)]

    def fmt_row(cells):
        return "| " + " | ".join(str(c).ljust(col_w[i]) for i, c in enumerate(cells)) + " |"

    sep = "|" + "|".join("-" * (w + 2) for w in col_w) + "|"

    print(fmt_row(headers))
    print(sep)
    for r in table_rows:
        print(fmt_row(r))

    print()

    # ── Summary ───────────────────────────────────────────────────────────────
    total = sum(r["n_kept"] for r in rows)
    total_groups = len(rows)
    n_other = n_no_params  # no-params is a sub-class of fail-gate for summary purposes
    n_fail_total = n_fail_gate + n_no_params

    print(
        f"BACKLOG: {total} kept experiments across {total_groups} (strategy, universe) groups "
        f"→ {n_promote_eligible} promote-eligible "
        f"/ {n_fail_total} fail client gate "
        f"/ {n_already_superseded} already superseded"
    )
    if n_no_params:
        print(f"  (of fail-gate: {n_no_params} lack params in research_best / JSON fallback)")


if __name__ == "__main__":
    main()
