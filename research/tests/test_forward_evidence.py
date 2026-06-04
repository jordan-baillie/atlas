"""Tests for the forward-evidence gate (rapid pipeline #418)."""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from research.forward_evidence import evaluate_forward, days_to_decision  # noqa: E402


def test_strong_positive_edge_passes():
    rng = np.random.default_rng(0)
    # genuinely high-t edge over 45 i.i.d. days -> passes via the t-stat power route
    r = rng.normal(0.0015, 0.005, 45)
    out = evaluate_forward(r, clv=0.5)
    assert out["verdict"] == "PASS", out
    assert out["checks"]["power"] and out["checks"]["sharpe"] and out["checks"]["positive_return"]
    assert out["t_stat"] >= 1.8


def test_autocorrelated_window_not_overcounted():
    """#424: a low-t edge over a long but serially-correlated window must NOT pass on raw day
    count. eff_obs (n / IACT) should drop well below n for strongly autocorrelated returns."""
    rng = np.random.default_rng(11)
    # AR(1) with high persistence, tiny drift -> many days but few INDEPENDENT obs, low t
    n = 60
    e = rng.normal(0.0002, 0.004, n)
    r = np.zeros(n)
    for i in range(1, n):
        r[i] = 0.7 * r[i - 1] + e[i]
    out = evaluate_forward(r)
    assert out["eff_obs"] < n            # autocorrelation-adjusted below raw days
    assert out["iact"] > 1.0


def test_trade_cohort_power_route():
    """#424: a cross-sectional strategy with many INDEPENDENT bets can clear the power check
    via the cluster-adjusted trade route even when the daily series alone is underpowered."""
    rng = np.random.default_rng(12)
    daily = rng.normal(0.0004, 0.006, 30)          # positive but modest daily t
    # 40 independent bets, each cohort a distinct entry month, strong per-bet edge
    tr = rng.normal(0.01, 0.03, 40)
    cohorts = list(range(40))
    out = evaluate_forward(daily, trade_returns=tr, trade_cohorts=cohorts)
    assert out["n_bets"] == 40
    assert out["trade_t"] is not None


def test_negative_edge_after_window_fails():
    rng = np.random.default_rng(1)
    r = rng.normal(-0.0006, 0.006, 40)
    out = evaluate_forward(r)
    assert out["verdict"] == "FAIL", out


def test_too_early_is_insufficient():
    rng = np.random.default_rng(2)
    r = rng.normal(0.001, 0.005, 8)   # only 8 days < min_days 20
    out = evaluate_forward(r)
    assert out["verdict"] == "INSUFFICIENT" and not out["checks"]["min_days"]


def test_positive_but_underpowered_is_insufficient():
    rng = np.random.default_rng(3)
    # positive but tiny/noisy edge over exactly 20 days -> not enough power, not negative
    r = rng.normal(0.0001, 0.02, 22)
    out = evaluate_forward(r)
    assert out["verdict"] in ("INSUFFICIENT", "FAIL")
    if out["verdict"] == "INSUFFICIENT":
        assert out["cum_return"] != 0


def test_clv_gate_blocks_when_negative():
    rng = np.random.default_rng(4)
    r = rng.normal(0.0008, 0.006, 40)
    out = evaluate_forward(r, clv=-0.2)   # negative CLV must block PASS
    assert out["verdict"] != "PASS" and out["checks"]["clv"] is False


def test_days_to_decision_projects():
    rng = np.random.default_rng(5)
    r = rng.normal(0.0006, 0.008, 15)
    eta = days_to_decision(r)
    assert "eta_days" in eta


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
