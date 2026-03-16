"""Baseline regression test — run before any research or after strategy changes.

Loads the active SP500 config, runs a walk-forward backtest, and asserts
trade count and Sharpe are within expected bounds. Prevents silent
regressions from strategy code changes.

Usage:
    python3 -m pytest tests/test_baseline_regression.py -v
    python3 -m pytest tests/test_baseline_regression.py -v -m slow   # include stability test
"""
import copy

import pytest

from scripts.strategy_evaluator import get_active_config, load_market_data, run_backtest


# Expected bounds — update these after each validated config promotion
EXPECTED_MIN_TRADES = 80
EXPECTED_MAX_TRADES = 700
EXPECTED_MIN_SHARPE = -2.0  # Allow negative but not catastrophic
EXPECTED_STRATEGIES = {"mean_reversion", "trend_following"}  # OG may have 0 trades


@pytest.fixture(scope="module")
def baseline_result():
    """Run baseline backtest once for all tests."""
    cfg = get_active_config("sp500")
    data = load_market_data("sp500")
    return run_backtest(copy.deepcopy(cfg), data)


def test_minimum_trades(baseline_result):
    """Strategy must generate a minimum number of trades."""
    trades = baseline_result["total_trades"]
    assert trades >= EXPECTED_MIN_TRADES, (
        f"Only {trades} trades (min {EXPECTED_MIN_TRADES}). "
        f"Likely confidence gate filtering too aggressively."
    )


def test_maximum_trades(baseline_result):
    """Sanity cap — too many trades means a filter broke."""
    trades = baseline_result["total_trades"]
    assert trades <= EXPECTED_MAX_TRADES, (
        f"{trades} trades exceeds {EXPECTED_MAX_TRADES}. "
        f"A filter may have been accidentally disabled."
    )


def test_sharpe_not_catastrophic(baseline_result):
    """Sharpe should not be catastrophically negative."""
    sharpe = baseline_result["sharpe"]
    assert sharpe >= EXPECTED_MIN_SHARPE, (
        f"Sharpe {sharpe:.3f} below {EXPECTED_MIN_SHARPE}. "
        f"Check for broken strategy logic or data issues."
    )


def test_active_strategies_produce_trades(baseline_result):
    """Each expected strategy must generate at least 1 trade."""
    breakdown = baseline_result.get("strategy_breakdown", {})
    for strat in EXPECTED_STRATEGIES:
        assert strat in breakdown, f"Strategy {strat} missing from results"
        assert breakdown[strat]["trades"] > 0, (
            f"{strat} generated 0 trades — confidence scoring may be broken"
        )


def test_no_negative_equity(baseline_result):
    """Final equity must be positive."""
    equity = baseline_result.get("final_equity", 0)
    assert equity > 0, f"Final equity ${equity} — backtest blew up"


# ---------------------------------------------------------------------------
# Walk-forward stability test — multi-offset validation
# ---------------------------------------------------------------------------

@pytest.mark.slow
def test_walkforward_stability():
    """Walk-forward trade count must be stable across start-date offsets.

    Trims [0, 5, 10] trading days from the start of data and runs a full
    walk-forward for each.  Checks that the coefficient of variation of the
    trade count (std / mean) stays below 0.50 — a loose bound that flags
    cases where the strategy fires very differently depending on the exact
    calendar alignment of the walk-forward windows.

    A passing result does NOT guarantee profitability; it means the strategy
    is not over-fit to a specific window boundary.

    Note: this test runs 3 full walk-forward backtests on the SP500
    universe and is slow (~minutes).  Run with ``pytest -m slow`` or as
    part of a full regression gate before config promotion.
    """
    from backtest.engine import BacktestEngine
    from scripts.strategy_evaluator import get_strategy_class

    cfg = get_active_config("sp500")
    data = load_market_data("sp500")

    # Build enabled strategies
    strategies = []
    for name, scfg in cfg.get("strategies", {}).items():
        if scfg.get("enabled", False):
            try:
                cls = get_strategy_class(name)
                strategies.append(cls(copy.deepcopy(cfg)))
            except Exception as exc:
                pytest.skip(f"Could not load strategy '{name}': {exc}")

    if not strategies:
        pytest.skip("No enabled strategies found in active config")

    engine = BacktestEngine(copy.deepcopy(cfg))
    result = engine.run_walkforward_multioffset(data, strategies, offsets=[0, 5, 10])

    cv_trades = result.get("cv_trades", 0)
    median_trades = result.get("median_trades", 0)

    assert median_trades > 0, (
        f"Median trade count is {median_trades} — strategies produced no trades across offsets"
    )
    assert cv_trades < 0.50, (
        f"cv_trades={cv_trades:.3f} >= 0.50 — walk-forward is too sensitive to "
        f"start-date alignment. Per-offset: {result.get('per_offset', [])}"
    )
