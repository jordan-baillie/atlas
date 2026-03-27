"""Alpaca live data poller for SSE streaming to the dashboard.

Runs as a background thread, polls Alpaca REST API every N seconds,
and stores latest snapshots in thread-safe state that SSE handlers read.

Data fetched:
  - Account (equity, cash, buying_power, margin, etc.)
  - Positions (with unrealized P&L, intraday changes)
  - Recent orders (last 20)
  - Market clock (open/close status)

All values in USD. No FX conversion.
"""

import json
import logging
import threading
import time
from datetime import datetime
from pathlib import Path

logger = logging.getLogger("alpaca_stream")

# ── Thread-safe state ────────────────────────────────────────
_lock = threading.Lock()
_state: dict = {
    "account": {},
    "positions": [],
    "orders": [],
    "market_clock": {},
    "timestamp": "",
    "error": None,
    "seq": 0,  # Monotonic sequence number for change detection
}
_running = False
_thread: threading.Thread | None = None

PROJECT_ROOT = Path("/root/atlas")


def _get_alpaca_client():
    """Create an Alpaca TradingClient using stored credentials."""
    import sys
    sys.path.insert(0, str(PROJECT_ROOT))
    from brokers.secrets import get_secret

    api_key = get_secret("ALPACA_API_KEY")
    api_secret = get_secret("ALPACA_SECRET_KEY")
    paper = (get_secret("ALPACA_PAPER") or "false").lower() in ("true", "1")

    if not api_key or not api_secret:
        raise ValueError("Alpaca credentials not found")

    from alpaca.trading.client import TradingClient
    return TradingClient(api_key=api_key, secret_key=api_secret, paper=paper)


def _get_data_client():
    """Create an Alpaca StockHistoricalDataClient for snapshots."""
    import sys
    sys.path.insert(0, str(PROJECT_ROOT))
    from brokers.secrets import get_secret

    api_key = get_secret("ALPACA_API_KEY")
    api_secret = get_secret("ALPACA_SECRET_KEY")

    if not api_key or not api_secret:
        raise ValueError("Alpaca credentials not found")

    from alpaca.data.historical import StockHistoricalDataClient
    return StockHistoricalDataClient(api_key=api_key, secret_key=api_secret)


def _load_plan_metadata() -> dict:
    """Load strategy/stop/sector metadata from latest plan files."""
    meta = {}
    try:
        plans_dir = PROJECT_ROOT / "paper_engine" / "plans"
        if not plans_dir.exists():
            return meta
        plan_files = sorted(plans_dir.glob("plan_sp500_*.json"), reverse=True)
        for pf in plan_files[:5]:
            try:
                plan = json.loads(pf.read_text())
                for t in plan.get("trades", []):
                    ticker = t.get("ticker", "")
                    if ticker and ticker not in meta:
                        meta[ticker] = {
                            "strategy": t.get("strategy", ""),
                            "stop_price": t.get("stop_price", 0),
                            "sector": t.get("sector", ""),
                            "entry_date": t.get("entry_date", ""),
                        }
            except Exception:
                continue
    except Exception:
        pass
    return meta


def _fetch_snapshot(client, data_client) -> dict:
    """Fetch account, positions, orders, and clock from Alpaca REST API."""
    now = datetime.now()

    # Account
    acct = client.get_account()
    equity = float(acct.equity)
    last_equity = float(acct.last_equity)

    # Load starting equity for P&L calculation
    import sys
    sys.path.insert(0, str(PROJECT_ROOT))
    from utils.config import get_active_config
    config = get_active_config("sp500")
    portfolio_path = PROJECT_ROOT / "paper_engine" / "portfolio_sp500.json"
    seq = config.get("risk", {}).get("starting_equity", 5000)
    try:
        portfolio = json.loads(portfolio_path.read_text())
        seq = portfolio.get("starting_equity", seq) or seq
    except Exception:
        pass

    total_pnl = round(equity - seq, 2)
    total_pnl_pct = round(total_pnl / seq * 100, 2) if seq > 0 else 0.0

    account_data = {
        "equity": equity,
        "cash": float(acct.cash),
        "buying_power": float(acct.buying_power),
        "long_market_value": float(acct.long_market_value),
        "last_equity": last_equity,
        "initial_margin": float(acct.initial_margin),
        "maintenance_margin": float(acct.maintenance_margin),
        "margin_usage_pct": round(float(acct.maintenance_margin) / equity * 100, 2) if equity > 0 else 0.0,
        "daytrade_count": int(acct.daytrade_count),
        "starting_equity": round(seq, 2),
        "total_pnl": total_pnl,
        "total_pnl_pct": total_pnl_pct,
    }

    # Positions
    raw_positions = client.get_all_positions()
    plan_meta = _load_plan_metadata()

    # Get snapshots for all position tickers (for intraday data)
    tickers = [p.symbol for p in raw_positions]
    snapshots = {}
    if tickers and data_client:
        try:
            from alpaca.data.requests import StockSnapshotRequest
            snap_req = StockSnapshotRequest(symbol_or_symbols=tickers)
            snapshots = data_client.get_stock_snapshot(snap_req)
        except Exception as e:
            logger.warning("Snapshot fetch failed: %s", e)

    # ── Fetch authoritative prices from Tiingo ─────────────────
    # Alpaca position current_price can be stale/incorrect (observed 8%+ error).
    tiingo_prices: dict[str, float] = {}
    us_tickers = [p.symbol for p in raw_positions if not p.symbol.endswith(".AX")]
    if us_tickers:
        try:
            from data.tiingo import get_tiingo_client
            tiingo = get_tiingo_client()
            if tiingo is not None:
                quotes = tiingo.get_quotes(us_tickers)
                for t, q in quotes.items():
                    price = q.get("price", 0)
                    if price and float(price) > 0:
                        tiingo_prices[t.upper()] = {
                            "price": float(price),
                            "prev_close": float(q.get("prev_close", 0) or 0),
                        }
        except Exception as e:
            logger.warning("Tiingo price fetch failed in alpaca_stream: %s", e)

    positions_data = []
    today_pnl = 0.0
    for p in raw_positions:
        sym = p.symbol
        meta = plan_meta.get(sym, {})
        entry_price = float(p.avg_entry_price)
        shares = int(p.qty)
        cost_basis = float(p.cost_basis)

        # Use Tiingo price if available; fall back to Alpaca
        alpaca_price = float(p.current_price)
        tq = tiingo_prices.get(sym.upper())
        if tq and tq["price"] > 0:
            current_price = tq["price"]
            lastday_price = tq["prev_close"] if tq["prev_close"] > 0 else entry_price
            if abs(current_price - alpaca_price) / alpaca_price > 0.02:
                logger.warning(
                    "alpaca_stream: %s price mismatch — Tiingo=$%.2f Alpaca=$%.2f (using Tiingo)",
                    sym, current_price, alpaca_price,
                )
        else:
            current_price = alpaca_price
            lastday_price = float(p.lastday_price) if hasattr(p, 'lastday_price') and p.lastday_price else 0

        # Recalculate PnL from authoritative price
        market_value = round(current_price * shares, 2)
        unrealized_pnl = round(market_value - cost_basis, 2)
        unrealized_pnl_pct = round(unrealized_pnl / cost_basis * 100, 2) if cost_basis > 0 else 0

        intraday_pnl = round((current_price - lastday_price) * shares, 2) if lastday_price > 0 else 0
        intraday_pnl_pct = round((current_price - lastday_price) / lastday_price * 100, 2) if lastday_price > 0 else 0

        today_pnl += intraday_pnl

        positions_data.append({
            "ticker": sym,
            "strategy": meta.get("strategy", "unknown"),
            "entry_date": meta.get("entry_date", ""),
            "entry_price": round(entry_price, 4),
            "current_price": round(current_price, 4),
            "shares": shares,
            "market_value": market_value,
            "unrealized_pnl": unrealized_pnl,
            "unrealized_pnl_pct": unrealized_pnl_pct,
            "cost_basis": round(cost_basis, 2),
            "today_pnl": round(intraday_pnl, 2),
            "stop_price": round(float(meta.get("stop_price", 0) or 0), 4),
            "sector": meta.get("sector", "Unknown"),
            "currency": "USD",
            "intraday_pnl": round(intraday_pnl, 2),
            "intraday_pnl_pct": round(intraday_pnl_pct, 2),
            "change_today": round(intraday_pnl_pct, 2),
            "lastday_price": round(lastday_price, 2),
        })

    # Sort by |unrealized_pnl| descending
    positions_data.sort(key=lambda x: abs(x["unrealized_pnl"]), reverse=True)

    # ── Recompute account-level values from Tiingo-enriched positions ───
    # Alpaca's account.equity and account.long_market_value use Alpaca prices,
    # which can be stale or incorrect. Recompute using Tiingo market values.
    long_market_value = sum(p["market_value"] for p in positions_data)
    equity = round(account_data["cash"] + long_market_value, 2)
    total_pnl = round(equity - seq, 2)
    total_pnl_pct = round(total_pnl / seq * 100, 2) if seq > 0 else 0.0
    margin_usage_pct = round(account_data["maintenance_margin"] / equity * 100, 2) if equity > 0 else 0.0

    # Update account_data with recomputed values
    account_data.update({
        "equity": equity,
        "long_market_value": round(long_market_value, 2),
        "total_pnl": total_pnl,
        "total_pnl_pct": total_pnl_pct,
        "margin_usage_pct": margin_usage_pct,
    })

    # Orders (last 20)
    from alpaca.trading.requests import GetOrdersRequest
    from alpaca.trading.enums import QueryOrderStatus
    orders_req = GetOrdersRequest(status=QueryOrderStatus.ALL, limit=20)
    raw_orders = client.get_orders(orders_req)
    orders_data = []
    for o in raw_orders:
        filled_price = 0.0
        if o.filled_avg_price:
            filled_price = float(o.filled_avg_price)
        orders_data.append({
            "id": str(o.id),
            "symbol": o.symbol,
            "side": o.side.value,
            "qty": int(float(o.qty)) if o.qty else 0,
            "type": o.order_type.value if hasattr(o, 'order_type') else o.type.value,
            "status": o.status.value,
            "filled_qty": int(float(o.filled_qty)) if o.filled_qty else 0,
            "filled_price": round(filled_price, 2),
            "stop_price": round(float(o.stop_price), 2) if o.stop_price else 0.0,
            "trail_price": round(float(o.trail_price), 2) if o.trail_price else 0.0,
            "trail_percent": round(float(o.trail_percent), 2) if o.trail_percent else 0.0,
            "submitted_at": o.submitted_at.strftime("%Y-%m-%d %H:%M:%S") if o.submitted_at else "",
            "filled_at": o.filled_at.strftime("%Y-%m-%d %H:%M:%S") if o.filled_at else "",
            "limit_price": round(float(o.limit_price), 2) if o.limit_price else 0.0,
        })

    # Market clock
    clock = client.get_clock()
    clock_data = {
        "is_open": clock.is_open,
        "next_open": clock.next_open.isoformat() if clock.next_open else "",
        "next_close": clock.next_close.isoformat() if clock.next_close else "",
        "timestamp": clock.timestamp.isoformat() if clock.timestamp else now.isoformat(),
    }

    # Summary (use recomputed account values)
    summary = {
        "equity": account_data["equity"],
        "today_pnl": round(today_pnl, 2),
        "total_pnl": account_data["total_pnl"],
        "total_pnl_pct": account_data["total_pnl_pct"],
        "open_positions": len(positions_data),
    }

    return {
        "account": account_data,
        "positions": positions_data,
        "orders": orders_data,
        "market_clock": clock_data,
        "summary": summary,
        "timestamp": now.isoformat(),
    }


def _poll_loop(interval_open: int = 10, interval_closed: int = 60):
    """Background polling loop. Faster when market open, slower when closed."""
    global _running, _state

    client = None
    data_client = None

    while _running:
        try:
            if client is None:
                client = _get_alpaca_client()
                data_client = _get_data_client()
                logger.info("Alpaca clients initialized")

            snapshot = _fetch_snapshot(client, data_client)

            with _lock:
                _state["account"] = snapshot["account"]
                _state["positions"] = snapshot["positions"]
                _state["orders"] = snapshot["orders"]
                _state["market_clock"] = snapshot["market_clock"]
                _state["summary"] = snapshot["summary"]
                _state["timestamp"] = snapshot["timestamp"]
                _state["error"] = None
                _state["seq"] += 1

            is_open = snapshot["market_clock"].get("is_open", False)
            interval = interval_open if is_open else interval_closed

        except Exception as e:
            logger.error("Alpaca poll failed: %s", e)
            with _lock:
                _state["error"] = str(e)
                _state["seq"] += 1
            interval = 30  # Back off on error

        time.sleep(interval)


def start(interval_open: int = 10, interval_closed: int = 60):
    """Start the background polling thread."""
    global _running, _thread
    if _running:
        return
    _running = True
    _thread = threading.Thread(
        target=_poll_loop,
        args=(interval_open, interval_closed),
        daemon=True,
        name="alpaca-stream",
    )
    _thread.start()
    logger.info("Alpaca stream poller started (open=%ds, closed=%ds)", interval_open, interval_closed)


def stop():
    """Stop the background polling thread."""
    global _running
    _running = False


def get_state() -> dict:
    """Get the latest snapshot (thread-safe copy)."""
    with _lock:
        return {
            "account": _state["account"].copy() if _state["account"] else {},
            "positions": list(_state["positions"]),
            "orders": list(_state["orders"]),
            "market_clock": _state["market_clock"].copy() if _state["market_clock"] else {},
            "summary": _state.get("summary", {}).copy(),
            "timestamp": _state["timestamp"],
            "error": _state["error"],
            "seq": _state["seq"],
        }


def get_seq() -> int:
    """Get current sequence number without copying state."""
    with _lock:
        return _state["seq"]
