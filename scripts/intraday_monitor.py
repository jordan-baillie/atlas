#!/usr/bin/env python3
"""Atlas Intraday Position Monitor.

Runs during ASX trading hours to check open positions against live
prices and fire Telegram alerts when:

  🔴 Stop breached     — intraday low hit or passed the stop price
  🟡 Stop proximity    — price within 2% of stop
  🟢 Take-profit hit   — intraday high hit the TP target
  ⚠️  Portfolio DD      — equity drawdown exceeds threshold

Designed to run every 30 minutes via cron (10:00–16:00 AEST, Mon–Fri).
Alert deduplication prevents the same alert from firing twice in one
trading session.

Usage:
    python3 scripts/intraday_monitor.py                  # default: asx
    python3 scripts/intraday_monitor.py --market sp500
    python3 scripts/intraday_monitor.py --dry-run        # print, don't send
"""

import argparse
import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from atlas_bootstrap import PROJECT_ROOT as PROJECT
os.chdir(PROJECT)

BRISBANE = ZoneInfo("Australia/Brisbane")
LOG_DIR = PROJECT / "logs"
LOG_DIR.mkdir(exist_ok=True)
ALERT_STATE_DIR = PROJECT / "logs" / "intraday"
ALERT_STATE_DIR.mkdir(parents=True, exist_ok=True)

from utils.logging_config import setup_logging
log = setup_logging("intraday_monitor", extra_log_file="intraday_monitor")

# ── Thresholds ────────────────────────────────────────────────

STOP_PROXIMITY_PCT = 0.03      # alert when price within 3% of stop
PORTFOLIO_DD_PCT = 0.03        # alert when portfolio DD exceeds 3%

# 2026-04-29: commits e58c8557 + b50636c6 changed equity attribution from
# global broker equity (~$5189) to per-market equity (~$1233 for sp500).
# equity_history entries BEFORE this date used the old denominator and produce
# false-positive drawdown alerts (e.g. "78% drawdown") when compared against
# current per-market equity.  Filter them out when computing the peak.
_ATTRIBUTION_RESET_DATE = "2026-04-29"


# ── Alert dedup ───────────────────────────────────────────────

def _get_market_tz(market_id: str) -> ZoneInfo:
    """Get the operator timezone for a market, falling back to Brisbane."""
    try:
        from markets import get_market
        return get_market(market_id).operator_tz()
    except (ImportError, KeyError):
        return BRISBANE


def _alert_state_path(market_id: str) -> Path:
    """One state file per trading day per market."""
    tz = _get_market_tz(market_id)
    today = datetime.now(tz).strftime("%Y-%m-%d")
    return ALERT_STATE_DIR / f"{market_id}_{today}.json"


def _load_fired(market_id: str) -> dict:
    p = _alert_state_path(market_id)
    if p.exists():
        try:
            return json.loads(p.read_text())
        except (json.JSONDecodeError, UnicodeDecodeError, OSError) as _load_err:
            log.debug("Could not parse alert state file %s: %s", p, _load_err)
            return {}
    return {}


def _save_fired(market_id: str, fired: dict):
    p = _alert_state_path(market_id)
    p.write_text(json.dumps(fired, indent=2, default=str))


def _already_fired(fired: dict, key: str) -> bool:
    return key in fired


def _mark_fired(fired: dict, key: str):
    fired[key] = datetime.now(BRISBANE).isoformat()


# ── Price fetching ────────────────────────────────────────────

def fetch_live_prices(tickers: list[str]) -> dict:
    """Fetch current prices — Alpaca snapshots first, yfinance fallback.

    For SP500 markets: tries Alpaca ``get_snapshot_prices()`` which provides
    real-time data (no 15-min delay).  Falls back to yfinance batch download
    for any tickers not covered by Alpaca (ASX, HK, etc.) or if Alpaca
    is unavailable.

    Returns dict: ticker -> {close, low, high, last_price}
    """
    if not tickers:
        return {}

    log.info(f"Fetching live prices for {len(tickers)} tickers...")
    prices = {}

    # ── Alpaca (primary) ──────────────────────────────────────
    try:
        from brokers.alpaca.market_data import get_snapshot_prices
        alpaca_raw = get_snapshot_prices(tickers)
        for ticker, snap in alpaca_raw.items():
            price = snap.get("price", 0)
            if price > 0:
                prices[ticker] = {
                    "close": price,
                    "last_price": price,
                    "low": snap.get("day_low", price),
                    "high": snap.get("day_high", price),
                }
        if alpaca_raw:
            log.info(f"Alpaca: got {len(alpaca_raw)}/{len(tickers)} prices")
    except Exception as e:  # noqa: BLE001 — Alpaca broker call can raise any SDK exception
        log.debug("Alpaca snapshot fetch failed: %s", e)

    # Determine which tickers still need prices
    missing = [t for t in tickers if t not in prices]
    if not missing:
        return prices

    # ── yfinance fallback for remaining tickers ───────────────
    try:
        import yfinance as yf
        import pandas as pd

        log.info(f"yfinance fallback for {len(missing)} tickers: {missing}")
        data = yf.download(
            missing, period="1d", interval="1d",
            progress=False, threads=True, group_by="ticker",
        )

        if not data.empty:
            for ticker in missing:
                try:
                    row = data if len(missing) == 1 else data[ticker]
                    if row.empty or row.dropna(how="all").empty:
                        continue
                    last = row.iloc[-1]
                    close = float(last.get("Close", 0))
                    low = float(last.get("Low", close))
                    high = float(last.get("High", close))
                    if close > 0:
                        prices[ticker] = {
                            "close": close,
                            "last_price": close,
                            "low": low,
                            "high": high,
                        }
                except (ValueError, TypeError, KeyError, IndexError) as e:  # float/dict parse errors
                    log.debug("  %s: yfinance parse error: %s", ticker, e)
        else:
            log.warning("yfinance returned empty data for fallback tickers")

    except ImportError:
        log.warning("yfinance not installed — no fallback available for missing tickers")
    except (ConnectionError, OSError, RuntimeError) as e:  # network/IO errors in yfinance download
        log.error("yfinance batch download failed: %s", e)

    log.info(f"Got prices for {len(prices)}/{len(tickers)} tickers")
    return prices


# ── Checks ────────────────────────────────────────────────────

def check_positions(portfolio, prices: dict, fired: dict) -> list[dict]:
    """Check all positions for stop/TP/proximity alerts.

    Returns list of alert dicts: {type, severity, ticker, message, ...}
    """
    alerts = []

    for pos in portfolio.positions:
        ticker = pos.ticker
        if ticker not in prices:
            continue

        p = prices[ticker]
        current = p["close"]
        low = p["low"]
        high = p["high"]

        entry = pos.entry_price
        stop = pos.stop_price
        tp = pos.take_profit

        # ── Stop breach (intraday low hit stop) ──────────────
        if low <= stop:
            key = f"stop_breach:{ticker}"
            if not _already_fired(fired, key):
                pnl_pct = (stop - entry) / entry * 100
                alerts.append({
                    "type": "stop_breach",
                    "severity": "🔴",
                    "ticker": ticker,
                    "message": (
                        f"<b>{ticker}</b> STOP BREACHED\n"
                        f"  Low ${low:.2f} ≤ stop ${stop:.2f}\n"
                        f"  Entry ${entry:.2f} → P&L {pnl_pct:+.1f}%\n"
                        f"  Current ${current:.2f}"
                    ),
                })
                _mark_fired(fired, key)
            continue  # don't also fire proximity if stop already breached

        # ── Stop proximity ────────────────────────────────────
        distance_pct = (current - stop) / current if current > 0 else 1.0
        if distance_pct <= STOP_PROXIMITY_PCT:
            key = f"stop_near:{ticker}"
            if not _already_fired(fired, key):
                pnl_pct = (current - entry) / entry * 100
                alerts.append({
                    "type": "stop_near",
                    "severity": "🟡",
                    "ticker": ticker,
                    "message": (
                        f"<b>{ticker}</b> near stop ({distance_pct:.1%} away)\n"
                        f"  Current ${current:.2f}  Stop ${stop:.2f}\n"
                        f"  Entry ${entry:.2f} → P&L {pnl_pct:+.1f}%"
                    ),
                })
                _mark_fired(fired, key)

        # ── Take-profit hit ───────────────────────────────────
        if tp and high >= tp:
            key = f"tp_hit:{ticker}"
            if not _already_fired(fired, key):
                pnl_pct = (tp - entry) / entry * 100
                alerts.append({
                    "type": "tp_hit",
                    "severity": "🟢",
                    "ticker": ticker,
                    "message": (
                        f"<b>{ticker}</b> TAKE-PROFIT HIT\n"
                        f"  High ${high:.2f} ≥ target ${tp:.2f}\n"
                        f"  Entry ${entry:.2f} → P&L {pnl_pct:+.1f}%"
                    ),
                })
                _mark_fired(fired, key)

    return alerts


def check_portfolio_drawdown(portfolio, prices: dict, fired: dict) -> list[dict]:
    """Check portfolio-level drawdown against peak equity.

    Only considers equity_history entries dated >= _ATTRIBUTION_RESET_DATE.
    Earlier entries used global broker equity as the denominator; the
    2026-04-29 attribution refactor switched to per-market equity (~4× smaller).
    Comparing current per-market equity against those old global peaks produces
    false-positive drawdown alerts (e.g. "78% drawdown" when actually up 27%).
    Falls back to starting_equity if no post-reset history exists yet.
    """
    alerts = []

    price_map = {t: p["close"] for t, p in prices.items()}
    equity = portfolio.equity(price_map)
    starting = portfolio.starting_equity

    # Use equity history peak — but only from post-attribution-reset entries.
    # Pre-reset entries reflect global broker equity and are incomparable to
    # per-market equity values produced after the 2026-04-29 refactor.
    peak = starting
    for snap in portfolio.equity_history:
        snap_date = snap.get("date", "")
        if snap_date and snap_date < _ATTRIBUTION_RESET_DATE:
            continue  # skip pre-attribution-fix history
        eq = snap.get("equity", 0)
        if eq > peak:
            peak = eq

    if peak <= 0:
        return alerts

    dd = (peak - equity) / peak

    if dd >= PORTFOLIO_DD_PCT:
        key = f"portfolio_dd:{dd:.0%}"
        if not _already_fired(fired, key):
            alerts.append({
                "type": "portfolio_dd",
                "severity": "⚠️",
                "ticker": "PORTFOLIO",
                "message": (
                    f"<b>Portfolio drawdown {dd:.1%}</b>\n"
                    f"  Equity ${equity:,.2f}  Peak ${peak:,.2f}\n"
                    f"  Cash ${portfolio.cash:,.2f}"
                ),
            })
            _mark_fired(fired, key)

    return alerts



# ── Telegram ──────────────────────────────────────────────────

def send_intraday_alert(alerts: list[dict], market_id: str) -> bool:
    """Format and send a single consolidated Telegram message."""
    from utils.telegram import send_message, _esc

    tz = _get_market_tz(market_id)
    now_dt = datetime.now(tz)
    now_str = now_dt.strftime("%H:%M %Z")

    lines = [f"🔔 <b>Atlas Intraday [{market_id.upper()}]</b>  <i>{now_str}</i>\n"]

    for a in alerts:
        lines.append(f"{a['severity']} {a['message']}")
        lines.append("")

    msg = "\n".join(lines).strip()
    return send_message(msg)


def build_status_line(portfolio, prices: dict, market_id: str) -> str:
    """One-line status for logging (not sent to Telegram)."""
    price_map = {t: p["close"] for t, p in prices.items()}
    equity = portfolio.equity(price_map)
    n = len(portfolio.positions)
    total_pnl = sum(
        (prices.get(p.ticker, {}).get("close", p.entry_price) - p.entry_price) * p.shares
        for p in portfolio.positions
    )
    return f"[{market_id}] equity=${equity:,.2f} positions={n} unrealPnL=${total_pnl:+,.2f}"


# ── Main ──────────────────────────────────────────────────────


def _has_open_paper_trades_for_universe(universe: str) -> bool:
    """Return True if there is at least one open paper trade for *universe*."""
    try:
        from db.atlas_db import get_open_paper_trades
        rows = get_open_paper_trades()
        return any(r.get("universe") == universe for r in rows)
    except Exception as _e:
        log.debug("_has_open_paper_trades_for_universe failed (non-fatal): %s", _e)
        return False


def main():
    parser = argparse.ArgumentParser(description="Atlas Intraday Monitor")
    parser.add_argument("--market", "-m", default="asx", help="Market ID (default: asx)")
    parser.add_argument("--dry-run", action="store_true", help="Print alerts, don't send")
    args = parser.parse_args()

    market_id = args.market
    tz = _get_market_tz(market_id)
    now = datetime.now(tz)
    tz_label = now.strftime("%Z")
    log.info(f"{'='*50}")
    log.info(f"Intraday monitor: {market_id} @ {now.strftime('%Y-%m-%d %H:%M')} {tz_label}")

    # ── Load portfolio from live broker ─────────────────────────
    from utils.config import get_active_config
    from brokers.live_portfolio import LivePortfolio

    config = get_active_config(market_id)

    # Skip markets that aren't live-enabled (avoids ERROR-level Telegram spam)
    # Paper mode is active even without live_enabled (targets Alpaca paper account)
    _trading_cfg = config.get("trading", {})
    _mode_intraday = _trading_cfg.get("mode", "live")
    if not (_trading_cfg.get("live_enabled", False) or _mode_intraday == "paper"):
        log.info("[%s] Market %s has live_enabled=False — skipping monitor", _mode_intraday.upper(), market_id)
        try:
            from monitor.health_writer import heartbeat as _hb
            _hb("intraday_monitor", "skipped", {"market": market_id, "reason": "market_disabled"})
        except Exception:
            pass
        return
    _mode_label_intraday = f"[{_mode_intraday.upper()}]"

    portfolio = LivePortfolio(config, market_id=market_id)
    if not portfolio.connect():
        log.error("Broker connection failed — cannot monitor positions")
        return

    if not portfolio.broker_data_valid:
        log.warning("Broker returned zeroed data (likely offline) — skipping monitor cycle")
        return

    if not portfolio.positions:
        log.info("No open positions — nothing to monitor.")
        # Don't return early if paper trades exist — paper pass still needs to run
        if not _has_open_paper_trades_for_universe(market_id):
            return

    # ── Enrich positions with stop prices from plan/state ──────
    # IBKR doesn't return stop_price — read from today's plan file
    # or the live state file as fallback.
    _plan_stops = {}
    _state_stops = {}
    try:
        plan_path = PROJECT / "plans" / f"plan_{market_id}_{now.strftime('%Y-%m-%d')}.json"
        if plan_path.exists():
            import json as _json
            with open(plan_path) as _f:
                plan = _json.load(_f)
            for e in plan.get("proposed_entries", []):
                t = e.get("ticker", "")
                sp = e.get("stop_price", 0)
                if t and sp:
                    _plan_stops[t] = sp
        state_path = PROJECT / "brokers" / "state" / f"live_{market_id}.json"
        if state_path.exists():
            import json as _json
            with open(state_path) as _f:
                state = _json.load(_f)
            for p in state.get("positions", []):
                t = p.get("ticker", "")
                sp = p.get("stop_price", 0)
                if t and sp:
                    _state_stops[t] = sp
    except (json.JSONDecodeError, OSError, KeyError, AttributeError) as e:  # JSON/file/key errors
        log.warning("Failed to load stop prices from plan/state: %s", e)

    enriched = 0
    for pos in portfolio.positions:
        if pos.stop_price == 0 or pos.stop_price is None:
            sp = _plan_stops.get(pos.ticker) or _state_stops.get(pos.ticker) or 0
            if sp:
                pos.stop_price = sp
                enriched += 1
    if enriched:
        log.info(f"Enriched {enriched} positions with stop prices from plan/state files")

    if portfolio.positions:
        tickers = [p.ticker for p in portfolio.positions]
        log.info(f"Monitoring {len(tickers)} positions: {tickers}")
    else:
        tickers = []

    # ── Load alert state (dedup) ──────────────────────────────
    fired = _load_fired(market_id)

    # ── Fetch prices ──────────────────────────────────────────
    prices = fetch_live_prices(tickers)
    if not prices and tickers:
        log.error("No prices fetched — cannot monitor")
        # Alert about data failure
        key = "price_fetch_failed"
        if not _already_fired(fired, key):
            _mark_fired(fired, key)
            _save_fired(market_id, fired)
            if not args.dry_run:
                from utils.telegram import send_message
                send_message(
                    f"🔌 <b>Atlas Intraday [{market_id.upper()}]</b>\n\n"
                    f"Failed to fetch live prices for {len(tickers)} tickers.\n"
                    f"Position monitoring unavailable this cycle."
                )
        # Don't return if paper trades exist — paper pass still runs
        if not _has_open_paper_trades_for_universe(market_id):
            return

    # ── Run checks ────────────────────────────────────────────
    all_alerts = []
    if prices:
        all_alerts.extend(check_positions(portfolio, prices, fired))
        all_alerts.extend(check_portfolio_drawdown(portfolio, prices, fired))

    # ── Save dedup state ──────────────────────────────────────
    _save_fired(market_id, fired)

    # ── Log status (only when prices available) ─────────────
    if prices:
        status = build_status_line(portfolio, prices, market_id)
        log.info(status)

    # Log per-position distance to stop
    for pos in portfolio.positions:
        if pos.ticker in prices:
            cur = prices[pos.ticker]["close"]
            dist = (cur - pos.stop_price) / cur * 100 if cur > 0 else 0
            pnl = (cur - pos.entry_price) / pos.entry_price * 100
            exch = "🛡" if getattr(pos, "stop_order_id", "") else "  "
            log.info(
                f"  {exch} {pos.ticker:10s} cur=${cur:>7.2f}  stop=${pos.stop_price:>7.2f}  "
                f"dist={dist:>5.1f}%  pnl={pnl:>+5.1f}%"
            )

    # ── Send alerts ───────────────────────────────────────────
    if all_alerts:
        log.info(f"Firing {len(all_alerts)} alert(s)")
        if args.dry_run:
            print(f"\n--- DRY RUN: {len(all_alerts)} alerts ---")
            for a in all_alerts:
                print(f"  {a['severity']} [{a['type']}] {a['ticker']}")
                # Strip HTML for terminal display
                import re
                clean = re.sub(r"<[^>]+>", "", a["message"])
                for line in clean.strip().split("\n"):
                    print(f"    {line.strip()}")
                print()
        else:
            send_intraday_alert(all_alerts, market_id)
    else:
        log.info("All positions within normal range — no alerts")

    log.info("Monitor cycle complete")

    # ── Paper pass: dual-pass routing for PAPER lifecycle strategies ─────
    if _has_open_paper_trades_for_universe(market_id):
        log.info("[PAPER] Open paper trades detected for %s — running paper monitor pass", market_id)
        try:
            paper_config = {**config, "trading": {**config.get("trading", {}), "mode": "paper"}}
            paper_portfolio = LivePortfolio(paper_config, market_id=market_id)
            if paper_portfolio.connect() and paper_portfolio.broker_data_valid and paper_portfolio.positions:
                _paper_key = f"{market_id}_paper"
                paper_fired = _load_fired(_paper_key)
                paper_tickers = [p.ticker for p in paper_portfolio.positions]
                paper_prices = fetch_live_prices(paper_tickers)
                paper_alerts: list[dict] = []
                if paper_prices:
                    paper_alerts.extend(check_positions(paper_portfolio, paper_prices, paper_fired))
                    paper_alerts.extend(check_portfolio_drawdown(paper_portfolio, paper_prices, paper_fired))
                _save_fired(_paper_key, paper_fired)
                if paper_alerts:
                    log.info("[PAPER] %d alert(s) from paper pass", len(paper_alerts))
                    if args.dry_run:
                        print(f"\n--- [PAPER] DRY RUN: {len(paper_alerts)} paper alerts ---")
                        for a in paper_alerts:
                            print(f"  {a['severity']} [paper/{a['type']}] {a['ticker']}")
                    else:
                        from utils.telegram import send_message
                        _tz_now = datetime.now(tz).strftime("%H:%M %Z")
                        _paper_lines = [f"\U0001f535 <b>Atlas Intraday [PAPER/{market_id.upper()}]</b>  <i>{_tz_now}</i>\n"]
                        for _a in paper_alerts:
                            _paper_lines.append(f"{_a['severity']} {_a['message']}")
                            _paper_lines.append("")
                        send_message("\n".join(_paper_lines).strip())
                else:
                    log.info("[PAPER] All paper positions within normal range")
        except Exception as _pp_exc:  # noqa: BLE001 — paper pass is non-fatal
            log.error("[PAPER] Paper monitor pass failed (non-fatal): %s", _pp_exc)
    else:
        log.debug("[PAPER] No open paper trades for %s — skipping paper monitor pass", market_id)


if __name__ == "__main__":
    main()
