"""Tests for StrategyAllocationPool.

Run with:  python -m pytest tests/test_allocation.py -v
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from utils.allocation import StrategyAllocationPool, build_allocation_pool


# ── Fixtures ─────────────────────────────────────────────────────────────────

def _make_pool_cfg(mode="hard_pool", overflow=True, pools=None):
    if pools is None:
        pools = {
            "trend_following":   {"max_positions": 3},
            "mean_reversion":    {"max_positions": 2},
            "opening_gap":       {"max_positions": 2},
            "_other":            {"max_positions": 2},
        }
    return {
        "allocation": {
            "enabled": True,
            "mode": mode,
            "overflow_enabled": overflow,
            "pools": pools,
        }
    }


def _pos(strategy, n=1):
    """Return n open position dicts for a given strategy."""
    return [{"strategy": strategy}] * n


# ── Basic behaviour ───────────────────────────────────────────────────────────

class TestDisabled:
    def test_disabled_by_default(self):
        pool = StrategyAllocationPool({})
        assert not pool.is_enabled()

    def test_disabled_always_accepts(self):
        pool = StrategyAllocationPool({})
        ok, _ = pool.can_accept("trend_following", _pos("trend_following", 100))
        assert ok

    def test_explicit_disabled(self):
        cfg = {"allocation": {"enabled": False, "pools": {"trend_following": {"max_positions": 1}}}}
        pool = StrategyAllocationPool(cfg)
        ok, _ = pool.can_accept("trend_following", _pos("trend_following", 99))
        assert ok


class TestHardPool:
    def setup_method(self):
        self.pool = StrategyAllocationPool(_make_pool_cfg(mode="hard_pool"))

    def test_within_cap_accepted(self):
        ok, reason = self.pool.can_accept("trend_following", _pos("trend_following", 2))
        assert ok, reason

    def test_at_cap_rejected(self):
        ok, reason = self.pool.can_accept("trend_following", _pos("trend_following", 3))
        assert not ok
        assert "trend_following" in reason

    def test_other_strategy_at_cap_rejected(self):
        ok, reason = self.pool.can_accept("mean_reversion", _pos("mean_reversion", 2))
        assert not ok

    def test_empty_portfolio_accepted(self):
        ok, _ = self.pool.can_accept("trend_following", [])
        assert ok

    def test_mixed_positions(self):
        positions = _pos("trend_following", 2) + _pos("mean_reversion", 1)
        ok, _ = self.pool.can_accept("trend_following", positions)
        assert ok  # tf at 2/3

        positions = _pos("trend_following", 3) + _pos("mean_reversion", 1)
        ok, reason = self.pool.can_accept("trend_following", positions)
        assert not ok  # tf at 3/3

    def test_unnamed_strategy_uses_other_pool(self):
        # unknown_strategy falls through to _other (cap=2)
        ok, _ = self.pool.can_accept("exotic_strat", _pos("exotic_strat", 1))
        assert ok

        ok, reason = self.pool.can_accept("exotic_strat", _pos("exotic_strat", 2))
        assert not ok  # _other cap = 2

    def test_count_by_strategy(self):
        positions = _pos("trend_following", 3) + _pos("mean_reversion", 1)
        assert self.pool.count_by_strategy("trend_following", positions) == 3
        assert self.pool.count_by_strategy("mean_reversion", positions) == 1
        assert self.pool.count_by_strategy("opening_gap", positions) == 0


class TestSoftPool:
    def setup_method(self):
        self.pool = StrategyAllocationPool(_make_pool_cfg(mode="soft_pool", overflow=True))

    def test_own_pool_not_full_accepted(self):
        ok, _ = self.pool.can_accept("trend_following", _pos("trend_following", 2))
        assert ok

    def test_own_pool_full_overflow_available(self):
        # tf cap=3, overflow(_other) cap=2.  3 tf positions, 0 overflow used.
        ok, reason = self.pool.can_accept("trend_following", _pos("trend_following", 3))
        assert ok, f"Expected overflow acceptance, got: {reason}"

    def test_own_pool_full_overflow_full_rejected(self):
        # tf cap=3, overflow used by 2 other strategies borrowing 1 each
        positions = (
            _pos("trend_following", 3)       # tf at cap
            + _pos("mean_reversion", 2)      # mr at cap (borrowing 0 from overflow, but mr cap=2 exact)
            + _pos("opening_gap", 3)         # og cap=2, 1 in overflow
            + _pos("momentum_breakout", 3)   # not in pools → uses _other directly
        )
        # overflow: opening_gap borrowed 1 (1 above cap of 2), momentum_breakout borrowed 3 (not in pools)
        # total overflow = 1 + 3 = 4 but _other cap = 2 → overflow full
        ok, reason = self.pool.can_accept("trend_following", positions)
        assert not ok

    def test_no_overflow_flag(self):
        pool = StrategyAllocationPool(_make_pool_cfg(mode="soft_pool", overflow=False))
        ok, reason = pool.can_accept("trend_following", _pos("trend_following", 3))
        assert not ok  # own pool full, overflow disabled


class TestCountsSummary:
    def test_empty_portfolio(self):
        pool = StrategyAllocationPool(_make_pool_cfg())
        summary = pool.counts_summary([])
        assert "trend_following" in summary
        assert summary["trend_following"]["used"] == 0
        assert summary["trend_following"]["cap"] == 3

    def test_with_positions(self):
        pool = StrategyAllocationPool(_make_pool_cfg())
        positions = _pos("trend_following", 2) + _pos("mean_reversion", 1)
        summary = pool.counts_summary(positions)
        assert summary["trend_following"]["used"] == 2
        assert summary["mean_reversion"]["used"] == 1
        assert summary["opening_gap"]["used"] == 0

    def test_disabled_returns_empty(self):
        pool = StrategyAllocationPool({})
        summary = pool.counts_summary(_pos("trend_following", 5))
        assert summary == {}


class TestBuildAllocationPool:
    def test_factory_returns_pool(self):
        pool = build_allocation_pool(_make_pool_cfg())
        assert isinstance(pool, StrategyAllocationPool)
        assert pool.is_enabled()

    def test_factory_disabled(self):
        pool = build_allocation_pool({})
        assert not pool.is_enabled()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
