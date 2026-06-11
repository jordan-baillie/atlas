"""Price arbiter — decides between Tiingo and Alpaca when they disagree.

Authority is Alpaca (execution venue). If disagreement exceeds halt_pct,
the ticker is flagged in a module-level set; new entries should check
is_ticker_halted(ticker) and skip.
"""
import json
import logging
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from atlas.kernel.paths import CONFIG_DIR, DATA_DIR

logger = logging.getLogger(__name__)
_CONFIG_PATH = CONFIG_DIR / "price_arbiter.json"
_THROTTLE_PATH = DATA_DIR / "price_arbiter_alert_throttle.json"
_ALERT_THROTTLE_HOURS = 6
_HALTED_TICKERS: set[str] = set()

_DEFAULT_CFG = {"warn_pct": 2.0, "halt_pct": 5.0, "authority_on_mismatch": "alpaca"}


def _load_config() -> dict:
    try:
        with open(_CONFIG_PATH) as f:
            return json.load(f)
    except Exception as e:
        logger.warning(f"Price-arbiter config load failed, using defaults: {e}")
        return dict(_DEFAULT_CFG)


def _should_send_alert(ticker: str) -> bool:
    """Return True if a Telegram alert should be sent for this ticker.

    Uses a file-based throttle so repeated process invocations (cron) do not
    spam.  Reads and writes the JSON atomically (tempfile + rename).  On any
    IO error the function defaults to True (fail-open) so alerts are never
    silently lost.
    """
    ticker_upper = ticker.upper()
    try:
        throttle: dict = {}
        if _THROTTLE_PATH.exists():
            try:
                with open(_THROTTLE_PATH) as f:
                    throttle = json.load(f)
            except (json.JSONDecodeError, OSError):
                throttle = {}

        last_ts_str = throttle.get(ticker_upper)
        if last_ts_str:
            try:
                last_ts = datetime.fromisoformat(last_ts_str)
                # Ensure both are timezone-aware for comparison
                if last_ts.tzinfo is None:
                    last_ts = last_ts.replace(tzinfo=timezone.utc)
                if datetime.now(timezone.utc) - last_ts < timedelta(hours=_ALERT_THROTTLE_HOURS):
                    return False
            except (ValueError, TypeError):
                pass  # Malformed ts — treat as absent, allow send

        # Update the throttle file atomically
        throttle[ticker_upper] = datetime.now(timezone.utc).isoformat()
        _THROTTLE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = _THROTTLE_PATH.with_suffix(".tmp")
        with open(tmp, "w") as f:
            json.dump(throttle, f, indent=2)
        tmp.replace(_THROTTLE_PATH)
        return True
    except Exception as e:
        logger.warning("price_arbiter throttle IO error: %s — defaulting to send", e)
        return True


def _send_telegram_bg(msg: str) -> None:
    """Fire a Telegram CRITICAL alert in a daemon thread.

    Extracted as a module-level function so tests can patch it cleanly via
    ``patch("atlas.brokers.price_arbiter._send_telegram_bg", ...)``.
    """
    def _run() -> None:
        try:
            from atlas.kernel.notify import send_message
            send_message(f"\U0001f6a8 {msg}")
        except Exception as e:
            logger.warning("telegram alert failed: %s", e)
    threading.Thread(target=_run, daemon=True, name="price_arbiter_alert").start()


def arbitrate(ticker: str, tiingo_price: float, alpaca_price: float) -> float:
    """Return the authoritative price. Updates halt set if spread > halt_pct."""
    cfg = _load_config()
    if tiingo_price <= 0 and alpaca_price <= 0:
        return 0.0
    if tiingo_price <= 0:
        return alpaca_price
    if alpaca_price <= 0:
        return tiingo_price

    spread_pct = abs(tiingo_price - alpaca_price) / alpaca_price * 100
    authority_price = alpaca_price if cfg["authority_on_mismatch"] == "alpaca" else tiingo_price

    if spread_pct >= cfg["halt_pct"]:
        _HALTED_TICKERS.add(ticker.upper())
        msg = (
            f"CRITICAL price halt: {ticker} Tiingo=${tiingo_price:.2f} "
            f"Alpaca=${alpaca_price:.2f} spread={spread_pct:.2f}% \u2014 BLOCKING NEW ENTRIES"
        )
        # RTH gating — outside Regular Trading Hours, large divergence is
        # almost always a session-boundary artifact (Alpaca post-market
        # last trade vs Tiingo prev close), not a data-integrity issue.
        # Still mark the ticker halted, still log loudly — but don't page
        # the operator.
        try:
            from atlas.kernel.market_hours import is_rth
            within_rth = is_rth()
        except Exception as e:
            logger.warning("price_arbiter: is_rth() failed (%s) — defaulting to RTH", e)
            within_rth = True

        if within_rth:
            logger.critical(msg)
            if _should_send_alert(ticker):
                _send_telegram_bg(msg)
        else:
            logger.warning(
                "price divergence outside RTH — ticker=%s tiingo=$%.2f alpaca=$%.2f spread=%.2f%% (not alerting)",
                ticker, tiingo_price, alpaca_price, spread_pct,
            )
    elif spread_pct >= cfg["warn_pct"]:
        logger.info(
            "price_arbiter %s: Tiingo=$%.2f Alpaca=$%.2f spread=%.2f%% (using %s)",
            ticker, tiingo_price, alpaca_price, spread_pct, cfg["authority_on_mismatch"],
        )
    return authority_price


def is_ticker_halted(ticker: str) -> bool:
    return ticker.upper() in _HALTED_TICKERS


def halted_tickers() -> set[str]:
    return set(_HALTED_TICKERS)


def clear_halts() -> None:
    """Used by tests or a manual operator reset."""
    _HALTED_TICKERS.clear()
