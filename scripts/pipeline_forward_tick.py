#!/usr/bin/env python3
"""Daily forward-evidence tick for the rapid validate->live pipeline (#420 / #418).

For each candidate in the `paper` stage, accrue genuine FORWARD daily net-of-cost returns
(only days on/after the candidate's forward_start), evaluate the power-based forward gate,
and advance the candidate:
    PASS         -> stage `microlive_gate` (then a HUMAN arms micro-live; never automatic)
    FAIL         -> stage `failed`
    INSUFFICIENT -> stay in `paper`, keep accruing

Forward returns are obtained by re-running the candidate's walk-forward backtest on the
CURRENT (freshly-ingested) data and keeping the daily returns dated on/after forward_start.
As real trading days are ingested, that series grows one genuine OOS day at a time. To stay
cheap, the backtest is SKIPPED when no new data exists past forward_start.

Designed for a daily cron: nice'd + flock'd, low CPU. Reuses the battery's backtest path
(no separate execution code).
"""
from __future__ import annotations

import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

import pandas as pd  # noqa: E402

from research import pipeline as pl  # noqa: E402
from research import forward_evidence as fe  # noqa: E402
from research.cross_oos import adapter  # noqa: E402
from backtest.engine import BacktestEngine  # noqa: E402
from scripts.strategy_evaluator import STRATEGY_REGISTRY, load_sandbox_strategy  # noqa: E402
from utils.config import get_active_config  # noqa: E402
import scripts.validate_oos as vo  # noqa: E402

FWD_DIR = PROJECT / "data" / "pipeline_forward"


def _make_strategy(name, cfg):
    cls = STRATEGY_REGISTRY.get(name) or load_sandbox_strategy(name)
    return cls(cfg)


def _forward_returns(name: str, params: dict, market: str, max_positions: int) -> pd.Series:
    data = vo.load_data(market=market)
    data = {k: v for k, v in data.items() if len(v) >= 260}
    cfg = get_active_config(market)
    cfg.setdefault("strategies", {})[name] = {"enabled": True, **(params or {})}
    cfg.setdefault("risk", {})["max_open_positions"] = max_positions
    res = BacktestEngine(cfg).run_walkforward(data, [_make_strategy(name, cfg)])
    return adapter.daily_returns(res.equity_curve), res.trades


def _latest_data_date(market: str):
    data = vo.load_data(market=market)
    mx = None
    for df in data.values():
        if len(df):
            d = df.index.max()
            mx = d if mx is None or d > mx else mx
    return mx


def tick(market: str = "sp500", max_positions: int = 35) -> None:
    FWD_DIR.mkdir(parents=True, exist_ok=True)
    state = pl._load()
    candidates = state.get("candidates", {})
    paper = [c for c in candidates.values() if c.get("stage") == "paper"]
    if not paper:
        print("forward-tick: no candidates in 'paper' stage")
        return
    latest = _latest_data_date(market)
    print(f"forward-tick {datetime.now(timezone.utc).isoformat()} | latest data {str(latest)[:10]} "
          f"| {len(paper)} paper candidate(s)")

    for c in paper:
        label = c["label"]
        fwd_start = c.get("forward_start") or date.today().isoformat()
        if not c.get("forward_start"):
            pl.set_stage(label, "paper", forward_start=fwd_start)
        store = FWD_DIR / f"{label.replace('/', '_').replace('@', '_at_')}.json"
        series = {}
        if store.exists():
            try:
                series = json.loads(store.read_text())
            except Exception:
                series = {}

        # Cheap skip: no data past forward_start yet -> nothing to accrue.
        if latest is not None and str(latest)[:10] < fwd_start and not series:
            verdict = fe.evaluate_forward([], )
            pl.set_stage(label, "paper", forward_verdict="INSUFFICIENT", forward_days=0)
            print(f"  [{label}] no data past forward_start {fwd_start} yet -> INSUFFICIENT (0d)")
            continue

        # Re-run the backtest and accrue genuine forward days (date >= forward_start).
        try:
            r, all_trades = _forward_returns(c["name"], c.get("params", {}), market, max_positions)
        except Exception as e:
            print(f"  [{label}] backtest failed: {e}")
            continue
        for ts, val in r.items():
            d = str(pd.Timestamp(ts).date())
            if d >= fwd_start:
                series[d] = float(val)
        store.write_text(json.dumps(series, indent=2, sort_keys=True))

        # #424: cluster-adjusted independent-bet route — forward trades (exit on/after
        # forward_start), grouped into monthly cohorts so simultaneous positions are one bet.
        ftr, fcoh = [], []
        for t in all_trades or []:
            ed = str(pd.Timestamp(t.get("exit_date", t["entry_date"])).date())
            pv = t.get("position_value", 0) or 0
            if ed >= fwd_start and pv > 0:
                ftr.append(float(t["pnl"]) / pv)
                fcoh.append(pd.Timestamp(t["entry_date"]).to_period("M"))

        vals = [series[k] for k in sorted(series)]
        ev = fe.evaluate_forward(vals, trade_returns=ftr or None, trade_cohorts=fcoh or None)
        new_stage = "paper"
        if ev["verdict"] == "PASS":
            new_stage = "microlive_gate"
        elif ev["verdict"] == "FAIL":
            new_stage = "failed"
        pl.set_stage(label, new_stage, forward_verdict=ev["verdict"], forward_days=ev["n_days"])
        print(f"  [{label}] forward_days={ev['n_days']} verdict={ev['verdict']} "
              f"sharpe={ev.get('sharpe')} t={ev.get('t_stat')} bets={ev.get('n_bets')} "
              f"trade_t={ev.get('trade_t')} -> stage={new_stage}")
        if new_stage == "microlive_gate":
            print(f"  *** [{label}] CLEARED the forward gate -> awaiting HUMAN micro-live confirm "
                  f"(research.microlive_gate.arm_microlive(..., confirmed=True)) ***")


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--market", default="sp500")
    ap.add_argument("--max-positions", type=int, default=35)
    a = ap.parse_args()
    tick(a.market, a.max_positions)
