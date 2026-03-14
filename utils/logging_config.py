"""Atlas Centralised Logging Configuration.

Single source of truth for logging across all Atlas modules.
Replaces the 14 competing `basicConfig()` calls scattered throughout
scripts/, services/, and utils/.

Usage — scripts that run as __main__:
    from utils.logging_config import setup_logging
    setup_logging("eod_settlement")          # logs to atlas.log + stderr
    setup_logging("eod_settlement", "eod")   # also logs to eod.log

Usage — library modules (no setup needed):
    import logging
    logger = logging.getLogger(__name__)      # inherits root config

Telegram error handler:
    Automatically captures ERROR+ log records and batches them into a
    single Telegram alert at process exit (via atexit).  This means
    broker errors, data errors, etc. that were previously invisible
    now surface in Telegram without any code changes in calling modules.

    To disable for a specific script (e.g. tests):
        setup_logging("test_script", telegram_errors=False)
"""

from __future__ import annotations

import atexit
import json
import logging
import os
import sys
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
LOG_DIR = PROJECT_ROOT / "logs"
LOG_DIR.mkdir(exist_ok=True)

# Standard format used everywhere
LOG_FMT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
LOG_DATE_FMT = "%Y-%m-%d %H:%M:%S"

# Prevent double-setup (basicConfig is idempotent but our setup isn't)
_setup_done = False


# ═══════════════════════════════════════════════════════════════
# Telegram error collector — batches ERROR+ records for one alert
# ═══════════════════════════════════════════════════════════════

class TelegramErrorCollector(logging.Handler):
    """Collects ERROR+ log records during a process run.

    At process exit (atexit), sends a single batched Telegram alert
    if any errors were collected.  This replaces the pattern of
    individual try/except blocks needing to import and call send_error.

    Thread-safe.  Max 20 records to avoid Telegram message overflow.
    """

    MAX_RECORDS = 20

    def __init__(self, script_name: str = ""):
        super().__init__(level=logging.ERROR)
        self.script_name = script_name
        self.records: list[logging.LogRecord] = []
        self._lock = threading.Lock()
        self._flushed = False

    def emit(self, record: logging.LogRecord):
        # yfinance logs routine download failures (delisted tickers, 404s) at
        # ERROR level. These are data-quality issues, not system errors, and
        # must never be forwarded to Telegram as operator alerts.
        if record.name.startswith("yfinance"):
            return
        with self._lock:
            if len(self.records) < self.MAX_RECORDS:
                self.records.append(record)

    def flush_to_telegram(self):
        """Send collected errors as one Telegram message."""
        with self._lock:
            if self._flushed or not self.records:
                return
            self._flushed = True
            records = list(self.records)

        try:
            from utils.telegram import send_message, _esc
        except Exception:
            return  # can't send — silently give up

        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        n = len(records)
        script = self.script_name or "unknown"

        lines = [
            f"🚨 <b>Atlas Errors [{_esc(script)}]</b>",
            f"<i>{now}</i>",
            f"",
            f"<b>{n} error{'s' if n > 1 else ''} during run:</b>",
            "",
        ]

        for i, rec in enumerate(records[:10], 1):
            ts = datetime.fromtimestamp(rec.created).strftime("%H:%M:%S")
            name = rec.name.replace("atlas.", "")
            msg = rec.getMessage()[:200]
            lines.append(f"<b>{i}.</b> [{ts}] <code>{_esc(name)}</code>")
            lines.append(f"   {_esc(msg)}")
            if rec.exc_info and rec.exc_info[1]:
                exc_type = type(rec.exc_info[1]).__name__
                exc_msg = str(rec.exc_info[1])[:150]
                lines.append(f"   <i>{_esc(exc_type)}: {_esc(exc_msg)}</i>")
            lines.append("")

        if n > 10:
            lines.append(f"<i>… +{n - 10} more errors (check logs)</i>")

        # Add runbook hint based on error patterns
        hints = _classify_errors(records)
        if hints:
            lines.append("")
            lines.append("<b>Likely cause:</b>")
            for h in hints:
                lines.append(f"  → {h}")

        send_message("\n".join(lines))


def _classify_errors(records: list[logging.LogRecord]) -> list[str]:
    """Classify errors into actionable categories for the alert."""
    hints = []
    messages = " ".join(r.getMessage().lower() for r in records)
    names = " ".join(r.name.lower() for r in records)

    if "connect" in messages or "broker" in messages:
        hints.append("🔌 Broker connection — check broker connectivity")
    if "timeout" in messages:
        hints.append("⏱ Timeout — network issue or API rate limit")
    if "price" in messages and ("fetch" in messages or "download" in messages):
        hints.append("📊 Price data — check yfinance / market hours")
    if "permission" in messages or "credential" in messages or "token" in messages:
        hints.append("🔑 Auth — check ~/.atlas-secrets.json")
    if "disk" in messages or "space" in messages or "no space" in messages:
        hints.append("💾 Disk space — run weekly_maintenance.sh")
    if "halted" in messages or "drawdown" in messages:
        hints.append("⛔ Risk halt — daily drawdown limit breached, manual review needed")
    if "config" in messages and ("missing" in messages or "invalid" in messages):
        hints.append("⚙️ Config — check config/active/*.json")

    return hints


# Global collector reference (for manual flush if needed)
_collector: Optional[TelegramErrorCollector] = None


def get_error_collector() -> Optional[TelegramErrorCollector]:
    """Get the active error collector (if setup with telegram_errors=True)."""
    return _collector


# ═══════════════════════════════════════════════════════════════
# Main setup function
# ═══════════════════════════════════════════════════════════════

def setup_logging(
    script_name: str = "",
    extra_log_file: str = "",
    level: int = logging.INFO,
    telegram_errors: bool = True,
) -> logging.Logger:
    """Configure logging for an Atlas script/service.

    Args:
        script_name: Name for the log context (e.g. "eod_settlement").
        extra_log_file: Additional log file name (e.g. "eod" → logs/eod.log).
        level: Minimum log level (default INFO).
        telegram_errors: If True, collect ERROR+ records and send to
            Telegram as a batch at process exit.

    Returns:
        Root logger (or named logger if script_name given).

    Idempotent — calling twice with the same args is safe.
    """
    global _setup_done, _collector

    if _setup_done:
        return logging.getLogger(f"atlas.{script_name}" if script_name else "atlas")

    # ── Root logger config ──────────────────────────────────────
    root = logging.getLogger()
    root.setLevel(level)

    # Clear any pre-existing handlers (from competing basicConfig calls)
    root.handlers.clear()

    # Formatter
    fmt = logging.Formatter(LOG_FMT, datefmt=LOG_DATE_FMT)

    # stderr handler
    stderr_h = logging.StreamHandler(sys.stderr)
    stderr_h.setFormatter(fmt)
    root.addHandler(stderr_h)

    # Main atlas.log (append)
    main_h = logging.FileHandler(LOG_DIR / "atlas.log", mode="a")
    main_h.setFormatter(fmt)
    root.addHandler(main_h)

    # Optional extra log file
    if extra_log_file:
        fname = extra_log_file if extra_log_file.endswith(".log") else f"{extra_log_file}.log"
        extra_h = logging.FileHandler(LOG_DIR / fname, mode="a")
        extra_h.setFormatter(fmt)
        root.addHandler(extra_h)

    # ── Telegram error collector ────────────────────────────────
    if telegram_errors:
        _collector = TelegramErrorCollector(script_name=script_name)
        root.addHandler(_collector)
        atexit.register(_collector.flush_to_telegram)

    # Suppress noisy third-party loggers
    for noisy in ["urllib3", "peewee"]:
        logging.getLogger(noisy).setLevel(logging.WARNING)
    # yfinance logs routine download failures (delisted tickers, 404s) at ERROR
    # level. Suppress to CRITICAL so they never appear in stderr or Telegram.
    # The backtest engine already handles missing data gracefully (tickers with
    # no data are skipped), so silencing these is safe.
    logging.getLogger("yfinance").setLevel(logging.CRITICAL)

    _setup_done = True

    logger = logging.getLogger(f"atlas.{script_name}" if script_name else "atlas")
    logger.debug("Logging configured: script=%s, level=%s, telegram=%s",
                 script_name, logging.getLevelName(level), telegram_errors)
    return logger
