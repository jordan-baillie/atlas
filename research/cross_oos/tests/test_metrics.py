"""Tests for cross_oos.metrics."""
import math
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from research.cross_oos import metrics as m  # noqa: E402


def test_sharpe_matches_convention():
    r = np.array([0.01, -0.02, 0.03, 0.00, 0.015])
    expected = r.mean() / r.std(ddof=1)  # per-period
    assert m.sharpe(r, periods=1) == pytest.approx(expected)
    assert m.annualized_sharpe(r, 365) == pytest.approx(expected * math.sqrt(365))


def test_sharpe_edges():
    assert math.isnan(m.sharpe([0.01]))           # < 2 obs
    assert math.isnan(m.sharpe([0.01, 0.01]))     # zero std


def test_profit_factor():
    r = np.array([2.0, -1.0, 3.0, -1.0])  # gains 5, losses 2
    assert m.profit_factor(r) == pytest.approx(2.5)
    assert m.profit_factor([1.0, 2.0]) == float("inf")  # no losses


def test_equity_and_drawdown():
    r = np.array([0.10, -0.20, 0.05])
    eq = m.equity_curve(r)                # [1.1, 0.9, 0.95]
    assert eq[-1] == pytest.approx(0.95)
    # peak 1.1 → trough 0.9 → dd = 0.2/1.1
    assert m.max_drawdown(eq) == pytest.approx(0.2 / 1.1)


def test_skew_kurtosis_normal():
    rng = np.random.default_rng(0)
    r = rng.standard_normal(20000)
    assert m.skewness(r) == pytest.approx(0.0, abs=0.1)
    assert m.kurtosis(r, excess=True) == pytest.approx(0.0, abs=0.2)
    assert m.kurtosis(r, excess=False) == pytest.approx(3.0, abs=0.2)


def test_summary_keys():
    s = m.summary(np.array([0.01, -0.01, 0.02, 0.0]))
    for k in ("n", "net", "sharpe_ann", "sharpe_raw", "profit_factor",
              "max_drawdown", "skew", "kurtosis"):
        assert k in s
    assert s["n"] == 4
