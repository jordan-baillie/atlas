"""live/track_expectation.py — Track-vs-expectation gate.

Atlas's reconcile-shadow checks broker-vs-internal STATE; this checks strategy-vs-BACKTEST. It answers the
board's (2026-06-09) forward-paper question: is the live/shadow strategy tracking its MODELED expectation, or
diverging (an execution problem or a dead edge)? Pure + side-effect-free: realized daily returns + the modeled
backtest expectation -> a verdict the promotion/halt logic consumes.

Forward-paper bar to unlock real capital (the caller checks n_trades>=40-50, >=2 regimes too):
  +ve net-of-cost expectancy AND not diverging from the model.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np


@dataclass
class Expectation:
    """The MODELED (backtest) daily-return distribution for the strategy."""
    daily_mean: float
    daily_std: float
    sharpe: float = 0.0          # modeled annualized Sharpe (optional, for the catastrophe check)


@dataclass
class TrackVerdict:
    n_obs: int
    realized_mean: float
    realized_std: float
    realized_sharpe: float
    expectancy_positive: bool
    mean_z: float                # (realized - modeled mean) / SE  (negative = below model)
    worst_daily_z: float         # largest |daily - modeled mean| / modeled std (execution-anomaly detector)
    status: str                  # insufficient | on_track | diverging | halt
    reasons: list = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status in ("on_track", "insufficient")


def evaluate(realized_daily, model: Expectation, *, min_obs: int = 20, ann: int = 252,
             max_mean_z: float = 3.0, max_daily_z: float = 4.0,
             min_sharpe_frac: float = -0.25) -> TrackVerdict:
    """realized_daily: net-of-cost daily returns observed live/shadow. Returns a TrackVerdict.

    - HALT     : enough data AND net expectancy <= 0 (fails the forward-paper bar).
    - diverging: realized mean far BELOW model (mean_z < -max_mean_z), OR a daily move > max_daily_z from model
                 (execution anomaly), OR realized Sharpe collapses below min_sharpe_frac * modeled.
    - on_track : positive expectancy and tracking the model within tolerance.
    """
    r = np.asarray([x for x in realized_daily if x == x], dtype=float)  # drop NaN
    n = int(r.size)
    if n < min_obs:
        return TrackVerdict(n, float("nan"), float("nan"), float("nan"), False, float("nan"),
                            float("nan"), "insufficient", [f"only {n} obs (<{min_obs})"])

    rm, rs = float(r.mean()), float(r.std(ddof=1)) if n > 1 else 0.0
    rsharpe = float(rm / rs * np.sqrt(ann)) if rs > 0 else 0.0
    se = (model.daily_std / np.sqrt(n)) if model.daily_std > 0 else float("nan")
    mean_z = float((rm - model.daily_mean) / se) if se and se == se and se > 0 else 0.0
    worst_daily_z = float(np.max(np.abs(r - model.daily_mean)) / model.daily_std) if model.daily_std > 0 else 0.0

    reasons, status = [], "on_track"
    expectancy_positive = rm > 0
    if not expectancy_positive:
        status = "halt"
        reasons.append(f"net expectancy <= 0 ({rm:.5f}/day over {n} obs) — fails forward-paper bar")
    else:
        if mean_z < -max_mean_z:
            status = "diverging"
            reasons.append(f"realized mean {mean_z:.1f} SE below model")
        if worst_daily_z > max_daily_z:
            status = "diverging"
            reasons.append(f"a daily move {worst_daily_z:.1f}std off model (execution anomaly?)")
        if model.sharpe > 0 and rsharpe < min_sharpe_frac * model.sharpe:
            status = "diverging"
            reasons.append(f"realized Sharpe {rsharpe:.2f} << modeled {model.sharpe:.2f}")

    return TrackVerdict(n, rm, rs, rsharpe, expectancy_positive, mean_z, worst_daily_z, status, reasons)
