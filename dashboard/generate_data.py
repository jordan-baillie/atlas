#!/usr/bin/env python3
"""Generate dashboard-data.json for Atlas static dashboard.

Produces a JSON payload consumed by the single-page dashboard.
Includes portfolio state, today's plan, backtest metrics, and task tracker.

When trading.mode == "live" and broker == "moomoo", equity/cash/positions
are fetched from the live Moomoo account. Paper state is used for metadata
(strategy, entry_date, stop_price, confidence, rationale) that the broker
doesn't track.
"""

import json
import logging
import re
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

logger = logging.getLogger("atlas.dashboard")
BRISBANE = ZoneInfo("Australia/Brisbane")
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
OUTPUT = PROJECT_ROOT / "dashboard" / "data" / "dashboard-data.json"


def safe_json(path, default=None):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default if default is not None else {}


def get_config(market_id: str = "asx"):
    return safe_json(PROJECT_ROOT / "config" / "active" / f"{market_id}.json", {})


def get_portfolio(config):
    # Load from per-market state file first, fall back to legacy
    market_id = config.get("market", "asx")
    per_market = PROJECT_ROOT / "paper_engine" / "state" / f"{market_id}.json"
    legacy = PROJECT_ROOT / "paper_engine" / "portfolio_state.json"

    state = None
    if per_market.exists():
        state = safe_json(per_market, None)
    if state is None:
        state = safe_json(legacy, None)

    seq = config.get("risk", {}).get("starting_equity", 5000)
    if state is None:
        return {"cash": seq, "positions": [], "closed_trades": [],
                "equity_history": [], "halted": False, "starting_equity": seq}
    state["starting_equity"] = seq
    return state


def get_live_broker_data(config):
    """Fetch account info and positions from Moomoo broker.

    Returns (account_info_dict, positions_list, connected) or (None, [], False)
    on failure. Enriches broker positions with paper-state metadata
    (strategy, entry_date, stop_price, confidence, sector).
    """
    trading = config.get("trading", {})
    if trading.get("broker") != "moomoo" or not trading.get("live_enabled"):
        return None, [], False

    try:
        from brokers.moomoo.broker import MomooBroker

        broker = MomooBroker(config, live=True)
        if not broker.connect():
            logger.warning("Dashboard: broker connect failed")
            return None, [], False

        try:
            acct = broker.get_account_info()
            positions = broker.get_positions()
            open_orders = broker.get_open_orders() or []
        finally:
            broker.disconnect()

        if not acct:
            return None, [], False

        # Build account dict
        acct_data = {
            "equity": round(acct.equity, 2),
            "cash": round(acct.cash, 2),
            "market_value": round(acct.market_value, 2),
            "buying_power": round(acct.buying_power, 2),
            "total_pnl": round(acct.total_pnl, 2),
            "total_pnl_pct": round(acct.total_pnl_pct, 2),
            "num_positions": acct.num_positions,
            "currency": acct.currency,
        }

        # Build positions list — enrich with paper-state metadata
        paper_state = get_portfolio(config)
        paper_by_ticker = {}
        for p in paper_state.get("positions", []):
            paper_by_ticker[p.get("ticker", "")] = p

        pos_list = []
        for pos in positions:
            # Skip non-Atlas positions (e.g. manually held WDS, XOP)
            paper = paper_by_ticker.get(pos.ticker)

            pos_dict = {
                "ticker": pos.ticker,
                "entry_price": round(pos.entry_price, 4),
                "shares": pos.shares,
                "current_price": round(pos.current_price, 4),
                "market_value": round(pos.market_value, 2),
                "unrealized_pnl": round(pos.unrealized_pnl, 2),
                "unrealized_pnl_pct": round(pos.unrealized_pnl_pct, 2),
                "cost_basis": round(pos.cost_basis, 2),
                # Metadata from paper state (broker doesn't track these)
                "strategy": paper.get("strategy", "") if paper else "",
                "entry_date": paper.get("entry_date", "") if paper else "",
                "stop_price": paper.get("stop_price", 0) if paper else 0,
                "confidence": paper.get("confidence", 0) if paper else 0,
                "sector": paper.get("sector", "Unknown") if paper else pos.sector,
                "entry_value": paper.get("entry_value", pos.cost_basis) if paper else pos.cost_basis,
                "is_atlas": paper is not None,
            }
            pos_list.append(pos_dict)

        # Pending orders
        orders_list = []
        for o in open_orders:
            r = o.raw or {}
            from brokers.moomoo.mapper import to_atlas
            orders_list.append({
                "order_id": o.order_id,
                "ticker": to_atlas(r.get("code", o.ticker)),
                "side": o.side.value if o.side else r.get("trd_side", "?"),
                "qty": int(r.get("qty", o.requested_qty)),
                "price": round(float(r.get("price", o.requested_price)), 2),
                "order_type": r.get("order_type", "?"),
                "status": r.get("order_status", "?"),
                "created": r.get("create_time", ""),
                "filled_qty": int(r.get("dealt_qty", o.filled_qty)),
                "fill_price": round(float(r.get("dealt_avg_price", o.fill_price)), 2),
                "market": r.get("order_market", ""),
            })

        logger.info("Dashboard: broker data OK — equity=$%.2f, %d positions, %d orders",
                     acct_data["equity"], len(pos_list), len(orders_list))
        return acct_data, pos_list, True, orders_list

    except Exception as e:
        logger.error("Dashboard: broker fetch failed: %s", e, exc_info=True)
        return None, [], False, []


def get_latest_plan():
    plans_dir = PROJECT_ROOT / "paper_engine" / "plans"
    if not plans_dir.exists():
        return None
    files = sorted(plans_dir.glob("plan_*.json"), reverse=True)
    return safe_json(files[0], None) if files else None


def sync_broker_fills(market_id: str, broker_positions: list, config: dict):
    """Sync broker fills into paper state for allocation tracking.

    Compares broker positions (filtered to this market) with paper state.
    Any broker position whose ticker is in the approved plan but NOT in
    paper state is a new fill — record it immediately.

    Called on every dashboard refresh so fills show up within minutes.
    """
    from paper_engine.engine import PaperPortfolio

    portfolio = PaperPortfolio(config, market_id=market_id)
    paper_tickers = {p.ticker for p in portfolio.positions}

    # Load latest plan to find Atlas-managed entries
    plan = get_latest_plan()
    if not plan:
        return
    plan_entries = {e["ticker"]: e for e in plan.get("proposed_entries", [])}

    synced = 0
    for bp in broker_positions:
        ticker = bp.get("ticker", "")
        if ticker in paper_tickers:
            continue  # already tracked
        if ticker not in plan_entries:
            continue  # not an Atlas-managed position (manual hold)

        entry = plan_entries[ticker]
        fill_price = bp.get("entry_price", 0)
        shares = int(bp.get("shares", 0))
        if fill_price <= 0 or shares <= 0:
            continue

        trade_date = plan.get("trade_date", datetime.now(BRISBANE).strftime("%Y-%m-%d"))

        class _Sig:
            def __init__(self):
                self.ticker = ticker
                self.strategy = entry.get("strategy", "unknown")
                self.entry_price = fill_price
                self.stop_price = entry.get("stop_price", 0)
                self.take_profit = entry.get("take_profit")
                self.position_size = shares
                self.confidence = entry.get("confidence", 0)
                self.rationale = entry.get("rationale", "")
                self.sector = entry.get("sector", "Unknown")

        portfolio.execute_entry(_Sig(), fill_price, trade_date)
        synced += 1
        logger.info("Fill sync [%s]: %s %dx @ $%.2f → paper state",
                     market_id, ticker, shares, fill_price)

    if synced:
        logger.info("Synced %d new fills for %s", synced, market_id)


def get_live_prices(tickers):
    """Fetch live intraday prices via yfinance.

    Uses 15m interval for the current day. Falls back gracefully
    if market is closed or data unavailable.

    Returns dict of ticker -> {"close": float, "prev_close": float|None, "date": str, "live": bool}
    """
    prices = {}
    if not tickers:
        return prices

    ticker_list = list(tickers)
    try:
        import yfinance as yf
        # Batch download — single HTTP call for all tickers
        data = yf.download(ticker_list, period="2d", interval="15m",
                           progress=False, threads=True)
        if data.empty:
            return prices

        for t in ticker_list:
            try:
                if len(ticker_list) > 1:
                    series = data["Close"][t].dropna()
                else:
                    series = data["Close"].dropna()
                if len(series) == 0:
                    continue
                last_price = float(series.iloc[-1])
                prev_price = float(series.iloc[-2]) if len(series) > 1 else None
                last_ts = series.index[-1]
                prices[t] = {
                    "close": last_price,
                    "prev_close": prev_price,
                    "date": str(last_ts),
                    "live": True,
                }
            except Exception:
                pass
    except Exception as e:
        print(f"  WARN: live price fetch failed: {e}")

    return prices


def get_cache_prices(tickers):
    """Load prices from parquet cache (daily close data)."""
    prices = {}
    for subdir in ["asx", "sp500", ""]:
        cache = PROJECT_ROOT / "data" / "cache" / subdir if subdir else PROJECT_ROOT / "data" / "cache"
        if not cache.exists():
            continue
        for t in tickers:
            if t in prices:
                continue
            fp = cache / (t.replace(".", "_") + ".parquet")
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


def get_prices(tickers):
    """Get latest prices — live intraday first, cache fallback.

    During market hours: returns live 15-min delayed prices.
    Outside market hours: returns last daily close from cache.
    """
    if not tickers:
        return {}

    # Try live prices first
    prices = get_live_prices(tickers)

    # Fill any missing tickers from cache
    missing = tickers - set(prices.keys())
    if missing:
        cache_prices = get_cache_prices(missing)
        prices.update(cache_prices)

    return prices


def get_backtest_data():
    """Load backtest equity curve and metrics."""
    bt_curve_path = PROJECT_ROOT / "backtest" / "results" / "backtest_equity_curve.json"
    bt_report_path = PROJECT_ROOT / "backtest" / "results" / "phase5_report.json"

    curve_data = safe_json(bt_curve_path, None)
    report = safe_json(bt_report_path, {})

    result = {"equity_curve": [], "metrics": {}, "trade_markers": []}

    if curve_data:
        result["equity_curve"] = curve_data.get("equity_curve", [])
        result["metrics"] = curve_data.get("metrics", {})
        result["trade_markers"] = curve_data.get("trade_markers", [])

    # Merge final_metrics from phase5 report if available
    final = report.get("final_metrics", {})
    if final:
        result["report_metrics"] = final

    return result


def parse_tasks():
    """Parse tasks/todo.md into structured task lists."""
    todo_path = PROJECT_ROOT / "tasks" / "todo.md"
    if not todo_path.exists():
        return {"upcoming": [], "in_progress": [], "done": []}

    text = todo_path.read_text()
    tasks = {"upcoming": [], "in_progress": [], "done": []}
    current_section = None

    for line in text.splitlines():
        stripped = line.strip()

        lower = stripped.lower()
        if lower.startswith("## upcoming") or lower.startswith("## todo"):
            current_section = "upcoming"
            continue
        elif lower.startswith("## in progress") or lower.startswith("## active") or lower.startswith("## current"):
            current_section = "in_progress"
            continue
        elif lower.startswith("## done") or lower.startswith("## completed") or lower.startswith("## finished"):
            current_section = "done"
            continue
        elif stripped.startswith("## "):
            current_section = None
            continue

        if current_section is None:
            continue

        m = re.match(r'^-\s*\[(.)\]\s*(.+)$', stripped)
        if m:
            text_val = m.group(2).strip()
        elif stripped.startswith("- "):
            text_val = stripped[2:].strip()
        else:
            continue

        if text_val:
            tasks[current_section].append(text_val)

    return tasks


def _get_benchmark_curve(ticker: str, eq_curve: list, starting_equity: float) -> list:
    """Build a benchmark equity curve scaled to the same starting equity.

    Uses cached parquet data so no extra API calls needed.
    The benchmark is scaled so that on the first equity curve date,
    its value equals starting_equity — making visual comparison fair.
    """
    if not eq_curve:
        return []

    start_date = eq_curve[0]["date"]

    # Load benchmark from cache
    for subdir in ["sp500", "asx", ""]:
        cache = PROJECT_ROOT / "data" / "cache" / subdir if subdir else PROJECT_ROOT / "data" / "cache"
        fp = cache / (ticker.replace(".", "_") + ".parquet")
        if fp.exists():
            try:
                df = pd.read_parquet(fp)

                # Find the base price on or just before the start date
                on_or_before = df[df.index <= start_date]
                if len(on_or_before) == 0:
                    continue
                base_price = float(on_or_before["close"].iloc[-1])

                # Include data from start date onward (use on_or_before's
                # last row as the anchor point even if it's the day before)
                from_date = str(on_or_before.index[-1].date())
                df = df[df.index >= from_date]
                if len(df) == 0:
                    continue

                benchmark = []
                for idx, row in df.iterrows():
                    d = str(idx.date())
                    scaled = round(float(row["close"]) / base_price * starting_equity, 2)
                    benchmark.append({"date": d, "equity": scaled})

                return benchmark
            except Exception:
                continue
    return []


def generate_market(market_id: str, broker_cache: dict | None = None):
    """Generate dashboard data for a single market.

    broker_cache: optional dict to reuse a single broker connection.
      Keys: "acct", "positions", "ok". If None, connects fresh.
    """
    config = get_config(market_id)
    portfolio = get_portfolio(config)
    plan = get_latest_plan()
    ledger = safe_json(PROJECT_ROOT / "journal" / "trade_ledger.json", [])

    seq = portfolio.get("starting_equity", 5000)
    fees_cfg = config.get("fees", {})
    commission = fees_cfg.get("commission_per_trade", 3.0)

    trading = config.get("trading", {})
    is_live_mode = (trading.get("mode") == "live"
                    and trading.get("broker") == "moomoo"
                    and trading.get("live_enabled", False))

    # ── Try live broker data first ──────────────────────────────
    broker_acct, broker_positions, broker_ok = None, [], False
    if is_live_mode:
        if broker_cache and broker_cache.get("ok"):
            # Reuse shared broker connection — filter positions to this market
            broker_acct = broker_cache["acct"]
            all_broker_pos = broker_cache["positions"]
            # Filter positions by market
            if market_id == "sp500":
                broker_positions = [p for p in all_broker_pos if not p.get("ticker", "").endswith(".AX")]
            elif market_id == "asx":
                broker_positions = [p for p in all_broker_pos if p.get("ticker", "").endswith(".AX")]
            else:
                broker_positions = all_broker_pos
            broker_ok = True
        else:
            broker_acct, broker_positions, broker_ok, _orders = get_live_broker_data(config)

    if broker_ok and broker_acct:
        # Broker is the source of truth — no paper sync needed
        portfolio = get_portfolio(config)

        # Live mode: use broker for real-time positions/prices,
        # but equity/cash from paper state (tracks per-market allocation).
        positions = broker_positions
        atlas_positions = [p for p in positions if p.get("is_atlas", True)]
        manual_positions = [p for p in positions if not p.get("is_atlas", True)]
        all_positions = positions
        data_source = "moomoo"

        # Atlas P&L: only from Atlas-managed positions
        total_entry_value = sum(p.get("entry_value", 0) for p in atlas_positions)
        atlas_value = sum(p.get("market_value", 0) for p in atlas_positions)
        market_pnl = round(atlas_value - total_entry_value, 2)
        total_commissions = round(len(atlas_positions) * commission, 2)

        # Manual positions value (not managed by Atlas)
        manual_value = sum(p.get("market_value", 0) for p in manual_positions)

        # Equity/cash from paper state allocation (e.g. $4k for SP500)
        # NOT the full broker account balance
        paper_cash = portfolio.get("cash", seq)
        paper_pos_value = sum(p.get("market_value", 0) for p in atlas_positions)
        equity = round(paper_cash + paper_pos_value, 2)
        cash = paper_cash
        pos_value = paper_pos_value

        total_pnl = round(equity - seq, 2)
        total_pnl_pct = round(total_pnl / seq * 100, 2) if seq > 0 else 0
    else:
        # Paper/fallback mode
        positions = portfolio.get("positions", [])
        atlas_positions = positions
        all_positions = positions
        data_source = "paper"
        cash = portfolio.get("cash", seq)

        # Collect tickers needing prices
        tickers = {p.get("ticker", "") for p in positions}
        if plan:
            for e in plan.get("proposed_entries", []):
                tickers.add(e.get("ticker", ""))
        tickers.discard("")
        prices = get_prices(tickers)

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

        total_entry_value = sum(p.get("entry_value", 0) for p in positions)
        total_commissions = round(len(positions) * commission, 2)
        market_pnl = round(pos_value - total_entry_value, 2)

    # Realized P&L from closed trades
    closed = portfolio.get("closed_trades", []) or ledger or []
    realized_pnl = round(sum(t.get("pnl", 0) for t in closed), 2)

    # ── Open positions ──────────────────────────────────────────
    now = datetime.now(BRISBANE)
    open_pos = []
    strategy_stats = {}

    for p in all_positions:
        t = p.get("ticker", "")
        is_atlas = p.get("is_atlas", True)

        if broker_ok:
            # Broker already provides current_price and unrealized_pnl
            ep = p.get("entry_price", 0)
            sh = p.get("shares", 0)
            cp = p.get("current_price", ep)
            upnl = p.get("unrealized_pnl", round((cp - ep) * sh, 2))
            upnl_pct = p.get("unrealized_pnl_pct", round((cp - ep) / ep * 100, 2) if ep > 0 else 0)
        else:
            ep = p.get("entry_price", 0)
            sh = p.get("shares", 0)
            cp = prices[t]["close"] if t in prices else ep
            upnl = round((cp - ep) * sh, 2)
            upnl_pct = round((cp - ep) / ep * 100, 2) if ep > 0 else 0

        ed = p.get("entry_date", "")
        dh = 0
        if ed:
            try:
                entry_dt = datetime.strptime(ed, "%Y-%m-%d").replace(tzinfo=BRISBANE)
                dh = (now - entry_dt).days
            except Exception:
                pass

        strat = p.get("strategy", "") or ("manual" if not is_atlas else "unknown")
        # Only Atlas positions in strategy breakdown
        if is_atlas:
            if strat not in strategy_stats:
                strategy_stats[strat] = {"count": 0, "pnl": 0, "value": 0}
            strategy_stats[strat]["count"] += 1
            strategy_stats[strat]["pnl"] += upnl
            strategy_stats[strat]["value"] += cp * sh

        open_pos.append({
            "ticker": t, "strategy": strat,
            "entry_date": ed, "entry_price": ep, "current_price": round(cp, 4),
            "shares": sh, "pnl": upnl, "pnl_pct": upnl_pct,
            "stop_price": p.get("stop_price", 0),
            "days_held": dh, "sector": p.get("sector", ""),
            "is_atlas": is_atlas,
        })

    # Strategy performance summary
    strat_summary = []
    for s, data in sorted(strategy_stats.items()):
        strat_summary.append({
            "strategy": s,
            "positions": data["count"],
            "unrealized_pnl": round(data["pnl"], 2),
            "market_value": round(data["value"], 2),
        })

    # Plan summary
    plan_data = None
    if plan:
        plan_data = {
            "trade_date": plan.get("trade_date", ""),
            "status": plan.get("status", "UNKNOWN"),
            "entries": plan.get("proposed_entries", []),
            "exits": plan.get("proposed_exits", []),
        }

    # Closed trade stats
    wins = sum(1 for t in closed if t.get("pnl", 0) > 0)
    win_rate = round(wins / len(closed) * 100, 1) if closed else 0

    # ── Equity curve (per-market, persistent) ─────────────────
    curve_path = PROJECT_ROOT / "logs" / f"equity_curve_{market_id}.json"
    eq_curve = safe_json(curve_path, [])
    if not isinstance(eq_curve, list):
        eq_curve = []

    today_str = now.strftime("%Y-%m-%d")
    if not eq_curve or eq_curve[-1].get("date") != today_str:
        eq_curve.append({"date": today_str, "equity": round(equity, 2)})
    else:
        eq_curve[-1]["equity"] = round(equity, 2)

    # Persist
    with open(curve_path, "w") as f:
        json.dump(eq_curve, f, indent=2)

    # ── Benchmark (SPY for sp500, IOZ.AX for asx) ──────────
    benchmark_ticker = config.get("universe", {}).get("benchmark_ticker", "SPY")
    benchmark_curve = _get_benchmark_curve(benchmark_ticker, eq_curve, seq)

    # Risk
    risk_cfg = config.get("risk", {})
    invested = sum(p.get("entry_value", 0) for p in positions)
    exposure_pct = round(invested / equity * 100, 1) if equity > 0 else 0

    # Tasks
    tasks = parse_tasks()

    # Trading mode info
    dry_run = trading.get("live_safety", {}).get("dry_run_first", True)
    if is_live_mode and not dry_run:
        mode_label = "live"
    elif is_live_mode and dry_run:
        mode_label = "live_dry_run"
    else:
        mode_label = "paper"

    # Split positions into Atlas-managed and manual
    atlas_open = [p for p in open_pos if p.get("is_atlas", True)]
    manual_open = [p for p in open_pos if not p.get("is_atlas", True)]

    # Manual positions P&L
    manual_pnl = round(sum(p.get("pnl", 0) for p in manual_open), 2)
    manual_value = round(sum(p.get("current_price", 0) * p.get("shares", 0) for p in manual_open), 2)

    # Assemble
    result = {
        "timestamp": now.isoformat(),
        "config_version": config.get("version", "unknown"),
        "project": config.get("project", "Atlas"),
        "trading_mode": mode_label,
        "data_source": data_source if broker_ok else "paper",
        "broker": trading.get("broker", "paper"),
        "portfolio": {
            "equity": equity, "cash": round(cash, 2),
            "starting_equity": seq,
            "total_pnl": total_pnl, "total_pnl_pct": total_pnl_pct,
            "num_open": len(atlas_open), "num_atlas": len(atlas_open),
            "win_rate": win_rate,
            "commission_per_trade": commission,
            "total_commissions": total_commissions,
            "market_pnl": market_pnl,
            "realized_pnl": realized_pnl,
            "open_positions": atlas_open,
            "buying_power": broker_acct["buying_power"] if broker_ok else round(cash, 2),
        },
        "manual_positions": {
            "positions": manual_open,
            "num_open": len(manual_open),
            "unrealized_pnl": manual_pnl,
            "market_value": manual_value,
        },
        "strategy_summary": strat_summary,
        "equity_curve": eq_curve,
        "benchmark_curve": benchmark_curve,
        "benchmark_ticker": benchmark_ticker,
        "plan": plan_data,
        "closed_trades": closed,
        "risk": {
            "exposure_pct": exposure_pct,
            "max_positions": risk_cfg.get("max_open_positions", 10),
            "halted": portfolio.get("halted", False),
            "risk_per_trade": risk_cfg.get("risk_per_trade", 0.005),
            "max_portfolio_risk": risk_cfg.get("max_portfolio_risk", 0.05),
        },
        "tasks": tasks,
    }

    # Add pending orders from broker cache
    if broker_cache and broker_cache.get("orders"):
        # Filter orders to this market
        all_orders = broker_cache["orders"]
        if market_id == "sp500":
            market_orders = [o for o in all_orders if o.get("market") == "US"]
        elif market_id == "asx":
            market_orders = [o for o in all_orders if o.get("market") in ("AU", "")]
        else:
            market_orders = all_orders
        result["pending_orders"] = market_orders
    else:
        result["pending_orders"] = []

    return result


def generate_research_data() -> dict:
    """Generate research section data for the dashboard.

    Includes: queue status, recent results, cumulative impact,
    strategy coverage matrix, and next scheduled session.
    """
    research_dir = PROJECT_ROOT / "research"
    queue_path = research_dir / "queue.json"
    journal_path = research_dir / "journal.json"
    experiments_dir = research_dir / "experiments"

    # Queue
    queue = safe_json(queue_path, [])
    queued = [e for e in queue if e.get("status") == "queued"]
    running = [e for e in queue if e.get("status") in ("claimed", "running")]
    completed = [e for e in queue if e.get("status") in ("passed", "failed", "partial", "promoted", "rejected")]

    # Journal
    journal = safe_json(journal_path, [])

    # Recent results (last 10)
    recent_results = []
    for entry in journal[-10:]:
        recent_results.append({
            "experiment_id": entry.get("experiment_id", "?"),
            "timestamp": entry.get("timestamp", ""),
            "market": entry.get("market", "?"),
            "strategy": entry.get("strategy", "N/A"),
            "category": entry.get("category", "?"),
            "verdict": entry.get("verdict", "?"),
            "key_metrics": entry.get("key_metrics", {}),
            "delta_vs_baseline": entry.get("delta_vs_baseline", {}),
            "promoted": entry.get("promoted", False),
            "learnings": entry.get("learnings", []),
        })

    # Cumulative impact from promoted experiments
    promoted_entries = [e for e in journal if e.get("promoted")]
    cumulative_sharpe_delta = sum(
        e.get("delta_vs_baseline", {}).get("sharpe", 0)
        for e in promoted_entries
    )

    # Strategy coverage matrix
    all_strategies = set()
    all_markets = set()
    coverage = {}  # {strategy: {market: {last_tested, verdict, experiment_id}}}
    for entry in journal:
        strat = entry.get("strategy")
        market = entry.get("market")
        if strat and market:
            all_strategies.add(strat)
            all_markets.add(market)
            key = f"{strat}:{market}"
            coverage[key] = {
                "strategy": strat,
                "market": market,
                "last_tested": entry.get("timestamp", ""),
                "verdict": entry.get("verdict", "?"),
                "experiment_id": entry.get("experiment_id", "?"),
            }

    # Experiment statistics
    total_experiments = len(journal)
    passed_count = sum(1 for e in journal if e.get("verdict") == "pass")
    failed_count = sum(1 for e in journal if e.get("verdict") == "fail")
    partial_count = sum(1 for e in journal if e.get("verdict") == "partial")
    promoted_count = len(promoted_entries)

    return {
        "queue": {
            "total": len(queue),
            "queued": len(queued),
            "running": len(running),
            "completed": len(completed),
            "items": [
                {
                    "id": e.get("id", "?"),
                    "title": e.get("title", "?"),
                    "priority": e.get("priority", "?"),
                    "category": e.get("category", "?"),
                    "market": e.get("market", "?"),
                    "status": e.get("status", "?"),
                    "estimated_runtime_min": e.get("estimated_runtime_min", 0),
                }
                for e in queue
            ],
        },
        "recent_results": recent_results,
        "statistics": {
            "total_experiments": total_experiments,
            "passed": passed_count,
            "failed": failed_count,
            "partial": partial_count,
            "promoted": promoted_count,
            "pass_rate_pct": round(passed_count / total_experiments * 100, 1) if total_experiments else 0,
        },
        "cumulative_impact": {
            "sharpe_delta": round(cumulative_sharpe_delta, 4),
            "promotions": promoted_count,
        },
        "strategy_coverage": list(coverage.values()),
        "strategies_tested": sorted(all_strategies),
        "markets_tested": sorted(all_markets),
    }


def generate():
    """Generate multi-market dashboard data.

    Connects to broker once, shares the connection for all live markets,
    then writes a combined payload to dashboard-data.json.
    """
    from markets.registry import list_markets

    markets = list_markets()  # ['asx', 'sp500']

    # ── Single broker connection shared across markets ──────────
    broker_cache = None
    for mid in markets:
        cfg = get_config(mid)
        trading = cfg.get("trading", {})
        if (trading.get("mode") == "live"
                and trading.get("broker") == "moomoo"
                and trading.get("live_enabled", False)):
            acct, positions, ok, orders = get_live_broker_data(cfg)
            if ok:
                broker_cache = {"acct": acct, "positions": positions, "ok": True, "orders": orders}
            break  # one connection serves all markets (same Moomoo account)

    # ── Generate per-market data ────────────────────────────────
    market_data = {}
    for mid in markets:
        print(f"\n  Generating {mid}...")
        data = generate_market(mid, broker_cache=broker_cache)
        market_data[mid] = data

    # ── Pick primary market for top-level fields (backward compat) ──
    # Use sp500 if it's in live mode, otherwise asx
    primary = "sp500" if "sp500" in market_data else markets[0]
    primary_data = market_data[primary]

    # ── Merge: combine positions and stats from all markets ─────
    all_positions = []
    all_manual = []
    all_closed = []
    total_equity = 0
    total_cash = 0
    combined_strats = {}

    for mid, md in market_data.items():
        pf = md.get("portfolio", {})
        # Tag positions with market
        for p in pf.get("open_positions", []):
            p["market"] = mid
            all_positions.append(p)
        for p in md.get("manual_positions", {}).get("positions", []):
            p["market"] = mid
            all_manual.append(p)
        for t in md.get("closed_trades", []):
            t["market"] = mid
            all_closed.append(t)
        # Strategy summary merge
        for s in md.get("strategy_summary", []):
            key = s["strategy"]
            if key not in combined_strats:
                combined_strats[key] = {"strategy": key, "positions": 0, "unrealized_pnl": 0, "market_value": 0}
            combined_strats[key]["positions"] += s["positions"]
            combined_strats[key]["unrealized_pnl"] += s["unrealized_pnl"]
            combined_strats[key]["market_value"] += s["market_value"]

    # ── Build combined result ───────────────────────────────────
    now = datetime.now(BRISBANE)
    broker_acct_data = broker_cache["acct"] if broker_cache else None

    # ── Research data ──────────────────────────────────────────
    research_data = generate_research_data()

    result = {
        "timestamp": now.isoformat(),
        "project": "Atlas",
        "markets": market_data,
        # Top-level summary (combined across markets)
        "trading_mode": primary_data.get("trading_mode", "paper"),
        "data_source": primary_data.get("data_source", "paper"),
        "broker": primary_data.get("broker", "paper"),
        "config_version": {mid: md.get("config_version", "?") for mid, md in market_data.items()},
        "account": {
            "equity": broker_acct_data["equity"] if broker_acct_data else 0,
            "cash": broker_acct_data["cash"] if broker_acct_data else 0,
            "buying_power": broker_acct_data["buying_power"] if broker_acct_data else 0,
            "currency": broker_acct_data["currency"] if broker_acct_data else "AUD",
        } if broker_acct_data else None,
        "portfolio": primary_data.get("portfolio", {}),
        "manual_positions": {
            "positions": all_manual,
            "num_open": len(all_manual),
            "unrealized_pnl": round(sum(p.get("pnl", 0) for p in all_manual), 2),
            "market_value": round(sum(p.get("current_price", 0) * p.get("shares", 0) for p in all_manual), 2),
        },
        "strategy_summary": list(combined_strats.values()),
        "equity_curve": primary_data.get("equity_curve", []),
        "benchmark_curve": primary_data.get("benchmark_curve", []),
        "benchmark_ticker": primary_data.get("benchmark_ticker", "SPY"),
        "plan": primary_data.get("plan"),
        "closed_trades": all_closed,
        "risk": primary_data.get("risk", {}),
        "tasks": primary_data.get("tasks", {}),
        "research": research_data,
    }

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT, "w") as f:
        json.dump(result, f, indent=2, default=str)

    print(f"\nDashboard data written to {OUTPUT}")
    for mid, md in market_data.items():
        pf = md.get("portfolio", {})
        label = f"{'🔴 LIVE' if md.get('trading_mode') == 'live' else '📝 PAPER'}"
        print(f"  {mid.upper():6s} {label}: ${pf.get('equity',0):,.2f} equity, "
              f"{pf.get('num_open',0)} positions, "
              f"v{md.get('config_version','?')}")
    if broker_acct_data:
        print(f"  BROKER: ${broker_acct_data['equity']:,.2f} total equity, "
              f"${broker_acct_data['cash']:,.2f} cash, "
              f"${broker_acct_data['buying_power']:,.2f} buying power")


if __name__ == "__main__":
    generate()
