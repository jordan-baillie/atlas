"""Tests for telegram_bot.py passive mode rendering fix (Commit 4, spec §11.4)."""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))


def _stub_plan() -> dict:
    """Minimal plan dict that satisfies format_plan_message."""
    return {
        "trade_date": "2026-05-05",
        "proposed_entries": [],
        "proposed_exits": [],
        "portfolio_snapshot": {},
        "risk_summary": {},
    }


def _config_for_mode(mode: str, broker: str = "alpaca", dry_run: bool = False) -> dict:
    """Build a minimal config dict for a given mode."""
    return {
        "market": "sp500",
        "trading": {
            "mode": mode,
            "broker": broker,
            "live_safety": {"dry_run_first": dry_run},
            "live_enabled": True,
        },
        "risk": {"starting_equity": 1000, "max_open_positions": 5},
        "fees": {},
        "strategies": {},
    }


def test_passive_mode_renders_passive_not_live():
    """mode='passive' → '⏸ PASSIVE' in output, '🔴 LIVE' NOT in output."""
    from services.telegram_bot import format_plan_message
    cfg = _config_for_mode("passive")
    with patch("services.telegram_bot.get_active_config", return_value=cfg):
        result = format_plan_message(_stub_plan(), "sp500")
    assert "PASSIVE" in result, f"Expected 'PASSIVE' in: {result}"
    assert "🔴 LIVE" not in result, f"Expected '🔴 LIVE' NOT in: {result}"


def test_live_mode_unchanged():
    """mode='live', broker='alpaca', dry_run=False → '🔴 LIVE'."""
    from services.telegram_bot import format_plan_message
    cfg = _config_for_mode("live", broker="alpaca", dry_run=False)
    with patch("services.telegram_bot.get_active_config", return_value=cfg):
        result = format_plan_message(_stub_plan(), "sp500")
    assert "🔴 LIVE" in result, f"Expected '🔴 LIVE' in: {result}"
    assert "PASSIVE" not in result


def test_paper_broker_unchanged():
    """broker not in ('alpaca',) → '📝 PAPER'."""
    from services.telegram_bot import format_plan_message
    cfg = _config_for_mode("live", broker="paper")
    with patch("services.telegram_bot.get_active_config", return_value=cfg):
        result = format_plan_message(_stub_plan(), "sp500")
    assert "PAPER" in result


def test_dry_run_unchanged():
    """mode='live', dry_run=True → '🔶 LIVE (DRY RUN)'."""
    from services.telegram_bot import format_plan_message
    cfg = _config_for_mode("live", broker="alpaca", dry_run=True)
    with patch("services.telegram_bot.get_active_config", return_value=cfg):
        result = format_plan_message(_stub_plan(), "sp500")
    assert "DRY RUN" in result, f"Expected 'DRY RUN' in: {result}"
