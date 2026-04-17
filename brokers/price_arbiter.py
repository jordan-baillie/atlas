"""Price arbiter — decides between Tiingo and Alpaca when they disagree.

Authority is Alpaca (execution venue). If disagreement exceeds halt_pct,
the ticker is flagged in a module-level set; new entries should check
is_ticker_halted(ticker) and skip.
"""
import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)
_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "price_arbiter.json"
_HALTED_TICKERS: set[str] = set()

_DEFAULT_CFG = {"warn_pct": 2.0, "halt_pct": 5.0, "authority_on_mismatch": "alpaca"}


def _load_config() -> dict:
    try:
        with open(_CONFIG_PATH) as f:
            return json.load(f)
    except Exception:
        return dict(_DEFAULT_CFG)


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
            f"Alpaca=${alpaca_price:.2f} spread={spread_pct:.2f}% — BLOCKING NEW ENTRIES"
        )
        logger.critical(msg)
        try:
            from utils.telegram import send_message
            send_message(f"🚨 {msg}")
        except Exception as e:
            logger.warning("telegram alert failed: %s", e)
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
