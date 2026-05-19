#!/usr/bin/env python3
"""CLI wrapper for the paper-trading progress report.

Usage:
    python3 scripts/paper_progress_cli.py [--json | --markdown] [--telegram]

Options:
    --json      Output raw JSON (default)
    --markdown  Output human-readable Markdown table
    --telegram  Also POST the digest via Telegram
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

# Bootstrap sys.path so script runs from any working directory
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

from services.paper_progress import (
    DAYS_THRESHOLD,
    DELTA_THRESHOLD,
    SHARPE_THRESHOLD,
    TRADES_THRESHOLD,
    compute_paper_progress,
)

# ── Status display ──────────────────────────────────────────────────────────

STATUS_EMOJI = {
    "ready": "🟢",
    "progressing": "🟡",
    "failing": "🔴",
    "insufficient_data": "⚪",
}


def _fmt_num(v: float | None, dp: int = 3) -> str:
    return f"{v:.{dp}f}" if v is not None else "—"


def _gate_str(gates: dict) -> str:
    """Compact gate summary: ✓/✗ per gate."""
    marks = {
        "days": "✓" if gates.get("days_pass") else "✗",
        "trades": "✓" if gates.get("trades_pass") else "✗",
        "sharpe": "✓" if gates.get("sharpe_pass") else "✗",
        "delta": "✓" if gates.get("delta_pass") else "✗",
    }
    return f"{marks['days']}30d {marks['trades']}10tr {marks['sharpe']}Sh {marks['delta']}Δ"


# ── Markdown renderer ───────────────────────────────────────────────────────

def render_markdown(rows: list[dict]) -> str:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines: list[str] = [
        f"## 📊 Paper Strategy Progress  ·  {ts}",
        "",
        f"_Promotion bar: ≥{DAYS_THRESHOLD}d in paper · "
        f"≥{TRADES_THRESHOLD} trades · "
        f"Sharpe ≥{SHARPE_THRESHOLD} · "
        f"|Δ research| < {DELTA_THRESHOLD}_",
        "",
    ]

    if not rows:
        lines.append("_No strategies currently in PAPER state._")
        return "\n".join(lines)

    # Table header
    header = "| Strategy | Universe | Days | Trades | Sharpe | Δ Research | Status | Gates |"
    sep    = "|----------|----------|-----:|-------:|-------:|-----------:|--------|-------|"
    lines += [header, sep]

    for r in rows:
        emoji = STATUS_EMOJI.get(r["status"], "⚪")
        status_label = r["status"].replace("_", " ").title()
        delta_str = (
            f"{r['sharpe_delta']:+.3f}" if r["sharpe_delta"] is not None else "—"
        )
        lines.append(
            f"| {r['strategy']} | {r['universe']} | {r['days_in_paper']} | "
            f"{r['trade_count']} | {_fmt_num(r['sharpe'])} | {delta_str} | "
            f"{emoji} {status_label} | {_gate_str(r['gates'])} |"
        )

    lines += [
        "",
        "---",
        "**Gate key**: ✓30d = ≥30 calendar days · "
        "✓10tr = ≥10 closed trades · "
        "✓Sh = Sharpe ≥0.3 · "
        "✓Δ = |paper − research Sharpe| < 0.5",
        "",
        f"_For stricter auto-promotion gates (OOS, 30-trade bar), "
        f"see `scripts/auto_promote_paper_to_live.py`._",
    ]
    return "\n".join(lines)


# ── Telegram renderer ───────────────────────────────────────────────────────

def render_telegram(rows: list[dict]) -> str:
    """HTML-safe Telegram message for the digest."""
    from utils.telegram import tg_escape

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [
        f"<b>📊 Paper Progress Digest</b>  ·  {tg_escape(ts)}",
        "",
        f"Bar: ≥{DAYS_THRESHOLD}d · ≥{TRADES_THRESHOLD} trades · Sh≥{SHARPE_THRESHOLD} · |Δ|&lt;{DELTA_THRESHOLD}",
        "",
    ]

    if not rows:
        lines.append("No strategies in PAPER state.")
        return "\n".join(lines)

    for r in rows:
        emoji = STATUS_EMOJI.get(r["status"], "⚪")
        delta_str = (
            f"{r['sharpe_delta']:+.3f}" if r["sharpe_delta"] is not None else "—"
        )
        status_label = r["status"].replace("_", " ").title()
        lines.append(
            f"{emoji} <b>{tg_escape(r['strategy'])}</b>/{tg_escape(r['universe'])} "
            f"— {r['days_in_paper']}d · {r['trade_count']} trades · "
            f"Sh {_fmt_num(r['sharpe'])} · Δ {tg_escape(delta_str)} "
            f"→ {tg_escape(status_label)}"
        )
        lines.append(f"  Gates: {tg_escape(_gate_str(r['gates']))}")

    return "\n".join(lines)


# ── Main ────────────────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--json", action="store_true", help="Output raw JSON (default)")
    group.add_argument("--markdown", action="store_true", help="Output Markdown table")
    parser.add_argument("--telegram", action="store_true", help="Send digest via Telegram")
    args = parser.parse_args(argv)

    rows = compute_paper_progress()

    if args.markdown:
        output = render_markdown(rows)
        print(output)
    else:
        # Default: JSON
        payload = {
            "strategies": rows,
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }
        print(json.dumps(payload, indent=2))

    if args.telegram:
        try:
            from utils.telegram import notify
            msg = render_telegram(rows)
            notify(
                msg,
                level="info",
                category="paper_progress",
                parse_mode="HTML",
            )
            print("\n[telegram] Digest sent.", file=sys.stderr)
        except Exception as exc:
            print(f"\n[telegram] Failed to send: {exc}", file=sys.stderr)
            return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
