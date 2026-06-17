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


def _capital_base(s) -> float | None:
    """The book's accounting basis (book.json capital_base). A CHANGE means the book was
    re-based/reset (drift correction, capital change, migration) — a daily 'return' must NOT
    bridge that discontinuity, or it manufactures a phantom gain/loss. 2026-06-16: a
    $14,500->$5,000 drift-correction re-base produced a spurious -66% that tripped the L4
    drawdown breaker and froze the whole forward-paper track. Returns None when there is no
    book (legacy account-equity fallback) — then the guard is inert and behaviour is unchanged."""
    book_f = LIVE_DATA / s.name / "book.json"
    if not book_f.exists():
        return None
    try:
        return float(json.loads(book_f.read_text(encoding="utf-8")).get("capital_base"))
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return None


def record_one(s, asof: str) -> dict:
    d = LIVE_DATA / s.name
    d.mkdir(parents=True, exist_ok=True)
    eq = _strategy_equity(s)
    if eq is None:
        return {"name": s.name, "skipped": "no equity"}
    cap_base = _capital_base(s)
    state_f = d / "equity_state.json"
    prev = json.loads(state_f.read_text()) if state_f.exists() else {}
    last_eq, last_date, last_cap = prev.get("equity"), prev.get("date"), prev.get("capital_base")
    out = {"name": s.name, "equity": round(eq, 2), "asof": asof}
    # Re-basing guard: if the book's capital_base changed since the last record, the equity
    # series is DISCONTINUOUS — reset the baseline and emit NO return across the break.
    rebased = (last_cap is not None and cap_base is not None
               and abs(float(last_cap) - float(cap_base)) > 1e-6)
    if rebased:
        out["rebaselined"] = {"from_capital_base": float(last_cap), "to_capital_base": float(cap_base)}
        logger.warning("%s: capital_base re-based %s -> %s; baseline reset, NO return emitted "
                       "across the discontinuity", s.name, last_cap, cap_base)
    elif last_eq and last_date != asof and last_eq > 0:
        ret = eq / last_eq - 1.0
        rec = {"date": asof, "ret": round(ret, 8), "equity": round(eq, 2)}
        with (d / "returns.jsonl").open("a") as fh:
            fh.write(json.dumps(rec) + "\n")
        out["ret"] = rec["ret"]
    else:
        out["baseline"] = True
    from atlas.kernel.lockfile import atomic_write_json
    atomic_write_json(state_f, {"equity": round(eq, 2), "date": asof, "capital_base": cap_base})
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
