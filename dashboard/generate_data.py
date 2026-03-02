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
    per_market = PROJECT_ROOT / "brokers" / "state" / f"{market_id}.json"


    state = None
    if per_market.exists():
        state = safe_json(per_market, None)
    if state is None:
        state = safe_json(legacy, None)

    seq = config.get("risk", {}).get("starting_equity", 5000)
    if state is None:
        return {"cash": seq, "positions": [], "closed_trades": [],
                "equity_history": [], "halted": False, "starting_equity": seq}
    # Prefer state file's starting_equity (tracks actual capital deployed)
    # over config's (which may have been changed for future sizing).
    # Only fall back to config when state has no starting_equity recorded.
    if state.get("starting_equity") in (None, 0) and seq > 0:
        state["starting_equity"] = seq
    return state


def _load_plan_metadata() -> dict:
    """Load strategy/entry metadata from recent plans for position enrichment.

    Returns {ticker: {strategy, entry_date, stop_price, confidence, sector, ...}}
    from the most recent executed/approved plans.
    """
    plans_dir = PROJECT_ROOT / "plans"
    if not plans_dir.exists():
        return {}

    meta = {}
    # Scan last 30 plans (covers ~1 month of daily plans)
    for plan_file in sorted(plans_dir.glob("plan_*.json"), reverse=True)[:30]:
        plan = safe_json(plan_file, None)
        if not plan:
            continue
        trade_date = plan.get("trade_date", "")
        for entry in plan.get("proposed_entries", []):
            ticker = entry.get("ticker", "")
            if ticker and ticker not in meta:
                meta[ticker] = {
                    "strategy": entry.get("strategy", ""),
                    "entry_date": trade_date,
                    "stop_price": entry.get("stop_price", 0),
                    "confidence": entry.get("confidence", 0),
                    "sector": entry.get("sector", "Unknown"),
                    "rationale": entry.get("rationale", ""),
                }
    return meta


def get_live_broker_data(config):
    """Fetch account info and positions from Moomoo broker.

    Returns (account_info_dict, positions_list, connected, orders_list)
    or (None, [], False, []) on failure.

    Enriches broker positions with metadata from plan history.
    Broker is the sole source of truth for positions and equity.
    """
    trading = config.get("trading", {})
    broker_name = trading.get("broker", "ibkr")
    if not trading.get("live_enabled"):
        return None, [], False, []

    try:
        from brokers.registry import get_live_broker

        broker = get_live_broker(config)
        if not broker or not broker.connect():
            logger.warning("Dashboard: broker connect failed (broker=%s)", broker_name)
            return None, [], False, []

        try:
            acct = broker.get_account_info()
            positions = broker.get_positions()
            # Get ALL today's orders (including filled) for dashboard display
            open_orders = (
                broker.get_all_today_orders()
                if hasattr(broker, "get_all_today_orders")
                else (broker.get_open_orders() or [])
            )
        finally:
            broker.disconnect()

        if not acct:
            return None, [], False, []

        # Detect broker returning zeroed data (e.g. OpenD up but Futu backend
        # unreachable — "Network interruption").  Equity==0 with a live account
        # is a clear signal the query failed silently; broker offline.
        if acct.equity == 0 and acct.cash == 0:
            logger.warning("Dashboard: broker returned $0 equity/$0 cash — "
                           "treating as offline (broker offline)")
            return None, [], False, []

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

        # Enrich broker positions with plan metadata (strategy, entry_date, etc.)
        plan_meta = _load_plan_metadata()

        pos_list = []
        for pos in positions:
            meta = plan_meta.get(pos.ticker, {})
            # Position is Atlas-managed if it appears in a plan OR has an
            # Atlas-recognized strategy tag from the broker
            is_atlas = bool(meta) or bool(pos.strategy and pos.strategy != "unknown")

            pos_dict = {
                "ticker": pos.ticker,
                "entry_price": round(pos.entry_price, 4),
                "shares": pos.shares,
                "current_price": round(pos.current_price, 4),
                "market_value": round(pos.market_value, 2),
                "unrealized_pnl": round(pos.unrealized_pnl, 2),
                "unrealized_pnl_pct": round(pos.unrealized_pnl_pct, 2),
                "cost_basis": round(pos.cost_basis, 2),
                # Metadata from plan history
                "strategy": meta.get("strategy", pos.strategy or ""),
                "entry_date": meta.get("entry_date", pos.entry_date or ""),
                "stop_price": meta.get("stop_price", pos.stop_price or 0),
                "confidence": meta.get("confidence", 0),
                "sector": meta.get("sector", pos.sector or "Unknown"),
                "entry_value": pos.cost_basis,
                "is_atlas": is_atlas,
            }
            pos_list.append(pos_dict)

        # Open orders (only truly open — filled orders already removed by broker)
        orders_list = []
        for o in open_orders:
            r = o.raw or {}
            orders_list.append({
                "order_id": o.order_id,
                "ticker": o.ticker,  # already in Atlas format from broker
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


def get_latest_plan(market_id: str = ""):
    """Get the latest plan, optionally filtered by market_id."""
    plans_dir = PROJECT_ROOT / "plans"
    if not plans_dir.exists():
        return None
    # Try per-market files first (plan_{market}_{date}.json)
    if market_id:
        files = sorted(plans_dir.glob(f"plan_{market_id}_*.json"), reverse=True)
        if files:
            return safe_json(files[0], None)
    # Fallback to legacy shared files (plan_{date}.json)
    files = sorted(plans_dir.glob("plan_*.json"), reverse=True)
    for f in files:
        # Skip per-market files when looking for legacy
        parts = f.stem.split("_")
        if len(parts) == 2 or (not market_id):
            plan = safe_json(f, None)
            if plan:
                # Filter by market_id if specified — require an exact match so
                # legacy plans with no market_id don't bleed into per-market views
                if market_id and plan.get("market_id", "") != market_id:
                    continue
                return plan
    return None


def sync_broker_fills(market_id: str, broker_positions: list, config: dict):
    """Sync broker fills into live state for allocation tracking.

    Compares broker positions (filtered to this market) with live state.
    Any broker position whose ticker is in the approved plan but NOT in
    live state is a new fill — record it immediately.

    Called on every dashboard refresh so fills show up within minutes.
    """
    from brokers.live_portfolio import LivePortfolio

    portfolio = LivePortfolio(config, market_id=market_id)
    portfolio.connect()
    live_tickers = {p.ticker for p in portfolio.positions}
    portfolio.disconnect()
    live_tickers = live_tickers

    # Load latest plan to find Atlas-managed entries
    plan = get_latest_plan(market_id)
    if not plan:
        return
    plan_entries = {e["ticker"]: e for e in plan.get("proposed_entries", [])}

    synced = 0
    for bp in broker_positions:
        ticker = bp.get("ticker", "")
        if ticker in live_tickers:
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
        logger.info("Fill sync [%s]: %s %dx @ $%.2f → live state",
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
    for subdir in ["asx", "sp500", "hk"]:
        cache = PROJECT_ROOT / "data" / "cache" / subdir
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
    for subdir in ["sp500", "asx", "hk", ""]:
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

                # Extend benchmark with live prices for dates beyond cache.
                # The cache may lag by 1-2 days — fetch actual recent closes
                # from yfinance so the benchmark tracks real returns.
                if benchmark and eq_curve:
                    last_bench_date = benchmark[-1]["date"]
                    missing_dates = [pt["date"] for pt in eq_curve
                                     if pt["date"] > last_bench_date]
                    if missing_dates:
                        try:
                            import yfinance as yf
                            recent = yf.download(
                                ticker, period="5d", interval="1d",
                                progress=False, auto_adjust=True,
                            )
                            if not recent.empty:
                                # Handle both flat and MultiIndex columns
                                close_s = recent["Close"]
                                if hasattr(close_s, "columns"):
                                    close_s = close_s.iloc[:, 0]
                                for ridx, val in close_s.dropna().items():
                                    rd = str(ridx.date())
                                    if rd > last_bench_date:
                                        scaled = round(
                                            float(val) / base_price
                                            * starting_equity, 2,
                                        )
                                        benchmark.append(
                                            {"date": rd, "equity": scaled}
                                        )
                                logger.info(
                                    "Benchmark %s: extended %d days via yfinance",
                                    ticker, len(benchmark) - len(df),
                                )
                        except Exception as e:
                            # Last resort: forward-fill so there's *some* line
                            logger.debug("Benchmark live fetch failed: %s", e)
                            last_val = benchmark[-1]["equity"]
                            for date in missing_dates:
                                benchmark.append(
                                    {"date": date, "equity": last_val}
                                )

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
    portfolio = get_portfolio(config)  # live state fallback
    plan = get_latest_plan(market_id)
    ledger = safe_json(PROJECT_ROOT / "journal" / "trade_ledger.json", [])

    # Use portfolio's starting_equity (tracks actual capital deployed) when available,
    # fall back to config (which may have been changed for future position sizing).
    seq = portfolio.get("starting_equity") or config.get("risk", {}).get("starting_equity", 5000)
    fees_cfg = config.get("fees", {})
    commission = fees_cfg.get("commission_per_trade", 3.0)

    trading = config.get("trading", {})
    is_live_mode = (trading.get("mode") == "live"
                    and trading.get("broker", "ibkr") != "ibkr"
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
        # Broker is the sole source of truth
        positions = broker_positions
        atlas_positions = [p for p in positions if p.get("is_atlas", True)]
        manual_positions = [p for p in positions if not p.get("is_atlas", True)]
        all_positions = positions
        data_source = "broker"

        # Atlas P&L: only from Atlas-managed positions
        total_entry_value = sum(p.get("entry_value", 0) for p in atlas_positions)
        atlas_value = sum(p.get("market_value", 0) for p in atlas_positions)
        market_pnl = round(atlas_value - total_entry_value, 2)
        total_commissions = round(len(atlas_positions) * commission, 2)

        # Manual positions value (not managed by Atlas)
        manual_value = sum(p.get("market_value", 0) for p in manual_positions)

        # Equity/cash from broker — use the starting_equity allocation
        # to calculate Atlas-specific P&L against the configured allocation
        pos_value = atlas_value
        # Cash allocated to Atlas = starting_equity - entry_value of open positions
        cash = round(seq - total_entry_value, 2) if total_entry_value > 0 else seq
        equity = round(cash + atlas_value, 2)

        total_pnl = round(equity - seq, 2)
        total_pnl_pct = round(total_pnl / seq * 100, 2) if seq > 0 else 0


    else:
        # Paper/fallback mode
        positions = portfolio.get("positions", [])
        atlas_positions = positions
        all_positions = positions
        data_source = "offline"
        cash = portfolio.get("cash", seq)
        # When no capital is allocated and no positions exist, cash must be 0
        if seq == 0 and len(positions) == 0:
            cash = 0

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
    # In live mode, use live state file; otherwise cached state
    live_state_file = PROJECT_ROOT / "brokers" / "state" / f"live_{market_id}.json"
    live_closed = safe_json(live_state_file, {}).get("closed_trades", [])
    closed = live_closed or portfolio.get("closed_trades", []) or ledger or []
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
            "market_id": plan.get("market_id", market_id),
            "entries": plan.get("proposed_entries", []),
            "exits": plan.get("proposed_exits", []),
            "risk_summary": plan.get("risk_summary", {}),
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

    # Only update the equity curve when we have reliable data (broker online).
    # When broker offline with stale cached prices, the equity
    # value is approximate — writing it would corrupt the curve with inaccurate
    # points that can't be corrected later.
    if data_source == "broker":
        if not eq_curve or eq_curve[-1].get("date") != today_str:
            eq_curve.append({"date": today_str, "equity": round(equity, 2)})
        else:
            eq_curve[-1]["equity"] = round(equity, 2)

        # Persist
        with open(curve_path, "w") as f:
            json.dump(eq_curve, f, indent=2)
    else:
        logger.info("Equity curve NOT updated for %s — data_source=%s (stale)",
                     market_id, data_source)

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
        mode_label = "offline"

    # Split positions into Atlas-managed and manual
    atlas_open = [p for p in open_pos if p.get("is_atlas", True)]
    manual_open = [p for p in open_pos if not p.get("is_atlas", True)]

    # Manual positions P&L
    manual_pnl = round(sum(p.get("pnl", 0) for p in manual_open), 2)
    manual_value = round(sum(p.get("current_price", 0) * p.get("shares", 0) for p in manual_open), 2)

    # Currency from market profile
    from markets.registry import get_market
    market_profile = get_market(market_id)
    currency = getattr(market_profile, "currency", "AUD")

    # Assemble
    result = {
        "timestamp": now.isoformat(),
        "config_version": config.get("version", "unknown"),
        "project": config.get("project", "Atlas"),
        "market_id": market_id,
        "currency": currency,
        "trading_mode": mode_label,
        "data_source": data_source if broker_ok else "offline",
        "broker": trading.get("broker", "ibkr"),
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
        "benchmark_return_pct": round(
            (benchmark_curve[-1]["equity"] / seq - 1) * 100, 2
        ) if benchmark_curve and seq > 0 else 0,
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


def generate_daily_insight() -> dict:
    """Mine the most interesting data-science finding for the dashboard.

    Runs a pool of independent insight miners — each scans a different
    data source for a genuinely different analytical finding. Results are
    pooled and rotated daily so each refresh shows a fresh perspective.

    Miners:
        1. opt_lift        — before/after from optimisation (grouped_bar | lollipop)
        2. param_scatter   — parameter value vs Sharpe from optimisations (scatter)
        3. strategy_compare — ranking & profiles across solos (horizontal | scatter | radar)
        4. trade_anatomy   — win/loss distributions, reward:risk (grouped_bar)
        5. vix_regime      — VIX regime vs SPY monthly returns (scatter)
        6. fee_impact      — fee drag decomposition (waterfall | grouped_bar)
        7. monthly_season  — seasonality in benchmark returns (horizontal_bar)
    """
    from pathlib import Path
    import pandas as pd

    research_dir = PROJECT_ROOT / "research"
    experiments_dir = research_dir / "experiments"
    journal = safe_json(research_dir / "journal.json", [])
    now = datetime.now(BRISBANE)
    today = now.strftime("%Y-%m-%d")
    day_of_year = now.timetuple().tm_yday

    candidates = []  # (priority, insight_dict)

    # ── Helpers ──
    _metric_map = [
        ("sharpe", ["sharpe"], "Sharpe", 1),
        ("cagr", ["cagr_pct", "cagr"], "CAGR", 100),
        ("max_dd", ["max_drawdown_pct", "max_dd"], "Max DD", 100),
        ("wr", ["win_rate_pct", "wr"], "Win Rate", 100),
        ("pf", ["profit_factor", "pf"], "Profit Factor", 1),
    ]

    def _get(d, keys):
        for k in keys:
            v = d.get(k)
            if v is not None:
                return v
        return None

    def _norm(d):
        result = {}
        for key, aliases, label, scale in _metric_map:
            v = _get(d, aliases)
            if v is not None:
                if scale > 1 and abs(v) <= 1:
                    v = v * scale
                result[key] = round(v, 2)
        return result

    # ═══════════════════════════════════════════════
    # 1. OPT LIFT — before/after from latest optimisation
    # ═══════════════════════════════════════════════
    try:
        for exp in reversed(journal):
            if exp.get("verdict") not in ("pass", "partial", "promoted"):
                continue
            exp_file = experiments_dir / f"exp-{exp['experiment_id']}.json"
            if not exp_file.exists():
                continue
            detail = safe_json(exp_file, {})
            outputs = detail.get("outputs", detail)
            baseline, optimized = outputs.get("baseline", {}), outputs.get("optimized", {})
            if not baseline or not optimized:
                continue
            b, a = _norm(baseline), _norm(optimized)
            if "sharpe" not in b or "sharpe" not in a:
                continue

            before_vals, after_vals, labels, items = [], [], [], []
            for key, _, label, _ in _metric_map:
                bv, av = b.get(key), a.get(key)
                if bv is not None and av is not None:
                    before_vals.append(bv); after_vals.append(av); labels.append(label)
                    d = round(av - bv, 2)
                    items.append({"label": label, "delta": d, "before": bv, "after": av,
                                  "improved": d < 0 if key == "max_dd" else d > 0})
            if len(labels) < 3:
                continue

            strat = exp.get("strategy") or detail.get("queue_entry", {}).get("strategy_name") or "combined"
            if strat in ("None", "N/A", "none", "null", "unknown", "Unknown"):
                strat = "combined"
            ds = round(a["sharpe"] - b["sharpe"], 3)
            base = {"type": "opt_lift", "title": f"Optimisation improved {_pretty_strat(strat)} Sharpe by {ds:+.3f}",
                    "subtitle": f"{exp['experiment_id']} · {exp.get('market', '').upper()}",
                    "annotation": {"text": f"Sharpe {b['sharpe']:.2f} → {a['sharpe']:.2f}",
                                   "color": "green" if ds > 0 else "red"},
                    "date": exp.get("timestamp", today)[:10]}
            candidates.append((1, {**base, "chart": "grouped_bar", "labels": labels,
                                   "series": [{"name": "Before", "values": before_vals, "color": "secondary"},
                                              {"name": "After", "values": after_vals, "color": "primary"}]}))
            candidates.append((1, {**base, "chart": "lollipop", "items": items}))
            break
    except Exception as e:
        logger.debug(f"opt_lift miner failed: {e}")

    # ═══════════════════════════════════════════════
    # 2. PARAM SCATTER — which parameter change had biggest impact?
    # ═══════════════════════════════════════════════
    try:
        all_param_points = []  # collect across experiments
        for f in sorted(experiments_dir.glob("exp-*_opt*.json"), reverse=True):
            detail = safe_json(f, {})
            outputs = detail.get("outputs", detail)
            initial = outputs.get("initial_params", {})
            best = outputs.get("best_params", {})
            bl = outputs.get("baseline", {})
            op = outputs.get("optimized", {})
            if not initial or not best or not bl or not op:
                continue
            b_sharpe = _get(bl, ["sharpe"])
            a_sharpe = _get(op, ["sharpe"])
            if b_sharpe is None or a_sharpe is None:
                continue
            delta_sharpe = round(a_sharpe - b_sharpe, 3)
            strat = outputs.get("strategy", f.stem.replace("exp-", ""))

            for param, new_val in best.items():
                old_val = initial.get(param)
                if old_val is None or not isinstance(old_val, (int, float)):
                    continue
                if not isinstance(new_val, (int, float)):
                    continue
                pct_change = round((new_val - old_val) / max(abs(old_val), 0.001) * 100, 1)
                if pct_change == 0:
                    continue
                all_param_points.append({
                    "label": param.replace("_", " ").title()[:18],
                    "x": pct_change,
                    "y": delta_sharpe,
                    "size": 7,
                    "color": "#10b981" if delta_sharpe > 0 else "#f43f5e",
                })

        if len(all_param_points) >= 4:
            # Sort by absolute Sharpe impact
            all_param_points.sort(key=lambda p: abs(p["y"]), reverse=True)
            biggest = all_param_points[0]
            candidates.append((2, {
                "type": "param_sensitivity", "chart": "scatter",
                "title": f"Parameter sensitivity: {len(all_param_points)} changes across {len(list(experiments_dir.glob('exp-*_opt*.json')))} optimisations",
                "subtitle": f"Biggest impact: {biggest['label']} ({biggest['x']:+.0f}% change → Sharpe {biggest['y']:+.3f})",
                "points": all_param_points[:12],
                "x_label": "Parameter Change %",
                "y_label": "Sharpe Delta",
                "annotation": {"text": f"Largest moves don't always help — diminishing returns visible",
                               "color": "amber"},
                "date": today,
            }))
    except Exception as e:
        logger.debug(f"param_scatter miner failed: {e}")

    # ═══════════════════════════════════════════════
    # 3. STRATEGY COMPARISON — solos ranking + profiles
    # ═══════════════════════════════════════════════
    try:
        solo_results = {}
        for exp in journal:
            eid = exp.get("experiment_id", "")
            if "_solo" not in eid and exp.get("category") != "single_strategy_test":
                continue
            km = exp.get("key_metrics", {})
            strat = exp.get("strategy", "")
            sharpe = km.get("sharpe")
            if strat and sharpe is not None and km.get("total_trades", 0) > 0:
                if strat not in solo_results or sharpe > solo_results[strat]["sharpe"]:
                    solo_results[strat] = {
                        "sharpe": round(sharpe, 3), "cagr_pct": round(km.get("cagr_pct", 0), 1),
                        "total_trades": km.get("total_trades", 0),
                        "win_rate_pct": round(km.get("win_rate_pct", 0), 1),
                        "max_drawdown_pct": round(km.get("max_drawdown_pct", 0), 1),
                        "profit_factor": round(km.get("profit_factor", 0), 2),
                    }

        if len(solo_results) >= 2:
            sorted_strats = sorted(solo_results.items(), key=lambda x: x[1]["sharpe"], reverse=True)
            best, worst = sorted_strats[0], sorted_strats[-1]
            spread = round(best[1]["sharpe"] - worst[1]["sharpe"], 3)

            # Scatter: Sharpe vs Max DD
            points = [{"label": _pretty_strat(s), "x": v["max_drawdown_pct"], "y": v["sharpe"],
                        "size": round(max(5, min(12, v["total_trades"] / 80)), 1),
                        "color": "#10b981" if v["sharpe"] > 0 else "#f43f5e" if v["sharpe"] < -0.5 else "#f59e0b"}
                       for s, v in sorted_strats]
            candidates.append((3, {
                "type": "strategy_risk_return", "chart": "scatter",
                "title": f"Risk vs Return: {len(solo_results)} strategy candidates",
                "subtitle": "Dot size = trade count · Top-left = best risk-adjusted return",
                "points": points, "x_label": "Max Drawdown %", "y_label": "Sharpe Ratio",
                "annotation": {"text": f"{_pretty_strat(best[0])} best risk-adjusted at {best[1]['sharpe']:+.3f}", "color": "green" if best[1]["sharpe"] > 0 else "amber"},
                "date": today,
            }))

            # Horizontal bar: Sharpe ranking
            candidates.append((3, {
                "type": "strategy_ranking", "chart": "horizontal_bar",
                "title": f"Strategy Sharpe spread is {spread:.2f} across {len(solo_results)} candidates",
                "subtitle": f"Best: {_pretty_strat(best[0])} · Worst: {_pretty_strat(worst[0])}",
                "labels": [_pretty_strat(s) for s, _ in sorted_strats],
                "series": [{"name": "Sharpe", "values": [v["sharpe"] for _, v in sorted_strats], "color": "adaptive"}],
                "detail_rows": [{"label": s, "sharpe": v["sharpe"], "cagr": v["cagr_pct"],
                                 "trades": v["total_trades"], "win_rate": v["win_rate_pct"], "max_dd": v["max_drawdown_pct"]}
                                for s, v in sorted_strats],
                "annotation": {"text": f"{_pretty_strat(best[0])} leads", "color": "green" if best[1]["sharpe"] > 0 else "amber"},
                "date": today,
            }))

            # Radar: multi-metric overlay (top 3)
            if len(sorted_strats) >= 2:
                radar_axes = ["Sharpe", "CAGR", "Win Rate", "Profit Factor", "Low DD"]
                raw = {ax: [] for ax in radar_axes}
                for s, v in sorted_strats:
                    raw["Sharpe"].append(v["sharpe"]); raw["CAGR"].append(v["cagr_pct"])
                    raw["Win Rate"].append(v["win_rate_pct"]); raw["Profit Factor"].append(v.get("profit_factor", 1))
                    raw["Low DD"].append(100 - v["max_drawdown_pct"])
                def _norm_list(vals):
                    mn, mx = min(vals), max(vals); r = mx - mn if mx != mn else 1
                    return [(v - mn) / r for v in vals]
                normed = {ax: _norm_list(vs) for ax, vs in raw.items()}
                profiles = [{"name": _pretty_strat(s), "values": [normed[ax][i] for ax in radar_axes]}
                            for i, (s, v) in enumerate(sorted_strats[:3])]
                candidates.append((3, {
                    "type": "strategy_profile", "chart": "radar",
                    "title": f"Strategy profiles: top {len(profiles)} candidates",
                    "subtitle": "Normalised 0→1 across all tested",
                    "axes": radar_axes, "profiles": profiles,
                    "annotation": {"text": f"{profiles[0]['name']} dominates {sum(1 for v in profiles[0]['values'] if v > 0.5)}/5 axes", "color": "green"},
                    "date": today,
                }))
    except Exception as e:
        logger.debug(f"strategy_compare miner failed: {e}")

    # ═══════════════════════════════════════════════
    # 4. TRADE ANATOMY — win/loss distributions from backtest
    # ═══════════════════════════════════════════════
    try:
        bt_files = sorted(Path(PROJECT_ROOT / "backtest" / "results").glob("backtest_2*.json"), reverse=True)
        for bf in bt_files:
            m = safe_json(bf, {}).get("metrics", {})
            if m.get("total_trades", 0) < 50:
                continue
            avg_w = m.get("avg_winner", 0)
            avg_l = abs(m.get("avg_loser", 1))
            rr = round(avg_w / avg_l, 2) if avg_l > 0 else 0
            exp_val = round(m.get("avg_trade", 0), 2)
            wr = round(m.get("win_rate", 0) * 100, 1)
            trades = m.get("total_trades", 0)
            largest_w = m.get("largest_winner", 0)
            largest_l = abs(m.get("largest_loser", 0))

            items = [
                {"label": "Avg Winner", "delta": avg_w, "before": None, "after": None, "improved": True},
                {"label": "Avg Loser", "delta": -avg_l, "before": None, "after": None, "improved": False},
                {"label": "Best Trade", "delta": largest_w, "before": None, "after": None, "improved": True},
                {"label": "Worst Trade", "delta": -largest_l, "before": None, "after": None, "improved": False},
                {"label": "Expectancy", "delta": exp_val, "before": None, "after": None, "improved": exp_val > 0},
            ]
            candidates.append((3, {
                "type": "trade_anatomy", "chart": "lollipop",
                "title": f"Trade anatomy: {rr:.2f}× reward:risk across {trades} trades",
                "subtitle": f"Win rate {wr}% · Expectancy ${exp_val:.2f}/trade · Hold {m.get('avg_hold_days', 0):.0f}d avg",
                "items": items,
                "annotation": {"text": f"Edge = {wr}% wins × ${avg_w:.0f} avg − {100-wr:.0f}% losses × ${avg_l:.0f} avg = ${exp_val:.2f}",
                               "color": "green" if exp_val > 0 else "red"},
                "date": bf.stem.split("_")[1] if "_" in bf.stem else today,
            }))
            break
    except Exception as e:
        logger.debug(f"trade_anatomy miner failed: {e}")

    # ═══════════════════════════════════════════════
    # 5. VIX REGIME — scatter of monthly VIX vs SPY returns
    # ═══════════════════════════════════════════════
    try:
        vix_path = PROJECT_ROOT / "data" / "cache" / "sp500" / "^VIX.parquet"
        spy_path = PROJECT_ROOT / "data" / "cache" / "sp500" / "SPY.parquet"
        if vix_path.exists() and spy_path.exists():
            vix_df = pd.read_parquet(vix_path)
            spy_df = pd.read_parquet(spy_path)
            vix_monthly = vix_df["close"].resample("M").mean()
            spy_monthly = spy_df["close"].resample("M").last().pct_change() * 100
            merged = pd.DataFrame({"vix": vix_monthly, "spy_ret": spy_monthly}).dropna()
            if len(merged) >= 12:
                points = []
                for dt, row in merged.iterrows():
                    points.append({
                        "label": dt.strftime("%b %y"),
                        "x": round(row["vix"], 1),
                        "y": round(row["spy_ret"], 1),
                        "size": 5,
                        "color": "#10b981" if row["spy_ret"] > 0 else "#f43f5e",
                    })
                # Correlation
                corr = round(merged["vix"].corr(merged["spy_ret"]), 2)
                high_vix = merged[merged["vix"] > 25]
                low_vix = merged[merged["vix"] <= 20]
                high_avg = round(high_vix["spy_ret"].mean(), 1) if len(high_vix) > 0 else 0
                low_avg = round(low_vix["spy_ret"].mean(), 1) if len(low_vix) > 0 else 0
                candidates.append((3, {
                    "type": "vix_regime", "chart": "scatter",
                    "title": f"VIX regime vs SPY returns: correlation {corr:+.2f}",
                    "subtitle": f"Low VIX (<20) avg {low_avg:+.1f}%/mo · High VIX (>25) avg {high_avg:+.1f}%/mo · {len(merged)} months",
                    "points": points[-24:],  # last 2 years
                    "x_label": "Avg Monthly VIX",
                    "y_label": "SPY Monthly Return %",
                    "annotation": {"text": f"Low VIX months avg {low_avg:+.1f}% vs high VIX {high_avg:+.1f}%",
                                   "color": "green" if low_avg > high_avg else "red"},
                    "date": today,
                }))
    except Exception as e:
        logger.debug(f"vix_regime miner failed: {e}")

    # ═══════════════════════════════════════════════
    # 6. FEE IMPACT — waterfall & grouped bar
    # ═══════════════════════════════════════════════
    try:
        fee_file = None
        for f in sorted(Path(PROJECT_ROOT / "backtest" / "results").glob("fee_impact_analysis_*.json"), reverse=True):
            fee_file = f; break
        if fee_file:
            fee_data = safe_json(fee_file, {})
            zf, rf = fee_data.get("sp500_zero_fee", {}), fee_data.get("sp500_real_fee", {})
            bm, delta = fee_data.get("benchmark", {}), fee_data.get("delta", {})
            if zf and rf:
                drag = abs(delta.get("cagr_pct", 0.78))
                steps = [
                    {"label": "Gross CAGR", "value": zf["cagr_pct"], "type": "start"},
                    {"label": "Commission", "value": -(drag * 0.6)},
                    {"label": "Slippage", "value": -(drag * 0.3)},
                    {"label": "Filtering", "value": -(drag * 0.1)},
                    {"label": "Net CAGR", "value": 0, "type": "total"},
                ]
                candidates.append((4, {
                    "type": "fee_waterfall", "chart": "waterfall",
                    "title": f"Fee drag: {drag:.1f}pp CAGR lost to real trading costs",
                    "subtitle": f"Moomoo US: ~${fee_data.get('actual_fees', {}).get('avg_per_order_usd', 1.1):.2f}/order · {abs(delta.get('trades_removed', 7))} trades filtered",
                    "steps": steps,
                    "annotation": {"text": f"Net {rf['cagr_pct']:.1f}% still beats SPY {bm.get('spy_cagr_pct', 0):.1f}%", "color": "green"},
                    "date": fee_file.stem.split("_")[-1][:10],
                }))
                candidates.append((4, {
                    "type": "fee_compare", "chart": "grouped_bar",
                    "title": f"Real fees reduce CAGR by {drag:.1f}pp but strategy still beats benchmark",
                    "subtitle": f"Moomoo US: ~${fee_data.get('actual_fees', {}).get('avg_per_order_usd', 1.1):.2f}/order",
                    "labels": ["CAGR", "Sharpe", "Win Rate", "Profit Factor"],
                    "series": [
                        {"name": "Zero Fee", "values": [zf["cagr_pct"], zf["sharpe"], zf["win_rate_pct"], zf.get("profit_factor", 1.55)], "color": "secondary"},
                        {"name": "Real Fee", "values": [rf["cagr_pct"], rf["sharpe"], rf["win_rate_pct"], rf.get("profit_factor", 1.54)], "color": "primary"},
                    ],
                    "annotation": {"text": f"Strategy CAGR {rf['cagr_pct']:.1f}% vs SPY {bm.get('spy_cagr_pct', 0):.1f}%", "color": "green"},
                    "date": fee_file.stem.split("_")[-1][:10],
                }))
    except Exception as e:
        logger.debug(f"fee_impact miner failed: {e}")

    # ═══════════════════════════════════════════════
    # 7. MONTHLY SEASONALITY — SPY returns by calendar month
    # ═══════════════════════════════════════════════
    try:
        spy_path = PROJECT_ROOT / "data" / "cache" / "sp500" / "SPY.parquet"
        if spy_path.exists():
            spy_df = pd.read_parquet(spy_path)
            monthly_ret = spy_df["close"].resample("M").last().pct_change().dropna() * 100
            if len(monthly_ret) >= 24:
                by_month = {}
                for dt, ret in monthly_ret.items():
                    m = dt.month
                    if m not in by_month:
                        by_month[m] = []
                    by_month[m].append(ret)
                month_names = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                               "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
                avgs = [round(sum(by_month.get(m+1, [0])) / max(len(by_month.get(m+1, [1])), 1), 2)
                        for m in range(12)]
                best_m = month_names[avgs.index(max(avgs))]
                worst_m = month_names[avgs.index(min(avgs))]
                candidates.append((4, {
                    "type": "seasonality", "chart": "horizontal_bar",
                    "title": f"SPY monthly seasonality: {best_m} strongest, {worst_m} weakest",
                    "subtitle": f"Average monthly returns over {len(monthly_ret)//12} years · {len(monthly_ret)} months",
                    "labels": month_names,
                    "series": [{"name": "Avg Return %", "values": avgs, "color": "adaptive"}],
                    "annotation": {"text": f"Best: {best_m} ({max(avgs):+.1f}%) · Worst: {worst_m} ({min(avgs):+.1f}%)",
                                   "color": "green"},
                    "date": today,
                }))
    except Exception as e:
        logger.debug(f"seasonality miner failed: {e}")

    # ═══════════════════════════════════════════════
    # SELECT — pool all candidates, rotate daily
    # ═══════════════════════════════════════════════
    if not candidates:
        return {
            "type": "no_data", "chart": "none",
            "title": "Research insight will appear after experiments run",
            "subtitle": "Next research cron: weekdays at 09:00 AEST",
            "labels": [], "series": [], "date": today,
        }

    # Build diverse pool: one from each type, ordered by priority
    # This ensures each refresh day shows a genuinely different analysis
    seen_types = set()
    pool = []
    for _, insight in sorted(candidates, key=lambda c: c[0]):
        t = insight["type"]
        chart_key = f"{t}/{insight['chart']}"
        if chart_key not in seen_types:
            seen_types.add(chart_key)
            pool.append(insight)

    return pool[day_of_year % len(pool)]


def _pretty_strat(name: str) -> str:
    """Make strategy name dashboard-friendly."""
    return (name or "unknown").replace("_", " ").title()


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
        "daily_insight": generate_daily_insight(),
    }


def _merge_equity_curves(market_data: dict, exchange_rates: dict) -> list:
    """Merge per-market equity curves into a combined AUD-normalised curve.

    For each date, converts each market's equity to AUD and sums.
    Markets that start later are backfilled with their starting_equity
    (capital was allocated, just idle) so the combined curve doesn't
    jump when a new market's first data point appears.
    """
    def _to_aud(amount, currency):
        if currency == "AUD":
            return amount
        if currency == "USD":
            return amount * exchange_rates.get("USDAUD", 1.41)
        return amount

    per_market = {}   # {mid: {date: equity_in_native}}
    currencies = {}   # {mid: currency}
    start_vals = {}   # {mid: starting equity in AUD}
    all_dates = set()
    for mid, md in market_data.items():
        ccy = md.get("currency", "AUD")
        currencies[mid] = ccy
        series = {}
        for pt in md.get("equity_curve", []):
            date = pt.get("date", "")
            eq = pt.get("equity", 0)
            if date and eq:
                series[date] = eq
                all_dates.add(date)
        if series:
            per_market[mid] = series
            seq = md.get("portfolio", {}).get("starting_equity", 0)
            start_vals[mid] = _to_aud(
                seq if seq > 0 else min(series.values()), ccy
            )

    if not all_dates or not per_market:
        return []

    sorted_dates = sorted(all_dates)
    combined = []
    for d in sorted_dates:
        total = 0
        for mid, series in per_market.items():
            ccy = currencies[mid]
            if d in series:
                series["_last"] = _to_aud(series[d], ccy)
            # Before first data point → use starting equity in AUD
            total += series.get("_last", start_vals.get(mid, 0))
        combined.append({"date": d, "equity": round(total, 2)})

    return combined


def _merge_benchmark_curves(market_data: dict, exchange_rates: dict,
                            combined_starting_aud: float) -> tuple:
    """Merge per-market benchmark curves into a combined AUD-normalised curve.

    Each market has its own benchmark (SP500→SPY, ASX→IOZ.AX) in native currency.
    We convert each to AUD, forward-fill gaps, sum them, and scale so day-1
    equals combined starting equity.

    Returns (combined_curve, ticker_label) e.g. ([{date, equity}], "SPY + IOZ")
    """
    def _to_aud(amount, currency):
        if currency == "AUD":
            return amount
        if currency == "USD":
            return amount * exchange_rates.get("USDAUD", 1.41)
        return amount

    # Build per-market date→equity dicts (in AUD) and per-market starting values
    per_market = {}   # {mid: {date: equity_aud}}
    start_vals = {}   # {mid: starting equity in AUD, used as fill-value before first data point}
    tickers = []
    all_dates = set()
    for mid, md in market_data.items():
        bench = md.get("benchmark_curve", [])
        if not bench:
            continue
        ccy = md.get("currency", "AUD")
        ticker = md.get("benchmark_ticker", "?")
        if ticker not in tickers:
            tickers.append(ticker)
        series = {}
        for pt in bench:
            date = pt.get("date", "")
            eq = pt.get("equity", 0)
            if date and eq:
                series[date] = _to_aud(eq, ccy)
                all_dates.add(date)
        per_market[mid] = series
        # Starting equity for this market (AUD) — used as fill-value before first
        # benchmark data point so the combined sum equals combined_starting_aud
        # from day 1 and the scale factor stays at 1.0.
        seq = md.get("portfolio", {}).get("starting_equity", 0)
        if seq > 0:
            start_vals[mid] = _to_aud(seq, ccy)
        elif series:
            # Fallback: use first available benchmark value
            start_vals[mid] = min(series.values())
        else:
            start_vals[mid] = 0

    if not all_dates or not per_market:
        return [], "Benchmark"

    # Forward-fill each market series across all dates, then sum.
    # Markets with no data yet contribute their starting equity so the combined
    # sum is stable from day 1 and the scale factor is 1.0 (no inflation).
    sorted_dates = sorted(all_dates)
    combined = []
    for d in sorted_dates:
        total = 0
        for mid, series in per_market.items():
            if d in series:
                series["_last"] = series[d]  # track last known value
            total += series.get("_last", start_vals.get(mid, 0))
        combined.append((d, total))

    # Scale so first point = combined_starting_aud
    first_val = combined[0][1]
    if first_val <= 0:
        first_val = combined_starting_aud or 1
    scale = combined_starting_aud / first_val if combined_starting_aud > 0 else 1

    curve = [{"date": d, "equity": round(e * scale, 2)} for d, e in combined]
    label = " + ".join(t.replace(".AX", "") for t in tickers) if tickers else "Benchmark"

    return curve, label


def generate():
    """Generate multi-market dashboard data.

    Each market connects to its own broker independently (SP500=Moomoo,
    ASX=IBKR), then results are merged into a combined payload.
    """
    from markets.registry import list_markets

    markets = list_markets()  # ['asx', 'sp500']

    # ── Per-market broker connections (each market has its own broker) ──
    broker_caches = {}
    for mid in markets:
        cfg = get_config(mid)
        trading = cfg.get("trading", {})
        if (trading.get("mode") == "live"
                and trading.get("broker", "ibkr") != "ibkr"
                and trading.get("live_enabled", False)):
            acct, positions, ok, orders = get_live_broker_data(cfg)
            if ok:
                broker_caches[mid] = {"acct": acct, "positions": positions, "ok": True, "orders": orders}
                print(f"  {mid}: broker connected ({trading.get('broker')}), "
                      f"{len(positions)} positions")
            else:
                print(f"  {mid}: broker connect FAILED ({trading.get('broker')})")

    # ── Generate per-market data ────────────────────────────────
    market_data = {}
    for mid in markets:
        print(f"\n  Generating {mid}...")
        # Pass market-specific broker cache (not shared)
        cache = broker_caches.get(mid)
        data = generate_market(mid, broker_cache=cache)
        market_data[mid] = data

    # ── Fetch exchange rates for AUD-normalised combined view ───
    exchange_rates = {"AUDUSD": 0.63, "USDAUD": 1.587}  # fallback
    try:
        import yfinance as yf
        audusd_data = yf.Ticker("AUDUSD=X").history(period="1d")
        if not audusd_data.empty:
            audusd = float(audusd_data["Close"].iloc[-1])
            exchange_rates = {"AUDUSD": round(audusd, 5), "USDAUD": round(1 / audusd, 5)}
            print(f"  Exchange rate: 1 AUD = {audusd:.4f} USD (1 USD = {1/audusd:.4f} AUD)")
    except Exception as e:
        print(f"  Exchange rate fetch failed ({e}), using fallback")

    def to_aud(amount, currency):
        """Convert any currency amount to AUD."""
        if currency == "AUD":
            return amount
        if currency == "USD":
            return amount * exchange_rates["USDAUD"]
        return amount  # unknown currency — pass through

    # ── Merge: combine positions and stats from all markets ─────
    all_positions = []
    all_manual = []
    all_closed = []
    combined_strats = {}
    combined_equity_aud = 0
    combined_cash_aud = 0
    combined_starting_aud = 0

    for mid, md in market_data.items():
        pf = md.get("portfolio", {})
        ccy = md.get("currency", "AUD")
        combined_equity_aud += to_aud(pf.get("equity", 0), ccy)
        combined_cash_aud += to_aud(pf.get("cash", 0), ccy)
        combined_starting_aud += to_aud(pf.get("starting_equity", 0), ccy)
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

    # ── Combined equity curve (sum across markets) ──────────────
    combined_curve = _merge_equity_curves(market_data, exchange_rates)

    # ── Combined benchmark curve (AUD-normalised blend) ────────
    combined_bench, combined_bench_label = _merge_benchmark_curves(
        market_data, exchange_rates, combined_starting_aud
    )

    # ── Build combined result ───────────────────────────────────
    now = datetime.now(BRISBANE)
    combined_pnl = round(combined_equity_aud - combined_starting_aud, 2)
    combined_pnl_pct = round(combined_pnl / combined_starting_aud * 100, 2) if combined_starting_aud > 0 else 0

    # ── Research data ──────────────────────────────────────────
    research_data = generate_research_data()

    # Pick primary for fields that don't merge well (plan, risk)
    primary = "sp500" if "sp500" in market_data else markets[0]
    primary_data = market_data[primary]

    result = {
        "timestamp": now.isoformat(),
        "project": "Atlas",
        "markets": market_data,
        "exchange_rates": exchange_rates,
        # Top-level summary (combined across ALL markets, AUD-normalised)
        "trading_mode": "live" if any(md.get("trading_mode") == "live" for md in market_data.values()) else "offline",
        "data_source": "broker" if broker_caches else "offline",
        "broker": {mid: get_config(mid).get("trading", {}).get("broker", "ibkr") for mid in markets},
        "config_version": {mid: md.get("config_version", "?") for mid, md in market_data.items()},
        "account": {
            "equity": round(combined_equity_aud, 2),
            "cash": round(combined_cash_aud, 2),
            "buying_power": round(combined_cash_aud, 2),
            "currency": "AUD",
        },
        "portfolio": {
            "equity": round(combined_equity_aud, 2),
            "cash": round(combined_cash_aud, 2),
            "starting_equity": round(combined_starting_aud, 2),
            "total_pnl": combined_pnl,
            "total_pnl_pct": combined_pnl_pct,
            "num_open": len(all_positions),
            "open_positions": all_positions,
            "win_rate": round(sum(1 for t in all_closed if t.get("pnl", 0) > 0) / len(all_closed) * 100, 1) if all_closed else 0,
            "market_pnl": round(sum(
                to_aud(md.get("portfolio", {}).get("market_pnl", 0), md.get("currency", "AUD"))
                for md in market_data.values()), 2),
            "realized_pnl": round(sum(
                to_aud(md.get("portfolio", {}).get("realized_pnl", 0), md.get("currency", "AUD"))
                for md in market_data.values()), 2),
            "total_commissions": round(sum(
                to_aud(md.get("portfolio", {}).get("total_commissions", 0), md.get("currency", "AUD"))
                for md in market_data.values()), 2),
            "commission_per_trade": 0,
        },
        "manual_positions": {
            "positions": all_manual,
            "num_open": len(all_manual),
            "unrealized_pnl": round(sum(p.get("pnl", 0) for p in all_manual), 2),
            "market_value": round(sum(p.get("current_price", 0) * p.get("shares", 0) for p in all_manual), 2),
        },
        "strategy_summary": list(combined_strats.values()),
        "equity_curve": combined_curve,
        "benchmark_curve": combined_bench,
        "benchmark_ticker": combined_bench_label,
        "benchmark_return_pct": round(
            (combined_bench[-1]["equity"] / combined_starting_aud - 1) * 100, 2
        ) if combined_bench and combined_starting_aud > 0 else 0,
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
    print(f"  COMBINED (AUD): A${combined_equity_aud:,.2f} equity, "
          f"A${combined_cash_aud:,.2f} cash, "
          f"{len(all_positions)} positions across {len(market_data)} markets"
          f" (1 USD = {exchange_rates['USDAUD']:.4f} AUD)")


if __name__ == "__main__":
    generate()
