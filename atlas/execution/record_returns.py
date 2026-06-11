"""live/record_returns.py — realized daily-return recorder for the forward-paper gate.

The track-vs-expectation gate (live/track_expectation.py) consumes data/live/<name>/returns.jsonl. Nothing
wrote it until now, so the gate stayed 'insufficient' forever. This records the REALIZED daily return of each
deployed shadow strategy from the broker, so expectancy evidence actually accumulates.

Attribution model (2026-06-10, N-strategy ready): each shadow strategy keeps a VIRTUAL SUB-BOOK
(live/virtual_book.py — its own positions+cash at its capital slice, fills applied at execution).
Realized return = day-over-day change of the BOOK's mark-to-market equity at live broker prices.
Exact per-strategy attribution no matter how many strategies share the paper account. Falls back to
account equity ONLY when a book doesn't exist yet (pre-migration single-strategy case).
Run BEFORE the daily rebalance so the delta reflects the held book.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from atlas.execution import registry
from atlas.kernel.paths import LIVE_DATA_DIR, PROJECT_ROOT

logger = logging.getLogger("atlas.live.record_returns")

LIVE_DATA = PROJECT_ROOT / "data" / "live"


def _broker(s):
    from atlas.brokers.registry import get_live_broker
    br = get_live_broker({"trading": {"broker": s.broker, "mode": "paper"}, "market": s.name})
    if br is not None and not br.is_connected:
        br.connect()
    return br


def _strategy_equity(s) -> float | None:
    """Virtual-book MTM at live prices (N-strategy correct). Fallback: account equity (legacy)."""
    br = _broker(s)
    if br is None:
        return None
    book_f = LIVE_DATA / s.name / "book.json"
    if book_f.exists():
        from atlas.execution.virtual_book import VirtualBook
        book = VirtualBook(s.name)
        if not book.positions:
            return book.cash or None
        try:
            prices = br.get_prices(list(book.positions)) or {}
        except Exception as e:
            logger.warning("%s: price fetch for book MTM failed: %s", s.name, e)
            return None
        return book.mtm({k: float(v) for k, v in prices.items()})
    # legacy fallback: whole-account equity (only valid while ONE strategy uses the account)
    ai = br.get_account_info()
    return float(getattr(ai, "equity", 0) or 0) or None


def record_one(s, asof: str) -> dict:
    d = LIVE_DATA / s.name
    d.mkdir(parents=True, exist_ok=True)
    eq = _strategy_equity(s)
    if eq is None:
        return {"name": s.name, "skipped": "no equity"}
    state_f = d / "equity_state.json"
    prev = json.loads(state_f.read_text()) if state_f.exists() else {}
    last_eq, last_date = prev.get("equity"), prev.get("date")
    out = {"name": s.name, "equity": round(eq, 2), "asof": asof}
    if last_eq and last_date != asof and last_eq > 0:
        ret = eq / last_eq - 1.0
        rec = {"date": asof, "ret": round(ret, 8), "equity": round(eq, 2)}
        with (d / "returns.jsonl").open("a") as fh:
            fh.write(json.dumps(rec) + "\n")
        out["ret"] = rec["ret"]
    else:
        out["baseline"] = True
    state_f.write_text(json.dumps({"equity": round(eq, 2), "date": asof}, indent=2))
    return out


def record_all(asof: str | None = None) -> list:
    asof = asof or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    res = []
    for s in registry.deployed("shadow"):
        try:
            res.append(record_one(s, asof))
        except Exception as e:
            logger.exception("record_returns %s failed", s.name)
            res.append({"name": s.name, "error": str(e)})
    return res


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=None)
    a = ap.parse_args()
    for r in record_all(a.date):
        print(r)
