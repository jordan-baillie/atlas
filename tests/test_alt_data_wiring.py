"""
tests/test_alt_data_wiring.py — Tests for Item C3: alt-data overlay integration.

Verifies:
  1. get_alt_data_summary() returns "" for empty tickers
  2. get_alt_data_summary() returns formatted lines when scraper returns records
  3. _load_alt_data() returns "" when alt_data.enabled=False (default)
  4. _load_alt_data() calls get_alt_data_summary when enabled=True with tickers
  5. _build_user_prompt includes alt_data token when alt_data kwarg is set
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


# ─────────────────────────────────────────────────────────────────────────────
# Test 1 — get_alt_data_summary: empty tickers → ""
# ─────────────────────────────────────────────────────────────────────────────

def test_get_alt_data_summary_empty_tickers():
    """Passing an empty list (or None) should return empty string without scraping."""
    from overlay.sources.alt_data import get_alt_data_summary

    assert get_alt_data_summary([]) == ""
    assert get_alt_data_summary(None) == ""


# ─────────────────────────────────────────────────────────────────────────────
# Test 2 — get_alt_data_summary: mock scraper records → formatted lines
# ─────────────────────────────────────────────────────────────────────────────

def test_get_alt_data_summary_with_mock_records():
    """When OpenInsiderScraper.scrape returns records, output should contain ticker and headline."""
    from overlay.sources.alt_data import get_alt_data_summary

    fake_records = [
        {
            "headline": "Insider Buy: Tim Cook (CEO) bought 10,000 shares of AAPL ($2,000,000)",
            "relevance_score": 0.9,
        },
        {
            "headline": "Insider Sale: Jeff Williams (COO) sold 2,000 shares of AAPL ($400,000)",
            "relevance_score": 0.7,
        },
    ]

    with patch("overlay.sources.alt_data.OpenInsiderScraper") as MockScraper:
        instance = MockScraper.return_value
        instance.scrape.return_value = fake_records

        result = get_alt_data_summary(["AAPL"])

    assert "AAPL:" in result
    # First record (higher relevance_score) should appear
    assert "Tim Cook" in result or "Insider Buy" in result


# ─────────────────────────────────────────────────────────────────────────────
# Test 3 — _load_alt_data: returns "" when disabled (default)
# ─────────────────────────────────────────────────────────────────────────────

def test_load_alt_data_off_when_disabled():
    """_load_alt_data should return "" without calling get_alt_data_summary when disabled."""
    from overlay import engine

    mock_cfg = {"alt_data": {"enabled": False}}

    with patch("utils.config.load_config", return_value=mock_cfg):
        with patch("overlay.sources.alt_data.get_alt_data_summary") as mock_summary:
            result = engine._load_alt_data()

    assert result == ""
    mock_summary.assert_not_called()


# ─────────────────────────────────────────────────────────────────────────────
# Test 4 — _load_alt_data: returns summary when enabled=True with tickers
# ─────────────────────────────────────────────────────────────────────────────

def test_load_alt_data_on_when_enabled_with_tickers():
    """_load_alt_data returns get_alt_data_summary result when enabled and tickers provided."""
    from overlay import engine

    mock_cfg = {"alt_data": {"enabled": True, "tickers": ["AAPL"]}}

    with patch("utils.config.load_config", return_value=mock_cfg):
        with patch(
            "overlay.sources.alt_data.get_alt_data_summary",
            return_value="AAPL: test signal",
        ) as mock_summary:
            result = engine._load_alt_data()

    assert result == "AAPL: test signal"
    mock_summary.assert_called_once_with(tickers=["AAPL"])


# ─────────────────────────────────────────────────────────────────────────────
# Test 5 — _build_user_prompt: alt_data token appears in prompt
# ─────────────────────────────────────────────────────────────────────────────

class _StubRegime:
    """Minimal stub that satisfies _build_user_prompt's attribute access."""

    def __init__(self):
        self.date = "2026-04-28"
        self.sizing_multiplier = 1.0
        self.max_positions = 10
        self.reasoning = "stub reasoning"
        self.scores = {}
        self.active_universes = ["sp500"]
        self.enabled_strategies = ["momentum_breakout"]

        # state attribute with .value
        class _State:
            value = "bull_risk_on"

        self.state = _State()


def test_overlay_pipeline_includes_alt_data_when_enabled():
    """_build_user_prompt should include ALT_DATA_TEST_TOKEN in the returned prompt."""
    from overlay.engine import _build_user_prompt

    regime = _StubRegime()
    prompt = _build_user_prompt(
        regime,
        news="",
        charts="",
        alt_data="ALT_DATA_TEST_TOKEN",
    )

    assert "ALT_DATA_TEST_TOKEN" in prompt, (
        f"Expected ALT_DATA_TEST_TOKEN in prompt, got:\n{prompt[:500]}"
    )
    # Also verify the section header appears
    assert "ALT DATA" in prompt
