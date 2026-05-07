"""Centralised alert dispatch — thin facade over utils.telegram.

Goal: provide a single seam for alert routing, levels, dedup, and future
multi-channel support.  All current alerts go through Telegram via
utils.telegram, but the AlertManager API hides this so future channels
(Slack, email, in-app) can be added without changing call sites.

Findings — utils.telegram.notify actual signature (read 2026-05-07):
    notify(message: str, *, level: str | None = None,
           category: str = "general", parse_mode: str = "HTML") -> bool

  Differences from spec-assumed signature:
    - Single "message" param (no title/body split)
    - "level" is a str ("CRITICAL"/"WARNING"/"INFO") not an int
    - No "silent" parameter on notify() — only send_message() supports it
    - Has "category" and "parse_mode" kwargs instead

  Adaptation applied in AlertManager.notify():
    - Combines title + body into a single HTML message string
    - Maps AlertLevel enum to CRITICAL/WARNING/INFO strings
    - "silent" from notify() signature is forwarded to send_message()
      directly when needed; not passed to utils.telegram.notify()
"""
from __future__ import annotations

from enum import IntEnum
from typing import Optional
import logging

logger = logging.getLogger(__name__)


class AlertLevel(IntEnum):
    """Alert severity.  Lower = more urgent."""
    CRITICAL = 0   # System down, immediate action required
    IMPORTANT = 1  # User attention needed
    INFO = 2       # FYI


# Map AlertLevel to the string level that utils.telegram.notify() understands.
_LEVEL_STR: dict[int, str] = {
    AlertLevel.CRITICAL: "CRITICAL",
    AlertLevel.IMPORTANT: "WARNING",
    AlertLevel.INFO: "INFO",
}


class AlertManager:
    """Centralised alert dispatch.  Wraps utils.telegram for now;
    future versions can route to multiple channels.

    Usage::

        am = get_alert_manager()
        am.send("Raw HTML message")
        am.notify("Daily summary", body=text, level=AlertLevel.INFO)
        am.info("All good")
        am.important("Check required", body="details...")
        am.critical("Kill switch tripped", body="...")
    """

    def __init__(self, *, telegram_enabled: bool = True) -> None:
        self.telegram_enabled = telegram_enabled

    # ------------------------------------------------------------------ #
    # Raw send                                                             #
    # ------------------------------------------------------------------ #

    def send(self, text: str, *, parse_mode: str = "HTML",
             silent: bool = False) -> bool:
        """Raw send — no extra formatting.  Returns True on success."""
        if not self.telegram_enabled:
            logger.info("alert (telegram disabled): %s", text[:200])
            return True
        try:
            from utils.telegram import send_message  # lazy import
            return bool(send_message(text, parse_mode=parse_mode, silent=silent))
        except Exception as exc:
            logger.warning("AlertManager.send failed: %s", exc)
            return False

    # ------------------------------------------------------------------ #
    # Formatted notify                                                     #
    # ------------------------------------------------------------------ #

    def notify(
        self,
        title: str,
        body: str = "",
        *,
        level: AlertLevel = AlertLevel.INFO,
        silent: bool = False,
    ) -> bool:
        """Formatted notify with title + optional body.

        Combines title and body into a single HTML message, then routes
        through utils.telegram.notify() with the appropriate level string.

        Note: *silent* is accepted for API symmetry but utils.telegram.notify
        does not support it.  When silent=True the message is sent as a raw
        send_message() call so the flag is honoured end-to-end.
        """
        if not self.telegram_enabled:
            logger.info(
                "alert (telegram disabled): %s — %s",
                title, (body or "")[:200],
            )
            return True
        try:
            message = (f"<b>{title}</b>\n{body}") if body else title
            level_str = _LEVEL_STR.get(int(level), "INFO")
            if silent:
                # utils.telegram.notify() has no silent param — use send_message
                from utils.telegram import send_message
                _PREFIX = {"CRITICAL": "🚨 ", "WARNING": "⚠️ ", "INFO": "ℹ️ "}
                prefix = _PREFIX.get(level_str, "")
                return bool(
                    send_message(f"{prefix}{message}", silent=True)
                )
            from utils.telegram import notify as _tg_notify
            return bool(_tg_notify(message, level=level_str))
        except Exception as exc:
            logger.warning("AlertManager.notify failed: %s", exc)
            return False

    # ------------------------------------------------------------------ #
    # Convenience level methods                                            #
    # ------------------------------------------------------------------ #

    def info(self, title: str, body: str = "") -> bool:
        """Send an INFO-level notification."""
        return self.notify(title, body, level=AlertLevel.INFO)

    def important(self, title: str, body: str = "") -> bool:
        """Send an IMPORTANT-level notification."""
        return self.notify(title, body, level=AlertLevel.IMPORTANT)

    def critical(self, title: str, body: str = "") -> bool:
        """Send a CRITICAL-level notification."""
        return self.notify(title, body, level=AlertLevel.CRITICAL)


# ------------------------------------------------------------------ #
# Process-wide singleton                                               #
# ------------------------------------------------------------------ #

_INSTANCE: Optional[AlertManager] = None


def get_alert_manager() -> AlertManager:
    """Return the process-wide AlertManager singleton.

    Creates the instance on first call with default settings.  Replace
    via module-level assignment when needed in tests::

        import alerting.manager as _am
        _am._INSTANCE = AlertManager(telegram_enabled=False)
    """
    global _INSTANCE
    if _INSTANCE is None:
        _INSTANCE = AlertManager()
    return _INSTANCE
