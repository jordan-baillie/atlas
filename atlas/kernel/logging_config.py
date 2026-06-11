"""Atlas Centralised Logging Configuration.

Single source of truth for logging across all Atlas modules.
Replaces the 14 competing `basicConfig()` calls scattered throughout
scripts/, services/, and utils/.

Usage — scripts that run as __main__:
    from atlas.kernel.logging_config import setup_logging
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
from atlas.kernel.paths import PROJECT_ROOT


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
            from atlas.kernel.notify import send_message, _esc
        except ImportError as exc:
            # Can't import telegram at process exit — print to stderr (logger may be torn down).
            print(f"[TelegramErrorCollector] Could not import telegram module: {exc} — skipping batch alert", file=sys.stderr)
            return

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

        # Cross-run throttle: a script that errors every cron run would otherwise flood
        # Telegram. Suppress a repeat of the SAME error set within the throttle window
        # (new/different errors still alert immediately; the SQLite errors table still
        # records every occurrence for the dashboard).
        _sig = _error_batch_signature(records, script)
        if not _telegram_throttle_ok(_sig):
            print(f"[TelegramErrorCollector] throttled duplicate error alert ({_sig})",
                  file=sys.stderr)
            return
        send_message("\n".join(lines))




# ═══════════════════════════════════════════════════════════════
# Cross-run Telegram throttle — stop a recurring error from flooding
# ═══════════════════════════════════════════════════════════════

TELEGRAM_ERROR_THROTTLE_SEC = 4 * 3600  # same error-set alerts at most once per 4h


def _error_batch_signature(records, script: str) -> str:
    """Stable signature for a batch of error records (script + distinct logger|message set)."""
    import hashlib
    sigs = sorted({f"{r.name}|{r.getMessage()[:120]}" for r in records})
    h = hashlib.sha1(("||".join(sigs)).encode("utf-8", "replace")).hexdigest()[:16]
    return f"{script}:{h}"


def _telegram_throttle_ok(signature: str, window_sec: int = TELEGRAM_ERROR_THROTTLE_SEC) -> bool:
    """True if this error signature has NOT been Telegram-alerted within window_sec.

    Persists last-sent timestamps in data/.telegram_error_throttle.json. Fail-open:
    any error in the throttle logic returns True (send) so real alerts are never lost.
    """
    import json as _json
    import time as _time
    try:
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "data", ".telegram_error_throttle.json")
        now = _time.time()
        state = {}
        try:
            with open(path) as _f:
                state = _json.load(_f)
        except Exception:
            state = {}
        last = float(state.get(signature, 0) or 0)
        if now - last < window_sec:
            return False
        state[signature] = now
        # prune entries older than 7 days to keep the file small
        state = {k: v for k, v in state.items() if now - float(v or 0) < 7 * 86400}
        tmp = path + ".tmp"
        with open(tmp, "w") as _f:
            _json.dump(state, _f)
        os.replace(tmp, path)
        return True
    except Exception:
        return True  # fail-open: never suppress a real alert due to a throttle bug


# ═══════════════════════════════════════════════════════════════
# SQLite error writer — persists every ERROR+ to data/atlas.db:errors
# ═══════════════════════════════════════════════════════════════

class SQLiteErrorWriter(logging.Handler):
    """Persist every ERROR+ log record to data/atlas.db:errors.

    Writes synchronously on each emit() call.  Thread-safe via instance lock.
    Fails-open: if SQLite is unavailable the record is silently dropped — this
    handler must never crash the calling process.

    yfinance.* records are filtered identically to TelegramErrorCollector.

    Dedup: if a fingerprint already exists in errors, bumps occurrence_count
    instead of inserting a new row (true occurrence count for severity
    prioritisation).

    Args:
        script_name: Used as the ``service`` column value.
        db_path: Override the database path — for tests only.  Production code
            leaves this None which respects the ``atlas.db._db_path_override``
            pattern (and falls back to the production DB_PATH).
    """

    DEDUP_WINDOW_SEC = 300  # 5 min — Engineering report §1.4

    def __init__(self, script_name: str = "", db_path: str | None = None) -> None:
        super().__init__(level=logging.ERROR)
        self.script_name = script_name
        self._db_path = db_path
        self._lock = threading.Lock()
        self._hostname = self._safe_hostname()
        self._pid = os.getpid()

    @staticmethod
    def _safe_hostname() -> str | None:
        try:
            import socket
            return socket.gethostname()
        except OSError:
            # Hostname unavailable in restricted environments — acceptable silent failure.
            return None

    def emit(self, record: logging.LogRecord) -> None:
        # Filter yfinance noise — mirrors TelegramErrorCollector
        if record.name.startswith("yfinance"):
            return
        try:
            self._write_record(record)
        except Exception as exc:
            # Never crash the calling process due to a logging-handler failure.
            # Use handleError (Python logging convention) to route to sys.stderr without recursion.
            self.handleError(record)

    def _write_record(self, record: logging.LogRecord) -> None:
        from atlas.kernel.error_fingerprint import compute_fingerprint  # avoids circular import
        from atlas import db as atlas_db  # local import

        exc_type: str | None = None
        exc_msg: str | None = None
        tb: str | None = None
        if record.exc_info and record.exc_info[1] is not None:
            exc_type = type(record.exc_info[1]).__name__
            exc_msg = str(record.exc_info[1])[:1000]
            try:
                import traceback as _tb
                tb = "".join(_tb.format_exception(*record.exc_info))[:8000]
            except Exception as exc:
                # traceback.format_exception failed (e.g., exotic exception type) — degrade gracefully.
                # Can't use logger here (inside emit handler — recursion risk). Print to stderr.
                tb = f"<traceback format error: {exc}>"
                print(f"[SQLiteErrorWriter] traceback.format_exception failed: {exc}", file=sys.stderr)

        fp = compute_fingerprint(
            exc_type=exc_type,
            message=record.getMessage(),
            file_path=getattr(record, "pathname", None),
            line_number=getattr(record, "lineno", None),
        )
        ts = datetime.utcfromtimestamp(record.created).strftime("%Y-%m-%dT%H:%M:%S")

        with self._lock:
            with atlas_db.get_db(self._db_path) as conn:
                row = conn.execute(
                    "SELECT id, occurrence_count FROM errors WHERE fingerprint = ?",
                    (fp,),
                ).fetchone()
                if row is not None:
                    # Bump count — we want a true occurrence count regardless of
                    # the 5-min dedup window (window is for re-evaluation cadence)
                    conn.execute(
                        "UPDATE errors"
                        " SET occurrence_count = occurrence_count + 1,"
                        "     last_seen_ts = ?,"
                        "     ts = ?"
                        " WHERE id = ?",
                        (ts, ts, row["id"]),
                    )
                    return
                # New fingerprint — full insert
                conn.execute(
                    """INSERT INTO errors (
                        fingerprint, first_seen_ts, last_seen_ts, occurrence_count, ts,
                        source, service, level, logger_name, message,
                        exc_type, exc_message, traceback,
                        file_path, line_number, function_name, pid, hostname,
                        classification, tier, remediation_status
                    ) VALUES (
                        ?, ?, ?, 1, ?,
                        'python_logger', ?, ?, ?, ?,
                        ?, ?, ?,
                        ?, ?, ?, ?, ?,
                        'UNCLASSIFIED', 99, 'NEW'
                    )""",
                    (
                        fp, ts, ts, ts,
                        self.script_name or None,
                        record.levelname,
                        record.name,
                        record.getMessage()[:8000],
                        exc_type,
                        exc_msg,
                        tb,
                        getattr(record, "pathname", None),
                        getattr(record, "lineno", None),
                        getattr(record, "funcName", None),
                        self._pid,
                        self._hostname,
                    ),
                )


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
    force_console: bool = False,
) -> logging.Logger:
    """Configure logging for an Atlas script/service.

    Args:
        script_name: Name for the log context (e.g. "eod_settlement").
        extra_log_file: Additional log file name (e.g. "eod" → logs/eod.log).
        level: Minimum log level (default INFO).
        telegram_errors: If True, collect ERROR+ records and send to
            Telegram as a batch at process exit.
        force_console: If True, add a StreamHandler to *stdout* in addition
            to the standard stderr handler.  Use this for scripts whose
            output is redirected to a log file via ``>> file`` (which only
            captures stdout by default) rather than ``>> file 2>&1``.
            overlay/cron.py uses this to ensure all log lines appear in
            the weekly ``overlay_eval_YYYYMMDD.log`` file.

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

    # Optional stdout handler — used when the calling process redirects stdout
    # to a log file via `>> file` without `2>&1`.  Adds a second stream so that
    # log lines are captured regardless of which stream the redirect targets.
    if force_console:
        stdout_h = logging.StreamHandler(sys.stdout)
        stdout_h.setFormatter(fmt)
        root.addHandler(stdout_h)

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
    # Never send Telegram alerts from pytest runs — tests intentionally exercise
    # failure paths (bad credentials, rejected orders, invalid dates) which
    # generate ERROR-level log records that must not surface as production alerts.
    _in_pytest = "pytest" in sys.modules or bool(os.getenv("PYTEST_CURRENT_TEST"))
    if telegram_errors and not _in_pytest:
        _collector = TelegramErrorCollector(script_name=script_name)
        root.addHandler(_collector)
        atexit.register(_collector.flush_to_telegram)

    # SQLite error writer — persists every ERROR+ to data/atlas.db:errors.
    # Gated by _in_pytest (same rule as Telegram) AND the env-var kill-switch.
    if not _in_pytest and os.environ.get("ATLAS_SQLITE_ERROR_WRITER", "1") != "0":
        try:
            _sqlite_writer = SQLiteErrorWriter(script_name=script_name)
            root.addHandler(_sqlite_writer)
        except Exception as exc:
            # Never let logging setup fail because SQLite is unavailable.
            # Print to stderr — logger isn't fully configured yet at this point.
            print(f"[setup_logging] SQLiteErrorWriter init failed (non-fatal): {exc}", file=sys.stderr)

    # Suppress noisy third-party loggers
    for noisy in ["urllib3", "peewee", "httpx", "httpcore"]:
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
