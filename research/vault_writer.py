#!/usr/bin/env python3
"""
Atlas Vault Writer — Incremental Obsidian note writer.

Writes individual Markdown notes to research/vault/ in real-time (not batch rebuild).
Matches the vault format established by scripts/build_obsidian_vault.py.

Usage:
    from research.vault_writer import (
        write_experiment_note, update_strategy_card,
        write_parameter_insight, write_daily_log, update_knowledge_base
    )
"""

import json
import re
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

# ─── Constants ───────────────────────────────────────────────────────────────

ATLAS_ROOT = Path(__file__).resolve().parent.parent

VAULT_ROOT = ATLAS_ROOT / "research" / "vault"

JOURNAL_PATH = ATLAS_ROOT / "research" / "journal.json"

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

ACTIVE_STRATEGIES = {"mean_reversion", "trend_following", "opening_gap"}
FILTER_STRATEGIES = {"sma200_filter", "portfolio_filter", "combined"}


# ─── Helpers (adapted from build_obsidian_vault.py) ──────────────────────────


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


def _load_journal_for_strategy(strategy_id: str) -> list[dict]:
    """Load journal entries for a specific strategy."""
    if not JOURNAL_PATH.exists():
        return []
    try:
        raw: list[dict] = json.loads(JOURNAL_PATH.read_text())
        return [e for e in raw if e.get("strategy") == strategy_id]
    except (json.JSONDecodeError, OSError):
        return []


def _load_all_journal() -> dict[str, dict]:
    """Load journal.json, deduplicate by experiment_id keeping latest timestamp."""
    if not JOURNAL_PATH.exists():
        return {}
    try:
        raw: list[dict] = json.loads(JOURNAL_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return {}
    seen: dict[str, dict] = {}
    for entry in raw:
        eid = entry.get("experiment_id", "")
        if not eid:
            continue
        existing = seen.get(eid)
        if existing is None or entry.get("timestamp", "") >= existing.get("timestamp", ""):
            seen[eid] = entry
    return seen


# ─── Note Generators ─────────────────────────────────────────────────────────


def write_experiment_note(
    experiment_id: str,
    journal_entry: dict,
    envelope: dict = None,
    output_dir: Path = None,
) -> Path:
    """Write a single experiment note to Experiments/{id}.md. Overwrites if exists.

    Args:
        experiment_id: Experiment ID string (e.g. 'wave5_mr_solo')
        journal_entry: Dict with keys: strategy, verdict, key_metrics, hypothesis,
                       learnings, market, category, timestamp
        envelope: Optional envelope dict with acceptance_criteria, method, param_grid,
                  baseline_comparison fields
        output_dir: Output directory (defaults to VAULT_ROOT / 'Experiments')

    Returns:
        Path to the written file.
    """
    if output_dir is None:
        output_dir = VAULT_ROOT / "Experiments"
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    eid = experiment_id
    entry = journal_entry

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
    strategy_human = strategy_to_human(strategy_raw)
    wave_link = f"[[Wave {wave_n}]]" if wave_n else "N/A"
    title_parts = re.sub(r"^wave\d+_", "", eid).replace("_", " ").title()
    title = title_parts

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

    # Envelope metadata (acceptance criteria, method, param_grid)
    if envelope:
        method = envelope.get("method") or (envelope.get("queue_entry") or {}).get("method", "")
        param_grid = envelope.get("param_grid") or (envelope.get("inputs") or {}).get("param_grid")
        if method or param_grid:
            lines += ["## Experiment Setup", ""]
            if method:
                lines.append(f"- **Method:** `{method}`")
            if param_grid and isinstance(param_grid, dict):
                grid_str = ", ".join(f"`{k}`: {v}" for k, v in param_grid.items())
                lines.append(f"- **Param Grid:** {grid_str}")
            lines.append("")

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

    # Learnings
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

    outfile = output_dir / f"{eid}.md"
    outfile.write_text("\n".join(lines))
    return outfile


def update_strategy_card(
    strategy_id: str,
    journal_entries: list = None,
    output_dir: Path = None,
) -> Path:
    """Update (or create) a strategy card at Strategies/{Human Name}.md.

    Reads existing card if present, appends new experiment results.
    Includes: status, experiment history table, best params, key learnings.
    If journal_entries is None, reads from research/journal.json for this strategy.

    Args:
        strategy_id: Strategy identifier (e.g. 'mean_reversion')
        journal_entries: List of journal entry dicts, or None to load from file
        output_dir: Output directory (defaults to VAULT_ROOT / 'Strategies')

    Returns:
        Path to the written file.
    """
    if output_dir is None:
        output_dir = VAULT_ROOT / "Strategies"
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    if journal_entries is None:
        journal_entries = _load_journal_for_strategy(strategy_id)

    human_name = strategy_to_human(strategy_id)
    status = get_strategy_status(strategy_id)
    kebab = strategy_to_kebab(strategy_id)

    # Aggregate stats
    sharpes = []
    for e in journal_entries:
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

    verdicts = [e.get("verdict", "unknown") for e in journal_entries]
    pass_count = sum(1 for v in verdicts if v == "pass")
    fail_count = sum(1 for v in verdicts if v == "fail")
    partial_count = sum(1 for v in verdicts if v == "partial")
    promoted_count = sum(1 for e in journal_entries if e.get("promoted"))

    # Find best params from best-performing experiment
    best_params: dict = {}
    if sharpes and best_sharpe is not None:
        for e in journal_entries:
            km = e.get("key_metrics") or {}
            s = km.get("sharpe")
            try:
                if s is not None and float(s) == best_sharpe:
                    # Pull params from experiment envelope if available
                    eid = e.get("experiment_id", "")
                    env_path = ATLAS_ROOT / "research" / "experiments" / f"exp-{eid}.json"
                    if env_path.exists():
                        try:
                            env = json.loads(env_path.read_text())
                            inputs = env.get("inputs") or {}
                            strategy_params = inputs.get("strategy_params") or inputs.get("params_override") or {}
                            if strategy_params:
                                best_params = strategy_params
                        except (json.JSONDecodeError, OSError):
                            pass
                    break
            except (TypeError, ValueError):
                pass

    # Collect all learnings
    all_learnings: list[str] = []
    seen_l: set[str] = set()
    for e in journal_entries:
        for l in e.get("learnings") or []:
            if l not in seen_l:
                all_learnings.append(l)
                seen_l.add(l)

    fm: dict[str, Any] = {
        "strategy_id": strategy_id,
        "type": "strategy",
        "status": status,
        "total_experiments": len(journal_entries),
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
            f"> **Status:** `{status.upper()}` | **Experiments:** {len(journal_entries)} | "
            f"**Promotions:** {promoted_count}"
        ),
        "",
        "## Overview",
        "",
        f"Research strategy `{strategy_id}`. See experiments below.",
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
            f"| Total Experiments | {len(journal_entries)} |",
            f"| Pass / Partial / Fail | {pass_count} / {partial_count} / {fail_count} |",
            f"| Promotions | {promoted_count} |",
            "",
        ]

    # Best params block
    if best_params:
        lines += ["## Best Parameters", ""]
        for k, v in best_params.items():
            lines.append(f"- `{k}`: {v}")
        lines.append("")

    # Experiments table sorted by date
    sorted_exps = sorted(journal_entries, key=lambda e: e.get("timestamp", ""))
    lines += [
        "## Experiment History",
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

    # Key learnings
    if all_learnings:
        lines += ["## Key Learnings", ""]
        for l in all_learnings:
            lines.append(f"- {l}")
        lines.append("")

    outfile = output_dir / f"{human_name}.md"
    outfile.write_text("\n".join(lines))
    return outfile


def write_parameter_insight(
    strategy_id: str,
    param_name: str,
    findings: dict,
    output_dir: Path = None,
) -> Path:
    """Write a parameter insight note to Parameters/{Strategy} - {param}.md.

    Args:
        strategy_id: Strategy identifier
        param_name: Parameter name (e.g. 'rsi_period')
        findings: Dict with keys:
            - optimal_value: Best observed value
            - tested_range: List or description of tested values
            - sensitivity: Description of sensitivity (low/medium/high or text)
            - related_experiments: List of experiment IDs
        output_dir: Output directory (defaults to VAULT_ROOT / 'Parameters')

    Returns:
        Path to the written file.
    """
    if output_dir is None:
        output_dir = VAULT_ROOT / "Parameters"
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    human_name = strategy_to_human(strategy_id)
    param_display = param_name.replace("_", " ").title()

    optimal_value = findings.get("optimal_value")
    tested_range = findings.get("tested_range")
    sensitivity = findings.get("sensitivity", "unknown")
    related_experiments = findings.get("related_experiments", [])

    fm: dict[str, Any] = {
        "type": "parameter_insight",
        "strategy_id": strategy_id,
        "param_name": param_name,
        "tags": [
            "parameter",
            f"strategy/{strategy_to_kebab(strategy_id)}",
            f"param/{param_name.replace('_', '-')}",
        ],
    }
    if optimal_value is not None:
        fm["optimal_value"] = optimal_value

    lines: list[str] = [
        build_frontmatter(fm),
        "",
        f"# {human_name} — {param_display}",
        "",
        f"> **Strategy:** [[{human_name}]] | **Parameter:** `{param_name}`",
        "",
        "## Findings",
        "",
        "| Field | Value |",
        "|-------|-------|",
        f"| Optimal Value | `{optimal_value}` |",
        f"| Tested Range | {tested_range if tested_range is not None else 'N/A'} |",
        f"| Sensitivity | {sensitivity} |",
        "",
    ]

    if related_experiments:
        lines += ["## Related Experiments", ""]
        for exp_id in related_experiments:
            lines.append(f"- [[{exp_id}]]")
        lines.append("")

    lines += [
        "---",
        "",
        f"Strategy:: [[{human_name}]]",
    ]

    filename = f"{human_name} - {param_name.replace('_', ' ').title()}.md"
    outfile = output_dir / filename
    outfile.write_text("\n".join(lines))
    return outfile


def write_daily_log(
    date_str: str = None,
    experiments_today: list = None,
    output_dir: Path = None,
) -> Path:
    """Write or update Daily Logs/{YYYY-MM-DD}.md with today's research activity.

    Includes: experiments run count, pass/fail breakdown, key findings, queue status.
    If date_str is None, uses today. Appends to existing file if already exists.

    Args:
        date_str: Date string YYYY-MM-DD, or None for today
        experiments_today: List of experiment result dicts with keys:
            - experiment_id, verdict, strategy, key_metrics, hypothesis, learnings
        output_dir: Output directory (defaults to VAULT_ROOT / 'Daily Logs')

    Returns:
        Path to the written file.
    """
    if output_dir is None:
        output_dir = VAULT_ROOT / "Daily Logs"
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    if date_str is None:
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    if experiments_today is None:
        experiments_today = []

    outfile = output_dir / f"{date_str}.md"

    # Count pass/fail/partial
    pass_count = sum(1 for e in experiments_today if e.get("verdict") == "pass")
    fail_count = sum(1 for e in experiments_today if e.get("verdict") == "fail")
    partial_count = sum(1 for e in experiments_today if e.get("verdict") == "partial")
    total_count = len(experiments_today)

    # Load queue status
    queue_status = _get_queue_status()

    now_str = datetime.now(timezone.utc).strftime("%H:%M UTC")

    if outfile.exists():
        # Append a new entry section
        existing = outfile.read_text()
        update_block_lines: list[str] = [
            "",
            f"## Update @ {now_str}",
            "",
        ]
        if experiments_today:
            update_block_lines += [
                f"Ran {total_count} experiments: {pass_count} pass, {partial_count} partial, {fail_count} fail",
                "",
                "| Experiment | Strategy | Verdict | Sharpe |",
                "|------------|----------|---------|--------|",
            ]
            for exp in experiments_today:
                eid = exp.get("experiment_id", "?")
                strat = strategy_to_human(exp.get("strategy"))
                verdict = exp.get("verdict", "?")
                km = exp.get("key_metrics") or {}
                sharpe = km.get("sharpe") or km.get("combined_sharpe")
                update_block_lines.append(
                    f"| [[{eid}]] | {strat} | `{verdict}` | {fmt_float(sharpe)} |"
                )
            update_block_lines.append("")

        # Key findings from today's experiments
        key_findings = []
        for exp in experiments_today:
            for l in exp.get("learnings") or []:
                key_findings.append(l)
        if key_findings:
            update_block_lines += ["**Key Findings:**", ""]
            for f_item in key_findings[:10]:  # cap at 10
                update_block_lines.append(f"- {f_item}")
            update_block_lines.append("")

        outfile.write_text(existing + "\n".join(update_block_lines))
    else:
        # Create new daily log
        fm: dict[str, Any] = {
            "type": "daily_log",
            "date": date_str,
            "experiments_run": total_count,
            "tags": ["daily-log", f"date/{date_str}"],
        }

        lines: list[str] = [
            build_frontmatter(fm),
            "",
            f"# Research Log — {date_str}",
            "",
            f"> **Date:** {date_str} | **Experiments:** {total_count} | "
            f"**Pass/Partial/Fail:** {pass_count}/{partial_count}/{fail_count}",
            "",
        ]

        if queue_status:
            lines += [
                "## Queue Status",
                "",
                f"- **Queued:** {queue_status.get('queued', 0)}",
                f"- **Running:** {queue_status.get('running', 0)}",
                f"- **Completed today:** {total_count}",
                "",
            ]

        if experiments_today:
            lines += [
                "## Experiments Run",
                "",
                "| Experiment | Strategy | Verdict | Sharpe |",
                "|------------|----------|---------|--------|",
            ]
            for exp in experiments_today:
                eid = exp.get("experiment_id", "?")
                strat = strategy_to_human(exp.get("strategy"))
                verdict = exp.get("verdict", "?")
                km = exp.get("key_metrics") or {}
                sharpe = km.get("sharpe") or km.get("combined_sharpe")
                lines.append(
                    f"| [[{eid}]] | {strat} | `{verdict}` | {fmt_float(sharpe)} |"
                )
            lines.append("")

        # Key findings from today's experiments
        key_findings = []
        for exp in experiments_today:
            for l in exp.get("learnings") or []:
                key_findings.append(l)
        if key_findings:
            lines += ["## Key Findings", ""]
            for f_item in key_findings[:10]:
                lines.append(f"- {f_item}")
            lines.append("")

        outfile.write_text("\n".join(lines))

    return outfile


def update_knowledge_base(
    new_findings: list = None,
    output_dir: Path = None,
) -> Path:
    """Append new findings to KNOWLEDGE_BASE.md.

    Each finding: {category, text, source_experiment, date}
    Organizes under existing sections or creates new ones.
    Does NOT rebuild from scratch — only appends.

    Args:
        new_findings: List of finding dicts with keys:
            - category: Section name (e.g. 'Confirmed Patterns', 'Strategy Learnings')
            - text: The finding text
            - source_experiment: Experiment ID or None
            - date: Date string or None
        output_dir: Output directory (defaults to VAULT_ROOT)

    Returns:
        Path to KNOWLEDGE_BASE.md
    """
    if output_dir is None:
        output_dir = VAULT_ROOT
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    if new_findings is None:
        new_findings = []

    kb_path = output_dir / "KNOWLEDGE_BASE.md"

    # Group new findings by category
    by_category: dict[str, list[dict]] = defaultdict(list)
    for finding in new_findings:
        cat = finding.get("category", "Miscellaneous")
        by_category[cat].append(finding)

    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    if not kb_path.exists():
        # Create a minimal KNOWLEDGE_BASE.md stub
        lines: list[str] = [
            "# Atlas Research Knowledge Base",
            "",
            f"> **Auto-generated:** {now_str} | **Source:** vault_writer.py incremental updates",
            ">",
            "> This is the AI agent's internal knowledge base. Read this file at session start.",
            "> Regenerate: `python3 scripts/build_obsidian_vault.py --force`",
            "",
            "---",
            "",
        ]
        kb_path.write_text("\n".join(lines))

    if not new_findings:
        return kb_path

    # Build append block
    append_lines: list[str] = [
        "",
        f"---",
        "",
        f"## Incremental Update — {now_str}",
        "",
    ]

    for category, findings in by_category.items():
        append_lines += [f"### {category}", ""]
        for finding in findings:
            text = finding.get("text", "")
            source = finding.get("source_experiment")
            date = finding.get("date", "")
            entry_parts = [f"- {text}"]
            if source:
                entry_parts[0] += f" ([[{source}]])"
            if date:
                entry_parts[0] += f" — {date}"
            append_lines.append(entry_parts[0])
        append_lines.append("")

    existing = kb_path.read_text()
    kb_path.write_text(existing + "\n".join(append_lines))
    return kb_path


# ─── Internal helpers ────────────────────────────────────────────────────────


def _get_queue_status() -> dict:
    """Get a summary of current queue status."""
    queue_path = ATLAS_ROOT / "research" / "queue.json"
    if not queue_path.exists():
        return {}
    try:
        queue = json.loads(queue_path.read_text())
        counts: dict[str, int] = defaultdict(int)
        for item in queue:
            counts[item.get("status", "unknown")] += 1
        return dict(counts)
    except (json.JSONDecodeError, OSError):
        return {}
