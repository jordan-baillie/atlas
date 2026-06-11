"""Live pipeline API — the forge->live shadow loop surfaced for the dashboard "Live" tab.

GET /api/live  — deployed strategies (live/registry) enriched with per-strategy VIRTUAL-BOOK
stats (data/live/<name>/{book.json,equity_state.json,returns.jsonl}), a portfolio rollup across
all books, the latest daily shadow report (data/live/daily/*.json), and the kill-switch state.
Read-only; serves RECORDED state only (no broker calls in the request path — book equity is as
of the last record_returns run, daily 23:45 AEST).
"""
from __future__ import annotations

import json
import logging
from dataclasses import asdict
from pathlib import Path

from fastapi import APIRouter, Depends
from fastapi.security import HTTPBasicCredentials

from atlas.dashboard.auth import check_auth
from atlas.kernel.paths import LIVE_DATA_DIR

router = APIRouter(tags=["live"])
logger = logging.getLogger("chat_server.live")

_LIVE = LIVE_DATA_DIR


def _book_stats(name: str, capital: float) -> dict:
    """Per-strategy virtual-book stats from recorded state. Never raises."""
    d = _LIVE / name
    stats: dict = {"book_equity": None, "cash": None, "n_positions": None, "capital_base": capital,
                   "cum_return": None, "last_return": None, "days_tracked": 0,
                   "realized_sharpe": None, "equity_curve": []}
    try:
        book_f = d / "book.json"
        if book_f.exists():
            b = json.loads(book_f.read_text())
            stats["cash"] = b.get("cash")
            stats["n_positions"] = len(b.get("positions") or {})
            stats["capital_base"] = b.get("capital_base", capital)
    except Exception as e:
        logger.debug("live: book read failed for %s: %s", name, e)
    try:
        st = d / "equity_state.json"
        if st.exists():
            stats["book_equity"] = json.loads(st.read_text()).get("equity")
    except Exception:
        pass
    try:
        rets, curve = [], []
        f = d / "returns.jsonl"
        if f.exists():
            for line in f.read_text().splitlines():
                if not line.strip():
                    continue
                try:
                    r = json.loads(line)
                    rets.append(float(r["ret"]))
                    curve.append({"date": r.get("date"), "equity": r.get("equity")})
                except Exception:
                    continue
        if rets:
            cum = 1.0
            for r in rets:
                cum *= 1 + r
            stats["cum_return"] = round(cum - 1, 6)
            stats["last_return"] = round(rets[-1], 6)
            stats["days_tracked"] = len(rets)
            if len(rets) >= 10:
                import statistics
                mu, sd = statistics.mean(rets), statistics.pstdev(rets)
                if sd > 0:
                    stats["realized_sharpe"] = round(mu / sd * (252 ** 0.5), 2)
            stats["equity_curve"] = curve[-90:]   # last ~quarter for the sparkline
    except Exception as e:
        logger.debug("live: returns read failed for %s: %s", name, e)
    return stats


@router.get("/api/live")
def live_state(_auth: HTTPBasicCredentials = Depends(check_auth)) -> dict:
    out: dict = {"deployed": [], "portfolio": None, "daily": None,
                 "kill_switch": {"blocked": False, "reason": None}}

    # deployed strategies + virtual-book stats + portfolio rollup
    try:
        from atlas.execution import registry
        rows = []
        for s in registry.deployed():
            row = asdict(s)
            row["book"] = _book_stats(s.name, s.capital)
            rows.append(row)
        out["deployed"] = rows
        tracked = [r for r in rows if r["book"]["book_equity"] is not None]
        if tracked:
            eq = sum(r["book"]["book_equity"] for r in tracked)
            base = sum(r["book"]["capital_base"] or 0 for r in tracked)
            out["portfolio"] = {
                "n_strategies": len(rows), "n_tracked": len(tracked),
                "total_equity": round(eq, 2), "total_capital_base": round(base, 2),
                "total_pnl": round(eq - base, 2),
                "total_return": round(eq / base - 1, 6) if base else None,
            }
    except Exception as e:
        logger.warning("live: registry read failed: %s", e)

    # latest daily shadow report
    try:
        days = sorted((_LIVE / "daily").glob("*.json"))
        if days:
            out["daily"] = json.loads(days[-1].read_text())
    except Exception as e:
        logger.warning("live: daily report read failed: %s", e)

    # kill-switch state (read-only)
    try:
        from atlas.execution.kill_switch import check_all_layers
        br = check_all_layers()
        if br is not None:
            reason = getattr(br, "reason", None) or getattr(br, "message", None) or str(br)
            layer = getattr(br, "layer", None)
            out["kill_switch"] = {"blocked": True, "reason": reason, "layer": layer}
    except Exception as e:
        logger.debug("live: kill-switch check skipped: %s", e)

    return out
