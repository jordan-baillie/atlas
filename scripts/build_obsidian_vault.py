#!/usr/bin/env python3
"""
Atlas Obsidian Vault Generator

Converts Atlas research data (journal.json, experiment envelopes, wave briefs)
into an Obsidian-compatible Markdown vault with linked notes.

Usage:
    python3 scripts/build_obsidian_vault.py --output-dir research/vault/ --force
"""

import argparse
import json
import re
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Optional


# ─── Constants ───────────────────────────────────────────────────────────────

ATLAS_ROOT = Path(__file__).resolve().parent.parent

STRATEGY_NAMES: dict[str, str] = {
    "mean_reversion": "Mean Reversion",
    "trend_following": "Trend Following",
    "opening_gap": "Opening Gap",
    "momentum_breakout": "Momentum Breakout",
    "short_term_mr": "Short Term MR",
    "sector_rotation": "Sector Rotation",
    "mtf_momentum": "MTF Momentum",
    "bb_squeeze": "Bollinger Band Squeeze",
    "connors_rsi2": "ConnorsRSI2",
    "lower_band_reversion": "Lower Band Reversion",
    "triple_rsi": "Triple RSI",
    "portfolio_filter": "Portfolio Filter",
    "combined": "Combined Portfolio",
    "sma200_filter": "SMA-200 Filter",
}

# Strategies currently in the live config (v2.1+)
ACTIVE_STRATEGIES = {"mean_reversion", "trend_following", "opening_gap"}
# Strategies used as portfolio-level filters or modifiers
FILTER_STRATEGIES = {"sma200_filter", "portfolio_filter", "combined"}


# ─── Helpers ─────────────────────────────────────────────────────────────────


def strategy_to_human(strategy_id: Optional[str]) -> str:
    """Return human-readable strategy name."""
    if not strategy_id:
        return "Portfolio Filter"
    return STRATEGY_NAMES.get(strategy_id, strategy_id.replace("_", " ").title())


def strategy_to_kebab(strategy_id: Optional[str]) -> str:
    """Return kebab-case tag component for strategy."""
    if not strategy_id:
        return "portfolio-filter"
    return strategy_id.replace("_", "-")


def get_strategy_status(strategy_id: Optional[str]) -> str:
    """Determine strategy lifecycle status."""
    if not strategy_id:
        return "filter"
    if strategy_id in ACTIVE_STRATEGIES:
        return "active"
    if strategy_id in FILTER_STRATEGIES:
        return "filter"
    return "dormant"


def get_wave_number(experiment_id: str) -> Optional[int]:
    """Extract wave number from experiment_id like 'wave1_moment_solo'."""
    m = re.match(r"^wave(\d+)_", experiment_id)
    return int(m.group(1)) if m else None


def derive_title(experiment_id: str) -> str:
    """Convert experiment_id to a readable title.

    Examples:
        wave1_moment_solo    -> "Momentum Breakout Solo"
        wave2_rsi2_solo      -> "ConnorsRSI2 Solo"
        wave4_lbr_no_sma200  -> "LBR No SMA-200"
    """
    # Strip wave prefix
    title = re.sub(r"^wave\d+_", "", experiment_id)
    # Normalise double underscores
    title = title.replace("__", "_")

    # Ordered substitution table (longer patterns first to avoid partial matches)
    subs = [
        ("mr_hold5_oos", "MR Hold-5 OOS Validation"),
        ("mr_strength_exit", "MR Strength Exit"),
        ("cross_mkt", "Cross Market"),
        ("chandelier_tf", "Chandelier TF"),
        ("exit_mr", "MR Exit"),
        ("exit_og", "Opening Gap Exit"),
        ("vol_combined", "Volume Combined"),
        ("vol_sweep", "Volume Sweep"),
        ("vol_filter", "Volume Filter"),
        ("vix_filter", "VIX Filter"),
        ("tom_filter", "Turn-of-Month Filter"),
        ("ibs_sweep", "IBS Sweep"),
        ("hold_combined", "Hold Combined"),
        ("rsi_period", "RSI Period"),
        ("band_sweep", "Band Sweep"),
        ("no_sma200", "No SMA-200"),
        ("asx_reopt", "ASX Re-optimization"),
        ("lbr_solo_relaxed", "LBR Solo Relaxed"),
        ("lbr_band_sweep", "LBR Band Sweep"),
        ("lbr_ibs_sweep", "LBR IBS Sweep"),
        ("lbr_no_sma200", "LBR No SMA-200"),
        ("lbr_solo", "LBR Solo"),
        ("moment_solo", "Momentum Breakout Solo"),
        ("moment_opt", "Momentum Breakout Optimization"),
        ("moment_comb", "Momentum Breakout Combined"),
        ("moment_oos", "Momentum Breakout OOS Validation"),
        ("short__solo", "Short Term MR Solo"),
        ("short__opt", "Short Term MR Optimization"),
        ("short__comb", "Short Term MR Combined"),
        ("short__oos", "Short Term MR OOS Validation"),
        ("sector_solo", "Sector Rotation Solo"),
        ("sector_opt", "Sector Rotation Optimization"),
        ("sector_comb", "Sector Rotation Combined"),
        ("sector_oos", "Sector Rotation OOS Validation"),
        ("mtf_mo_solo", "MTF Momentum Solo"),
        ("mtf_mo_opt", "MTF Momentum Optimization"),
        ("mtf_mo_comb", "MTF Momentum Combined"),
        ("mtf_mo_oos", "MTF Momentum OOS Validation"),
        ("bb_squ_solo", "BB Squeeze Solo"),
        ("bb_squ_opt", "BB Squeeze Optimization"),
        ("bb_squ_comb", "BB Squeeze Combined"),
        ("bb_squ_oos", "BB Squeeze OOS Validation"),
        ("rsi2_solo", "ConnorsRSI2 Solo"),
        ("rsi2_opt", "ConnorsRSI2 Optimization"),
        ("trsi_solo", "Triple RSI Solo"),
        ("trsi_opt", "Triple RSI Optimization"),
        ("trsi_comb", "Triple RSI Combined"),
        ("lbr_combined", "LBR Combined"),
        ("lbr_opt", "LBR Optimization"),
        ("lbr_oos", "LBR OOS Validation"),
        ("mr_solo", "Mean Reversion Solo"),
        ("mr_opt", "Mean Reversion Optimization"),
        ("tf_solo", "Trend Following Solo"),
    ]
    for pattern, replacement in subs:
        if title == pattern:
            return replacement

    # Fallback: replace underscores with spaces and title-case
    return title.replace("_", " ").title()


def fmt_float(val: Any, decimals: int = 2) -> str:
    """Format a numeric value or return 'N/A'."""
    if val is None:
        return "N/A"
    try:
        return f"{float(val):.{decimals}f}"
    except (TypeError, ValueError):
        return str(val)


def yaml_str(val: Any) -> str:
    """Render a YAML scalar value (basic single-line)."""
    if val is None:
        return "null"
    if isinstance(val, bool):
        return "true" if val else "false"
    if isinstance(val, (int, float)):
        return str(val)
    s = str(val)
    # Quote strings that contain YAML special characters
    needs_quote = any(c in s for c in (':', '#', '[', ']', '{', '}', ',', '&', '*', '?',
                                        '|', '-', '<', '>', '=', '!', '%', '@', '`',
                                        '\n', '"', "'"))
    if needs_quote:
        escaped = s.replace('"', '\\"')
        return f'"{escaped}"'
    return s


def build_frontmatter(fields: dict[str, Any]) -> str:
    """Build YAML frontmatter block from an ordered dict."""
    lines = ["---"]
    for key, val in fields.items():
        if isinstance(val, list):
            lines.append(f"{key}:")
            for item in val:
                lines.append(f"  - {yaml_str(item)}")
        else:
            lines.append(f"{key}: {yaml_str(val)}")
    lines.append("---")
    return "\n".join(lines)


def parse_date(timestamp: Optional[str]) -> str:
    """Extract YYYY-MM-DD from an ISO timestamp string."""
    if not timestamp:
        return ""
    return str(timestamp)[:10]


# ─── Data Loaders ────────────────────────────────────────────────────────────


def load_journal(journal_path: Path) -> dict[str, dict]:
    """Load journal.json; deduplicate by experiment_id keeping latest timestamp."""
    if not journal_path.exists():
        print(f"  WARNING: journal not found: {journal_path}", file=sys.stderr)
        return {}

    raw: list[dict] = json.loads(journal_path.read_text())
    seen: dict[str, dict] = {}
    for entry in raw:
        eid = entry.get("experiment_id", "")
        if not eid:
            continue
        existing = seen.get(eid)
        # Keep the latest timestamp; if equal, keep the last occurrence
        if existing is None or entry.get("timestamp", "") >= existing.get("timestamp", ""):
            seen[eid] = entry
    return seen


def load_experiment_envelopes(experiments_dir: Path) -> dict[str, dict]:
    """Load exp-*.json and eval-*.json experiment envelopes keyed by id."""
    envelopes: dict[str, dict] = {}
    if not experiments_dir.exists():
        return envelopes
    for f in experiments_dir.glob("*.json"):
        if not f.name.startswith(("exp-", "eval-")):
            continue
        try:
            data = json.loads(f.read_text())
            eid = data.get("id", "")
            if eid:
                # Prefer richer (larger) file when duplicates exist
                if eid not in envelopes or len(str(data)) > len(str(envelopes[eid])):
                    envelopes[eid] = data
        except Exception as e:
            print(f"  WARNING: failed to load {f.name}: {e}", file=sys.stderr)
    return envelopes


def load_wave_briefs(waves_dir: Path) -> list[dict]:
    """Load wave_*_brief.json files sorted by wave number."""
    briefs: list[dict] = []
    if not waves_dir.exists():
        return briefs
    for f in sorted(waves_dir.glob("wave_*_brief.json")):
        try:
            briefs.append(json.loads(f.read_text()))
        except Exception as e:
            print(f"  WARNING: failed to load {f.name}: {e}", file=sys.stderr)
    return briefs


def get_wave_experiments(brief: dict, journal: dict[str, dict]) -> list[str]:
    """Extract experiment IDs from a wave brief (handles both list-of-str and list-of-obj)."""
    raw = brief.get("experiments", [])
    ids: list[str] = []
    for item in raw:
        if isinstance(item, str):
            ids.append(item)
        elif isinstance(item, dict):
            eid = item.get("id", "")
            if eid:
                ids.append(eid)
    return ids


# ─── Note Generators ─────────────────────────────────────────────────────────


def build_experiment_note(
    entry: dict,
    envelope: Optional[dict],
    output_dir: Path,
    force: bool,
) -> bool:
    """Generate a single experiment Markdown note. Returns True if written."""
    eid = entry.get("experiment_id", "")
    if not eid:
        return False

    outfile = output_dir / f"{eid}.md"
    if outfile.exists() and not force:
        return False

    # ── Field extraction ───────────────────────────────────────────────────
    strategy_raw = entry.get("strategy") or "portfolio_filter"
    wave_n = get_wave_number(eid)
    metrics = entry.get("key_metrics") or {}
    timestamp = entry.get("timestamp", "")
    date_str = parse_date(timestamp)
    verdict = entry.get("verdict", "unknown")
    promoted = entry.get("promoted", False)
    category = entry.get("category", "")
    market = entry.get("market", "sp500")

    # Flexible metric accessor (handles varied naming)
    def m(key: str, *alts: str) -> Any:
        v = metrics.get(key)
        if v is not None:
            return v
        for alt in alts:
            v = metrics.get(alt)
            if v is not None:
                return v
        return None

    sharpe = m("sharpe", "combined_sharpe")
    cagr = m("cagr_pct", "cagr", "combined_cagr")
    max_dd = m("max_drawdown_pct", "max_dd", "combined_dd", "max_dd_pct")
    win_rate = m("win_rate_pct", "wr")
    total_trades = m("total_trades", "trades")
    profit_factor = m("profit_factor", "pf")
    total_pnl = m("total_pnl")
    avg_trade = m("avg_trade")
    sortino = m("sortino")
    final_equity = m("final_equity")

    # ── YAML frontmatter ───────────────────────────────────────────────────
    tags = [
        "experiment",
        f"strategy/{strategy_to_kebab(strategy_raw)}",
        f"verdict/{verdict}",
    ]
    if wave_n:
        tags.append(f"wave/{wave_n}")
    if category:
        tags.append(f"category/{category}")
    if market:
        tags.append(f"market/{market}")

    fm: dict[str, Any] = {"experiment_id": eid}
    if wave_n:
        fm["wave"] = wave_n
    fm["strategy"] = strategy_raw
    fm["category"] = category or "unknown"
    fm["market"] = market
    fm["verdict"] = verdict
    fm["promoted"] = promoted

    if sharpe is not None:
        try:
            fm["sharpe"] = round(float(sharpe), 4)
        except (TypeError, ValueError):
            pass
    if cagr is not None:
        try:
            fm["cagr"] = round(float(cagr), 2)
        except (TypeError, ValueError):
            pass
    if max_dd is not None:
        try:
            fm["max_drawdown"] = round(float(max_dd), 2)
        except (TypeError, ValueError):
            pass
    if win_rate is not None:
        try:
            fm["win_rate"] = round(float(win_rate), 2)
        except (TypeError, ValueError):
            pass
    if total_trades is not None:
        try:
            fm["total_trades"] = int(total_trades)
        except (TypeError, ValueError):
            pass
    if profit_factor is not None:
        try:
            fm["profit_factor"] = round(float(profit_factor), 4)
        except (TypeError, ValueError):
            pass
    if total_pnl is not None:
        try:
            fm["total_pnl"] = round(float(total_pnl), 2)
        except (TypeError, ValueError):
            pass
    if date_str:
        fm["date"] = date_str
    fm["tags"] = tags

    # ── Body ───────────────────────────────────────────────────────────────
    title = derive_title(eid)
    strategy_human = strategy_to_human(strategy_raw)
    wave_link = f"[[Wave {wave_n}]]" if wave_n else "N/A"

    # Use envelope hypothesis if richer
    hypothesis = entry.get("hypothesis", "")
    if envelope:
        qe_hyp = (envelope.get("queue_entry") or {}).get("hypothesis", "")
        if qe_hyp and len(qe_hyp) > len(hypothesis):
            hypothesis = qe_hyp

    lines: list[str] = [
        build_frontmatter(fm),
        "",
        f"# {title}",
        "",
        (
            f"> **Wave:** {wave_link} | **Strategy:** [[{strategy_human}]] | "
            f"**Verdict:** `{verdict.upper()}` | **Promoted:** {'✅' if promoted else '❌'}"
        ),
        "",
    ]

    # Hypothesis
    if hypothesis:
        lines += ["## Hypothesis", "", hypothesis, ""]

    # Results table
    lines += ["## Results", ""]
    has_metrics = any(
        v is not None
        for v in [sharpe, cagr, max_dd, win_rate, total_trades, profit_factor, total_pnl, avg_trade, sortino]
    )

    if has_metrics:
        lines += ["| Metric | Value |", "|--------|-------|"]
        rows = [
            ("Sharpe", fmt_float(sharpe)),
            ("Sortino", fmt_float(sortino)),
            ("CAGR", f"{fmt_float(cagr)}%"),
            ("Max Drawdown", f"{fmt_float(max_dd)}%"),
            ("Win Rate", f"{fmt_float(win_rate)}%"),
            ("Profit Factor", fmt_float(profit_factor)),
            ("Total Trades", str(int(total_trades)) if total_trades is not None else "N/A"),
            ("Total PnL", f"${fmt_float(total_pnl)}"),
            ("Avg Trade", f"${fmt_float(avg_trade)}"),
            ("Final Equity", f"${fmt_float(final_equity)}"),
        ]
        for label, val in rows:
            if val not in ("N/A", "$N/A", "N/A%"):
                lines.append(f"| {label} | {val} |")

        # Combined vs baseline block
        if metrics.get("combined_sharpe") is not None and metrics.get("baseline_sharpe") is not None:
            lines += [
                "",
                "**Combined vs Baseline:**",
                "",
                "| Metric | Baseline | Combined | Delta |",
                "|--------|----------|----------|-------|",
                (
                    f"| Sharpe | {fmt_float(metrics.get('baseline_sharpe'))} | "
                    f"{fmt_float(metrics.get('combined_sharpe'))} | "
                    f"{fmt_float(metrics.get('delta_sharpe'))} |"
                ),
                (
                    f"| CAGR | {fmt_float(metrics.get('baseline_cagr'))}% | "
                    f"{fmt_float(metrics.get('combined_cagr'))}% | "
                    f"{fmt_float(metrics.get('delta_cagr'))}% |"
                ),
                (
                    f"| Max DD | {fmt_float(metrics.get('baseline_dd'))}% | "
                    f"{fmt_float(metrics.get('combined_dd'))}% | — |"
                ),
            ]
    else:
        lines.append("*No metrics recorded for this experiment.*")
    lines.append("")

    # Strategy breakdown
    breakdown = metrics.get("strategy_breakdown")
    if isinstance(breakdown, dict) and breakdown:
        lines += ["## Strategy Breakdown", ""]
        lines += ["| Strategy | Trades | Total PnL | Win Rate |", "|----------|--------|-----------|----------|"]
        for strat_name, strat_data in breakdown.items():
            if isinstance(strat_data, dict):
                t = strat_data.get("trades", "N/A")
                p = fmt_float(strat_data.get("total_pnl"))
                w = fmt_float(strat_data.get("win_rate_pct"))
                lines.append(f"| {strat_name} | {t} | ${p} | {w}% |")
        lines.append("")

    # Verdict section
    verdict_rationale = ""
    acceptance_desc = ""
    if envelope:
        verdict_rationale = envelope.get("verdict_rationale", "")
        qe = envelope.get("queue_entry") or {}
        ac = qe.get("acceptance_criteria") or {}
        if isinstance(ac, dict):
            acceptance_desc = ac.get("description", "")
        elif isinstance(ac, str):
            acceptance_desc = ac

    lines += ["## Verdict", "", f"**{verdict.upper()}**"]
    if acceptance_desc:
        lines += ["", f"*Criteria:* {acceptance_desc}"]
    if verdict_rationale:
        lines += ["", verdict_rationale]
    lines.append("")

    # Delta vs baseline
    delta = entry.get("delta_vs_baseline") or {}
    if delta:
        lines += ["## Delta vs Baseline", ""]
        lines += ["| Metric | Change |", "|--------|--------|"]
        for k, v in delta.items():
            lines.append(f"| {k.replace('_', ' ').title()} | {fmt_float(v)} |")
        lines.append("")

    # Learnings (merge journal + envelope, deduplicated)
    learnings: list[str] = list(entry.get("learnings") or [])
    if envelope:
        env_learnings = envelope.get("learnings") or []
        seen_l: set[str] = set(learnings)
        for l in env_learnings:
            if l not in seen_l:
                learnings.append(l)
                seen_l.add(l)

    if learnings:
        lines += ["## Learnings", ""]
        for learning in learnings:
            lines.append(f"- {learning}")
        lines.append("")

    # Footer links
    lines += ["---", "", f"Strategy:: [[{strategy_human}]]"]
    if wave_n:
        lines.append(f"Wave:: [[Wave {wave_n}]]")

    outfile.write_text("\n".join(lines))
    return True


def build_strategy_notes(
    journal: dict[str, dict],
    output_dir: Path,
    force: bool,
) -> int:
    """Generate one note per unique strategy. Returns count of notes written."""
    # Group experiments by strategy
    by_strategy: dict[str, list[dict]] = defaultdict(list)
    for eid, entry in journal.items():
        strat = entry.get("strategy") or "portfolio_filter"
        by_strategy[strat].append(entry)

    # Strategy descriptions
    strategy_descriptions: dict[str, str] = {
        "mean_reversion": (
            "RSI(14) + z-score mean reversion on individual SP500 stocks. "
            "Core active strategy. Enters oversold reversals with ATR-based stop losses. "
            "Optimized via coordinate descent to Sharpe 1.04, CAGR 15.69% (v2.0)."
        ),
        "trend_following": (
            "Fast/slow MA crossover trend following with pullback entries. "
            "Core active strategy. Enters higher-probability pullbacks within confirmed uptrends."
        ),
        "opening_gap": (
            "Gap-and-fill strategy targeting gap-down opens with IBS confirmation. "
            "Core active strategy. Short holding periods (2-7 days)."
        ),
        "momentum_breakout": (
            "N-day high breakout with trend MA alignment. "
            "Passes solo tests (Sharpe 0.30, CAGR 8.0%) after optimization. "
            "Degrades combined portfolio due to position slot contention."
        ),
        "short_term_mr": (
            "RSI(2)/IBS rapid 1-5 day reversion strategy (Connors-style). "
            "Passes solo tests (Sharpe 0.27, CAGR 7.6%, 63% WR) after optimization. "
            "Degrades combined portfolio due to position slot contention."
        ),
        "sector_rotation": (
            "Top-down momentum sector rotation. "
            "Selects strongest GICS sectors by momentum, buys strongest stocks within them. "
            "Passes solo (Sharpe 0.43) but degrades combined portfolio."
        ),
        "mtf_momentum": (
            "Multi-timeframe momentum: daily pullbacks within weekly uptrends. "
            "Has API bugs (generate_signals signature mismatch). Needs code fix before retesting."
        ),
        "bb_squeeze": (
            "Bollinger Band Squeeze (BB inside Keltner Channel). "
            "Identifies low-volatility regimes preceding explosive directional moves. "
            "Marginally viable after optimization; not currently in active portfolio."
        ),
        "connors_rsi2": (
            "ConnorsRSI2 mean reversion strategy designed for ETFs. "
            "Fails on individual SP500 stocks. Not viable as-designed — see ETF Adaptation Fails pattern."
        ),
        "lower_band_reversion": (
            "IBS-based lower band reversion (Quantitativo LBR). "
            "Published Sharpe 2.11 on SPY. On individual stocks: Sharpe -2.08. "
            "Classic ETF-to-stock adaptation failure."
        ),
        "triple_rsi": (
            "Triple RSI (modified Connors R3): RSI(5)<30 + 3 consecutive declining days. "
            "Published 90% WR, PF 4.0 on SPY. "
            "Testing on individual stocks in Wave 3."
        ),
        "sma200_filter": (
            "SMA-200 filter: only enter when stock is above its 200-day moving average. "
            "Biggest filter win: +0.28 Sharpe improvement. Promoted to v2.1 config."
        ),
        "combined": (
            "Combined portfolio (MR + TF + OG + additional strategies). "
            "Baseline: Sharpe 0.59 (v2.0). With SMA-200 filter: Sharpe 0.87 (v2.1)."
        ),
        "portfolio_filter": (
            "Portfolio-level filter experiments (VIX, volume, cross-market, turn-of-month). "
            "Tests portfolio-wide regime or entry filters."
        ),
    }

    written = 0
    for strat_id, experiments in sorted(by_strategy.items()):
        human_name = strategy_to_human(strat_id)
        outfile = output_dir / f"{human_name}.md"
        if outfile.exists() and not force:
            continue

        status = get_strategy_status(strat_id)
        kebab = strategy_to_kebab(strat_id)

        # Aggregate stats
        sharpes = []
        for e in experiments:
            km = e.get("key_metrics") or {}
            s = km.get("sharpe")
            if s is not None:
                try:
                    sharpes.append(float(s))
                except (TypeError, ValueError):
                    pass

        best_sharpe = max(sharpes) if sharpes else None
        worst_sharpe = min(sharpes) if sharpes else None
        avg_sharpe = (sum(sharpes) / len(sharpes)) if sharpes else None

        verdicts = [e.get("verdict", "unknown") for e in experiments]
        pass_count = sum(1 for v in verdicts if v == "pass")
        fail_count = sum(1 for v in verdicts if v == "fail")
        partial_count = sum(1 for v in verdicts if v == "partial")
        promoted_count = sum(1 for e in experiments if e.get("promoted"))

        fm: dict[str, Any] = {
            "strategy_id": strat_id,
            "type": "strategy",
            "status": status,
            "total_experiments": len(experiments),
        }
        if best_sharpe is not None:
            fm["best_sharpe"] = round(best_sharpe, 4)
        fm["tags"] = ["strategy", f"strategy/{kebab}"]

        lines: list[str] = [
            build_frontmatter(fm),
            "",
            f"# {human_name}",
            "",
            (
                f"> **Status:** `{status.upper()}` | **Experiments:** {len(experiments)} | "
                f"**Promotions:** {promoted_count}"
            ),
            "",
            "## Overview",
            "",
            strategy_descriptions.get(strat_id, f"Research strategy `{strat_id}`. See experiments below."),
            "",
        ]

        # Aggregate metrics block
        if sharpes:
            lines += [
                "## Aggregate Metrics",
                "",
                "| Metric | Value |",
                "|--------|-------|",
                f"| Best Sharpe | {fmt_float(best_sharpe)} |",
                f"| Worst Sharpe | {fmt_float(worst_sharpe)} |",
                f"| Avg Sharpe | {fmt_float(avg_sharpe)} |",
                f"| Total Experiments | {len(experiments)} |",
                f"| Pass / Partial / Fail | {pass_count} / {partial_count} / {fail_count} |",
                f"| Promotions | {promoted_count} |",
                "",
            ]

        # Experiments table sorted by date
        sorted_exps = sorted(experiments, key=lambda e: e.get("timestamp", ""))
        lines += [
            "## Experiments",
            "",
            "| Experiment | Wave | Verdict | Sharpe | CAGR | Promoted |",
            "|------------|------|---------|--------|------|----------|",
        ]
        for exp in sorted_exps:
            exp_id = exp.get("experiment_id", "")
            exp_wave = get_wave_number(exp_id) or "?"
            exp_verdict = exp.get("verdict", "?")
            km = exp.get("key_metrics") or {}
            exp_sharpe = km.get("sharpe") or km.get("combined_sharpe")
            exp_cagr = km.get("cagr_pct") or km.get("cagr") or km.get("combined_cagr")
            exp_prom = "✅" if exp.get("promoted") else ""
            lines.append(
                f"| [[{exp_id}]] | {exp_wave} | `{exp_verdict}` | "
                f"{fmt_float(exp_sharpe)} | {fmt_float(exp_cagr)}% | {exp_prom} |"
            )

        lines.append("")

        outfile.write_text("\n".join(lines))
        written += 1

    return written


def build_wave_notes(
    briefs: list[dict],
    journal: dict[str, dict],
    output_dir: Path,
    force: bool,
) -> int:
    """Generate one note per research wave. Returns count written."""
    brief_map: dict[int, dict] = {b.get("wave_number"): b for b in briefs if b.get("wave_number")}

    # Discover all wave numbers present in journal
    journal_waves: set[int] = set()
    for eid in journal:
        wn = get_wave_number(eid)
        if wn:
            journal_waves.add(wn)

    all_wave_numbers = sorted(journal_waves | set(brief_map.keys()))

    # Synthetic brief for wave 2 (no brief file exists)
    wave2_exps = sorted([eid for eid in journal if get_wave_number(eid) == 2])
    synthetic_briefs: dict[int, dict] = {
        2: {
            "wave_number": 2,
            "generated_at": "2026-03-03T23:00:00+00:00",
            "status": "complete",
            "theme": "Exit Rule Optimization & Volume/Timing Filters",
            "theme_rationale": (
                "Wave 2 tested exit rule improvements (Chandelier TF stop, MR/OG exit rules), "
                "a volume-combined portfolio test, and a turn-of-month timing filter. "
                "Key finding: ConnorsRSI2 solo test shows ETF strategy adaptation fails on individual stocks."
            ),
            "experiments": wave2_exps,
            "key_findings_so_far": [],
        }
    }

    written = 0
    for wave_n in all_wave_numbers:
        outfile = output_dir / f"Wave {wave_n}.md"
        if outfile.exists() and not force:
            continue

        brief = brief_map.get(wave_n) or synthetic_briefs.get(wave_n) or {}
        status = brief.get("status", "complete")
        if status == "in_progress":
            status = "complete"

        theme = brief.get("theme", f"Wave {wave_n}")
        generated_at = brief.get("generated_at", "")
        start_date = parse_date(generated_at)
        theme_rationale = brief.get("theme_rationale", "")
        key_findings = brief.get("key_findings_so_far") or []

        # Combine brief experiment list with journal-discovered IDs
        brief_exp_ids = get_wave_experiments(brief, journal)
        journal_wave_ids = [eid for eid in journal if get_wave_number(eid) == wave_n]
        # Deduplicate preserving order (brief IDs first)
        seen_ids: set[str] = set()
        all_exp_ids: list[str] = []
        for eid in brief_exp_ids + journal_wave_ids:
            if eid not in seen_ids:
                all_exp_ids.append(eid)
                seen_ids.add(eid)

        fm: dict[str, Any] = {
            "wave": wave_n,
            "status": status,
            "theme": theme,
        }
        if start_date:
            fm["start_date"] = start_date
        fm["experiment_count"] = len(all_exp_ids)
        fm["tags"] = ["wave", f"wave/{wave_n}"]

        lines: list[str] = [
            build_frontmatter(fm),
            "",
            f"# Wave {wave_n}: {theme}",
            "",
            (
                f"> **Status:** `{status.upper()}` | "
                f"**Experiments:** {len(all_exp_ids)} | "
                f"**Started:** {start_date}"
            ),
            "",
        ]

        if theme_rationale:
            lines += ["## Theme Rationale", "", theme_rationale, ""]

        # Experiments table
        lines += [
            "## Experiments",
            "",
            "| Experiment | Verdict | Strategy | Sharpe | Promoted |",
            "|------------|---------|----------|--------|----------|",
        ]
        for eid in all_exp_ids:
            entry = journal.get(eid)
            if entry:
                exp_verdict = entry.get("verdict", "?")
                exp_strat = entry.get("strategy") or "portfolio_filter"
                km = entry.get("key_metrics") or {}
                exp_sharpe = km.get("sharpe") or km.get("combined_sharpe")
                exp_prom = "✅" if entry.get("promoted") else ""
                lines.append(
                    f"| [[{eid}]] | `{exp_verdict}` | {strategy_to_human(exp_strat)} | "
                    f"{fmt_float(exp_sharpe)} | {exp_prom} |"
                )
            else:
                lines.append(f"| [[{eid}]] | — | — | — | |")

        lines.append("")

        if key_findings:
            lines += ["## Key Findings", ""]
            for finding in key_findings:
                lines.append(f"- {finding}")
            lines.append("")

        outfile.write_text("\n".join(lines))
        written += 1

    return written


def build_pattern_notes(output_dir: Path, force: bool) -> int:
    """Generate confirmed research pattern notes. Returns count written."""
    patterns: list[dict[str, Any]] = [
        {
            "filename": "Fee Drag at Low Equity.md",
            "title": "Fee Drag at Low Equity",
            "discovered_in": "Wave 1",
            "body": """\
At $4K equity, Moomoo fees ($1.10/trade) eat 74% of gross profit. Need ~$10K for viable Moomoo or switch to Alpaca ($0 commission).

## Finding

With $4,000 starting equity and Moomoo's $1.10/trade flat fee:
- Average trade profit ~$1.50 gross
- Fee eats 74% of each win
- Net expectancy turns negative despite a positive gross edge

## Implication

- Need ~$10K+ equity for Moomoo fees to become manageable (<20% of avg win)
- **OR** switch to Alpaca ($0 commission) to run at $4K equity

## Resolution

Switch broker to Alpaca for commission-free trading at current equity levels.
Moomoo viable only at $10K+ or with larger average trade sizes.

## Related

- [[Wave 1]]
""",
        },
        {
            "filename": "ETF Strategy Adaptation Fails.md",
            "title": "ETF Strategy Adaptation Fails",
            "discovered_in": "Wave 2",
            "body": """\
ConnorsRSI2 and LBR designed for ETFs fail on individual stocks. Don't adapt ETF strategies to stocks.

## Finding

Strategies published for ETFs (SPY, QQQ) do not transfer directly to individual SP500 stocks:

- **ConnorsRSI2**: Designed for ETF mean reversion. On individual stocks generates insufficient trades and negative Sharpe.
- **Lower Band Reversion (LBR)**: Published Sharpe 2.11 on SPY. On individual stocks: Sharpe -2.08, despite 58% win rate.

## Root Cause

ETFs have smoother price action, stronger mean reversion properties, and lower volatility per unit than individual stocks.
Strategies calibrated for ETF distributions underfit the noisier individual stock signals.

## Implication

Do not directly adapt ETF-backtested strategies to individual stocks without:
1. Re-parameterizing for individual stock distributions
2. Adding stock-specific filters (earnings blackouts, liquidity)
3. Accepting significantly reduced signal quality

## Related

- [[Wave 2]]
- [[Wave 4]]
""",
        },
        {
            "filename": "Position Slot Contention.md",
            "title": "Position Slot Contention",
            "discovered_in": "Wave 1",
            "body": """\
All 4 dormant strategies fail combined portfolio due to slot contention at max_positions=10. Need allocation pools before adding strategies.

## Finding

Every dormant strategy tested in waves 1-3 passed the solo test but failed when added to the combined portfolio:

| Strategy | Solo Sharpe | Combined Sharpe Delta |
|----------|-------------|----------------------|
| Momentum Breakout | 0.30 | -0.75 |
| Short Term MR | 0.27 | -0.29 |
| BB Squeeze | ~0 | degraded |
| Sector Rotation | 0.43 | degraded |

## Root Cause

`max_open_positions=10` is a zero-sum constraint. When a new strategy adds 200-700 trades/year,
it competes directly for position slots with the proven MR+TF+OG strategies that drive returns.

## Implication

**Do not add more strategies until allocation pools are implemented.**
Need per-strategy position caps (e.g., MR gets 5 slots, TF gets 3, new strategy gets 2).

## Resolution Path

Implement allocation pools feature to partition position slots per strategy type.
This unlocks all dormant strategies that passed solo testing.

## Related

- [[Wave 1]]
- [[Wave 3]]
""",
        },
        {
            "filename": "SMA-200 Filter Win.md",
            "title": "SMA-200 Filter Win",
            "discovered_in": "Wave 1",
            "body": """\
Biggest filter win: +0.28 Sharpe improvement, promoted to v2.1 config.

## Finding

Adding SMA-200 filter (only enter trades when stock is above its 200-day moving average) to all 3 active strategies:

| Metric | Before | After | Delta |
|--------|--------|-------|-------|
| Sharpe | 0.59 | 0.87 | **+0.28** |
| CAGR | 10.1% | 11.7% | +1.6pp |
| Max DD | 6.6% | 5.3% | -1.2pp |
| Trades | 443 | 270 | -39% |

## Why It Works

SMA-200 filters out entries in downtrending stocks. When a stock is below its 200-day MA:
- More likely to continue lower (trend confirmation)
- Mean reversion entries face stronger headwinds
- Breakout entries have lower follow-through

## Note

Previous coordinate descent optimization **rejected** SMA-200 because it reduces trade count too aggressively
(optimizer penalizes low trade counts). The filter only shows its value in a clean A/B test.

## Promotion

Promoted to `config/versions/sp500_v2.1.json`. Applied to mean_reversion, trend_following, and opening_gap.

## Related

- [[Wave 1]]
- [[SMA-200 Filter]]
""",
        },
        {
            "filename": "VIX Filter Counterproductive.md",
            "title": "VIX Filter Counterproductive",
            "discovered_in": "Wave 1",
            "body": """\
VIX filter is counterproductive for MR-heavy portfolio because MR needs high-VIX panic periods.

## Finding

All 4 VIX threshold levels tested (20, 25, 30, 35) degrade portfolio performance.

## Root Cause

**Mean reversion thrives during high-VIX (panic) periods.** When VIX is high:
- Stocks are oversold, creating large z-score dislocations
- Reversal probability is highest
- MR generates its best signals

Applying a VIX filter blocks entries precisely when MR alpha is highest.

## Implication

- **CLOSED**: Do not re-test VIX filters on combined portfolio
- VIX filter might work for a **trend-only** portfolio (trends break down in panic)
- For MR-heavy portfolios: VIX is a signal TO enter, not to avoid

## Related

- [[Wave 1]]
- [[Wave 3]]
""",
        },
    ]

    written = 0
    for pattern in patterns:
        outfile = output_dir / pattern["filename"]
        if outfile.exists() and not force:
            continue

        discovered_in = pattern["discovered_in"]

        fm: dict[str, Any] = {
            "type": "pattern",
            "status": "confirmed",
            "impact": "high",
            "discovered_in": f"[[{discovered_in}]]",
            "tags": ["pattern", "pattern/confirmed"],
        }

        lines: list[str] = [
            build_frontmatter(fm),
            "",
            f"# {pattern['title']}",
            "",
            f"> **Type:** Pattern | **Status:** Confirmed | **Impact:** High | **Discovered:** [[{discovered_in}]]",
            "",
            pattern["body"],
        ]

        outfile.write_text("\n".join(lines))
        written += 1

    return written


# ─── Main ─────────────────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate Obsidian vault notes from Atlas research data."
    )
    parser.add_argument(
        "--output-dir",
        default="research/vault/",
        help="Output directory for vault notes (default: research/vault/)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing notes",
    )
    parser.add_argument(
        "--root",
        default=str(ATLAS_ROOT),
        help="Atlas project root directory (default: script's parent directory)",
    )
    args = parser.parse_args()

    root = Path(args.root).resolve()
    output_dir = (root / args.output_dir).resolve() if not Path(args.output_dir).is_absolute() else Path(args.output_dir).resolve()

    # Data paths
    journal_path = root / "research" / "journal.json"
    experiments_dir = root / "research" / "experiments"
    waves_dir = root / "research" / "waves"

    print("Atlas Obsidian Vault Generator")
    print(f"  Root:          {root}")
    print(f"  Output dir:    {output_dir}")
    print(f"  Force:         {args.force}")
    print()

    # Create output subdirectories
    for subdir in ("Experiments", "Strategies", "Waves", "Patterns"):
        (output_dir / subdir).mkdir(parents=True, exist_ok=True)

    # ── Load data ──────────────────────────────────────────────────────────
    print("Loading data sources...")
    journal = load_journal(journal_path)
    print(f"  Journal entries:       {len(journal)} unique experiment IDs")

    envelopes = load_experiment_envelopes(experiments_dir)
    print(f"  Experiment envelopes:  {len(envelopes)}")

    wave_briefs = load_wave_briefs(waves_dir)
    print(f"  Wave briefs:           {len(wave_briefs)}")
    print()

    # ── Experiments ────────────────────────────────────────────────────────
    print("Generating Experiment notes...")
    exp_written = 0
    exp_skipped = 0
    for eid, entry in sorted(journal.items()):
        wrote = build_experiment_note(
            entry=entry,
            envelope=envelopes.get(eid),
            output_dir=output_dir / "Experiments",
            force=args.force,
        )
        if wrote:
            exp_written += 1
        else:
            exp_skipped += 1
    print(f"  Written: {exp_written}  Skipped (already exists): {exp_skipped}")

    # ── Strategies ─────────────────────────────────────────────────────────
    print("Generating Strategy notes...")
    strat_written = build_strategy_notes(
        journal=journal,
        output_dir=output_dir / "Strategies",
        force=args.force,
    )
    print(f"  Written: {strat_written}")

    # ── Waves ──────────────────────────────────────────────────────────────
    print("Generating Wave notes...")
    wave_written = build_wave_notes(
        briefs=wave_briefs,
        journal=journal,
        output_dir=output_dir / "Waves",
        force=args.force,
    )
    print(f"  Written: {wave_written}")

    # ── Patterns ───────────────────────────────────────────────────────────
    print("Generating Pattern notes...")
    pattern_written = build_pattern_notes(
        output_dir=output_dir / "Patterns",
        force=args.force,
    )
    print(f"  Written: {pattern_written}")

    # ── Summary ────────────────────────────────────────────────────────────
    total = exp_written + strat_written + wave_written + pattern_written
    print()
    print("=" * 50)
    print("Vault generation complete:")
    print(f"  Experiments:  {exp_written} notes")
    print(f"  Strategies:   {strat_written} notes")
    print(f"  Waves:        {wave_written} notes")
    print(f"  Patterns:     {pattern_written} notes")
    print(f"  Total:        {total} notes")
    print(f"  Location:     {output_dir}")


if __name__ == "__main__":
    main()
