"""Atlas Telegram Notification Module.

Sends alerts to a Telegram chat via the Bot API.  Used by the cron
wrapper to report daily run outcomes (plan summaries, settlement
results, errors).

Credentials are read from ~/.atlas-secrets.json:
    {
        "telegram_bot_token": "...",
        "telegram_chat_id": "..."
    }

Usage (Python):
    from utils.telegram import send_message, send_premarket_summary, send_postclose_summary, send_error

    send_message("Hello from Atlas")
    send_premarket_summary(plan_path="plans/plan_2026-02-25.json")
    send_postclose_summary(market_id="asx")
    send_error("premarket", "Traceback ...")

Usage (CLI — called from bash):
    python3 scripts/telegram_notify.py premarket-ok  [plan_path]
    python3 scripts/telegram_notify.py postclose-ok  [market_id]
    python3 scripts/telegram_notify.py error         <mode> <logfile>
"""

from __future__ import annotations

import json
import logging
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
import time as _time
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SECRETS_PATH = Path.home() / ".atlas-secrets.json"
TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"

# Max Telegram message length (UTF-8).
MAX_MSG_LEN = 4000


# ---------------------------------------------------------------------------
# Credential loading
# ---------------------------------------------------------------------------

def _load_credentials() -> tuple[str, str]:
    """Return (bot_token, chat_id) from secrets file or env vars.

    Priority: env vars > secrets file.
    Raises ValueError if neither source provides both values.
    """
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")

    if not (token and chat_id) and SECRETS_PATH.exists():
        with open(SECRETS_PATH) as f:
            secrets = json.load(f)
        token = token or secrets.get("telegram_bot_token", "")
        chat_id = chat_id or secrets.get("telegram_chat_id", "")

    if not token or not chat_id:
        raise ValueError(
            "Telegram credentials not found. Set TELEGRAM_BOT_TOKEN and "
            "TELEGRAM_CHAT_ID env vars, or add telegram_bot_token / "
            "telegram_chat_id to ~/.atlas-secrets.json"
        )
    return token, chat_id


# ---------------------------------------------------------------------------
# Core send
# ---------------------------------------------------------------------------

def send_message(text: str, parse_mode: str = "HTML", silent: bool = False,
                 reply_markup: dict = None) -> bool:
    """Send a message to the configured Telegram chat.

    Args:
        text: Message body (HTML or plain text).
        parse_mode: 'HTML' or 'MarkdownV2'.
        silent: If True, send without notification sound.
        reply_markup: Optional inline keyboard markup dict
                      e.g. {"inline_keyboard": [[{"text": "OK", "callback_data": "ok"}]]}

    Returns:
        True if sent successfully, False otherwise.
    """
    try:
        token, chat_id = _load_credentials()
    except ValueError as e:
        logger.error("Telegram send failed: %s", e)
        return False

    # Truncate to Telegram limit
    if len(text) > MAX_MSG_LEN:
        text = text[: MAX_MSG_LEN - 20] + "\n\n… (truncated)"

    payload_dict = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": parse_mode,
        "disable_notification": silent,
        "disable_web_page_preview": True,
    }
    if reply_markup:
        payload_dict["reply_markup"] = reply_markup

    payload = json.dumps(payload_dict).encode("utf-8")

    url = TELEGRAM_API.format(token=token)
    req = urllib.request.Request(
        url, data=payload, headers={"Content-Type": "application/json"},
    )

    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = json.loads(resp.read())
            if body.get("ok"):
                logger.info("Telegram message sent (chat_id=%s)", chat_id)
                return True
            logger.warning("Telegram API returned ok=false: %s", body)
            return False
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        logger.error("Telegram HTTP %d: %s", e.code, body)
        return False
    except Exception as e:
        logger.error("Telegram send error: %s", e)
        return False


# ---------------------------------------------------------------------------
# Formatted alerts
# ---------------------------------------------------------------------------

def _esc(text: str) -> str:
    """Escape HTML special characters."""
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _read_dashboard_data() -> Optional[dict]:
    """Read the latest dashboard-data.json (generated by generate_data.py).

    This is the single source of truth for all Telegram messages — it
    already has correct per-market equity, positions, P&L, and broker
    account totals from the most recent dashboard refresh.
    """
    dash_path = PROJECT_ROOT / "dashboard" / "data" / "dashboard-data.json"
    if not dash_path.exists():
        logger.warning("dashboard-data.json not found")
        return None
    try:
        with open(dash_path) as f:
            return json.load(f)
    except Exception as e:
        logger.error("Failed to read dashboard-data.json: %s", e)
        return None


def _read_eod_summary(trade_date: str) -> Optional[dict]:
    """Read EOD settlement summary for a given date."""
    path = PROJECT_ROOT / "logs" / f"eod_summary_{trade_date}.json"
    if not path.exists():
        return None
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return None


def _read_eod_report(trade_date: str) -> Optional[str]:
    """Read the full-text EOD report for a given date."""
    path = PROJECT_ROOT / "logs" / f"eod_{trade_date}.txt"
    if not path.exists():
        return None
    try:
        return path.read_text(errors="replace")
    except Exception:
        return None


def _fmt_currency(value: float, currency: str = "USD") -> str:
    """Format currency inline without importing helpers (keep telegram module lightweight)."""
    sign = "-" if value < 0 else ""
    av = abs(value)
    if currency == "AUD":
        return f"{sign}A${av:,.2f}"
    return f"{sign}${av:,.2f}"


def _fmt_pnl(value: float, currency: str = "USD") -> str:
    """Format P&L with +/- prefix."""
    s = "+" if value >= 0 else ""
    return s + _fmt_currency(value, currency)


def _build_market_block(market_id: str, md: dict) -> list[str]:
    """Build message lines for a single market from dashboard data."""
    pf = md.get("portfolio", {})
    mode = md.get("trading_mode", "live")
    currency = "AUD" if market_id == "asx" else "USD"

    mode_icon = {"live": "🔴 LIVE", "live_dry_run": "🔶 DRY RUN", }.get(mode, mode)
    equity = pf.get("equity", 0)
    total_pnl = pf.get("total_pnl", 0)
    total_pnl_pct = pf.get("total_pnl_pct", 0)
    cash = pf.get("cash", 0)
    n_atlas = pf.get("num_open", 0)
    realized = pf.get("realized_pnl", 0)
    manual = md.get("manual_positions", {})
    n_manual = manual.get("num_open", 0)

    lines = [
        f"<b>{market_id.upper()} {mode_icon}</b>",
        f"  Equity: <b>{_fmt_currency(equity, currency)}</b> ({_fmt_pnl(total_pnl, currency)}, {total_pnl_pct:+.1f}%)",
        f"  Cash: {_fmt_currency(cash, currency)}",
        f"  Positions: {n_atlas} atlas" + (f" + {n_manual} manual" if n_manual else ""),
    ]

    if realized != 0:
        lines.append(f"  Realized P&amp;L: {_fmt_pnl(realized, currency)}")

    # Atlas positions
    atlas_pos = pf.get("open_positions", [])
    if atlas_pos:
        sorted_pos = sorted(atlas_pos, key=lambda p: p.get("pnl", 0), reverse=True)
        for p in sorted_pos[:6]:
            ticker = p.get("ticker", "?")
            pnl = p.get("pnl", 0)
            pnl_pct = p.get("pnl_pct", 0)
            icon = "🟢" if pnl >= 0 else "🔴"
            lines.append(
                f"  {icon} <b>{_esc(ticker)}</b>  "
                f"{_fmt_pnl(pnl, currency)} ({pnl_pct:+.1f}%)"
            )
        if len(sorted_pos) > 6:
            lines.append(f"  … +{len(sorted_pos) - 6} more")

    # Manual positions (compact)
    manual_pos = manual.get("positions", [])
    if manual_pos:
        manual_pnl = manual.get("unrealized_pnl", 0)
        lines.append(f"  <i>Manual: {n_manual} pos, {_fmt_pnl(manual_pnl, currency)}</i>")

    return lines


def _build_portfolio_snapshot(market_id: str = "sp500") -> Optional[str]:
    """Build an HTML portfolio snapshot from dashboard-data.json.

    Used by the /status bot command and premarket summary.
    Reads from the already-generated dashboard data (no broker connection).
    """
    try:
        dash = _read_dashboard_data()
        if not dash:
            return None

        lines = []

        # Broker account (if available)
        acct = dash.get("account")
        if acct:
            lines.extend([
                "<b>Broker Account:</b>",
                f"  Equity: <b>{_fmt_currency(acct['equity'], acct.get('currency', 'AUD'))}</b>",
                f"  Cash: {_fmt_currency(acct['cash'], acct.get('currency', 'AUD'))}",
                f"  Buying Power: {_fmt_currency(acct.get('buying_power', 0), acct.get('currency', 'AUD'))}",
                "",
            ])

        # Per-market blocks
        markets = dash.get("markets", {})
        for mid in sorted(markets.keys()):
            lines.extend(_build_market_block(mid, markets[mid]))
            lines.append("")

        return "\n".join(lines).rstrip()
    except Exception as e:
        logger.error("Failed to build portfolio snapshot: %s", e, exc_info=True)
        return None


def send_premarket_summary(plan_path: Optional[str] = None, market_id: str = "sp500") -> bool:
    """Send a summary of the pre-market plan generation.

    Includes portfolio snapshot from dashboard data and plan details.
    """
    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    if plan_path is None:
        today = datetime.now().strftime("%Y-%m-%d")
        plan_path = str(PROJECT_ROOT / f"plans/plan_{market_id}_{today}.json")

    # Portfolio snapshot from dashboard data
    snapshot = _build_portfolio_snapshot(market_id)

    plan_file = Path(plan_path)
    if not plan_file.exists():
        msg = (
            f"📊 <b>Atlas Pre-Market [{market_id.upper()}]</b>\n"
            f"<i>{now}</i>\n\n"
        )
        if snapshot:
            msg += snapshot + "\n\n"
        msg += "⚠️ No plan file generated today.\nCheck logs for errors."
        return send_message(msg)

    with open(plan_file) as f:
        plan = json.load(f)

    entries = plan.get("proposed_entries", plan.get("entries", []))
    exits = plan.get("proposed_exits", plan.get("exits", []))

    # Build entries summary
    if entries:
        entry_lines = []
        for e in entries[:6]:
            ticker = e.get("ticker", "?")
            strategy = e.get("strategy", "?")
            conf = e.get("confidence", 0)
            price = e.get("entry_price", 0)
            size = e.get("position_size", 0)
            value = price * size if size else 0
            line = f"  • <b>{_esc(ticker)}</b> ({strategy}) @ ${price:.2f}"
            if value:
                line += f" × {size} = ${value:,.0f}"
            if conf:
                line += f"  [{conf:.0%}]"
            entry_lines.append(line)
        entry_text = "\n".join(entry_lines)
        if len(entries) > 6:
            entry_text += f"\n  … +{len(entries) - 6} more"
    else:
        entry_text = "  No new entries"

    # Build exits summary
    if exits:
        exit_lines = []
        for x in exits[:4]:
            ticker = x.get("ticker", "?")
            reason = x.get("reason", x.get("exit_reason", "?"))
            exit_lines.append(f"  • <b>{_esc(ticker)}</b> — {_esc(reason)}")
        exit_text = "\n".join(exit_lines)
        if len(exits) > 4:
            exit_text += f"\n  … +{len(exits) - 4} more"
    else:
        exit_text = "  No exits"

    # Risk summary from plan
    risk = plan.get("risk_summary", {})
    risk_line = ""
    if risk:
        cost = risk.get("total_proposed_cost", 0)
        risk_amt = risk.get("total_proposed_risk", 0)
        exposure = risk.get("portfolio_exposure_pct", 0)
        risk_line = f"\n⚠️ Cost ${cost:,.0f} | Risk ${risk_amt:,.0f} | Exposure {exposure:.0f}%\n"

    msg = (
        f"📊 <b>Atlas Pre-Market [{market_id.upper()}]</b>\n"
        f"<i>{now}</i>\n\n"
    )
    if snapshot:
        msg += snapshot + "\n\n"
    msg += (
        f"<b>Today's Plan:</b>\n"
        f"  Entries: {len(entries)} | Exits: {len(exits)}\n"
    )
    if entries:
        msg += f"\n<b>🟢 Entries ({len(entries)}):</b>\n{entry_text}\n"
    if exits:
        msg += f"\n<b>🟡 Exits ({len(exits)}):</b>\n{exit_text}\n"
    if risk_line:
        msg += risk_line
    if not entries and not exits:
        msg += "  → <b>Hold all positions</b>\n"
    msg += f"\nStatus: <b>awaiting approval</b>"
    return send_message(msg)


def send_postclose_summary(market_id: str = "sp500") -> bool:
    """Send post-close summary with multi-market data and exits.

    Reads from dashboard-data.json (just refreshed by cron) for accurate
    per-market equity, positions, and P&L.  Also reads EOD reports for
    exit details.  Single message covers all active markets.
    """
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    today = datetime.now().strftime("%Y-%m-%d")

    dash = _read_dashboard_data()
    if not dash:
        return send_message(
            f"📈 <b>Atlas Post-Close</b>\n"
            f"<i>{now}</i>\n\n"
            f"⚠️ Dashboard data unavailable. Check logs."
        )

    lines = [
        f"📈 <b>Atlas Post-Close</b>",
        f"<i>{now}</i>",
        "",
    ]

    # Broker account totals
    acct = dash.get("account")
    if acct:
        acur = acct.get("currency", "AUD")
        lines.extend([
            f"<b>Broker Account</b>",
            f"  Equity: <b>{_fmt_currency(acct['equity'], acur)}</b>",
            f"  Cash: {_fmt_currency(acct['cash'], acur)}",
            f"  Buying Power: {_fmt_currency(acct.get('buying_power', 0), acur)}",
            "",
        ])

    # Per-market blocks
    markets = dash.get("markets", {})
    for mid in sorted(markets.keys()):
        lines.extend(_build_market_block(mid, markets[mid]))
        lines.append("")

    # EOD exits (from EOD report or closed trades in dashboard data)
    eod_report = _read_eod_report(today)
    closed_today = [
        t for t in dash.get("closed_trades", [])
        if t.get("exit_date", "") == today
    ]

    if closed_today:
        total_realized = sum(t.get("pnl", 0) for t in closed_today)
        lines.append(f"<b>Trades Closed Today ({len(closed_today)}):</b>")
        for t in closed_today:
            ticker = t.get("ticker", "?")
            pnl = t.get("pnl", 0)
            pnl_pct = t.get("pnl_pct", 0)
            reason = t.get("exit_reason", t.get("reason", "?"))
            strategy = t.get("strategy", "")
            mid_tag = t.get("market", "")
            icon = "🟢" if pnl >= 0 else "🔴"
            line = f"  {icon} <b>{_esc(ticker)}</b>"
            if strategy:
                line += f" [{_esc(strategy)}]"
            line += f"  {_fmt_pnl(pnl)} ({pnl_pct:+.1f}%)"
            if reason:
                line += f" — {_esc(reason)}"
            lines.append(line)
        lines.append(f"  Total realized: <b>{_fmt_pnl(total_realized)}</b>")
        lines.append("")

    # EOD settlement status
    eod_summary = _read_eod_summary(today)
    if eod_summary:
        stop_exits = eod_summary.get("stop_exits", 0)
        tp_exits = eod_summary.get("tp_exits", 0)
        halted = eod_summary.get("halted", False)
        status_parts = []
        if stop_exits:
            status_parts.append(f"🔴 {stop_exits} stop-loss exits")
        if tp_exits:
            status_parts.append(f"🟢 {tp_exits} take-profit exits")
        if halted:
            status_parts.append("⛔ TRADING HALTED")
        if status_parts:
            lines.append("<b>Settlement:</b> " + " | ".join(status_parts))
        elif not closed_today:
            lines.append("✅ No exits triggered. All positions held.")
    elif not closed_today:
        lines.append("✅ No exits triggered.")

    # Timestamp from dashboard
    ts = dash.get("timestamp", "")
    if ts:
        lines.append(f"\n<i>Data as of {_esc(ts[:19])}</i>")

    return send_message("\n".join(lines))


def send_error(mode: str, detail: str, logfile: Optional[str] = None) -> bool:
    """Send an error alert for a failed cron run.

    Enhanced version: extracts Python tracebacks from the log, classifies
    the error type, and includes actionable runbook hints.
    """
    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    lines_out = [
        f"🚨 <b>Atlas CRON FAILED</b>",
        f"<i>{now}</i>",
        "",
        f"<b>Mode:</b> {_esc(mode)}",
    ]

    # Extract useful content from logfile
    log_lines = []
    if logfile and Path(logfile).exists():
        log_lines = Path(logfile).read_text(errors="replace").splitlines()

    # Try to find Python traceback in the log
    traceback_text = ""
    for i, line in enumerate(log_lines):
        if "Traceback (most recent call last)" in line:
            tb = log_lines[i:min(i + 25, len(log_lines))]
            traceback_text = "\n".join(tb)
            break

    # Try to find ERROR-level log lines
    error_lines = [l for l in log_lines if "[ERROR]" in l or "ERROR:" in l]

    if traceback_text:
        lines_out.append("")
        lines_out.append("<b>Traceback:</b>")
        lines_out.append(f"<pre>{_esc(traceback_text[-1200:])}</pre>")
    elif error_lines:
        lines_out.append("")
        lines_out.append(f"<b>Errors ({len(error_lines)}):</b>")
        for el in error_lines[-5:]:
            lines_out.append(f"<pre>{_esc(el[-200:])}</pre>")
    elif detail:
        lines_out.append("")
        lines_out.append(f"<b>Detail:</b>")
        lines_out.append(f"<pre>{_esc(detail[:1000])}</pre>")

    # Show last 8 lines of log as context
    if log_lines and not traceback_text:
        tail = log_lines[-8:]
        lines_out.append("")
        lines_out.append("<b>Log tail:</b>")
        lines_out.append(f"<pre>{_esc(chr(10).join(tail)[-800:])}</pre>")

    # Classify error and provide actionable hints
    all_text = (detail + " " + " ".join(log_lines[-30:])).lower()
    hints = []
    if "connect" in all_text or "broker" in all_text:
        hints.append("🔌 Check broker connectivity")
    if "timeout" in all_text or "timed out" in all_text:
        hints.append("⏱ Timeout — network or API rate limit")
    if "yfinance" in all_text or "download" in all_text and "fail" in all_text:
        hints.append("📊 Data fetch — yfinance may be throttled or market closed")
    if "delisted" in all_text or "no price data" in all_text:
        hints.append("📊 Data quality — yfinance download failures (delisted/missing tickers). Check universe list.")
    if "permission" in all_text or "credential" in all_text or "token" in all_text:
        hints.append("🔑 Auth — check ~/.atlas-secrets.json")
    if "no space" in all_text or "disk" in all_text:
        hints.append("💾 Disk — run <code>scripts/weekly_maintenance.sh</code>")
    if "import" in all_text and "error" in all_text:
        hints.append("📦 Missing dependency — check pip packages")
    if "killed" in all_text or "oom" in all_text or "memory" in all_text:
        hints.append("🧠 OOM — process killed by kernel, reduce batch size")

    if hints:
        lines_out.append("")
        lines_out.append("<b>Likely cause:</b>")
        for h in hints:
            lines_out.append(f"  {h}")

    # Recovery action
    lines_out.append("")
    lines_out.append(f"<b>Recover:</b> <code>cd /root/atlas &amp;&amp; scripts/pi-cron.sh {_esc(mode)} sp500</code>")

    return send_message("\n".join(lines_out))


def send_startup() -> bool:
    """Send a simple connectivity/startup test message."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    return send_message(
        f"🤖 <b>Atlas Telegram Connected</b>\n"
        f"<i>{now}</i>\n\n"
        f"Alerts are active. You'll receive:\n"
        f"  📊 Pre-market plan summaries\n"
        f"  📈 Post-close settlement reports\n"
        f"  🔬 Research experiment results\n"
        f"  🚨 Error alerts for failed runs"
    )


# ---------------------------------------------------------------------------
# Research notifications
# ---------------------------------------------------------------------------

def send_research_started(experiment_id: str, hypothesis: str,
                          market: str, estimated_min: int = 0) -> bool:
    """Notify that a research experiment has started."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    eta = f" — ETA ~{estimated_min}min" if estimated_min else ""
    msg = (
        f"🔬 <b>Research: Experiment Started</b>\n"
        f"<i>{now}</i>\n\n"
        f"<b>ID:</b> <code>{_esc(experiment_id)}</code>\n"
        f"<b>Market:</b> {_esc(market.upper())}\n"
        f"<b>Hypothesis:</b> {_esc(hypothesis[:200])}{eta}"
    )
    return send_message(msg, silent=True)


def send_research_result(experiment_id: str, strategy: str,
                         market: str, verdict: str,
                         key_metrics: dict = None,
                         delta: dict = None) -> bool:
    """Notify experiment completion with pass/fail and key metrics."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    icon = {"pass": "✅", "fail": "❌", "partial": "⚠️"}.get(verdict, "❓")

    lines = [
        f"📊 <b>Research Result: {icon} {_esc(verdict.upper())}</b>",
        f"<i>{now}</i>",
        "",
        f"<b>ID:</b> <code>{_esc(experiment_id)}</code>",
        f"<b>Strategy:</b> {_esc(strategy or 'N/A')}",
        f"<b>Market:</b> {_esc(market.upper())}",
    ]

    if key_metrics:
        lines.append("")
        lines.append("<b>Key Metrics:</b>")
        for k, v in key_metrics.items():
            if isinstance(v, float):
                lines.append(f"  {_esc(k)}: {v:.4f}")
            else:
                lines.append(f"  {_esc(k)}: {v}")

    if delta:
        lines.append("")
        lines.append("<b>Delta vs Baseline:</b>")
        for k, v in delta.items():
            if isinstance(v, (int, float)):
                sign = "+" if v > 0 else ""
                lines.append(f"  {_esc(k)}: {sign}{v:.4f}")

    return send_message("\n".join(lines))


def send_research_promotion_request(experiment_id: str, market: str,
                                     comparisons: dict,
                                     oos_details: dict = None) -> bool:
    """Send a rich promotion request with Approve/Deny inline buttons."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    # Metrics where lower is better (green for decrease, red for increase)
    _LOWER_IS_BETTER = {'max_drawdown', 'max_drawdown_pct', 'max_dd', 'drawdown'}
    # Metrics that are ratios/fractions to display as percentages
    _PCT_METRICS = {'cagr', 'cagr_pct', 'max_drawdown', 'max_drawdown_pct', 'max_dd',
                    'win_rate', 'win_rate_pct', 'drawdown', 'total_return'}
    # Human-friendly names
    _LABELS = {
        'sharpe': 'Sharpe', 'cagr': 'CAGR', 'cagr_pct': 'CAGR',
        'max_drawdown': 'Max DD', 'max_drawdown_pct': 'Max DD', 'max_dd': 'Max DD',
        'profit_factor': 'Profit Factor', 'win_rate': 'Win Rate', 'win_rate_pct': 'Win Rate',
        'sortino': 'Sortino', 'total_return': 'Total Return', 'drawdown': 'Drawdown',
    }

    lines = [
        f"🏆 <b>Research: Promotion Request</b>",
        f"<i>{now}</i>",
        "",
        f"<b>Experiment:</b> <code>{_esc(experiment_id)}</code>",
        f"<b>Market:</b> {_esc(market.upper())}",
        "",
        "<b>Before → After:</b>",
    ]

    for metric, data in comparisons.items():
        b = data.get('baseline', 0)
        c = data.get('candidate', 0)
        d = data.get('delta', 0)
        inverted = metric in _LOWER_IS_BETTER
        is_pct = metric in _PCT_METRICS
        label = _LABELS.get(metric, metric)

        # For inverted metrics (DD), negative delta = improvement
        improved = (d < 0) if inverted else (d > 0)
        worsened = (d > 0) if inverted else (d < 0)
        icon = "🟢" if improved else "🔴" if worsened else "⚪"
        arrow = "↑" if d > 0 else "↓" if d < 0 else "→"

        if is_pct:
            lines.append(f"  {icon} {_esc(label)}: {b*100:.1f}% → {c*100:.1f}% ({d*100:+.1f}pp {arrow})")
        else:
            lines.append(f"  {icon} {_esc(label)}: {b:.3f} → {c:.3f} ({d:+.3f} {arrow})")

    if oos_details:
        # Only show tests that have real results (not N/A or ?)
        oos_tests = [
            ("Test 1 (Time Split)", oos_details.get('test1')),
            ("Test 2 (Perturbation)", oos_details.get('test2')),
            ("Test 3 (Walk-Forward)", oos_details.get('test3')),
        ]
        has_any = any(v and str(v) not in ('?', 'N/A', 'None', '') for _, v in oos_tests)
        if has_any:
            lines.extend(["", "<b>OOS Validation:</b>"])
            for name, val in oos_tests:
                val_str = str(val) if val else ''
                if val_str in ('?', 'N/A', 'None', ''):
                    continue
                verdict_icon = "✅" if "PASS" in val_str.upper() else "❌" if "FAIL" in val_str.upper() else "⏭️"
                lines.append(f"  {verdict_icon} {name}: {_esc(val_str)}")

    keyboard = {
        "inline_keyboard": [[
            {"text": "✅ Approve & Promote", "callback_data": f"research:{experiment_id}:approve:{market}"},
            {"text": "❌ Reject", "callback_data": f"research:{experiment_id}:reject:{market}"},
        ]]
    }

    return send_message("\n".join(lines), reply_markup=keyboard)


def send_research_weekly_digest(experiments_run: int, passed: int,
                                 failed: int, promoted: int,
                                 cumulative_sharpe_delta: float = 0,
                                 next_queue: list = None) -> bool:
    """Send a weekly research summary digest."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    lines = [
        f"📅 <b>Weekly Research Digest</b>",
        f"<i>{now}</i>",
        "",
        f"<b>Experiments:</b> {experiments_run} run",
        f"  ✅ Passed: {passed}",
        f"  ❌ Failed: {failed}",
        f"  🏆 Promoted: {promoted}",
    ]

    if cumulative_sharpe_delta != 0:
        sign = "+" if cumulative_sharpe_delta > 0 else ""
        lines.append(f"\n<b>Cumulative Sharpe Δ:</b> {sign}{cumulative_sharpe_delta:.4f}")

    if next_queue:
        lines.append("\n<b>Next in Queue:</b>")
        for item in next_queue[:5]:
            lines.append(f"  • [{item.get('priority', '?')}] {_esc(item.get('title', '?')[:60])}")

    return send_message("\n".join(lines))


def send_research_complete(market_id: str = "sp500") -> bool:
    """Send research session completion summary."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    # Try to read journal for today's results
    journal_summary = ""
    try:
        journal_path = PROJECT_ROOT / "research" / "journal.json"
        if journal_path.exists():
            with open(journal_path) as f:
                journal = json.load(f)
            today = datetime.now().strftime("%Y-%m-%d")
            today_entries = [e for e in journal if e.get("timestamp", "").startswith(today)]
            if today_entries:
                passed = sum(1 for e in today_entries if e.get("verdict") == "pass")
                failed = sum(1 for e in today_entries if e.get("verdict") == "fail")
                promoted = sum(1 for e in today_entries if e.get("promoted"))
                journal_summary = (
                    f"\n<b>Today's Results:</b>\n"
                    f"  Experiments: {len(today_entries)}\n"
                    f"  ✅ Passed: {passed} | ❌ Failed: {failed} | 🏆 Promoted: {promoted}"
                )
    except Exception:
        pass

    msg = (
        f"🔬 <b>Research Session Complete [{market_id.upper()}]</b>\n"
        f"<i>{now}</i>"
        f"{journal_summary}"
    )
    return send_message(msg)


# ---------------------------------------------------------------------------
# Photo & Document Delivery (our "AirDrop" equivalent)
# ---------------------------------------------------------------------------

def send_photo(image_path: str, caption: str = "", silent: bool = False) -> bool:
    """Send a photo (PNG/JPG) to the configured Telegram chat.

    Args:
        image_path: Path to the image file.
        caption: Optional caption (HTML, max 1024 chars).
        silent: If True, send without notification sound.

    Returns True if sent successfully.
    """
    try:
        token, chat_id = _load_credentials()
    except ValueError as e:
        logger.error("Telegram send_photo failed: %s", e)
        return False

    image_path = Path(image_path)
    if not image_path.exists():
        logger.error("Image not found: %s", image_path)
        return False

    # Multipart form upload
    boundary = f"----AtlasBoundary{os.urandom(8).hex()}"
    parts = []

    # chat_id field
    parts.append(
        f"--{boundary}\r\n"
        f"Content-Disposition: form-data; name=\"chat_id\"\r\n\r\n"
        f"{chat_id}\r\n"
    )

    # caption field
    if caption:
        caption = caption[:1024]
        parts.append(
            f"--{boundary}\r\n"
            f"Content-Disposition: form-data; name=\"caption\"\r\n\r\n"
            f"{caption}\r\n"
        )
        parts.append(
            f"--{boundary}\r\n"
            f"Content-Disposition: form-data; name=\"parse_mode\"\r\n\r\n"
            f"HTML\r\n"
        )

    # silent
    if silent:
        parts.append(
            f"--{boundary}\r\n"
            f"Content-Disposition: form-data; name=\"disable_notification\"\r\n\r\n"
            f"true\r\n"
        )

    # photo file
    photo_header = (
        f"--{boundary}\r\n"
        f"Content-Disposition: form-data; name=\"photo\"; filename=\"{image_path.name}\"\r\n"
        f"Content-Type: image/png\r\n\r\n"
    )

    photo_data = image_path.read_bytes()
    ending = f"\r\n--{boundary}--\r\n"

    body = "".join(parts).encode() + photo_header.encode() + photo_data + ending.encode()

    url = f"https://api.telegram.org/bot{token}/sendPhoto"
    req = urllib.request.Request(
        url, data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read())
            if result.get("ok"):
                logger.info("Photo sent: %s (%d KB)", image_path.name,
                            len(photo_data) // 1024)
                return True
            logger.warning("Telegram sendPhoto ok=false: %s", result)
            return False
    except Exception as e:
        logger.error("Telegram send_photo error: %s", e)
        return False


def send_document(file_path: str, caption: str = "", silent: bool = False) -> bool:
    """Send a file as a Telegram document.

    Args:
        file_path: Path to the file.
        caption: Optional caption (HTML, max 1024 chars).
        silent: If True, send without notification sound.

    Returns True if sent successfully.
    """
    try:
        token, chat_id = _load_credentials()
    except ValueError as e:
        logger.error("Telegram send_document failed: %s", e)
        return False

    file_path = Path(file_path)
    if not file_path.exists():
        logger.error("File not found: %s", file_path)
        return False

    boundary = f"----AtlasBoundary{os.urandom(8).hex()}"
    parts = []

    parts.append(
        f"--{boundary}\r\n"
        f"Content-Disposition: form-data; name=\"chat_id\"\r\n\r\n"
        f"{chat_id}\r\n"
    )
    if caption:
        parts.append(
            f"--{boundary}\r\n"
            f"Content-Disposition: form-data; name=\"caption\"\r\n\r\n"
            f"{caption[:1024]}\r\n"
        )
        parts.append(
            f"--{boundary}\r\n"
            f"Content-Disposition: form-data; name=\"parse_mode\"\r\n\r\n"
            f"HTML\r\n"
        )
    if silent:
        parts.append(
            f"--{boundary}\r\n"
            f"Content-Disposition: form-data; name=\"disable_notification\"\r\n\r\n"
            f"true\r\n"
        )

    file_data = file_path.read_bytes()
    doc_header = (
        f"--{boundary}\r\n"
        f"Content-Disposition: form-data; name=\"document\"; filename=\"{file_path.name}\"\r\n"
        f"Content-Type: application/octet-stream\r\n\r\n"
    )
    ending = f"\r\n--{boundary}--\r\n"

    body = "".join(parts).encode() + doc_header.encode() + file_data + ending.encode()

    url = f"https://api.telegram.org/bot{token}/sendDocument"
    req = urllib.request.Request(
        url, data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read())
            if result.get("ok"):
                logger.info("Document sent: %s (%d KB)", file_path.name,
                            len(file_data) // 1024)
                return True
            logger.warning("Telegram sendDocument ok=false: %s", result)
            return False
    except Exception as e:
        logger.error("Telegram send_document error: %s", e)
        return False


def send_charts(charts: list, caption: str = "", silent: bool = True) -> bool:
    """Send multiple chart images to Telegram.

    Sends the first chart with caption, rest silently.
    Returns True if all sent successfully.
    """
    if not charts:
        return True

    from pathlib import Path as _P
    ok = True
    for i, chart in enumerate(charts):
        cap = caption if i == 0 else ""
        if not send_photo(str(chart), caption=cap, silent=silent):
            ok = False
    return ok


# ---------------------------------------------------------------------------
# Smart Notification System
# ---------------------------------------------------------------------------
# Prevents notification spam via rate limiting + batching.
#
# Priority levels:
#   CRITICAL  — always send immediately (errors, health alerts)
#   IMPORTANT — send immediately, rate-limited per category (30min cooldown)
#   INFO      — accumulated into periodic digest
#   SILENT    — suppressed (log only)
#
# Usage:
#   from utils.telegram import notify, flush_digest, CRITICAL, IMPORTANT, INFO
#   notify("Engine started", level=IMPORTANT, category="session")
#   notify("Strategy improved", level=INFO, category="improvement")
#   flush_digest()  # send accumulated INFO messages as one digest
# ---------------------------------------------------------------------------

CRITICAL = 0
IMPORTANT = 1
INFO = 2
SILENT = 3

_NOTIFY_STATE_PATH = Path("/tmp/atlas-notify-state.json")
_DIGEST_INTERVAL_S = 7200    # auto-flush digest every 2 hours
_RATE_LIMIT_S = 1800          # 30 min cooldown for IMPORTANT per category
_MAX_QUEUED = 200             # cap queued messages to prevent unbounded growth


def _load_notify_state() -> dict:
    """Load notification state from disk (cross-process safe)."""
    try:
        if _NOTIFY_STATE_PATH.exists():
            with open(_NOTIFY_STATE_PATH) as f:
                return json.load(f)
    except Exception:
        pass
    return {"last_sent": {}, "queued": [], "last_digest": 0}


def _save_notify_state(state: dict) -> None:
    """Persist notification state to disk."""
    try:
        # Trim queue if too large
        if len(state.get("queued", [])) > _MAX_QUEUED:
            state["queued"] = state["queued"][-_MAX_QUEUED:]
        _NOTIFY_STATE_PATH.write_text(json.dumps(state, indent=2))
    except Exception as e:
        logger.warning("Failed to save notify state: %s", e)


def notify(text: str, level: int = INFO, category: str = "general",
           silent: bool = False) -> bool:
    """Smart notification with rate limiting and batching.

    Args:
        text: Message body (HTML).
        level: CRITICAL, IMPORTANT, INFO, or SILENT.
        category: Grouping key for rate limiting and digest sections.
        silent: If True, send without notification sound.

    Returns:
        True if sent/queued successfully.
    """
    if level == SILENT:
        logger.debug("Notification suppressed [%s]: %s", category, text[:80])
        return True

    if level == CRITICAL:
        return send_message(text, silent=silent)

    now = _time.time()
    state = _load_notify_state()

    if level == IMPORTANT:
        last = state.get("last_sent", {}).get(category, 0)
        if now - last < _RATE_LIMIT_S:
            # Rate-limited — queue for digest instead of dropping
            logger.info("Rate-limited [%s] — queued for digest", category)
            state.setdefault("queued", []).append({
                "text": text, "category": category, "ts": now,
            })
            _save_notify_state(state)
            return True
        state.setdefault("last_sent", {})[category] = now
        _save_notify_state(state)
        return send_message(text, silent=silent)

    # INFO — batch into digest
    state.setdefault("queued", []).append({
        "text": text, "category": category, "ts": now,
    })
    _save_notify_state(state)

    # Auto-flush if digest interval has passed (but not on first-ever message)
    last_digest = state.get("last_digest", 0)
    if last_digest > 0 and now - last_digest > _DIGEST_INTERVAL_S:
        return flush_digest()

    return True


def flush_digest() -> bool:
    """Send all accumulated INFO messages as a single digest.

    Groups messages by category, shows most recent per category,
    and clears the queue. Call this at natural boundaries (end of
    cycle, end of session, etc.).

    Returns True if sent or nothing to send.
    """
    state = _load_notify_state()
    queued = state.get("queued", [])
    if not queued:
        return True

    now = _time.time()

    # Group by category
    by_cat: dict[str, list[dict]] = {}
    for msg in queued:
        cat = msg.get("category", "general")
        by_cat.setdefault(cat, []).append(msg)

    # Category display order and icons
    _ICONS = {
        "improvement": "📈",
        "cycle": "🔄",
        "sweep": "🔬",
        "session": "🚀",
        "general": "📋",
    }

    n_total = len(queued)
    oldest = min(m.get("ts", now) for m in queued)
    span_min = (now - oldest) / 60

    lines = [
        f"📋 <b>Atlas Research Digest</b>",
        f"<i>{n_total} updates over {span_min:.0f} min</i>",
        "",
    ]

    for cat, msgs in sorted(by_cat.items()):
        icon = _ICONS.get(cat, "•")
        cat_label = cat.replace("_", " ").title()
        lines.append(f"{icon} <b>{_esc(cat_label)} ({len(msgs)})</b>")

        # Show last 5 messages per category (most recent first)
        for msg in msgs[-5:]:
            # Extract a brief summary — strip outer HTML bold tags
            brief = msg["text"]
            # Remove leading emoji + bold tag for digest brevity
            for prefix in ("🔬 ", "📈 ", "🔄 ", "📊 "):
                brief = brief.removeprefix(prefix)
            brief = brief.replace("<b>", "").replace("</b>", "")
            # Take first line only, truncate
            first_line = brief.split("\n")[0][:120]
            lines.append(f"  • {first_line}")

        if len(msgs) > 5:
            lines.append(f"  <i>… +{len(msgs) - 5} more</i>")
        lines.append("")

    state["queued"] = []
    state["last_digest"] = now
    _save_notify_state(state)

    return send_message("\n".join(lines), silent=True)
