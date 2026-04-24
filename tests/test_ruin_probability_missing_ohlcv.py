"""Regression tests for ruin_probability when some/all tickers lack OHLCV history.

Root cause: _get_returns_matrix silently drops tickers with no data via
inner-join / dropna, so tickers[] (len N) diverges from returns_df.columns
(len M < N), causing  Z (n_paths, N) @ L.T (M, M)  to crash.

Fix: after _get_returns_matrix, intersect tickers with available columns;
treat missing positions as cash; return no_data_for_positions when all drop.
"""
from __future__ import annotations

import logging

import numpy as np
import pandas as pd
import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_positions(tickers: list[str], shares: float = 10.0, price: float = 100.0) -> list[dict]:
    return [{"ticker": t, "shares": shares, "current_price": price} for t in tickers]


def _fake_returns(tickers: list[str], n: int = 60, seed: int = 0) -> pd.DataFrame:
    """Synthetic log-return DataFrame for *tickers* with *n* rows."""
    rng = np.random.default_rng(seed)
    data = rng.normal(0, 0.01, size=(n, len(tickers)))
    return pd.DataFrame(data, columns=tickers)


# ---------------------------------------------------------------------------
# Test 1 — one ticker has no OHLCV; must not crash, must warn, must return ok
# ---------------------------------------------------------------------------

def test_missing_one_ticker_does_not_crash(monkeypatch, caplog):
    """2 positions, XLK has no OHLCV rows → status='ok', WARNING logged."""
    from risk import ruin_probability as rp

    good_tickers = ["AAPL"]
    all_tickers  = ["AAPL", "XLK"]     # XLK has no history

    # _get_returns_matrix returns only the 'good' tickers
    monkeypatch.setattr(
        "risk.ruin_probability._get_returns_matrix",
        lambda tickers, lookback_days: _fake_returns(good_tickers, n=60),
        raising=False,
    )
    # Need to patch inside portfolio_var module too (imported lazily)
    import risk.portfolio_var as pv
    monkeypatch.setattr(pv, "_get_returns_matrix",
                        lambda tickers, lookback_days: _fake_returns(good_tickers, n=60))

    positions = _make_positions(all_tickers, shares=10.0, price=100.0)
    equity = 2500.0  # 2 × $1 000 + $500 cash

    with caplog.at_level(logging.WARNING, logger="risk.ruin_probability"):
        result = rp.compute_ruin_probability(
            current_equity=equity,
            positions=positions,
            horizons=(30,),
            n_paths=500,
            seed=42,
        )

    assert result["status"] == "ok", f"Expected ok, got {result['status']!r}"
    assert "30d" in result["horizons"]
    assert result["tickers"] == good_tickers, (
        f"Expected only good tickers in result, got {result['tickers']}"
    )
    # Cash should be bumped by the dropped position value (10 * 100 = 1000)
    assert result["cash"] >= 1000.0 - 1e-6, (
        f"Expected cash ≥ 1000, got {result['cash']}"
    )
    # WARNING must mention the dropped ticker
    assert any("XLK" in m for m in caplog.messages), (
        f"Expected WARNING mentioning XLK in logs; got: {caplog.messages}"
    )


# ---------------------------------------------------------------------------
# Test 2 — ALL positions have no OHLCV → no_data_for_positions, no crash
# ---------------------------------------------------------------------------

def test_all_tickers_missing_returns_no_data_status(monkeypatch):
    """All positions have no OHLCV → status='no_data_for_positions'."""
    from risk import ruin_probability as rp
    import risk.portfolio_var as pv

    all_tickers = ["XLK", "XLC"]

    # Returns matrix is empty (no columns overlap)
    monkeypatch.setattr(
        "risk.ruin_probability._get_returns_matrix",
        lambda tickers, lookback_days: pd.DataFrame({"DUMMY": [0.01] * 60}),
        raising=False,
    )
    monkeypatch.setattr(pv, "_get_returns_matrix",
                        lambda tickers, lookback_days: pd.DataFrame({"DUMMY": [0.01] * 60}))

    positions = _make_positions(all_tickers)
    equity = 2000.0

    result = rp.compute_ruin_probability(
        current_equity=equity,
        positions=positions,
        horizons=(30, 60),
        n_paths=200,
        seed=7,
    )

    assert result["status"] == "no_data_for_positions", (
        f"Expected no_data_for_positions, got {result['status']!r}"
    )
    # All horizons should still be present with prob_ruin=0
    for key in ("30d", "60d"):
        assert key in result["horizons"], f"Missing horizon {key}"
        assert result["horizons"][key]["prob_ruin"] == 0.0
    # No crash — result has current_equity and floor
    assert result["current_equity"] == equity
    assert result["floor"] == pytest.approx(equity * 0.70)


# ---------------------------------------------------------------------------
# Test 3 — all positions have OHLCV → unchanged behaviour (sanity check)
# ---------------------------------------------------------------------------

def test_all_tickers_present_unchanged_behaviour(monkeypatch):
    """All positions have OHLCV data → normal computation, status='ok'."""
    from risk import ruin_probability as rp
    import risk.portfolio_var as pv

    tickers = ["AAPL", "MSFT"]
    returns = _fake_returns(tickers, n=60, seed=1)

    monkeypatch.setattr(
        "risk.ruin_probability._get_returns_matrix",
        lambda t, lookback_days: returns,
        raising=False,
    )
    monkeypatch.setattr(pv, "_get_returns_matrix",
                        lambda t, lookback_days: returns)

    positions = _make_positions(tickers, shares=5.0, price=200.0)
    equity = 2200.0

    result = rp.compute_ruin_probability(
        current_equity=equity,
        positions=positions,
        horizons=(30,),
        n_paths=1000,
        seed=99,
    )

    assert result["status"] == "ok", f"Expected ok, got {result['status']!r}"
    assert result["tickers"] == tickers
    assert "30d" in result["horizons"]
    prob = result["horizons"]["30d"]["prob_ruin"]
    assert 0.0 <= prob <= 1.0, f"prob_ruin out of range: {prob}"
    # positions_value should match the unmodified sum
    expected_pos_value = 5.0 * 200.0 * 2
    assert result["positions_value"] == pytest.approx(expected_pos_value)
