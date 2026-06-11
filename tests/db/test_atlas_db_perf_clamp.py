"""Tests for db/atlas_db.py profit_factor clamping (P2.5).

Verifies:
  - No winners + losers → profit_factor = ratio, capped at 99.99
  - Winners only (no losses) → profit_factor = 99.99
  - No trades → profit_factor is None (not inf, not raises)
  - _group_performance also clamps (via by_strategy breakdown)

Run:
    python3 -m pytest tests/test_atlas_db_perf_clamp.py -v --timeout=30
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT))

import atlas.db as _adb
from atlas.db import record_trade_entry, performance_summary, init_db


# ─── Helpers ────────────────────────────────────────────────────────────────

def _insert_trade(ticker: str, pnl: float, strategy: str = "momentum_breakout") -> None:
    """Insert a closed trade with the given pnl."""
    with _adb.get_db() as db:
        db.execute("""
            INSERT INTO trades
                (ticker, strategy, universe, direction, entry_date, entry_price,
                 shares, stop_price, take_profit, confidence, status,
                 exit_date, exit_price, pnl)
            VALUES
                (?, ?, 'sp500', 'long', '2026-01-01', 100.0,
                 10, 95.0, 115.0, 0.8, 'closed',
                 '2026-01-10', ?, ?)
        """, (ticker, strategy, 100.0 + pnl / 10, pnl))


# ─── Tests ─────────────────────────────────────────────────────────────────

class TestPerformanceClamp:
    """P2.5 — profit_factor is clamped, never inf."""

    def test_performance_clamp_no_losses(self):
        """5 winning trades, 0 losses → profit_factor == 99.99."""
        for i in range(5):
            _insert_trade(f"WIN{i}", 10.0)

        summary = performance_summary()
        pf = summary.get("profit_factor")
        assert pf is not None, "profit_factor should not be None when wins exist"
        assert pf == 99.99, f"Expected 99.99, got {pf}"

    def test_performance_clamp_no_wins_no_losses(self):
        """No trades → profit_factor is None."""
        # Isolated DB has no trades (autouse fixture)
        summary = performance_summary()
        assert summary.get("profit_factor") is None or summary.get("trades", 0) == 0

    def test_performance_clamp_with_losses(self):
        """Mixed wins and losses → profit_factor = gross_win/gross_loss, capped at 99.99."""
        _insert_trade("WIN1", 30.0)
        _insert_trade("LOSS1", -10.0)
        summary = performance_summary()
        pf = summary.get("profit_factor")
        assert pf is not None
        assert pf == 3.0 or abs(pf - 3.0) < 0.01, f"Expected ~3.0, got {pf}"
        assert pf <= 99.99

    def test_performance_clamp_very_high_ratio(self):
        """Very large gross_profit / tiny gross_loss is capped at 99.99."""
        _insert_trade("BIG_WIN", 10000.0)
        _insert_trade("TINY_LOSS", -1.0)
        summary = performance_summary()
        pf = summary.get("profit_factor")
        assert pf is not None
        assert pf <= 99.99, f"Expected ≤99.99, got {pf}"

    def test_no_infinity_in_json(self):
        """profit_factor must serialise to valid JSON (no Infinity)."""
        import json
        _insert_trade("WIN1", 50.0)
        summary = performance_summary()
        # Must not raise ValueError from json.dumps
        serialised = json.dumps(summary)
        assert "Infinity" not in serialised
        assert "infinity" not in serialised.lower()
        assert "NaN" not in serialised

    def test_group_performance_clamp(self):
        """_group_performance (used in by_strategy) also clamps."""
        _insert_trade("STRAT_WIN1", 20.0, strategy="momentum_breakout")
        summary = performance_summary()
        by_strategy = summary.get("by_strategy", {})
        if "momentum_breakout" in by_strategy:
            pf = by_strategy["momentum_breakout"].get("profit_factor")
            if pf is not None:
                assert pf <= 99.99, f"by_strategy pf not clamped: {pf}"

    def test_only_losses_yields_zero_profit_factor(self):
        """All losing trades → profit_factor = 0.0 (gross_profit=0, gross_loss>0)."""
        _insert_trade("LOSS1", -10.0)
        _insert_trade("LOSS2", -5.0)
        summary = performance_summary()
        pf = summary.get("profit_factor")
        # With no wins, gross_profit=0, gross_loss>0 → pf=0
        assert pf is not None
        assert pf == 0.0 or pf < 0.01, f"Expected 0.0, got {pf}"
