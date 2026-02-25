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


def send_premarket_summary(plan_path: Optional[str] = None, market_id: str = "asx") -> bool:
    """Send a summary of the pre-market plan generation.

    Reads the plan JSON and formats a concise alert with signal count,
    top picks, and risk summary.
    """
    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    if plan_path is None:
        today = datetime.now().strftime("%Y-%m-%d")
        plan_path = str(PROJECT_ROOT / f"paper_engine/plans/plan_{today}.json")

    plan_file = Path(plan_path)
    if not plan_file.exists():
        return send_message(
            f"📊 <b>Atlas Pre-Market [{market_id.upper()}]</b>\n"
            f"<i>{now}</i>\n\n"
            f"⚠️ No plan file generated today.\n"
            f"Check logs for errors."
        )

    with open(plan_file) as f:
        plan = json.load(f)

    entries = plan.get("entries", [])
    exits = plan.get("exits", [])
    meta = plan.get("metadata", plan)

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
        entry_text = "  None"

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
        exit_text = "  None"

    msg = (
        f"📊 <b>Atlas Pre-Market [{market_id.upper()}]</b>\n"
        f"<i>{now}</i>\n\n"
        f"<b>Entries ({len(entries)}):</b>\n{entry_text}\n\n"
        f"<b>Exits ({len(exits)}):</b>\n{exit_text}\n\n"
        f"Status: <b>awaiting approval</b>"
    )
    return send_message(msg)


def send_postclose_summary(market_id: str = "asx") -> bool:
    """Send post-close settlement summary with equity and position snapshot."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    try:
        sys.path.insert(0, str(PROJECT_ROOT))
        from utils.config import get_active_config
        from paper_engine.engine import PaperPortfolio
        from utils.helpers import format_currency

        config = get_active_config(market_id)
        pp = PaperPortfolio(config, market_id=market_id)
        summary = pp.portfolio_summary()

        from markets.registry import get_market
        market = get_market(market_id)
        currency = market.currency

        equity = summary["equity"]
        cash = summary["cash"]
        positions = summary.get("open_positions", [])
        n_pos = len(positions)
        invested = equity - cash

        # Calculate total unrealized PnL
        total_pnl = sum(p.get("unrealized_pnl", 0) for p in positions)
        pnl_sign = "+" if total_pnl >= 0 else ""

        # Top movers
        sorted_pos = sorted(positions, key=lambda p: abs(p.get("unrealized_pnl", 0)), reverse=True)
        if sorted_pos:
            mover_lines = []
            for p in sorted_pos[:5]:
                ticker = p.get("ticker", "?")
                pnl = p.get("unrealized_pnl", 0)
                pnl_pct = p.get("unrealized_pnl_pct", 0)
                icon = "🟢" if pnl >= 0 else "🔴"
                mover_lines.append(
                    f"  {icon} <b>{_esc(ticker)}</b>  {'+' if pnl >= 0 else ''}{format_currency(pnl, currency)} ({'+' if pnl_pct >= 0 else ''}{pnl_pct:.1%})"
                )
            mover_text = "\n".join(mover_lines)
        else:
            mover_text = "  No positions"

        msg = (
            f"📈 <b>Atlas Post-Close [{market_id.upper()}]</b>\n"
            f"<i>{now}</i>\n\n"
            f"<b>Portfolio:</b>\n"
            f"  Equity: <b>{format_currency(equity, currency)}</b>\n"
            f"  Cash: {format_currency(cash, currency)}\n"
            f"  Invested: {format_currency(invested, currency)}\n"
            f"  Positions: {n_pos}\n"
            f"  Unrealised PnL: <b>{pnl_sign}{format_currency(total_pnl, currency)}</b>\n\n"
            f"<b>Top Movers:</b>\n{mover_text}"
        )
        return send_message(msg)

    except Exception as e:
        logger.error("Failed to build post-close summary: %s", e, exc_info=True)
        return send_message(
            f"📈 <b>Atlas Post-Close [{market_id.upper()}]</b>\n"
            f"<i>{now}</i>\n\n"
            f"⚠️ Settlement completed but summary failed:\n"
            f"<code>{_esc(str(e))}</code>"
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
        f"  🚨 Error alerts for failed runs"
    )
