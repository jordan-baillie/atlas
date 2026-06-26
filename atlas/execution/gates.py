"""atlas.execution.gates — go-live gate evidence (G6 slippage, G7 broker errors, track-vs-expectation).

Read-only over data/live/<name>/{fills,runs,returns}.jsonl.

TWO DISTINCT COST QUANTITIES (do not conflate):
  • executor config slippage_pct=0.0005 (5 bps, one-sided, paper-fill simulation) — NOT a gate bar.
  • MODELED_COST_BPS per-book backtest round-trip turnover cost the strategy edge was net-of (e.g. 8 bps).
The G6 bar = SLIPPAGE_MULT × MODELED_COST_BPS[book] (2 × 8 = 16 bps), deriving directly from
crucible/forward/evidence.py's frozen per-book cost spec. The two quantities measure different things.

Bars per board policy (crucible LIVE_INTEGRATION_MAP): slippage median <= 2x modeled cost,
broker-error rate < 1%. The track gate wraps atlas.execution.track_expectation.evaluate().

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

SLIPPAGE_BAR_BPS = 16.0      # G6 default bar = SLIPPAGE_MULT * 8.0 bps; see MODELED_COST_BPS below.
                             # The executor config slippage_pct=0.0005 (5 bps one-sided paper-fill
                             # simulation) is a DISTINCT quantity — not this gate bar.
SLIPPAGE_MULT = 2.0          # G6 bar multiplier: bar = SLIPPAGE_MULT × MODELED_COST_BPS[book]
# Per-book frozen modeled cost = the design's PER-UNIT-TURNOVER (≈ per-side) cost the backtest
# edge was net-of (NOT round-trip; the ×2 multiplier supplies the round-trip headroom). Each value
# is verbatim from the book's frozen design cost spec (data/live/<book>/meta.json -> strategy_path).
# SINGLE SOURCE OF TRUTH is crucible/forward/evidence.py:MODELED_COST_BPS; this table MIRRORS it and
# is drift-guarded by tests/execution/test_modeled_cost_sync.py. Board sign-off required to add/edit.
MODELED_COST_BPS = {
    "val_mom_trend_smallcap": 8.0,  # auto_value_momentum_complementary_combination_smith2_96154.py:45
                                    #   cost_bps=8.0 "per-unit-turnover cost"
    "amihud_illiq_tranched_v3": 7.5,  # auto_amihud_illiquidity_premium_deployable_sh_smith1_99153.py:166
                                      #   asymmetric legs (long 30/side, short 7.5/side, hedge 2/side).
                                      #   A SELL fill is ambiguous (long-exit vs short-entry) so a
                                      #   single fill-based bar cannot map side->leg; we register the
                                      #   CONSERVATIVE tightest leg (7.5 -> bar 15) so no cheap-leg
                                      #   false-PASS is possible. Position-aware per-leg G6 is the
                                      #   proper refinement (board task) — until then this is strict.
}
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
                  modeled_cost_bps: float | None = None,
                  lookback_days: int = LOOKBACK_DAYS, today: Optional[date] = None) -> dict:
    """G6 — median signed slippage (bps, + = adverse) over filled orders in the window.

    Picker (mirrors crucible/forward/evidence.py _slip, Leg B Phase 2):
    prefers slippage_open_bps (CLEAN, vs official open) over slippage_bps
    (CONTAMINATED by stale IEX decision prices in thin names).  slip_ref records
    which reference was used: "open", "mixed", "decision_px(stale)", or None.

    When modeled_cost_bps is provided the bar is overridden to
    SLIPPAGE_MULT * modeled_cost_bps (per-book methodology); bar_bps is used only
    when modeled_cost_bps is None.

    The day-1 book build is EXCLUDED (canonical methodology, crucible
    forward/evidence.py _g6): establishing the whole book at once is a one-off
    cost, not the steady-state daily rebalance the gate measures. Build day =
    the earliest fill date across ALL fills (not just the window), so the
    exclusion stays correct even after the build day ages out of the lookback."""
    if modeled_cost_bps is not None:
        bar_bps = SLIPPAGE_MULT * modeled_cost_bps
    out = {"median_bps": None, "p75_bps": None, "worst_bps": None, "n_fills": 0,
           "lookback_days": lookback_days, "bar_bps": bar_bps, "pass": None,
           "build_day_excluded": None, "slip_ref": None}
    try:
        cut = _cutoff(lookback_days, today)
        build_day = min((str(f["date"]) for f in fills if f.get("date")), default=None)
        out["build_day_excluded"] = build_day

        def _slip(f):
            # Leg B Phase 2: prefer CLEAN official-open slippage; decision_px is contaminated
            # by stale IEX prices in thin names (mirror crucible/forward/evidence.py _slip).
            v = f.get("slippage_open_bps")
            if v is not None:
                return float(v), "open"
            v = f.get("slippage_bps")
            return (float(v), "decision_px") if v is not None else (None, None)

        picked = [_slip(f) for f in fills
                  if str(f.get("date", "")) >= cut and str(f.get("date", "")) != build_day]
        sl = [v for v, _ in picked if v is not None]
        refs = {r for _, r in picked if r}
        ref = None if not refs else (
            "open" if refs == {"open"} else (
                "mixed" if "open" in refs else "decision_px(stale)"
            )
        )
        out["slip_ref"] = ref
        if not sl:
            out["reason"] = "accumulating"
            return out
        out["n_fills"] = len(sl)
        out["median_bps"] = round(statistics.median(sl), 2)
        if len(sl) >= 4:
            out["p75_bps"] = round(statistics.quantiles(sl, n=4)[2], 2)
        out["worst_bps"] = round(max(sl), 2)
        out["pass"] = out["median_bps"] <= bar_bps
    except Exception as e:
        logger.warning("gates: slippage_gate failed: %s", e)
    return out


def futures_slippage_gate(fills: list, *, lookback_days: int = LOOKBACK_DAYS,
                          today: Optional[date] = None) -> dict:
    """G6 (futures) — per-symbol median adverse slippage in TICKS vs the frozen bar.

    Pre-registered 2026-06-12 (tasks/IB_MICRO_ADAPTER_PLAN.md): the equity bps bar is
    the wrong ruler for futures (16 bps of MES ≈ 40 ticks). Bar per symbol =
    2-tick spread allowance + commission expressed in ticks (futures_cost_spec —
    same frozen table the executor trades from). Same day-1 build exclusion as the
    equity gate. Symbols PASS only if every traded symbol passes (tri-state).
    """
    out = {"per_symbol": {}, "n_fills": 0, "lookback_days": lookback_days,
           "pass": None, "build_day_excluded": None}
    try:
        from atlas.brokers.ib.broker import futures_cost_spec
        cut = _cutoff(lookback_days, today)
        build_day = min((str(f["date"]) for f in fills if f.get("date")), default=None)
        out["build_day_excluded"] = build_day
        by_sym: dict = {}
        for f in fills:
            if (f.get("slippage_ticks") is None or str(f.get("date", "")) < cut
                    or str(f.get("date", "")) == build_day):
                continue
            by_sym.setdefault(str(f.get("ticker", "")).upper(), []).append(float(f["slippage_ticks"]))
        verdicts = []
        for sym, vals in sorted(by_sym.items()):
            spec = futures_cost_spec(sym)
            if not spec:
                continue
            med = round(statistics.median(vals), 2)
            ok = med <= spec["bar_ticks"]
            out["per_symbol"][sym] = {"median_ticks": med, "bar_ticks": spec["bar_ticks"],
                                      "worst_ticks": round(max(vals), 2), "n_fills": len(vals),
                                      "pass": ok}
            out["n_fills"] += len(vals)
            verdicts.append(ok)
        if verdicts:
            out["pass"] = all(verdicts)
    except Exception as e:
        logger.warning("gates: futures_slippage_gate failed: %s", e)
    return out


def _classify_broker_error(err: str) -> str:
    """Coarse class for a broker rejection (err = lowercased order-row 'err' field).

    Classes are evidence categories, not excuses — everything except wash-trade collisions
    stays IN the G7 rate; the class breakdown just makes the rate diagnosable.
    """
    if not err:
        return "unknown"
    if "42210000" in err or "hard-to-borrow" in err:
        return "htb"
    if "halt" in err:
        return "halt"
    if "not active" in err or "not tradable" in err or "not found" in err:
        return "inactive_asset"
    if "buying power" in err or "insufficient" in err:
        return "buying_power"
    if "opg orders must be submitted" in err or "extended hours" in err:
        return "order_window"
    if "pdt" in err or "day trade" in err or "daytrade" in err:
        return "pdt"
    return "other"


def broker_error_gate(runs: list, *, bar_pct: float = BROKER_ERROR_BAR_PCT,
                      lookback_days: int = LOOKBACK_DAYS, today: Optional[date] = None) -> dict:
    """G7 — broker rejection rate over real (non-dry, non-blocked) orders in the window.

    An error is an order row with ok == False. ok == None means the broker-result
    join never happened (counted separately as n_unmatched, out of the denominator).
    """
    out = {"n_orders": 0, "n_errors": 0, "n_unmatched": 0, "n_excluded_wash": 0,
           "error_rate_pct": None, "bar_pct": bar_pct, "pass": None, "error_classes": {}}
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
                err = (o.get("err") or "").lower()
                # Wash-trade collisions are an artifact of N strategies sharing ONE paper
                # execution account (opposite-side open order from a sibling strategy) —
                # impossible on the dedicated accounts canary/live use, hence NOT
                # deployability evidence. Excluded from numerator AND denominator.
                if ok is False and "wash trade" in err:
                    out["n_excluded_wash"] += 1
                    continue
                out["n_orders"] += 1
                if ok is False:
                    out["n_errors"] += 1
                    cls = _classify_broker_error(err)
                    out["error_classes"][cls] = out["error_classes"].get(cls, 0) + 1
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
            # Tri-state honesty: "insufficient" is ACCRUING (None), not PASS.
            # TrackVerdict.ok includes insufficient (it shouldn't HALT the book),
            # but a go-live gate must not read PASS off 2 observations.
            "reasons": list(v.reasons),
            "pass": None if v.status == "insufficient" else v.ok,
        })
    except Exception as e:
        logger.warning("gates: track_gate failed: %s", e)
    return out


def fill_quality(fills: list, *, lookback_days: int = LOOKBACK_DAYS,
                 today: Optional[date] = None) -> dict:
    """Advisory fill-quality evidence (FIX 3 — NOT a registered go-live gate).

    Returns n_filled / n_cancelled / n_unresolved / n_total / fill_rate_pct over the
    lookback window. pass=None always (ADVISORY only — promoting fill-rate to a binary
    go-live blocker requires board pre-registration; see AGENTS.md and the board gate
    registry before raising this to a hard threshold).

    Counts ALL fills in the window (including build day, unlike G6 which excludes it).
    Status classification is case-insensitive: 'FILLED' and 'filled' are equivalent.
    Unresolved = submitted / pending / accepted / new / any non-terminal status.
    """
    TERMINAL_FILLED = {"filled", "partially_filled"}
    TERMINAL_CANCELLED = {"cancelled", "canceled", "expired", "rejected"}
    cut = _cutoff(lookback_days, today)
    n_filled = n_cancelled = n_unresolved = n_total = 0
    for f in fills:
        if str(f.get("date", "")) < cut:
            continue
        status = str(f.get("status", "") or "").lower().strip()
        n_total += 1
        if status in TERMINAL_FILLED:
            n_filled += 1
        elif status in TERMINAL_CANCELLED:
            n_cancelled += 1
        else:
            n_unresolved += 1
    fill_rate = round(n_filled / n_total * 100.0, 2) if n_total > 0 else None
    return {
        "n_filled": n_filled,
        "n_cancelled": n_cancelled,
        "n_unresolved": n_unresolved,
        "n_total": n_total,
        "fill_rate_pct": fill_rate,
        "pass": None,   # ADVISORY — not a registered hard gate
    }


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
                   base: Optional[Path] = None, lookback_days: int = LOOKBACK_DAYS,
                   strict_modeled_cost: bool = True) -> dict:
    """All three gates for one deployed strategy, from its data/live/<name>/ files.

    strict_modeled_cost defaults to True (canonical, matching crucible/forward/evidence.py
    _g6 which has NO opt-out): an equity book absent from MODELED_COST_BPS returns
    pass=None with reason='unregistered_modeled_cost' rather than being scored on the
    default 16 bps bar with contaminated decision_px data — a false FAIL/PASS is worse
    than an honest 'cannot evaluate'. Both None and False prevent an overall go-live PASS
    (see _tri_state), so this is strictly more honest with no safety regression. The
    strict=False escape hatch exists only for legacy diagnostics; production must not use it.
    """
    try:
        d = (base if base is not None else LIVE_DATA) / name
        fills = _jsonl(d / "fills.jsonl")
        runs = _jsonl(d / "runs.jsonl")
        realized = []
        for r in _jsonl(d / "returns.jsonl"):
            v = _safe(r.get("ret"))
            if v is not None:
                realized.append(v)
        # futures fills carry slippage_ticks (record_fills) -> judge G6 in tick space
        # (pre-reg 2026-06-12); bps stays as the diagnostic. Mixed books: any tick data
        # routes to the futures gate (a futures book judged in bps would false-pass).
        has_ticks = any(f.get("slippage_ticks") is not None for f in fills)
        if has_ticks:
            slip = futures_slippage_gate(fills, lookback_days=lookback_days)
            slip["ruler"] = "ticks"
        else:
            mc = MODELED_COST_BPS.get(name)
            if mc is not None:
                # Registered book: use per-book bar = SLIPPAGE_MULT × modeled cost
                slip = slippage_gate(fills, modeled_cost_bps=mc, lookback_days=lookback_days)
            elif strict_modeled_cost:
                # LOUD config gap: DEPLOYED book missing from MODELED_COST_BPS.
                # Mirror crucible/forward/evidence.py _g6 unregistered_modeled_cost path.
                # Gate cannot evaluate without the frozen cost spec — return pass=None
                # (NOT pass=False) to force the operator to register the book's cost.
                build_day = min(
                    (str(f["date"]) for f in fills if f.get("date")), default=None
                )
                slip = {
                    "median_bps": None, "p75_bps": None, "worst_bps": None, "n_fills": 0,
                    "lookback_days": lookback_days, "bar_bps": None, "pass": None,
                    "build_day_excluded": build_day, "slip_ref": None,
                    "reason": "unregistered_modeled_cost",
                    "note": (f"DEPLOYED book '{name}' missing from MODELED_COST_BPS — "
                             f"register its frozen modeled cost; G6 cannot evaluate until then"),
                }
            else:
                # Backward-compatible default: unregistered books use default bar (16 bps).
                slip = slippage_gate(fills, lookback_days=lookback_days)
            slip["ruler"] = "bps"
        out = {
            "slippage": slip,
            "broker_errors": broker_error_gate(runs, lookback_days=lookback_days),
            "track": track_gate(realized, expectation),
            "fill_quality": fill_quality(fills, lookback_days=lookback_days),
        }
        out["pass"] = _tri_state([out["slippage"]["pass"], out["broker_errors"]["pass"],
                                  out["track"]["pass"]])
        return out
    except Exception as e:
        logger.warning("gates: evaluate_gates failed for %s: %s", name, e)
        return {"slippage": slippage_gate([]), "broker_errors": broker_error_gate([]),
                "track": track_gate([], None), "fill_quality": fill_quality([]), "pass": None}


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
