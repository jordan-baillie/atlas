#!/usr/bin/env python3
"""
Mock agent dashboard for a single tmux pane.

Renders a structured live view of one agent: current task, files touched
with +/- line counts, latest diff snippet, and a scrolling activity log.

Usage: agent_pane.py --persona engineer|research|validation|planning
"""
from __future__ import annotations

import argparse
import os
import random
import shutil
import signal
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable


# ---------- ANSI ----------
RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
CLEAR = "\033[2J\033[H"
HIDE = "\033[?25l"
SHOW = "\033[?25h"


def fg(r: int, g: int, b: int) -> str:
    return f"\033[38;2;{r};{g};{b}m"


def bg(r: int, g: int, b: int) -> str:
    return f"\033[48;2;{r};{g};{b}m"


# Catppuccin Mocha palette
TEXT = fg(205, 214, 244)
SUBTLE = fg(127, 132, 156)
MUTED = fg(88, 91, 112)
ROSE = fg(245, 194, 231)
MAUVE = fg(203, 166, 247)
BLUE = fg(137, 180, 250)
SAPPHIRE = fg(116, 199, 236)
TEAL = fg(148, 226, 213)
GREEN = fg(166, 227, 161)
YELLOW = fg(249, 226, 175)
PEACH = fg(250, 179, 135)
RED = fg(243, 139, 168)
LAVENDER = fg(180, 190, 254)


PERSONA_COLORS = {
    "engineer": (BLUE, "ENGINEERING LEAD"),
    "research": (MAUVE, "RESEARCH ANALYST"),
    "validation": (GREEN, "VALIDATION LEAD"),
    "planning": (PEACH, "PLANNING LEAD"),
}


# ---------- Data ----------
@dataclass
class FileEdit:
    status: str  # M / A / D
    path: str
    added: int
    removed: int


@dataclass
class Activity:
    when: datetime
    tool: str
    detail: str


@dataclass
class AgentState:
    persona: str
    task_id: str
    task_title: str
    task_desc: str
    model: str
    started_at: datetime
    cost: float
    files: list[FileEdit] = field(default_factory=list)
    activity: list[Activity] = field(default_factory=list)
    diff_path: str = ""
    diff_lines: list[tuple[str, str]] = field(default_factory=list)  # ("+"/"-"/" ", text)
    now_action: str = ""
    now_target: str = ""
    now_meta: str = ""
    status: str = "running"
    turns: int = 0


# ---------- Personas ----------
def engineer_state() -> AgentState:
    s = AgentState(
        persona="engineer",
        task_id="#378",
        task_title="Restore SaverPots goal semantics",
        task_desc="Wrap classifier around legacy pot model; preserve target-date math.",
        model="claude-opus-4-7",
        started_at=datetime.now() - timedelta(minutes=4, seconds=32),
        cost=1.32,
    )
    s.files = [
        FileEdit("M", "finance/SaverPots.tsx", 218, 156),
        FileEdit("M", "finance/FinanceTab.tsx", 12, 4),
        FileEdit("A", "finance/_goal-classifier.ts", 122, 0),
        FileEdit("M", "hooks/useSaverTargets.ts", 18, 2),
    ]
    s.diff_path = "finance/_goal-classifier.ts"
    s.diff_lines = [
        ("+", "export function classifyGoal(pot: SaverPot): GoalKind {"),
        ("+", "  if (pot.targetDate && pot.targetAmount) return 'fixed';"),
        ("+", "  if (pot.targetAmount) return 'amount-only';"),
        ("+", "  if (pot.targetDate)   return 'date-only';"),
        ("+", "  return 'open-ended';"),
        ("+", "}"),
    ]
    s.now_action = "write"
    s.now_target = "finance/SaverPots.tsx"
    s.now_meta = "374 lines  +218 -156"
    return s


def research_state() -> AgentState:
    s = AgentState(
        persona="research",
        task_id="#412",
        task_title="Discover edge-case regimes for overlay v3",
        task_desc="LLM-loop over 18 months of intraday bars, surface anomalies.",
        model="claude-sonnet-4-6",
        started_at=datetime.now() - timedelta(minutes=12, seconds=8),
        cost=4.81,
    )
    s.files = [
        FileEdit("M", "research/discovery/discovery.py", 64, 12),
        FileEdit("A", "research/regimes/_anomaly_pool.py", 198, 0),
        FileEdit("M", "research/llm_loop_runner.py", 22, 6),
    ]
    s.diff_path = "research/regimes/_anomaly_pool.py"
    s.diff_lines = [
        ("+", "def gap_open_clusters(bars: pl.DataFrame) -> list[Cluster]:"),
        ("+", "    gaps = bars.filter(pl.col('gap_pct').abs() > 0.012)"),
        ("+", "    return cluster_by_session(gaps, min_size=3)"),
        ("+", ""),
        (" ", "# called from llm_loop_runner.discover_regimes"),
    ]
    s.now_action = "bash"
    s.now_target = "pi -p --model claude-sonnet-4-6 …"
    s.now_meta = "streaming  ·  82 tok/s"
    return s


def validation_state() -> AgentState:
    s = AgentState(
        persona="validation",
        task_id="#377",
        task_title="Validate B4 burn-down + Finance tab",
        task_desc="Replay 24h of paper trades against new burn-down math.",
        model="claude-opus-4-7",
        started_at=datetime.now() - timedelta(minutes=1, seconds=23),
        cost=0.41,
    )
    s.files = [
        FileEdit("M", "tests/finance/test_burndown.py", 88, 14),
        FileEdit("M", "tests/finance/fixtures/pots.json", 30, 0),
    ]
    s.diff_path = "tests/finance/test_burndown.py"
    s.diff_lines = [
        (" ", "def test_overspend_aware_projection():"),
        ("-", "    assert projected.eta is None"),
        ("+", "    assert projected.eta == date(2026, 9, 1)"),
        ("+", "    assert projected.overspend_flag is True"),
    ]
    s.now_action = "bash"
    s.now_target = "pytest tests/finance -x -q"
    s.now_meta = "running  ·  47 / 112 passed"
    return s


def planning_state() -> AgentState:
    s = AgentState(
        persona="planning",
        task_id="#380",
        task_title="Plan variant-E (treasury) dashboard tab",
        task_desc="Architect 6 new endpoints; map to existing burn-down hooks.",
        model="claude-opus-4-7",
        started_at=datetime.now() - timedelta(minutes=9, seconds=0),
        cost=0.72,
    )
    s.files = [
        FileEdit("A", "tasks/treasury_plan.md", 142, 0),
        FileEdit("M", "tasks/todo.md", 8, 0),
    ]
    s.diff_path = "tasks/treasury_plan.md"
    s.diff_lines = [
        ("+", "## Endpoints"),
        ("+", "  GET  /api/treasury/positions"),
        ("+", "  GET  /api/treasury/yield-curve"),
        ("+", "  POST /api/treasury/rebalance/preview"),
        ("+", ""),
        ("+", "## Open questions"),
        ("+", "  - reuse SaverPots burn-down or fork?"),
    ]
    s.now_action = "delegate"
    s.now_target = "Research Analyst"
    s.now_meta = "awaiting reply  ·  18s"
    return s


PERSONAS: dict[str, Callable[[], AgentState]] = {
    "engineer": engineer_state,
    "research": research_state,
    "validation": validation_state,
    "planning": planning_state,
}


# ---------- Activity tickers (mock) ----------
ENGINEER_TICKS = [
    ("read", "finance/WhatIfPanel.tsx"),
    ("read", "finance/SaverPots.tsx"),
    ("edit", "finance/SaverPots.tsx:128  +6 -2"),
    ("bash", "tsc --noEmit  ·  0 errors"),
    ("read", "hooks/useSaverTargets.ts"),
    ("edit", "finance/_goal-classifier.ts:14  +3"),
    ("write", "finance/SaverPots.tsx  (374 lines)"),
]
RESEARCH_TICKS = [
    ("bash", "pi --mode json --model sonnet-4-6"),
    ("read", "research/discovery/discovery.py:88"),
    ("write", "research/regimes/_anomaly_pool.py"),
    ("bash", "polars: scan 18mo intraday  ·  4.2s"),
    ("read", "research/llm_loop_runner.py:220-264"),
    ("delegate", "→ Engineering Lead  (apply patch)"),
]
VALIDATION_TICKS = [
    ("bash", "pytest -x -q  ·  47/112"),
    ("read", "tests/finance/test_burndown.py"),
    ("bash", "pytest -x -q  ·  68/112"),
    ("edit", "tests/.../pots.json  +14 -0"),
    ("bash", "pytest -x -q  ·  112/112 ✓"),
    ("bash", "ruff check  ·  clean"),
]
PLANNING_TICKS = [
    ("read", "tasks/todo.md"),
    ("read", "dashboard-ui/src/components/finance/"),
    ("write", "tasks/treasury_plan.md  (142 lines)"),
    ("delegate", "→ Research Analyst  (yield-curve sources)"),
    ("task", "add  #381  treasury endpoints"),
    ("task", "add  #382  treasury UI shell"),
]
TICKS = {
    "engineer": ENGINEER_TICKS,
    "research": RESEARCH_TICKS,
    "validation": VALIDATION_TICKS,
    "planning": PLANNING_TICKS,
}


# ---------- Rendering ----------
def hr_top(width: int, label: str, color: str, right: str) -> str:
    inner = width - 2
    label_str = f" {color}{BOLD}{label}{RESET} "
    right_str = f" {SUBTLE}{right}{RESET} "
    # visible widths (strip ANSI for length math)
    vis_l = len(label) + 2
    vis_r = len(right) + 2
    fill = inner - vis_l - vis_r
    fill = max(fill, 1)
    return f"{MUTED}╭─{RESET}{label_str}{MUTED}{'─' * fill}{RESET}{right_str}{MUTED}─╮{RESET}"


def hr_bottom(width: int, right: str) -> str:
    inner = width - 2
    right_str = f" {SUBTLE}{right}{RESET} "
    vis_r = len(right) + 2
    fill = inner - vis_r
    fill = max(fill, 1)
    return f"{MUTED}╰{'─' * fill}{RESET}{right_str}{MUTED}─╯{RESET}"


def section_header(width: int, title: str, color: str) -> str:
    label = f"  {color}{BOLD}{title}{RESET}"
    pad = width - len(title) - 4
    return f"{MUTED}│{RESET}{label}{' ' * max(pad, 0)}{MUTED}│{RESET}"


def body_line(width: int, content_visible: str, content_ansi: str) -> str:
    inner = width - 4  # 2 for borders + 2 for left/right pad
    pad = inner - len(content_visible)
    if pad < 0:
        # truncate visible text from right
        cutoff = len(content_visible) + pad
        content_visible = content_visible[:cutoff]
        content_ansi = content_visible  # we already truncated; drop ansi for safety
        pad = 0
    return f"{MUTED}│{RESET} {content_ansi}{' ' * pad} {MUTED}│{RESET}"


def blank_line(width: int) -> str:
    return f"{MUTED}│{RESET}{' ' * (width - 2)}{MUTED}│{RESET}"


def fmt_elapsed(start: datetime) -> str:
    delta = datetime.now() - start
    secs = int(delta.total_seconds())
    h, rem = divmod(secs, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h{m:02d}m"
    return f"{m}:{s:02d}"


def render(state: AgentState, width: int, height: int) -> str:
    color, label = PERSONA_COLORS[state.persona]
    out = []

    # Header
    right = f"${state.cost:.2f}  ·  {fmt_elapsed(state.started_at)}  ·  {GREEN}● {state.status}{SUBTLE}"
    right_vis = f"${state.cost:.2f}  ·  {fmt_elapsed(state.started_at)}  ·  ● {state.status}"
    out.append(hr_top_visible(width, label, color, right, right_vis))

    # Task line
    task_vis = f"{state.task_id}  {state.task_title}"
    task_ansi = f"{LAVENDER}{state.task_id}{RESET}  {TEXT}{state.task_title}{RESET}"
    out.append(body_line(width, task_vis, task_ansi))
    desc_vis = state.task_desc
    desc_ansi = f"{SUBTLE}{state.task_desc}{RESET}"
    out.append(body_line(width, desc_vis, desc_ansi))
    out.append(blank_line(width))

    # NOW section
    out.append(section_header(width, "NOW", color))
    now_vis = f"  {state.now_action:<8} {state.now_target}"
    now_ansi = f"  {YELLOW}{state.now_action:<8}{RESET} {TEXT}{state.now_target}{RESET}"
    out.append(body_line(width, now_vis, now_ansi))
    meta_vis = f"           {state.now_meta}"
    meta_ansi = f"           {SUBTLE}{state.now_meta}{RESET}"
    out.append(body_line(width, meta_vis, meta_ansi))
    out.append(blank_line(width))

    # FILES section
    out.append(section_header(width, "FILES TOUCHED", color))
    for f in state.files:
        status_color = {"M": YELLOW, "A": GREEN, "D": RED}.get(f.status, TEXT)
        path = f.path
        max_path = width - 28
        if len(path) > max_path:
            path = "…" + path[-(max_path - 1):]
        plus = f"+{f.added}"
        minus = f"-{f.removed}" if f.removed else ""
        stat = f"{plus:<6} {minus:<5}"
        line_vis = f"  {f.status}  {path:<{max_path}} {stat}"
        line_ansi = (
            f"  {status_color}{f.status}{RESET}  {TEXT}{path:<{max_path}}{RESET} "
            f"{GREEN}{plus:<6}{RESET}{RED}{minus:<5}{RESET}"
        )
        out.append(body_line(width, line_vis, line_ansi))
    out.append(blank_line(width))

    # DIFF section
    out.append(section_header(width, f"LATEST DIFF  ·  {state.diff_path}", color))
    max_diff = max(height - len(out) - 12, 3)
    for marker, text in state.diff_lines[:max_diff]:
        c = GREEN if marker == "+" else RED if marker == "-" else SUBTLE
        body = f"  {marker} {text}"
        # truncate
        max_body = width - 4
        if len(body) > max_body:
            body = body[: max_body - 1] + "…"
        out.append(body_line(width, body, f"{c}{body}{RESET}"))
    out.append(blank_line(width))

    # ACTIVITY section — fill remaining
    used = len(out)
    remaining = height - used - 2  # leave room for blank + footer
    if remaining > 2:
        out.append(section_header(width, "ACTIVITY", color))
        remaining -= 1
        rows = state.activity[-remaining:]
        for a in rows:
            ts = a.when.strftime("%H:%M:%S")
            tool_color = {
                "read": SAPPHIRE, "write": GREEN, "edit": YELLOW,
                "bash": MAUVE, "task": LAVENDER, "delegate": PEACH,
            }.get(a.tool, TEXT)
            row_vis = f"  {ts}  {a.tool:<8} {a.detail}"
            row_ansi = (
                f"  {DIM}{SUBTLE}{ts}{RESET}  "
                f"{tool_color}{a.tool:<8}{RESET} {TEXT}{a.detail}{RESET}"
            )
            # truncate
            if len(row_vis) > width - 4:
                row_vis = row_vis[: width - 5] + "…"
                row_ansi = row_vis
            out.append(body_line(width, row_vis, row_ansi))
        # pad
        while len(out) < height - 1:
            out.append(blank_line(width))

    # Footer
    foot = f"turn {state.turns}  ·  {state.model}"
    out.append(hr_bottom(width, foot))

    return "\n".join(out)


def hr_top_visible(width: int, label: str, color: str, right_ansi: str, right_vis: str) -> str:
    inner = width - 2
    label_str = f" {color}{BOLD}{label}{RESET} "
    right_str = f" {right_ansi} "
    vis_l = len(label) + 2
    vis_r = len(right_vis) + 2
    fill = inner - vis_l - vis_r
    fill = max(fill, 1)
    return f"{MUTED}╭─{RESET}{label_str}{MUTED}{'─' * fill}{RESET}{right_str}{MUTED}─╮{RESET}"


# ---------- Main loop ----------
def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--persona", choices=list(PERSONAS.keys()), required=True)
    args = p.parse_args()

    state = PERSONAS[args.persona]()
    ticks = TICKS[args.persona]
    tick_idx = 0

    # seed activity
    base = datetime.now() - timedelta(seconds=90)
    for i, (tool, detail) in enumerate(ticks[:5]):
        state.activity.append(Activity(base + timedelta(seconds=i * 15), tool, detail))
    state.turns = 5

    def cleanup(*_):
        sys.stdout.write(SHOW + RESET + "\n")
        sys.stdout.flush()
        sys.exit(0)

    signal.signal(signal.SIGINT, cleanup)
    signal.signal(signal.SIGTERM, cleanup)

    sys.stdout.write(HIDE)
    try:
        while True:
            size = shutil.get_terminal_size((80, 24))
            w, h = size.columns, size.lines
            # advance state every ~3s
            tool, detail = ticks[tick_idx % len(ticks)]
            state.activity.append(Activity(datetime.now(), tool, detail))
            state.activity = state.activity[-20:]
            state.turns += 1
            state.cost += random.uniform(0.01, 0.08)
            # rotate "now"
            state.now_action = tool
            state.now_target = detail
            state.now_meta = f"turn {state.turns}  ·  {random.randint(40, 120)} tok/s"
            tick_idx += 1

            sys.stdout.write(CLEAR)
            sys.stdout.write(render(state, w, h))
            sys.stdout.flush()
            time.sleep(2.4 + random.random() * 1.2)
    finally:
        cleanup()


if __name__ == "__main__":
    main()
