"""Atlas-ASX Paper Trading Dashboard."""

import json
import os
from datetime import datetime
from pathlib import Path

import pandas as pd
from flask import Flask, jsonify, render_template

app = Flask(__name__)
PROJECT_ROOT = Path(__file__).parent.parent


def safe_json(path, default=None):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default if default is not None else {}


def get_config():
    return safe_json(PROJECT_ROOT / "config" / "active_config.json", {})


def get_portfolio(config):
    state = safe_json(PROJECT_ROOT / "paper_engine" / "portfolio_state.json", None)
    seq = config.get("risk", {}).get("starting_equity", 5000)
    if state is None:
        return {
            "cash": seq, "positions": [], "closed_trades": [],
            "equity_history": [], "daily_high_water": seq,
            "halted": False, "halt_reason": "", "starting_equity": seq,
        }
    state["starting_equity"] = seq
    return state


def get_latest_plan():
    plans_dir = PROJECT_ROOT / "paper_engine" / "plans"
    if not plans_dir.exists():
        return None
    files = sorted(plans_dir.glob("plan_*.json"), reverse=True)
    return safe_json(files[0], None) if files else None


def get_prices(tickers):
    prices = {}
    cache = PROJECT_ROOT / "data" / "cache"
    if not cache.exists():
        return prices
    for t in tickers:
        fp = cache / (t.replace(".", "_") + ".parquet")
        if fp.exists():
            try:
                df = pd.read_parquet(fp)
                if len(df) > 0:
                    prices[t] = {
                        "close": float(df["close"].iloc[-1]),
                        "prev_close": float(df["close"].iloc[-2]) if len(df) > 1 else None,
                        "date": str(df.index[-1].date()),
                    }
            except Exception:
                pass
    return prices


def get_benchmark_history():
    fp = PROJECT_ROOT / "data" / "cache" / "IOZ_AX.parquet"
    if not fp.exists():
        return []
    try:
        df = pd.read_parquet(fp)
        df = df.tail(252)
        first = float(df["close"].iloc[0])
        return [{"date": str(d.date()), "value": round(float(c) / first * 5000, 2)}
                for d, c in zip(df.index, df["close"])]
    except Exception:
        return []


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/data")
def api_data():
    config = get_config()
    portfolio = get_portfolio(config)
    plan = get_latest_plan()
    ledger = safe_json(PROJECT_ROOT / "journal" / "trade_ledger.json", [])
    backtest = safe_json(PROJECT_ROOT / "backtest" / "results" / "phase5_report.json", {})
    journal = safe_json(PROJECT_ROOT / "journal" / "decision_journal.json", [])

    seq = portfolio.get("starting_equity", 5000)
    positions = portfolio.get("positions", [])
    cash = portfolio.get("cash", seq)

    # Collect tickers
    tickers = set()
    for p in positions:
        tickers.add(p.get("ticker", ""))
    if plan:
        for e in plan.get("proposed_entries", []):
            tickers.add(e.get("ticker", ""))
        for e in plan.get("rejected_entries", []):
            tickers.add(e.get("ticker", ""))
    tickers.discard("")
    prices = get_prices(tickers)

    # Equity calc
    pos_value = 0
    for p in positions:
        t = p.get("ticker", "")
        if t in prices:
            pos_value += prices[t]["close"] * p.get("shares", 0)
        else:
            pos_value += p.get("entry_value", 0)
    equity = round(cash + pos_value, 2)
    total_pnl = round(equity - seq, 2)
    total_pnl_pct = round(total_pnl / seq * 100, 2) if seq > 0 else 0

    # Open positions
    open_pos = []
    for p in positions:
        t = p.get("ticker", "")
        ep = p.get("entry_price", 0)
        sh = p.get("shares", 0)
        cp = prices[t]["close"] if t in prices else ep
        upnl = round((cp - ep) * sh, 2)
        upnl_pct = round((cp - ep) / ep * 100, 2) if ep > 0 else 0
        ed = p.get("entry_date", "")
        dh = 0
        if ed:
            try:
                dh = (datetime.now() - datetime.strptime(ed, "%Y-%m-%d")).days
            except Exception:
                pass
        open_pos.append({
            "ticker": t, "strategy": p.get("strategy", ""),
            "entry_date": ed, "entry_price": ep, "current_price": cp,
            "shares": sh, "unrealized_pnl": upnl, "unrealized_pnl_pct": upnl_pct,
            "stop_price": p.get("stop_price", 0),
            "take_profit": p.get("take_profit"), "days_held": dh,
        })

    # Closed trades
    closed = portfolio.get("closed_trades", []) or ledger or []
    wins = sum(1 for t in closed if t.get("pnl", 0) > 0)
    win_rate = round(wins / len(closed) * 100, 1) if closed else 0

    # Backtest fallback
    bt_metrics = backtest.get("final_metrics", {})
    if not closed and bt_metrics:
        try:
            win_rate = float(str(bt_metrics.get("win_rate", "0%")).replace("%", ""))
        except Exception:
            win_rate = 0

    # Max drawdown
    eq_hist = portfolio.get("equity_history", [])
    max_dd = 0
    if eq_hist:
        peak = 0
        for eh in eq_hist:
            ev = eh.get("equity", seq)
            peak = max(peak, ev)
            dd = (peak - ev) / peak * 100 if peak > 0 else 0
            max_dd = max(max_dd, dd)
    elif bt_metrics:
        try:
            max_dd = float(str(bt_metrics.get("max_dd", "0%")).replace("%", ""))
        except Exception:
            max_dd = 0

    # Equity curve
    eq_curve = []
    if eq_hist:
        eq_curve = [{"date": e.get("date", ""), "equity": e.get("equity", seq)} for e in eq_hist]
    else:
        eq_curve = [{"date": datetime.now().strftime("%Y-%m-%d"), "equity": seq}]

    # Benchmark
    benchmark = get_benchmark_history()

    # Watchlist
    watchlist = []
    if plan:
        for r in plan.get("rejected_entries", []):
            reason = r.get("rejection_reason", "")
            if "max positions" in reason.lower() or "exceeded" in reason.lower():
                watchlist.append({
                    "ticker": r.get("ticker"), "strategy": r.get("strategy"),
                    "confidence": r.get("confidence", 0),
                    "entry_price": r.get("entry_price"),
                    "stop_price": r.get("stop_price"),
                    "take_profit": r.get("take_profit"),
                    "rationale": r.get("rationale", ""),
                })
        watchlist.sort(key=lambda x: x.get("confidence", 0), reverse=True)

    # Strategy performance
    strat_perf = {}
    if closed:
        for t in closed:
            s = t.get("strategy", "unknown")
            if s not in strat_perf:
                strat_perf[s] = {"trades": 0, "wins": 0, "total_pnl": 0}
            strat_perf[s]["trades"] += 1
            pnl = t.get("pnl", 0)
            strat_perf[s]["total_pnl"] += pnl
            if pnl > 0:
                strat_perf[s]["wins"] += 1
        for s in strat_perf:
            n = strat_perf[s]["trades"]
            strat_perf[s]["win_rate"] = round(strat_perf[s]["wins"] / n * 100, 1) if n else 0
            strat_perf[s]["avg_pnl"] = round(strat_perf[s]["total_pnl"] / n, 2) if n else 0
            strat_perf[s]["total_pnl"] = round(strat_perf[s]["total_pnl"], 2)
    elif bt_metrics:
        strat_perf["combined (backtest)"] = {
            "trades": bt_metrics.get("total_trades", 0),
            "win_rate": float(str(bt_metrics.get("win_rate", "0%")).replace("%", "")),
            "total_pnl": float(str(bt_metrics.get("net_pnl", "$0")).replace("$", "").replace(",", "")),
            "avg_pnl": 0, "wins": 0,
        }
        n = strat_perf["combined (backtest)"]["trades"]
        if n > 0:
            strat_perf["combined (backtest)"]["avg_pnl"] = round(
                strat_perf["combined (backtest)"]["total_pnl"] / n, 2)
            strat_perf["combined (backtest)"]["wins"] = int(
                n * strat_perf["combined (backtest)"]["win_rate"] / 100)

    # Risk monitor
    risk_cfg = config.get("risk", {})
    pos_val = sum(p.get("entry_value", 0) for p in positions)
    exposure_pct = round(pos_val / equity * 100, 1) if equity > 0 else 0
    sectors = {}
    for p in positions:
        s = p.get("sector", "Unknown")
        sectors[s] = sectors.get(s, 0) + 1
    hwm = portfolio.get("daily_high_water", seq)
    daily_dd = round((hwm - equity) / hwm * 100, 2) if hwm > 0 else 0

    risk_monitor = {
        "equity": equity,
        "exposure_pct": exposure_pct,
        "position_value": round(pos_val, 2),
        "positions_used": len(positions),
        "positions_max": risk_cfg.get("max_open_positions", 5),
        "sector_concentration": sectors,
        "max_sector_allowed": risk_cfg.get("max_sector_concentration", 2),
        "daily_drawdown_pct": daily_dd,
        "max_daily_drawdown_pct": round(risk_cfg.get("max_daily_drawdown_pct", 0.02) * 100, 2),
        "risk_per_trade": round(equity * risk_cfg.get("max_risk_per_trade_pct", 0.005), 2),
        "max_risk_per_trade_pct": round(risk_cfg.get("max_risk_per_trade_pct", 0.005) * 100, 2),
        "halted": portfolio.get("halted", False),
        "halt_reason": portfolio.get("halt_reason", ""),
    }

    # Today's plan
    plan_data = None
    if plan:
        plan_data = {
            "trade_date": plan.get("trade_date", ""),
            "generated_at": plan.get("generated_at", ""),
            "status": plan.get("status", "UNKNOWN"),
            "proposed_entries": plan.get("proposed_entries", []),
            "rejected_entries": plan.get("rejected_entries", []),
            "proposed_exits": plan.get("proposed_exits", []),
            "portfolio_snapshot": plan.get("portfolio_snapshot", {}),
            "risk_summary": plan.get("risk_summary", {}),
        }


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
