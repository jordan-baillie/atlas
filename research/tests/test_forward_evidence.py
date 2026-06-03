"""Tests for the forward-evidence gate (rapid pipeline #418)."""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from research.forward_evidence import evaluate_forward, days_to_decision  # noqa: E402


def test_strong_positive_edge_passes():
    rng = np.random.default_rng(0)
    # ~0.08%/day mean, 0.6% std over 40 days -> strong positive, high t
    r = rng.normal(0.0008, 0.006, 40)
    out = evaluate_forward(r, clv=0.5)
    assert out["verdict"] == "PASS", out
    assert out["checks"]["power"] and out["checks"]["sharpe"] and out["checks"]["positive_return"]


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
