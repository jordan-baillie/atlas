"""Forward-evidence gate for the rapid validate->live pipeline.

Board memo 2026-06-03-rapid-validate-to-live-pipeline (5-0): replaces the slow
"40-50 closed trades" bar with a statistical-power, time/return-based standard computed
from a strategy's FORWARD (paper or micro-live) daily net-of-cost return series. A strategy
clears the gate once it has run long enough to show, with statistical power, a positive
net-of-cost edge. High-turnover strategies clear in weeks; slow ones take longer (and that
is correct, not a bug).

Three verdicts:
  PASS         - enough evidence of a positive net-of-cost edge -> may advance (micro-live).
  INSUFFICIENT - positive so far but not enough days / power yet -> keep running (auto-extend).
  FAIL         - after the minimum window the edge is non-positive -> revert / cut.

Pure functions over a daily net-of-cost return series (+ optional CLV). No I/O, no network.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from research.cross_oos import metrics as cm

TRADING_DAYS = 252

# Defaults per the board memo. min_t OR min_eff_obs satisfies the power requirement.
DEFAULTS = {
    "min_days": 20,        # minimum forward trading days before any PASS/FAIL
    "min_sharpe": 0.5,     # annualised forward Sharpe floor
    "min_t": 1.8,          # t-stat of daily net returns (~one-sided 96%)
    "min_eff_obs": 30,     # alternative power route: enough independent observations
    "max_clv_required": True,  # require CLV >= 0 when a CLV figure is supplied
}


def _t_stat(returns: np.ndarray) -> float:
    """t-stat of mean daily net return vs zero (mean / standard error)."""
    r = np.asarray(returns, dtype=float)
    r = r[np.isfinite(r)]
    if r.size < 3:
        return float("nan")
    sd = r.std(ddof=1)
    if sd == 0:
        return float("nan")
    return float(r.mean() / (sd / np.sqrt(r.size)))


def evaluate_forward(
    returns_daily,
    *,
    clv: float | None = None,
    min_days: int = DEFAULTS["min_days"],
    min_sharpe: float = DEFAULTS["min_sharpe"],
    min_t: float = DEFAULTS["min_t"],
    min_eff_obs: int = DEFAULTS["min_eff_obs"],
    periods: int = TRADING_DAYS,
) -> dict:
    """Evaluate a forward daily net-of-cost return series against the power-based gate.

    Parameters
    ----------
    returns_daily : per-period (daily) NET-OF-COST fractional returns from paper/micro-live.
    clv           : optional Closing-Line-Value figure (>=0 required if supplied).

    Returns a dict with verdict (PASS|INSUFFICIENT|FAIL), per-check booleans, and metrics.
    """
    r = pd.Series(returns_daily, dtype=float).replace([np.inf, -np.inf], np.nan).dropna()
    n = int(len(r))
    if n == 0:
        return {"verdict": "INSUFFICIENT", "reason": "no forward data", "n_days": 0,
                "checks": {}, "cum_return": 0.0, "sharpe": float("nan"),
                "t_stat": float("nan"), "eff_obs": 0, "clv": clv}

    arr = r.to_numpy()
    cum = float(np.sum(arr))                       # additive net return (board convention)
    sharpe = cm.annualized_sharpe(arr, periods)
    t = _t_stat(arr)
    eff_obs = n                                    # daily obs proxy (independent-ish)
    power_ok = (t == t and t >= min_t) or (eff_obs >= min_eff_obs)
    clv_ok = (clv is None) or (clv >= 0)

    checks = {
        "min_days": n >= min_days,
        "positive_return": cum > 0,
        "sharpe": (sharpe == sharpe and sharpe >= min_sharpe),
        "clv": clv_ok,
        "power": bool(power_ok),
    }

    # Verdict ladder: too-early -> INSUFFICIENT; negative-after-window -> FAIL;
    # all-pass -> PASS; positive-but-underpowered -> INSUFFICIENT (auto-extend).
    if n < min_days:
        verdict, reason = "INSUFFICIENT", f"only {n} of {min_days} min forward days"
    elif cum <= 0 or (sharpe == sharpe and sharpe < 0):
        verdict, reason = "FAIL", "non-positive net-of-cost edge after minimum window"
    elif all(checks.values()):
        verdict, reason = "PASS", "positive net-of-cost edge with sufficient power"
    else:
        missing = [k for k, v in checks.items() if not v]
        verdict, reason = "INSUFFICIENT", f"positive but not yet sufficient: {missing}"

    return {
        "verdict": verdict, "reason": reason, "n_days": n,
        "cum_return": round(cum, 6),
        "sharpe": None if sharpe != sharpe else round(float(sharpe), 4),
        "t_stat": None if t != t else round(float(t), 3),
        "eff_obs": eff_obs, "clv": clv, "checks": checks,
        "thresholds": {"min_days": min_days, "min_sharpe": min_sharpe,
                       "min_t": min_t, "min_eff_obs": min_eff_obs},
    }


def days_to_decision(returns_daily, **kw) -> dict:
    """Diagnostic: estimate how many more forward days until a likely PASS at the current rate.

    Uses the current daily mean/std to project when the t-stat would reach min_t (rough).
    """
    r = pd.Series(returns_daily, dtype=float).dropna().to_numpy()
    min_t = kw.get("min_t", DEFAULTS["min_t"])
    if r.size < 5 or r.std(ddof=1) == 0 or r.mean() <= 0:
        return {"eta_days": None, "note": "need positive mean + variance to project"}
    # t = mean/(sd/sqrt(n)) -> n_needed = (min_t*sd/mean)^2
    n_needed = (min_t * r.std(ddof=1) / r.mean()) ** 2
    return {"eta_days": int(max(0, np.ceil(n_needed - r.size))),
            "n_needed_total": int(np.ceil(n_needed)), "n_so_far": int(r.size)}


__all__ = ["TRADING_DAYS", "DEFAULTS", "evaluate_forward", "days_to_decision"]
