#!/usr/bin/env python3
"""Conditional Drawdown Hedge Experiment.

Tests whether a conditional SH (inverse S&P 500) hedge reduces portfolio
max drawdown by ≥5pp at ≤2% annual return drag.

Phases:
  1. Hedge signal in isolation — measure raw trigger behavior and SH P&L
  2. Combined portfolio overlay — baseline vs hedged at multiple hedge ratios
  3. Sensitivity analysis — 6 trigger variants
  4. Document results to brain

Usage:
    python3 research/experiments/hedge_experiment.py [--market sp500]
"""

import copy
import json
import sys
import time
import argparse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

PROJECT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT))

from backtest.engine import BacktestEngine
from backtest.metrics import calc_cagr, calc_max_drawdown, calc_sharpe, calc_sortino
from utils.config import get_active_config
from universe.builder import get_universe_tickers

MARKET = "sp500"
OUTPUT_DIR = PROJECT / "research" / "reports"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
RESULTS_DIR = PROJECT / "research" / "results"
RESULTS_DIR.mkdir(parents=True, exist_ok=True)

# ─── Data Loading ────────────────────────────────────────────────────────────

def download_yf(ticker: str, start: str = "2019-01-01") -> pd.DataFrame:
    """Download OHLCV from yfinance, return clean DataFrame."""
    import yfinance as yf
    raw = yf.download(ticker, start=start, progress=False)
    if raw.empty:
        raise ValueError(f"No data for {ticker}")
    # Flatten MultiIndex columns from yfinance
    if isinstance(raw.columns, pd.MultiIndex):
        raw.columns = [c[0].lower() for c in raw.columns]
    else:
        raw.columns = [c.lower() for c in raw.columns]
    raw.index.name = "date"
    return raw


def load_spy() -> pd.DataFrame:
    """Load SPY from cache or download."""
    cache = PROJECT / "data" / "cache" / MARKET / "SPY.parquet"
    if cache.exists():
        df = pd.read_parquet(cache)
        print(f"  SPY from cache: {df.index[0].date()} to {df.index[-1].date()} ({len(df)} rows)")
        return df
    df = download_yf("SPY")
    print(f"  SPY downloaded: {df.index[0].date()} to {df.index[-1].date()} ({len(df)} rows)")
    return df


def load_sh() -> pd.DataFrame:
    """Download SH (ProShares Short S&P500)."""
    cache = PROJECT / "data" / "cache" / MARKET / "SH.parquet"
    if cache.exists():
        df = pd.read_parquet(cache)
        if len(df) > 100:
            print(f"  SH from cache: {df.index[0].date()} to {df.index[-1].date()} ({len(df)} rows)")
            return df
    df = download_yf("SH")
    # Save to cache
    df.to_parquet(cache)
    print(f"  SH downloaded: {df.index[0].date()} to {df.index[-1].date()} ({len(df)} rows)")
    return df


def load_vix() -> pd.DataFrame:
    """Download VIX."""
    df = download_yf("^VIX")
    print(f"  VIX downloaded: {df.index[0].date()} to {df.index[-1].date()} ({len(df)} rows)")
    return df


def load_universe_data(config: dict) -> Dict[str, pd.DataFrame]:
    """Load OHLCV for all universe tickers."""
    tickers = get_universe_tickers(MARKET)
    base_cache = PROJECT / config["data"]["cache_dir"]
    market_cache = base_cache / MARKET
    data = {}
    for ticker in tickers:
        fname = ticker.replace(".", "_") + ".parquet"
        path = market_cache / fname
        if not path.exists():
            path = base_cache / fname
        if path.exists():
            data[ticker] = pd.read_parquet(path)
    return data


# ─── Hedge Signal ────────────────────────────────────────────────────────────

def apply_hysteresis(signal: pd.Series, on_days: int = 3, off_days: int = 5) -> pd.Series:
    """Apply sticky on/off logic to a boolean signal.

    Requires `on_days` consecutive True values to activate.
    Requires `off_days` consecutive False values to deactivate.
    """
    active = False
    consecutive_on = 0
    consecutive_off = 0
    result = pd.Series(False, index=signal.index)

    for i, val in enumerate(signal):
        if val:
            consecutive_on += 1
            consecutive_off = 0
        else:
            consecutive_off += 1
            consecutive_on = 0

        if not active and consecutive_on >= on_days:
            active = True
        elif active and consecutive_off >= off_days:
            active = False

        result.iloc[i] = active

    return result


@dataclass
class TriggerConfig:
    """Parameters for the hedge trigger signal."""
    name: str
    use_trend: bool = True          # SPY < SMA-200
    use_momentum: bool = True       # 20d return < threshold
    momentum_threshold: float = -0.05
    use_vix: bool = True            # VIX > threshold
    vix_threshold: float = 25.0
    on_days: int = 3
    off_days: int = 5


def compute_hedge_signal(
    spy: pd.DataFrame,
    vix: pd.DataFrame,
    trigger: TriggerConfig,
) -> pd.Series:
    """Compute the hedge activation signal for given trigger parameters."""
    close = spy["close"]

    # Start with all-True, then AND in each condition
    signal = pd.Series(True, index=close.index)

    # Condition 1: SPY below 200-day SMA
    if trigger.use_trend:
        sma200 = close.rolling(200, min_periods=200).mean()
        signal = signal & (close < sma200)

    # Condition 2: 20-day negative momentum
    if trigger.use_momentum:
        ret_20d = close.pct_change(20)
        signal = signal & (ret_20d < trigger.momentum_threshold)

    # Condition 3: VIX above threshold
    if trigger.use_vix:
        vix_close = vix["close"].reindex(close.index, method="ffill")
        signal = signal & (vix_close > trigger.vix_threshold)

    # Apply hysteresis
    signal = apply_hysteresis(signal, trigger.on_days, trigger.off_days)

    return signal


# ─── Phase 1: Hedge in Isolation ─────────────────────────────────────────────

def phase1_hedge_isolation(
    spy: pd.DataFrame,
    sh: pd.DataFrame,
    vix: pd.DataFrame,
    trigger: TriggerConfig,
) -> dict:
    """Measure hedge behavior in isolation."""
    print(f"\n{'='*70}")
    print(f"  PHASE 1: Hedge in Isolation — {trigger.name}")
    print(f"{'='*70}")

    hedge_active = compute_hedge_signal(spy, vix, trigger)

    # Align SH to SPY dates
    spy_close = spy["close"]
    sh_close = sh["close"].reindex(spy_close.index, method="ffill")
    sh_returns = sh_close.pct_change().fillna(0)

    # Hedge daily returns: SH return when active, 0 otherwise
    hedge_returns = sh_returns.copy()
    hedge_returns[~hedge_active] = 0.0

    # SPY daily returns for comparison
    spy_returns = spy_close.pct_change().fillna(0)

    # Cumulative
    hedge_cumulative = (1 + hedge_returns).cumprod()
    total_return = hedge_cumulative.iloc[-1] - 1
    total_days = len(hedge_active)
    active_days = int(hedge_active.sum())

    # Count activation episodes
    transitions = hedge_active.astype(int).diff().fillna(0)
    num_activations = int((transitions == 1).sum())

    print(f"\n  Trigger: {trigger.name}")
    print(f"  Active days: {active_days} / {total_days} ({100*active_days/total_days:.1f}%)")
    print(f"  Activations: {num_activations} episodes")
    print(f"  Hedge total return: {total_return*100:+.2f}%")

    # Per-year stats
    yearly = {}
    print(f"\n  {'Year':>6} {'Active':>8} {'Total':>8} {'Active%':>8} {'HedgeRet':>10} {'SPY Active':>12} {'SPY Inactive':>13}")
    print(f"  {'-'*73}")

    for year in sorted(spy_close.index.year.unique()):
        mask = spy_close.index.year == year
        yr_active = hedge_active[mask]
        yr_hedge_ret = hedge_returns[mask]
        yr_spy_ret = spy_returns[mask]

        yr_active_days = int(yr_active.sum())
        yr_total = len(yr_active)
        yr_hedge_pnl = (1 + yr_hedge_ret).prod() - 1

        # SPY return during active vs inactive periods
        yr_active_bool = yr_active.astype(bool)
        spy_when_active = yr_spy_ret[yr_active_bool].sum() if yr_active_days > 0 else 0
        spy_when_inactive = yr_spy_ret[~yr_active_bool].sum()

        yearly[str(year)] = {
            "active_days": yr_active_days,
            "total_days": yr_total,
            "hedge_return_pct": round(yr_hedge_pnl * 100, 2),
            "spy_return_active_pct": round(spy_when_active * 100, 2),
            "spy_return_inactive_pct": round(spy_when_inactive * 100, 2),
        }

        print(f"  {year:>6} {yr_active_days:>8} {yr_total:>8} "
              f"{100*yr_active_days/max(yr_total,1):>7.1f}% "
              f"{yr_hedge_pnl*100:>+9.2f}% "
              f"{spy_when_active*100:>+11.2f}% "
              f"{spy_when_inactive*100:>+12.2f}%")

    return {
        "trigger": trigger.name,
        "total_days": total_days,
        "active_days": active_days,
        "active_pct": round(100 * active_days / total_days, 1),
        "num_activations": num_activations,
        "hedge_total_return_pct": round(total_return * 100, 2),
        "yearly": yearly,
    }


# ─── Phase 2: Combined Portfolio ─────────────────────────────────────────────

def get_strategies(config):
    """Instantiate all enabled strategies."""
    from strategies.mean_reversion import MeanReversion
    from strategies.momentum_breakout import MomentumBreakout
    from strategies.trend_following import TrendFollowing
    from strategies.sector_rotation import SectorRotation
    from strategies.short_term_mr import ShortTermMR
    from strategies.opening_gap import OpeningGap
    from strategies.connors_rsi2 import ConnorsRSI2

    strats = []
    sc = config["strategies"]
    if sc.get("momentum_breakout", {}).get("enabled"):
        strats.append(MomentumBreakout(config))
    if sc.get("mean_reversion", {}).get("enabled"):
        strats.append(MeanReversion(config))
    if sc.get("trend_following", {}).get("enabled"):
        strats.append(TrendFollowing(config))
    if sc.get("sector_rotation", {}).get("enabled"):
        strats.append(SectorRotation(config))
    if sc.get("short_term_mr", {}).get("enabled"):
        strats.append(ShortTermMR(config))
    if sc.get("opening_gap", {}).get("enabled"):
        strats.append(OpeningGap(config))
    if sc.get("connors_rsi2", {}).get("enabled"):
        strats.append(ConnorsRSI2(config))
    return strats


def run_baseline_backtest(config, data) -> Tuple[pd.Series, dict]:
    """Run baseline 7-strategy portfolio backtest, return equity curve and metrics."""
    print(f"\n  Running baseline 7-strategy backtest...")
    t0 = time.time()
    strategies = get_strategies(config)
    print(f"  Strategies: {[s.name for s in strategies]}")

    engine = BacktestEngine(config, market_id=MARKET)
    result = engine.run_walkforward(data, strategies)

    elapsed = time.time() - t0
    print(f"  Baseline done in {elapsed:.0f}s — {result.metrics.get('total_trades', 0)} trades")
    return result.equity_curve, result.metrics


def overlay_hedge(
    baseline_equity: pd.Series,
    sh: pd.DataFrame,
    hedge_active: pd.Series,
    hedge_ratio: float,
) -> pd.Series:
    """Overlay hedge returns onto baseline equity curve (Approach A — post-hoc).

    When hedge is active, the portfolio allocates `hedge_ratio` of daily return
    to SH and `(1 - hedge_ratio)` to the baseline.
    """
    sh_close = sh["close"].reindex(baseline_equity.index, method="ffill")
    sh_returns = sh_close.pct_change().fillna(0)
    base_returns = baseline_equity.pct_change().fillna(0)

    # Align hedge_active to equity index
    ha = hedge_active.reindex(baseline_equity.index, method="ffill").fillna(False)

    combined_returns = base_returns.copy()
    active_mask = ha.astype(bool)

    # When active: blended return
    combined_returns[active_mask] = (
        (1 - hedge_ratio) * base_returns[active_mask]
        + hedge_ratio * sh_returns[active_mask]
    )

    combined_equity = (1 + combined_returns).cumprod() * baseline_equity.iloc[0]
    return combined_equity


def compute_metrics(equity: pd.Series) -> dict:
    """Compute standard metrics from an equity curve."""
    returns = equity.pct_change().dropna()
    return {
        "cagr_pct": round(calc_cagr(equity) * 100, 2),
        "max_dd_pct": round(calc_max_drawdown(equity) * 100, 2),
        "sharpe": round(calc_sharpe(returns), 3),
        "sortino": round(calc_sortino(returns), 3),
        "total_return_pct": round((equity.iloc[-1] / equity.iloc[0] - 1) * 100, 2),
    }


def compute_annual_drag(baseline_equity: pd.Series, hedged_equity: pd.Series) -> float:
    """Compute annualized return drag from hedging."""
    base_cagr = calc_cagr(baseline_equity)
    hedged_cagr = calc_cagr(hedged_equity)
    return round((base_cagr - hedged_cagr) * 100, 2)


def phase2_combined(
    baseline_equity: pd.Series,
    baseline_metrics: dict,
    sh: pd.DataFrame,
    vix: pd.DataFrame,
    spy: pd.DataFrame,
    trigger: TriggerConfig,
) -> dict:
    """Compare baseline vs hedged portfolio at multiple hedge ratios."""
    print(f"\n{'='*70}")
    print(f"  PHASE 2: Combined Portfolio — Hedge Ratio Sweep")
    print(f"{'='*70}")

    hedge_active = compute_hedge_signal(spy, vix, trigger)
    active_days = int(hedge_active.reindex(baseline_equity.index, method="ffill").fillna(False).sum())
    total_days = len(baseline_equity)
    print(f"\n  Hedge active on {active_days}/{total_days} portfolio days ({100*active_days/total_days:.1f}%)")

    base_m = compute_metrics(baseline_equity)
    hedge_ratios = [0.0, 0.10, 0.15, 0.20, 0.25, 0.30, 0.40, 0.50]
    results = []

    header = (f"  {'Ratio':>6} {'CAGR%':>8} {'MaxDD%':>8} {'Sharpe':>8} "
              f"{'Sortino':>8} {'Return%':>9} {'Drag%':>7}")
    print(f"\n{header}")
    print(f"  {'-'*56}")

    for ratio in hedge_ratios:
        if ratio == 0.0:
            m = base_m.copy()
            m["hedge_ratio"] = 0.0
            m["annual_drag_pct"] = 0.0
        else:
            hedged = overlay_hedge(baseline_equity, sh, hedge_active, ratio)
            m = compute_metrics(hedged)
            m["hedge_ratio"] = ratio
            m["annual_drag_pct"] = compute_annual_drag(baseline_equity, hedged)

        results.append(m)
        ratio_str = f"{ratio*100:.0f}%" if ratio > 0 else "BASE"
        print(f"  {ratio_str:>6} {m['cagr_pct']:>8.2f} {m['max_dd_pct']:>8.2f} "
              f"{m['sharpe']:>8.3f} {m['sortino']:>8.3f} "
              f"{m['total_return_pct']:>9.2f} {m['annual_drag_pct']:>+7.2f}")

    # Find sweet spot (best Sharpe)
    best = max(results, key=lambda r: r["sharpe"])
    base_dd = base_m["max_dd_pct"]
    best_dd = best["max_dd_pct"]
    dd_reduction = base_dd - best_dd

    print(f"\n  Sweet spot: {best['hedge_ratio']*100:.0f}% hedge ratio")
    print(f"    Sharpe: {base_m['sharpe']:.3f} → {best['sharpe']:.3f} (Δ{best['sharpe']-base_m['sharpe']:+.3f})")
    print(f"    MaxDD:  {base_dd:.2f}% → {best_dd:.2f}% (reduction: {dd_reduction:.2f}pp)")
    print(f"    Drag:   {best['annual_drag_pct']:+.2f}%/year")

    return {
        "trigger": trigger.name,
        "baseline": base_m,
        "hedge_results": results,
        "sweet_spot_ratio": best["hedge_ratio"],
        "dd_reduction_pp": round(dd_reduction, 2),
        "best_sharpe": best["sharpe"],
        "annual_drag_at_sweet_spot": best["annual_drag_pct"],
    }


# ─── Phase 3: Sensitivity Analysis ───────────────────────────────────────────

def get_trigger_variants() -> List[TriggerConfig]:
    """Return the 6 trigger variants for sensitivity testing."""
    return [
        TriggerConfig(
            name="base",
            use_trend=True, use_momentum=True, momentum_threshold=-0.05,
            use_vix=True, vix_threshold=25, on_days=3, off_days=5,
        ),
        TriggerConfig(
            name="loose",
            use_trend=True, use_momentum=True, momentum_threshold=-0.03,
            use_vix=True, vix_threshold=20, on_days=2, off_days=3,
        ),
        TriggerConfig(
            name="tight",
            use_trend=True, use_momentum=True, momentum_threshold=-0.08,
            use_vix=True, vix_threshold=30, on_days=5, off_days=7,
        ),
        TriggerConfig(
            name="no_momentum",
            use_trend=True, use_momentum=False,
            use_vix=True, vix_threshold=25, on_days=3, off_days=5,
        ),
        TriggerConfig(
            name="no_vix",
            use_trend=True, use_momentum=True, momentum_threshold=-0.05,
            use_vix=False, on_days=3, off_days=5,
        ),
        TriggerConfig(
            name="trend_only",
            use_trend=True, use_momentum=False,
            use_vix=False, on_days=3, off_days=5,
        ),
    ]


def phase3_sensitivity(
    baseline_equity: pd.Series,
    sh: pd.DataFrame,
    spy: pd.DataFrame,
    vix: pd.DataFrame,
    hedge_ratio: float,
) -> dict:
    """Test multiple trigger variants at the chosen hedge ratio."""
    print(f"\n{'='*70}")
    print(f"  PHASE 3: Sensitivity Analysis — {hedge_ratio*100:.0f}% hedge ratio")
    print(f"{'='*70}")

    variants = get_trigger_variants()
    base_m = compute_metrics(baseline_equity)
    results = []

    header = (f"  {'Variant':<15} {'Active%':>8} {'Episodes':>9} "
              f"{'CAGR%':>7} {'MaxDD%':>7} {'Sharpe':>7} {'DDΔpp':>6} {'Drag%':>7}")
    print(f"\n{header}")
    print(f"  {'-'*68}")

    for trigger in variants:
        hedge_active = compute_hedge_signal(spy, vix, trigger)
        ha_aligned = hedge_active.reindex(baseline_equity.index, method="ffill").fillna(False)
        active_pct = round(100 * ha_aligned.sum() / len(ha_aligned), 1)
        transitions = ha_aligned.astype(int).diff().fillna(0)
        episodes = int((transitions == 1).sum())

        hedged = overlay_hedge(baseline_equity, sh, hedge_active, hedge_ratio)
        m = compute_metrics(hedged)
        dd_reduction = base_m["max_dd_pct"] - m["max_dd_pct"]
        drag = compute_annual_drag(baseline_equity, hedged)

        row = {
            "variant": trigger.name,
            "active_pct": active_pct,
            "episodes": episodes,
            "cagr_pct": m["cagr_pct"],
            "max_dd_pct": m["max_dd_pct"],
            "sharpe": m["sharpe"],
            "dd_reduction_pp": round(dd_reduction, 2),
            "annual_drag_pct": drag,
        }
        results.append(row)

        print(f"  {trigger.name:<15} {active_pct:>7.1f}% {episodes:>9} "
              f"{m['cagr_pct']:>7.2f} {m['max_dd_pct']:>7.2f} {m['sharpe']:>7.3f} "
              f"{dd_reduction:>+5.2f} {drag:>+7.2f}")

    # Unhedged baseline row
    print(f"  {'UNHEDGED':<15} {'0.0':>7}% {'0':>9} "
          f"{base_m['cagr_pct']:>7.2f} {base_m['max_dd_pct']:>7.2f} {base_m['sharpe']:>7.3f} "
          f"{'0.00':>6} {'0.00':>7}")

    # How many variants improve Sharpe?
    improved = sum(1 for r in results if r["sharpe"] > base_m["sharpe"])
    print(f"\n  {improved}/{len(results)} variants improve Sharpe vs unhedged baseline")

    return {
        "hedge_ratio": hedge_ratio,
        "baseline": base_m,
        "variants": results,
        "variants_improving_sharpe": improved,
    }


# ─── Phase 4: Document Results ───────────────────────────────────────────────

def write_brain_decision(p1, p2, p3, output_dir: Path):
    """Write brain decision document."""
    base_dd = p2["baseline"]["max_dd_pct"]
    sweet_ratio = p2["sweet_spot_ratio"]
    dd_red = p2["dd_reduction_pp"]
    drag = p2["annual_drag_at_sweet_spot"]
    best_sharpe = p2["best_sharpe"]
    base_sharpe = p2["baseline"]["sharpe"]
    improving = p3["variants_improving_sharpe"]
    total_variants = len(p3["variants"])

    # Decision logic
    pass_dd = dd_red >= 5.0
    pass_drag = abs(drag) <= 2.0
    pass_sharpe = best_sharpe >= base_sharpe
    pass_robust = improving >= 3
    promoted = pass_dd and pass_drag and pass_sharpe and pass_robust

    if promoted:
        status = "PROMOTED"
        decision = (f"ACCEPT — hedge reduces MaxDD by {dd_red:.1f}pp at {abs(drag):.2f}%/year drag. "
                    f"Sharpe {'improves' if best_sharpe > base_sharpe else 'maintained'}. "
                    f"Robust across {improving}/{total_variants} variants.")
    elif dd_red >= 3.0:
        status = "NEEDS_MORE_DATA"
        decision = (f"MARGINAL — hedge reduces MaxDD by {dd_red:.1f}pp but "
                    f"{'drag too high' if abs(drag) > 2.0 else ''}"
                    f"{'Sharpe degrades' if best_sharpe < base_sharpe else ''}"
                    f"{'not robust' if improving < 3 else ''}. "
                    f"Consider regime-gated deployment or forward test.")
    else:
        status = "REJECTED"
        decision = (f"REJECT — MaxDD reduction only {dd_red:.1f}pp (need ≥5pp). "
                    f"Annual drag {drag:+.2f}%. Cost exceeds benefit.")

    timestamp = time.strftime("%Y-%m-%d")

    # Build yearly table for Phase 1
    yearly_rows = ""
    for year, d in sorted(p1["yearly"].items()):
        yearly_rows += (f"| {year} | {d['active_days']}/{d['total_days']} "
                        f"({100*d['active_days']/max(d['total_days'],1):.0f}%) "
                        f"| {d['hedge_return_pct']:+.2f}% "
                        f"| {d['spy_return_active_pct']:+.2f}% "
                        f"| {d['spy_return_inactive_pct']:+.2f}% |\n")

    # Build ratio table for Phase 2
    ratio_rows = ""
    for r in p2["hedge_results"]:
        hr = f"{r['hedge_ratio']*100:.0f}%" if r['hedge_ratio'] > 0 else "BASE"
        ratio_rows += (f"| {hr} | {r['max_dd_pct']:.2f}% | {r['cagr_pct']:.2f}% "
                       f"| {r['sharpe']:.3f} | {r.get('annual_drag_pct', 0):+.2f}% |\n")

    # Build sensitivity table for Phase 3
    sens_rows = ""
    for r in p3["variants"]:
        sens_rows += (f"| {r['variant']} | {r['active_pct']:.1f}% | {r['episodes']} "
                      f"| {r['max_dd_pct']:.2f}% | {r['sharpe']:.3f} "
                      f"| {r['dd_reduction_pp']:+.2f}pp | {r['annual_drag_pct']:+.2f}% |\n")

    doc = f"""# Decision: Conditional Drawdown Hedge

**Date:** {timestamp}
**Status:** {status}
**Config:** SP500 v3.0, 7 strategies, $5K equity

## Hypothesis

Adding a conditional SH (inverse S&P 500) hedge that activates during confirmed
downtrends (SPY < SMA200 AND VIX > 25 AND 20d return < -5%) reduces max drawdown
by ≥5 percentage points at a cost of ≤2% annual return drag.

## Results

### Phase 1: Hedge Signal in Isolation

Trigger: SPY < SMA-200 AND 20d return < -5% AND VIX > 25 (3-day on / 5-day off hysteresis)

| Year | Active Days | Hedge Return | SPY When Active | SPY When Inactive |
|------|-------------|-------------|-----------------|-------------------|
{yearly_rows}
Total active: {p1['active_days']}/{p1['total_days']} days ({p1['active_pct']:.1f}%), {p1['num_activations']} episodes

### Phase 2: Portfolio Overlay at Multiple Hedge Ratios

Sweet spot: **{sweet_ratio*100:.0f}% hedge ratio**

| Hedge Ratio | Max DD | CAGR | Sharpe | Annual Drag |
|-------------|--------|------|--------|-------------|
{ratio_rows}
MaxDD reduction at sweet spot: **{dd_red:.2f}pp**
Sharpe: {base_sharpe:.3f} → {best_sharpe:.3f}

### Phase 3: Sensitivity Analysis (at {p3['hedge_ratio']*100:.0f}% hedge ratio)

| Variant | Active% | Episodes | Max DD | Sharpe | DD Reduction | Drag |
|---------|---------|----------|--------|--------|-------------|------|
{sens_rows}
{improving}/{total_variants} variants improve Sharpe vs unhedged baseline.

## Decision

{decision}

### Criteria Evaluation

| Criterion | Required | Actual | Pass? |
|-----------|----------|--------|-------|
| MaxDD reduction | ≥ 5pp | {dd_red:.2f}pp | {'✅' if pass_dd else '❌'} |
| Annual drag | ≤ 2% | {abs(drag):.2f}% | {'✅' if pass_drag else '❌'} |
| Sharpe improvement | ≥ baseline | {best_sharpe:.3f} vs {base_sharpe:.3f} | {'✅' if pass_sharpe else '❌'} |
| Robustness | ≥ 3/6 variants | {improving}/{total_variants} | {'✅' if pass_robust else '❌'} |

## Implementation Notes

{'If promoted: implement as a portfolio-level risk overlay in the engine, not as a strategy. '
 'Requires: (1) daily SPY/VIX data in the pipeline, (2) hedge trigger computation in plan generation, '
 '(3) SH order execution through Alpaca. Recommend 6-month paper trade before live deployment.'
 if promoted else
 'No implementation needed. Close this research line. '
 'Individual stock shorting and inverse-ETF hedging both fail on SP500 — the market has too strong '
 'a positive drift for short-side strategies to overcome in a $5K portfolio.'}

## Risk Notes

- SH has daily rebalancing drag (~0.5-1.0% annually vs perfect -1x)
- All testing is in-sample — forward validation recommended if ever revisited
- Transaction costs negligible (Alpaca $0 commission) but slippage on SH entry/exit adds up
- At $5K portfolio, a 30% hedge allocation = $1,500 in SH — adequate liquidity
"""

    brain_dir = PROJECT / "research" / "brain" / "decisions"
    brain_dir.mkdir(parents=True, exist_ok=True)
    with open(brain_dir / "conditional_hedge.md", "w") as f:
        f.write(doc)
    print(f"\n  Brain decision: research/brain/decisions/conditional_hedge.md")

    # Also write experiment record
    exp_dir = PROJECT / "research" / "brain" / "experiments"
    exp_dir.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%dT%H%M%S")
    exp_doc = f"""# Experiment: Conditional Drawdown Hedge

**ID:** ar-{ts}
**Date:** {timestamp}
**Type:** Portfolio-level risk overlay
**Status:** {status}

## Summary

Tested conditional SH (inverse S&P500) hedge triggered by SPY < SMA200 AND
VIX > 25 AND 20d return < -5%. Overlay approach (post-hoc) on 7-strategy
portfolio equity curve.

## Key Metrics (at {sweet_ratio*100:.0f}% hedge ratio)

- MaxDD reduction: {dd_red:.2f}pp ({base_dd:.2f}% → {base_dd - dd_red:.2f}%)
- CAGR impact: {drag:+.2f}%
- Sharpe: {base_sharpe:.3f} → {best_sharpe:.3f}
- Hedge active: {p1['active_pct']:.1f}% of trading days ({p1['num_activations']} episodes)

## Verdict

{decision}
"""
    with open(exp_dir / f"ar-{ts}.md", "w") as f:
        f.write(exp_doc)
    print(f"  Experiment: research/brain/experiments/ar-{ts}.md")

    return {"status": status, "decision": decision}


# ─── Main ────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Conditional Drawdown Hedge Experiment")
    parser.add_argument("--market", default="sp500")
    parser.add_argument("--output", default=str(OUTPUT_DIR / "hedge_experiment.json"))
    args = parser.parse_args()

    global MARKET
    MARKET = args.market

    print("=" * 70)
    print("  CONDITIONAL DRAWDOWN HEDGE EXPERIMENT")
    print("=" * 70)
    print(f"\n  Market: {MARKET}")
    print(f"  Output: {args.output}")

    # ── Load data ──
    print(f"\n  Loading data...")
    spy = load_spy()
    sh = load_sh()
    vix = load_vix()

    config = get_active_config(MARKET)
    print(f"  Loading universe data for backtest...")
    universe_data = load_universe_data(config)
    print(f"  Universe: {len(universe_data)} tickers")

    # ── Phase 1: Hedge in isolation ──
    base_trigger = TriggerConfig(
        name="base",
        use_trend=True, use_momentum=True, momentum_threshold=-0.05,
        use_vix=True, vix_threshold=25, on_days=3, off_days=5,
    )
    p1_result = phase1_hedge_isolation(spy, sh, vix, base_trigger)

    # ── Phase 2: Combined portfolio ──
    print(f"\n  Running baseline portfolio backtest (this takes ~8-10 minutes)...")
    baseline_equity, baseline_metrics = run_baseline_backtest(config, universe_data)
    print(f"  Baseline: Sharpe={baseline_metrics.get('sharpe',0):.3f}, "
          f"CAGR={baseline_metrics.get('cagr',0)*100:.2f}%, "
          f"MaxDD={baseline_metrics.get('max_drawdown',0)*100:.2f}%")

    p2_result = phase2_combined(baseline_equity, baseline_metrics, sh, vix, spy, base_trigger)

    # Choose hedge ratio for sensitivity: use sweet spot
    sweet_ratio = p2_result["sweet_spot_ratio"]
    # If sweet spot is 0 (no hedging beats baseline), use 0.20 for sensitivity
    test_ratio = sweet_ratio if sweet_ratio > 0 else 0.20

    # ── Phase 3: Sensitivity ──
    p3_result = phase3_sensitivity(baseline_equity, sh, spy, vix, test_ratio)

    # ── Phase 4: Document ──
    print(f"\n{'='*70}")
    print(f"  PHASE 4: Documentation")
    print(f"{'='*70}")
    p4_result = write_brain_decision(p1_result, p2_result, p3_result, OUTPUT_DIR)

    # ── Save full results ──
    full_results = {
        "experiment": "conditional_drawdown_hedge",
        "timestamp": time.strftime("%Y%m%dT%H%M%S"),
        "market": MARKET,
        "phase1_isolation": p1_result,
        "phase2_combined": p2_result,
        "phase3_sensitivity": p3_result,
        "phase4_decision": p4_result,
        "baseline_metrics": {k: round(v, 4) if isinstance(v, float) else v
                            for k, v in baseline_metrics.items()},
    }

    with open(args.output, "w") as f:
        json.dump(full_results, f, indent=2, default=str)
    print(f"\n  Full results: {args.output}")

    # ── Final Verdict ──
    print(f"\n{'='*70}")
    print(f"  FINAL VERDICT: {p4_result['status']}")
    print(f"{'='*70}")
    print(f"\n  {p4_result['decision']}")
    print()


if __name__ == "__main__":
    main()
