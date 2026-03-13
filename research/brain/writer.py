"""Brain writer — real-time updates to the structured research memory.

Called by sweep.py after each keep/discard decision.  No LLM needed.
All writes are atomic (write-to-temp + rename) to avoid corruption.

Directory layout:
    research/brain/
    ├── INDEX.md, state.json
    ├── strategies/{name}.md, strategies/_index.md
    ├── params/{name}.md, params/_index.md
    ├── experiments/{id}.md, experiments/_index.md
    ├── sweeps/{session}.md, sweeps/_index.md
    ├── patterns/_index.md, patterns/{name}.md
    ├── decisions/_index.md, decisions/{name}.md
    ├── hypotheses/_index.md
    └── regime/
"""

import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

BRAIN_ROOT = Path(__file__).resolve().parent
STATE_PATH = BRAIN_ROOT / "state.json"


# ─── Atomic write helper ────────────────────────────────────────────────────

def _atomic_write(path: Path, content: str) -> None:
    """Write content to path atomically (temp file + rename)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        os.write(fd, content.encode("utf-8"))
        os.close(fd)
        os.replace(tmp, path)
    except Exception:
        os.close(fd) if not os.get_inheritable(fd) else None
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _now_file() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")


# ─── State (machine-readable) ───────────────────────────────────────────────

def load_state() -> dict:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text())
    return {}


def save_state(state: dict) -> None:
    _atomic_write(STATE_PATH, json.dumps(state, indent=2, default=str) + "\n")


def update_state(**kwargs) -> dict:
    """Merge kwargs into state.json and return updated state."""
    state = load_state()
    state.update(kwargs)
    state["updated_at"] = _now_iso()
    save_state(state)
    return state


# ─── Strategy files ─────────────────────────────────────────────────────────

def update_strategy(
    strategy: str,
    metrics: dict,
    params: dict,
    status: str = "active",
    description: str = "",
) -> None:
    """Rewrite strategies/{strategy}.md with current state + append history."""
    path = BRAIN_ROOT / "strategies" / f"{strategy}.md"
    history_lines = []

    # Preserve existing history section
    if path.exists():
        in_history = False
        for line in path.read_text().splitlines():
            if line.strip() == "## History":
                in_history = True
                continue
            if in_history and line.startswith("## "):
                break
            if in_history and line.startswith("| "):
                history_lines.append(line)

    # Build new history entry
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
    sharpe = metrics.get("sharpe", 0)
    trades = metrics.get("total_trades", 0)
    pf = metrics.get("profit_factor", 0)
    cagr = metrics.get("cagr_pct", 0)
    new_entry = f"| {ts} | {sharpe:.4f} | {trades} | {pf:.2f} | {cagr:.1f}% | {description} |"

    # Keep last 30 history entries
    history_lines.append(new_entry)
    history_lines = history_lines[-30:]

    # Format params for display (top-level only, skip nested dicts)
    param_lines = []
    for k, v in sorted(params.items()):
        if not isinstance(v, dict):
            param_lines.append(f"| {k} | {v} |")

    content = f"""# {strategy}

> **Status:** {status} | **Best Sharpe:** {sharpe:.4f} | **Trades:** {trades}
> **Updated:** {ts}

## Current Best Params

| Parameter | Value |
|-----------|-------|
{chr(10).join(param_lines)}

## Current Metrics

| Metric | Value |
|--------|-------|
| Sharpe | {sharpe:.4f} |
| CAGR | {cagr:.1f}% |
| Profit Factor | {pf:.2f} |
| Max Drawdown | {metrics.get('max_drawdown_pct', 0):.1f}% |
| Total Trades | {trades} |
| Win Rate | {metrics.get('win_rate_pct', 0):.1f}% |

## History

| Date | Sharpe | Trades | PF | CAGR | Change |
|------|--------|--------|----|------|--------|
{chr(10).join(history_lines)}
"""
    _atomic_write(path, content)


def rebuild_strategy_index() -> None:
    """Rebuild strategies/_index.md from individual strategy files."""
    strat_dir = BRAIN_ROOT / "strategies"
    rows = []

    for f in sorted(strat_dir.glob("*.md")):
        if f.name.startswith("_"):
            continue
        name = f.stem
        # Parse blockquote lines for status/sharpe/trades/updated
        text = f.read_text()
        status = sharpe = trades = updated = "?"
        for line in text.splitlines():
            if "**Status:**" in line:
                m = re.search(r'\*\*Status:\*\*\s*(\S+)', line)
                if m:
                    status = m.group(1)
                m = re.search(r'\*\*Best Sharpe:\*\*\s*([\d\.\-]+)', line)
                if m:
                    sharpe = m.group(1)
                m = re.search(r'\*\*Trades:\*\*\s*(\d+)', line)
                if m:
                    trades = m.group(1)
            if "**Updated:**" in line:
                m = re.search(r'\*\*Updated:\*\*\s*(.+)', line)
                if m:
                    updated = m.group(1).strip()
        rows.append(f"| {name} | {status} | {sharpe} | {trades} | {updated} |")

    content = f"""# Strategies Index

> {len(rows)} strategies tracked. Read individual files for full details.

| Strategy | Status | Best Sharpe | Trades | Last Updated |
|----------|--------|-------------|--------|--------------|
{chr(10).join(rows)}
"""
    _atomic_write(strat_dir / "_index.md", content)


# ─── Parameter insights ─────────────────────────────────────────────────────

def record_param_result(
    strategy: str,
    param_name: str,
    value: Any,
    old_value: Any,
    kept: bool,
    sharpe_delta: float,
    new_sharpe: float,
) -> None:
    """Append a test result to params/{param_name}.md."""
    path = BRAIN_ROOT / "params" / f"{param_name}.md"

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
    status = "✅ kept" if kept else "❌ discard"
    new_line = f"| {ts} | {strategy} | {old_value} → {value} | {status} | {sharpe_delta:+.4f} | {new_sharpe:.4f} |"

    if path.exists():
        content = path.read_text()
        # Append to results table
        content = content.rstrip() + "\n" + new_line + "\n"
        # Trim to last 50 results
        lines = content.splitlines()
        table_start = None
        for i, line in enumerate(lines):
            if line.startswith("| Date"):
                table_start = i
                break
        if table_start is not None:
            header = lines[:table_start + 2]  # heading + separator
            data = [l for l in lines[table_start + 2:] if l.startswith("| ")]
            data = data[-50:]
            content = "\n".join(header + data) + "\n"
    else:
        content = f"""# {param_name}

> Parameter tested across strategies. Shows what values work and where.

| Date | Strategy | Change | Result | Sharpe Δ | New Sharpe |
|------|----------|--------|--------|----------|------------|
{new_line}
"""

    _atomic_write(path, content)


def rebuild_param_index() -> None:
    """Rebuild params/_index.md from individual param files."""
    param_dir = BRAIN_ROOT / "params"
    rows = []

    for f in sorted(param_dir.glob("*.md")):
        if f.name.startswith("_"):
            continue
        name = f.stem
        # Count results
        text = f.read_text()
        n_tests = sum(1 for line in text.splitlines()
                      if line.startswith("| ") and "kept" in line or "discard" in line)
        n_kept = sum(1 for line in text.splitlines()
                     if line.startswith("| ") and "✅" in line)
        rows.append(f"| {name} | {n_tests} | {n_kept} |")

    content = f"""# Parameters Index

> {len(rows)} parameters tracked. Read individual files for test history.

| Parameter | Tests | Kept |
|-----------|-------|------|
{chr(10).join(rows)}
"""
    _atomic_write(param_dir / "_index.md", content)


# ─── Experiment records ──────────────────────────────────────────────────────

def record_experiment(
    experiment_id: str,
    strategy: str,
    param_name: str,
    value: Any,
    old_value: Any,
    kept: bool,
    metrics: dict,
    sharpe_delta: float,
) -> None:
    """Write a single experiment record to experiments/{id}.md."""
    path = BRAIN_ROOT / "experiments" / f"{experiment_id}.md"
    ts = _now_iso()
    status = "kept" if kept else "discarded"

    content = f"""# {experiment_id}

> **Strategy:** {strategy} | **Status:** {status} | **{ts}**

## Change
- **Parameter:** {param_name}
- **Value:** {old_value} → {value}
- **Sharpe Δ:** {sharpe_delta:+.4f}

## Metrics
| Metric | Value |
|--------|-------|
| Sharpe | {metrics.get('sharpe', 0):.4f} |
| CAGR | {metrics.get('cagr_pct', 0):.1f}% |
| Profit Factor | {metrics.get('profit_factor', 0):.2f} |
| Max Drawdown | {metrics.get('max_drawdown_pct', 0):.1f}% |
| Total Trades | {metrics.get('total_trades', 0)} |
| Win Rate | {metrics.get('win_rate_pct', 0):.1f}% |
"""
    _atomic_write(path, content)


def rebuild_experiment_index() -> None:
    """Rebuild experiments/_index.md from last 100 experiment files."""
    exp_dir = BRAIN_ROOT / "experiments"
    files = sorted(exp_dir.glob("*.md"), key=lambda f: f.name)
    files = [f for f in files if not f.name.startswith("_")]

    # Prune: keep last 100
    if len(files) > 100:
        for old in files[:-100]:
            old.unlink()
        files = files[-100:]

    rows = []
    for f in reversed(files):  # newest first
        text = f.read_text()
        strategy = status = change = sharpe_d = "?"
        for line in text.splitlines():
            if "**Strategy:**" in line:
                parts = line.split("|")
                for p in parts:
                    p = p.strip()
                    if p.startswith("**Strategy:**"):
                        strategy = p.replace("**Strategy:**", "").strip()
                    elif p.startswith("**Status:**"):
                        status = p.replace("**Status:**", "").strip()
            if "**Parameter:**" in line:
                change = line.replace("- **Parameter:**", "").strip()
            if "**Sharpe Δ:**" in line:
                sharpe_d = line.replace("- **Sharpe Δ:**", "").strip()
        rows.append(f"| {f.stem} | {strategy} | {change} | {status} | {sharpe_d} |")

    content = f"""# Experiments Index

> Last {len(rows)} experiments (newest first). Older experiments pruned.

| ID | Strategy | Parameter | Result | Sharpe Δ |
|----|----------|-----------|--------|----------|
{chr(10).join(rows)}
"""
    _atomic_write(exp_dir / "_index.md", content)


# ─── Sweep sessions ─────────────────────────────────────────────────────────

class SweepSession:
    """Tracks a single sweep session. Created at sweep start, flushed at end."""

    def __init__(self, session_id: str = ""):
        self.session_id = session_id or _now_file()
        self.started_at = _now_iso()
        self.results: List[dict] = []

    def add_result(
        self,
        strategy: str,
        param_name: str,
        value: Any,
        kept: bool,
        sharpe_delta: float,
        new_sharpe: float,
    ) -> None:
        self.results.append({
            "strategy": strategy,
            "param": param_name,
            "value": value,
            "kept": kept,
            "sharpe_delta": sharpe_delta,
            "new_sharpe": new_sharpe,
        })

    def flush(self, runtime_s: float = 0) -> None:
        """Write sweep session summary to sweeps/{session_id}.md."""
        path = BRAIN_ROOT / "sweeps" / f"{self.session_id}.md"
        n_kept = sum(1 for r in self.results if r["kept"])
        n_total = len(self.results)

        rows = []
        for r in self.results:
            status = "✅" if r["kept"] else "❌"
            rows.append(
                f"| {r['strategy']} | {r['param']}={r['value']} | "
                f"{status} | {r['sharpe_delta']:+.4f} | {r['new_sharpe']:.4f} |"
            )

        content = f"""# Sweep {self.session_id}

> **Started:** {self.started_at} | **Runtime:** {runtime_s:.0f}s
> **Results:** {n_kept}/{n_total} kept

| Strategy | Change | Result | Sharpe Δ | New Sharpe |
|----------|--------|--------|----------|------------|
{chr(10).join(rows)}
"""
        _atomic_write(path, content)

    @staticmethod
    def rebuild_index() -> None:
        """Rebuild sweeps/_index.md from session files."""
        sweep_dir = BRAIN_ROOT / "sweeps"
        files = sorted(sweep_dir.glob("*.md"), key=lambda f: f.name, reverse=True)
        files = [f for f in files if not f.name.startswith("_")]

        # Keep last 50 sessions
        if len(files) > 50:
            for old in files[50:]:
                old.unlink()
            files = files[:50]

        rows = []
        for f in files:
            text = f.read_text()
            runtime = results = "?"
            for line in text.splitlines():
                m = re.search(r'\*\*Runtime:\*\*\s*(.+?)(?:\s*\||\s*$)', line)
                if m:
                    runtime = m.group(1).strip()
                m = re.search(r'\*\*Results:\*\*\s*(.+?)(?:\s*$)', line)
                if m:
                    results = m.group(1).strip()
            rows.append(f"| {f.stem} | {results} | {runtime} |")

        content = f"""# Sweep Sessions Index

> Last {len(rows)} sessions (newest first).

| Session | Results (kept/total) | Runtime |
|---------|----------------------|---------|
{chr(10).join(rows)}
"""
        _atomic_write(sweep_dir / "_index.md", content)


# ─── Patterns / Decisions / Hypotheses (agent-written, index auto-rebuilt) ──

def rebuild_simple_index(subdir: str, title: str) -> None:
    """Rebuild _index.md for a simple directory of notes."""
    d = BRAIN_ROOT / subdir
    files = sorted(d.glob("*.md"))
    files = [f for f in files if not f.name.startswith("_")]

    rows = []
    for f in files:
        name = f.stem
        # Grab first blockquote line as summary
        summary = ""
        for line in f.read_text().splitlines():
            if line.startswith("> "):
                summary = line[2:].strip()[:80]
                break
        rows.append(f"| [{name}]({f.name}) | {summary} |")

    content = f"""# {title}

> {len(rows)} entries.

| Name | Summary |
|------|---------|
{chr(10).join(rows)}
"""
    _atomic_write(d / "_index.md", content)


# ─── INDEX.md (top-level orientation) ────────────────────────────────────────

def rebuild_root_index() -> None:
    """Rebuild the top-level INDEX.md with current counts and last-updated."""
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    def _count(subdir):
        d = BRAIN_ROOT / subdir
        return sum(1 for f in d.glob("*.md") if not f.name.startswith("_"))

    state = load_state()
    last_sweep = state.get("last_sweep_session", "—")

    content = f"""# Atlas Research Brain

> **Updated:** {ts} | **Last sweep:** {last_sweep}

## Navigation

| Directory | Contents | Count |
|-----------|----------|-------|
| [strategies/](_index.md) | Per-strategy params, metrics, history | {_count('strategies')} |
| [params/](_index.md) | Parameter test results across strategies | {_count('params')} |
| [experiments/](_index.md) | Individual experiment records (rolling 100) | {_count('experiments')} |
| [sweeps/](_index.md) | Sweep session summaries | {_count('sweeps')} |
| [patterns/](_index.md) | Confirmed rules — never violate | {_count('patterns')} |
| [decisions/](_index.md) | Closed decisions — don't revisit | {_count('decisions')} |
| [hypotheses/](_index.md) | Open questions to test | {_count('hypotheses')} |
| [regime/](_index.md) | Market regime analysis | {_count('regime')} |

## Quick Reference

- **state.json** — machine-readable current state (best params, metrics)
- Read `strategies/_index.md` first to see which strategies are performing
- Read `params/_index.md` to see what's been tested
- Read `patterns/_index.md` before proposing changes (rules to respect)
- Read `decisions/_index.md` before re-testing closed topics
"""
    _atomic_write(BRAIN_ROOT / "INDEX.md", content)


# ─── Promotion records ───────────────────────────────────────────────────────

def record_promotion(
    strategy: str,
    market: str,
    prev_version: str,
    new_version: str,
    delta_sharpe: float,
    metrics_comparison: dict,
    auto: bool = True,
) -> None:
    """Write a promotion decision record to decisions/promotion_{strategy}_{date}.md.

    Args:
        strategy: Strategy name that was promoted (e.g. 'mean_reversion').
        market: Market the promotion applies to (e.g. 'sp500').
        prev_version: Config version before promotion (e.g. 'v2.2').
        new_version: Config version after promotion (e.g. 'v2.3').
        delta_sharpe: Change in Sharpe (+ve means improvement).
        metrics_comparison: Dict with 'candidate' and 'active' sub-dicts,
            each containing keys: sharpe, cagr_pct, max_drawdown_pct,
            sortino, profit_factor, total_trades (all optional — missing keys
            are rendered as '—').
        auto: True if promoted automatically (no human approval).
    """
    ts_file = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    ts_human = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    filename = f"promotion_{strategy}_{ts_file}.md"
    path = BRAIN_ROOT / "decisions" / filename

    mode = "auto" if auto else "manual"
    direction = "promoted" if delta_sharpe >= 0 else "demoted"
    summary = (
        f"{strategy} {direction} on {market}: "
        f"{prev_version} → {new_version}, Sharpe Δ {delta_sharpe:+.4f} ({mode})"
    )

    # Build metrics comparison table
    candidate = metrics_comparison.get("candidate", {})
    active = metrics_comparison.get("active", {})

    def _fmt(d: dict, key: str, fmt: str = ".4f") -> str:
        v = d.get(key)
        if v is None:
            return "—"
        try:
            return format(float(v), fmt)
        except (TypeError, ValueError):
            return str(v)

    metrics_rows = [
        f"| Sharpe | {_fmt(active, 'sharpe')} | {_fmt(candidate, 'sharpe')} | {delta_sharpe:+.4f} |",
        f"| CAGR % | {_fmt(active, 'cagr_pct', '.1f')} | {_fmt(candidate, 'cagr_pct', '.1f')} | — |",
        f"| Max DD % | {_fmt(active, 'max_drawdown_pct', '.1f')} | {_fmt(candidate, 'max_drawdown_pct', '.1f')} | — |",
        f"| Sortino | {_fmt(active, 'sortino')} | {_fmt(candidate, 'sortino')} | — |",
        f"| Profit Factor | {_fmt(active, 'profit_factor', '.2f')} | {_fmt(candidate, 'profit_factor', '.2f')} | — |",
        f"| Total Trades | {_fmt(active, 'total_trades', '.0f')} | {_fmt(candidate, 'total_trades', '.0f')} | — |",
    ]

    content = f"""# Promotion: {strategy} → {new_version}

> {summary}

## Summary

- **Strategy:** {strategy}
- **Market:** {market}
- **Mode:** {mode}
- **Timestamp:** {ts_human}
- **Versions:** {prev_version} → {new_version}
- **Sharpe Δ:** {delta_sharpe:+.4f}

## Metrics Comparison

| Metric | Active ({prev_version}) | Candidate ({new_version}) | Delta |
|--------|------------------------|--------------------------|-------|
{chr(10).join(metrics_rows)}
"""

    _atomic_write(path, content)
    # Rebuild decisions index so the new entry appears immediately
    rebuild_simple_index("decisions", "Decisions")


# ─── Convenience: rebuild all indexes ────────────────────────────────────────

def rebuild_all_indexes() -> None:
    """Rebuild every _index.md and INDEX.md. Called at end of sweep session."""
    rebuild_strategy_index()
    rebuild_param_index()
    rebuild_experiment_index()
    SweepSession.rebuild_index()
    rebuild_simple_index("patterns", "Patterns")
    rebuild_simple_index("decisions", "Decisions")
    rebuild_simple_index("hypotheses", "Hypotheses")
    rebuild_simple_index("regime", "Market Regime")
    rebuild_root_index()


# ─── Execution Intelligence ──────────────────────────────────────────────────

import logging as _logging
_exec_logger = _logging.getLogger("atlas.brain.writer")


def update_execution_intelligence(days: int = 7):
    """Run execution telemetry analysis and update brain docs.

    Called by director or weekly cron.
    """
    try:
        from research.brain.execution import weekly_review
        weekly_review()
    except Exception as e:
        _exec_logger.warning("Execution intelligence update failed: %s", e)
