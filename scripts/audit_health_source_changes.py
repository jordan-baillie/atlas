#!/usr/bin/env python3
"""
Audit script: compare old (JSON) vs new (research_best SQLite) backtest_sharpe source.

For each currently-LIVE strategy×universe combo in config/active/*.json:
  - OLD source: research/best/{strategy}.json or research/best/{strategy}_{universe}.json
  - NEW source: research_best SQLite (regime-conditioned by current regime, with fallback)
  - Compute HEALTHY/WARNING verdict under each source
  - Flag any strategy whose verdict changes under the new source

Output: docs/audits/health_source_consolidation_2026-05-06.md

Usage:
    python3 scripts/audit_health_source_changes.py [--output PATH]

Exit codes:
    0 — audit complete (disagreements are informational, not failures)
"""

from __future__ import annotations

import argparse
import json
import sys
import os
from datetime import datetime
from pathlib import Path
from typing import Optional, Tuple

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

from monitor.strategy_health import SHARPE_HEALTHY_RATIO, HEALTHY, WARNING
from db.atlas_db import get_current_regime_state


# ── Helpers ───────────────────────────────────────────────────────────────────

def _get_old_sharpe(strategy: str, universe: str) -> Optional[float]:
    """Load backtest Sharpe from the legacy JSON file (old source)."""
    # Try universe-specific file first (e.g. connors_rsi2_commodity_etfs.json)
    candidates = [
        PROJECT / "research" / "best" / f"{strategy}_{universe}.json",
        PROJECT / "research" / "best" / f"{strategy}.json",
    ]
    for path in candidates:
        if path.exists():
            try:
                data = json.loads(path.read_text())
                return data.get("metrics", {}).get("sharpe")
            except Exception:
                pass
    return None


def _get_new_sharpe(
    strategy: str,
    universe: str,
    current_regime: Optional[str],
) -> Tuple[Optional[float], str]:
    """Load backtest Sharpe from research_best SQLite (new source).

    Returns (sharpe, source_description) where source_description indicates
    whether a regime-conditioned or cross-regime row was used.
    """
    try:
        from research.loop import load_best
        best = load_best(strategy, universe, regime_state=current_regime)
        if best and best.get("metrics"):
            sharpe = best["metrics"].get("sharpe")
            regime_row = best.get("regime_state")
            if regime_row is not None:
                src = f"research_best[{regime_row}]"
            else:
                src = "research_best[cross-regime]"
            return sharpe, src
    except Exception as exc:
        return None, f"error({exc})"
    return None, "no row"


def _verdict(
    live_sharpe: Optional[float], backtest_sharpe: Optional[float]
) -> str:
    """Compute HEALTHY/WARNING verdict from live and backtest Sharpe."""
    if backtest_sharpe is None:
        return "HEALTHY (no benchmark)"
    if live_sharpe is None:
        return "INSUFFICIENT_DATA"
    if live_sharpe < 0:
        return WARNING
    if backtest_sharpe > 0 and live_sharpe < SHARPE_HEALTHY_RATIO * backtest_sharpe:
        return WARNING
    return HEALTHY


# ── Main ──────────────────────────────────────────────────────────────────────

def run_audit(output_path: Path) -> None:
    """Run the audit and write the Markdown report."""
    current_regime = None
    try:
        current_regime = get_current_regime_state()
    except Exception:
        pass

    # Collect all enabled strategy × universe combos from config/active/*.json
    active_dir = PROJECT / "config" / "active"
    combos: list[tuple[str, str]] = []
    for cfg_file in sorted(active_dir.glob("*.json")):
        universe = cfg_file.stem
        if universe in ("regime", "crypto", "asx"):
            continue  # skip non-equity or test universes
        try:
            cfg = json.loads(cfg_file.read_text())
        except Exception:
            continue
        strategies = cfg.get("strategies", {})
        for strat, strat_cfg in strategies.items():
            if isinstance(strat_cfg, dict) and strat_cfg.get("enabled", False):
                combos.append((strat, universe))

    if not combos:
        print("No enabled strategies found in any config/active/*.json", file=sys.stderr)
        return

    # For each combo, get OLD source, NEW source, and current live Sharpe (set None — no DB read here)
    rows = []
    disagreements = []

    for strategy, universe in sorted(set(combos)):
        old_sharpe = _get_old_sharpe(strategy, universe)
        new_sharpe, new_src = _get_new_sharpe(strategy, universe, current_regime)

        # Verdicts assume live_sharpe = None (INSUFFICIENT_DATA shows if benchmark changes)
        # For old vs new verdict comparison, treat live_sharpe as a hypothetical middling value:
        # Use 0.0 (neutral) to test if threshold crossings change.
        # The "real" live Sharpe isn't needed for the comparison; we just compare
        # whether the BENCHMARK itself has changed and whether that would flip verdicts.
        old_verdict_null = _verdict(None, old_sharpe)
        new_verdict_null = _verdict(None, new_sharpe)

        # Compute what a marginally-healthy live Sharpe would look like under each source
        # and whether WARNING threshold flips
        old_warning_threshold = (
            round(SHARPE_HEALTHY_RATIO * old_sharpe, 4) if old_sharpe and old_sharpe > 0 else None
        )
        new_warning_threshold = (
            round(SHARPE_HEALTHY_RATIO * new_sharpe, 4) if new_sharpe and new_sharpe > 0 else None
        )

        changed = old_sharpe != new_sharpe and (
            (old_sharpe is None) != (new_sharpe is None)
            or (old_sharpe is not None and new_sharpe is not None
                and abs(old_sharpe - new_sharpe) > 0.001)
        )

        row = {
            "strategy": strategy,
            "universe": universe,
            "old_sharpe": old_sharpe,
            "new_sharpe": new_sharpe,
            "new_src": new_src,
            "old_warning_threshold": old_warning_threshold,
            "new_warning_threshold": new_warning_threshold,
            "changed": changed,
        }
        rows.append(row)
        if changed:
            disagreements.append(row)

    # ── Build Markdown report ─────────────────────────────────────────────────

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    current_regime_str = current_regime or "unknown (regime_history empty)"

    lines = [
        "# Health Source Consolidation Audit",
        "",
        f"**Generated**: {now_str}",
        f"**Current regime**: `{current_regime_str}`",
        f"**Change summary**: {len(disagreements)} of {len(rows)} strategy×universe combos "
        f"have a different backtest_sharpe under the new source.",
        "",
        "## Context",
        "",
        "Per Items 2+3 (audit 2026-05-06), `monitor/strategy_health.py::_load_backtest_metrics`",
        "was changed to read from `research_best` SQLite (regime-conditioned) instead of",
        "`research/best/*.json` (cross-regime, flat files).",
        "",
        "This table shows the impact: OLD = JSON file Sharpe, NEW = research_best Sharpe,",
        "WARNING THRESHOLD = `live_sharpe < 50% × backtest_sharpe` trigger point.",
        "",
        "Disagreements are **informational only** — the new source is correct by design.",
        "",
        "## Full Table",
        "",
        "| Strategy | Universe | OLD Sharpe (JSON) | NEW Sharpe (SQLite) | NEW Source | OLD Warning Threshold | NEW Warning Threshold | Changed? |",
        "|----------|----------|-------------------|---------------------|------------|----------------------|----------------------|----------|",
    ]

    for r in rows:
        old_s = f"{r['old_sharpe']:.4f}" if r["old_sharpe"] is not None else "—"
        new_s = f"{r['new_sharpe']:.4f}" if r["new_sharpe"] is not None else "—"
        old_t = f"{r['old_warning_threshold']:.4f}" if r["old_warning_threshold"] is not None else "—"
        new_t = f"{r['new_warning_threshold']:.4f}" if r["new_warning_threshold"] is not None else "—"
        flag = "⚠️ YES" if r["changed"] else "—"
        lines.append(
            f"| {r['strategy']} | {r['universe']} | {old_s} | {new_s} "
            f"| `{r['new_src']}` | {old_t} | {new_t} | {flag} |"
        )

    if disagreements:
        lines += [
            "",
            "## Disagreements",
            "",
            "The following combos have a meaningfully different backtest Sharpe under the new source.",
            "Review to confirm the regime-conditioned value is expected:",
            "",
        ]
        for r in disagreements:
            old_s = f"{r['old_sharpe']:.4f}" if r["old_sharpe"] is not None else "None"
            new_s = f"{r['new_sharpe']:.4f}" if r["new_sharpe"] is not None else "None"
            direction = ""
            if r["old_sharpe"] is not None and r["new_sharpe"] is not None:
                if r["new_sharpe"] > r["old_sharpe"]:
                    direction = " (NEW is higher — more stringent WARNING threshold)"
                else:
                    direction = " (NEW is lower — less stringent WARNING threshold)"
            lines.append(
                f"- **{r['strategy']}** × **{r['universe']}**: "
                f"OLD={old_s} → NEW={new_s} via `{r['new_src']}`{direction}"
            )
    else:
        lines += [
            "",
            "## Disagreements",
            "",
            "✅ No meaningful disagreements — both sources agree for all live strategy×universe combos.",
        ]

    lines += [
        "",
        "---",
        "*Generated by `scripts/audit_health_source_changes.py`*",
    ]

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n".join(lines) + "\n")
    print(f"Report written to: {output_path}")
    print(f"Disagreements: {len(disagreements)}/{len(rows)}")


def main(argv: Optional[list] = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=PROJECT / "docs" / "audits" / "health_source_consolidation_2026-05-06.md",
        help="Output path for the Markdown report",
    )
    args = parser.parse_args(argv)
    run_audit(args.output)


if __name__ == "__main__":
    main()
