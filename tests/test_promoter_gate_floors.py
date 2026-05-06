"""Tests for tightened promotion gates per audit 2026-05-06 Rec 1.1-1.4."""
import pytest
from research.promoter import _sanity_check


def test_sanity_check_sharpe_floor():
    """Sub-rec 1.2: IS Sharpe >= 0.5 required."""
    # 0.4 fails the new floor
    result = _sanity_check({"sharpe": 0.4, "cagr_pct": 5, "total_trades": 50})
    assert not result["pass"]
    assert "0.5" in result["reason"]
    # 0.5 passes
    result = _sanity_check({"sharpe": 0.5, "cagr_pct": 5, "total_trades": 50})
    assert result["pass"]
    # 1.0 passes
    result = _sanity_check({"sharpe": 1.0, "cagr_pct": 5, "total_trades": 50})
    assert result["pass"]


def test_sanity_check_negative_sharpe_blocked():
    """Negative Sharpe is now blocked (was already, but reinforce)."""
    result = _sanity_check({"sharpe": -0.5, "cagr_pct": 5, "total_trades": 50})
    assert not result["pass"]


def test_sanity_check_zero_sharpe_blocked():
    """Zero Sharpe is now blocked (previously passed because gate was > 0, not >= 0.5)."""
    result = _sanity_check({"sharpe": 0.0, "cagr_pct": 5, "total_trades": 50})
    assert not result["pass"]


def test_sanity_check_just_below_floor():
    """0.499 just below the new floor — must fail."""
    result = _sanity_check({"sharpe": 0.499, "cagr_pct": 5, "total_trades": 50})
    assert not result["pass"]
    assert "0.5" in result["reason"]


def test_sanity_check_trades_floor_unchanged():
    """Trade count floor at 20 (portfolio-level, unchanged)."""
    result = _sanity_check({"sharpe": 1.0, "cagr_pct": 5, "total_trades": 19})
    assert not result["pass"]
    result = _sanity_check({"sharpe": 1.0, "cagr_pct": 5, "total_trades": 20})
    assert result["pass"]


def test_sanity_check_cagr_blocked():
    """Zero CAGR still fails."""
    result = _sanity_check({"sharpe": 1.0, "cagr_pct": 0, "total_trades": 50})
    assert not result["pass"]


def test_dsr_stats_per_strategy():
    """Sub-rec 1.1: variance is now per-strategy, not cross-strategy."""
    from db.atlas_db import get_db

    with get_db() as db:
        # Insert 5 mean_reversion experiments + 5 momentum_breakout experiments
        for i, sharpe in enumerate([0.5, 0.6, 0.7, 0.8, 0.9]):
            db.execute(
                "INSERT OR IGNORE INTO research_experiments "
                "(id, strategy, universe, sharpe, created_at) "
                "VALUES (?, ?, ?, ?, datetime('now'))",
                (f"test-mr-{i}", "dsr_test_mean_reversion", "sp500_dsr_test", sharpe),
            )
        for i, sharpe in enumerate([-2.0, -1.5, -1.0, 1.5, 2.0]):  # high variance
            db.execute(
                "INSERT OR IGNORE INTO research_experiments "
                "(id, strategy, universe, sharpe, created_at) "
                "VALUES (?, ?, ?, ?, datetime('now'))",
                (f"test-mb-{i}", "dsr_test_momentum_breakout", "sp500_dsr_test", sharpe),
            )
        db.commit()

    from research.loop import _get_dsr_stats
    mr_stats = _get_dsr_stats(strategy="dsr_test_mean_reversion", market="sp500_dsr_test")
    mb_stats = _get_dsr_stats(strategy="dsr_test_momentum_breakout", market="sp500_dsr_test")

    # mean_reversion has tight variance (0.5-0.9 range)
    # momentum_breakout has wide variance (-2 to 2 range)
    assert mr_stats["variance_of_sharpes"] < mb_stats["variance_of_sharpes"]
    assert mr_stats["num_experiments"] == 5
    assert mb_stats["num_experiments"] == 5


def test_dsr_stats_cross_strategy_isolation():
    """Per-strategy variance ignores other strategies' Sharpe distribution."""
    from db.atlas_db import get_db
    from research.loop import _get_dsr_stats

    with get_db() as db:
        # Insert tight strategy
        for i, sharpe in enumerate([0.9, 0.95, 1.0, 1.05, 1.1]):
            db.execute(
                "INSERT OR IGNORE INTO research_experiments "
                "(id, strategy, universe, sharpe, created_at) "
                "VALUES (?, ?, ?, ?, datetime('now'))",
                (f"test-tight-{i}", "dsr_test_tight_strat", "sp500_isolation_test", sharpe),
            )
        # Insert wild strategy in the same universe
        for i, sharpe in enumerate([-5.0, -4.0, 0.0, 4.0, 5.0]):
            db.execute(
                "INSERT OR IGNORE INTO research_experiments "
                "(id, strategy, universe, sharpe, created_at) "
                "VALUES (?, ?, ?, ?, datetime('now'))",
                (f"test-wild-{i}", "dsr_test_wild_strat", "sp500_isolation_test", sharpe),
            )
        db.commit()

    tight_stats = _get_dsr_stats(strategy="dsr_test_tight_strat", market="sp500_isolation_test")
    wild_stats = _get_dsr_stats(strategy="dsr_test_wild_strat", market="sp500_isolation_test")

    # tight_strat var must be tiny (<0.01), wild_strat must be huge (>10)
    assert tight_stats["variance_of_sharpes"] < 0.1
    assert wild_stats["variance_of_sharpes"] > 5.0
    # And tight is isolated from wild — this proves the cross-strategy leak is fixed
    assert tight_stats["variance_of_sharpes"] < wild_stats["variance_of_sharpes"]


def test_keep_or_discard_min_trades_floor_raised():
    """Sub-rec 1.3: Gate 2 min_trades floor raised from 10 to 30 in keep_or_discard.

    When baseline has 0 trades, min_trades = 30 (the new absolute floor).
    Old code: min_trades = 10 (old floor when b_trades=0).
    """
    from research.loop import keep_or_discard

    # Baseline with 0 trades — floor becomes max(30, 0) = 30
    baseline_zero = {"sharpe": 1.0, "total_trades": 0, "max_drawdown_pct": 10.0}

    # Experiment with 25 trades — previously passed (>10 old floor), now fails (<30 new floor)
    exp_25 = {"sharpe": 1.1, "total_trades": 25, "max_drawdown_pct": 10.0}
    result = keep_or_discard(baseline_zero, exp_25, 0)
    assert result["decision"] == "discard", f"25 trades should fail new 30-floor: {result}"
    assert "25" in result["rationale"]

    # 15 trades also failed the old 10-floor? No — old floor was 10 when b_trades=0.
    # Actually old code: min_trades = 10 when b_trades=0, so 15 > 10 passed.
    exp_15 = {"sharpe": 1.1, "total_trades": 15, "max_drawdown_pct": 10.0}
    result_15 = keep_or_discard(baseline_zero, exp_15, 0)
    assert result_15["decision"] == "discard", f"15 trades should also fail new 30-floor: {result_15}"

    # Experiment with 30 trades — should pass the floor check (exact floor value)
    exp_30 = {"sharpe": 1.1, "total_trades": 30, "max_drawdown_pct": 10.0}
    result_30 = keep_or_discard(baseline_zero, exp_30, 0)
    assert result_30["decision"] == "keep", f"30 trades should pass new floor: {result_30}"


def test_keep_or_discard_70pct_rule_dominates_at_high_baseline():
    """When baseline has many trades, 70% rule dominates over the 30-floor."""
    from research.loop import keep_or_discard

    # baseline=100 → min_trades = max(30, 70) = 70
    baseline_100 = {"sharpe": 1.0, "total_trades": 100, "max_drawdown_pct": 10.0}
    exp_50 = {"sharpe": 1.1, "total_trades": 50, "max_drawdown_pct": 10.0}
    # 50 < 70 → discard (70% rule kicks in above 30)
    result = keep_or_discard(baseline_100, exp_50, 0)
    assert result["decision"] == "discard"

    # 71 trades → just above 70% rule → should pass trade check
    exp_71 = {"sharpe": 1.1, "total_trades": 71, "max_drawdown_pct": 10.0}
    result_71 = keep_or_discard(baseline_100, exp_71, 0)
    assert result_71["decision"] == "keep", f"71 trades (>70% of 100) should pass: {result_71}"
