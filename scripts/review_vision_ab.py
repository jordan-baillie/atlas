"""
review_vision_ab.py — Chart-Vision A/B Review

Usage:
    python -m scripts.review_vision_ab [--days 5] [--telegram]

Reads /root/atlas/logs/overlay_vision_ab/*.jsonl (last N days), prints a
structured comparison of text-only vs vision overlay decisions, and optionally
posts a Telegram summary.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import warnings
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

log = logging.getLogger(__name__)

_LOG_DIR = Path("/root/atlas/logs/overlay_vision_ab")
_MAX_TELEGRAM_CHARS = 3900


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Chart-Vision A/B Review")
    p.add_argument("--days", type=int, default=5,
                   help="How many calendar days of logs to analyse (default 5)")
    p.add_argument("--telegram", action="store_true",
                   help="Post summary to Telegram after printing")
    return p.parse_args()


def _load_entries(days: int) -> list[dict]:
    """Load and parse JSONL entries from the last N days of log files."""
    cutoff = datetime.now(timezone.utc).date() - timedelta(days=days - 1)
    entries: list[dict] = []

    if not _LOG_DIR.exists():
        return entries

    for path in sorted(_LOG_DIR.glob("*.jsonl")):
        # filename is YYYY-MM-DD.jsonl
        try:
            file_date = datetime.strptime(path.stem, "%Y-%m-%d").date()
        except ValueError:
            continue
        if file_date < cutoff:
            continue
        for lineno, line in enumerate(path.read_text().splitlines(), 1):
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError as exc:
                warnings.warn(
                    f"Skipping malformed line {lineno} in {path.name}: {exc}",
                    stacklevel=2,
                )
                print(f"WARNING: skipping malformed line {lineno} in {path.name}: {exc}",
                      file=sys.stderr)
    return entries


def _build_report(entries: list[dict], days: int) -> str:
    """Crunch numbers and return the formatted report string."""
    total_cycles = len(entries)
    vision_ok = sum(1 for e in entries if e.get("vision_decision") is not None)

    # Per-ticker counters: {ticker: {text_bull, text_bear, vis_bull, vis_bear, div}}
    ticker_stats: dict[str, dict[str, int]] = defaultdict(
        lambda: {"text_bull": 0, "text_bear": 0, "vis_bull": 0, "vis_bear": 0, "div": 0}
    )

    # Global divergence flag counters: {flag: count}
    flag_counts: dict[str, int] = defaultdict(int)
    # {flag: set of tickers}
    flag_tickers: dict[str, set] = defaultdict(set)

    unique_vision_signal_counts: list[int] = []  # per cycle: # tickers vision-unique

    for entry in entries:
        td = entry.get("text_decision", {})
        vd = entry.get("vision_decision")
        avoid = set(td.get("tickers_to_avoid") or [])
        text_adjust = bool(td.get("adjust", False))

        # Per-cycle: tickers where vision saw bear but text did not
        vision_unique_count = 0

        # Text signal per analysed ticker
        for ticker in entry.get("tickers_analysed") or []:
            if ticker in avoid or text_adjust:
                ticker_stats[ticker]["text_bear"] += 1
            else:
                ticker_stats[ticker]["text_bull"] += 1

        # Vision signal per chart_vision_signals entry
        if vd and isinstance(vd.get("chart_vision_signals"), list):
            for sig in vd["chart_vision_signals"]:
                ticker = sig.get("ticker", "")
                if not ticker:
                    continue
                tighten = bool(sig.get("tighten_rec", False))
                if tighten:
                    ticker_stats[ticker]["vis_bear"] += 1
                    if ticker not in avoid and not text_adjust:
                        vision_unique_count += 1
                else:
                    ticker_stats[ticker]["vis_bull"] += 1

        unique_vision_signal_counts.append(vision_unique_count)

        # Divergence flags
        for flag_entry in entry.get("divergence_flags") or []:
            flag = flag_entry.get("flag", "unknown")
            ticker = flag_entry.get("ticker", "?")
            flag_counts[flag] += 1
            flag_tickers[flag].add(ticker)
            ticker_stats[ticker]["div"] += 1

    # Date range string
    if entries:
        ts_list = [e.get("timestamp", "") for e in entries if e.get("timestamp")]
        first = min(ts_list) if ts_list else "?"
        last = max(ts_list) if ts_list else "?"
        date_range = f"{first[:10]} → {last[:10]}"
    else:
        date_range = f"(last {days} days — no data)"

    lines: list[str] = [
        f"=== Chart-Vision A/B Review — {date_range} ===",
        f"Total cycles: {total_cycles}",
    ]
    if total_cycles:
        pct = 100 * vision_ok / total_cycles
        lines.append(f"Cycles with vision response: {vision_ok} ({pct:.0f}%)")
    else:
        lines.append("Cycles with vision response: 0")
    lines.append("")

    # Per-ticker agreement
    if ticker_stats:
        lines.append("Per-ticker agreement:")
        total_divergences = 0
        total_observations = 0
        for ticker in sorted(ticker_stats):
            ts = ticker_stats[ticker]
            obs = ts["text_bull"] + ts["text_bear"]
            div = ts["div"]
            total_divergences += div
            total_observations += obs
            div_pct = 100 * div / obs if obs else 0
            vis_bear = ts["vis_bear"]
            vis_bull = ts["vis_bull"]
            txt_bear = ts["text_bear"]
            txt_bull = ts["text_bull"]
            div_str = f"  DIVERGENCE: {div}/{obs} ({div_pct:.0f}%)" if div else ""
            lines.append(
                f"  {ticker}: text=bull({txt_bull})/bear({txt_bear}) "
                f"vision=bull({vis_bull})/bear({vis_bear}){div_str}"
            )
        lines.append("")

        # Overall divergence rate
        overall_div_pct = 100 * total_divergences / total_observations if total_observations else 0
    else:
        overall_div_pct = 0.0

    # Divergence summary by flag
    if flag_counts:
        lines.append("Divergence summary by flag:")
        for flag, count in sorted(flag_counts.items(), key=lambda x: -x[1]):
            n_tickers = len(flag_tickers[flag])
            lines.append(f"  {flag}: {count} occurrences across {n_tickers} tickers")
        lines.append("")
    else:
        lines.append("Divergence summary by flag: none recorded")
        lines.append("")

    # Unique-signal metric
    if unique_vision_signal_counts:
        avg_unique = sum(unique_vision_signal_counts) / len(unique_vision_signal_counts)
    else:
        avg_unique = 0.0
    lines.append(
        f"Unique-signal metric: vision flagged tightening on "
        f"{avg_unique:.1f} tickers/cycle not in text's avoid list (avg per cycle)."
    )
    lines.append("")

    # Verdict heuristic
    if total_cycles < 5:
        verdict_tier = "INSUFFICIENT DATA"
    elif total_cycles < 15:
        verdict_tier = "EARLY"
    else:
        verdict_tier = "READY" if overall_div_pct > 10 else "AGREES"

    lines.append(
        f"Verdict hint: vision adds ~{avg_unique:.1f} tickers/cycle of unique signal; "
        f"{overall_div_pct:.1f}% overall divergence rate. "
        f"[{verdict_tier}] to consider full activation."
    )

    return "\n".join(lines)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    args = _parse_args()

    entries = _load_entries(args.days)
    report = _build_report(entries, args.days)

    print(report)

    if args.telegram:
        try:
            from utils.telegram import send_message  # type: ignore[import]
            truncated = report[:_MAX_TELEGRAM_CHARS]
            if len(report) > _MAX_TELEGRAM_CHARS:
                truncated += "\n[truncated]"
            send_message(truncated, silent=True)
            print("Telegram: sent.")
        except Exception as exc:
            print(f"WARNING: Telegram send failed (non-fatal): {exc}", file=sys.stderr)


if __name__ == "__main__":
    main()
