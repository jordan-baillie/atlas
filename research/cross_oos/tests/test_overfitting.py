"""Tests for cross_oos.overfitting — PSR, expected-max-Sharpe, DSR, and PBO (CSCV)."""
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from research.cross_oos import overfitting as o  # noqa: E402


# ── PSR ──────────────────────────────────────────────────────────────────────
def test_psr_at_benchmark_is_half():
    assert o.probabilistic_sharpe_ratio(0.1, n_obs=500, sr_benchmark=0.1) == pytest.approx(0.5, abs=1e-9)


def test_psr_monotonic_in_sharpe_and_n():
    lo = o.probabilistic_sharpe_ratio(0.05, n_obs=500)
    hi = o.probabilistic_sharpe_ratio(0.15, n_obs=500)
    assert hi > lo
    small_n = o.probabilistic_sharpe_ratio(0.1, n_obs=100)
    big_n = o.probabilistic_sharpe_ratio(0.1, n_obs=2000)
    assert big_n > small_n  # more evidence → more confident the SR>0


# ── Expected max Sharpe / DSR ────────────────────────────────────────────────
def test_expected_max_sharpe_increases_with_trials():
    a = o.expected_max_sharpe(10, sr_variance=0.04)
    b = o.expected_max_sharpe(1000, sr_variance=0.04)
    assert b > a > 0
    assert o.expected_max_sharpe(1, 0.04) == 0.0  # single trial: nothing to deflate


def test_dsr_penalizes_more_trials():
    kw = dict(sr=0.12, n_obs=1500, sr_variance=0.02 ** 2)
    dsr_few = o.deflated_sharpe_ratio(n_trials=1, **kw)
    dsr_many = o.deflated_sharpe_ratio(n_trials=200, **kw)
    assert dsr_few > dsr_many
    assert 0.0 <= dsr_many <= 1.0 and 0.0 <= dsr_few <= 1.0


# ── PBO via CSCV ─────────────────────────────────────────────────────────────
def test_pbo_near_half_on_pure_noise():
    rng = np.random.default_rng(7)
    M = rng.standard_normal((400, 20))  # 20 configs, all noise
    res = o.pbo_cscv(M, n_splits=10)
    assert res["n_configs"] == 20 and res["n_combos"] > 0
    assert 0.30 <= res["pbo"] <= 0.70, f"noise PBO should be ~0.5, got {res['pbo']}"


def test_pbo_low_for_genuinely_dominant_config():
    rng = np.random.default_rng(11)
    M = rng.standard_normal((400, 20)) * 0.5
    M[:, 5] += 0.6  # config 5 has a real, persistent positive drift
    res = o.pbo_cscv(M, n_splits=10)
    assert res["pbo"] < 0.10, f"a genuinely dominant config should give low PBO, got {res['pbo']}"


def test_pbo_validates_inputs():
    with pytest.raises(ValueError):
        o.pbo_cscv(np.zeros((100, 1)))          # need >=2 configs
    with pytest.raises(ValueError):
        o.pbo_cscv(np.zeros((100, 5)), n_splits=7)  # n_splits must be even
