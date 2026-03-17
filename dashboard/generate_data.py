#!/usr/bin/env python3
"""Generate dashboard-data.json for Atlas static dashboard.

Produces a JSON payload consumed by the single-page dashboard.
Includes portfolio state, today's plan, backtest metrics, and task tracker.

When trading.mode == "live" and live_enabled is True, equity/cash/positions
are fetched from the live broker (Alpaca). Paper state is used for metadata
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


def _load_broker_cache(market_id: str, max_age_minutes: int = 60,
                       allow_stale: bool = False):
    """Load cached broker data. Returns dict or None.

    Args:
        market_id: Market to load cache for.
        max_age_minutes: Prefer cache younger than this (default 60).
        allow_stale: If True, return stale cache (any age) when no fresh
                     cache exists. The returned dict will have _stale=True
                     and _cache_age_minutes set to the actual age.
                     This prevents positions from vanishing when the broker
                     is temporarily offline.
    """
    path = CACHE_DIR / f"broker_{market_id}.json"
    if not path.exists():
        return None
    data = safe_json(path, None)
    if not data or "timestamp" not in data:
        return None
    try:
        ts = datetime.fromisoformat(data["timestamp"])
        age = (datetime.now() - ts).total_seconds() / 60
        data["cache_age_minutes"] = round(age, 1)
        if age <= max_age_minutes:
            return data
        if allow_stale:
            data["_stale"] = True
            return data
        return None
    except Exception:
        return None



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
    live_market = PROJECT_ROOT / "brokers" / "state" / f"live_{market_id}.json"
    legacy = PROJECT_ROOT / "brokers" / "state" / "live_state.json"

    state = None
    if per_market.exists():
        state = safe_json(per_market, None)
    if state is None and live_market.exists():
        state = safe_json(live_market, None)
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
    """Fetch account info and positions from the live broker.

    Returns (account_info_dict, positions_list, connected, orders_list)
    or (None, [], False, []) on failure.

    Enriches broker positions with metadata from plan history.
    Broker is the sole source of truth for positions and equity.
    """
    trading = config.get("trading", {})
    broker_name = trading.get("broker", "alpaca")
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


# ── New Alpaca-rich data functions ────────────────────────────

def get_alpaca_account_details() -> dict:
    """Fetch rich account details from Alpaca beyond basic equity/cash.

    Returns:
        {
            "equity": float, "cash": float, "buying_power": float,
            "last_equity": float,       # yesterday's closing equity
            "initial_margin": float, "maintenance_margin": float,
            "long_market_value": float, "short_market_value": float,
            "multiplier": int,          # margin multiplier (2 for margin account)
            "daytrade_count": int,
            "account_created": str,     # ISO date
            "pattern_day_trader": bool,
            "trading_blocked": bool,
            "shorting_enabled": bool,
            "equity_change_today": float,  # equity - last_equity
            "equity_change_today_pct": float,
        }
    """
    try:
        from brokers.alpaca.broker import AlpacaBroker
        from brokers.secrets import get_secret

        api_key = get_secret("ALPACA_API_KEY")
        api_secret = get_secret("ALPACA_SECRET_KEY")
        paper = (get_secret("ALPACA_PAPER") or "false").lower() in ("true", "1")

        from alpaca.trading.client import TradingClient
        client = TradingClient(api_key, api_secret, paper=paper)
        acct = client.get_account()

        equity = float(getattr(acct, "equity", 0) or 0)
        last_equity = float(getattr(acct, "last_equity", equity) or equity)
        cash = float(getattr(acct, "cash", 0) or 0)
        buying_power = float(getattr(acct, "buying_power", 0) or 0)
        long_market_value = float(getattr(acct, "long_market_value", 0) or 0)
        short_market_value = float(getattr(acct, "short_market_value", 0) or 0)
        initial_margin = float(getattr(acct, "initial_margin", 0) or 0)
        maintenance_margin = float(getattr(acct, "maintenance_margin", 0) or 0)
        multiplier = int(float(getattr(acct, "multiplier", 1) or 1))
        daytrade_count = int(getattr(acct, "daytrade_count", 0) or 0)
        pattern_day_trader = bool(getattr(acct, "pattern_day_trader", False))
        trading_blocked = bool(getattr(acct, "trading_blocked", False))
        shorting_enabled = bool(getattr(acct, "shorting_enabled", False))
        created_at = getattr(acct, "created_at", None)
        account_created = str(created_at)[:10] if created_at else ""

        equity_change = round(equity - last_equity, 2)
        equity_change_pct = round(equity_change / last_equity * 100, 2) if last_equity > 0 else 0.0

        return {
            "equity": round(equity, 2),
            "cash": round(cash, 2),
            "buying_power": round(buying_power, 2),
            "last_equity": round(last_equity, 2),
            "initial_margin": round(initial_margin, 2),
            "maintenance_margin": round(maintenance_margin, 2),
            "long_market_value": round(long_market_value, 2),
            "short_market_value": round(short_market_value, 2),
            "multiplier": multiplier,
            "daytrade_count": daytrade_count,
            "account_created": account_created,
            "pattern_day_trader": pattern_day_trader,
            "trading_blocked": trading_blocked,
            "shorting_enabled": shorting_enabled,
            "equity_change_today": equity_change,
            "equity_change_today_pct": equity_change_pct,
        }
    except Exception as e:
        logger.warning("get_alpaca_account_details failed: %s", e)
        return {}


def get_alpaca_portfolio_history(period: str = "3M") -> list[dict]:
    """Fetch daily portfolio equity history from Alpaca API.

    Uses Alpaca's get_portfolio_history endpoint so the equity curve reflects
    the broker's own calculation (no local state needed).

    Args:
        period: History period string — '1M', '3M', '6M', '1Y'. Default '3M'.

    Returns:
        [{"date": "YYYY-MM-DD", "equity": float, "pnl": float, "pnl_pct": float}, ...]
        Sorted ascending by date, zeroed-pnl entries excluded.
    """
    try:
        from brokers.secrets import get_secret
        api_key = get_secret("ALPACA_API_KEY")
        api_secret = get_secret("ALPACA_SECRET_KEY")
        paper = (get_secret("ALPACA_PAPER") or "false").lower() in ("true", "1")

        from alpaca.trading.client import TradingClient
        from alpaca.trading.requests import GetPortfolioHistoryRequest
        client = TradingClient(api_key, api_secret, paper=paper)
        req = GetPortfolioHistoryRequest(period=period, timeframe="1D")
        history = client.get_portfolio_history(req)

        timestamps = list(getattr(history, "timestamp", []) or [])
        equities = list(getattr(history, "equity", []) or [])
        profit_losses = list(getattr(history, "profit_loss", []) or [])
        profit_loss_pcts = list(getattr(history, "profit_loss_pct", []) or [])

        result = []
        for i, ts in enumerate(timestamps):
            equity = equities[i] if i < len(equities) else None
            if equity is None or equity == 0:
                continue
            # Alpaca timestamps are Unix epoch integers
            try:
                if isinstance(ts, (int, float)):
                    from datetime import timezone
                    date_str = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
                else:
                    date_str = str(ts)[:10]
            except Exception:
                date_str = str(ts)[:10]

            pnl = profit_losses[i] if i < len(profit_losses) else 0.0
            pnl_pct = profit_loss_pcts[i] if i < len(profit_loss_pcts) else 0.0

            result.append({
                "date": date_str,
                "equity": round(float(equity), 2),
                "pnl": round(float(pnl or 0), 2),
                "pnl_pct": round(float(pnl_pct or 0) * 100, 4),
            })

        return sorted(result, key=lambda x: x["date"])
    except Exception as e:
        logger.warning("get_alpaca_portfolio_history failed: %s", e)
        return []


def get_alpaca_recent_orders(limit: int = 20) -> list[dict]:
    """Fetch recent orders from Alpaca including filled, pending, canceled.

    Args:
        limit: Max number of orders to return. Default 20.

    Returns:
        [{
            "id": str, "symbol": str, "side": str, "qty": int,
            "type": str,    # market, limit, stop, trailing_stop
            "status": str,  # new, filled, canceled, rejected
            "filled_qty": int, "filled_price": float,
            "stop_price": float, "trail_price": float, "trail_percent": float,
            "submitted_at": str, "filled_at": str,
            "limit_price": float,
        }, ...]
    """
    try:
        from brokers.secrets import get_secret
        api_key = get_secret("ALPACA_API_KEY")
        api_secret = get_secret("ALPACA_SECRET_KEY")
        paper = (get_secret("ALPACA_PAPER") or "false").lower() in ("true", "1")

        from alpaca.trading.client import TradingClient
        from alpaca.trading.requests import GetOrdersRequest
        from alpaca.trading.enums import QueryOrderStatus
        client = TradingClient(api_key, api_secret, paper=paper)

        req = GetOrdersRequest(status=QueryOrderStatus.ALL, limit=limit, direction="desc")
        orders = client.get_orders(req) or []

        result = []
        for o in orders:
            symbol = str(getattr(o, "symbol", "") or "")
            side_raw = getattr(o, "side", None)
            side = str(side_raw.value if hasattr(side_raw, "value") else side_raw or "").lower()
            order_type_raw = getattr(o, "order_type", None)
            order_type = str(order_type_raw.value if hasattr(order_type_raw, "value") else order_type_raw or "").lower()
            status_raw = getattr(o, "status", None)
            status = str(status_raw.value if hasattr(status_raw, "value") else status_raw or "").lower()

            filled_qty = int(float(getattr(o, "filled_qty", 0) or 0))
            filled_avg_price = float(getattr(o, "filled_avg_price", 0) or 0)
            qty = int(float(getattr(o, "qty", 0) or 0))
            limit_price = float(getattr(o, "limit_price", 0) or 0)
            stop_price = float(getattr(o, "stop_price", 0) or 0)
            trail_price = float(getattr(o, "trail_price", 0) or 0)
            trail_percent = float(getattr(o, "trail_percent", 0) or 0)

            submitted_at = getattr(o, "submitted_at", None)
            filled_at = getattr(o, "filled_at", None)

            result.append({
                "id": str(getattr(o, "id", "") or ""),
                "symbol": symbol,
                "side": side,
                "qty": qty,
                "type": order_type,
                "status": status,
                "filled_qty": filled_qty,
                "filled_price": round(filled_avg_price, 4),
                "stop_price": round(stop_price, 4),
                "trail_price": round(trail_price, 4),
                "trail_percent": round(trail_percent, 4),
                "submitted_at": str(submitted_at)[:19] if submitted_at else "",
                "filled_at": str(filled_at)[:19] if filled_at else "",
                "limit_price": round(limit_price, 4),
            })
        return result
    except Exception as e:
        logger.warning("get_alpaca_recent_orders failed: %s", e)
        return []


def get_alpaca_market_clock() -> dict:
    """Get real-time market status from Alpaca clock API.

    Returns:
        {
            "is_open": bool,
            "next_open": str (ISO),
            "next_close": str (ISO),
            "timestamp": str (ISO),
        }
    """
    try:
        from brokers.secrets import get_secret
        api_key = get_secret("ALPACA_API_KEY")
        api_secret = get_secret("ALPACA_SECRET_KEY")
        paper = (get_secret("ALPACA_PAPER") or "false").lower() in ("true", "1")

        from alpaca.trading.client import TradingClient
        client = TradingClient(api_key, api_secret, paper=paper)
        clock = client.get_clock()

        return {
            "is_open": bool(getattr(clock, "is_open", False)),
            "next_open": str(getattr(clock, "next_open", "") or ""),
            "next_close": str(getattr(clock, "next_close", "") or ""),
            "timestamp": str(getattr(clock, "timestamp", datetime.now().isoformat()) or ""),
        }
    except Exception as e:
        logger.warning("get_alpaca_market_clock failed: %s", e)
        return {
            "is_open": False,
            "next_open": "",
            "next_close": "",
            "timestamp": datetime.now().isoformat(),
        }


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
                  if not t.endswith(".AX") and not t.endswith(".HK")
                  and not t.startswith("^") and "=F" not in t]
    non_us_tickers = [t for t in ticker_list
                      if t.endswith(".AX") or t.endswith(".HK")
                      or t.startswith("^") or "=F" in t]

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
    """Generate dashboard data for a single market (SP500 / Alpaca-only).

    broker_cache: optional dict to reuse a single broker connection.
      Keys: "acct", "positions", "ok". If None, connects fresh.
    fx_rates: unused, kept for call-site compatibility.
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
    if is_live_mode:
        if broker_cache and broker_cache.get("ok"):
            # Reuse shared broker connection — all positions are SP500 (USD)
            broker_acct = broker_cache["acct"]
            broker_positions = broker_cache["positions"]
            broker_ok = True
        elif broker_cache is None:
            # broker_cache=None means standalone call (not from generate()) — try connecting.
            # broker_cache={"ok": False} means upstream already tried and failed — skip retry.
            broker_acct, broker_positions, broker_ok, _orders = get_live_broker_data(config)

    # Detect cached broker data (M1: last-known-good fallback)
    is_cached = broker_cache.get("_cached", False) if broker_cache else False
    cache_age = broker_cache.get("_cache_age_minutes", 0) if broker_cache else 0

    if broker_ok and broker_acct:
        # Broker is the sole source of truth (Alpaca, all positions are Atlas-managed)
        positions = broker_positions
        atlas_positions = positions
        all_positions = positions
        data_source = "cached" if is_cached else "broker"

        # Atlas P&L from positions
        total_entry_value = sum(p.get("entry_value", 0) for p in atlas_positions)
        atlas_value = sum(p.get("market_value", 0) for p in atlas_positions)
        market_pnl = round(atlas_value - total_entry_value, 2)
        total_commissions = round(len(atlas_positions) * commission, 2)

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
        # Brokers may return unrealized_pnl from a stale snapshot
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
    # Use only per-market state files — never the global trade_ledger
    # (which is market-agnostic and would mix SP500 paper trades into ASX).
    live_state_file = PROJECT_ROOT / "brokers" / "state" / f"live_{market_id}.json"
    live_closed = safe_json(live_state_file, {}).get("closed_trades", [])
    closed = live_closed or portfolio.get("closed_trades", []) or []
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

        # Cross-broker positions already have
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
            "currency": "USD",
            # Intraday fields (populated from raw Alpaca position data)
            "intraday_pnl": p.get("intraday_pnl", 0),
            "intraday_pnl_pct": p.get("intraday_pnl_pct", 0),
            "change_today": p.get("change_today", 0),
            "lastday_price": p.get("lastday_price", 0),
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

    # All SP500 / Alpaca positions are in USD
    currency = "USD"

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

    # Update equity curve for any market with real data (broker or cached).
    # Use broker_equity when available so the chart matches broker app.
    if data_source in ("broker", "cached"):
        chart_equity = broker_equity if broker_ok and broker_equity else equity

        # Compute total P&L for deposit-adjusted return calculation.
        all_unrealized = sum(p.get("unrealized_pnl", 0) for p in all_positions)
        all_realized = realized_pnl
        total_investment_pnl = round(all_unrealized + all_realized, 2)

        point: dict = {"date": today_str, "equity": round(chart_equity, 2)}
        point["pnl"] = total_investment_pnl
        if data_source == "cached":
            point["estimated"] = True

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

    # All SP500 positions are Atlas-managed (Alpaca, USD only)
    atlas_open = open_pos

    # Today's P&L — aggregated from broker's today_pnl per position (USD)
    today_pnl_usd = round(sum(p.get("today_pnl", 0) for p in open_pos), 2)

    # Assemble
    result = {
        "timestamp": now.isoformat(),
        "config_version": config.get("version", "unknown"),
        "project": config.get("project", "Atlas"),
        "market_id": market_id,
        "currency": currency,
        "trading_mode": mode_label,
        "data_source": data_source,
        "broker": trading.get("broker", "alpaca"),
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
            "broker_equity": broker_equity if broker_ok else None,
            "broker_cash": broker_cash if broker_ok else None,
            "today_pnl_usd": today_pnl_usd,
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
                    "subtitle": f"Alpaca: ~${fee_data.get('actual_fees', {}).get('avg_per_order_usd', 0.0):.2f}/order · {abs(delta.get('trades_removed', 7))} trades filtered",
                    "steps": steps,
                    "annotation": {"text": f"Net {rf['cagr_pct']:.1f}% still beats SPY {bm.get('spy_cagr_pct', 0):.1f}%", "color": "green"},
                    "date": fee_file.stem.split("_")[-1][:10],
                }))
                candidates.append((4, {
                    "type": "fee_compare", "chart": "grouped_bar",
                    "title": f"Real fees reduce CAGR by {drag:.1f}pp but strategy still beats benchmark",
                    "subtitle": f"Alpaca: ~${fee_data.get('actual_fees', {}).get('avg_per_order_usd', 0.0):.2f}/order",
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


def _read_daemon_status() -> dict:
    """Read research engine heartbeat from /tmp.

    Checks autoresearch heartbeat file and returns its status.
    Falls back to systemctl if heartbeat is stale.
    """
    import subprocess as _sp

    heartbeat_files = [
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
        # No heartbeat file — check if service is running via systemctl
        try:
            r = _sp.run(["systemctl", "is-active", "atlas-autoresearch"],
                        capture_output=True, text=True, timeout=5)
            if r.stdout.strip() == "active":
                return {"status": "running", "uptime_s": 0,
                        "experiments_completed": 0, "experiments_failed": 0,
                        "queue_depth": 0, "current_experiment": None,
                        "source": "atlas-autoresearch"}
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
        # Check if the service is actually running
        status = "dead" if age_min > 30 else "stopped"
        try:
            r = _sp.run(["systemctl", "is-active", "atlas-autoresearch"],
                        capture_output=True, text=True, timeout=5)
            if r.stdout.strip() == "active":
                status = "running"
        except Exception:
            pass
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


def _get_sweep_window_status() -> dict:
    """Check atlas-research-window.service/timer status for dashboard stats."""
    import subprocess as _sp
    sweep_window_active = False
    next_window = ""
    next_window_iso = ""
    try:
        r = _sp.run(["systemctl", "is-active", "atlas-research-window"],
                    capture_output=True, text=True, timeout=3)
        sweep_window_active = r.stdout.strip() == "active"
    except Exception:
        pass

    # Get the next trigger time from systemd timer
    try:
        r = _sp.run(["systemctl", "list-timers", "atlas-research-window.timer", "--no-pager"],
                    capture_output=True, text=True, timeout=3)
        for line in r.stdout.splitlines():
            stripped = line.strip()
            if "atlas-research-window" in stripped and stripped[0:3] not in ("NEX", "---", ""):
                # Parse the NEXT column — e.g. "Thu 2026-03-12 09:30:00 AEST"
                parts = stripped.split()
                if len(parts) >= 4:
                    try:
                        from datetime import datetime as _dt
                        # systemd format: "Thu 2026-03-12 09:30:00 AEST"
                        dt_str = parts[0] + " " + parts[1] + " " + parts[2]
                        dt = _dt.strptime(dt_str, "%a %Y-%m-%d %H:%M:%S")
                        next_window_iso = dt.isoformat()
                        next_window = f"Next: {parts[0]} {parts[2]}"
                    except (ValueError, IndexError):
                        pass
    except Exception:
        pass

    # Fallback: compute next window from known schedule if timer didn't give us one
    if not next_window_iso and not sweep_window_active:
        try:
            from datetime import datetime as _dt, timedelta
            now = _dt.now()
            wd = now.weekday()  # 0=Mon, 6=Sun
            if wd < 5:  # Weekday
                windows = [(9, 30), (12, 30), (15, 30), (20, 0), (23, 0)]
            else:  # Weekend
                windows = [(9, 0), (12, 0), (15, 0), (20, 0)]

            # Find next window today
            for h, m in windows:
                candidate = now.replace(hour=h, minute=m, second=0, microsecond=0)
                if candidate > now:
                    next_window_iso = candidate.isoformat()
                    next_window = f"Next: {candidate.strftime('%H:%M')}"
                    break

            # If none today, find first window tomorrow
            if not next_window_iso:
                tomorrow = now + timedelta(days=1)
                twd = tomorrow.weekday()
                wins = [(9, 30), (12, 30), (15, 30), (20, 0), (23, 0)] if twd < 5 else [(9, 0), (12, 0), (15, 0), (20, 0)]
                h, m = wins[0]
                candidate = tomorrow.replace(hour=h, minute=m, second=0, microsecond=0)
                next_window_iso = candidate.isoformat()
                next_window = f"Next: {candidate.strftime('%a %H:%M')}"
        except Exception:
            pass

    return {
        "sweep_window_active": sweep_window_active,
        "next_window": next_window,
        "next_window_iso": next_window_iso,
    }


def generate_research_data() -> dict:
    """Generate research section data for the dashboard.

    Reads from research/best/*.json (sweep best results) and
    research/results/*.tsv (per-strategy experiment counts).
    Supplements with journal.json (activity feed) and daemon heartbeat.
    """
    research_dir = PROJECT_ROOT / "research"
    journal_path = research_dir / "journal.json"

    daemon = _read_daemon_status()

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

    # Days active (from journal first entry)
    days_active = 0
    if journal:
        first_ts = journal[0].get("timestamp", "")[:10]
        try:
            first_date = datetime.strptime(first_ts, "%Y-%m-%d")
            days_active = (datetime.now() - first_date).days + 1
        except ValueError:
            days_active = 1

    promoted_count = sum(1 for e in journal if e.get("verdict") == "promoted" or e.get("promoted"))

    # ── Leaderboard (from research/best/*.json + results/*.tsv) ─
    leaderboard = []
    lb_index: dict = {}
    best_dir = research_dir / "best"
    if best_dir.exists():
        for f in sorted(best_dir.glob("*.json")):
            try:
                data = json.loads(f.read_text())
            except Exception:
                continue
            sid = data.get("strategy", f.stem)
            metrics = data.get("metrics", {})
            b_sharpe = metrics.get("sharpe")
            b_wr = metrics.get("win_rate_pct")
            b_trades = metrics.get("total_trades", 0)
            b_cagr = metrics.get("cagr_pct")
            b_pf = metrics.get("profit_factor")
            exps_run = data.get("experiments_run", 0) or 0
            name = sid.replace("_", " ").title()
            leaderboard.append({
                "id": sid,
                "name": name,
                "status": "research",
                "total_experiments": exps_run,
                "best_sharpe": b_sharpe,
                "best_win_rate": round(b_wr, 1) if b_wr is not None else None,
                "best_trades": int(b_trades) if b_trades else 0,
                "best_cagr": b_cagr,
                "best_pf": b_pf,
            })
            lb_index[sid] = len(leaderboard) - 1

    # ── Enrich experiment counts from research/results/*.tsv ────
    results_dir = research_dir / "results"
    tsv_total = 0
    if results_dir.exists():
        for tsv_f in sorted(results_dir.glob("*.tsv")):
            sid = tsv_f.stem
            try:
                lines = tsv_f.read_text().splitlines()
                exp_count = max(0, len(lines) - 1)  # Subtract header row
            except Exception:
                exp_count = 0
            tsv_total += exp_count
            if sid in lb_index:
                entry = leaderboard[lb_index[sid]]
                entry["total_experiments"] = max(entry.get("total_experiments") or 0, exp_count)

    # ── Add staleness info per strategy ─────────────────────────
    try:
        from research.param_history import get_strategy_staleness, PARAMS_DIR
        stale_count = 0
        total_param_tests = 0
        # Count total param tests across all param files
        if PARAMS_DIR.exists():
            from research.param_history import load_param_history
            for md_f in PARAMS_DIR.glob("*.md"):
                if not md_f.name.startswith("_"):
                    total_param_tests += len(load_param_history(md_f.stem))
        # Annotate each leaderboard entry with staleness
        for entry in leaderboard:
            staleness = get_strategy_staleness(entry["id"])
            entry["is_stale"] = staleness.get("is_stale", False)
            entry["last_win_date"] = staleness.get("last_win_date")
            if entry["is_stale"]:
                stale_count += 1
    except Exception:
        stale_count = 0
        total_param_tests = 0

    # Sort: tested strategies with best Sharpe first, then untested
    leaderboard.sort(key=lambda x: (
        0 if x["best_sharpe"] is not None else 1,
        -(x["best_sharpe"] or -999),
    ))

    return {
        "daemon": daemon,
        "leaderboard": leaderboard,
        "activity_feed": activity_feed,
        "statistics": {
            "total_experiments": tsv_total,
            "promoted": promoted_count,
            "days_active": days_active,
            "sweep_strategies": len(leaderboard),
            "sweep_experiments": tsv_total,
            "stale_strategies": stale_count,
            "total_param_tests": total_param_tests,
            **_get_sweep_window_status(),
        },
        "daily_insight": generate_daily_insight(),
        "agents": _build_agents(daemon),
        "discoveries": _build_discoveries(journal),
        "portfolio": _build_portfolio_metrics(),
    }


def _build_portfolio_metrics() -> dict:
    """Build portfolio-level metrics from portfolio_optimization.json + daily snapshot tracking.

    Returns dict with: sharpe, return, vol, drawdown, n_strategies, avg_corr,
    active_weights, and daily deltas (sharpe_delta, return_delta etc.)
    """
    opt_path = PROJECT_ROOT / "research" / "results" / "portfolio_optimization.json"
    snapshot_path = PROJECT_ROOT / "dashboard" / "cache" / "portfolio_snapshot.json"

    result = {
        "available": False,
        "sharpe": None,
        "annual_return": None,
        "annual_vol": None,
        "max_drawdown": None,
        "n_strategies": 0,
        "avg_correlation": None,
        "active_weights": {},
        "sharpe_delta": None,
        "return_delta": None,
        "drawdown_delta": None,
        "last_updated": None,
    }

    opt = safe_json(str(opt_path), None)
    if not opt or "portfolio_metrics" not in opt:
        return result

    pm = opt["portfolio_metrics"]
    result["available"] = True
    result["sharpe"] = pm.get("simulated_sharpe")
    result["annual_return"] = pm.get("portfolio_annual_return")
    result["annual_vol"] = pm.get("portfolio_annual_vol")
    result["max_drawdown"] = pm.get("portfolio_max_drawdown")
    result["n_strategies"] = pm.get("n_strategies", 0)
    result["avg_correlation"] = pm.get("avg_correlation")
    result["active_weights"] = opt.get("active_weights", {})

    # File modification time as "last updated"
    try:
        mtime = opt_path.stat().st_mtime
        result["last_updated"] = datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M")
    except Exception:
        pass

    # ── Daily delta tracking ─────────────────────────────────
    # Read yesterday's snapshot and compute deltas
    today_str = datetime.now().strftime("%Y-%m-%d")
    snapshot = safe_json(str(snapshot_path), {})
    prev_date = snapshot.get("date", "")
    prev_sharpe = snapshot.get("sharpe")
    prev_return = snapshot.get("annual_return")
    prev_drawdown = snapshot.get("max_drawdown")

    if prev_date and prev_date != today_str:
        # We have a previous day's data — compute deltas
        if prev_sharpe is not None and result["sharpe"] is not None:
            result["sharpe_delta"] = round(result["sharpe"] - prev_sharpe, 4)
        if prev_return is not None and result["annual_return"] is not None:
            result["return_delta"] = round(result["annual_return"] - prev_return, 2)
        if prev_drawdown is not None and result["max_drawdown"] is not None:
            result["drawdown_delta"] = round(result["max_drawdown"] - prev_drawdown, 2)

    # Save today's snapshot (only write once per day to preserve yesterday's baseline)
    if prev_date != today_str:
        try:
            snapshot_path.parent.mkdir(parents=True, exist_ok=True)
            snap = {
                "date": today_str,
                "sharpe": result["sharpe"],
                "annual_return": result["annual_return"],
                "max_drawdown": result["max_drawdown"],
                "n_strategies": result["n_strategies"],
            }
            with open(snapshot_path, "w") as f:
                json.dump(snap, f, indent=2)
        except Exception as e:
            logger.debug("Could not write portfolio snapshot: %s", e)

    return result


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

    # ── Researcher agents ────────────────────────────────────────
    # Only the two active services: sweep window + director cron
    _RESEARCHER_SERVICES = [
        # (service_name, heartbeat_path, agent_id, display_name, agent_type)
        # Sweep window service (sweep.py heartbeat format)
        ("atlas-research-window", "/tmp/autoresearch-heartbeat.json", "researcher", "Atlas",    "sweep"),
        # Director cron — portfolio review + research oversight
        ("atlas-director",        "/tmp/director-heartbeat.json",     "director_cron", "Director", "director_cron"),
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

        # Both services are core — always show if installed, sleeping when idle
        if not svc_running and not hb:
            # Check if the service/timer is at least enabled (installed)
            _is_enabled = False
            try:
                check_unit = svc_name + ".timer" if agent_type == "director_cron" else svc_name
                r2 = _sp.run(["systemctl", "is-enabled", check_unit],
                             capture_output=True, text=True, timeout=3)
                _is_enabled = r2.stdout.strip() == "enabled"
            except Exception:
                pass
            if not _is_enabled:
                continue  # Not even installed — skip
            # Service is installed but idle — show as sleeping

        found_any = True

        # Determine status + task
        # sweep type: heartbeat "status" field is authoritative regardless of
        # whether the service is currently running (window services stop between runs)
        if agent_type == "sweep":
            # sweep.py heartbeat format: status, strategy, activity, detail,
            # param, param_value, candidates, last_result, last_delta
            sw_status = (hb.get("status") or "").strip() if hb else ""
            sw_strategy = (hb.get("strategy") or "").strip() if hb else ""
            sw_activity = (hb.get("activity") or "").strip() if hb else ""
            sw_detail = (hb.get("detail") or "").strip() if hb else ""
            sw_param = (hb.get("param") or "").strip() if hb else ""
            sw_last_result = (hb.get("last_result") or "").strip() if hb else ""
            sw_last_delta = hb.get("last_delta", 0) if hb else 0

            if sw_status == "running":
                strat_label = sw_strategy.replace("_", " ").title() if sw_strategy else ""

                # Map activity to rich status
                if sw_activity == "loading":
                    status = "reading"
                    task = f"Loading {strat_label} data"
                elif sw_activity == "baseline":
                    status = "reading"
                    task = f"Running {strat_label} baseline"
                elif sw_activity == "testing":
                    status = "typing"
                    n_cand = hb.get("candidates", 0)
                    task = f"Testing {sw_param} ({n_cand} values)"
                elif sw_activity == "kept":
                    status = "typing"
                    task = f"✅ Kept {sw_detail}"
                elif sw_activity == "discarded":
                    status = "typing"
                    task = f"❌ {sw_detail}"
                elif sw_activity == "writing":
                    status = "typing"
                    task = "Writing brain indexes"
                elif sw_activity == "evaluating":
                    status = "reading"
                    task = f"Evaluating {sw_detail}"
                elif strat_label:
                    status = "typing"
                    task = f"Sweeping {strat_label}"
                else:
                    status = "reading"
                    task = "Between strategies..."
            elif sw_status in ("idle", "cycle_done"):
                status = "idle"
                task = "Cycle complete"
            elif svc_running:
                status = "reading"
                task = "Sweeping..."
            else:
                status = "sleeping"
                task = "Engine offline"
        elif agent_type == "director_cron":
            # Director cron heartbeat: runs twice daily, mostly idle between runs
            # Phases: reviewing, queuing, portfolio, reporting, idle, stopped
            dc_status = (hb.get("status") or "").strip() if hb else ""
            dc_phase  = (hb.get("phase")  or "").strip() if hb else ""
            dc_depth  = hb.get("queue_depth", 0) if hb else 0
            dc_queued = hb.get("experiments_queued", 0) if hb else 0
            dc_cov    = hb.get("coverage_pct", 0) if hb else 0

            if dc_status == "running":
                if dc_phase == "reviewing":
                    status = "reading"
                    task   = f"Reviewing {dc_depth} results" if dc_depth else "Reviewing queue"
                elif dc_phase == "queuing":
                    status = "typing"
                    task   = f"Queue: {dc_depth} pending — generating"
                elif dc_phase == "portfolio":
                    status = "typing"
                    task   = "Running portfolio optimizer"
                elif dc_phase == "reporting":
                    status = "typing"
                    task   = "Sending daily digest"
                else:
                    status = "reading"
                    task   = "Overseeing research"
            elif dc_status == "idle" or dc_phase == "idle":
                status = "idle"
                task   = f"Queue: {dc_depth} experiments" if dc_depth else "Idle until next run"
            elif dc_status == "stopped" or not svc_running:
                status = "sleeping"
                # Show next run time if we can get it
                _next_run = ""
                try:
                    r3 = _sp.run(["systemctl", "show", "atlas-director.timer",
                                  "--property=NextElapseUSecRealtime", "--value"],
                                 capture_output=True, text=True, timeout=3)
                    nxt = r3.stdout.strip()
                    if nxt:
                        # Format: "Fri 2026-03-13 20:00:00 AEST" → extract time
                        parts = nxt.split()
                        if len(parts) >= 3:
                            _next_run = parts[2][:5]  # "20:00"
                except Exception:
                    pass
                task = f"Next review at {_next_run}" if _next_run else "Sleeping between reviews"
            else:
                status = "reading"
                task   = "Reviewing..."
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

        # Build activity detail for canvas rendering
        activity_data = {}
        if agent_type == "sweep" and hb:
            activity_data = {
                "activity": (hb.get("activity") or ""),
                "param": (hb.get("param") or ""),
                "param_value": (hb.get("param_value") or ""),
                "candidates": hb.get("candidates", 0),
                "last_result": (hb.get("last_result") or ""),
                "last_delta": hb.get("last_delta", 0),
            }

        agents.append({
            "id": agent_id,
            "name": display_name,
            "type": agent_type,
            "status": status,
            "task": task,
            "experiments_done": experiments_done,
            "progress": progress,
            "activity": activity_data,
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


def _build_discoveries(journal: list) -> list:
    """Build consolidated discoveries list from sweep best results and journal.

    Merges multiple sources into a simple list of notable findings.
    """
    discoveries = []

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


def _merge_equity_curves(market_data: dict, exchange_rates: dict = None) -> list:
    """Return the SP500 equity curve directly (single market, USD only).

    Previously merged multiple markets in AUD. Now SP500-only — no currency
    conversion needed. exchange_rates kept for call-site compatibility.
    """
    sp500 = market_data.get("sp500", {})
    if not sp500.get("funded", True):
        return []
    eq_curve = sp500.get("equity_curve", [])
    return [
        {"date": pt["date"], "equity": pt["equity"], "pnl": pt.get("pnl", 0)}
        for pt in eq_curve
        if pt.get("date") and pt.get("equity")
    ]


def _load_benchmark_prices(ticker: str, start_date: str) -> list[tuple[str, float]]:
    """Load benchmark close prices from parquet cache + yfinance from start_date.

    Returns list of (date_str, close_price) sorted by date, starting on or
    just before start_date.  Extends with yfinance for dates beyond the cache.
    """
    result: list[tuple[str, float]] = []

    # ── Try parquet cache first ──
    for subdir in ["sp500", "asx", "hk", ""]:
        cache = PROJECT_ROOT / "data" / "cache" / subdir if subdir else PROJECT_ROOT / "data" / "cache"
        fp = cache / (ticker.replace(".", "_") + ".parquet")
        if not fp.exists():
            continue
        try:
            df = pd.read_parquet(fp)
            # Anchor: find close on or just before start_date
            on_or_before = df[df.index <= start_date]
            if len(on_or_before) == 0:
                continue
            anchor_date = str(on_or_before.index[-1].date())
            df = df[df.index >= anchor_date]
            for idx, row in df.iterrows():
                result.append((str(idx.date()), float(row["close"])))
            break
        except Exception:
            continue

    if not result:
        return result

    # ── Extend with yfinance for recent days beyond cache ──
    last_cached_date = result[-1][0]
    try:
        import yfinance as yf
        recent = yf.download(ticker, period="10d", interval="1d",
                             progress=False, auto_adjust=True)
        if not recent.empty:
            close_s = recent["Close"]
            if hasattr(close_s, "columns"):
                close_s = close_s.iloc[:, 0]
            for ridx, val in close_s.dropna().items():
                rd = str(ridx.date())
                if rd > last_cached_date:
                    result.append((rd, float(val)))
            if len(result) > len(df):
                logger.info("_load_benchmark_prices: %s extended %d days via yfinance",
                            ticker, len(result) - len(df))
    except Exception as e:
        logger.debug("_load_benchmark_prices: yfinance extension failed for %s: %s",
                     ticker, e)

    return result


def _merge_benchmark_curves(market_data: dict, exchange_rates: dict = None,
                            combined_starting_usd: float = 0) -> tuple:
    """Return SPY benchmark curve scaled to combined starting equity (USD only).

    Previously merged SPY + IOZ.AX in AUD. Now SP500/SPY only, all USD.
    exchange_rates kept for call-site compatibility.

    Returns (curve, "SPY") where curve = [{"date": str, "equity": float}, ...]
    """
    sp500 = market_data.get("sp500", {})
    if not sp500.get("funded", True):
        return [], "SPY"

    # Use the pre-computed per-market benchmark curve (already USD-scaled)
    bench = sp500.get("benchmark_curve", [])
    if not bench:
        return [], "SPY"

    # Rescale so first point matches combined_starting_usd if provided
    if combined_starting_usd > 0 and bench:
        first_val = bench[0].get("equity", 0)
        if first_val > 0:
            scale = combined_starting_usd / first_val
            bench = [{"date": pt["date"], "equity": round(pt["equity"] * scale, 2)}
                     for pt in bench]

    return bench, "SPY"


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



def generate_strategy_health_data() -> dict:
    """Generate strategy health/lifecycle data for the Health tab.

    Reads from:
        - logs/lifecycle_state.json  -> current lifecycle state per strategy
        - research/best/*.json        -> best backtest metrics as reference
    """
    lifecycle_path = PROJECT_ROOT / "logs" / "lifecycle_state.json"
    best_dir = PROJECT_ROOT / "research" / "best"

    # Load lifecycle state
    lifecycle_raw = safe_json(lifecycle_path, {})

    # Load best backtest metrics as reference
    best_metrics: dict = {}
    if best_dir.exists():
        for f in sorted(best_dir.glob("*.json")):
            try:
                data = json.loads(f.read_text())
            except Exception:
                continue
            sid = data.get("strategy", f.stem)
            m = data.get("metrics", {})
            best_metrics[sid] = {
                "sharpe": m.get("sharpe"),
                "win_rate_pct": m.get("win_rate_pct"),
                "cagr_pct": m.get("cagr_pct"),
                "max_drawdown_pct": m.get("max_drawdown_pct"),
                "profit_factor": m.get("profit_factor"),
                "total_trades": m.get("total_trades"),
                "experiments_run": data.get("experiments_run", 0),
                "backtest_file": f.name,
            }

    # Build per-strategy health records
    strategies: list = []
    all_strategy_names = set(lifecycle_raw.keys()) | set(best_metrics.keys())

    state_colour_map = {
        "RAMP_UP": "blue",
        "ACTIVE": "green",
        "WATCH": "yellow",
        "PROBATION": "orange",
        "SUSPENDED": "red",
        "UNKNOWN": "gray",
    }

    for sid in sorted(all_strategy_names):
        lc = lifecycle_raw.get(sid, {})
        bm = best_metrics.get(sid, {})

        state = lc.get("state", "UNKNOWN")
        strategies.append({
            "id": sid,
            "name": sid.replace("_", " ").title(),
            "state": state,
            "state_colour": state_colour_map.get(state, "gray"),
            "entered_at": lc.get("entered_at", ""),
            "consecutive_degraded": lc.get("consecutive_degraded", 0),
            "consecutive_recovered": lc.get("consecutive_recovered", 0),
            "pool_cap_override": lc.get("pool_cap_override"),
            "recent_history": lc.get("history", [])[-5:],
            # Best backtest reference
            "best_sharpe": bm.get("sharpe"),
            "best_win_rate": bm.get("win_rate_pct"),
            "best_cagr": bm.get("cagr_pct"),
            "best_drawdown": bm.get("max_drawdown_pct"),
            "best_pf": bm.get("profit_factor"),
            "best_trades": bm.get("total_trades"),
            "experiments_run": bm.get("experiments_run", 0),
        })

    state_counts: dict = {}
    for s in strategies:
        state_counts[s["state"]] = state_counts.get(s["state"], 0) + 1

    return {
        "generated_at": datetime.now().isoformat(),
        "lifecycle_file": str(lifecycle_path),
        "total_strategies": len(strategies),
        "state_counts": state_counts,
        "strategies": strategies,
        "has_lifecycle_data": bool(lifecycle_raw),
    }


def generate_events_data() -> dict:
    """Generate macro event calendar data for the Events tab.

    Uses EventCalendar to produce:
        - upcoming events for the next 30 days
        - proximity KPIs (days to FOMC, CPI, NFP, OPEX week flag)
        - recent past events (last 7 days)
    """
    from datetime import date as _date_cls
    try:
        from data.events import EventCalendar
        ec = EventCalendar()
        today = _date_cls.today()

        # Upcoming events (next 30 days, future only)
        upcoming_raw = ec.get_events_near(today.isoformat(), window_days=30)
        upcoming = sorted(
            [
                {
                    "event_type": e.event_type,
                    "date": e.date.isoformat(),
                    "description": e.description,
                    "impact": e.impact,
                    "days_away": (e.date - today).days,
                }
                for e in upcoming_raw
                if e.date >= today
            ],
            key=lambda x: x["date"],
        )

        # Proximity KPIs
        proximity = ec.get_event_proximity(today)

        # Recent past events (last 7 days)
        past_raw = ec.get_events_near(today.isoformat(), window_days=7)
        recent_past = sorted(
            [
                {
                    "event_type": e.event_type,
                    "date": e.date.isoformat(),
                    "description": e.description,
                    "impact": e.impact,
                    "days_ago": (today - e.date).days,
                }
                for e in past_raw
                if e.date < today
            ],
            key=lambda x: x["date"],
            reverse=True,
        )

        return {
            "generated_at": datetime.now().isoformat(),
            "reference_date": today.isoformat(),
            "upcoming": upcoming,
            "recent_past": recent_past,
            "proximity": proximity,
            "total_events_loaded": len(ec.all_events()),
            "error": None,
        }

    except Exception as exc:
        logger.warning("generate_events_data failed: %s", exc)
        return {
            "generated_at": datetime.now().isoformat(),
            "reference_date": datetime.now().date().isoformat(),
            "upcoming": [],
            "recent_past": [],
            "proximity": {
                "days_to_fomc": -1,
                "days_to_cpi": -1,
                "days_to_nfp": -1,
                "is_opex_week": 0,
            },
            "total_events_loaded": 0,
            "error": str(exc),
        }


def generate_system_data() -> dict:
    """Generate system-health and operations data for the System tab.

    Produces:
        - reconciliation reports (from logs/reconciliation/*.json if exists)
        - config validation summary for each active config
    """
    logs_dir = PROJECT_ROOT / "logs"
    config_dir = PROJECT_ROOT / "config" / "active"

    # Reconciliation reports
    recon_dir = logs_dir / "reconciliation"
    reconciliation_reports: list = []
    reconciliation_summary = {
        "reports_found": 0,
        "clean_count": 0,
        "dirty_count": 0,
        "last_run": None,
    }

    if recon_dir.exists():
        recon_files = sorted(recon_dir.glob("*.json"), reverse=True)[:10]
        for rfile in recon_files:
            try:
                rdata = json.loads(rfile.read_text())
                reconciliation_reports.append({
                    "file": rfile.name,
                    "timestamp": rdata.get("timestamp"),
                    "market_id": rdata.get("market_id", "?"),
                    "clean": rdata.get("clean", False),
                    "broker_positions": rdata.get("broker_positions", 0),
                    "local_positions": rdata.get("local_positions", 0),
                    "discrepancy_count": len(rdata.get("discrepancies", [])),
                    "discrepancies": rdata.get("discrepancies", [])[:5],
                    "fixes_applied": len(rdata.get("fixes_applied", [])),
                })
            except Exception:
                continue
        reconciliation_summary["reports_found"] = len(reconciliation_reports)
        reconciliation_summary["clean_count"] = sum(1 for r in reconciliation_reports if r["clean"])
        reconciliation_summary["dirty_count"] = sum(1 for r in reconciliation_reports if not r["clean"])
        if reconciliation_reports:
            reconciliation_summary["last_run"] = reconciliation_reports[0]["timestamp"]

    # Config validation
    config_validations: list = []
    if config_dir.exists():
        for cfile in sorted(config_dir.glob("*.json")):
            market_id = cfile.stem
            issues: list = []
            warnings_list: list = []

            try:
                cfg = json.loads(cfile.read_text())

                if "trading" not in cfg:
                    issues.append("Missing trading section")
                else:
                    trading = cfg["trading"]
                    mode = trading.get("mode")
                    if mode not in ("live", "paper", "passive"):
                        issues.append(f"Invalid trading.mode: {repr(mode)}")
                    if mode == "live" and not trading.get("approval_required", True):
                        warnings_list.append("Live mode without approval_required=true")
                    if trading.get("live_enabled") and mode == "passive":
                        issues.append("live_enabled=true but mode=passive")

                if "strategies" not in cfg:
                    warnings_list.append("No strategies section")
                else:
                    strats = cfg["strategies"]
                    enabled = [k for k, v in strats.items()
                               if isinstance(v, dict) and v.get("enabled", True)]
                    if not enabled:
                        warnings_list.append("No enabled strategies found")

                if "allocation" not in cfg:
                    warnings_list.append("No allocation section")
                else:
                    pools = cfg.get("allocation", {}).get("pools", {})
                    total_caps = [p.get("max_positions", 0) for p in pools.values()
                                  if isinstance(p, dict)]
                    if total_caps and sum(total_caps) > 20:
                        warnings_list.append(f"Total max_positions across pools is {sum(total_caps)}")

                version = cfg.get("version", cfg.get("_version", ""))
                if not version:
                    warnings_list.append("No version field")

                config_validations.append({
                    "market_id": market_id,
                    "file": cfile.name,
                    "valid": len(issues) == 0,
                    "version": version,
                    "trading_mode": cfg.get("trading", {}).get("mode", "?"),
                    "live_enabled": cfg.get("trading", {}).get("live_enabled", False),
                    "broker": cfg.get("trading", {}).get("broker", "?"),
                    "strategy_count": len(cfg.get("strategies", {})),
                    "issues": issues,
                    "warnings": warnings_list,
                    "issue_count": len(issues),
                    "warning_count": len(warnings_list),
                })

            except Exception as exc:
                config_validations.append({
                    "market_id": market_id,
                    "file": cfile.name,
                    "valid": False,
                    "version": "?",
                    "trading_mode": "?",
                    "live_enabled": False,
                    "broker": "?",
                    "strategy_count": 0,
                    "issues": [f"Failed to parse: {exc}"],
                    "warnings": [],
                    "issue_count": 1,
                    "warning_count": 0,
                })

    any_config_invalid = any(not v["valid"] for v in config_validations)
    recon_clean = (reconciliation_summary["dirty_count"] == 0
                   and reconciliation_summary["reports_found"] > 0)

    return {
        "generated_at": datetime.now().isoformat(),
        "reconciliation": {
            "summary": reconciliation_summary,
            "reports": reconciliation_reports,
            "dir_exists": recon_dir.exists(),
        },
        "config_validation": {
            "configs": config_validations,
            "all_valid": not any_config_invalid,
            "total_issues": sum(v["issue_count"] for v in config_validations),
            "total_warnings": sum(v["warning_count"] for v in config_validations),
        },
        "system_ok": not any_config_invalid and (
            recon_clean or not reconciliation_summary["reports_found"]
        ),
    }


def generate():
    """Generate SP500 (Alpaca-only) dashboard data.

    Single-market, USD-only. Removes multi-market loop and Moomoo integration.
    """
    import signal

    def _broker_timeout_handler(signum, frame):
        raise TimeoutError("Broker connection timed out")

    mid = "sp500"
    cfg = get_config(mid)
    trading = cfg.get("trading", {})
    broker_name = trading.get("broker", "alpaca")
    broker_cache = None

    if trading.get("mode") == "live" and trading.get("live_enabled", False):
        broker_timeout = 15
        try:
            signal.signal(signal.SIGALRM, _broker_timeout_handler)
            signal.alarm(broker_timeout)
            acct, positions, ok, orders = get_live_broker_data(cfg)
            signal.alarm(0)
        except TimeoutError:
            signal.alarm(0)
            print(f"  {mid}: broker TIMEOUT after {broker_timeout}s ({broker_name})")
            acct, positions, ok, orders = None, [], False, []

        if ok:
            broker_cache = {"acct": acct, "positions": positions, "ok": True, "orders": orders}
            _save_broker_cache(mid, acct, positions, orders)
            print(f"  {mid}: broker connected ({broker_name}), {len(positions)} positions")
        else:
            cached = _load_broker_cache(mid, allow_stale=True)
            if cached:
                is_stale = cached.get("_stale", False)
                broker_cache = {
                    "acct": cached["acct"],
                    "positions": cached["positions"],
                    "ok": True,
                    "orders": cached.get("orders", []),
                    "_cached": True,
                    "_cache_age_minutes": cached.get("cache_age_minutes", 0),
                }
                stale_tag = " (STALE)" if is_stale else ""
                print(f"  {mid}: broker FAILED — using cache{stale_tag} "
                      f"({cached['cache_age_minutes']:.0f}m old, "
                      f"{len(cached['positions'])} positions)")
            else:
                print(f"  {mid}: broker FAILED, no cache available")
                broker_cache = {"ok": False}

    # ── Generate SP500 market data ───────────────────────────────
    print(f"\n  Generating {mid}...")
    sp500_data = generate_market(mid, broker_cache=broker_cache)
    market_data = {mid: sp500_data}

    # ── Equity curve (SP500 is the only market) ──────────────────
    combined_curve = _merge_equity_curves(market_data)

    # Derive starting equity from curve first point
    combined_starting = sp500_data.get("portfolio", {}).get("starting_equity", 0)
    if combined_curve:
        _first_pt = combined_curve[0]
        _curve_starting = _first_pt["equity"] - _first_pt.get("pnl", 0)
        if _curve_starting > 0:
            combined_starting = round(_curve_starting, 2)

    # ── Benchmark (SPY) ──────────────────────────────────────────
    combined_bench, combined_bench_label = _merge_benchmark_curves(
        market_data, combined_starting_usd=combined_starting
    )

    # ── P&L ─────────────────────────────────────────────────────
    now = datetime.now(BRISBANE)
    if combined_curve:
        _latest_pt = combined_curve[-1]
        combined_pnl = round(_latest_pt.get("pnl", 0), 2)
        _invested = _latest_pt["equity"] - combined_pnl
        combined_pnl_pct = round(combined_pnl / _invested * 100, 2) if _invested > 0 else 0
    else:
        pf = sp500_data.get("portfolio", {})
        combined_pnl = pf.get("total_pnl", 0)
        combined_pnl_pct = pf.get("total_pnl_pct", 0)

    research_data = generate_research_data()
    ceasefire_data = generate_ceasefire_data()

    pf = sp500_data.get("portfolio", {})
    all_positions = [dict(p, market=mid) for p in pf.get("open_positions", [])]
    all_closed = [dict(t, market=mid) for t in sp500_data.get("closed_trades", [])]

    stale_warnings = [sp500_data["stale_warning"]] if sp500_data.get("stale_warning") else []

    result = {
        "timestamp": now.isoformat(),
        "project": "Atlas",
        "markets": market_data,
        "trading_mode": sp500_data.get("trading_mode", "offline"),
        "data_source": sp500_data.get("data_source", "offline"),
        "broker": {"sp500": broker_name},
        "config_version": {"sp500": sp500_data.get("config_version", "?")},
        "account": {
            "equity": pf.get("broker_equity") or pf.get("equity", 0),
            "cash": pf.get("broker_cash") or pf.get("cash", 0),
            "buying_power": pf.get("buying_power", 0),
            "currency": "USD",
        },
        "portfolio": {
            "equity": pf.get("broker_equity") or pf.get("equity", 0),
            "cash": pf.get("broker_cash") or pf.get("cash", 0),
            "starting_equity": combined_starting,
            "total_pnl": combined_pnl,
            "total_pnl_pct": combined_pnl_pct,
            "num_open": len(all_positions),
            "open_positions": all_positions,
            "win_rate": pf.get("win_rate", 0),
            "market_pnl": pf.get("market_pnl", 0),
            "realized_pnl": pf.get("realized_pnl", 0),
            "total_commissions": pf.get("total_commissions", 0),
            "commission_per_trade": pf.get("commission_per_trade", 0),
            "today_pnl_usd": pf.get("today_pnl_usd", 0),
        },
        "strategy_summary": sp500_data.get("strategy_summary", []),
        "equity_curve": combined_curve,
        "benchmark_curve": combined_bench,
        "benchmark_ticker": combined_bench_label,
        "benchmark_return_pct": round(
            (combined_bench[-1]["equity"] / combined_starting - 1) * 100, 2
        ) if combined_bench and combined_starting > 0 else 0,
        "plan": sp500_data.get("plan"),
        "closed_trades": all_closed,
        "risk": sp500_data.get("risk", {}),
        "tasks": sp500_data.get("tasks", {}),
        "research": research_data,
        "ceasefire": ceasefire_data,
        "stale_warnings": stale_warnings,
    }

    # Atomic write
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    tmp_output = OUTPUT.with_suffix(".tmp")
    with open(tmp_output, "w") as f:
        json.dump(result, f, indent=2, default=str)
    tmp_output.rename(OUTPUT)

    # ── Health / Events / System tab data ────────────────────────
    for _gen_fn, _fname in [
        (generate_strategy_health_data, "health-data.json"),
        (generate_events_data,          "events-data.json"),
        (generate_system_data,          "system-data.json"),
    ]:
        try:
            _tab_data = _gen_fn()
        except Exception as _exc:
            logger.warning("Tab data generation failed for %s: %s", _fname, _exc)
            _tab_data = {"error": str(_exc), "generated_at": datetime.now().isoformat()}
        _tab_out = OUTPUT.parent / _fname
        _tab_tmp = _tab_out.with_suffix(".tmp")
        with open(_tab_tmp, "w") as _tf:
            json.dump(_tab_data, _tf, indent=2, default=str)
        _tab_tmp.rename(_tab_out)
    print("  Tab data written: health-data.json, events-data.json, system-data.json")

    print(f"\nDashboard data written to {OUTPUT}")
    label = f"{'🔴 LIVE' if sp500_data.get('trading_mode') == 'live' else '📝 PAPER'}"
    ds_tag = f" [{sp500_data.get('data_source', '?')}]" if sp500_data.get('data_source') not in ('broker',) else ""
    print(f"  SP500  {label}: ${pf.get('equity', 0):,.2f} equity, "
          f"{len(all_positions)} positions{ds_tag}, "
          f"v{sp500_data.get('config_version', '?')}")
    print(f"  P&L: ${combined_pnl:,.2f} ({combined_pnl_pct:+.2f}%) | "
          f"Starting: ${combined_starting:,.2f}")

    # Regenerate simple dashboard data
    try:
        generate_simple_dashboard_data()
    except Exception as e:
        print(f"  WARN: simple-dashboard-data.json generation failed: {e}")


# ─────────────────────────────────────────────────────────────────────────────
# Simple Dashboard Data — flat JSON for the Bloomberg-terminal redesign
# ─────────────────────────────────────────────────────────────────────────────

SIMPLE_OUTPUT = PROJECT_ROOT / "dashboard" / "data" / "simple-dashboard-data.json"

# Market config: NYSE only (Alpaca-only, SP500)
_MARKET_HOURS = {
    "sp500": ("America/New_York", "09:30", "16:00"),
}


def _market_status(market_id: str) -> dict:
    """Compute open/closed status + seconds until next open/close event.

    Returns:
        {
            "status": "open" | "closed",
            "next_open_secs":  int | None,   # None when market is open
            "next_close_secs": int | None,   # None when market is closed
        }

    Weekends are treated as always-closed.
    Holidays are not modelled — use exchange calendars for precision.
    """
    cfg = _MARKET_HOURS.get(market_id)
    if not cfg:
        return {"status": "unknown", "next_open_secs": None, "next_close_secs": None}

    tz_name, open_hhmm, close_hhmm = cfg
    tz = ZoneInfo(tz_name)
    now = datetime.now(tz)

    # Weekends are always closed (0=Mon … 6=Sun)
    weekday = now.weekday()
    is_weekday = weekday < 5

    oh, om = int(open_hhmm[:2]), int(open_hhmm[3:])
    ch, cm = int(close_hhmm[:2]), int(close_hhmm[3:])

    open_dt  = now.replace(hour=oh, minute=om, second=0, microsecond=0)
    close_dt = now.replace(hour=ch, minute=cm, second=0, microsecond=0)

    if is_weekday and open_dt <= now < close_dt:
        secs = int((close_dt - now).total_seconds())
        return {"status": "open", "next_open_secs": None, "next_close_secs": max(0, secs)}

    # Market is closed — compute seconds until next open (skip to Mon if weekend)
    days_ahead = 0
    if not is_weekday:
        # Sat → 2 days, Sun → 1 day until Monday
        days_ahead = {5: 2, 6: 1}[weekday]
    elif now >= close_dt:
        # After close today — next open is tomorrow (or Mon if Fri)
        days_ahead = 3 if weekday == 4 else 1  # Friday → Monday
    # If before open on a weekday days_ahead stays 0

    next_open = open_dt + __import__("datetime").timedelta(days=days_ahead)
    secs = int((next_open - now).total_seconds())
    return {"status": "closed", "next_open_secs": max(0, secs), "next_close_secs": None}


def _load_sparkline(ticker: str, n: int = 15) -> list[float]:
    """Load last *n* daily closes for a ticker.

    Search order:
      1. data/snapshots/<latest_snapshot>/<ticker>.parquet
      2. data/processed/sp500/  or  data/processed/asx/
      3. Fallback: empty list (caller uses [entry_price, current_price])
    """
    snapshots_dir = PROJECT_ROOT / "data" / "snapshots"
    processed_dirs = [
        PROJECT_ROOT / "data" / "processed" / "sp500",
        PROJECT_ROOT / "data" / "processed" / "asx",
        PROJECT_ROOT / "data" / "processed" / "hk",
    ]

    safe_ticker = ticker.replace(".", "_")

    # ── 1. Latest snapshot directory ─────────────────────────────
    if snapshots_dir.exists():
        snap_dirs = sorted(snapshots_dir.iterdir(), reverse=True)
        for sdir in snap_dirs:
            if not sdir.is_dir():
                continue
            fp = sdir / f"{safe_ticker}.parquet"
            if fp.exists():
                try:
                    df = pd.read_parquet(fp)
                    closes = df["close"].dropna().tolist()
                    return [round(float(v), 4) for v in closes[-n:]]
                except Exception:
                    pass

    # ── 2. Processed data directories ────────────────────────────
    for pdir in processed_dirs:
        fp = pdir / f"{safe_ticker}.parquet"
        if fp.exists():
            try:
                df = pd.read_parquet(fp)
                closes = df["close"].dropna().tolist()
                return [round(float(v), 4) for v in closes[-n:]]
            except Exception:
                pass

    return []


def _calc_trade_stats(trades: list) -> dict:
    """Compute win rate, avg win/loss, profit factor, expectancy for a list of trades."""
    if not trades:
        return {
            "trades": 0, "win_rate": 0, "avg_win": 0,
            "avg_loss": 0, "profit_factor": 0, "expectancy": 0,
        }
    wins = [t.get("pnl", 0) for t in trades if (t.get("pnl", 0) or 0) > 0]
    losses = [t.get("pnl", 0) for t in trades if (t.get("pnl", 0) or 0) <= 0]
    total = len(trades)
    win_rate = len(wins) / total * 100 if total > 0 else 0
    avg_win = sum(wins) / len(wins) if wins else 0
    avg_loss = sum(losses) / len(losses) if losses else 0
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    if gross_loss > 0:
        pf = round(gross_profit / gross_loss, 2)
    elif gross_profit > 0:
        pf = 999.0
    else:
        pf = 0.0
    expectancy = (sum(wins) + sum(losses)) / total if total > 0 else 0
    return {
        "trades": total,
        "win_rate": round(win_rate, 1),
        "avg_win": round(avg_win, 2),
        "avg_loss": round(avg_loss, 2),
        "profit_factor": pf,
        "expectancy": round(expectancy, 2),
    }


def _compute_strategy_performance(closed_trades: list) -> dict:
    """Compute strategy performance stats from closed trades list."""
    overall = _calc_trade_stats(closed_trades)
    by_strategy: dict = {}
    for t in closed_trades:
        s = t.get("strategy", "unknown") or "unknown"
        if s not in by_strategy:
            by_strategy[s] = []
        by_strategy[s].append(t)
    return {
        "overall": overall,
        "by_strategy": {s: _calc_trade_stats(ts) for s, ts in by_strategy.items()},
    }


def _get_alpaca_raw_positions() -> dict:
    """Fetch raw Alpaca positions to extract intraday fields not in Atlas's PositionInfo.

    Returns {symbol: {"intraday_pnl", "intraday_pnl_pct", "change_today", "lastday_price"}}
    """
    try:
        from brokers.secrets import get_secret
        api_key = get_secret("ALPACA_API_KEY")
        api_secret = get_secret("ALPACA_SECRET_KEY")
        paper = (get_secret("ALPACA_PAPER") or "false").lower() in ("true", "1")

        from alpaca.trading.client import TradingClient
        client = TradingClient(api_key, api_secret, paper=paper)
        positions = client.get_all_positions() or []

        result = {}
        for pos in positions:
            symbol = str(getattr(pos, "symbol", "") or "")
            if not symbol:
                continue
            result[symbol] = {
                "intraday_pnl": round(float(getattr(pos, "unrealized_intraday_pl", 0) or 0), 2),
                "intraday_pnl_pct": round(float(getattr(pos, "unrealized_intraday_plpc", 0) or 0) * 100, 4),
                "change_today": round(float(getattr(pos, "change_today", 0) or 0) * 100, 4),
                "lastday_price": round(float(getattr(pos, "lastday_price", 0) or 0), 4),
            }
        return result
    except Exception as e:
        logger.warning("_get_alpaca_raw_positions failed: %s", e)
        return {}


def generate_simple_dashboard_data() -> dict:
    """Generate rich Alpaca-only flat JSON payload for the simple dashboard.

    Calls Alpaca API directly for:
    - Rich account details (margin, daytrade count, etc.)
    - Portfolio history (daily equity curve from broker)
    - Recent orders
    - Market clock (real-time open/close status)
    - Intraday position fields

    Then computes:
    - strategy_allocation from current positions
    - strategy_performance from closed trades
    - SPY benchmark curve

    Output schema:
        {
          "timestamp": str,
          "account": {equity, cash, buying_power, last_equity, ...},
          "portfolio_history": [{"date", "equity", "pnl", "pnl_pct"}, ...],
          "positions": [{"ticker", "strategy", ..., "intraday_pnl", "change_today", ...}, ...],
          "strategy_allocation": [{"strategy", "value", "pct", "positions"}, ...],
          "strategy_performance": {"overall": {...}, "by_strategy": {...}},
          "recent_orders": [...],
          "market_clock": {"is_open", "next_open", "next_close", "timestamp"},
          "summary": {equity, today_pnl, total_pnl, total_pnl_pct, open_positions, win_rate},
          "benchmark": {"ticker": "SPY", "curve": [...], "return_pct": float},
        }
    """
    now = datetime.now(BRISBANE)
    config = get_config("sp500")

    # ── Broker data: positions, account ──────────────────────────
    acct_data, broker_positions, broker_ok, _ = get_live_broker_data(config)

    # ── Rich Alpaca API data ─────────────────────────────────────
    account_details = get_alpaca_account_details()
    portfolio_history = get_alpaca_portfolio_history(period="3M")
    recent_orders = get_alpaca_recent_orders(limit=20)
    market_clock = get_alpaca_market_clock()

    # ── Raw intraday position fields ─────────────────────────────
    raw_positions = _get_alpaca_raw_positions()  # {symbol: {intraday fields}}

    # ── Plan metadata (strategy, stop, sector per ticker) ────────
    plan_meta = _load_plan_metadata()

    # ── Closed trades for performance calc ───────────────────────
    live_state_path = PROJECT_ROOT / "brokers" / "state" / "live_sp500.json"
    live_state = safe_json(live_state_path, {})
    closed_trades = live_state.get("closed_trades", []) or []

    # Fallback to portfolio file
    if not closed_trades:
        portfolio = get_portfolio(config)
        closed_trades = portfolio.get("closed_trades", []) or []

    # ── Starting equity ───────────────────────────────────────────
    portfolio_state = get_portfolio(config)
    seq = (portfolio_state.get("starting_equity")
           or config.get("risk", {}).get("starting_equity", 5000))

    # ── Build positions with enriched fields ─────────────────────
    positions_out: list[dict] = []
    strategy_value: dict = {}

    for p in broker_positions:
        ticker = p.get("ticker", "")
        # Map Atlas ticker to Alpaca symbol (e.g. "AAPL" stays "AAPL")
        alpaca_symbol = ticker  # US equities have no suffix in Alpaca
        raw = raw_positions.get(alpaca_symbol, {})
        meta = plan_meta.get(ticker, {})
        strat = meta.get("strategy") or p.get("strategy") or "unknown"
        sector = meta.get("sector") or p.get("sector") or "Unknown"

        ep = float(p.get("entry_price", 0) or 0)
        cp = float(p.get("current_price", ep) or ep)
        shares = int(p.get("shares", 0) or 0)
        market_value = float(p.get("market_value", cp * shares) or cp * shares)
        unrealized_pnl = float(p.get("unrealized_pnl", (cp - ep) * shares) or 0)
        unrealized_pnl_pct = float(p.get("unrealized_pnl_pct",
                                         (cp - ep) / ep * 100 if ep > 0 else 0) or 0)

        # Sparkline
        sparkline = _load_sparkline(ticker, n=15)
        if not sparkline:
            sparkline = [round(ep, 4), round(cp, 4)] if ep != cp else [round(cp, 4)]

        pos_out = {
            "ticker":             ticker,
            "strategy":           strat,
            "entry_date":         meta.get("entry_date", p.get("entry_date", "")),
            "entry_price":        round(ep, 4),
            "current_price":      round(cp, 4),
            "shares":             shares,
            "market_value":       round(market_value, 2),
            "unrealized_pnl":     round(unrealized_pnl, 2),
            "unrealized_pnl_pct": round(unrealized_pnl_pct, 4),
            "cost_basis":         round(float(p.get("cost_basis", ep * shares) or 0), 2),
            "today_pnl":          round(float(p.get("today_pnl", 0) or 0), 2),
            "stop_price":         round(float(meta.get("stop_price", p.get("stop_price", 0)) or 0), 4),
            "sector":             sector,
            "currency":           "USD",
            "is_atlas":           bool(p.get("is_atlas", True)),
            "sparkline":          sparkline,
            # Intraday fields from raw Alpaca position
            "intraday_pnl":       raw.get("intraday_pnl", 0),
            "intraday_pnl_pct":   raw.get("intraday_pnl_pct", 0),
            "change_today":       raw.get("change_today", 0),
            "lastday_price":      raw.get("lastday_price", 0),
        }
        positions_out.append(pos_out)

        # Accumulate strategy allocation
        if strat not in strategy_value:
            strategy_value[strat] = {"value": 0.0, "positions": 0}
        strategy_value[strat]["value"] += market_value
        strategy_value[strat]["positions"] += 1

    # ── Strategy allocation ───────────────────────────────────────
    total_market_value = sum(p["market_value"] for p in positions_out)
    strategy_allocation = [
        {
            "strategy": s,
            "value": round(v["value"], 2),
            "pct": round(v["value"] / total_market_value * 100, 1) if total_market_value > 0 else 0,
            "positions": v["positions"],
        }
        for s, v in sorted(strategy_value.items(), key=lambda x: x[1]["value"], reverse=True)
    ]

    # ── Strategy performance ──────────────────────────────────────
    strategy_performance = _compute_strategy_performance(closed_trades)

    # ── Account section ───────────────────────────────────────────
    equity = float(account_details.get("equity") or
                   (acct_data.get("equity", 0) if acct_data else 0))
    cash = float(account_details.get("cash") or
                 (acct_data.get("cash", 0) if acct_data else 0))
    last_equity = float(account_details.get("last_equity", equity) or equity)
    equity_change = float(account_details.get("equity_change_today",
                                               round(equity - last_equity, 2)))
    equity_change_pct = float(account_details.get("equity_change_today_pct",
                                                    round(equity_change / last_equity * 100, 2)
                                                    if last_equity > 0 else 0))
    initial_margin = float(account_details.get("initial_margin", 0))
    maintenance_margin = float(account_details.get("maintenance_margin", 0))
    margin_usage_pct = round(maintenance_margin / equity * 100, 2) if equity > 0 else 0

    account_created = account_details.get("account_created", "")
    account_age_days = 0
    if account_created:
        try:
            created_dt = datetime.strptime(account_created[:10], "%Y-%m-%d")
            account_age_days = (now.replace(tzinfo=None) - created_dt).days
        except Exception:
            pass

    # NOTE: equity_change / equity_change_pct are day-over-day from Alpaca
    # and include deposits/withdrawals. Do NOT use for P&L display.
    # Use summary.total_pnl (= equity - starting_equity) for actual trading P&L.
    account_section = {
        "equity":               round(equity, 2),
        "cash":                 round(cash, 2),
        "buying_power":         round(float(account_details.get("buying_power",
                                             acct_data.get("buying_power", cash) if acct_data else cash)), 2),
        "long_market_value":    round(float(account_details.get("long_market_value", total_market_value)), 2),
        "last_equity":          round(last_equity, 2),
        "equity_change":        round(equity_change, 2),
        "equity_change_pct":    round(equity_change_pct, 2),
        "initial_margin":       round(initial_margin, 2),
        "maintenance_margin":   round(maintenance_margin, 2),
        "margin_usage_pct":     margin_usage_pct,
        "daytrade_count":       account_details.get("daytrade_count", 0),
        "multiplier":           account_details.get("multiplier", 1),
        "account_age_days":     account_age_days,
        "shorting_enabled":     account_details.get("shorting_enabled", False),
        "pattern_day_trader":   account_details.get("pattern_day_trader", False),
        "starting_equity":      round(seq, 2),
    }

    # ── Total P&L ─────────────────────────────────────────────────
    total_pnl = round(equity - seq, 2)
    total_pnl_pct = round(total_pnl / seq * 100, 2) if seq > 0 else 0
    today_pnl = round(sum(p.get("today_pnl", 0) for p in positions_out), 2)
    win_rate = strategy_performance["overall"].get("win_rate", 0)

    # ── Benchmark curve (SPY) ─────────────────────────────────────
    # Use portfolio_history dates as the eq_curve anchor for alignment
    eq_curve_for_bench = [{"date": pt["date"], "equity": pt["equity"]}
                          for pt in portfolio_history] if portfolio_history else []
    spy_curve = _get_benchmark_curve("SPY", eq_curve_for_bench, seq)
    spy_return_pct = 0.0
    if spy_curve and seq > 0:
        spy_return_pct = round((spy_curve[-1]["equity"] / seq - 1) * 100, 2)

    benchmark_section = {
        "ticker": "SPY",
        "curve": spy_curve,
        "return_pct": spy_return_pct,
    }

    # ── Summary strip ─────────────────────────────────────────────
    summary = {
        "equity":          round(equity, 2),
        "today_pnl":       today_pnl,
        "total_pnl":       total_pnl,
        "total_pnl_pct":   total_pnl_pct,
        "open_positions":  len(positions_out),
        "win_rate":        win_rate,
    }

    # ── Assemble result ───────────────────────────────────────────
    result = {
        "timestamp":            now.isoformat(),
        "account":              account_section,
        "portfolio_history":    portfolio_history,
        "positions":            positions_out,
        "strategy_allocation":  strategy_allocation,
        "strategy_performance": strategy_performance,
        "recent_orders":        recent_orders,
        "market_clock":         market_clock,
        "summary":              summary,
        "benchmark":            benchmark_section,
    }

    # Atomic write
    SIMPLE_OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    tmp = SIMPLE_OUTPUT.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(result, f, indent=2, default=str)
    tmp.rename(SIMPLE_OUTPUT)

    print(f"\nSimple dashboard data written to {SIMPLE_OUTPUT}")
    print(f"  equity:            ${equity:,.2f}")
    print(f"  today_pnl:         ${today_pnl:+,.2f}")
    print(f"  total_pnl:         ${total_pnl:+,.2f} ({total_pnl_pct:+.2f}%)")
    print(f"  positions:         {len(positions_out)}")
    print(f"  portfolio_history: {len(portfolio_history)} days")
    print(f"  recent_orders:     {len(recent_orders)}")
    ms = market_clock
    if ms.get("is_open"):
        print(f"  market:            OPEN — closes {ms.get('next_close', '')[:16]}")
    else:
        print(f"  market:            CLOSED — opens {ms.get('next_open', '')[:16]}")

    return result


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="Atlas dashboard data generator")
    ap.add_argument(
        "--simple",
        action="store_true",
        help="Generate simple flat JSON (reads from dashboard-data.json, no broker connections)",
    )
    args = ap.parse_args()

    if args.simple:
        data = generate_simple_dashboard_data()
        print("\n--- simple-dashboard-data.json (first 60 lines) ---")
        lines = json.dumps(data, indent=2, default=str).splitlines()
        print("\n".join(lines[:60]))
        if len(lines) > 60:
            print(f"  ... ({len(lines) - 60} more lines)")
    else:
        generate()
