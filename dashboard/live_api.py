#!/usr/bin/env python3
"""Atlas-ASX Live Dashboard API.

Lightweight Flask server that serves dashboard data with live prices.
Fetches current prices from yfinance on each request (max 5 tickers).
"""

import json
import re
import time
import threading
from datetime import datetime
from zoneinfo import ZoneInfo
from pathlib import Path

import pandas as pd
import yfinance as yf
from flask import Flask, jsonify
from flask_cors import CORS

BRISBANE = ZoneInfo("Australia/Brisbane")
PROJECT = Path("/a0/usr/projects/atlas-asx")

app = Flask(__name__)
CORS(app)

# Price cache - refreshes every 30s max
_price_cache = {}
_price_cache_time = 0
PRICE_CACHE_TTL = 30
_price_lock = threading.Lock()


def safe_json(path, default=None):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default if default is not None else {}


def parse_metric_number(val):
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return float(val)
    s = re.sub(r'[\$%x,+]', '', str(val).strip())
    try:
        return float(s)
    except (ValueError, TypeError):
        return None


def fetch_live_prices(tickers):
    """Fetch live prices with 30s cache."""
    global _price_cache, _price_cache_time
    now = time.time()
    with _price_lock:
        if now - _price_cache_time < PRICE_CACHE_TTL and set(tickers) <= set(_price_cache.keys()):
            return {t: _price_cache[t] for t in tickers if t in _price_cache}

    prices = {}
    for ticker in tickers:
        try:
            data = yf.download(ticker, period="5d", progress=False, auto_adjust=True)
            if data.empty:
                continue
            if hasattr(data.columns, "levels") and data.columns.nlevels > 1:
                data.columns = [c[0].lower() for c in data.columns]
            else:
                data.columns = [c.lower() for c in data.columns]
            prices[ticker] = {
                "close": float(data["close"].iloc[-1]),
                "prev_close": float(data["close"].iloc[-2]) if len(data) > 1 else None,
                "date": str(data.index[-1].date()) if hasattr(data.index[-1], 'date') else str(data.index[-1]),
                "live": True,
            }
        except Exception:
            pass

    # Fill missing from parquet cache
    cache_dir = PROJECT / "data" / "cache"
    for t in tickers:
        if t not in prices:
            fp = cache_dir / (t.replace(".", "_") + ".parquet")
            if fp.exists():
                try:
                    df = pd.read_parquet(fp)
                    if len(df) > 0:
                        prices[t] = {
                            "close": float(df["close"].iloc[-1]),
                            "prev_close": float(df["close"].iloc[-2]) if len(df) > 1 else None,
                            "date": str(df.index[-1].date()),
                            "live": False,
                        }
                except Exception:
                    pass

    with _price_lock:
        _price_cache.update(prices)
        _price_cache_time = now
    return prices


def get_cached_prices(tickers):
    """Get prices from parquet cache only."""
    prices = {}
    cache_dir = PROJECT / "data" / "cache"
    for t in tickers:
        fp = cache_dir / (t.replace(".", "_") + ".parquet")
        if fp.exists():
            try:
                df = pd.read_parquet(fp)
                if len(df) > 0:
                    prices[t] = {
                        "close": float(df["close"].iloc[-1]),
                        "prev_close": float(df["close"].iloc[-2]) if len(df) > 1 else None,
                        "date": str(df.index[-1].date()),
                        "live": False,
                    }
            except Exception:
                pass
    return prices


@app.route("/api/live-data")
def api_live_data():
    config = safe_json(PROJECT / "config" / "active_config.json", {})

    # Portfolio
    state = safe_json(PROJECT / "paper_engine" / "portfolio_state.json", None)
    seq = config.get("risk", {}).get("starting_equity", 5000)
    if state is None:
        portfolio = {"cash": seq, "positions": [], "closed_trades": [],
                     "equity_history": [], "daily_high_water": seq,
                     "halted": False, "halt_reason": "", "starting_equity": seq}
    else:
        portfolio = state
        portfolio["starting_equity"] = seq

    positions = portfolio.get("positions", [])
    cash = portfolio.get("cash", seq)

    # Plan
    plans_dir = PROJECT / "paper_engine" / "plans"
    plan = None
    if plans_dir.exists():
        files = sorted(plans_dir.glob("plan_*.json"), reverse=True)
        if files:
            plan = safe_json(files[0], None)

    # Collect tickers
    pos_tickers = set(p.get("ticker", "") for p in positions) - {""}
    other_tickers = set()
    if plan:
        for e in plan.get("proposed_entries", []):
            other_tickers.add(e.get("ticker", ""))
        for e in plan.get("rejected_entries", []):
            other_tickers.add(e.get("ticker", ""))
    other_tickers.discard("")
    other_tickers -= pos_tickers

    # Live prices for positions during market hours
    now_aest = datetime.now(BRISBANE)
    is_market = now_aest.weekday() < 5 and 10 <= now_aest.hour < 17

    if is_market and pos_tickers:
        prices = fetch_live_prices(list(pos_tickers))
    else:
        prices = get_cached_prices(list(pos_tickers))
    prices.update(get_cached_prices(list(other_tickers)))

    # Equity
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
                dh = (now_aest.replace(tzinfo=None) - datetime.strptime(ed, "%Y-%m-%d")).days
            except Exception:
                pass
        open_pos.append({
            "ticker": t, "strategy": p.get("strategy", ""),
            "entry_date": ed, "entry_price": ep, "current_price": round(cp, 4),
            "shares": sh, "unrealized_pnl": upnl, "unrealized_pnl_pct": upnl_pct,
            "stop_price": p.get("stop_price", 0),
            "take_profit": p.get("take_profit"), "days_held": dh,
        })

    # Backtest / closed trades
    backtest = safe_json(PROJECT / "backtest" / "results" / "phase5_report.json", {})
    bt_metrics = backtest.get("final_metrics", {})
    ledger = safe_json(PROJECT / "journal" / "trade_ledger.json", [])
    closed = portfolio.get("closed_trades", []) or ledger or []
    wins = sum(1 for t in closed if t.get("pnl", 0) > 0)
    win_rate = round(wins / len(closed) * 100, 1) if closed else 0
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
    eq_curve = [{"date": e.get("date", ""), "equity": e.get("equity", seq)} for e in eq_hist] if eq_hist else [{"date": now_aest.strftime("%Y-%m-%d"), "equity": seq}]

    # Benchmark
    benchmark = []
    fp = PROJECT / "data" / "cache" / "IOZ_AX.parquet"
    if fp.exists():
        try:
            df = pd.read_parquet(fp)
            df = df.tail(252)
            first = float(df["close"].iloc[0])
            benchmark = [{"date": str(d.date()), "value": round(float(c) / first * 5000, 2)}
                         for d, c in zip(df.index, df["close"])]
        except Exception:
            pass

    # Watchlist
    watchlist = []
    if plan:
        for r in plan.get("rejected_entries", []):
            reason = r.get("rejection_reason", "")
            if "max positions" in reason.lower() or "exceeded" in reason.lower():
                tp = r.get("ticker", "")
                cp = prices[tp]["close"] if tp in prices else r.get("entry_price", 0)
                watchlist.append({
                    "ticker": tp, "strategy": r.get("strategy"),
                    "confidence": r.get("confidence", 0),
                    "entry_price": r.get("entry_price"),
                    "current_price": round(cp, 2),
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
