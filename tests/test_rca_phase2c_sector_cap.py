"""RCA #2C — Regression tests: sector concentration cap enforcement."""
from __future__ import annotations

import logging
import pytest

from risk.sector_cap import apply_sector_cap


def _sig(ticker: str, sector: str | None, confidence: float = 0.8) -> dict:
    return {"ticker": ticker, "sector": sector, "confidence": confidence}


def _pos(sector: str) -> dict:
    return {"sector": sector}


class TestSectorCapCore:
    def test_third_same_sector_signal_is_rejected(self):
        candidates = [
            _sig("AAPL", "Technology"),
            _sig("MSFT", "Technology"),
            _sig("NVDA", "Technology"),
        ]
        accepted = apply_sector_cap(candidates, existing_positions=[], cap=2)
        assert len(accepted) == 2

    def test_existing_position_counts_against_cap(self):
        existing = [_pos("Technology")]
        candidates = [_sig("MSFT", "Technology"), _sig("NVDA", "Technology")]
        accepted = apply_sector_cap(candidates, existing_positions=existing, cap=2)
        assert len(accepted) == 1

    def test_different_sectors_not_capped(self):
        sectors = ["Technology", "Healthcare", "Energy", "Financial Services", "Industrials"]
        candidates = [_sig(f"T{i}", s) for i, s in enumerate(sectors)]
        accepted = apply_sector_cap(candidates, existing_positions=[], cap=2)
        assert len(accepted) == 5

    def test_higher_confidence_wins_when_capped(self):
        candidates = [
            _sig("AAPL", "Technology", confidence=0.9),
            _sig("MSFT", "Technology", confidence=0.7),
            _sig("NVDA", "Technology", confidence=0.85),
        ]
        accepted = apply_sector_cap(candidates, existing_positions=[], cap=2)
        assert len(accepted) == 2
        accepted_tickers = {c["ticker"] for c in accepted}
        assert "AAPL" in accepted_tickers
        assert "NVDA" in accepted_tickers
        assert "MSFT" not in accepted_tickers

    def test_cap_logs_warning_with_details(self, caplog):
        candidates = [
            _sig("AAPL", "Technology", confidence=0.9),
            _sig("MSFT", "Technology", confidence=0.8),
            _sig("NVDA", "Technology", confidence=0.7),
        ]
        with caplog.at_level(logging.WARNING, logger="risk.sector_cap"):
            accepted = apply_sector_cap(candidates, existing_positions=[], cap=2)
        assert len(accepted) == 2
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        msg = warnings[0].getMessage()
        assert "NVDA" in msg
        assert "Technology" in msg
        assert "2" in msg

    def test_unknown_sector_treated_as_own_bucket(self):
        candidates = [
            _sig("A", None, confidence=0.9),
            _sig("B", "", confidence=0.8),
            _sig("C", None, confidence=0.7),
        ]
        accepted = apply_sector_cap(candidates, existing_positions=[], cap=2)
        assert len(accepted) == 2
        accepted_tickers = {c["ticker"] for c in accepted}
        assert "A" in accepted_tickers
        assert "B" in accepted_tickers
        assert "C" not in accepted_tickers

    def test_zero_existing_positions_no_cap_interference(self):
        candidates = [_sig("AAPL", "Technology"), _sig("MSFT", "Technology")]
        accepted = apply_sector_cap(candidates, existing_positions=[], cap=2)
        assert len(accepted) == 2

    def test_cap_one_allows_exactly_one_per_sector(self):
        candidates = [
            _sig("AAPL", "Technology", confidence=0.9),
            _sig("MSFT", "Technology", confidence=0.6),
        ]
        accepted = apply_sector_cap(candidates, existing_positions=[], cap=1)
        assert len(accepted) == 1
        assert accepted[0]["ticker"] == "AAPL"
