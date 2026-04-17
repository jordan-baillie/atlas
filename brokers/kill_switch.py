"""File-based kill switch — prevents new order placement if HALT file exists.

Usage:
    from brokers.kill_switch import is_halted, halt_reason
    if is_halted():
        logger.critical("Trading halted: %s", halt_reason())
        return

Existing positions continue to be monitored; this only blocks NEW order placement.
"""
from pathlib import Path
import logging

logger = logging.getLogger(__name__)
_HALT_FILE = Path("/root/atlas/data/HALT")


def is_halted() -> bool:
    return _HALT_FILE.exists()


def halt_reason() -> str:
    if not _HALT_FILE.exists():
        return ""
    try:
        return _HALT_FILE.read_text().strip() or "No reason given"
    except Exception as e:
        return f"(could not read HALT file: {e})"


def halt(reason: str) -> None:
    _HALT_FILE.parent.mkdir(parents=True, exist_ok=True)
    _HALT_FILE.write_text(reason.strip() + "\n")
    logger.critical("KILL SWITCH ENGAGED: %s", reason)


def resume() -> None:
    if _HALT_FILE.exists():
        _HALT_FILE.unlink()
        logger.warning("Kill switch released — trading resumed")
