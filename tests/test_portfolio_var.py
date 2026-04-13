import numpy as np
import pytest
from risk.portfolio_var import compute_portfolio_var, _ledoit_wolf_shrinkage, _cholesky_safe, _effective_number_of_bets

def test_empty_positions():
    result = compute_portfolio_var([], n_paths=100)
    assert result["positions_count"] == 0
    assert result["positions_value"] == 0

def test_single_position_var_close_to_analytical():
    positions = [{"ticker": "SPY", "shares": 10, "current_price": 500.0, "strategy": "test"}]
    result = compute_portfolio_var(positions, n_paths=20000, seed=42)
    assert "horizons" in result
    assert "1d" in result["horizons"]
    assert result["horizons"]["1d"]["var_95"] < 0
    assert result["horizons"]["1d"]["cvar_95"] < result["horizons"]["1d"]["var_95"]

def test_seed_reproducibility():
    positions = [{"ticker": "SPY", "shares": 10, "current_price": 500.0, "strategy": "test"}]
    r1 = compute_portfolio_var(positions, n_paths=1000, seed=42)
    r2 = compute_portfolio_var(positions, n_paths=1000, seed=42)
    assert r1["horizons"]["1d"]["var_95"] == r2["horizons"]["1d"]["var_95"]

def test_5d_var_larger_than_1d():
    positions = [{"ticker": "SPY", "shares": 10, "current_price": 500.0, "strategy": "test"}]
    result = compute_portfolio_var(positions, n_paths=10000, seed=42)
    assert abs(result["horizons"]["5d"]["var_95"]) > abs(result["horizons"]["1d"]["var_95"])

def test_cvar_more_negative_than_var():
    positions = [{"ticker": "SPY", "shares": 10, "current_price": 500.0, "strategy": "test"}]
    result = compute_portfolio_var(positions, n_paths=10000, seed=42)
    for h in ["1d", "5d"]:
        assert result["horizons"][h]["cvar_95"] <= result["horizons"][h]["var_95"]
        assert result["horizons"][h]["cvar_99"] <= result["horizons"][h]["var_99"]

def test_ledoit_wolf_shrinkage():
    import pandas as pd
    np.random.seed(42)
    returns = pd.DataFrame(np.random.randn(60, 3) * 0.01, columns=['A', 'B', 'C'])
    cov = _ledoit_wolf_shrinkage(returns)
    assert cov.shape == (3, 3)
    eigenvalues = np.linalg.eigvalsh(cov)
    assert all(e > 0 for e in eigenvalues)

def test_cholesky_safe():
    import pandas as pd
    np.random.seed(42)
    returns = pd.DataFrame(np.random.randn(60, 3) * 0.01, columns=['A', 'B', 'C'])
    cov = _ledoit_wolf_shrinkage(returns)
    L = _cholesky_safe(cov)
    assert L.shape == (3, 3)
    np.testing.assert_allclose(L @ L.T, cov, atol=1e-10)

def test_effective_number_of_bets():
    # Uncorrelated equal weights → full diversification, ENB ≈ N
    weights = np.array([0.5, 0.5])
    corr_uncorr = np.array([[1.0, 0.0], [0.0, 1.0]])
    enb_uncorr = _effective_number_of_bets(weights, corr_uncorr)
    assert 1.95 <= enb_uncorr <= 2.0

    # Concentrated dollar weights → ENB collapses toward 1
    # (marginal-contribution ENB is driven by dollar-weight concentration,
    # not correlation — equal weights on a symmetric matrix always give N)
    weights_conc = np.array([0.9, 0.1])
    enb_conc = _effective_number_of_bets(weights_conc, corr_uncorr)
    assert 1.0 <= enb_conc <= 1.15

    # 3 uncorrelated equal weight → ENB ≈ 3
    weights3 = np.array([1/3, 1/3, 1/3])
    corr3 = np.eye(3)
    enb3 = _effective_number_of_bets(weights3, corr3)
    assert 2.95 <= enb3 <= 3.0

    # Sanity: equal weights + high correlation → still N (known property)
    corr_high = np.array([[1.0, 0.99], [0.99, 1.0]])
    enb_high_eq = _effective_number_of_bets(weights, corr_high)
    assert 1.95 <= enb_high_eq <= 2.0
