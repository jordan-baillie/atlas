"""Tests for cross_oos.gates — Plan §2 hard-gate evaluation."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from research.cross_oos import gates as g  # noqa: E402


def _passing_bundle() -> dict:
    return {
        "median_cpcv_sharpe": 1.1,
        "frac_paths_positive": 0.72,
        "pbo": 0.20,
        "dsr": 0.98,
        "top_asset_frac": 0.25,
        "loo_venue_ok": True,
        "min_regime_sharpe": 0.3,
        "max_regime_pnl_frac": 0.40,
        "cost_stress_sharpe": 0.7,
        "forward_net": 0.03,
    }


def test_full_pass_bundle():
    rep = g.evaluate_gates(_passing_bundle())
    assert rep["overall_pass"] is True
    assert rep["n_fail"] == 0 and rep["n_missing"] == 0


def test_single_failing_gate_fails_overall():
    b = _passing_bundle()
    b["pbo"] = 0.65  # above the 0.50 ceiling
    rep = g.evaluate_gates(b)
    assert rep["overall_pass"] is False
    assert rep["n_fail"] == 1
    failed = [r.name for r in rep["gates"] if r.status == "fail"]
    assert failed == ["pbo"]


def test_missing_measurement_is_not_a_pass():
    b = _passing_bundle()
    del b["forward_net"]  # never ran the forward holdout
    rep = g.evaluate_gates(b)
    assert rep["overall_pass"] is False
    assert rep["n_missing"] == 1


def test_nonfinite_value_is_missing():
    b = _passing_bundle()
    b["median_cpcv_sharpe"] = float("nan")
    rep = g.evaluate_gates(b)
    assert rep["overall_pass"] is False
    assert any(r.status == "missing" and r.name == "median_cpcv_sharpe" for r in rep["gates"])


def test_concentration_and_regime_boundaries():
    b = _passing_bundle()
    b["top_asset_frac"] = 0.35   # gate is strict <0.35 → fail at exactly 0.35
    assert g.evaluate_gates(b)["overall_pass"] is False
    b = _passing_bundle()
    b["max_regime_pnl_frac"] = 0.50  # gate is <=0.50 → pass at exactly 0.50
    assert g.evaluate_gates(b)["overall_pass"] is True


def test_format_report_runs():
    rep = g.evaluate_gates(_passing_bundle())
    txt = g.format_report(rep)
    assert "Cross-OOS gates: PASS" in txt
