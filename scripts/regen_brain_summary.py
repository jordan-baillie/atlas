#!/usr/bin/env python3
"""
scripts/regen_brain_summary.py — Regenerate research/brain/SUMMARY.md

Summarises:
  1. Strategy lifecycle state distribution (from strategy_lifecycle table)
  2. Active strategies per market (from config/active/*.json)
  3. Top-10 strategies by research_best.sharpe
  4. Recent transitions / promotions (strategy_lifecycle_history + data/promotion_log.json)
  5. Contaminated research_best files — uses research/integrity.py if available, else JSON fallback

Usage:
  python3 scripts/regen_brain_summary.py
"""

from __future__ import annotations

import datetime
import json
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).parent.parent
OUTPUT_PATH = PROJECT_ROOT / "research" / "brain" / "SUMMARY.md"

sys.path.insert(0, str(PROJECT_ROOT))


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def _db_rows(sql: str, params: tuple = ()) -> list[dict]:
    """Run a SELECT and return list of row dicts (fail-safe)."""
    try:
        from db import atlas_db
        with atlas_db.get_db() as conn:
            conn.row_factory = __import__("sqlite3").Row
            rows = conn.execute(sql, params).fetchall()
            return [dict(r) for r in rows]
    except Exception as e:
        return [{"_error": str(e)}]


# ── Section builders ───────────────────────────────────────────────────────────

def _section_lifecycle_states() -> str:
    rows = _db_rows(
        "SELECT state, COUNT(*) AS cnt FROM strategy_lifecycle GROUP BY state ORDER BY cnt DESC"
    )
    if not rows or "_error" in rows[0]:
        err = rows[0].get("_error", "unknown") if rows else "no rows"
        return f"*(strategy_lifecycle query failed: {err})*\n"

    lines = ["| State | Count |", "|-------|-------|"]
    total = 0
    for r in rows:
        lines.append(f"| `{r['state']}` | {r['cnt']} |")
        total += r["cnt"]
    lines.append(f"| **Total** | **{total}** |")
    return "\n".join(lines) + "\n"


def _section_active_per_market() -> str:
    active_dir = PROJECT_ROOT / "config" / "active"
    lines: list[str] = []
    for cfg_path in sorted(active_dir.glob("*.json")):
        market = cfg_path.stem
        cfg = _load_json(cfg_path)
        if not cfg or not isinstance(cfg, dict):
            continue
        strats = cfg.get("strategies", {})
        trading = cfg.get("trading", {})
        mode = trading.get("mode", cfg.get("mode", "?"))
        live_enabled = trading.get("live_enabled", cfg.get("live_enabled", False))
        if isinstance(strats, dict):
            strategy_list = sorted(strats.keys())
        else:
            strategy_list = []
        status_icon = "🟢" if live_enabled else "⚪"
        lines.append(f"**{market}** {status_icon} (mode=`{mode}`, live_enabled=`{live_enabled}`)")
        if strategy_list:
            lines.append("  " + ", ".join(f"`{s}`" for s in strategy_list))
        else:
            lines.append("  *(no strategies configured)*")
        lines.append("")
    return "\n".join(lines) if lines else "*(no config/active/*.json found)*\n"


def _section_top10_by_sharpe() -> str:
    rows = _db_rows(
        """
        SELECT strategy, universe, regime_state, sharpe, trades, max_dd_pct, updated_at
        FROM research_best
        ORDER BY sharpe DESC
        LIMIT 10
        """
    )
    if not rows or "_error" in rows[0]:
        err = rows[0].get("_error", "unknown") if rows else "no rows"
        return f"*(research_best query failed: {err})*\n"

    lines = [
        "| # | Strategy | Universe | Regime | Sharpe | Trades | Max DD% | Updated |",
        "|---|----------|----------|--------|--------|--------|---------|---------|",
    ]
    for i, r in enumerate(rows, 1):
        regime = r.get("regime_state") or "cross"
        sharpe = f"{r['sharpe']:.4f}" if r.get("sharpe") is not None else "—"
        trades = r.get("trades", "—")
        dd = f"{r['max_dd_pct']:.1f}%" if r.get("max_dd_pct") is not None else "—"
        updated = (r.get("updated_at") or "—")[:10]
        lines.append(
            f"| {i} | `{r['strategy']}` | `{r['universe']}` | `{regime}` "
            f"| {sharpe} | {trades} | {dd} | {updated} |"
        )
    return "\n".join(lines) + "\n"


def _section_recent_promotions() -> str:
    # Primary: strategy_lifecycle_history (most authoritative)
    rows = _db_rows(
        """
        SELECT strategy, universe, from_state, to_state, transitioned_at, reason, operator
        FROM strategy_lifecycle_history
        ORDER BY transitioned_at DESC
        LIMIT 10
        """
    )
    lines: list[str] = []
    if rows and "_error" not in rows[0]:
        lines.append("*Source: strategy_lifecycle_history table*")
        lines.append("")
        lines.append("| Strategy | Universe | Transition | Date | Reason |")
        lines.append("|----------|----------|------------|------|--------|")
        for r in rows:
            fr = r.get("from_state") or "—"
            to = r.get("to_state") or "—"
            date = (r.get("transitioned_at") or "—")[:10]
            reason = (r.get("reason") or "—")[:60].replace("|", "\\|")
            lines.append(
                f"| `{r['strategy']}` | `{r['universe']}` | `{fr}` → `{to}` | {date} | {reason} |"
            )
        lines.append("")

    # Secondary: data/promotion_log.json (auto-promotion runs)
    promo_path = PROJECT_ROOT / "data" / "promotion_log.json"
    promo_data = _load_json(promo_path)
    if promo_data and isinstance(promo_data, list) and promo_data:
        last_5 = promo_data[-5:]
        lines.append("*Source: data/promotion_log.json (auto-promotion runs)*")
        lines.append("")
        lines.append("| Strategy | Universe | Paper Sharpe | Research Sharpe | Date |")
        lines.append("|----------|----------|-------------|-----------------|------|")
        for entry in reversed(last_5):
            strat = entry.get("strategy", "—")
            univ = entry.get("universe", "—")
            ps = f"{entry['paper_sharpe']:.4f}" if entry.get("paper_sharpe") is not None else "—"
            rs = f"{entry['research_sharpe']:.4f}" if entry.get("research_sharpe") is not None else "—"
            date = (entry.get("ts") or "—")[:10]
            lines.append(f"| `{strat}` | `{univ}` | {ps} | {rs} | {date} |")

    return "\n".join(lines) + "\n" if lines else "*(no promotion history found)*\n"


def _section_contaminated_research() -> str:
    """Check research_best for contamination flags. Uses integrity.py if available."""
    contaminated: list[dict] = []

    # Try Task A's research/integrity.py if it exists
    integrity_path = PROJECT_ROOT / "research" / "integrity.py"
    if integrity_path.exists():
        try:
            import importlib.util
            spec = importlib.util.spec_from_file_location("research.integrity", integrity_path)
            mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
            spec.loader.exec_module(mod)  # type: ignore[union-attr]
            if hasattr(mod, "check_solo"):
                result = mod.check_solo()
                if isinstance(result, list):
                    contaminated = result
        except Exception as e:
            pass  # Fall through to inline check

    # Fallback: check is_solo field in research_best JSON files
    if not contaminated:
        best_dir = PROJECT_ROOT / "research" / "best"
        if best_dir.exists():
            for json_file in best_dir.glob("*.json"):
                try:
                    data = json.loads(json_file.read_text())
                    if isinstance(data, dict):
                        # Look for is_solo=True or contamination flags
                        if data.get("is_solo") or data.get("contaminated"):
                            contaminated.append({
                                "file": str(json_file.relative_to(PROJECT_ROOT)),
                                "is_solo": data.get("is_solo"),
                                "contaminated": data.get("contaminated"),
                            })
                except (OSError, json.JSONDecodeError):
                    pass

    # Also check research_best table for solo_sharpe != portfolio_sharpe discrepancies
    rows = _db_rows(
        """
        SELECT strategy, universe, regime_state, sharpe, solo_sharpe, portfolio_sharpe, metric_type
        FROM research_best
        WHERE metric_type IS NOT NULL AND metric_type != 'portfolio'
        ORDER BY strategy, universe
        LIMIT 20
        """
    )

    lines: list[str] = []
    if contaminated:
        lines.append(f"**{len(contaminated)} contaminated file(s) found:**")
        for item in contaminated:
            lines.append(f"  - {item.get('file', item)}")
    else:
        lines.append("**No contaminated research_best JSON files detected.**")

    lines.append("")
    if rows and "_error" not in rows[0]:
        non_portfolio = [r for r in rows if r.get("metric_type") not in ("portfolio", None)]
        if non_portfolio:
            lines.append(f"*research_best rows with non-portfolio metric_type: {len(non_portfolio)}*")
            lines.append("")
            lines.append("| Strategy | Universe | Metric Type | Sharpe |")
            lines.append("|----------|----------|-------------|--------|")
            for r in non_portfolio[:10]:
                sharpe = f"{r['sharpe']:.4f}" if r.get("sharpe") is not None else "—"
                lines.append(
                    f"| `{r['strategy']}` | `{r['universe']}` "
                    f"| `{r.get('metric_type', '—')}` | {sharpe} |"
                )
        else:
            lines.append("*All research_best rows have metric_type=portfolio or NULL.*")

    return "\n".join(lines) + "\n"


# ── Main ───────────────────────────────────────────────────────────────────────

def build_summary() -> str:
    today = datetime.date.today().isoformat()
    lines: list[str] = []

    def h(text: str) -> None:
        lines.append(text)

    h("# Research Brain Summary")
    h("")
    h(f"*Auto-regenerated {today}. Re-run via `python3 scripts/regen_brain_summary.py`.*")
    h("")
    h("*This file is overwritten on each regen — do not edit by hand.*")
    h("")
    h("---")
    h("")

    h("## 1. Strategy Lifecycle State Distribution")
    h("")
    h(_section_lifecycle_states())

    h("## 2. Active Strategies per Market")
    h("")
    h(_section_active_per_market())

    h("## 3. Top-10 Strategies by Sharpe Ratio")
    h("")
    h(_section_top10_by_sharpe())

    h("## 4. Recent Transitions & Promotions")
    h("")
    h(_section_recent_promotions())

    h("## 5. Research Integrity Check")
    h("")
    h(_section_contaminated_research())
    h("")

    return "\n".join(lines)


def main() -> None:
    summary = build_summary()
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(summary + "\n")
    size = OUTPUT_PATH.stat().st_size
    print(f"Wrote {OUTPUT_PATH.relative_to(PROJECT_ROOT)}  ({size:,} bytes)")


if __name__ == "__main__":
    main()
