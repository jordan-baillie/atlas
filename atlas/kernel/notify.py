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
    from atlas.kernel.notify import send_message, send_premarket_summary, send_postclose_summary, send_error

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
from atlas.kernel.paths import PROJECT_ROOT

logger = logging.getLogger(__name__)


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

def _persist_outbound(
    chat_id: str,
    body: str,
    *,
    parse_mode: Optional[str],
    message_id: Optional[int],
    api_status: Optional[int],
    api_error: Optional[str],
) -> None:
    """Wrapper around atlas.db.record_telegram_outbound with import-time fail-open.

    Imports lazily so utils/telegram.py remains importable when db layer is
    unavailable (e.g. fresh checkout before init_db, or DB file missing).
    """
    try:
        from atlas.db import record_telegram_outbound
        record_telegram_outbound(
            chat_id,
            body,
            parse_mode=parse_mode,
            message_id=message_id,
            api_status=api_status,
            api_error=api_error,
        )
    except Exception as e:  # noqa: BLE001 — observability never breaks send
        logger.warning("telegram outbound capture skipped: %s", e)


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

    Side effect: persists every send attempt to telegram_messages table (fail-open).
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
            resp_body = json.loads(resp.read())
            if resp_body.get("ok"):
                logger.info("Telegram message sent (chat_id=%s)", chat_id)
                msg_id = None
                try:
                    msg_id = resp_body.get("result", {}).get("message_id")
                except Exception:
                    pass
                _persist_outbound(
                    chat_id, text,
                    parse_mode=parse_mode,
                    message_id=msg_id,
                    api_status=200,
                    api_error=None,
                )
                return True
            logger.warning("Telegram API returned ok=false: %s", resp_body)
            _persist_outbound(
                chat_id, text,
                parse_mode=parse_mode,
                message_id=None,
                api_status=200,
                api_error=f"api_ok_false: {resp_body!r}"[:500],
            )
            return False
    except urllib.error.HTTPError as e:
        err_body = e.read().decode(errors="replace")
        logger.error("Telegram HTTP %d: %s", e.code, err_body)
        _persist_outbound(
            chat_id, text,
            parse_mode=parse_mode,
            message_id=None,
            api_status=e.code,
            api_error=err_body[:500],
        )
        return False
    except Exception as e:
        logger.error("Telegram send error: %s", e)
        _persist_outbound(
            chat_id, text,
            parse_mode=parse_mode,
            message_id=None,
            api_status=None,
            api_error=repr(e)[:500],
        )
        return False




# ---------------------------------------------------------------------------
# Simple notify() — canonical convenience wrapper for scripts and health checks
# ---------------------------------------------------------------------------

def notify(
    message: str,
    *,
    level: str | None = None,
    category: str = "general",
    parse_mode: str = "HTML",
) -> bool:
    """Send a Telegram notification with optional level prefix.

    Convenience wrapper around send_message() that:
      - Prepends a level emoji (CRITICAL=🚨, WARNING=⚠️, INFO=ℹ️) if level provided
      - Logs failures via the module logger (does NOT raise)
      - Returns True on success, False on any failure

    Args:
        message: Body of the Telegram message (HTML or plain text per parse_mode)
        level: Optional severity — one of 'CRITICAL', 'WARNING', 'INFO', or None
        category: Optional category tag for log lines (e.g. 'health', 'research')
        parse_mode: Telegram parse_mode (default 'HTML'; pass '' for plain text)

    Returns:
        True if the Telegram API accepted the message, False otherwise.
    """
    _LEVEL_PREFIX = {
        "CRITICAL": "🚨 ",
        "WARNING": "⚠️ ",
        "INFO": "ℹ️ ",
    }
    prefix = _LEVEL_PREFIX.get(level or "", "")
    body = f"{prefix}{message}" if prefix else message
    try:
        return send_message(body, parse_mode=parse_mode)
    except Exception as e:
        logger.warning("telegram.notify failed (category=%s level=%s): %s", category, level, e)
        return False

# ---------------------------------------------------------------------------
# Formatted alerts
# ---------------------------------------------------------------------------

def _esc(text: str) -> str:
    """Escape HTML special characters."""
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def tg_escape(s: object) -> str:
    """Escape a string for safe inclusion in a Telegram HTML message.

    Use this for ANY dynamic/user-supplied content inserted into HTML messages
    (error strings, broker messages, ticker descriptions, exception text, etc.).
    Telegram HTML only supports a small subset of tags — any unrecognised
    ``<tag>`` causes a 400 parse error.

    Escapes: ``&`` → ``&amp;``, ``<`` → ``&lt;``, ``>`` → ``&gt;``, ``"`` → ``&quot;``
    Returns ``""`` for None input so callers never need to guard for None.

    Example::

        send_message(f"Error for {ticker}: {tg_escape(broker_error_msg)}")
    """
    if s is None:
        return ""
    return (
        str(s)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )
