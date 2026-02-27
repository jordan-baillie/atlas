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
    send_premarket_summary(plan_path="paper_engine/plans/plan_2026-02-25.json")
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

def send_message(text: str, parse_mode: str = "HTML", silent: bool = False) -> bool:
    """Send a message to the configured Telegram chat.

    Args:
        text: Message body (HTML or plain text).
        parse_mode: 'HTML' or 'MarkdownV2'.
        silent: If True, send without notification sound.

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

    payload = json.dumps({
        "chat_id": chat_id,
        "text": text,
        "parse_mode": parse_mode,
        "disable_notification": silent,
        "disable_web_page_preview": True,
    }).encode("utf-8")

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


def _fetch_cached_prices(tickers: list[str]) -> dict[str, float]:
    """Read latest close prices from parquet cache for given tickers.

    This avoids a network call — it reads whatever the most recent
    ingest/settlement wrote to disk.
    """
    import pandas as pd

    cache_dir = PROJECT_ROOT / "data" / "cache"
    prices = {}
    for ticker in tickers:
        cache_key = ticker.replace(".", "_")
        cache_path = cache_dir / f"{cache_key}.parquet"
        if cache_path.exists():
            try:
                df = pd.read_parquet(cache_path)
                if not df.empty and "close" in df.columns:
                    prices[ticker] = float(df.iloc[-1]["close"])
            except Exception:
                pass
    return prices


def _build_portfolio_snapshot(market_id: str = "sp500") -> Optional[str]:
    """Build an HTML portfolio snapshot block with live prices from broker.

    Returns None if portfolio can't be loaded.
    """
    try:
        from utils.config import get_active_config
        from brokers.live_portfolio import LivePortfolio
        from utils.helpers import format_currency
        from markets.registry import get_market

        config = get_active_config(market_id)
        pp = LivePortfolio(config, market_id=market_id)
        if not pp.connect():
            return None
        market = get_market(market_id)
        currency = market.currency

        tickers = [p.ticker for p in pp.positions]
        prices = _fetch_cached_prices(tickers) if tickers else {}
        summary = pp.portfolio_summary(prices)

        equity = summary["equity"]
        cash = summary["cash"]
        positions = summary.get("open_positions", [])
        n_pos = len(positions)
        invested = equity - cash
        total_pnl = sum(p.get("unrealized_pnl", 0) for p in positions)
        pnl_sign = "+" if total_pnl >= 0 else ""

        lines = [
            "<b>Portfolio:</b>",
            f"  Equity: <b>{format_currency(equity, currency)}</b>",
            f"  Cash: {format_currency(cash, currency)}",
            f"  Invested: {format_currency(invested, currency)}",
            f"  Positions: {n_pos}",
            f"  Unrealised PnL: <b>{pnl_sign}{format_currency(total_pnl, currency)}</b>",
        ]

        # Top movers
        sorted_pos = sorted(positions, key=lambda p: abs(p.get("unrealized_pnl", 0)), reverse=True)
        if sorted_pos:
            lines.append("")
            lines.append("<b>Top Movers:</b>")
            for p in sorted_pos[:5]:
                ticker = p.get("ticker", "?")
                pnl = p.get("unrealized_pnl", 0)
                pnl_pct = p.get("unrealized_pnl_pct", 0)  # already ×100 from Position class
                icon = "🟢" if pnl >= 0 else "🔴"
                lines.append(
                    f"  {icon} <b>{_esc(ticker)}</b>  "
                    f"{'+' if pnl >= 0 else ''}{format_currency(pnl, currency)} "
                    f"({'+' if pnl_pct >= 0 else ''}{pnl_pct:.1f}%)"
                )

        return "\n".join(lines)
    except Exception as e:
        logger.error("Failed to build portfolio snapshot: %s", e, exc_info=True)
        return None


def send_premarket_summary(plan_path: Optional[str] = None, market_id: str = "asx") -> bool:
    """Send a summary of the pre-market plan generation.

    Includes portfolio snapshot with live prices and plan details.
    """
    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    if plan_path is None:
        today = datetime.now().strftime("%Y-%m-%d")
        plan_path = str(PROJECT_ROOT / f"paper_engine/plans/plan_{today}.json")

    # Portfolio snapshot (always include — this is the main content)
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

    entries = plan.get("entries", [])
    exits = plan.get("exits", [])

    # Build entries summary
    if entries:
        entry_lines = []
        for e in entries[:6]:
            ticker = e.get("ticker", "?")
            strategy = e.get("strategy", "?")
            conf = e.get("confidence", 0)
            price = e.get("entry_price", 0)
            entry_lines.append(f"  • <b>{_esc(ticker)}</b> ({strategy}) @ ${price:.2f}  conf={conf:.0%}")
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
        msg += f"\n<b>Entries ({len(entries)}):</b>\n{entry_text}\n"
    if exits:
        msg += f"\n<b>Exits ({len(exits)}):</b>\n{exit_text}\n"
    if not entries and not exits:
        msg += "  → <b>Hold all positions</b>\n"
    msg += f"\nStatus: <b>awaiting approval</b>"
    return send_message(msg)


def send_postclose_summary(market_id: str = "asx") -> bool:
    """Send post-close settlement summary with equity and position snapshot.

    Fetches current prices from the parquet cache (which EOD settlement
    has already refreshed) so unrealised PnL and equity are accurate.
    """
    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    snapshot = _build_portfolio_snapshot(market_id)
    if snapshot:
        msg = (
            f"📈 <b>Atlas Post-Close [{market_id.upper()}]</b>\n"
            f"<i>{now}</i>\n\n"
            f"{snapshot}"
        )
        return send_message(msg)

    # Fallback if snapshot build fails
    return send_message(
        f"📈 <b>Atlas Post-Close [{market_id.upper()}]</b>\n"
        f"<i>{now}</i>\n\n"
        f"⚠️ Settlement completed but summary failed.\n"
        f"Check logs for details."
    )


def send_error(mode: str, detail: str, logfile: Optional[str] = None) -> bool:
    """Send an error alert for a failed cron run."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    # Grab last 15 lines of logfile if available
    log_tail = ""
    if logfile and Path(logfile).exists():
        lines = Path(logfile).read_text(errors="replace").splitlines()
        tail = lines[-15:] if len(lines) > 15 else lines
        log_tail = "\n\n<b>Log tail:</b>\n<pre>" + _esc("\n".join(tail)) + "</pre>"

    msg = (
        f"🚨 <b>Atlas CRON FAILED</b>\n"
        f"<i>{now}</i>\n\n"
        f"<b>Mode:</b> {_esc(mode)}\n"
        f"<b>Error:</b>\n<pre>{_esc(detail[:1500])}</pre>"
        f"{log_tail}"
    )
    return send_message(msg)


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
    """Send a rich promotion request with before/after metrics."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M")

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
        arrow = "↑" if d > 0 else "↓" if d < 0 else "→"
        icon = "🟢" if d > 0 else "🔴" if d < 0 else "⚪"
        lines.append(f"  {icon} {_esc(metric)}: {b:.4f} → {c:.4f} ({d:+.4f} {arrow})")

    if oos_details:
        lines.extend([
            "",
            "<b>OOS Validation:</b>",
            f"  Test 1 (Time Split): {_esc(str(oos_details.get('test1', '?')))}",
            f"  Test 2 (Perturbation): {_esc(str(oos_details.get('test2', '?')))}",
            f"  Test 3 (Walk-Forward): {_esc(str(oos_details.get('test3', '?')))}",
        ])

    lines.extend([
        "",
        "Reply <b>APPROVE</b> or <b>REJECT</b>.",
    ])

    return send_message("\n".join(lines))


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
