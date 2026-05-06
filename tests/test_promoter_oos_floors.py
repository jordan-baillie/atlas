"""Tests for OOS trade count + CAGR + Sharpe floors per audit Rec 1.2-1.4.

These test the gate logic in _run_oos_validation through result parsing.
"""
from unittest.mock import patch, MagicMock
from research.promoter import _run_oos_validation
import json
import tempfile
from pathlib import Path


def _build_oos_result(oos_sharpe=0.5, oos_cagr=10.0, oos_trades=50, is_cagr=8.0,
                      oos_pf=1.5, n_perturb=10, collapse=2):
    return {
        "test1_time_period_split": {
            "in_sample": {"cagr_pct": is_cagr},
            "out_of_sample": {
                "sharpe": oos_sharpe,
                "profit_factor": oos_pf,
                "cagr_pct": oos_cagr,
                "total_trades": oos_trades,
            },
        },
        "test2_perturbation": {"collapse_count": collapse},
        "n_perturbation_trials": n_perturb,
        "summary": {},
    }


def _run_with_mocked_oos(mock_data, candidate_config=None, market="sp500"):
    candidate_config = candidate_config or {"strategies": {"mean_reversion": {"enabled": True}}}
    with patch("research.promoter.subprocess.run") as mock_proc:
        mock_proc.return_value = MagicMock(returncode=0, stdout="", stderr="")
        with patch("pathlib.Path.exists", return_value=True), \
             patch("pathlib.Path.read_text", return_value=json.dumps(mock_data)), \
             patch("pathlib.Path.unlink"):
            return _run_oos_validation(candidate_config, market)


def test_oos_sharpe_floor_below_threshold():
    """Sub-rec 1.2: OOS Sharpe < 0.3 fails."""
    r = _run_with_mocked_oos(_build_oos_result(oos_sharpe=0.2))
    assert not r["pass"], f"Expected fail, got: {r}"
    assert "0.3" in r["reason"] or "Sharpe" in r["reason"]


def test_oos_sharpe_floor_at_zero():
    """OOS Sharpe = 0 fails (was the old boundary, now stricter)."""
    r = _run_with_mocked_oos(_build_oos_result(oos_sharpe=0.0))
    assert not r["pass"]


def test_oos_sharpe_floor_exactly_at_threshold():
    """OOS Sharpe = 0.3 exactly should pass the Sharpe gate (but may fail others)."""
    r = _run_with_mocked_oos(_build_oos_result(
        oos_sharpe=0.3, oos_cagr=10.0, oos_trades=50, is_cagr=8.0,
        oos_pf=1.5, n_perturb=10, collapse=2,
    ))
    # At exactly 0.3 the Sharpe gate should not fire
    assert "Sharpe" not in r.get("reason", "") or r["pass"], f"Got: {r}"


def test_oos_trades_floor():
    """Sub-rec 1.3: OOS trades < 30 fails."""
    r = _run_with_mocked_oos(_build_oos_result(oos_trades=29))
    assert not r["pass"], f"Expected fail, got: {r}"
    assert "30" in r["reason"] or "trades" in r["reason"].lower()


def test_oos_trades_floor_at_boundary():
    """OOS trades = 30 should pass the trade-count gate."""
    r = _run_with_mocked_oos(_build_oos_result(
        oos_sharpe=0.8, oos_trades=30, oos_cagr=15.0, oos_pf=2.0,
        is_cagr=8.0, n_perturb=10, collapse=1,
    ))
    assert "30" not in r.get("reason", "") or r["pass"], f"Got: {r}"


def test_oos_cagr_floor():
    """Sub-rec 1.4: OOS CAGR < 5% fails."""
    r = _run_with_mocked_oos(_build_oos_result(oos_cagr=4.0))
    assert not r["pass"], f"Expected fail, got: {r}"
    assert "5" in r["reason"] or "CAGR" in r["reason"]


def test_oos_cagr_floor_negative():
    """Negative OOS CAGR fails (was only checked via degradation % before)."""
    r = _run_with_mocked_oos(_build_oos_result(oos_cagr=-10.0))
    assert not r["pass"], f"Expected fail for negative OOS CAGR, got: {r}"


def test_oos_cagr_floor_zero():
    """OOS CAGR = 0 fails."""
    r = _run_with_mocked_oos(_build_oos_result(oos_cagr=0.0))
    assert not r["pass"]


def test_oos_all_pass():
    """Comfortable margins on all floors → pass."""
    r = _run_with_mocked_oos(_build_oos_result(
        oos_sharpe=0.8, oos_trades=100, oos_cagr=15.0, oos_pf=2.0,
        is_cagr=8.0, n_perturb=10, collapse=1,
    ))
    assert r["pass"], f"Expected pass, got: {r}"


def test_oos_result_includes_cagr_degradation_for_diagnostics():
    """Rec 1.4: cagr_degradation_pct still present in result dict for diagnostics."""
    r = _run_with_mocked_oos(_build_oos_result(
        oos_sharpe=0.8, oos_trades=100, oos_cagr=15.0, oos_pf=2.0,
        is_cagr=8.0, n_perturb=10, collapse=1,
    ))
    assert "cagr_degradation_pct" in r, "cagr_degradation_pct must be present for diagnostics"
