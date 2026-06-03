"""Smoke test for the rewritten scripts/validate_oos.py.

Monkeypatches load_data + run_backtest with synthetic BacktestResults so the full
orchestration runs in milliseconds (no real backtests), then asserts:
  - the new authoritative cross_oos section exists with gate_checks + verdict, and
  - the exact legacy keys the compiled TS extensions read are still present and typed
    correctly (the back-compat contract for atlas_risk_check_reopt_promotion).
"""
from __future__ import annotations

import importlib
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ATLAS_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ATLAS_ROOT))


@dataclass
class FakeResult:
    equity_curve: pd.Series = field(default_factory=lambda: pd.Series(dtype=float))
    trades: list = field(default_factory=list)
    metrics: dict = field(default_factory=dict)
    walk_forward_windows: list = field(default_factory=list)


def _make_fake_result(seed: int, n_days: int = 400):
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2022-01-03", periods=n_days, freq="B")
    rets = rng.normal(0.0007, 0.009, n_days)  # mild positive drift
    equity = 10000.0 * np.cumprod(1.0 + rets)
    eq = pd.Series(equity, index=idx)

    tickers = [f"T{i:02d}" for i in range(15)]
    regimes = ["bull", "bear", "chop", "neutral"]
    trades = []
    for d in idx[::3]:
        for _ in range(2):
            trades.append({
                "ticker": str(rng.choice(tickers)),
                "pnl": float(rng.normal(5.0, 30.0)),
                "exit_date": d,
                "entry_regime": str(rng.choice(regimes)),
            })
    # walk-forward windows (mostly positive)
    windows = []
    eqs = 10000.0
    for w in range(6):
        ee = eqs * (1.0 + rng.normal(0.02, 0.05))
        windows.append({
            "window": w, "test_start": idx[w * 50], "test_end": idx[min(w * 50 + 49, n_days - 1)],
            "trades": 20, "pnl": round(ee - eqs, 2),
            "equity_start": round(eqs, 2), "equity_end": round(ee, 2),
        })
        eqs = ee
    metrics = {
        "total_trades": len(trades), "cagr": 0.12, "sharpe": 0.9,
        "profit_factor": 1.6, "max_drawdown": 0.08, "win_rate": 0.52,
        "total_pnl": float(eq.iloc[-1] - 10000.0), "sortino": 1.1,
        "avg_trade": 5.0, "final_equity": float(eq.iloc[-1]),
    }
    return FakeResult(equity_curve=eq, trades=trades, metrics=metrics, walk_forward_windows=windows)


def test_validate_oos_emits_battery_and_legacy_contract(tmp_path, monkeypatch):
    vo = importlib.import_module("scripts.validate_oos")

    # Synthetic universe for split-date computation (needs DatetimeIndex + >= MIN_ROWS rows).
    idx = pd.date_range("2022-01-03", periods=400, freq="B")
    fake_data = {f"T{i:02d}": pd.DataFrame({"close": np.linspace(10, 20, 400)}, index=idx)
                 for i in range(15)}
    monkeypatch.setattr(vo, "load_data", lambda market=None: fake_data)

    call = {"n": 0}

    def fake_run_backtest(cfg, data, label=""):
        call["n"] += 1
        return _make_fake_result(seed=call["n"]), 0.01

    monkeypatch.setattr(vo, "run_backtest", fake_run_backtest)

    config_path = ATLAS_ROOT / "config" / "active" / "sp500.json"
    if not config_path.exists():
        pytest.skip("config/active/sp500.json not present")
    out_path = tmp_path / "oos_smoke.json"

    monkeypatch.setattr(sys, "argv", [
        "validate_oos.py",
        "--config-path", str(config_path),
        "--output-path", str(out_path),
        "--market", "sp500",
        "--grid-size", "4",
    ])

    rc = vo.main()
    assert rc == 0
    assert out_path.exists()
    d = json.loads(out_path.read_text())

    # --- New authoritative section ---
    assert "cross_oos" in d
    co = d["cross_oos"]
    assert set(co["bundle"]).issuperset({
        "median_cpcv_sharpe", "frac_paths_positive", "pbo", "dsr",
        "top_group_frac", "loo_group_ok", "min_regime_sharpe",
        "regime_concentration_ratio", "per_regime_expectancy_ok",
        "forward_net", "oos_cagr_degradation_ok",
    })
    assert co["verdict"] in {"PROMOTE", "SCREEN", "FAIL"}
    assert co["tier"] == co["verdict"]
    assert {"promote", "screen"} <= set(co["gate_summary"])
    assert "dsr_source" in co["diagnostics"]

    # --- Legacy back-compat contract read by atlas_risk_check_reopt_promotion (TS) ---
    t1 = d["test1_time_period_split"]
    assert isinstance(t1["out_of_sample"]["sharpe"], (int, float))
    assert isinstance(t1["out_of_sample"]["profit_factor"], (int, float))
    assert "cagr_pct" in t1["degradation_pct"]
    assert isinstance(d["test2_perturbation"]["robust"], bool)
    assert isinstance(
        d["test3_walkforward_consistency"]["window_analysis"]["win_rate_windows_pct"],
        (int, float),
    )
    assert d["summary"]["overall_verdict"] in {"PASS", "SCREEN", "FAIL"}
    # Only a PROMOTE-tier pass maps to overall_verdict == 'PASS'.
    assert (d["summary"]["overall_verdict"] == "PASS") == (co["tier"] == "PROMOTE")


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
