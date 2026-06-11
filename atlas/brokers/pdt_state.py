"""PDT (Pattern Day Trading) backoff state — ticker-level expiry.

Provides atomic, file-backed deferral tracking so every order-submit site
can pre-check and post-record PDT denials without hammering Alpaca.

State file: data/pdt_state.json  (distinct from the legacy
            data/pdt_deferred_state.json which uses ticker::market_id keys
            and a retry-window approach).

Key:   ticker (str)
Value: ISO 8601 expiry datetime string — entries whose expiry <= now are
       treated as cleared (same-day-entry restriction has lifted).

RTH close = 21:00 UTC (4 PM EST / 5 PM EDT): Alpaca's PDT same-day window
resets after this point, so deferred orders can be placed the following session.

Typical usage
-------------
Before submit (SELL orders):
    if is_pdt_deferred(ticker):
        logger.info("pdt_skip: %s until_rth_close", ticker)
        return  # skip this position

After PDT-denied response (error code 40310100):
    if "40310100" in str(err):
        set_pdt_deferred(ticker)   # records expiry = today 21:00 UTC
        logger.warning("pdt_deferred: %s until 21:00 UTC", ticker)
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import date, datetime, time, timezone
from pathlib import Path
from atlas.kernel.paths import PROJECT_ROOT

logger = logging.getLogger("atlas.pdt_state")

_PROJECT = PROJECT_ROOT
_STATE_FILE = _PROJECT / "data" / "pdt_state.json"

# Alpaca PDT denial code (also defined in broker.py — keep in sync)
_PDT_ERROR_CODE = "40310100"


# ── Time helpers ─────────────────────────────────────────────────────────────

def _rth_close_today() -> datetime:
    """Return today's US equity RTH close in UTC: 21:00 UTC (4 PM EST / 5 PM EDT).

    Alpaca's pattern-day-trading same-session window resets after this time,
    so deferred orders can safely be placed after 21:00 UTC on the denial day.
    """
    return datetime.combine(date.today(), time(21, 0), tzinfo=timezone.utc)


def _now_utc() -> datetime:
    """Current time, timezone-aware UTC."""
    return datetime.now(tz=timezone.utc)


# ── File I/O ─────────────────────────────────────────────────────────────────

def _load(path: Path = _STATE_FILE) -> dict[str, str]:
    """Load state dict from JSON file.  Returns {} on missing file or parse error."""
    try:
        if path.exists():
            with open(path) as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
    except Exception as exc:
        logger.warning("pdt_state: load failed (%s): %s", path, exc)
    return {}


def _save(state: dict[str, str], path: Path = _STATE_FILE) -> None:
    """Atomically persist state to JSON via tmp-file + os.replace."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(state, f, indent=2)
                f.write("\n")
            os.replace(tmp_path, str(path))
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
    except Exception as exc:
        logger.warning("pdt_state: save failed (%s): %s", path, exc)


# ── Public API ───────────────────────────────────────────────────────────────

def is_pdt_deferred(ticker: str, *, _path: "Path | None" = None) -> bool:
    """Return True if *ticker* is PDT-deferred and the expiry has NOT passed.

    Reads the state file fresh on every call (no module-level cache) to stay
    correct across multiple processes and cron runs.

    Parameters
    ----------
    ticker : Plain US equity symbol, e.g. "AVGO".
    _path  : Override state file path (for testing).
    """
    path = _path if _path is not None else _STATE_FILE
    state = _load(path)
    expiry_str = state.get(ticker)
    if not expiry_str:
        return False
    try:
        expiry = datetime.fromisoformat(expiry_str)
        # Normalise naive timestamps (legacy entries) to UTC
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=timezone.utc)
        return _now_utc() < expiry
    except Exception as exc:
        logger.debug("pdt_state: bad expiry for %s (%r): %s", ticker, expiry_str, exc)
        return False


def set_pdt_deferred(
    ticker: str,
    expiry: datetime | None = None,
    *,
    _path: "Path | None" = None,
) -> None:
    """Record *ticker* as PDT-deferred until *expiry* (default: today 21:00 UTC).

    Idempotent — if the ticker is already recorded with a later expiry the
    existing entry is preserved; a new denial with an earlier expiry does not
    shorten the backoff window.

    Parameters
    ----------
    ticker : Plain US equity symbol.
    expiry : When to lift the deferral.  Defaults to today's RTH close (21:00 UTC).
    _path  : Override state file path (for testing).
    """
    if expiry is None:
        expiry = _rth_close_today()
    # Ensure timezone-aware
    if expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=timezone.utc)

    path = _path if _path is not None else _STATE_FILE
    state = _load(path)
    existing_str = state.get(ticker)
    if existing_str:
        try:
            existing = datetime.fromisoformat(existing_str)
            if existing.tzinfo is None:
                existing = existing.replace(tzinfo=timezone.utc)
            # Don't shorten an existing deferral
            if existing > expiry:
                logger.debug(
                    "pdt_state: %s already deferred until %s (not shortening to %s)",
                    ticker, existing.isoformat(), expiry.isoformat(),
                )
                return
        except Exception:
            pass  # overwrite malformed entry

    state[ticker] = expiry.isoformat()
    _save(state, path)
    logger.info("pdt_state: set %s deferred until %s", ticker, expiry.isoformat())


def clear_expired(*, _path: "Path | None" = None) -> list[str]:
    """Remove all entries whose expiry <= now.

    Returns list of tickers that were cleared.  Safe to call frequently —
    if nothing is expired, no file write occurs.
    """
    path = _path if _path is not None else _STATE_FILE
    state = _load(path)
    now = _now_utc()
    cleared: list[str] = []

    for ticker, expiry_str in list(state.items()):
        try:
            expiry = datetime.fromisoformat(expiry_str)
            if expiry.tzinfo is None:
                expiry = expiry.replace(tzinfo=timezone.utc)
            if expiry <= now:
                cleared.append(ticker)
                del state[ticker]
        except Exception:
            # Malformed entry — clear it
            cleared.append(ticker)
            del state[ticker]

    if cleared:
        _save(state, path)
        logger.info("pdt_state: cleared expired entries: %s", cleared)
    return cleared
