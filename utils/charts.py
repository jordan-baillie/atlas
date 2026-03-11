"""Atlas Chart Generation — matplotlib-based charts for Telegram delivery.

Generates publication-quality PNGs from dashboard-data.json and research
journal data. Dark theme, phone-readable, optimized for Telegram photos.

Usage:
    from utils.charts import equity_chart, research_progress_chart, strategy_leaderboard_chart

    path = equity_chart(days=30)          # -> Path to PNG
    path = research_progress_chart()       # -> Path to PNG
    path = strategy_leaderboard_chart()    # -> Path to PNG
"""

from __future__ import annotations

import json
import logging
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CHARTS_DIR = PROJECT_ROOT / "artifacts" / "charts"
DASHBOARD_DATA = PROJECT_ROOT / "dashboard" / "data" / "dashboard-data.json"
JOURNAL_PATH = PROJECT_ROOT / "research" / "journal.json"


# ─── Theme ───────────────────────────────────────────────────────────────────

# Dark theme matching the Atlas dashboard aesthetic
_THEME = {
    "bg": "#1a1a2e",
    "panel": "#16213e",
    "text": "#e0e0e0",
    "text_dim": "#888888",
    "grid": "#2a2a4a",
    "green": "#00d97e",
    "red": "#e63946",
    "blue": "#4cc9f0",
    "amber": "#f4a261",
    "purple": "#b388ff",
    "cyan": "#00b4d8",
    "white": "#ffffff",
}

# Color cycle for multi-series charts
_COLORS = [
    _THEME["green"], _THEME["blue"], _THEME["amber"],
    _THEME["purple"], _THEME["cyan"], _THEME["red"],
    "#ff6b6b", "#48dbfb", "#feca57", "#ff9ff3",
]


def _setup_style():
    """Apply dark theme to matplotlib."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    plt.rcParams.update({
        "figure.facecolor": _THEME["bg"],
        "axes.facecolor": _THEME["panel"],
        "axes.edgecolor": _THEME["grid"],
        "axes.labelcolor": _THEME["text"],
        "axes.grid": True,
        "grid.color": _THEME["grid"],
        "grid.alpha": 0.5,
        "text.color": _THEME["text"],
        "xtick.color": _THEME["text_dim"],
        "ytick.color": _THEME["text_dim"],
        "legend.facecolor": _THEME["panel"],
        "legend.edgecolor": _THEME["grid"],
        "font.size": 11,
        "axes.titlesize": 14,
        "axes.titleweight": "bold",
    })
    return plt


def _save(fig, name: str) -> Path:
    """Save figure to charts directory, return path."""
    CHARTS_DIR.mkdir(parents=True, exist_ok=True)
    path = CHARTS_DIR / f"{name}.png"
    fig.savefig(path, dpi=150, bbox_inches="tight", facecolor=fig.get_facecolor())
    fig.clf()
    import matplotlib.pyplot as plt
    plt.close(fig)
    logger.info("Chart saved: %s (%d KB)", path.name, path.stat().st_size // 1024)
    return path


def _load_dashboard() -> Optional[dict]:
    """Load dashboard data."""
    try:
        with open(DASHBOARD_DATA) as f:
            return json.load(f)
    except Exception as e:
        logger.error("Failed to load dashboard data: %s", e)
        return None


def _load_journal() -> list[dict]:
    """Load research journal."""
    try:
        with open(JOURNAL_PATH) as f:
            return json.load(f)
    except Exception:
        return []


# ─── Chart: Equity Curve ─────────────────────────────────────────────────────

def equity_chart(days: int = 30) -> Optional[Path]:
    """Generate equity curve chart from dashboard data.

    Shows equity over time with P&L shading.
    """
    plt = _setup_style()
    dash = _load_dashboard()
    if not dash:
        return None

    eq = dash.get("equity_curve", [])
    if len(eq) < 2:
        logger.warning("Not enough equity data for chart (%d points)", len(eq))
        return None

    eq = eq[-days:]
    dates = [p["date"][-5:] for p in eq]  # MM-DD format
    values = [p.get("equity", 0) for p in eq]
    pnls = [p.get("pnl", 0) for p in eq]

    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(10, 5), height_ratios=[3, 1],
                                     gridspec_kw={"hspace": 0.3})

    # Equity line
    ax1.plot(dates, values, color=_THEME["green"], linewidth=2.5)
    ax1.fill_between(dates, values, alpha=0.15, color=_THEME["green"])
    ax1.set_title("Atlas Portfolio — Equity Curve")
    ax1.set_ylabel("Equity ($)")
    ax1.tick_params(axis="x", rotation=45)
    # Only show every Nth label to avoid crowding
    step = max(1, len(dates) // 8)
    ax1.set_xticks(range(0, len(dates), step))

    # Daily P&L bars
    colors = [_THEME["green"] if p >= 0 else _THEME["red"] for p in pnls]
    ax2.bar(dates, pnls, color=colors, alpha=0.8)
    ax2.axhline(y=0, color=_THEME["text_dim"], linewidth=0.5)
    ax2.set_ylabel("Daily P&L ($)")
    ax2.set_xticks(range(0, len(dates), step))
    ax2.tick_params(axis="x", rotation=45)

    return _save(fig, "equity_curve")


# ─── Chart: Strategy Leaderboard ─────────────────────────────────────────────

def strategy_leaderboard_chart(top_n: int = 10) -> Optional[Path]:
    """Horizontal bar chart of top strategies by Sharpe ratio.

    Shows Sharpe + trade count for each strategy.
    """
    plt = _setup_style()
    dash = _load_dashboard()
    if not dash:
        return None

    lb = dash.get("research", {}).get("leaderboard", [])
    if not lb:
        return None

    # Filter to strategies with actual Sharpe data, sort by Sharpe
    valid = [s for s in lb if (s.get("best_sharpe") or 0) > 0]
    valid.sort(key=lambda s: s.get("best_sharpe", 0), reverse=True)
    valid = valid[:top_n]

    if not valid:
        return None

    names = [s.get("name", s.get("id", "?"))[:22] for s in valid]
    sharpes = [s.get("best_sharpe", 0) for s in valid]
    trades = [s.get("best_trades", 0) for s in valid]

    fig, ax = plt.subplots(figsize=(10, max(4, len(valid) * 0.5 + 1)))

    # Horizontal bars
    y_pos = range(len(names))
    bars = ax.barh(y_pos, sharpes, color=_COLORS[:len(names)], alpha=0.85, height=0.6)

    # Add trade count labels
    for i, (sharpe, trade_ct) in enumerate(zip(sharpes, trades)):
        ax.text(sharpe + 0.01, i, f" {sharpe:.3f}  ({trade_ct} trades)",
                va="center", fontsize=10, color=_THEME["text"])

    ax.set_yticks(y_pos)
    ax.set_yticklabels(names)
    ax.invert_yaxis()
    ax.set_xlabel("Sharpe Ratio")
    ax.set_title("Strategy Leaderboard — Best Sharpe")

    return _save(fig, "strategy_leaderboard")


# ─── Chart: Research Progress ────────────────────────────────────────────────

def research_progress_chart(days: int = 14) -> Optional[Path]:
    """Experiments per day + cumulative pass rate over time."""
    plt = _setup_style()
    journal = _load_journal()
    if not journal:
        return None

    # Count experiments per day
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    recent = [e for e in journal if e.get("timestamp", "") >= cutoff]
    if not recent:
        return None

    day_counts = Counter()
    day_passes = Counter()
    for e in recent:
        day = e.get("timestamp", "")[:10]
        if not day:
            continue
        day_counts[day] += 1
        if e.get("verdict") == "pass":
            day_passes[day] += 1

    all_days = sorted(day_counts.keys())
    counts = [day_counts[d] for d in all_days]
    pass_rates = [
        round(day_passes[d] / day_counts[d] * 100) if day_counts[d] > 0 else 0
        for d in all_days
    ]
    labels = [d[-5:] for d in all_days]  # MM-DD

    fig, ax1 = plt.subplots(figsize=(10, 4.5))
    ax2 = ax1.twinx()

    # Experiment count bars
    ax1.bar(labels, counts, color=_THEME["blue"], alpha=0.7, label="Experiments")
    ax1.set_ylabel("Experiments / Day", color=_THEME["blue"])
    ax1.tick_params(axis="y", labelcolor=_THEME["blue"])
    ax1.tick_params(axis="x", rotation=45)

    # Pass rate line
    ax2.plot(labels, pass_rates, color=_THEME["green"], linewidth=2.5,
             marker="o", markersize=5, label="Pass Rate %")
    ax2.set_ylabel("Pass Rate %", color=_THEME["green"])
    ax2.tick_params(axis="y", labelcolor=_THEME["green"])
    ax2.set_ylim(0, 100)

    ax1.set_title("Research Progress — Experiments & Pass Rate")

    # Combined legend
    lines1, labels1 = ax1.get_legend_handles_labels()
    lines2, labels2 = ax2.get_legend_handles_labels()
    ax1.legend(lines1 + lines2, labels1 + labels2, loc="upper left")

    return _save(fig, "research_progress")


# ─── Chart: Before/After Comparison ──────────────────────────────────────────

def before_after_chart(
    title: str,
    metrics: dict[str, tuple[float, float]],
) -> Optional[Path]:
    """Side-by-side bar chart comparing before/after values.

    Args:
        title: Chart title (e.g. "Mean Reversion Re-optimization")
        metrics: Dict of {metric_name: (before, after)} tuples.
                 e.g. {"Sharpe": (0.31, 0.38), "Win Rate %": (55, 62)}
    """
    plt = _setup_style()
    import numpy as np

    if not metrics:
        return None

    names = list(metrics.keys())
    befores = [metrics[n][0] for n in names]
    afters = [metrics[n][1] for n in names]

    x = np.arange(len(names))
    width = 0.35

    fig, ax = plt.subplots(figsize=(10, max(3.5, len(names) * 0.8 + 1)))

    bars1 = ax.bar(x - width / 2, befores, width, label="Before",
                   color=_THEME["text_dim"], alpha=0.7)
    bars2 = ax.bar(x + width / 2, afters, width, label="After",
                   color=_THEME["green"], alpha=0.85)

    # Add value labels
    for bar in bars1:
        h = bar.get_height()
        ax.text(bar.get_x() + bar.get_width() / 2, h, f"{h:.2f}",
                ha="center", va="bottom", fontsize=9, color=_THEME["text_dim"])
    for bar in bars2:
        h = bar.get_height()
        ax.text(bar.get_x() + bar.get_width() / 2, h, f"{h:.2f}",
                ha="center", va="bottom", fontsize=9, color=_THEME["green"])

    # Delta annotations
    for i, name in enumerate(names):
        delta = afters[i] - befores[i]
        sign = "+" if delta >= 0 else ""
        color = _THEME["green"] if delta >= 0 else _THEME["red"]
        y_max = max(befores[i], afters[i])
        ax.text(i, y_max * 1.08, f"{sign}{delta:.3f}", ha="center",
                fontsize=10, fontweight="bold", color=color)

    ax.set_xticks(x)
    ax.set_xticklabels(names)
    ax.legend()
    ax.set_title(title)

    return _save(fig, "before_after")


# ─── Chart: Strategy Radar ──────────────────────────────────────────────────

def strategy_radar_chart(strategy_name: str) -> Optional[Path]:
    """Radar/spider chart showing a strategy's multi-dimensional performance.

    Axes: Sharpe, Win Rate, Trade Count (normalized), Profit Factor, Sortino.
    """
    plt = _setup_style()
    import numpy as np

    # Load from best files
    best_path = PROJECT_ROOT / "research" / "best" / f"{strategy_name}.json"
    if not best_path.exists():
        logger.warning("No best file for %s", strategy_name)
        return None

    with open(best_path) as f:
        best = json.load(f)

    metrics = best.get("metrics", {})
    if not metrics:
        return None

    # Normalize metrics to 0-1 scale for radar
    raw = {
        "Sharpe": metrics.get("sharpe", 0),
        "Win Rate": metrics.get("win_rate_pct", 0) / 100,
        "Trades": min(metrics.get("total_trades", 0) / 500, 1),
        "Profit Factor": min(metrics.get("profit_factor", 0) / 4, 1),
        "CAGR": min(metrics.get("cagr_pct", 0) / 20, 1),
    }

    labels = list(raw.keys())
    values = list(raw.values())
    n = len(labels)

    angles = np.linspace(0, 2 * np.pi, n, endpoint=False).tolist()
    values += values[:1]
    angles += angles[:1]

    fig, ax = plt.subplots(figsize=(6, 6), subplot_kw=dict(polar=True))
    ax.set_facecolor(_THEME["panel"])

    ax.plot(angles, values, color=_THEME["green"], linewidth=2)
    ax.fill(angles, values, alpha=0.25, color=_THEME["green"])

    ax.set_xticks(angles[:-1])
    ax.set_xticklabels(labels, size=10)
    ax.set_ylim(0, 1)
    ax.set_yticks([0.25, 0.5, 0.75, 1.0])
    ax.set_yticklabels(["", "0.5", "", "1.0"], size=8, color=_THEME["text_dim"])
    ax.spines["polar"].set_color(_THEME["grid"])
    ax.grid(color=_THEME["grid"], alpha=0.5)

    display_name = strategy_name.replace("_", " ").title()
    ax.set_title(f"{display_name} — Performance Profile", pad=20)

    return _save(fig, f"radar_{strategy_name}")


# ─── Convenience: Generate All ───────────────────────────────────────────────

def generate_all_charts() -> list[Path]:
    """Generate all standard charts. Returns list of paths."""
    charts = []
    for fn in (equity_chart, strategy_leaderboard_chart, research_progress_chart):
        try:
            path = fn()
            if path:
                charts.append(path)
        except Exception as e:
            logger.error("Chart generation failed (%s): %s", fn.__name__, e)
    return charts
