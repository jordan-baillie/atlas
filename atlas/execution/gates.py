"""atlas.execution.gates — go-live gate evidence (G6 slippage, G7 broker errors, track-vs-expectation).

Read-only over data/live/<name>/{fills,runs,returns}.jsonl. Bars per board policy
(crucible LIVE_INTEGRATION_MAP): slippage median <= 2x modeled cost (8 bps modeled
-> 16 bps bar), broker-error rate < 1%. The track gate wraps
atlas.execution.track_expectation.evaluate().

Every public function is best-effort: missing/empty data yields the null shape
with pass=None ("accruing"), and internal errors never propagate. All floats are
JSON-safe (NaN sanitized to None — the insufficient track verdict carries NaNs).
"""
from __future__ import annotations

import json
import logging
import math
import statistics
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from atlas.kernel.paths import LIVE_DATA_DIR as LIVE_DATA  # patched by tests/conftest.py

logger = logging.getLogger(__name__)

SLIPPAGE_BAR_BPS = 16.0      # G6: 2x the 8 bps modeled cost (board memo 2026-06-09 / commit 3b145fa)
BROKER_ERROR_BAR_PCT = 1.0   # G7: broker-error rate < 1% (crucible LIVE_INTEGRATION_MAP)
LOOKBACK_DAYS = 60           # evidence window (relevance, not perf — files are ~1 line/day)


def _jsonl(p: Path) -> list:
    """Tolerant JSONL reader (mirrors record_fills._jsonl)."""
    if not p.exists():
        return []
    out = []
    try:
        for line in p.read_text().splitlines():
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    except OSError:
        return []
    return out


def _cutoff(lookback_days: int, today: Optional[date] = None) -> str:
    """ISO date string N days back; string comparison works on YYYY-MM-DD."""
    d = today or datetime.now(timezone.utc).date()
    return (d - timedelta(days=lookback_days)).isoformat()


def _safe(v) -> Optional[float]:
    """NaN/inf -> None; everything else float()ed (JSON-safe)."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def slippage_gate(fills: list, *, bar_bps: float = SLIPPAGE_BAR_BPS,
                  lookback_days: int = LOOKBACK_DAYS, today: Optional[date] = None) -> dict:
    """G6 — median signed slippage (bps, + = adverse) over filled orders in the window."""
    out = {"median_bps": None, "p75_bps": None, "worst_bps": None, "n_fills": 0,
           "lookback_days": lookback_days, "bar_bps": bar_bps, "pass": None}
    try:
        cut = _cutoff(lookback_days, today)
        vals = [float(f["slippage_bps"]) for f in fills
                if f.get("slippage_bps") is not None and str(f.get("date", "")) >= cut]
        if not vals:
            return out
        out["n_fills"] = len(vals)
        out["median_bps"] = round(statistics.median(vals), 2)
        if len(vals) >= 4:
            out["p75_bps"] = round(statistics.quantiles(vals, n=4)[2], 2)
        out["worst_bps"] = round(max(vals), 2)
        out["pass"] = out["median_bps"] <= bar_bps
    except Exception as e:
        logger.warning("gates: slippage_gate failed: %s", e)
    return out


def broker_error_gate(runs: list, *, bar_pct: float = BROKER_ERROR_BAR_PCT,
                      lookback_days: int = LOOKBACK_DAYS, today: Optional[date] = None) -> dict:
    """G7 — broker rejection rate over real (non-dry, non-blocked) orders in the window.

    An error is an order row with ok == False. ok == None means the broker-result
    join never happened (counted separately as n_unmatched, out of the denominator).
    """
    out = {"n_orders": 0, "n_errors": 0, "n_unmatched": 0,
           "error_rate_pct": None, "bar_pct": bar_pct, "pass": None}
    try:
        cut = _cutoff(lookback_days, today)
        for run in runs:
            if run.get("dry_run") or run.get("blocked"):
                continue  # never reached the broker (mirror record_fills filter)
            if str(run.get("date", "")) < cut:
                continue
            for o in run.get("orders", []):
                ok = o.get("ok")
                if ok is None:
                    out["n_unmatched"] += 1
                    continue
                out["n_orders"] += 1
                if ok is False:
                    out["n_errors"] += 1
        if out["n_orders"] > 0:
            rate = out["n_errors"] / out["n_orders"] * 100.0
            out["error_rate_pct"] = round(rate, 3)
            out["pass"] = rate < bar_pct
    except Exception as e:
        logger.warning("gates: broker_error_gate failed: %s", e)
    return out


def track_gate(realized: list, expectation: Optional[dict]) -> dict:
    """Track-vs-expectation verdict (wraps track_expectation.evaluate; NaN-safe)."""
    out = {"status": None, "n_obs": 0, "realized_mean": None, "realized_sharpe": None,
           "expected_sharpe": None, "mean_z": None, "worst_daily_z": None,
           "reasons": [], "pass": None}
    try:
        if not expectation:
            return out
        out["expected_sharpe"] = _safe(expectation.get("sharpe"))
        from atlas.execution.track_expectation import Expectation, evaluate
        v = evaluate(realized, Expectation(**expectation))
        out.update({
            "status": v.status, "n_obs": v.n_obs,
            "realized_mean": _safe(v.realized_mean),
            "realized_sharpe": _safe(v.realized_sharpe),
            "mean_z": _safe(v.mean_z), "worst_daily_z": _safe(v.worst_daily_z),
            "reasons": list(v.reasons), "pass": v.ok,
        })
    except Exception as e:
        logger.warning("gates: track_gate failed: %s", e)
    return out


def _tri_state(passes: list) -> Optional[bool]:
    """False if any False; None if no False but any None; True otherwise (non-empty)."""
    if not passes:
        return None
    if any(p is False for p in passes):
        return False
    if any(p is None for p in passes):
        return None
    return True


def evaluate_gates(name: str, expectation: Optional[dict] = None, *,
                   base: Optional[Path] = None, lookback_days: int = LOOKBACK_DAYS) -> dict:
    """All three gates for one deployed strategy, from its data/live/<name>/ files."""
    try:
        d = (base if base is not None else LIVE_DATA) / name
        fills = _jsonl(d / "fills.jsonl")
        runs = _jsonl(d / "runs.jsonl")
        realized = []
        for r in _jsonl(d / "returns.jsonl"):
            v = _safe(r.get("ret"))
            if v is not None:
                realized.append(v)
        out = {
            "slippage": slippage_gate(fills, lookback_days=lookback_days),
            "broker_errors": broker_error_gate(runs, lookback_days=lookback_days),
            "track": track_gate(realized, expectation),
        }
        out["pass"] = _tri_state([out["slippage"]["pass"], out["broker_errors"]["pass"],
                                  out["track"]["pass"]])
        return out
    except Exception as e:
        logger.warning("gates: evaluate_gates failed for %s: %s", name, e)
        return {"slippage": slippage_gate([]), "broker_errors": broker_error_gate([]),
                "track": track_gate([], None), "pass": None}


def rollup(per_strategy: dict) -> dict:
    """Overall verdict across strategies (same tri-state logic)."""
    passes = [g.get("pass") for g in per_strategy.values()]
    return {
        "pass": _tri_state(passes),
        "n_strategies": len(per_strategy),
        "n_pass": sum(1 for p in passes if p is True),
        "n_fail": sum(1 for p in passes if p is False),
        "failing": sorted(n for n, g in per_strategy.items() if g.get("pass") is False),
    }
