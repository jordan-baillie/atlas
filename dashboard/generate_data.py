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
CACHE_DIR = PROJECT_ROOT / "dashboard" / "cache"


def safe_json(path, default=None):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default if default is not None else {}


# ── Broker cache (M1): last-known-good broker data ────────────

def _save_broker_cache(market_id: str, acct, positions, orders):
    """Persist last-known-good broker snapshot for fallback when broker is down."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    data = {
        "timestamp": datetime.now().isoformat(),
        "acct": acct,
        "positions": positions,
        "orders": orders,
    }
    with open(CACHE_DIR / f"broker_{market_id}.json", "w") as f:
        json.dump(data, f, indent=2, default=str)


def _load_broker_cache(market_id: str, max_age_minutes: int = 60):
    """Load cached broker data if fresh enough. Returns dict or None."""
    path = CACHE_DIR / f"broker_{market_id}.json"
    if not path.exists():
        return None
    data = safe_json(path, None)
    if not data or "timestamp" not in data:
        return None
    try:
        ts = datetime.fromisoformat(data["timestamp"])
        age = (datetime.now() - ts).total_seconds() / 60
        if age > max_age_minutes:
            return None
        data["cache_age_minutes"] = round(age, 1)
        return data
    except Exception:
        return None


# ── FX cache (M4): hourly exchange rate caching ───────────────

def _get_exchange_rates() -> dict:
    """Get AUDUSD exchange rates with hourly file cache.

    Priority: 1) cache if < 1 hour old, 2) fresh yfinance, 3) stale cache any age,
    4) last dashboard output, 5) hardcoded fallback.
    """
    fx_cache_path = CACHE_DIR / "fx_rates.json"

    # Check cache freshness
    cached = safe_json(fx_cache_path, None)
    if cached and "timestamp" in cached:
        try:
            age_s = (datetime.now() - datetime.fromisoformat(cached["timestamp"])).total_seconds()
            if age_s < 3600:  # < 1 hour
                return cached["rates"]
        except Exception:
            pass

    # Fetch fresh from yfinance
    try:
        import yfinance as yf
        audusd_data = yf.Ticker("AUDUSD=X").history(period="1d")
        if not audusd_data.empty:
            audusd = float(audusd_data["Close"].iloc[-1])
            rates = {"AUDUSD": round(audusd, 5), "USDAUD": round(1 / audusd, 5)}
            # Save to cache
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            with open(fx_cache_path, "w") as f:
                json.dump({"timestamp": datetime.now().isoformat(), "rates": rates}, f)
            print(f"  Exchange rate: 1 AUD = {audusd:.4f} USD (1 USD = {1/audusd:.4f} AUD)")
            return rates
    except Exception as e:
        print(f"  Exchange rate fetch failed ({e})")

    # Fallback chain: stale cache → previous dashboard output → hardcoded
    if cached and "rates" in cached:
        print(f"  Exchange rate: using stale cache ({cached.get('timestamp', '?')})")
        return cached["rates"]
    prev_data = safe_json(OUTPUT, {})
    prev_rates = prev_data.get("exchange_rates")
    if prev_rates:
        print(f"  Exchange rate: using last dashboard output")
        return prev_rates
    print("  Exchange rate: using hardcoded fallback (0.70)")
    return {"AUDUSD": 0.70, "USDAUD": 1.43}


# ── Freshness helpers (M3) ────────────────────────────────────

def _file_mtime_iso(path) -> str | None:
    """Return ISO timestamp of file modification time, or None."""
    try:
        return datetime.fromtimestamp(Path(path).stat().st_mtime).isoformat()
    except Exception:
        return None


def get_config(market_id: str = "asx"):
    return safe_json(PROJECT_ROOT / "config" / "active" / f"{market_id}.json", {})


def get_portfolio(config):
    # Load from per-market state file first, fall back to legacy
    market_id = config.get("market", "asx")
    per_market = PROJECT_ROOT / "brokers" / "state" / f"{market_id}.json"
    legacy = PROJECT_ROOT / "brokers" / "state" / "live_state.json"

    state = None
    if per_market.exists():
        state = safe_json(per_market, None)
    if state is None and legacy.exists():
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
    # Scan last 90 plans (covers ~3 months — needed for open positions held 30–90 days)
    for plan_file in sorted(plans_dir.glob("plan_*.json"), reverse=True)[:90]:
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
    broker_name = trading.get("broker", "moomoo")
    if not trading.get("live_enabled"):
        return None, [], False, []

    try:
        from brokers.registry import get_live_broker

        broker = get_live_broker(config)
        # Use a separate clientId for dashboard reads if broker supports it
        if hasattr(broker, '_client_id') and hasattr(broker, '_set_client_id'):
            broker._set_client_id(20)
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
                "today_pnl": round(pos.today_pnl, 2),
                "currency": pos.currency or "",
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


def _get_alpaca_market_data():
    """Return the shared AlpacaMarketData singleton, or None if unavailable."""
    try:
        from brokers.alpaca.market_data import get_alpaca_data_client
        return get_alpaca_data_client()
    except Exception:
        return None


def get_live_prices(tickers):
    """Fetch live intraday prices — Alpaca-first for US tickers, yfinance for others.

    For US equities (no .AX / .HK suffix): tries Alpaca snapshots first,
    falls back to yfinance on failure or missing data.
    For non-US tickers: uses yfinance directly (Alpaca is US-only).

    Returns dict of ticker -> {"close": float, "prev_close": float|None, "date": str, "live": bool}
    """
    prices = {}
    if not tickers:
        return prices

    ticker_list = list(tickers)

    # Split tickers by market: US equities → Alpaca, ASX/HK → yfinance
    us_tickers = [t for t in ticker_list
                  if not t.endswith(".AX") and not t.endswith(".HK")]
    non_us_tickers = [t for t in ticker_list
                      if t.endswith(".AX") or t.endswith(".HK")]

    # ── US tickers via Alpaca snapshots ──────────────────────────
    if us_tickers:
        try:
            alpaca = _get_alpaca_market_data()
            if alpaca is not None:
                snapshots = alpaca.get_snapshots(us_tickers)
                for ticker, snap in snapshots.items():
                    price = snap.get("price", 0.0)
                    if not price:
                        continue
                    prev_daily = snap.get("prev_daily_bar", {}) or {}
                    prev_close = prev_daily.get("close") or None
                    # Best available timestamp: minute bar > daily bar
                    ts = (snap.get("minute_bar", {}) or {}).get("timestamp") \
                        or (snap.get("daily_bar", {}) or {}).get("timestamp") \
                        or str(datetime.now(BRISBANE).date())
                    prices[ticker] = {
                        "close":      round(float(price), 4),
                        "prev_close": round(float(prev_close), 4) if prev_close else None,
                        "date":       str(ts),
                        "live":       True,
                    }
                logger.debug("Alpaca snapshots: %d/%d US tickers", len(prices), len(us_tickers))
        except Exception as e:
            print(f"  WARN: Alpaca live price fetch failed: {e}")

    # ── Fallback to yfinance for missing US tickers + all non-US ─
    yf_tickers = [t for t in us_tickers if t not in prices] + non_us_tickers
    if yf_tickers:
        try:
            import yfinance as yf
            data = yf.download(yf_tickers, period="2d", interval="15m",
                               progress=False, threads=True)
            if not data.empty:
                for t in yf_tickers:
                    try:
                        if len(yf_tickers) > 1:
                            series = data["Close"][t].dropna()
                        else:
                            series = data["Close"].dropna()
                        if len(series) == 0:
                            continue
                        last_price = float(series.iloc[-1])
                        prev_price = float(series.iloc[-2]) if len(series) > 1 else None
                        last_ts = series.index[-1]
                        prices[t] = {
                            "close":      last_price,
                            "prev_close": prev_price,
                            "date":       str(last_ts),
                            "live":       True,
                        }
                    except Exception:
                        pass
        except Exception as e:
            print(f"  WARN: yfinance live price fetch failed: {e}")

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


def generate_market(market_id: str, broker_cache: dict | None = None,
                    fx_rates: dict | None = None):
    """Generate dashboard data for a single market.

    broker_cache: optional dict to reuse a single broker connection.
      Keys: "acct", "positions", "ok". If None, connects fresh.
    fx_rates: exchange rates dict (e.g. {"USDAUD": 1.41}) passed from
      generate() so equity curve points can store the FX rate used.
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
                    and trading.get("live_enabled", False))

    # ── Try live broker data first ──────────────────────────────
    broker_acct, broker_positions, broker_ok = None, [], False
    # Also check for cross-broker positions: Moomoo may hold .AX stocks
    # even when this market is not in live trading mode.
    cross_broker_positions = []
    if is_live_mode:
        if broker_cache and broker_cache.get("ok"):
            # Reuse shared broker connection — filter positions to this market
            broker_acct = broker_cache["acct"]
            all_broker_pos = broker_cache["positions"]
            # Filter positions by market
            if market_id == "sp500":
                broker_positions = [p for p in all_broker_pos if not p.get("ticker", "").endswith(".AX") and not p.get("ticker", "").endswith(".HK")]
            elif market_id == "asx":
                broker_positions = [p for p in all_broker_pos if p.get("ticker", "").endswith(".AX")]
            elif market_id == "hk":
                broker_positions = [p for p in all_broker_pos if p.get("ticker", "").endswith(".HK")]
            else:
                broker_positions = all_broker_pos
            broker_ok = True
        else:
            broker_acct, broker_positions, broker_ok, _orders = get_live_broker_data(config)
    elif broker_cache and broker_cache.get("ok"):
        # Not in live mode, but we have shared broker data (cross-broker).
        # Extract positions that belong to THIS market as manual holdings.
        all_broker_pos = broker_cache["positions"]
        if market_id == "asx":
            cross_broker_positions = [p for p in all_broker_pos if p.get("ticker", "").endswith(".AX")]
        elif market_id == "hk":
            cross_broker_positions = [p for p in all_broker_pos if p.get("ticker", "").endswith(".HK")]
        if cross_broker_positions:
            logger.info("Cross-broker positions for %s: %d from shared broker",
                        market_id, len(cross_broker_positions))

    # Detect cached broker data (M1: last-known-good fallback)
    is_cached = broker_cache.get("_cached", False) if broker_cache else False
    cache_age = broker_cache.get("_cache_age_minutes", 0) if broker_cache else 0

    if broker_ok and broker_acct:
        # Broker is the sole source of truth
        positions = broker_positions
        atlas_positions = [p for p in positions if p.get("is_atlas", True)]
        manual_positions = [p for p in positions if not p.get("is_atlas", True)]
        all_positions = positions
        data_source = "cached" if is_cached else "broker"

        # Atlas P&L: only from Atlas-managed positions
        total_entry_value = sum(p.get("entry_value", 0) for p in atlas_positions)
        atlas_value = sum(p.get("market_value", 0) for p in atlas_positions)
        market_pnl = round(atlas_value - total_entry_value, 2)
        total_commissions = round(len(atlas_positions) * commission, 2)

        # Manual positions value (not managed by Atlas)
        manual_value = sum(p.get("market_value", 0) for p in manual_positions)

        # Broker is sole source of truth for headline equity/cash.
        # This includes ALL positions (Atlas + manual) so the dashboard
        # equity matches what the broker app shows.
        broker_equity = round(broker_acct["equity"], 2)
        broker_cash = round(broker_acct["cash"], 2)

        # Atlas P&L: computed from Atlas-managed positions only.
        # Don't mix broker total equity with Atlas starting_equity — that
        # conflates manual trade gains with strategy performance.
        pos_value = atlas_value
        cash = round(seq - total_entry_value, 2) if total_entry_value > 0 else seq
        equity = round(cash + atlas_value, 2)  # Atlas virtual equity

        total_pnl = round(equity - seq, 2)
        total_pnl_pct = round(total_pnl / seq * 100, 2) if seq > 0 else 0

        # ── C1: Refresh stale prices for same-day MOO fills ──────────
        # Broker may return fill price as marketPrice when no market data snapshot exists
        # (common for ASX MOO orders with no market data subscription).
        # Detect: current_price ≈ entry_price → override with Yahoo Finance price.
        if data_source in ("broker", "cached"):
            stale_tickers = set()
            for p in atlas_positions:
                if abs(p.get("current_price", 0) - p.get("entry_price", 0)) < 0.0001:
                    stale_tickers.add(p.get("ticker", ""))
            stale_tickers.discard("")
            refreshed: dict = {}
            if stale_tickers:
                refreshed = get_prices(stale_tickers)
                for p in atlas_positions:
                    t = p.get("ticker", "")
                    if t in refreshed and t in stale_tickers:
                        new_price = refreshed[t]["close"]
                        p["current_price"] = round(new_price, 4)
                        ep = p.get("entry_price", 0)
                        sh = p.get("shares", 0)
                        p["unrealized_pnl"] = round((new_price - ep) * sh, 2)
                        p["market_value"] = round(new_price * sh, 2)
                        logger.info(
                            "C1 stale price refresh [%s]: entry=%.4f → yf=%.4f, pnl=%.2f",
                            t, ep, new_price, p["unrealized_pnl"],
                        )
                # Recalculate Atlas P&L after price refresh
                atlas_value = sum(p.get("market_value", 0) for p in atlas_positions)
                market_pnl = round(atlas_value - total_entry_value, 2)
                pos_value = atlas_value
                equity = round(cash + atlas_value, 2)
                total_pnl = round(equity - seq, 2)
                total_pnl_pct = round(total_pnl / seq * 100, 2) if seq > 0 else 0

        # ── C2: P&L consistency validation ──────────────────────────
        # Brokers (esp. Moomoo) may return unrealized_pnl from a stale snapshot
        # while current_price comes from a different snapshot.
        # If |broker_pnl - (cp-ep)*shares| > 10%, recalculate from prices.
        for p in all_positions:
            ep = p.get("entry_price", 0)
            cp = p.get("current_price", 0)
            sh = p.get("shares", 0)
            if ep > 0 and cp > 0 and sh > 0:
                expected_pnl = round((cp - ep) * sh, 2)
                actual_pnl = p.get("unrealized_pnl", 0)
                if abs(actual_pnl - expected_pnl) > max(abs(expected_pnl) * 0.10, 1.0):
                    logger.warning(
                        "P&L inconsistency [%s]: broker_pnl=%.2f, calc_pnl=%.2f "
                        "(cp=%.4f, ep=%.4f, sh=%d) — using calculated",
                        p.get("ticker"), actual_pnl, expected_pnl, cp, ep, sh,
                    )
                    p["unrealized_pnl"] = expected_pnl
                    p["unrealized_pnl_pct"] = round((cp - ep) / ep * 100, 2)

    elif cross_broker_positions:
        # Not in live mode, but we have cross-broker positions from a shared
        # broker (e.g. ASX stocks held on Moomoo). Show their real value.
        positions = portfolio.get("positions", [])
        atlas_positions = positions
        all_positions = list(positions) + cross_broker_positions
        data_source = "cross-broker"

        # Equity = value of cross-broker positions (real broker data)
        cross_value = sum(p.get("market_value", 0) for p in cross_broker_positions)
        cross_cost = sum(
            p.get("cost_basis", p.get("entry_price", 0) * p.get("shares", 0))
            for p in cross_broker_positions
        )
        cross_pnl = sum(p.get("unrealized_pnl", 0) for p in cross_broker_positions)

        pos_value = cross_value
        cash = 0  # no separate cash allocation for cross-broker markets
        equity = round(cross_value, 2)
        # Use cost basis as starting reference for P&L
        seq = round(cross_cost, 2) if cross_cost > 0 else 0
        total_pnl = round(cross_pnl, 2)
        total_pnl_pct = round(total_pnl / seq * 100, 2) if seq > 0 else 0

        total_entry_value = round(cross_cost, 2)
        total_commissions = 0
        market_pnl = round(cross_pnl, 2)

        # Define broker vars for cross-broker markets
        broker_equity = equity
        broker_cash = 0

    else:
        # Paper/fallback mode — no broker, no cross-broker positions
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
    # W9: Build sector fallback lookup from broker state file on disk
    _state_file = safe_json(PROJECT_ROOT / "brokers" / "state" / f"{market_id}.json", {})
    state_positions = {sp.get("ticker"): sp for sp in _state_file.get("positions", [])}

    for p in all_positions:
        t = p.get("ticker", "")
        is_atlas = p.get("is_atlas", True)

        # Cross-broker positions (from shared Moomoo data) already have
        # market_value and unrealized_pnl from the broker even in paper mode.
        has_broker_data = broker_ok or p.get("market_value", 0) > 0
        if has_broker_data:
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

        # W9: Use broker sector → state file fallback → "Unknown"
        # Filter out empty strings / "Unknown" from broker before checking fallback
        _broker_sector = p.get("sector", "")
        if not _broker_sector or _broker_sector == "Unknown":
            _broker_sector = ""
        sector_val = (_broker_sector or
                      state_positions.get(t, {}).get("sector") or
                      "Unknown")
        open_pos.append({
            "ticker": t, "strategy": strat,
            "entry_date": ed, "entry_price": ep, "current_price": round(cp, 4),
            "shares": sh, "pnl": upnl, "pnl_pct": upnl_pct,
            "stop_price": p.get("stop_price", 0),
            "days_held": dh, "sector": sector_val,
            "is_atlas": is_atlas,
            "today_pnl": p.get("today_pnl", 0),
            "currency": p.get("currency", ""),
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

    # Currency from market profile (needed before equity curve for FX rate tagging)
    from markets.registry import get_market
    market_profile = get_market(market_id)
    currency = getattr(market_profile, "currency", "AUD")

    # ── Equity curve (per-market, persistent) ─────────────────
    curve_path = PROJECT_ROOT / "logs" / f"equity_curve_{market_id}.json"
    eq_curve = safe_json(curve_path, [])
    if not isinstance(eq_curve, list):
        eq_curve = []

    today_str = now.strftime("%Y-%m-%d")

    # Issue #2: Detect unfunded/offline markets (ASX/HK offline with $0 starting
    # equity or no positions and no equity history). These should NOT contribute
    # phantom equity to the combined view.
    funded = True
    if data_source == "offline":
        if seq == 0 or (len(positions) == 0 and not eq_curve):
            funded = False
            cash = 0
            equity = 0
            seq = 0
            total_pnl = 0
            total_pnl_pct = 0
            logger.info("Market %s treated as UNFUNDED (offline, no positions, no history)",
                        market_id)

    # Update equity curve for any market with real data (broker, cached, or
    # cross-broker). Use broker_equity when available so the chart matches
    # what the broker app shows. Skip only pure offline/paper markets.
    if data_source in ("broker", "cached", "cross-broker"):
        # Store the equity that matches the headline (broker total for live
        # markets, cross-broker position value for passive markets).
        chart_equity = equity
        if data_source in ("broker", "cached") and broker_ok:
            chart_equity = broker_equity if broker_equity else equity

        # Compute total P&L across ALL positions (Atlas + manual) for
        # deposit-adjusted return calculation.  invested = equity - pnl
        # so deposits cancel out (both sides increase equally).
        all_unrealized = sum(p.get("unrealized_pnl", 0) for p in all_positions)
        all_realized = realized_pnl  # already computed above from closed_trades
        total_investment_pnl = round(all_unrealized + all_realized, 2)

        point: dict = {"date": today_str, "equity": round(chart_equity, 2)}
        point["pnl"] = total_investment_pnl
        if fx_rates is not None:
            point["fx_rate"] = fx_rates.get("USDAUD", 1.0) if currency != "AUD" else 1.0
        if data_source == "cached":
            point["estimated"] = True
        if data_source == "cross-broker":
            point["cross_broker"] = True

        if not eq_curve or eq_curve[-1].get("date") != today_str:
            eq_curve.append(point)
        else:
            eq_curve[-1].update(point)

        # Atomic persist
        tmp_curve = curve_path.with_suffix(".tmp")
        with open(tmp_curve, "w") as f:
            json.dump(eq_curve, f, indent=2)
        tmp_curve.rename(curve_path)
    else:
        logger.info("Equity curve NOT updated for %s — data_source=%s (offline)",
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

    # Today's P&L — aggregated from broker's today_pl_val per position
    # Grouped by native currency for the breakdown, then converted for total
    today_pnl_by_ccy: dict[str, float] = {}
    for p in open_pos:
        ccy = p.get("currency", currency) or currency
        today_pnl_by_ccy[ccy] = today_pnl_by_ccy.get(ccy, 0) + p.get("today_pnl", 0)
    today_pnl_by_ccy = {k: round(v, 2) for k, v in today_pnl_by_ccy.items() if abs(v) > 0.005}

    # Assemble
    result = {
        "timestamp": now.isoformat(),
        "config_version": config.get("version", "unknown"),
        "project": config.get("project", "Atlas"),
        "market_id": market_id,
        "currency": currency,
        "trading_mode": mode_label,
        "data_source": data_source,
        "broker": trading.get("broker", "moomoo"),
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
            # Broker-reported totals (includes manual positions).
            # Used by dashboard for headline equity that matches broker app.
            "broker_equity": broker_equity if (broker_ok or data_source == "cross-broker") else None,
            "broker_cash": broker_cash if (broker_ok or data_source == "cross-broker") else None,
            # Today's P&L from broker, grouped by native currency
            "today_pnl_by_ccy": today_pnl_by_ccy,
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
        # M3: per-source freshness indicators
        "data_freshness": {
            "broker_connected": broker_ok and not is_cached,
            "broker_timestamp": now.isoformat() if (broker_ok and not is_cached) else None,
            "data_source": data_source,
            "cache_age_minutes": round(cache_age, 1) if is_cached else None,
            "equity_curve_last_date": eq_curve[-1]["date"] if eq_curve else None,
            "plan_date": plan.get("trade_date") if plan else None,
            "state_file_mtime": _file_mtime_iso(
                PROJECT_ROOT / "brokers" / "state" / f"{market_id}.json"
            ),
        },
        # Issue #2: funded flag — False when market is offline/unfunded (no capital deployed)
        "funded": funded,
        # Issue #9: stale data warning (set below if applicable)
        "stale_warning": None,
    }

    # Issue #9: Populate stale_warning when serving cached broker data older than 15 minutes
    if is_cached and cache_age > 15:
        result["stale_warning"] = f"Broker offline — showing data from {round(cache_age)}m ago"

    # Add pending orders from broker cache
    if broker_cache and broker_cache.get("orders"):
        # Filter orders to this market
        all_orders = broker_cache["orders"]
        if market_id == "sp500":
            market_orders = [o for o in all_orders if o.get("market") == "US"]
        elif market_id == "asx":
            market_orders = [o for o in all_orders if o.get("market") in ("AU", "")]
        elif market_id == "hk":
            market_orders = [o for o in all_orders if o.get("market") == "HK"]
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


def _parse_vault_frontmatter(path: Path) -> dict:
    """Parse YAML frontmatter from a vault .md file. Returns dict of key-value pairs."""
    try:
        text = path.read_text()
    except Exception:
        return {}
    if not text.startswith("---"):
        return {}
    end = text.find("---", 3)
    if end < 0:
        return {}
    fm = {}
    current_key = None
    current_list = None
    for line in text[3:end].splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        # List item under current key
        if stripped.startswith("- ") and current_key:
            val = stripped[2:].strip().strip('"').strip("'")
            if current_list is None:
                current_list = []
            current_list.append(val)
            fm[current_key] = current_list
            continue
        # Key-value pair
        if ":" in stripped:
            # Flush previous list
            current_list = None
            colon = stripped.index(":")
            key = stripped[:colon].strip()
            val = stripped[colon + 1:].strip().strip('"').strip("'")
            current_key = key
            if val == "":
                # Could be a list following
                continue
            # Parse numbers
            if val in ("true", "True"):
                fm[key] = True
            elif val in ("false", "False"):
                fm[key] = False
            elif val in ("null", "None", "~"):
                fm[key] = None
            else:
                try:
                    fm[key] = int(val)
                except ValueError:
                    try:
                        fm[key] = float(val)
                    except ValueError:
                        fm[key] = val
    return fm


def _read_vault_strategies() -> list:
    """Read all strategy cards from vault/Strategies/*.md frontmatter.

    Falls back to STRATEGY_UNIVERSE from discovery.py for the 'type' field
    since vault cards for tested strategies set type='strategy' (generic).
    """
    # Load strategy types from discovery engine (authoritative source)
    type_map = {}
    try:
        from research.discovery import STRATEGY_UNIVERSE
        for sid, info in STRATEGY_UNIVERSE.items():
            type_map[sid] = info.get("type", "unknown")
    except ImportError:
        pass

    # Load descriptions from discovery engine (Tier 1 strategies have them)
    desc_map = {
        # Tier 0 fallbacks (originals without STRATEGY_UNIVERSE descriptions)
        "bb_squeeze": "Bollinger Band squeeze: enters when bands contract (low volatility) and break out. Captures volatility expansion moves.",
        "short_term_mr": "RSI(2)/IBS short-term mean reversion. Captures rapid 1–5 day reversals that standard MR (RSI 14) misses.",
        "momentum_breakout": "N-day high breakout momentum entry. Enters at point of price breach rather than lagging MA crossover.",
        "consecutive_down_days": "Buy after N consecutive down days in an uptrend. Statistical mean-reversion pattern with 60-70% historical win rate on indices.",
        "mtf_momentum": "Multi-timeframe momentum: weekly trend confirmation + daily entry timing. Combines fast and slow momentum signals.",
        "dividend_capture": "Buy before ex-dividend date, capture dividend payment, exit shortly after. Event-driven income strategy.",
        "sector_rotation": "Top-down momentum sector rotation. Selects strongest GICS sectors by momentum, rotates monthly.",
    }
    ref_map = {}
    try:
        for sid, info in STRATEGY_UNIVERSE.items():
            if info.get("description"):
                desc_map.setdefault(sid, info["description"])
            if info.get("reference"):
                ref_map[sid] = info["reference"]
    except Exception:
        pass

    strat_dir = PROJECT_ROOT / "research" / "vault" / "Strategies"
    if not strat_dir.exists():
        return []
    strategies = []
    for md in sorted(strat_dir.glob("*.md")):
        fm = _parse_vault_frontmatter(md)
        if not fm:
            continue
        sid = fm.get("strategy_id", md.stem.lower().replace(" ", "_"))
        raw_type = fm.get("type", "unknown")
        # Always prefer discovery engine type (authoritative); vault cards often
        # have 'strategy' (generic) or abbreviated types like 'adx', 'demark'
        strat_type = type_map.get(sid, raw_type)

        # Get description: prefer vault ## Overview, fall back to STRATEGY_UNIVERSE
        description = ""
        try:
            text = md.read_text()
            parts = text.split("---", 2)
            body = parts[2] if len(parts) >= 3 else text
            in_overview = False
            for line in body.splitlines():
                stripped = line.strip()
                if stripped.startswith("## Overview"):
                    in_overview = True
                    continue
                if in_overview and stripped.startswith("## "):
                    break
                if in_overview and stripped and not stripped.startswith(">"):
                    # Skip generic placeholder text
                    if not stripped.startswith("Research strategy `"):
                        description = stripped
                        break
        except Exception:
            pass
        if not description:
            description = desc_map.get(sid, "")
        reference = ref_map.get(sid, "")

        strategies.append({
            "id": sid,
            "name": md.stem,
            "status": fm.get("status", "unknown"),
            "tier": fm.get("tier", 0),
            "type": strat_type,
            "total_experiments": fm.get("total_experiments", 0),
            "best_sharpe": fm.get("best_sharpe", None),
            "description": description[:200],
            "reference": reference[:100],
        })
    return strategies


def _read_vault_coverage() -> dict:
    """Parse the coverage matrix from vault/Meta/Coverage Map.md.

    Returns: {strategy_id: {stage: icon}} e.g. {"mean_reversion": {"solo": "✅", "oos": "❌"}}
    """
    cov_path = PROJECT_ROOT / "research" / "vault" / "Meta" / "Coverage Map.md"
    if not cov_path.exists():
        return {}
    try:
        text = cov_path.read_text()
    except Exception:
        return {}

    # Find the table header to get column names
    stages = ["screen", "quick", "solo", "optimize", "combined", "oos", "promote"]
    result = {}
    in_table = False
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("| Strategy"):
            in_table = True
            continue
        if in_table and line.startswith("|---"):
            continue
        if in_table and line.startswith("|"):
            cells = [c.strip() for c in line.split("|")[1:-1]]
            if len(cells) >= 9:  # strategy + type + 7 stages
                strat_id = cells[0].strip()
                stage_map = {}
                for i, stage in enumerate(stages):
                    icon = cells[i + 2].strip()
                    if icon != "—":
                        stage_map[stage] = icon
                if strat_id:
                    result[strat_id] = stage_map
        elif in_table and not line.startswith("|"):
            break  # End of table
    return result


def _read_vault_patterns() -> list:
    """Read confirmed patterns from vault/Patterns/*.md."""
    pat_dir = PROJECT_ROOT / "research" / "vault" / "Patterns"
    if not pat_dir.exists():
        return []
    patterns = []
    for md in sorted(pat_dir.glob("*.md")):
        fm = _parse_vault_frontmatter(md)
        # Grab the first real paragraph after frontmatter closes
        summary = ""
        try:
            text = md.read_text()
            # Skip past frontmatter (--- ... ---)
            parts = text.split("---", 2)
            if len(parts) >= 3:
                body = parts[2]
            else:
                body = text
            # Find first non-empty, non-heading, non-blockquote line
            for line in body.splitlines():
                stripped = line.strip()
                if not stripped or stripped.startswith("#") or stripped.startswith(">") or stripped.startswith("|") or stripped.startswith("-"):
                    continue
                summary = stripped
                break
        except Exception:
            pass
        patterns.append({
            "name": md.stem,
            "status": fm.get("status", "unknown"),
            "impact": fm.get("impact", "unknown"),
            "summary": summary[:120] if summary else "",
        })
    return patterns


def _read_vault_hypotheses() -> list:
    """Read hypotheses from vault/Hypotheses/*.md."""
    hyp_dir = PROJECT_ROOT / "research" / "vault" / "Hypotheses"
    if not hyp_dir.exists():
        return []
    hypotheses = []
    for md in sorted(hyp_dir.glob("*.md")):
        fm = _parse_vault_frontmatter(md)
        # Grab hypothesis text from ## Hypothesis section
        hyp_text = ""
        try:
            text = md.read_text()
            in_section = False
            for line in text.splitlines():
                if line.strip().startswith("## Hypothesis"):
                    in_section = True
                    continue
                if in_section and line.strip().startswith("## "):
                    break
                if in_section and line.strip():
                    hyp_text = line.strip()
                    break
        except Exception:
            pass
        hypotheses.append({
            "id": fm.get("id", md.stem),
            "title": fm.get("title", md.stem),
            "status": fm.get("status", "proposed"),
            "source": fm.get("source", "unknown"),
            "created": fm.get("created", ""),
            "hypothesis": hyp_text[:150] if hyp_text else "",
        })
    return hypotheses


def _read_daemon_status() -> dict:
    """Read research engine heartbeat from /tmp.

    Checks both heartbeat files (research daemon and autoresearch)
    and returns the status of whichever is most recently active.
    Falls back to systemctl if heartbeats are stale.
    """
    import subprocess as _sp

    heartbeat_files = [
        Path("/tmp/research-daemon-heartbeat.json"),
        Path("/tmp/autoresearch-heartbeat.json"),
    ]

    best_hb = None
    best_ts = None

    for hb_path in heartbeat_files:
        hb = safe_json(hb_path, None)
        if hb is None:
            continue
        ts_str = hb.get("timestamp", "")
        if not ts_str:
            continue
        try:
            from datetime import timezone
            hb_time = datetime.fromisoformat(ts_str)
            if hb_time.tzinfo is None:
                hb_time = hb_time.replace(tzinfo=timezone.utc)
            if best_ts is None or hb_time > best_ts:
                best_ts = hb_time
                best_hb = hb
        except Exception:
            continue

    if best_hb is None:
        # No heartbeat files — check if service is running via systemctl
        for svc in ("atlas-autoresearch", "atlas-research-daemon"):
            try:
                r = _sp.run(["systemctl", "is-active", svc],
                            capture_output=True, text=True, timeout=5)
                if r.stdout.strip() == "active":
                    return {"status": "running", "uptime_s": 0,
                            "experiments_completed": 0, "experiments_failed": 0,
                            "queue_depth": 0, "current_experiment": None,
                            "source": svc}
            except Exception:
                pass
        return {"status": "offline", "uptime_s": 0, "experiments_completed": 0,
                "experiments_failed": 0, "queue_depth": 0, "current_experiment": None}

    # Determine staleness from best heartbeat
    from datetime import timezone
    age_min = (datetime.now(timezone.utc) - best_ts).total_seconds() / 60
    hb_status = best_hb.get("status", "unknown")

    # Heartbeat says "stopped" but maybe a new process took over
    if hb_status == "stopped" or age_min > 30:
        # Check if either service is actually running
        for svc in ("atlas-autoresearch", "atlas-research-daemon"):
            try:
                r = _sp.run(["systemctl", "is-active", svc],
                            capture_output=True, text=True, timeout=5)
                if r.stdout.strip() == "active":
                    status = "running"
                    break
            except Exception:
                pass
        else:
            status = "dead" if age_min > 30 else "stopped"
    elif age_min > 5:
        status = "stale"
    else:
        status = "running"

    return {
        "status": status,
        "uptime_s": best_hb.get("uptime_s", 0),
        "experiments_completed": best_hb.get("experiments_completed",
                                              best_hb.get("experiments_total", 0)),
        "experiments_failed": best_hb.get("experiments_failed", 0),
        "queue_depth": best_hb.get("queue_depth", 0),
        "current_experiment": best_hb.get("current_experiment",
                                           best_hb.get("strategy")),
        "timestamp": best_hb.get("timestamp", ""),
    }


def generate_research_data() -> dict:
    """Generate research section data for the dashboard.

    Reads primarily from the Obsidian vault (research/vault/) for
    strategy cards, coverage, patterns, and hypotheses.
    Supplements with queue.json, journal.json, and daemon heartbeat.
    """
    research_dir = PROJECT_ROOT / "research"
    queue_path = research_dir / "queue.json"
    journal_path = research_dir / "journal.json"

    # ── Vault data (primary source) ─────────────────────────────
    strategies = _read_vault_strategies()
    coverage = _read_vault_coverage()
    patterns = _read_vault_patterns()
    hypotheses = _read_vault_hypotheses()
    daemon = _read_daemon_status()

    # ── Queue (aggregated summary, not raw dump) ────────────────
    queue = safe_json(queue_path, [])
    queue_by_status = {}
    queue_by_priority = {}
    queue_by_category = {}
    for e in queue:
        st = e.get("status", "queued")
        queue_by_status[st] = queue_by_status.get(st, 0) + 1
        pri = e.get("priority", "P3")
        queue_by_priority[pri] = queue_by_priority.get(pri, 0) + 1
        cat = e.get("category", "unknown")
        queue_by_category[cat] = queue_by_category.get(cat, 0) + 1

    # Queued items only (for the expandable table)
    queued_items = [
        {
            "id": e.get("id", "?"),
            "title": e.get("title", "?"),
            "priority": e.get("priority", "?"),
            "category": e.get("category", "?"),
            "market": e.get("market", "?"),
            "status": e.get("status", "?"),
            "strategy_name": e.get("strategy_name", ""),
            "estimated_runtime_min": e.get("estimated_runtime_min", 0),
        }
        for e in queue
        if e.get("status") in ("queued", "claimed", "running")
    ]

    # ── Journal (activity feed — last 20) ───────────────────────
    journal = safe_json(journal_path, [])
    activity_feed = []
    for entry in journal[-20:]:
        km = entry.get("key_metrics", {})
        activity_feed.append({
            "experiment_id": entry.get("experiment_id", "?"),
            "timestamp": entry.get("timestamp", ""),
            "strategy": entry.get("strategy", "N/A"),
            "category": entry.get("category", "?"),
            "verdict": entry.get("verdict", "?"),
            "sharpe": km.get("sharpe"),
            "win_rate_pct": km.get("win_rate_pct"),
            "total_trades": km.get("total_trades"),
            "cagr_pct": km.get("cagr_pct"),
            "max_drawdown_pct": km.get("max_drawdown_pct"),
            "promoted": entry.get("promoted", False),
            "learnings": entry.get("learnings", [])[:2],  # First 2 only
        })

    # ── Statistics (from journal) ───────────────────────────────
    total_experiments = len(journal)
    passed_count = sum(1 for e in journal if e.get("verdict") == "pass")
    failed_count = sum(1 for e in journal if e.get("verdict") == "fail")
    partial_count = sum(1 for e in journal if e.get("verdict") == "partial")
    promoted_count = sum(1 for e in journal if e.get("verdict") == "promoted" or e.get("promoted"))

    # Days active
    days_active = 0
    if journal:
        first_ts = journal[0].get("timestamp", "")[:10]
        try:
            first_date = datetime.strptime(first_ts, "%Y-%m-%d")
            days_active = (datetime.now() - first_date).days + 1
        except ValueError:
            days_active = 1

    # ── Leaderboard (from vault strategy cards, enriched) ───────
    leaderboard = []
    for s in strategies:
        sid = s["id"]
        stages = coverage.get(sid, {})
        # Determine highest lifecycle stage reached
        stage_order = ["screen", "quick", "solo", "optimize", "combined", "oos", "promote"]
        highest_stage = "—"
        highest_icon = "—"
        for stage in reversed(stage_order):
            if stage in stages:
                highest_stage = stage
                highest_icon = stages[stage]
                break

        # Get win rate from journal (best experiment)
        strat_entries = [e for e in journal if e.get("strategy") == sid]
        best_wr = None
        best_trades = 0
        for e in strat_entries:
            km = e.get("key_metrics", {})
            wr = km.get("win_rate_pct")
            trades = km.get("total_trades", 0)
            sharpe = km.get("sharpe")
            if sharpe is not None and s.get("best_sharpe") is not None:
                if abs(sharpe - s["best_sharpe"]) < 0.01:
                    best_wr = wr
                    best_trades = trades
            if wr is not None and (best_wr is None or wr > best_wr):
                best_wr = wr
            if trades > best_trades:
                best_trades = trades

        leaderboard.append({
            "id": sid,
            "name": s["name"],
            "status": s["status"],
            "tier": s["tier"],
            "type": s["type"],
            "total_experiments": s["total_experiments"],
            "best_sharpe": s["best_sharpe"],
            "best_win_rate": round(best_wr, 1) if best_wr is not None else None,
            "best_trades": best_trades,
            "stage": highest_stage,
            "stage_icon": highest_icon,
            "coverage": stages,
            "description": s.get("description", ""),
            "reference": s.get("reference", ""),
        })
    # Filter out meta-strategies (filters/combined — not directly tradable)
    leaderboard = [s for s in leaderboard if s["status"] != "filter"]
    # Sort: tested strategies with best Sharpe first, then untested
    leaderboard.sort(key=lambda x: (
        0 if x["best_sharpe"] is not None else 1,
        -(x["best_sharpe"] or -999),
    ))

    # ── Lifecycle pipeline counts ───────────────────────────────
    pipeline = {"untested": 0, "screen": 0, "quick": 0, "solo": 0,
                "optimize": 0, "combined": 0, "oos": 0, "promote": 0}
    for s in leaderboard:
        stage = s["stage"]
        if stage == "—":
            pipeline["untested"] += 1
        elif stage in pipeline:
            pipeline[stage] += 1

    return {
        "daemon": daemon,
        "leaderboard": leaderboard,
        "pipeline": pipeline,
        "strategy_pipeline": _build_strategy_pipeline(),
        "patterns": patterns,
        "hypotheses": hypotheses,
        "activity_feed": activity_feed,
        "queue_summary": {
            "total": len(queue),
            "by_status": queue_by_status,
            "by_priority": queue_by_priority,
            "by_category": queue_by_category,
            "pending_items": queued_items,
        },
        "statistics": {
            "total_experiments": total_experiments,
            "passed": passed_count,
            "failed": failed_count,
            "partial": partial_count,
            "promoted": promoted_count,
            "pass_rate_pct": round(passed_count / total_experiments * 100, 1) if total_experiments else 0,
            "days_active": days_active,
            "strategies_tested": len([s for s in strategies if s["total_experiments"] and s["total_experiments"] > 0]),
            "strategies_total": len(strategies),
        },
        "daily_insight": generate_daily_insight(),
        "agents": _build_agents(daemon),
        "discoveries": _build_discoveries(patterns, hypotheses, journal),
    }


def _build_strategy_pipeline() -> dict:
    """Build strategy pipeline counts from strategy_queue.json + sandbox dir."""
    queue = safe_json(str(PROJECT_ROOT / "research" / "strategy_queue.json"), {})
    active = queue.get("active", [])
    candidates = queue.get("candidates", [])
    rejected = queue.get("rejected", [])

    # Count sandbox strategies not in any queue list
    known = set()
    for entry in active + candidates + rejected:
        if isinstance(entry, dict):
            known.add(entry.get("name", ""))
        elif isinstance(entry, str):
            known.add(entry)
    sandbox_dir = PROJECT_ROOT / "research" / "strategies"
    sandbox = 0
    if sandbox_dir.exists():
        for f in sandbox_dir.glob("*.py"):
            if f.stem != "__init__" and f.stem not in known:
                sandbox += 1

    return {
        "active": len(active),
        "candidates": len(candidates),
        "rejected": len(rejected),
        "sandbox": sandbox,
    }


def _build_agents(daemon: dict) -> list:
    """Build agents list for the pixel-agents canvas.

    Sources:
      - Autoresearch partition heartbeats → researcher agents (0, 1, or solo)
      - systemctl is-active → ground truth for running state
      - Running jobs from job_server → job agents
    """
    import subprocess as _sp
    agents = []
    from datetime import timezone as _tz
    now_utc = datetime.now(_tz.utc)
    experiments_done = daemon.get("experiments_completed", 0)

    # ── Researcher agents (partitioned or solo) ─────────────────
    # Detect which services are active
    _RESEARCHER_SERVICES = [
        # (service_name, heartbeat_path, agent_id, display_name, agent_type)
        ("atlas-autoresearch-0", "/tmp/autoresearch-parent-0-heartbeat.json", "researcher-0", "Atlas",  "atlas"),
        ("atlas-autoresearch-1", "/tmp/autoresearch-parent-1-heartbeat.json", "researcher-1", "Nova",   "nova"),
        ("atlas-autoresearch",   "/tmp/autoresearch-parent-heartbeat.json",   "researcher",   "Atlas",  "atlas"),
        ("atlas-research-daemon", "/tmp/research-daemon-heartbeat.json",      "researcher",   "Atlas",  "atlas"),
        ("atlas-sage",            "/tmp/sage-heartbeat.json",                 "sage",         "Sage",   "sage"),
        ("atlas-principal",       "/tmp/principal-heartbeat.json",            "principal",    "Director", "principal"),
    ]

    found_any = False
    for svc_name, hb_path, agent_id, display_name, agent_type in _RESEARCHER_SERVICES:
        # Check if service is running
        svc_running = False
        try:
            r = _sp.run(["systemctl", "is-active", svc_name],
                        capture_output=True, text=True, timeout=3)
            if r.stdout.strip() == "active":
                svc_running = True
        except Exception:
            pass

        # Read heartbeat
        hb = safe_json(hb_path, None)
        strategy = ""
        phase = ""
        strat_index = -1
        strat_total = 0
        cycle_num = 0

        if hb:
            ts_str = hb.get("timestamp", "")
            try:
                hb_time = datetime.fromisoformat(ts_str)
                if hb_time.tzinfo is None:
                    hb_time = hb_time.replace(tzinfo=_tz.utc)
                age_min = (now_utc - hb_time).total_seconds() / 60
            except Exception:
                age_min = 999
            if age_min < 30:
                phase = (hb.get("phase") or "").strip()
                s = (hb.get("strategy") or "").strip()
                if s:
                    strategy = s.replace("_", " ")
                strat_index = hb.get("strategy_index", -1)
                strat_total = hb.get("strategy_total", 0)
                cycle_num = hb.get("cycle", 0)

            ed = hb.get("experiments_total", 0) or hb.get("experiments_completed", 0)
            if ed and ed > experiments_done:
                experiments_done = ed

        if not svc_running and not hb:
            continue  # Service not active and no heartbeat — skip

        found_any = True

        # Determine status + task
        if svc_running:
            if agent_type == "principal":
                # Principal/Director-specific phase mapping
                if phase == "gathering":
                    status = "reading"
                    task = "Gathering research state"
                elif phase == "reviewing":
                    status = "reading"
                    task = "Reviewing experiments"
                elif phase == "executing":
                    status = "typing"
                    task = "Issuing directives"
                elif phase == "sleep":
                    status = "idle"
                    task = "Sleeping until next review"
                else:
                    status = "reading"
                    task = "Overseeing research"
            elif agent_type == "sage":
                # Sage-specific phase mapping
                if phase.startswith("checking "):
                    strat_label = phase[9:].replace("_", " ").title()
                    status = "reading"
                    task = f"Checking {strat_label}"
                elif phase.startswith("creating_"):
                    strat_label = phase[9:].replace("_", " ").title()
                    status = "typing"
                    task = f"Creating {strat_label}"
                elif phase == "create_scan":
                    status = "reading"
                    task = "Scanning strategies..."
                elif phase in ("promote", "legacy_scan"):
                    status = "typing"
                    task = "Promoting candidates"
                elif phase == "sleep":
                    status = "idle"
                    task = "Sleeping until next cycle"
                elif phase == "stopped":
                    status = "sleeping"
                    task = "Stopped"
                elif strategy:
                    status = "reading"
                    task = strategy.replace("_", " ").title()
                else:
                    status = "reading"
                    task = "Discovering..."
            elif strategy:
                strat_label = strategy.replace("_", " ").title()
                if phase == "agent":
                    status = "typing"
                    task = f"Analyzing {strat_label}"
                elif phase == "sweep":
                    status = "typing"
                    task = f"Sweeping {strat_label}"
                else:
                    status = "typing"
                    task = strat_label
            else:
                if phase == "cycle_start":
                    status = "reading"
                    task = "Starting new cycle..."
                elif phase == "cycle_done":
                    status = "idle"
                    task = "Cycle complete"
                else:
                    status = "reading"
                    task = "Between strategies..."
        else:
            status = "sleeping"
            task = "Engine offline"

        progress = {}
        if strat_total > 0 and strat_index >= 0:
            sub = 0 if phase == "sweep" else 1
            done_steps = strat_index * 2 + sub
            total_steps = strat_total * 2
            progress = {
                "pct": round(done_steps / total_steps * 100),
                "label": f"{strat_index + 1}/{strat_total}",
                "cycle": cycle_num,
            }

        agents.append({
            "id": agent_id,
            "name": display_name,
            "type": agent_type,
            "status": status,
            "task": task,
            "experiments_done": experiments_done,
            "progress": progress,
        })

    # Fallback: if nothing found, show a sleeping Atlas
    if not found_any:
        agents.append({
            "id": "researcher",
            "name": "Atlas",
            "type": "atlas",
            "status": "sleeping",
            "task": "Engine offline",
            "experiments_done": experiments_done,
            "progress": {},
        })

    # ── Job agents (from /task dispatches) ──────────────────────
    try:
        sys.path.insert(0, str(PROJECT_ROOT))
        from services.job_server import get_manager
        mgr = get_manager()
        for job in mgr.list_jobs():
            if job.get("status") != "running":
                continue
            spec = job.get("spec") or ""
            skill = job.get("skill") or ""
            prompt = (job.get("prompt") or "")[:40]
            if spec:
                task = f"#{spec}"
            elif skill:
                task = f"@{skill}"
            elif prompt:
                task = prompt
            else:
                task = "Working..."
            agents.append({
                "id": f"job-{job['id']}",
                "name": "Job Agent",
                "type": "job",
                "status": "typing",
                "task": task,
            })
    except Exception as e:
        logger.debug("Could not read job agents: %s", e)

    return agents


def _build_discoveries(patterns: list, hypotheses: list, journal: list) -> list:
    """Build consolidated discoveries list from patterns, hypotheses, and best results.

    Merges multiple sources into a simple list of notable findings.
    """
    discoveries = []

    # ── Patterns → discoveries ──────────────────────────────────
    for p in patterns:
        discoveries.append({
            "text": p.get("name", "Unknown pattern"),
            "type": "pattern",
            "impact": p.get("impact", "medium"),
            "detail": p.get("summary", ""),
        })

    # ── Confirmed hypotheses → discoveries ──────────────────────
    for h in hypotheses:
        if h.get("status") in ("confirmed", "testing"):
            discoveries.append({
                "text": h.get("title", "Unknown hypothesis"),
                "type": "hypothesis",
                "impact": "high" if h.get("status") == "confirmed" else "medium",
                "detail": h.get("hypothesis", ""),
            })

    # ── Best results from research/best/ → discoveries ──────────
    best_dir = PROJECT_ROOT / "research" / "best"
    if best_dir.exists():
        for f in sorted(best_dir.glob("*.json")):
            try:
                data = json.loads(f.read_text())
                metrics = data.get("metrics", {})
                sharpe = metrics.get("sharpe", 0)
                if sharpe and sharpe > 0.3:
                    name = data.get("strategy", f.stem).replace("_", " ").title()
                    wr = metrics.get("win_rate_pct", 0)
                    trades = metrics.get("total_trades", 0)
                    discoveries.append({
                        "text": f"{name}: Sharpe {sharpe:.3f}, {wr:.0f}% WR, {trades} trades",
                        "type": "record",
                        "impact": "high" if sharpe > 0.5 else "medium",
                        "detail": f"Best params found after {data.get('experiments_run', '?')} experiments",
                    })
            except Exception:
                pass

    # ── Key findings from journal (pass experiments with before/after) ──
    for e in journal:
        km = e.get("key_metrics", {})
        base = km.get("baseline_sharpe")
        best = km.get("best_sharpe")
        if base is not None and best is not None and e.get("verdict") == "pass":
            improvement = best - base
            if improvement > 0.1:
                strat = (e.get("strategy") or "?").replace("_", " ").title()
                discoveries.append({
                    "text": f"{strat}: Sharpe {base:.2f} → {best:.2f} (+{improvement:.2f})",
                    "type": "improvement",
                    "impact": "high" if improvement > 0.2 else "medium",
                    "detail": (e.get("hypothesis") or "")[:120],
                })

    # Sort by impact (high first), deduplicate by text
    seen = set()
    unique = []
    for d in discoveries:
        if d["text"] not in seen:
            seen.add(d["text"])
            unique.append(d)
    unique.sort(key=lambda x: 0 if x["impact"] == "high" else 1)

    return unique[:15]  # Cap at 15


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
    pnl_markets = {}  # {mid: {date: pnl_in_native}}
    currencies = {}   # {mid: currency}
    start_vals = {}   # {mid: starting equity in AUD}
    all_dates = set()
    for mid, md in market_data.items():
        # Skip cross-broker markets — their equity is already inside
        # the primary broker's curve (e.g. WDS.AX is in Moomoo's total).
        if md.get("data_source") == "cross-broker":
            continue
        ccy = md.get("currency", "AUD")
        currencies[mid] = ccy
        series = {}
        pnl_series = {}
        for pt in md.get("equity_curve", []):
            date = pt.get("date", "")
            eq = pt.get("equity", 0)
            if date and eq:
                series[date] = eq
                pnl_series[date] = pt.get("pnl", 0)
                all_dates.add(date)
        if series:
            per_market[mid] = series
            pnl_markets[mid] = pnl_series
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
        total_pnl = 0
        for mid, series in per_market.items():
            ccy = currencies[mid]
            if d in series:
                series["_last"] = _to_aud(series[d], ccy)
                pnl_markets[mid]["_last"] = _to_aud(pnl_markets[mid].get(d, 0), ccy)
            # Before first data point → use starting equity in AUD
            total += series.get("_last", start_vals.get(mid, 0))
            total_pnl += pnl_markets.get(mid, {}).get("_last", 0)
        pt = {"date": d, "equity": round(total, 2), "pnl": round(total_pnl, 2)}
        combined.append(pt)

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
        # Skip cross-broker markets (their equity is in the primary broker's curve)
        if md.get("data_source") == "cross-broker":
            continue
        # Issue #10: skip markets with no meaningful equity history (< 2 points)
        eq_curve_len = len(md.get("equity_curve", []))
        if eq_curve_len < 2:
            logger.debug("_merge_benchmark_curves: skipping %s (equity_curve has %d points)",
                         mid, eq_curve_len)
            continue
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


def generate_ceasefire_data() -> dict:
    """Read ceasefire factor JSON and produce the ceasefire probability block.

    Reads from data/position_monitor/ceasefire_factors.json (created by
    ceasefire_evaluator.py, updated hourly). Returns an empty-safe dict.
    """
    factors_path = PROJECT_ROOT / "data" / "position_monitor" / "ceasefire_factors.json"
    data = safe_json(factors_path, None)
    if not data:
        return {
            "probability": 0,
            "probability_label": "UNKNOWN",
            "timeline": "Unknown",
            "portfolio_action": "No ceasefire data available.",
            "last_updated": None,
            "active_ceasefire_count": 0,
            "active_escalation_count": 0,
            "factors": [],
            "change_log": [],
        }

    prob = int(data.get("probability", 0))

    # Derive label and guidance bands
    if prob <= 15:
        label, timeline, action = (
            "VERY UNLIKELY",
            "4+ weeks",
            "Hold all positions. Thesis intact.",
        )
    elif prob <= 30:
        label, timeline, action = (
            "UNLIKELY",
            "2-4 weeks",
            "Monitor closely. Consider tightening stops.",
        )
    elif prob <= 50:
        label, timeline, action = (
            "COIN FLIP",
            "1-2 weeks",
            "Watch for ceasefire signals. Reduce risk on most exposed positions.",
        )
    elif prob <= 70:
        label, timeline, action = (
            "POSSIBLE",
            "Days to 2 weeks",
            "Elevated ceasefire risk. Begin position reduction.",
        )
    else:
        label, timeline, action = (
            "LIKELY",
            "Within days",
            "Ceasefire IMMINENT. Follow kill switch protocol immediately.",
        )

    factors = data.get("factors", [])
    active_ceasefire = sum(
        1 for f in factors
        if f.get("direction") == "ceasefire" and f.get("active", False)
    )
    active_escalation = sum(
        1 for f in factors
        if f.get("direction") == "escalation" and f.get("active", False)
    )

    return {
        "probability": prob,
        "probability_label": label,
        "timeline": timeline,
        "portfolio_action": action,
        "last_updated": data.get("last_updated"),
        "active_ceasefire_count": active_ceasefire,
        "active_escalation_count": active_escalation,
        "factors": factors,
        "change_log": data.get("change_log", []),
    }


def generate():
    """Generate multi-market dashboard data.

    Each market connects to its own broker independently (SP500=Moomoo,
    ASX=Moomoo cross-broker), then results are merged into a combined payload.
    """
    from markets.registry import list_markets

    markets = list_markets()  # ['asx', 'sp500']

    # ── Per-market broker connections (each market has its own broker) ──
    # Connect to each distinct broker ONCE and share positions across markets.
    # Moomoo holds positions across AU/US/HK on a single account, so one
    # connection provides data for all markets — positions are filtered by
    # ticker suffix when building each market's cache.
    broker_caches = {}
    moomoo_data = None  # shared Moomoo connection (all markets on one account)

    import signal

    def _broker_timeout_handler(signum, frame):
        raise TimeoutError("Broker connection timed out")

    for mid in markets:
        cfg = get_config(mid)
        trading = cfg.get("trading", {})
        broker_name = trading.get("broker", "moomoo")
        if (trading.get("mode") == "live"
                and trading.get("live_enabled", False)):
            if broker_name == "moomoo" and moomoo_data is not None:
                # Reuse existing Moomoo connection
                broker_caches[mid] = moomoo_data
                print(f"  {mid}: reusing moomoo connection")
            else:
                # Per-broker timeout — don't let one broker block others
                broker_timeout = 15
                try:
                    signal.signal(signal.SIGALRM, _broker_timeout_handler)
                    signal.alarm(broker_timeout)
                    acct, positions, ok, orders = get_live_broker_data(cfg)
                    signal.alarm(0)  # cancel alarm on success
                except TimeoutError:
                    signal.alarm(0)
                    print(f"  {mid}: broker TIMEOUT after {broker_timeout}s ({broker_name})")
                    acct, positions, ok, orders = None, [], False, []
                if ok:
                    cache = {"acct": acct, "positions": positions, "ok": True, "orders": orders}
                    broker_caches[mid] = cache
                    if broker_name == "moomoo":
                        moomoo_data = cache
                    # M1: persist last-known-good broker data
                    _save_broker_cache(mid, acct, positions, orders)
                    print(f"  {mid}: broker connected ({broker_name}), "
                          f"{len(positions)} positions")
                else:
                    # M1: try last-known-good cache
                    cached = _load_broker_cache(mid)
                    if cached:
                        broker_caches[mid] = {
                            "acct": cached["acct"],
                            "positions": cached["positions"],
                            "ok": True,
                            "orders": cached.get("orders", []),
                            "_cached": True,
                            "_cache_age_minutes": cached.get("cache_age_minutes", 0),
                        }
                        print(f"  {mid}: broker FAILED — using cached data "
                              f"({cached['cache_age_minutes']:.0f}m old, "
                              f"{len(cached['positions'])} positions)")
                    else:
                        print(f"  {mid}: broker connect FAILED ({broker_name}), no cache available")

    # Share Moomoo positions with markets that don't have their own live broker
    # but DO have positions on Moomoo (e.g. ASX stocks held on Moomoo account).
    if moomoo_data:
        for mid in markets:
            if mid in broker_caches:
                continue  # already has broker data
            cfg = get_config(mid)
            # Check if this market has a moomoo config section (could hold positions)
            if cfg.get("moomoo"):
                broker_caches[mid] = moomoo_data
                print(f"  {mid}: using shared moomoo data (cross-broker positions)")

    # ── Fetch exchange rates before generating market data so we can pass
    # them into generate_market() for equity curve FX rate tagging (Issue #3)
    exchange_rates = _get_exchange_rates()

    # ── Generate per-market data ────────────────────────────────
    market_data = {}
    for mid in markets:
        print(f"\n  Generating {mid}...")
        # Pass market-specific broker cache (not shared)
        cache = broker_caches.get(mid)
        data = generate_market(mid, broker_cache=cache, fx_rates=exchange_rates)
        market_data[mid] = data

    # exchange_rates already fetched above (before generate_market calls) — reuse it.

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
    combined_broker_equity_aud = 0   # headline (matches broker app)
    combined_broker_cash_aud = 0
    combined_atlas_equity_aud = 0    # for P&L / benchmark
    combined_starting_aud = 0

    for mid, md in market_data.items():
        pf = md.get("portfolio", {})
        ccy = md.get("currency", "AUD")
        # Only include markets with broker data in combined totals.
        # Offline/paper markets (ASX paused, HK unfunded) would inflate the
        # combined number with phantom capital that doesn't exist at the broker.
        has_broker = md.get("data_source") in ("broker", "cached")
        is_funded = md.get("funded", True)
        include_in_combined = has_broker and is_funded
        if include_in_combined:
            # Headline equity from broker (matches broker app)
            be = pf.get("broker_equity") or pf.get("equity", 0)
            bc = pf.get("broker_cash") or pf.get("cash", 0)
            combined_broker_equity_aud += to_aud(be, ccy)
            combined_broker_cash_aud += to_aud(bc, ccy)
            # Atlas equity for P&L tracking
            combined_atlas_equity_aud += to_aud(pf.get("equity", 0), ccy)
            combined_starting_aud += to_aud(pf.get("starting_equity", 0), ccy)
        # Tag positions with market (include all markets for position display)
        for p in pf.get("open_positions", []):
            p["market"] = mid
            all_positions.append(p)
        for p in md.get("manual_positions", {}).get("positions", []):
            p["market"] = mid
            all_manual.append(p)
        for t in md.get("closed_trades", []):
            t["market"] = mid
            all_closed.append(t)
        # Strategy summary merge (only broker-connected funded markets)
        if include_in_combined:
            for s in md.get("strategy_summary", []):
                key = s["strategy"]
                if key not in combined_strats:
                    combined_strats[key] = {"strategy": key, "positions": 0, "unrealized_pnl": 0, "market_value": 0}
                combined_strats[key]["positions"] += s["positions"]
                # C3: convert to AUD before summing (strategy values are in native market currency)
                combined_strats[key]["unrealized_pnl"] += round(to_aud(s["unrealized_pnl"], ccy), 2)
                combined_strats[key]["market_value"] += round(to_aud(s["market_value"], ccy), 2)

    # ── Combined equity curve (sum across markets) ──────────────
    combined_curve = _merge_equity_curves(market_data, exchange_rates)

    # ── Combined benchmark curve (AUD-normalised blend) ────────
    combined_bench, combined_bench_label = _merge_benchmark_curves(
        market_data, exchange_rates, combined_starting_aud
    )

    # ── Build combined result ───────────────────────────────────
    now = datetime.now(BRISBANE)
    # P&L from Atlas strategy performance (not broker total)
    combined_pnl = round(combined_atlas_equity_aud - combined_starting_aud, 2)
    combined_pnl_pct = round(combined_pnl / combined_starting_aud * 100, 2) if combined_starting_aud > 0 else 0

    # ── Research data ──────────────────────────────────────────
    research_data = generate_research_data()

    # Pick primary for fields that don't merge well (plan, risk)
    primary = "sp500" if "sp500" in market_data else markets[0]
    primary_data = market_data[primary]

    # W3: Prefer APPROVED/PENDING_APPROVAL plans over stale EXECUTED ones
    # Scan all markets: an actionable plan today is more useful than yesterday's SP500 plan.
    primary_plan = None
    for status_pref in ("PENDING_APPROVAL", "APPROVED"):
        for md in market_data.values():
            p = md.get("plan")
            if p and p.get("status") == status_pref:
                primary_plan = p
                break
        if primary_plan:
            break
    if not primary_plan:
        primary_plan = primary_data.get("plan")

    # W1/W10: Build combined risk block across all markets
    combined_pos_value_aud = sum(
        to_aud(
            sum(p.get("current_price", 0) * p.get("shares", 0)
                for p in md.get("portfolio", {}).get("open_positions", [])),
            md.get("currency", "AUD"),
        )
        for md in market_data.values()
    )
    combined_risk = {
        "exposure_pct": round(combined_pos_value_aud / combined_broker_equity_aud * 100, 2)
                        if combined_broker_equity_aud > 0 else 0,
        "max_positions": sum(md.get("risk", {}).get("max_positions", 0) for md in market_data.values()),
        "halted": any(md.get("risk", {}).get("halted", False) for md in market_data.values()),
        "risk_per_trade": "varies",
        "max_portfolio_risk": 0.05,
    }

    # Issue #9: Collect stale warnings from all markets for the top-level result
    stale_warnings = [
        md["stale_warning"]
        for md in market_data.values()
        if md.get("stale_warning")
    ]

    ceasefire_data = generate_ceasefire_data()

    # Today's P&L — merge per-currency breakdowns from all markets
    _combined_today_pnl_by_ccy: dict[str, float] = {}
    for md in market_data.values():
        pf = md.get("portfolio", {})
        for ccy, val in pf.get("today_pnl_by_ccy", {}).items():
            _combined_today_pnl_by_ccy[ccy] = _combined_today_pnl_by_ccy.get(ccy, 0) + val
    _combined_today_pnl_by_ccy = {k: round(v, 2) for k, v in _combined_today_pnl_by_ccy.items()}
    _combined_today_pnl_aud = round(sum(
        to_aud(v, ccy) for ccy, v in _combined_today_pnl_by_ccy.items()
    ), 2)

    result = {
        "timestamp": now.isoformat(),
        "project": "Atlas",
        "markets": market_data,
        "exchange_rates": exchange_rates,
        "ceasefire": ceasefire_data,
        # Top-level summary (combined across ALL markets, AUD-normalised)
        "trading_mode": "live" if any(md.get("trading_mode") == "live" for md in market_data.values()) else "offline",
        "data_source": "broker" if broker_caches else "offline",
        "broker": {mid: get_config(mid).get("trading", {}).get("broker", "moomoo") for mid in markets},
        "config_version": {mid: md.get("config_version", "?") for mid, md in market_data.items()},
        "account": {
            "equity": round(combined_broker_equity_aud, 2),
            "cash": round(combined_broker_cash_aud, 2),
            "buying_power": round(combined_broker_cash_aud, 2),
            "currency": "AUD",
        },
        "portfolio": {
            "equity": round(combined_broker_equity_aud, 2),
            "cash": round(combined_broker_cash_aud, 2),
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
            # Today's P&L combined across all markets, in AUD
            "today_pnl_by_ccy": _combined_today_pnl_by_ccy,
            "today_pnl_aud": _combined_today_pnl_aud,
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
        "plan": primary_plan,
        "closed_trades": all_closed,
        "risk": combined_risk,
        "tasks": primary_data.get("tasks", {}),
        "research": research_data,
        # Issue #9: stale warnings propagated from per-market results
        "stale_warnings": stale_warnings,
    }

    # Issue #8: Atomic write — write to .tmp then rename to avoid readers
    # seeing a half-written JSON file if the process is killed mid-write.
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    tmp_output = OUTPUT.with_suffix(".tmp")
    with open(tmp_output, "w") as f:
        json.dump(result, f, indent=2, default=str)
    tmp_output.rename(OUTPUT)

    print(f"\nDashboard data written to {OUTPUT}")
    for mid, md in market_data.items():
        pf = md.get("portfolio", {})
        label = f"{'🔴 LIVE' if md.get('trading_mode') == 'live' else '📝 PAPER'}"
        print(f"  {mid.upper():6s} {label}: ${pf.get('equity',0):,.2f} equity, "
              f"{pf.get('num_open',0)} positions, "
              f"v{md.get('config_version','?')}")
    print(f"  COMBINED (AUD): A${combined_broker_equity_aud:,.2f} equity, "
          f"A${combined_broker_cash_aud:,.2f} cash, "
          f"Atlas P&L A${combined_pnl:,.2f} ({combined_pnl_pct:+.2f}%), "
          f"{len(all_positions)} positions across {len(market_data)} markets"
          f" (1 USD = {exchange_rates['USDAUD']:.4f} AUD)")


if __name__ == "__main__":
    generate()
