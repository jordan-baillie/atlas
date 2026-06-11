"""Dashboard section builders for /api/dashboard-data.

Each builder constructs ONE coherent section of the dashboard payload.
Orchestrated by _build_dashboard_data() in dashboard.py.

Public builders:
  fetch_broker_state      -- parallel 8-RPC broker fetch
  build_account_section   -- equity, margin, total PnL
  build_positions_section -- per-position enrichment (metadata + intraday + stop)
  build_orders_section    -- flatten/normalize order records
  build_equity_curve_section -- portfolio_history from Alpaca or SQLite fallback
  build_strategy_stats    -- strategy_performance, strategy_allocation, SPY benchmark
  build_pnl_summary       -- today_pnl, return_pct (mutates positions + ph in-place)

Private helpers (also re-exported by dashboard.py for backward-compat):
  _calc_alpaca_intraday_pnl
  _calc_tiingo_daily_pnl
  _get_portfolio_history
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from atlas.kernel.paths import PROJECT_ROOT

logger = logging.getLogger("chat_server.dashboard")

_PROJECT_ROOT = PROJECT_ROOT


# ── PnL helpers ───────────────────────────────────────────────────────────────

def _calc_alpaca_intraday_pnl(positions: list) -> dict:
    """Sum intraday PnL from positions already enriched with Alpaca data.

    Returns a dict compatible with _calc_tiingo_daily_pnl for easy substitution.
    """
    per: dict = {}
    total = 0.0
    for p in positions:
        tk = p.get("ticker", "")
        if not tk:
            continue
        ipnl = float(p.get("intraday_pnl", 0) or 0)
        per[tk] = {
            "intraday_pnl": ipnl,
            "intraday_pnl_pct": p.get("intraday_pnl_pct", 0),
            "lastday_price": p.get("lastday_price", 0),
            "current_price": p.get("current_price", 0),
            # Compat fields so callers using today_close/yesterday_close still work
            "today_close": float(p.get("current_price", 0) or 0),
            "yesterday_close": float(p.get("lastday_price", 0) or 0),
            "daily_pnl": ipnl,
            "shares": float(p.get("qty", p.get("shares", 0)) or 0),
        }
        total += ipnl
    return {"per_position": per, "total_pnl": round(total, 2)}


def _calc_tiingo_daily_pnl(positions: list, market_id: str = "sp500") -> dict:
    """Calculate per-position daily PnL from Tiingo cached parquet data.

    Returns dict with:
      - per_position: {ticker: {yesterday_close, today_close, shares, daily_pnl}}
      - total_pnl: float
    """
    import pandas as pd

    cache_dir = _PROJECT_ROOT / "data" / "cache" / market_id
    result: dict = {"per_position": {}, "total_pnl": 0.0}

    for p in positions:
        ticker = p.get("ticker", "")
        shares = float(p.get("qty", p.get("shares", 0)) or 0)
        if not ticker or shares == 0:
            continue

        parquet_path = cache_dir / f"{ticker}.parquet"
        if not parquet_path.exists():
            continue

        try:
            df = pd.read_parquet(parquet_path)
            if len(df) < 2:
                continue
            today_close = float(df["close"].iloc[-1])
            yesterday_close = float(df["close"].iloc[-2])
            daily_pnl = round(shares * (today_close - yesterday_close), 2)

            result["per_position"][ticker] = {
                "yesterday_close": yesterday_close,
                "today_close": today_close,
                "shares": shares,
                "daily_pnl": daily_pnl,
            }
            result["total_pnl"] += daily_pnl
        except (ValueError, TypeError, KeyError, IndexError) as e:
            logger.debug("per-position PnL calc failed for %s: %s", ticker, e)
            continue

    result["total_pnl"] = round(result["total_pnl"], 2)
    return result


# ── Portfolio history helper ──────────────────────────────────────────────────

def _get_portfolio_history(broker) -> list:
    """Fetch Alpaca portfolio history (1 year, daily) and normalize to remove
    cash-flow events (deposits/withdrawals).

    Normalization formula (per-date):
        normalized_equity[i] = raw_equity[i] - cum_deposits_at_date[i] + total_deposits_ever

    Returns [] on any error so the caller can fall back to the SQLite
    equity_curve SUM aggregate.
    """
    try:
        from alpaca.trading.requests import GetPortfolioHistoryRequest
        from datetime import datetime as _dt_ph, timedelta as _td_ph, timezone as _tz_ph

        # ── 1. Portfolio history ──────────────────────────────────────────────
        _ph_req = GetPortfolioHistoryRequest(period="1A", timeframe="1D")
        _ph_resp = broker._broker_call(
            broker._trade_client.get_portfolio_history, _ph_req
        )
        _ts_list = list(getattr(_ph_resp, "timestamp", []) or [])
        _eq_list = list(getattr(_ph_resp, "equity", []) or [])
        _pl_list = list(getattr(_ph_resp, "profit_loss", []) or [])

        raw_rows: list = []
        for _i, (_ts, _eq) in enumerate(zip(_ts_list, _eq_list)):
            if _eq is None or _eq <= 0:
                continue
            _date_str = _dt_ph.fromtimestamp(int(_ts), tz=_tz_ph.utc).strftime("%Y-%m-%d")
            _day_pnl = (
                float(_pl_list[_i])
                if _i < len(_pl_list) and _pl_list[_i] is not None
                else 0.0
            )
            raw_rows.append({
                "date": _date_str,
                "raw_equity": round(float(_eq), 2),
                "day_pnl": round(_day_pnl, 2),
            })

        if not raw_rows:
            return []

        # ── 2. Account activities (cash flows) ───────────────────────────────
        cash_flow_by_date: dict = {}
        try:
            from alpaca.broker.requests import GetAccountActivitiesRequest
            from alpaca.trading.enums import ActivityType

            _act_types = [ActivityType.CSD, ActivityType.CSW, ActivityType.JNLC]
            _since = _dt_ph.now(tz=_tz_ph.utc) - _td_ph(days=400)
            _act_req = GetAccountActivitiesRequest(
                activity_types=_act_types,
                after=_since,
            )

            def _do_fetch_activities(req):
                fields = req.to_request_fields()
                return broker._trade_client.get("/account/activities", fields) or []

            _activities = broker._broker_call(_do_fetch_activities, _act_req) or []

            for _act in _activities:
                if isinstance(_act, dict):
                    _adate = _act.get("date")
                    _net = _act.get("net_amount")
                else:
                    _adate = getattr(_act, "date", None)
                    _net = getattr(_act, "net_amount", None)
                if _adate is None or _net is None:
                    continue
                if hasattr(_adate, "strftime"):
                    _adate_str = _adate.strftime("%Y-%m-%d")
                else:
                    _adate_str = str(_adate)[:10]
                try:
                    _net_f = float(_net)
                except (TypeError, ValueError):
                    continue
                cash_flow_by_date[_adate_str] = (
                    cash_flow_by_date.get(_adate_str, 0.0) + _net_f
                )
        except Exception as _act_err:  # noqa: BLE001
            logger.warning(
                "Alpaca account_activities fetch failed; equity curve will not "
                "be normalized for cash flows: %s",
                _act_err,
            )
            cash_flow_by_date = {}

        # ── 3. Cumulative deposits up to and including each curve date ────────
        if cash_flow_by_date:
            sorted_cf_dates = sorted(cash_flow_by_date.keys())
            cum_dep_per_row: list = []
            running = 0.0
            cf_idx = 0
            for r in raw_rows:
                while (
                    cf_idx < len(sorted_cf_dates)
                    and sorted_cf_dates[cf_idx] <= r["date"]
                ):
                    running += cash_flow_by_date[sorted_cf_dates[cf_idx]]
                    cf_idx += 1
                cum_dep_per_row.append(running)
            total_deposits_ever = running
            while cf_idx < len(sorted_cf_dates):
                total_deposits_ever += cash_flow_by_date[sorted_cf_dates[cf_idx]]
                cf_idx += 1
        else:
            cum_dep_per_row = [0.0] * len(raw_rows)
            total_deposits_ever = 0.0

        # ── 4. Emit normalized rows ───────────────────────────────────────────
        rows: list = []
        for i, r in enumerate(raw_rows):
            adjustment = total_deposits_ever - cum_dep_per_row[i]
            normalized_eq = round(r["raw_equity"] + adjustment, 2)
            rows.append({
                "date": r["date"],
                "equity": normalized_eq,
                "value": normalized_eq,
                "day_pnl": r["day_pnl"],
                "raw_equity": r["raw_equity"],
            })

        # ── 5. Diagnostic log ─────────────────────────────────────────────────
        if rows:
            raw_min = min(r["raw_equity"] for r in rows)
            raw_max = max(r["raw_equity"] for r in rows)
            norm_min = min(r["equity"] for r in rows)
            norm_max = max(r["equity"] for r in rows)
            logger.info(
                "Equity curve: %d days, %d cash-flow events, total_deposits=$%.2f, "
                "raw_range=[$%.2f, $%.2f] (spread $%.2f), "
                "normalized_range=[$%.2f, $%.2f] (spread $%.2f)",
                len(rows),
                len(cash_flow_by_date),
                total_deposits_ever,
                raw_min, raw_max, raw_max - raw_min,
                norm_min, norm_max, norm_max - norm_min,
            )
        return rows
    except Exception as _ph_err:  # noqa: BLE001
        logger.warning(
            "Alpaca portfolio_history fetch failed, will use SQLite fallback: %s",
            _ph_err,
        )
        return []


# ── Broker state fetch ────────────────────────────────────────────────────────

def fetch_broker_state(broker, portfolio_history_fn=None) -> dict:
    """Parallel fetch of all 8 broker RPCs.

    Args:
        broker: connected AlpacaBroker (connect() already called)
        portfolio_history_fn: callable(broker) -> list; defaults to
            _get_portfolio_history.  Pass dashboard.py's module attribute
            so that patch.object(dash_mod, '_get_portfolio_history', ...)
            in tests is honoured.

    Returns dict with keys:
        account_info, positions_info, orders_info  -- required (raises on failure)
        raw_acct, raw_positions, open_orders, clock -- optional (None/[] on failure)
        portfolio_history_raw                       -- always a list
    """
    from concurrent.futures import ThreadPoolExecutor

    if portfolio_history_fn is None:
        portfolio_history_fn = _get_portfolio_history

    with ThreadPoolExecutor(max_workers=8) as _exec:
        _f_account = _exec.submit(broker.get_account_info)
        _f_positions = _exec.submit(broker.get_positions)
        _f_orders = _exec.submit(broker.get_history_orders, 7)
        _f_raw_acct = _exec.submit(
            broker._broker_call, broker._trade_client.get_account
        )
        _f_raw_pos = _exec.submit(
            broker._broker_call, broker._trade_client.get_all_positions
        )
        _f_open = _exec.submit(broker.get_open_orders)
        _f_clock = _exec.submit(
            broker._broker_call, broker._trade_client.get_clock
        )
        _f_phist = _exec.submit(portfolio_history_fn, broker)

        # Required — propagate failure to caller
        account_info = _f_account.result()
        positions_info = _f_positions.result()
        orders_info = _f_orders.result()

        # Optional — individual fallbacks
        try:
            raw_acct = _f_raw_acct.result()
        except Exception as _e:  # noqa: BLE001
            raw_acct = None
            logger.debug("raw account fetch failed: %s", _e)

        try:
            raw_positions = _f_raw_pos.result()
        except Exception as _e:  # noqa: BLE001
            raw_positions = None
            logger.debug("raw positions fetch failed: %s", _e)

        try:
            open_orders = _f_open.result()
        except Exception as _e:  # noqa: BLE001
            open_orders = []
            logger.debug("open orders fetch failed: %s", _e)

        try:
            clock = _f_clock.result()
        except Exception as _e:  # noqa: BLE001
            clock = None
            logger.debug("clock fetch failed: %s", _e)

        portfolio_history_raw = _f_phist.result()  # always returns list

    return {
        "account_info": account_info,
        "positions_info": positions_info,
        "orders_info": orders_info,
        "raw_acct": raw_acct,
        "raw_positions": raw_positions,
        "open_orders": open_orders,
        "clock": clock,
        "portfolio_history_raw": portfolio_history_raw,
    }


# ── Account section ───────────────────────────────────────────────────────────

def build_account_section(
    account_info,
    raw_acct,
    positions: list,
    config_dir: Path,
    open_orders: list | None = None,
) -> dict:
    """Build the 'account' subsection: equity, margin_usage_pct, total_pnl, etc.

    Args:
        account_info: AccountInfo dataclass from broker.get_account_info()
        raw_acct:     raw Alpaca account object (for margin) or None
        positions:    list of position dicts (already asdict'd) — used for count
        config_dir:   Path to config/active/ directory for starting_equity aggregation

    Returns the account dict ready to store in result["account"].
    """
    import dataclasses
    import glob as _glob

    account = dataclasses.asdict(account_info)

    # 1a. Margin usage from raw Alpaca account
    try:
        initial_margin = float(getattr(raw_acct, "initial_margin", 0) or 0)
        equity_val = float(getattr(raw_acct, "equity", 0) or 0)
        account["margin_usage_pct"] = (
            round(initial_margin / equity_val * 100, 2) if equity_val > 0 else 0
        )
    except Exception as e:  # noqa: BLE001
        logger.debug("Margin usage calculation failed: %s", e)
        account["margin_usage_pct"] = 0

    # AccountInfo.num_positions is never set by the Alpaca adapter — use count
    account["num_positions"] = len(positions)

    # Open (pending) orders — explains margin reserved with zero positions (e.g. 50 market
    # orders queued after-hours by the forward-paper run reserve initial_margin until fill).
    account["open_orders"] = len(open_orders or [])

    # Bug-1 fix: aggregate starting_equity across ALL enabled markets
    _total_starting = 0.0
    for _f in sorted(_glob.glob(str(config_dir / "*.json"))):
        try:
            with open(_f) as _fh:
                _cfg = json.load(_fh)
            _se = float(_cfg.get("risk", {}).get("starting_equity", 0) or 0)
            if _se > 0:
                _total_starting += _se
        except (OSError, json.JSONDecodeError, ValueError) as _e:
            logger.debug("Could not read starting_equity from %s: %s", _f, _e)
            continue

    _equity = float(account.get("equity", 0) or 0)
    if _total_starting > 0:
        account["total_pnl"] = round(_equity - _total_starting, 2)
        account["total_pnl_pct"] = round(
            (_equity - _total_starting) / _total_starting * 100, 2
        )
    account["starting_equity_total"] = round(_total_starting, 2)

    return account


# ── Positions section ─────────────────────────────────────────────────────────

def build_positions_section(
    positions_info: list,
    raw_positions,
    open_orders: list,
) -> list:
    """Per-position enrichment: Atlas trade metadata + intraday PnL + broker stop override.

    Three-pass pattern (preserves original ordering):
      Pass 1 (1c): trade metadata from SQLite (strategy, entry_date, stop_price)
      Pass 2 (1b): Alpaca intraday enrichment from raw_positions
      Pass 3 (1c): stop_price override from broker's open orders

    Returns list of position dicts ready to store in result["positions"].
    """
    import dataclasses
    from atlas.db import get_db

    positions = [dataclasses.asdict(p) for p in positions_info]

    # Strategies that are placeholder / uninformative — always prefer a real one
    _POISON: set = {"reconciled", "unknown", "", None}

    # Pass 1: Atlas trade metadata from SQLite
    try:
        with get_db() as db:
            all_trades = db.execute(
                "SELECT ticker, strategy, entry_date, stop_price, entry_price,"
                "       (CASE WHEN exit_date IS NULL THEN 0 ELSE 1 END) AS is_closed"
                " FROM trades"
                " ORDER BY is_closed, id DESC"
            ).fetchall()

        trade_meta: dict = {}
        for t in all_trades:
            tk = t["ticker"]
            td = dict(t)
            if tk not in trade_meta:
                trade_meta[tk] = td
            elif (
                trade_meta[tk].get("strategy") in _POISON
                and td.get("strategy") not in _POISON
            ):
                # Prefer any real strategy over a placeholder entry
                trade_meta[tk] = td

        for p in positions:
            meta = trade_meta.get(p.get("ticker", ""))
            if meta:
                if not p.get("strategy"):
                    p["strategy"] = meta.get("strategy", "")
                if not p.get("entry_date"):
                    p["entry_date"] = meta.get("entry_date", "")
                if not p.get("stop_price") and meta.get("stop_price"):
                    p["stop_price"] = meta["stop_price"]
    except Exception as _e:  # noqa: BLE001
        logger.warning("Trade metadata enrichment failed: %s", _e)

    # Pass 2: Alpaca intraday enrichment (unrealized_intraday_pl etc.)
    try:
        alpaca_by_symbol: dict = {}
        for rp in raw_positions or []:
            sym = str(getattr(rp, "symbol", ""))
            alpaca_by_symbol[sym] = rp

        from atlas.brokers.alpaca import mapper
        for p in positions:
            atlas_ticker = p.get("ticker", "")
            alpaca_sym = mapper.to_alpaca(atlas_ticker)
            rp = alpaca_by_symbol.get(alpaca_sym)
            if rp:
                p["intraday_pnl"] = round(
                    float(getattr(rp, "unrealized_intraday_pl", 0) or 0), 2
                )
                p["intraday_pnl_pct"] = round(
                    float(getattr(rp, "unrealized_intraday_plpc", 0) or 0) * 100,
                    4,
                )
                p["lastday_price"] = round(
                    float(getattr(rp, "lastday_price", 0) or 0), 4
                )
    except Exception as e:  # noqa: BLE001
        logger.warning("Intraday enrichment failed: %s", e)

    # Pass 3: Override stop_price with broker's authoritative open-order value
    try:
        _stop_map: dict[str, list[float]] = {}
        for od in open_orders:
            od_d = od.asdict() if hasattr(od, "asdict") else vars(od)
            raw = dict(od_d.pop("raw", None) or {})
            od_d.update(raw)
            _side = str(od_d.get("side", "")).lower()
            _otype = str(od_d.get("order_type", od_d.get("type", ""))).lower()
            _sym = od_d.get("symbol", od_d.get("ticker", ""))
            _sp = od_d.get("stop_price") or od_d.get("stop_loss")
            if _side == "sell" and _otype in ("stop", "trailing_stop") and _sp:
                try:
                    _stop_map.setdefault(_sym, []).append(float(_sp))
                except (TypeError, ValueError):
                    pass
        for p in positions:
            tk = p.get("ticker", "")
            if tk in _stop_map:
                broker_stop = max(_stop_map[tk])
                p["stop_price"] = broker_stop
                p["stop_source"] = "broker"
            else:
                p.setdefault("stop_source", "ledger")
    except Exception as _stop_err:  # noqa: BLE001
        logger.warning("Broker stop_price override failed: %s", _stop_err)

    return positions


# ── Orders section ────────────────────────────────────────────────────────────

def build_orders_section(orders_info: list) -> list:
    """Flatten + normalize order records for dashboard compatibility.

    Frontend needs: symbol/type/qty/submitted_at/limit_price/stop_price/
    trail_price/filled_price/side/status
    """
    import dataclasses

    orders = []
    for o in orders_info:
        od = dataclasses.asdict(o)
        raw = od.pop("raw", {})
        od["symbol"] = raw.get("symbol", od.get("ticker", ""))
        od["type"] = raw.get("order_type", "limit")
        od["qty"] = od.get("requested_qty", raw.get("qty", 0))
        od["submitted_at"] = raw.get("submitted_at", "")
        od["limit_price"] = float(raw.get("limit_price", 0) or 0)
        od["stop_price"] = float(raw.get("stop_price", 0) or 0)
        od["trail_price"] = float(raw.get("trail_price", 0) or 0)
        od["filled_price"] = od.get("fill_price", 0)
        od["side"] = raw.get("side", str(od.get("side", "")))
        od["status"] = raw.get("status", str(od.get("status", "")))
        orders.append(od)
    return orders


# ── Equity curve section ──────────────────────────────────────────────────────

# Paper Book reset baseline — the equity curve + return % reflect ONLY the forge era, not the retired swing
# system that previously traded this paper account. Pre-inception (swing-era) Alpaca history is ignored.
PAPER_BOOK_INCEPTION = "2026-06-09"


def build_equity_curve_section(
    portfolio_history_raw: list,
    live_equity: float,
    market_clock: dict,
) -> list:
    """Build portfolio_history list from Alpaca data or SQLite fallback.

    Also appends/updates today's live equity point.

    Args:
        portfolio_history_raw: output of _get_portfolio_history (may be [])
        live_equity:           current account equity (for today's row)
        market_clock:          dict with 'is_open' key (for is_trading_day check)

    Returns final portfolio_history list.
    """
    portfolio_history: list = list(portfolio_history_raw)

    if not portfolio_history:
        # Fallback: SUM across markets per date (better than single market slice)
        try:
            from atlas.db import get_db
            with get_db() as db:
                _eq_rows = db.execute(
                    "SELECT date, SUM(equity) AS equity, SUM(day_pnl) AS day_pnl "
                    "FROM equity_curve GROUP BY date ORDER BY date"
                ).fetchall()
                portfolio_history = [
                    {
                        "date": r["date"],
                        "equity": r["equity"],
                        "value": r["equity"],
                        "day_pnl": r["day_pnl"] or 0,
                    }
                    for r in _eq_rows
                ]
        except Exception as _fb_err:  # noqa: BLE001
            logger.warning("Equity curve SQLite fallback failed: %s", _fb_err)
            portfolio_history = []

    # Append/update today's live equity
    if portfolio_history and live_equity:
        from datetime import datetime as _dt
        today_str = _dt.now().strftime("%Y-%m-%d")
        last_row = portfolio_history[-1]
        if last_row.get("date") == today_str:
            # Today's row exists — update equity, leave day_pnl for build_pnl_summary
            if abs((last_row.get("equity") or 0) - live_equity) > 0.01:
                last_row["equity"] = live_equity
                last_row["value"] = live_equity
        else:
            is_trading_day = (
                market_clock.get("is_open", False) or _dt.now().weekday() < 5
            )
            if is_trading_day:
                portfolio_history.append(
                    {
                        "date": today_str,
                        "equity": live_equity,
                        "value": live_equity,
                        "day_pnl": 0,
                    }
                )
            else:
                last_row["equity"] = live_equity
                last_row["value"] = live_equity

    # Paper Book: ignore the retired swing era — keep only forge-era points (>= inception).
    portfolio_history = [p for p in portfolio_history if str(p.get("date", "")) >= PAPER_BOOK_INCEPTION]
    return portfolio_history


# ── Strategy stats + SPY benchmark ───────────────────────────────────────────

def build_strategy_stats(
    positions: list,
    portfolio_history: list,
) -> dict:
    """Build strategy performance, allocation breakdown, and SPY benchmark.

    Opens one SQLite connection for all DB reads.

    Args:
        positions:         list of position dicts (already enriched)
        portfolio_history: current portfolio_history list (for SPY date alignment)

    Returns dict with keys:
        strategy_performance    -- {by_strategy: {...}, overall: {...}}
        strategy_allocation     -- [{strategy, value, pct, positions}, ...]
        benchmark               -- {ticker, curve, return_pct}  (only if SPY data available)
        _portfolio_history_filled  -- forward-filled portfolio list (if SPY present)
    """
    from atlas.db import get_db

    result: dict = {}

    with get_db() as db:
        # Strategy performance from closed trades (exclude phantoms/errors)
        trades_rows = db.execute(
            "SELECT strategy, pnl, pnl_pct FROM trades"
            " WHERE exit_date IS NOT NULL"
            "   AND (status IS NULL OR status != 'error')"
            "   AND (superseded=0 OR superseded IS NULL)"
            "   AND (exit_reason IS NULL"
            "        OR exit_reason NOT IN ('reconcile_phantom', 'reconcile_fill'))"
        ).fetchall()

        # F-06: synthetic/housekeeping strategies filtered from rollups
        _SKIP_STRATEGIES: frozenset = frozenset({"reconciled", "unknown", ""})
        by_strategy: dict = {}
        for t in trades_rows:
            s = t["strategy"]
            if not s or s in _SKIP_STRATEGIES:
                continue  # exclude synthetic markers (reconciled, unknown, empty, null)
            if s not in by_strategy:
                by_strategy[s] = {"trades": 0, "pnl": 0.0, "wins": 0}
            by_strategy[s]["trades"] += 1
            by_strategy[s]["pnl"] += t["pnl"] or 0
            if (t["pnl"] or 0) > 0:
                by_strategy[s]["wins"] += 1

        result["strategy_performance"] = {"by_strategy": by_strategy}

        # Overall performance metrics
        closed_rows = db.execute(
            "SELECT pnl FROM trades"
            " WHERE exit_date IS NOT NULL"
            "   AND (status IS NULL OR status != 'error')"
            "   AND (superseded=0 OR superseded IS NULL)"
            "   AND (exit_reason IS NULL"
            "        OR exit_reason NOT IN ('reconcile_phantom', 'reconcile_fill'))"
        ).fetchall()

        if closed_rows:
            pnls = [c["pnl"] for c in closed_rows if c["pnl"] is not None]
            wins = [p for p in pnls if p > 0]
            losses = [p for p in pnls if p <= 0]
            loss_sum = sum(losses)
            pf = abs(sum(wins) / loss_sum) if loss_sum != 0 else 99.99
            result["strategy_performance"]["overall"] = {
                "trades": len(pnls),
                "win_rate": len(wins) / len(pnls) if pnls else 0,
                "avg_win": sum(wins) / len(wins) if wins else 0,
                "avg_loss": sum(losses) / len(losses) if losses else 0,
                "profit_factor": min(pf, 99.99),
                "expectancy": sum(pnls) / len(pnls) if pnls else 0,
            }

        # SPY benchmark — aligned to portfolio window
        if portfolio_history:
            port_start_date = portfolio_history[0]["date"]
            port_start_equity = portfolio_history[0]["equity"] or 0
            port_end_date = portfolio_history[-1]["date"]
            spy_rows = db.execute(
                "SELECT date, close FROM ohlcv WHERE ticker = 'SPY' "
                "AND date >= ? AND date <= ? ORDER BY date",
                (port_start_date, port_end_date),
            ).fetchall()
            spy_by_date = {r["date"]: r["close"] for r in spy_rows}

            # Build full trading-day calendar = union of portfolio + SPY dates,
            # then forward-fill any gaps in portfolio equity.
            all_trading_days = sorted(
                set(p["date"] for p in portfolio_history) | set(spy_by_date.keys())
            )
            port_by_date = {p["date"]: p for p in portfolio_history}
            _last_eq: float | None = None
            filled_portfolio: list = []
            for _d in all_trading_days:
                if _d in port_by_date:
                    _row = port_by_date[_d]
                    _last_eq = _row["equity"]
                    filled_portfolio.append({
                        "date": _d,
                        "equity": _last_eq,
                        "value": _last_eq,
                        "day_pnl": _row.get("day_pnl", 0) or 0,
                    })
                elif _last_eq is not None:
                    # Forward-fill equity from previous trading day
                    filled_portfolio.append({
                        "date": _d,
                        "equity": _last_eq,
                        "value": _last_eq,
                        "day_pnl": 0.0,
                    })

            # Store the filled version so the orchestrator can update result
            result["_portfolio_history_filled"] = filled_portfolio

            if spy_rows and port_start_equity > 0:
                spy_start = spy_rows[0]["close"]
                scale = port_start_equity / spy_start if spy_start else 1
                _last_spy: float | None = None
                bench_curve = []
                for _row in filled_portfolio:
                    _d = _row["date"]
                    if _d in spy_by_date:
                        _last_spy = spy_by_date[_d]
                    if _last_spy is not None:
                        _eq = round(_last_spy * scale, 2)
                        bench_curve.append({"date": _d, "equity": _eq, "value": _eq})
                spy_return = (
                    (spy_rows[-1]["close"] / spy_rows[0]["close"]) - 1
                ) * 100
                result["benchmark"] = {
                    "ticker": "SPY",
                    "curve": bench_curve,
                    "return_pct": round(spy_return, 2),
                }

    # Strategy allocation breakdown from positions (no DB needed)
    alloc_map: dict = {}
    total_mv = 0.0
    for p in positions:
        s = p.get("strategy") or "manual"
        if s not in alloc_map:
            alloc_map[s] = {"value": 0.0, "positions": 0}
        mv = p.get("market_value") or 0
        alloc_map[s]["value"] += mv
        alloc_map[s]["positions"] += 1
        total_mv += mv
    result["strategy_allocation"] = [
        {
            "strategy": s,
            "value": round(v["value"], 2),
            "pct": round(v["value"] / total_mv * 100, 1) if total_mv > 0 else 0,
            "positions": v["positions"],
        }
        for s, v in sorted(alloc_map.items(), key=lambda x: -x[1]["value"])
    ]

    return result


# ── PnL summary section ───────────────────────────────────────────────────────

def build_pnl_summary(
    positions: list,
    market_id: str,
    portfolio_history: list,
    config: dict,
) -> dict:
    """Compute today_pnl, return_pct, and max_positions for result["summary"].

    Side effects (by design — avoids extra passes over the same data):
      - Mutates positions in-place: adds intraday_pnl / current_price_tiingo
        for positions where these were not set by the Alpaca enrichment pass.
      - Mutates portfolio_history in-place: backfills today's day_pnl row.

    Args:
        positions:         position dicts (may be mutated in-place)
        market_id:         e.g. "sp500" — passed to _calc_tiingo_daily_pnl
        portfolio_history: list of rows (today's day_pnl backfilled in-place)
        config:            active config dict for max_open_positions

    Returns dict of summary fields to merge into result["summary"].
    """
    from datetime import datetime as _dt

    # Choose PnL source: Alpaca intraday if already enriched, else Tiingo parquet
    if any(p.get("intraday_pnl") is not None for p in positions):
        daily_pnl = _calc_alpaca_intraday_pnl(positions)
    else:
        daily_pnl = _calc_tiingo_daily_pnl(positions, market_id=market_id)

    # Enrich positions with PnL data where still missing
    for p in positions:
        ticker = p.get("ticker", "")
        if ticker in daily_pnl["per_position"]:
            tp = daily_pnl["per_position"][ticker]
            if p.get("intraday_pnl") is None:
                p["intraday_pnl"] = tp["daily_pnl"]
                p["intraday_pnl_pct"] = round(
                    (tp["today_close"] - tp["yesterday_close"])
                    / tp["yesterday_close"] * 100,
                    4,
                ) if tp.get("yesterday_close", 0) != 0 else 0.0
            if tp.get("today_close"):
                p["current_price_tiingo"] = tp["today_close"]

    # Backfill today's day_pnl in portfolio_history (was 0 when section 3 ran)
    today = _dt.now().strftime("%Y-%m-%d")
    for _row in reversed(portfolio_history):
        if _row.get("date") == today:
            _row["day_pnl"] = daily_pnl["total_pnl"]
        else:
            break  # stop once we pass today's row(s)

    summary: dict = {
        "today_pnl": daily_pnl["total_pnl"],
        "today_pnl_detail": daily_pnl["per_position"],
        "max_positions": config.get("risk", {}).get("max_open_positions", 10),
    }

    # Portfolio return_pct from first to last equity curve point.
    # Set 0.0 explicitly for a just-started Paper Book (<2 points) so the UI does NOT fall back to the
    # legacy total_pnl_pct (which is computed vs the retired swing system's tiny allocated starting_equity).
    if len(portfolio_history) >= 2:
        _first_eq = portfolio_history[0].get("equity") or 0
        _last_eq = portfolio_history[-1].get("equity") or 0
        summary["return_pct"] = round((_last_eq / _first_eq - 1) * 100, 2) if _first_eq > 0 else 0.0
    else:
        summary["return_pct"] = 0.0

    return summary
