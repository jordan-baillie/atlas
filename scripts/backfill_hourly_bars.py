#!/usr/bin/env python3
"""Nightly hourly-bar backfill — primes the hourly OHLCV cache.

Reuses data/hourly_loader.load_hourly (Wave 4 P3, commit 4b9b8524).
Tickers loaded:
  - Full SP500 universe (via universe.builder.get_universe_tickers('sp500'))
  - All currently-held positions across live markets
    (brokers/state/live_*.json)
  - Reference tickers: SPY, QQQ, VIX

Storage budget: ~8KB × ~210 tickers ≈ ~1.7MB total.
Runtime budget: ~5-10 min (sequential; bounded by Alpaca rate limit + cache hits).

Schedule: nightly via scripts/atlas.crontab (~02:00 UTC = 12:00 AEST),
before US market opens. Cache primes intraday loads from chart_renders.

Exit codes:
  0  — success
  1  — caught exception during ticker loop (logged + continued)
  2  — hard initialization failure (no tickers loaded at all)
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from data.hourly_loader import load_hourly  # noqa: E402

LOG = logging.getLogger("backfill_hourly")

REFERENCE_TICKERS = ["SPY", "QQQ", "VIX"]
STATE_DIR = PROJECT_ROOT / "brokers" / "state"


def _sp500_tickers() -> list[str]:
    try:
        from universe.builder import get_universe_tickers
        return list(get_universe_tickers("sp500"))
    except Exception as exc:
        LOG.warning("get_universe_tickers('sp500') failed: %s", exc)
        return []


def _held_position_tickers() -> list[str]:
    """Pull all ticker symbols from every brokers/state/live_*.json file."""
    held: set[str] = set()
    if not STATE_DIR.exists():
        return []
    for state_file in STATE_DIR.glob("live_*.json"):
        try:
            data = json.loads(state_file.read_text())
            for pos in data.get("positions", []):
                t = pos.get("ticker")
                if t:
                    held.add(t)
        except (OSError, json.JSONDecodeError) as exc:
            LOG.warning("Could not read %s: %s", state_file.name, exc)
    return sorted(held)


def build_target_list() -> list[str]:
    sp500 = _sp500_tickers()
    held = _held_position_tickers()
    refs = REFERENCE_TICKERS
    combined = sorted(set(sp500) | set(held) | set(refs))
    LOG.info(
        "Targets: %d total (sp500=%d, held=%d, references=%d)",
        len(combined), len(sp500), len(held), len(refs),
    )
    return combined


def backfill(tickers: list[str], days: int = 30) -> dict:
    """Call load_hourly for each ticker. Return summary dict."""
    n_ok, n_fail, n_empty = 0, 0, 0
    t_start = time.time()
    for i, t in enumerate(tickers, 1):
        try:
            df = load_hourly(t, days=days)
            if df is None or len(df) == 0:
                n_empty += 1
                LOG.debug("[%d/%d] %s: empty", i, len(tickers), t)
            else:
                n_ok += 1
                if i % 25 == 0:
                    LOG.info("[%d/%d] progress (last=%s, rows=%d)", i, len(tickers), t, len(df))
        except Exception as exc:
            n_fail += 1
            LOG.warning("[%d/%d] %s: failed: %s", i, len(tickers), t, exc)
    elapsed = time.time() - t_start
    summary = {
        "ok": n_ok,
        "empty": n_empty,
        "failed": n_fail,
        "total": len(tickers),
        "elapsed_sec": round(elapsed, 1),
    }
    LOG.info("Backfill complete: %s", summary)
    return summary


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=30, help="Days of hourly history per ticker")
    ap.add_argument("--limit", type=int, default=None, help="Cap ticker count (debug)")
    ap.add_argument("--dry-run", action="store_true", help="List targets without loading")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    tickers = build_target_list()
    if args.limit:
        tickers = tickers[: args.limit]

    if args.dry_run:
        print(f"Would load {len(tickers)} tickers:")
        for t in tickers:
            print(f"  {t}")
        return 0

    if not tickers:
        LOG.error("Target list is empty — aborting")
        return 2

    summary = backfill(tickers, days=args.days)
    return 0 if summary["failed"] < len(tickers) else 1


if __name__ == "__main__":
    sys.exit(main())
