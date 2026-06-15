"""Reconcile recorded orders against actual broker fills -> fills.jsonl.

Closes the G6/G7 data gap in the go-live gate (board memo 2026-06-09): slippage
(fill price vs decision price) and broker-error rate can only be scored from real
fill data. Runs daily in the forward-paper cycle, AFTER record_returns and BEFORE
the new rebalance, reconciling any not-yet-reconciled run rows (fault-tolerant:
a missed day is picked up on the next).

For each order with an order_id in runs.jsonl that has no row in fills.jsonl yet,
query the broker and write:
    {date, ticker, side, qty, decision_px, fill_px, filled_qty, status,
     slippage_bps (signed, vs decision_px; KEPT for continuity but CONTAMINATED by stale
                   IEX decision prices in thin names — median 146bps, max 1795bps),
     official_open, slippage_open_bps (signed, fill vs the day's RAW official open — the
                   CLEAN execution-quality measure; the auction print is well-defined and
                   uncontaminated; this is the headline slippage going forward),
     prev_close, slippage_prevclose_bps (signed, fill vs prior raw close — the open-vs-close
                   TIMING reference the daily backtest implicitly assumes),
     filled_qty, status, order_id}

Why the change (Leg B Phase 2, board 2026-06-15): the cost-model validation needs a clean
realized-slippage series. decision_px is the IEX price at order time and is days-stale for
thin small-caps -> garbage. The official open (and prior close) are unambiguous references
both a live fill and a backtest can point at. Equity books only; futures keep their tick model.

Usage: python3 -m atlas.execution.record_fills [--days 5] [--backfill-opens]
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from atlas.kernel.paths import LIVE_DATA_DIR as LIVE_DATA
from atlas.execution.registry import deployed

logger = logging.getLogger(__name__)

LOOKBACK_DAYS = 5  # reconcile anything missed in the last week of runs


def _jsonl(p: Path) -> list:
    if not p.exists():
        return []
    out = []
    for line in p.read_text().splitlines():
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def _slippage_bps(side: str, ref_px: float, fill_px: float) -> float:
    """Signed slippage in basis points vs a reference price; positive = adverse
    (paid more on a BUY / received less on a SELL)."""
    if not ref_px or not fill_px:
        return 0.0
    raw = (fill_px - ref_px) / ref_px * 1e4
    return raw if side == "BUY" else -raw


def _fetch_open_map(tickers: list, dates: list) -> dict:
    """{(ticker, 'YYYY-MM-DD'): {'open': float, 'prev_close': float}} from RAW (unadjusted)
    daily bars over the spanned dates — raw so the open/close match actual (unadjusted) fills.
    One batched Alpaca historical call; empty/partial on any failure (callers fail-open).
    Equity tickers only; futures/crypto symbols simply return no bar."""
    if not tickers or not dates:
        return {}
    try:
        from datetime import datetime, timedelta
        from atlas.brokers.alpaca.market_data import get_historical_bars
        ds = sorted(set(dates))
        start = (datetime.strptime(ds[0], "%Y-%m-%d") - timedelta(days=6)).strftime("%Y-%m-%d")  # headroom for prev_close
        end = ds[-1]
        bars = get_historical_bars(sorted(set(tickers)), start, end, timeframe="1Day", adjustment="raw")
    except Exception as e:
        logger.warning("official-open fetch failed: %s", e)
        return {}
    out: dict = {}
    for tkr, df in (bars or {}).items():
        if df is None or getattr(df, "empty", True):
            continue
        try:
            opens = df["open"].astype(float)
            closes = df["close"].astype(float)
            idx = [d.strftime("%Y-%m-%d") for d in df.index]
            for i, dstr in enumerate(idx):
                prev_close = float(closes.iloc[i - 1]) if i > 0 else None
                out[(tkr, dstr)] = {"open": float(opens.iloc[i]), "prev_close": prev_close}
        except Exception:
            continue
    return out


def _clean_fields(side: str, fill_px: float, ref: dict | None) -> dict:
    """official_open / slippage_open_bps / prev_close / slippage_prevclose_bps for an equity fill."""
    if not fill_px or not ref:
        return {}
    op = ref.get("open"); pc = ref.get("prev_close")
    out = {}
    if op:
        out["official_open"] = round(op, 4)
        out["slippage_open_bps"] = round(_slippage_bps(side, op, fill_px), 2)
    if pc:
        out["prev_close"] = round(pc, 4)
        out["slippage_prevclose_bps"] = round(_slippage_bps(side, pc, fill_px), 2)
    return out


def _futures_slippage(ticker: str, side: str, decision_px: float, fill_px: float, qty: int) -> dict:
    """Tick/dollar slippage for futures fills (G6 futures cost model, pre-reg 2026-06-12).

    Returns {} for non-futures symbols — equity fills keep their bps-only records.
    """
    try:
        from atlas.brokers.ib.broker import futures_cost_spec
        spec = futures_cost_spec(ticker)
    except Exception:
        return {}
    if not spec or not decision_px or not fill_px:
        return {}
    raw = (fill_px - decision_px) / spec["tick_size"]
    ticks = raw if side == "BUY" else -raw           # signed; + = adverse
    return {"slippage_ticks": round(ticks, 2),
            "slippage_usd": round(ticks * spec["tick_value"] * abs(int(qty or 0)), 2)}


def reconcile_book(name: str, broker) -> int:
    d = LIVE_DATA / name
    runs = _jsonl(d / "runs.jsonl")[-LOOKBACK_DAYS * 3:]
    done = {f["order_id"] for f in _jsonl(d / "fills.jsonl") if f.get("order_id")}
    pending = []
    for run in runs:
        if run.get("dry_run") or run.get("blocked"):
            continue
        for o in run.get("orders", []):
            oid = o.get("order_id")
            if oid and oid not in done:
                pending.append((run["date"], o))
    if not pending:
        return 0

    # batch-fetch official opens for all pending equity fills up front (one historical call)
    open_map = _fetch_open_map([o["ticker"] for _, o in pending], [dt for dt, _ in pending])

    n = 0
    with (d / "fills.jsonl").open("a") as fh:
        for date, o in pending:
            try:
                res = broker.get_order_status(o["order_id"])
            except Exception as e:
                logger.warning("fill query failed %s %s: %s", name, o["order_id"], e)
                continue
            status = getattr(getattr(res, "status", None), "value", None) or str(getattr(res, "status", "?"))
            fill_px = float(getattr(res, "fill_price", 0.0) or 0.0)
            rec = {"date": date, "ticker": o["ticker"], "side": o["side"], "qty": o["qty"],
                   "decision_px": o.get("px"), "fill_px": fill_px or None,
                   "filled_qty": int(getattr(res, "filled_qty", 0) or 0),
                   "status": status,
                   "slippage_bps": round(_slippage_bps(o["side"], o.get("px") or 0.0, fill_px), 2)
                                   if fill_px else None,
                   "order_id": o["order_id"]}
            if fill_px:
                rec.update(_clean_fields(o["side"], fill_px, open_map.get((o["ticker"], date))))
                rec.update(_futures_slippage(o["ticker"], o["side"], o.get("px") or 0.0, fill_px, o.get("qty")))
            fh.write(json.dumps(rec) + "\n")
            n += 1
    return n


def backfill_opens(name: str) -> int:
    """One-off: enrich existing fills.jsonl rows with official_open/slippage_open_bps/prev_close
    (Leg B Phase 2). Idempotent — only touches filled rows missing the clean fields. Rewrites the
    file atomically. Returns rows enriched."""
    d = LIVE_DATA / name
    fp = d / "fills.jsonl"
    rows = _jsonl(fp)
    todo = [(r["ticker"], r["date"]) for r in rows
            if r.get("fill_px") and "slippage_open_bps" not in r]
    if not todo:
        return 0
    open_map = _fetch_open_map([t for t, _ in todo], [dt for _, dt in todo])
    n = 0
    for r in rows:
        if r.get("fill_px") and "slippage_open_bps" not in r:
            clean = _clean_fields(r["side"], float(r["fill_px"]), open_map.get((r["ticker"], r["date"])))
            if clean:
                r.update(clean)
                n += 1
    tmp = fp.with_suffix(".jsonl.tmp")
    tmp.write_text("".join(json.dumps(r) + "\n" for r in rows))
    tmp.replace(fp)
    return n


def main(backfill: bool = False) -> int:
    if backfill:
        # No broker needed — pure market-data enrichment of historical fills.
        for s in deployed():
            n = backfill_opens(s.name)
            logger.info("backfill_opens %s: %d rows enriched", s.name, n)
            print(f"[backfill_opens] {s.name}: {n} rows enriched")
        return 0
    from atlas.execution.daily import _build_broker
    for s in deployed():
        broker = _build_broker(s)
        if broker is None or not getattr(broker, "is_connected", False):
            logger.warning("record_fills: broker unavailable for %s — will retry next cycle", s.name)
            continue
        n = reconcile_book(s.name, broker)
        logger.info("record_fills %s: %d fills reconciled", s.name, n)
        print(f"[record_fills] {s.name}: {n} fills reconciled")
    return 0


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO)
    raise SystemExit(main(backfill="--backfill-opens" in sys.argv))
