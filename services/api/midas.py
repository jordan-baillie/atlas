"""Midas API route — cross-sectional funding-carry PAPER strategy (simulation only).

Reads the Midas SQLite store (/root/midas/data/midas.db) — same pattern as the finance tab
querying its SQLite directly. Serves returns + positions/trades to the dashboard Midas tab.
Route: GET /api/midas

STRICTLY simulation: the underlying engine places no orders and uses no auth/keys/capital.
This endpoint only runs read-only SELECTs.
"""
from __future__ import annotations

import logging
import sqlite3
import time as _time
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from fastapi.security import HTTPBasicCredentials

from services.auth import check_auth

router = APIRouter(prefix="/api/midas", tags=["midas"])
logger = logging.getLogger(__name__)

_DB = Path("/root/midas/data/midas.db")
_cache: dict = {"data": None, "ts": 0.0}
_TTL = 120
_MAX_POINTS = 500


def _connect():
    conn = sqlite3.connect(str(_DB), timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


def _build_payload() -> dict:
    if not _DB.exists():
        raise HTTPException(status_code=503, detail="midas.db not initialised yet")
    conn = _connect()
    try:
        st = conn.execute("SELECT * FROM status WHERE id=1").fetchone()
        st = dict(st) if st else {}

        # equity series (downsampled)
        rows = conn.execute("SELECT date, equity FROM forward_track ORDER BY date").fetchall()
        n = len(rows)
        step = max(1, n // _MAX_POINTS)
        equity = [{"date": rows[i]["date"], "equity": round(rows[i]["equity"], 5)} for i in range(0, n, step)]
        if rows and (not equity or equity[-1]["date"] != rows[-1]["date"]):
            equity.append({"date": rows[-1]["date"], "equity": round(rows[-1]["equity"], 5)})

        # latest position snapshot
        snap = conn.execute("SELECT MAX(snapshot_date) AS d FROM positions").fetchone()["d"]
        longs, shorts = [], []
        if snap:
            for r in conn.execute(
                "SELECT symbol, side, weight, funding_signal, adv FROM positions WHERE snapshot_date=?",
                (snap,)).fetchall():
                item = {"symbol": r["symbol"], "weight": r["weight"],
                        "funding_signal": r["funding_signal"], "adv": r["adv"]}
                (longs if r["side"] == "long" else shorts).append(item)
            longs.sort(key=lambda x: -x["weight"])
            shorts.sort(key=lambda x: x["weight"])

        rebs = [dict(r) for r in conn.execute(
            "SELECT date, n_long, n_short, turnover FROM rebalances ORDER BY date DESC LIMIT 12").fetchall()]

        # ---- Bybit DEMO execution (real fills, zero capital) ----
        demo = None
        try:
            ds = conn.execute("SELECT * FROM demo_status WHERE id=1").fetchone()
            if ds:
                ds = dict(ds)
                deq = [{"date": r["date"], "equity": round(r["equity_usd"], 4),
                        "pnl": round(r["pnl_usd"] or 0, 4)}
                       for r in conn.execute(
                           "SELECT date, equity_usd, pnl_usd FROM demo_equity ORDER BY date").fetchall()]
                dsnap = conn.execute("SELECT MAX(snapshot_date) AS d FROM demo_positions").fetchone()["d"]
                dlongs, dshorts = [], []
                if dsnap:
                    for r in conn.execute(
                        "SELECT symbol, side, qty, notional_usd, entry_price, unrealized_pnl "
                        "FROM demo_positions WHERE snapshot_date=? ORDER BY notional_usd DESC",
                        (dsnap,)).fetchall():
                        item = {"symbol": r["symbol"], "qty": r["qty"], "notional_usd": r["notional_usd"],
                                "entry_price": r["entry_price"], "unrealized_pnl": r["unrealized_pnl"]}
                        (dlongs if r["side"] == "Buy" else dshorts).append(item)
                demo = {
                    "running": True,
                    "endpoint": ds.get("endpoint"), "capital_usd": ds.get("capital_usd"),
                    "inception_date": ds.get("inception_date"), "last_run_ts": ds.get("last_run_ts"),
                    "updated_at": ds.get("updated_at"),
                    "equity_usd": ds.get("equity_usd"), "pnl_usd": ds.get("pnl_usd"),
                    "realized_pnl_usd": ds.get("realized_pnl_usd"), "gross_usd": ds.get("gross_usd"),
                    "n_positions": ds.get("n_positions"), "n_target": ds.get("n_target"),
                    "n_placed": ds.get("n_placed"), "n_skipped": ds.get("n_skipped"),
                    "n_errors": ds.get("n_errors"), "kill_present": bool(ds.get("kill_present")),
                    "as_of": dsnap, "equity_curve": deq,
                    "positions": {"n_long": len(dlongs), "n_short": len(dshorts),
                                  "longs": dlongs, "shorts": dshorts},
                }
        except sqlite3.OperationalError:
            demo = None  # demo tables not present yet
    finally:
        conn.close()

    def _g(k):
        return st.get(k)

    return {
        "strategy": "Cross-sectional funding carry (market-neutral, BTC-beta-neutral)",
        "mode": "PAPER SIMULATION — no capital, no orders, no leverage",
        "venue_data": "Binance Vision (survivorship-clean, incl. delisted)",
        "as_of": snap or _g("last_data_date"),
        "inception": _g("inception_date"),
        "universe_names_latest": (len(longs) + len(shorts)) or _g("universe_names"),
        "stats": {
            "forward_since_inception": {"n": _g("fwd_n"), "ann_return": _g("fwd_ann_return"),
                                        "ann_sharpe": _g("fwd_ann_sharpe"), "cum_return": _g("fwd_cum_return")},
            "full_backtest": {"n": _g("full_n"), "ann_return": _g("full_ann_return"),
                              "ann_sharpe": _g("full_ann_sharpe"), "max_dd": _g("full_max_dd"),
                              "cum_return": _g("full_cum_return")},
        },
        "decomposition": {"carry_ann": _g("carry_ann"), "price_ann": _g("price_ann"),
                          "cost_ann": _g("cost_ann"), "net_ann": _g("net_ann")},
        "equity": equity,
        "positions": {"as_of": snap, "n_long": len(longs), "n_short": len(shorts),
                      "longs": longs, "shorts": shorts, "recent_rebalances": rebs},
        "demo": demo,
        "disclaimer": "Research simulation + Bybit DEMO execution (zero real capital). Not live trading. Midas #32 gated.",
    }


@router.get("")
def midas_data(_auth: HTTPBasicCredentials = Depends(check_auth)):
    """GET /api/midas — Midas funding-carry paper data (returns + positions/trades) from SQLite."""
    now = _time.time()
    if _cache["data"] and (now - _cache["ts"]) < _TTL:
        return JSONResponse(content=_cache["data"])
    try:
        payload = _build_payload()
        _cache["data"] = payload
        _cache["ts"] = now
        return JSONResponse(content=payload)
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        logger.exception("Midas API (SQLite) failed")
        raise HTTPException(status_code=500, detail=str(e))
