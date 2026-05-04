"""Tests for EOD report header using market identifier (Fix 3).

Verifies that generate_eod_report() uses the market parameter in the
report header instead of a hardcoded "ATLAS-ASX".
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

import scripts.eod_settlement as eod  # noqa: E402


def _make_portfolio(market_id: str = "sp500") -> MagicMock:
    """Build a minimal mock portfolio that satisfies generate_eod_report."""
    portfolio = MagicMock()
    portfolio.equity_history = []
    portfolio.portfolio_summary.return_value = {
        "equity": 10_000.0,
        "starting_equity": 10_000.0,
        "cash": 2_000.0,
        "num_open": 3,
        "total_pnl": 500.0,
        "total_pnl_pct": 5.0,
        "open_positions": [],
    }
    return portfolio


class TestEodReportHeader:
    def test_sp500_header(self):
        """market='sp500' → header contains ATLAS-SP500."""
        portfolio = _make_portfolio()
        report = eod.generate_eod_report(
            portfolio, {}, "2026-05-01", [], [], market="sp500"
        )
        assert "ATLAS-SP500" in report, f"Expected ATLAS-SP500 in:\n{report[:200]}"

    def test_commodity_etfs_header(self):
        """market='commodity_etfs' → header contains ATLAS-COMMODITY_ETFS."""
        portfolio = _make_portfolio()
        report = eod.generate_eod_report(
            portfolio, {}, "2026-05-01", [], [], market="commodity_etfs"
        )
        assert "ATLAS-COMMODITY_ETFS" in report, (
            f"Expected ATLAS-COMMODITY_ETFS in:\n{report[:200]}"
        )

    def test_asx_default_header(self):
        """Default market='asx' → header contains ATLAS-ASX (backward compat)."""
        portfolio = _make_portfolio()
        report = eod.generate_eod_report(
            portfolio, {}, "2026-05-01", [], []
        )
        assert "ATLAS-ASX" in report, f"Expected ATLAS-ASX in:\n{report[:200]}"

    def test_header_not_hardcoded_asx(self):
        """Passing market='sp500' must NOT produce ATLAS-ASX in the header."""
        portfolio = _make_portfolio()
        report = eod.generate_eod_report(
            portfolio, {}, "2026-05-01", [], [], market="sp500"
        )
        lines = report.splitlines()
        # The header line is the second non-equals line; confirm no literal ATLAS-ASX
        header_lines = [l for l in lines if "END-OF-DAY REPORT" in l]
        assert header_lines, "No END-OF-DAY REPORT line found in report"
        assert "ATLAS-ASX" not in header_lines[0], (
            f"Header should not contain ATLAS-ASX when market='sp500': {header_lines[0]}"
        )

    def test_trade_date_in_header(self):
        """Trade date must appear in the header line."""
        portfolio = _make_portfolio()
        report = eod.generate_eod_report(
            portfolio, {}, "2026-05-01", [], [], market="sp500"
        )
        header_lines = [l for l in report.splitlines() if "END-OF-DAY REPORT" in l]
        assert "2026-05-01" in header_lines[0], (
            f"Trade date missing from header: {header_lines[0]}"
        )
