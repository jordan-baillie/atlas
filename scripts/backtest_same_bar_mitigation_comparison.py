#!/usr/bin/env python3
"""Backtest comparison: momentum_breakout same-bar stop mitigation.

Tests 4 variants of atr_stop_mult to reduce the ~27% same-bar stop rate
observed in live trading over the past 30 days.

Variants:
  Baseline  atr_stop_mult=0.61  (current config)
  B-2.5     atr_stop_mult=0.77  (~2.5% stop buffer)
  B-3.0     atr_stop_mult=0.92  (~3.0% stop buffer)
  B-3.5     atr_stop_mult=1.08  (~3.5% stop buffer)

Option A (entry_delay_minutes=15) is EXPLICITLY DEFERRED:
  Requires intraday (sub-daily) bar resolution. Atlas backtest uses daily bars
  only. On daily bars, a 15-min entry delay has no measurable effect. This
  variant cannot be evaluated until Task #316 (5-min intraday backfill) is
  complete. See decision note for reference.

Same-bar stop proxy on daily bars:
  In live trading, a same-bar stop = entry at open, stop triggered same day.
  On daily bars this cannot be reproduced exactly (minimum hold_days == 1).
  Proxy used: hold_days == 1 AND exit_reason == "stop_hit".
  This captures "stop hit on first available day after entry" — the
  daily-bar equivalent of a same-bar stop (stop so tight it was breached
  immediately). All same-bar rate numbers in this report are PROXY values.

Decision criteria:
  1. Same-bar proxy rate reduced by ≥50% (from ~27.3% → ≤13.7%)
  2. Sharpe preserved within 80% of baseline
  3. MaxDD not bloated by >20% of baseline
  If baseline Sharpe ≤ 0: pick highest Sharpe variant that meets same-bar
  reduction + MaxDD criteria. If none qualify: recommend MONITOR, no ship.

Usage:
    python3 scripts/backtest_same_bar_mitigation_comparison.py
"""

from __future__ import annotations

import copy
import json
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

from utils.logging_config import setup_logging

setup_logging("sb_mitigation_comparison", level=logging.WARNING)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
STRATEGY = "momentum_breakout"
BASELINE_SAME_BAR_RATE = 0.273  # observed 30-day live rate
SAME_BAR_TARGET_MAX = 0.137     # 50% reduction threshold
SHARPE_RETENTION_MIN = 0.80     # must retain ≥80% of baseline Sharpe
MAXDD_BLOAT_MAX = 1.20          # must not exceed 1.20× baseline MaxDD

VARIANTS: list[dict] = [
    {
        "name": "Baseline",
        "label": "atr_stop_mult=0.61 (~1.98% buffer) — current config",
        "atr_stop_mult": 0.61,
    },
    {
        "name": "B-2.5",
        "label": "atr_stop_mult=0.77 (~2.5% buffer)",
        "atr_stop_mult": 0.77,
    },
    {
        "name": "B-3.0",
        "label": "atr_stop_mult=0.92 (~3.0% buffer)",
        "atr_stop_mult": 0.92,
    },
    {
        "name": "B-3.5",
        "label": "atr_stop_mult=1.08 (~3.5% buffer)",
        "atr_stop_mult": 1.08,
    },
]

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _compute_same_bar_proxy_rate(trades: list[dict]) -> tuple[float, int, int]:
    """Compute proxy same-bar stop rate from trade list.

    Proxy: hold_days == 1 AND exit_reason == "stop_hit" for momentum_breakout
    trades only.

    Returns (rate, n_same_bar_proxy, total_mb_trades).
    """
    mb_trades = [
        t for t in trades
        if t.get("strategy") == STRATEGY
    ]
    total = len(mb_trades)
    if total == 0:
        return 0.0, 0, 0
    n_same = sum(
        1 for t in mb_trades
        if t.get("hold_days", 99) == 1
        and t.get("exit_reason") == "stop_hit"
    )
    return round(n_same / total, 4), n_same, total


def _run_variant(
    variant: dict,
    base_cfg: dict,
    data: dict,
) -> dict:
    """Run a single solo backtest for momentum_breakout with patched atr_stop_mult.

    Returns a result dict with status, metrics, and same_bar_proxy info.
    """
    from backtest.engine import BacktestEngine
    from backtest.metrics import calc_cagr_full_period
    from scripts.strategy_evaluator import (
        make_config_with_strategy,
        get_strategy_class,
    )

    result: dict = {
        "variant": variant["name"],
        "label": variant["label"],
        "atr_stop_mult": variant["atr_stop_mult"],
        "started_at": datetime.now(timezone.utc).isoformat(),
    }
    t0 = time.time()
    try:
        # Build solo config: only momentum_breakout enabled
        solo_cfg = make_config_with_strategy(base_cfg, STRATEGY, None, solo=True)

        # Apply atr_stop_mult patch
        solo_cfg["strategies"][STRATEGY]["atr_stop_mult"] = variant["atr_stop_mult"]
        solo_cfg["strategies"][STRATEGY]["enabled"] = True

        # Run backtest directly so we get both metrics AND trades
        strategies = [get_strategy_class(STRATEGY)(solo_cfg)]
        engine = BacktestEngine(solo_cfg)
        bt_result = engine.run_walkforward(data, strategies)

        raw_m = bt_result.metrics
        trades: list[dict] = bt_result.trades or []

        # Build normalised metric dict (mirrors run_backtest output)
        cagr = raw_m.get("cagr", 0) or 0
        cagr_pct = cagr * 100 if abs(cagr) < 2 else cagr

        # Full-period CAGR
        cagr_full_pct = cagr_pct
        if bt_result.equity_curve is not None and len(bt_result.equity_curve) > 0:
            all_dates: set = set()
            for df in data.values():
                if hasattr(df, "index") and len(df) > 0:
                    all_dates.add(df.index[0])
                    all_dates.add(df.index[-1])
            if all_dates:
                cagr_full_pct = round(
                    calc_cagr_full_period(
                        bt_result.equity_curve, min(all_dates), max(all_dates)
                    ) * 100,
                    4,
                )

        metrics: dict[str, Any] = {
            "total_trades": raw_m.get("total_trades", 0) or 0,
            "sharpe": round(raw_m.get("sharpe", 0) or 0, 4),
            "cagr_full_period_pct": cagr_full_pct,
            "max_drawdown_pct": round((raw_m.get("max_drawdown", 0) or 0) * 100, 4),
            "win_rate_pct": round((raw_m.get("win_rate", 0) or 0) * 100, 2),
            "profit_factor": round(raw_m.get("profit_factor", 0) or 0, 4),
            "total_pnl": round(raw_m.get("total_pnl", 0) or 0, 2),
        }

        # Same-bar proxy rate
        sbr, n_same, total_mb = _compute_same_bar_proxy_rate(trades)
        metrics["same_bar_proxy_rate"] = sbr
        metrics["same_bar_proxy_n"] = n_same
        metrics["same_bar_proxy_total_mb_trades"] = total_mb

        elapsed = round(time.time() - t0, 1)
        result.update({
            "status": "ok",
            "metrics": metrics,
            "elapsed_s": elapsed,
        })
        logger.info(
            "OK  %-8s  sharpe=%.4f  maxdd=%.2f%%  sb_proxy=%.1f%%  trades=%d  elapsed=%ss",
            variant["name"],
            metrics["sharpe"],
            metrics["max_drawdown_pct"],
            sbr * 100,
            metrics["total_trades"],
            elapsed,
        )
    except Exception as exc:
        elapsed = round(time.time() - t0, 1)
        result.update({
            "status": "error",
            "error": str(exc),
            "elapsed_s": elapsed,
        })
        logger.error("FAIL  %s  error=%s", variant["name"], exc, exc_info=True)

    result["finished_at"] = datetime.now(timezone.utc).isoformat()
    return result


# ---------------------------------------------------------------------------
# Decision logic
# ---------------------------------------------------------------------------

def _meets_criteria(
    variant_r: dict,
    baseline_r: dict,
) -> tuple[bool, list[str]]:
    """Check if variant meets all 3 decision criteria. Returns (pass, reasons)."""
    reasons: list[str] = []
    if variant_r["status"] != "ok":
        return False, ["variant errored"]
    if baseline_r["status"] != "ok":
        return False, ["baseline errored"]

    vm = variant_r["metrics"]
    bm = baseline_r["metrics"]

    baseline_sharpe = bm.get("sharpe", 0) or 0
    baseline_maxdd = bm.get("max_drawdown_pct", 0) or 0
    baseline_sbr = bm.get("same_bar_proxy_rate", BASELINE_SAME_BAR_RATE)

    # Criterion 1: same-bar proxy rate reduced by ≥50%
    target_sbr = baseline_sbr * 0.50  # ≤50% of baseline
    v_sbr = vm.get("same_bar_proxy_rate", 1.0)
    if v_sbr <= target_sbr:
        reasons.append(f"✓ SB proxy rate {v_sbr:.1%} ≤ target {target_sbr:.1%}")
    else:
        reasons.append(f"✗ SB proxy rate {v_sbr:.1%} > target {target_sbr:.1%} (need ≥50% reduction)")
        return False, reasons

    # Criterion 2: Sharpe within 80% of baseline (or baseline ≤ 0)
    v_sharpe = vm.get("sharpe", 0) or 0
    if baseline_sharpe <= 0:
        # Negative baseline: pick highest Sharpe instead
        reasons.append(f"~ Baseline Sharpe ≤ 0 ({baseline_sharpe:.4f}): using highest-Sharpe rule")
    else:
        min_sharpe = SHARPE_RETENTION_MIN * baseline_sharpe
        if v_sharpe >= min_sharpe:
            retained = (v_sharpe / baseline_sharpe) * 100
            reasons.append(
                f"✓ Sharpe {v_sharpe:.4f} ≥ {min_sharpe:.4f} "
                f"(retains {retained:.1f}% of baseline {baseline_sharpe:.4f})"
            )
        else:
            retained = (v_sharpe / baseline_sharpe) * 100 if baseline_sharpe else 0
            reasons.append(
                f"✗ Sharpe {v_sharpe:.4f} < {min_sharpe:.4f} "
                f"(retains only {retained:.1f}% of baseline {baseline_sharpe:.4f})"
            )
            return False, reasons

    # Criterion 3: MaxDD not bloated by >20%
    v_maxdd = vm.get("max_drawdown_pct", 0) or 0
    max_dd_allowed = abs(baseline_maxdd) * MAXDD_BLOAT_MAX
    if abs(v_maxdd) <= max_dd_allowed:
        reasons.append(
            f"✓ MaxDD {v_maxdd:.2f}% ≤ allowed {max_dd_allowed:.2f}% "
            f"(1.20× baseline {baseline_maxdd:.2f}%)"
        )
    else:
        reasons.append(
            f"✗ MaxDD {v_maxdd:.2f}% > allowed {max_dd_allowed:.2f}% "
            f"(1.20× baseline {baseline_maxdd:.2f}%)"
        )
        return False, reasons

    return True, reasons


def _apply_decision_rule(results: list[dict]) -> dict:
    """Apply decision rule and return decision dict."""
    baseline_r = next(r for r in results if r["variant"] == "Baseline")
    baseline_m = baseline_r.get("metrics", {})
    baseline_sharpe = baseline_m.get("sharpe", None) if baseline_r["status"] == "ok" else None
    baseline_sbr = baseline_m.get("same_bar_proxy_rate", BASELINE_SAME_BAR_RATE)

    if baseline_r["status"] != "ok":
        return {
            "chosen": None,
            "ship": False,
            "baseline_sharpe": None,
            "baseline_same_bar_proxy_rate": BASELINE_SAME_BAR_RATE,
            "rule_applied": "error",
            "rationale": "Baseline backtest errored — cannot apply decision rule.",
            "criteria_detail": {},
        }

    candidates = [r for r in results if r["variant"] != "Baseline" and r["status"] == "ok"]
    criteria_detail: dict[str, Any] = {}

    if baseline_sharpe is not None and baseline_sharpe <= 0:
        # Negative baseline: among variants meeting SB + MaxDD, pick highest Sharpe
        rule = "baseline_sharpe_le_0__pick_highest_sharpe"
        eligible: list[dict] = []
        for r in candidates:
            vm = r["metrics"]
            v_sbr = vm.get("same_bar_proxy_rate", 1.0)
            target_sbr = baseline_sbr * 0.50
            v_maxdd = vm.get("max_drawdown_pct", 0) or 0
            max_dd_allowed = abs(baseline_m.get("max_drawdown_pct", 0)) * MAXDD_BLOAT_MAX
            ok, reasons = _meets_criteria(r, baseline_r)
            criteria_detail[r["variant"]] = reasons
            # For negative baseline: ignore Sharpe retention; check SB + MaxDD only
            if v_sbr <= target_sbr and abs(v_maxdd) <= max_dd_allowed:
                eligible.append(r)

        if eligible:
            best = max(eligible, key=lambda r: r["metrics"].get("sharpe", -999))
            chosen_name = best["variant"]
            bsh = best["metrics"].get("sharpe", 0)
            sbr_val = best["metrics"].get("same_bar_proxy_rate", 0)
            rationale = (
                f"Baseline Sharpe={baseline_sharpe:.4f} (≤0). "
                f"Negative baseline rule: pick highest-Sharpe variant meeting "
                f"SB-reduction + MaxDD criteria. {chosen_name} wins with "
                f"Sharpe={bsh:.4f}, SB proxy rate={sbr_val:.1%}."
            )
            ship = True
        else:
            chosen_name = None
            rationale = (
                f"Baseline Sharpe={baseline_sharpe:.4f} (≤0). "
                f"No variant meets both SB-reduction (≥50%) and MaxDD "
                f"(≤1.20× baseline) criteria. Recommend MONITOR. No config change."
            )
            ship = False
    else:
        # Positive baseline: apply all 3 criteria, pick first (lowest multiplier) that passes
        rule = "positive_baseline__all_three_criteria"
        passing: list[dict] = []
        for r in candidates:
            ok, reasons = _meets_criteria(r, baseline_r)
            criteria_detail[r["variant"]] = reasons
            if ok:
                passing.append(r)

        if passing:
            # Prefer smallest multiplier (least risk change) among passing
            best = min(passing, key=lambda r: r["atr_stop_mult"])
            chosen_name = best["variant"]
            bsh = best["metrics"].get("sharpe", 0)
            bsbr = best["metrics"].get("same_bar_proxy_rate", 0)
            bmult = best["atr_stop_mult"]
            baseline_sbr_val = baseline_sbr
            rationale = (
                f"Baseline Sharpe={baseline_sharpe:.4f} (>0). "
                f"{chosen_name} (atr_stop_mult={bmult}) meets all 3 criteria: "
                f"SB proxy {bsbr:.1%} ≤ {baseline_sbr_val*0.5:.1%}, "
                f"Sharpe {bsh:.4f} ≥ {SHARPE_RETENTION_MIN*baseline_sharpe:.4f}, "
                f"MaxDD within 1.20× baseline. Smallest qualifying multiplier selected."
            )
            ship = True
        else:
            chosen_name = None
            rationale = (
                f"Baseline Sharpe={baseline_sharpe:.4f} (>0). "
                f"No variant meets all 3 decision criteria simultaneously. "
                f"Recommend MONITOR. No config change."
            )
            ship = False

    return {
        "chosen": chosen_name,
        "ship": ship,
        "baseline_sharpe": baseline_sharpe,
        "baseline_same_bar_proxy_rate": float(baseline_sbr),
        "rule_applied": rule,
        "rationale": rationale,
        "criteria_detail": criteria_detail,
    }


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def _print_markdown_table(results: list[dict], decision: dict) -> None:
    """Print a markdown comparison table + decision block to stdout."""
    print()
    header = (
        "| Variant  | atr_mult | Sharpe | CAGR%   | MaxDD%  | WinRate | Trades | "
        "SB-proxy%† |"
    )
    sep = (
        "|----------|----------|-------:|--------:|--------:|--------:|-------:|"
        "----------:|"
    )
    print(header)
    print(sep)

    for r in results:
        name = r["variant"]
        mult = r.get("atr_stop_mult", "?")
        if r["status"] != "ok":
            print(
                f"| {name:<8} | {mult:<8} | ERROR  |  ERROR  |  ERROR  |     --  |    -- |"
                f"        -- |"
            )
            continue
        m = r["metrics"]
        sharpe = m.get("sharpe", 0) or 0
        cagr = m.get("cagr_full_period_pct", 0) or 0
        maxdd = m.get("max_drawdown_pct", 0) or 0
        trades = m.get("total_trades", 0) or 0
        wr = m.get("win_rate_pct", 0) or 0
        sbr = m.get("same_bar_proxy_rate", 0) * 100
        print(
            f"| {name:<8} | {mult:<8} | {sharpe:6.3f} | {cagr:7.2f}% | {maxdd:7.2f}% | "
            f"{wr:5.1f}%   | {trades:6d} | {sbr:9.1f}% |"
        )

    print()
    bs = decision.get("baseline_sharpe", 0) or 0
    bs_sbr = decision.get("baseline_same_bar_proxy_rate", BASELINE_SAME_BAR_RATE)
    rule = decision.get("rule_applied", "?")
    print(
        f"† SB-proxy% = hold_days==1 + exit_reason=='stop_hit' for momentum_breakout "
        f"trades. Daily-bar proxy for same-bar stop; actual live rate ≈ {BASELINE_SAME_BAR_RATE*100:.1f}%."
    )
    print()
    print(f"**Decision rule**: baseline Sharpe={bs:.4f}, rule=`{rule}`")
    print(f"**Baseline SB proxy rate**: {bs_sbr*100:.1f}%  |  Target ≤ {bs_sbr*50:.1f}%")
    print(f"**CHOSEN**: {decision['chosen'] or 'NONE — no variant qualifies'}")
    print(f"**Ship?**: {'YES' if decision['ship'] else 'NO'}")
    print(f"**Rationale**: {decision['rationale']}")
    print()

    # Per-variant criteria detail
    cd = decision.get("criteria_detail", {})
    if cd:
        print("**Criteria detail per variant**:")
        for vname, reasons in cd.items():
            print(f"  {vname}:")
            for r in reasons:
                print(f"    {r}")
    print()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    from utils.config import get_active_config
    from scripts.strategy_evaluator import load_market_data

    print("=" * 72)
    print("momentum_breakout same-bar stop mitigation — backtest comparison")
    print(f"Run date: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 72)

    print("\nNOTE: Option A (entry_delay_minutes=15) is DEFERRED.")
    print("      Requires intraday bars (Task #316). Skipped in this run.")
    print()

    print("[1/3] Loading sp500 config and market data...")
    cfg = get_active_config("sp500")
    data = load_market_data("sp500")
    n_tickers = len(data)
    print(f"      Loaded {n_tickers} tickers.")

    print()
    print("[2/3] Running 4 backtest variants (sequential, ~60-120s each)...")
    print(
        "      Proxy metric: hold_days==1 + exit_reason=='stop_hit' "
        "(daily-bar proxy for same-bar stop)"
    )
    print()
    results: list[dict] = []
    total_elapsed = 0.0
    for v in VARIANTS:
        print(f"  -> {v['name']}: {v['label']}")
        r = _run_variant(v, cfg, data)
        results.append(r)
        total_elapsed += r.get("elapsed_s", 0)
        if r["status"] == "ok":
            m = r["metrics"]
            print(
                f"     Sharpe={m.get('sharpe', 0):.4f}  "
                f"MaxDD={m.get('max_drawdown_pct', 0):.2f}%  "
                f"SB-proxy={m.get('same_bar_proxy_rate', 0)*100:.1f}%  "
                f"({m.get('same_bar_proxy_n', 0)}/{m.get('same_bar_proxy_total_mb_trades', 0)} trades)  "
                f"elapsed={r['elapsed_s']}s"
            )
        else:
            print(f"     ERROR: {r.get('error', 'unknown')}")
    print(f"\n  Total elapsed: {total_elapsed:.0f}s")

    print()
    print("[3/3] Applying decision rule...")
    decision = _apply_decision_rule(results)

    _print_markdown_table(results, decision)

    # Persist results
    out: dict[str, Any] = {
        "run_at": datetime.now(timezone.utc).isoformat(),
        "n_tickers": n_tickers,
        "strategy": STRATEGY,
        "option_a_status": "DEFERRED — requires Task #316 (5-min intraday backfill)",
        "same_bar_proxy_note": (
            "hold_days==1 AND exit_reason=='stop_hit' for momentum_breakout trades. "
            "On daily bars, minimum hold is 1 day (never 0). This is the closest "
            "proxy for a same-bar stop in daily-bar backtesting."
        ),
        "variants": results,
        "decision": decision,
    }
    data_dir = PROJECT / "data"
    data_dir.mkdir(exist_ok=True)
    ts = datetime.now().strftime("%Y%m%dT%H%M%S")
    out_path = data_dir / f"same_bar_mitigation_comparison_{ts}.json"
    out_path.write_text(json.dumps(out, indent=2, default=str))
    print(f"Results persisted to: {out_path}")

    print(f"\n{'='*72}")
    print(f"DECISION  : {decision['chosen'] or 'DEFER — no variant meets criteria'}")
    print(f"SHIP?     : {'YES' if decision['ship'] else 'NO'}")
    print(f"RATIONALE : {decision['rationale']}")
    print(f"{'='*72}")
    print()


if __name__ == "__main__":
    main()
