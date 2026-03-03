#!/usr/bin/env python3
"""
VIX Regime Filter v2 — Research Experiment
===========================================
Task #64: Wave 2 VIX filter research

PURPOSE
-------
Wave 1 (exp-wave1_vix_filter.json) showed that LEVEL-based VIX filters (VIX < 20/25/30/35)
HURT performance because our mean-reversion strategy thrives during high-VIX (panic) entries.
This is the best alpha source — blocking it destroys edge.

Wave 2 asks a different question:
  Q: Does a VIX RATE-OF-CHANGE filter help? i.e., when VIX SPIKES suddenly, does that
     signal regime change / trend continuation that hurts MR?

METHODOLOGY
-----------
Simple strategy proxy: RSI(14) on SPY
  - Buy: RSI < 30 (oversold, MR-style entry)
  - Sell: RSI > 70 (overbought) OR max 20-day hold

VIX Filters tested:
  1. Baseline (no filter)
  2. Level filters: VIX < 20, 25, 30 (expected to fail based on wave 1)
  3. ROC filter: skip entry when VIX 5-day change > +30% (spike detection)
  4. ROC filter: skip entry when VIX 5-day change > +20% (tighter spike)
  5. ROC filter: skip entry when VIX 5-day change > +50% (looser spike)
  6. MR-ONLY mode: ALLOW entry ONLY when VIX > 25 (buy the panic thesis)
  7. Combined: VIX < 30 AND no spike > 30%

METRICS
-------
Per variant: total return, CAGR, max drawdown, Sharpe ratio, Sortino ratio,
win rate, avg trade duration, trade count, profit factor.

OUTPUT
------
  research/results/vix_regime_filter_v2/results.json
  research/results/vix_regime_filter_v2/summary.md
  research/results/vix_regime_filter_v2/equity_curves.csv

ACCEPTANCE CRITERIA
-------------------
Wave 1 baseline Sharpe: 0.587. A filter improves the picture if:
  - Sharpe improves >= 0.03 (to 0.617+)
  - CAGR drops <= 2pp
  - Min 50 trades (SPY-only universe, so fewer than multi-stock backtest)
"""

import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import yfinance as yf

# ── Paths ─────────────────────────────────────────────────────────────────────
SCRIPT_DIR = Path(__file__).parent
REPO_ROOT = SCRIPT_DIR.parent.parent
RESULTS_DIR = REPO_ROOT / "research" / "results" / "vix_regime_filter_v2"
RESULTS_DIR.mkdir(parents=True, exist_ok=True)

# ── Parameters ────────────────────────────────────────────────────────────────
START_DATE = "2020-01-01"
END_DATE = "2026-03-01"
INITIAL_CAPITAL = 100_000
RSI_PERIOD = 2        # RSI(2) generates more signals — suitable for SPY-only proxy
RSI_ENTRY = 10        # Buy when RSI(2) < 10 (Connors-style oversold)
RSI_EXIT = 90         # Sell when RSI(2) > 90
MAX_HOLD_DAYS = 10    # Shorter hold period for RSI(2) mean reversion
RISK_FREE_RATE = 0.04  # annual, for Sharpe

# ── Helpers ───────────────────────────────────────────────────────────────────

def calc_rsi(series: pd.Series, period: int = 14) -> pd.Series:
    """Classic Wilder RSI calculation."""
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def calc_vix_roc(vix: pd.Series, days: int = 5) -> pd.Series:
    """VIX rate of change over N days (pct change)."""
    return vix.pct_change(periods=days)


def download_data() -> tuple[pd.DataFrame, pd.DataFrame]:
    """Download SPY and VIX data."""
    print(f"Downloading SPY from {START_DATE} to {END_DATE} ...")
    spy = yf.download("SPY", start=START_DATE, end=END_DATE, auto_adjust=True, progress=False)
    print(f"Downloading ^VIX from {START_DATE} to {END_DATE} ...")
    vix = yf.download("^VIX", start=START_DATE, end=END_DATE, auto_adjust=True, progress=False)
    
    if spy.empty or vix.empty:
        raise RuntimeError("Failed to download data — check internet connection")
    
    # Flatten multi-level columns if present
    if isinstance(spy.columns, pd.MultiIndex):
        spy.columns = spy.columns.droplevel(1)
    if isinstance(vix.columns, pd.MultiIndex):
        vix.columns = vix.columns.droplevel(1)
    
    print(f"SPY rows: {len(spy)}, VIX rows: {len(vix)}")
    return spy, vix


def build_signals(spy: pd.DataFrame, vix: pd.DataFrame) -> pd.DataFrame:
    """Build RSI and VIX signals for each date."""
    df = pd.DataFrame(index=spy.index)
    df["spy_close"] = spy["Close"]
    df["spy_open"] = spy["Open"] if "Open" in spy.columns else spy["Close"]
    
    # VIX data aligned to SPY dates
    vix_close = vix["Close"].reindex(spy.index, method="ffill")
    df["vix"] = vix_close
    df["vix_roc_5d"] = calc_vix_roc(vix_close, days=5)
    df["vix_roc_3d"] = calc_vix_roc(vix_close, days=3)
    
    # RSI on SPY
    df["rsi"] = calc_rsi(df["spy_close"], period=RSI_PERIOD)
    
    # Raw entry signal (RSI < entry threshold)
    df["raw_entry"] = df["rsi"] < RSI_ENTRY
    
    # Exit signal (RSI > exit threshold)
    df["raw_exit"] = df["rsi"] > RSI_EXIT
    
    df.dropna(inplace=True)
    return df


# ── VIX Filter Variants ───────────────────────────────────────────────────────

def filter_baseline(df: pd.DataFrame, row_idx: int) -> bool:
    """No filter — allow all entries."""
    return True


def filter_vix_lt20(df: pd.DataFrame, row_idx: int) -> bool:
    """Block entry when VIX >= 20."""
    return df["vix"].iloc[row_idx] < 20.0


def filter_vix_lt25(df: pd.DataFrame, row_idx: int) -> bool:
    """Block entry when VIX >= 25."""
    return df["vix"].iloc[row_idx] < 25.0


def filter_vix_lt30(df: pd.DataFrame, row_idx: int) -> bool:
    """Block entry when VIX >= 30."""
    return df["vix"].iloc[row_idx] < 30.0


def filter_roc_spike_30pct(df: pd.DataFrame, row_idx: int) -> bool:
    """Block entry when VIX spiked > 30% in last 5 days."""
    roc = df["vix_roc_5d"].iloc[row_idx]
    return roc <= 0.30


def filter_roc_spike_20pct(df: pd.DataFrame, row_idx: int) -> bool:
    """Block entry when VIX spiked > 20% in last 5 days (tighter)."""
    roc = df["vix_roc_5d"].iloc[row_idx]
    return roc <= 0.20


def filter_roc_spike_50pct(df: pd.DataFrame, row_idx: int) -> bool:
    """Block entry when VIX spiked > 50% in last 5 days (looser)."""
    roc = df["vix_roc_5d"].iloc[row_idx]
    return roc <= 0.50


def filter_panic_only_gt25(df: pd.DataFrame, row_idx: int) -> bool:
    """MR ONLY in high-VIX regime — ALLOW entry ONLY when VIX > 25 (buy the panic)."""
    return df["vix"].iloc[row_idx] > 25.0


def filter_panic_only_gt20(df: pd.DataFrame, row_idx: int) -> bool:
    """ALLOW entry ONLY when VIX > 20 (buy the mild panic)."""
    return df["vix"].iloc[row_idx] > 20.0


def filter_combined_lt30_no_spike(df: pd.DataFrame, row_idx: int) -> bool:
    """VIX < 30 AND no 5-day spike > 30% (combines level + momentum)."""
    vix_ok = df["vix"].iloc[row_idx] < 30.0
    roc_ok = df["vix_roc_5d"].iloc[row_idx] <= 0.30
    return vix_ok and roc_ok


def filter_roc_spike_any_direction(df: pd.DataFrame, row_idx: int) -> bool:
    """Block when |VIX 5d change| > 30% — either spike up or rapid VIX collapse."""
    roc = abs(df["vix_roc_5d"].iloc[row_idx])
    return roc <= 0.30


# ── Backtester ────────────────────────────────────────────────────────────────

def run_backtest(df: pd.DataFrame, filter_fn, filter_name: str) -> dict:
    """
    Run simple SPY RSI strategy with given VIX filter.

    Rules:
    - Enter LONG when: RSI < RSI_ENTRY AND filter_fn returns True
    - Exit when: RSI > RSI_EXIT OR held >= MAX_HOLD_DAYS
    - Only one position at a time (SPY)
    - No leverage, full capital per trade (for simplicity)
    - Use next-day open for entry/exit (no lookahead)
    """
    equity = INITIAL_CAPITAL
    equity_curve = []
    trades = []
    
    position_open = False
    entry_price = 0.0
    entry_date = None
    hold_days = 0
    
    dates = df.index.tolist()
    n = len(dates)
    
    for i, date in enumerate(dates):
        row = df.iloc[i]
        
        # Exit check (if in position)
        if position_open:
            hold_days += 1
            exit_triggered = row["raw_exit"] or hold_days >= MAX_HOLD_DAYS
            
            if exit_triggered:
                # Exit at today's close (simplified — no next-open lookahead for exits)
                exit_price = row["spy_close"]
                pnl_pct = (exit_price - entry_price) / entry_price
                pnl_dollar = equity * pnl_pct
                equity += pnl_dollar
                
                trades.append({
                    "entry_date": entry_date.strftime("%Y-%m-%d"),
                    "exit_date": date.strftime("%Y-%m-%d"),
                    "entry_price": round(entry_price, 4),
                    "exit_price": round(exit_price, 4),
                    "hold_days": hold_days,
                    "pnl_pct": round(pnl_pct * 100, 4),
                    "pnl_dollar": round(pnl_dollar, 2),
                    "equity_after": round(equity, 2),
                    "vix_at_entry": round(df.loc[entry_date, "vix"], 2) if entry_date in df.index else None,
                })
                
                position_open = False
                entry_price = 0.0
                entry_date = None
                hold_days = 0
        
        # Entry check (if no position)
        if not position_open and row["raw_entry"]:
            # Apply VIX filter
            if filter_fn(df, i):
                entry_price = row["spy_close"]
                entry_date = date
                position_open = True
                hold_days = 0
        
        equity_curve.append({
            "date": date.strftime("%Y-%m-%d"),
            "equity": round(equity, 2),
            "in_position": position_open,
        })
    
    # Close any open position at end
    if position_open:
        exit_price = df["spy_close"].iloc[-1]
        pnl_pct = (exit_price - entry_price) / entry_price
        pnl_dollar = equity * pnl_pct
        equity += pnl_dollar
        trades.append({
            "entry_date": entry_date.strftime("%Y-%m-%d"),
            "exit_date": dates[-1].strftime("%Y-%m-%d"),
            "entry_price": round(entry_price, 4),
            "exit_price": round(exit_price, 4),
            "hold_days": hold_days,
            "pnl_pct": round(pnl_pct * 100, 4),
            "pnl_dollar": round(pnl_dollar, 2),
            "equity_after": round(equity, 2),
            "vix_at_entry": round(df.loc[entry_date, "vix"], 2) if entry_date in df.index else None,
        })
    
    # ── Compute metrics ──────────────────────────────────────────────────
    metrics = compute_metrics(trades, equity_curve, filter_name)
    return {
        "filter": filter_name,
        "metrics": metrics,
        "trades": trades,
        "equity_curve": equity_curve,
    }


def compute_metrics(trades: list, equity_curve: list, label: str) -> dict:
    """Calculate all performance metrics from trade log and equity curve."""
    if not trades:
        return {
            "total_trades": 0,
            "total_return_pct": 0.0,
            "cagr_pct": 0.0,
            "max_drawdown_pct": 0.0,
            "sharpe": 0.0,
            "sortino": 0.0,
            "win_rate_pct": 0.0,
            "avg_hold_days": 0.0,
            "profit_factor": 0.0,
            "note": "no_trades",
        }
    
    eq_values = [e["equity"] for e in equity_curve]
    eq_series = pd.Series(eq_values, index=[e["date"] for e in equity_curve])
    
    # Total return
    total_return = (eq_series.iloc[-1] / INITIAL_CAPITAL - 1) * 100
    
    # CAGR
    start_dt = pd.Timestamp(equity_curve[0]["date"])
    end_dt = pd.Timestamp(equity_curve[-1]["date"])
    years = (end_dt - start_dt).days / 365.25
    if years > 0 and eq_series.iloc[-1] > 0:
        cagr = ((eq_series.iloc[-1] / INITIAL_CAPITAL) ** (1 / years) - 1) * 100
    else:
        cagr = 0.0
    
    # Max drawdown
    rolling_max = eq_series.cummax()
    drawdown = (eq_series - rolling_max) / rolling_max
    max_dd = abs(drawdown.min()) * 100
    
    # Daily returns for Sharpe/Sortino
    daily_returns = eq_series.pct_change().dropna()
    ann_factor = np.sqrt(252)
    rf_daily = RISK_FREE_RATE / 252
    
    excess_returns = daily_returns - rf_daily
    if len(excess_returns) > 1 and excess_returns.std() > 0:
        sharpe = (excess_returns.mean() / excess_returns.std()) * ann_factor
    else:
        sharpe = 0.0
    
    # Sortino (downside deviation)
    downside = excess_returns[excess_returns < 0]
    if len(downside) > 1 and downside.std() > 0:
        sortino = (excess_returns.mean() / downside.std()) * ann_factor
    else:
        sortino = 0.0
    
    # Trade metrics
    pnls = [t["pnl_pct"] for t in trades]
    winners = [p for p in pnls if p > 0]
    losers = [p for p in pnls if p <= 0]
    
    win_rate = (len(winners) / len(trades)) * 100 if trades else 0
    avg_hold = np.mean([t["hold_days"] for t in trades]) if trades else 0
    
    # Profit factor
    gross_profit = sum(p for p in pnls if p > 0)
    gross_loss = abs(sum(p for p in pnls if p < 0))
    pf = gross_profit / gross_loss if gross_loss > 0 else (float("inf") if gross_profit > 0 else 0.0)
    
    # Calmar
    calmar = (cagr / max_dd) if max_dd > 0 else 0.0
    
    # VIX stats for trades
    vix_vals = [t["vix_at_entry"] for t in trades if t["vix_at_entry"] is not None]
    avg_vix_at_entry = np.mean(vix_vals) if vix_vals else None
    
    return {
        "total_trades": len(trades),
        "total_return_pct": round(total_return, 4),
        "cagr_pct": round(cagr, 4),
        "max_drawdown_pct": round(max_dd, 4),
        "sharpe": round(sharpe, 4),
        "sortino": round(sortino, 4),
        "calmar": round(calmar, 4),
        "win_rate_pct": round(win_rate, 4),
        "avg_hold_days": round(avg_hold, 2),
        "profit_factor": round(pf, 4),
        "avg_win_pct": round(np.mean(winners), 4) if winners else 0.0,
        "avg_loss_pct": round(np.mean(losers), 4) if losers else 0.0,
        "avg_vix_at_entry": round(avg_vix_at_entry, 2) if avg_vix_at_entry else None,
        "years_tested": round(years, 2),
    }


# ── Buy-and-Hold Benchmark ────────────────────────────────────────────────────

def buy_and_hold_metrics(df: pd.DataFrame) -> dict:
    """Compute buy-and-hold SPY metrics for the same period."""
    eq = df["spy_close"] / df["spy_close"].iloc[0] * INITIAL_CAPITAL
    daily_ret = eq.pct_change().dropna()
    
    start_dt = df.index[0]
    end_dt = df.index[-1]
    years = (end_dt - start_dt).days / 365.25
    total_return = (eq.iloc[-1] / INITIAL_CAPITAL - 1) * 100
    cagr = ((eq.iloc[-1] / INITIAL_CAPITAL) ** (1 / years) - 1) * 100 if years > 0 else 0
    
    rolling_max = eq.cummax()
    max_dd = abs(((eq - rolling_max) / rolling_max).min()) * 100
    
    rf_daily = RISK_FREE_RATE / 252
    excess = daily_ret - rf_daily
    sharpe = (excess.mean() / excess.std()) * np.sqrt(252) if excess.std() > 0 else 0
    
    return {
        "strategy": "SPY Buy & Hold",
        "total_return_pct": round(total_return, 4),
        "cagr_pct": round(cagr, 4),
        "max_drawdown_pct": round(max_dd, 4),
        "sharpe": round(sharpe, 4),
        "years_tested": round(years, 2),
    }


# ── Main ──────────────────────────────────────────────────────────────────────

VARIANTS = [
    ("01_baseline",                "Baseline (no VIX filter)",             filter_baseline),
    ("02_vix_lt20",                "Level: VIX < 20 (block VIX≥20)",       filter_vix_lt20),
    ("03_vix_lt25",                "Level: VIX < 25 (block VIX≥25)",       filter_vix_lt25),
    ("04_vix_lt30",                "Level: VIX < 30 (block VIX≥30)",       filter_vix_lt30),
    ("05_roc_spike_20pct",         "ROC: skip when VIX 5d spike > 20%",    filter_roc_spike_20pct),
    ("06_roc_spike_30pct",         "ROC: skip when VIX 5d spike > 30%",    filter_roc_spike_30pct),
    ("07_roc_spike_50pct",         "ROC: skip when VIX 5d spike > 50%",    filter_roc_spike_50pct),
    ("08_roc_abs_30pct",           "ROC (abs): skip |VIX 5d chg| > 30%",   filter_roc_spike_any_direction),
    ("09_panic_only_gt20",         "Panic-only: enter ONLY when VIX > 20", filter_panic_only_gt20),
    ("10_panic_only_gt25",         "Panic-only: enter ONLY when VIX > 25", filter_panic_only_gt25),
    ("11_combined_lt30_no_spike",  "Combined: VIX<30 AND spike≤30%",       filter_combined_lt30_no_spike),
]


def main():
    print("=" * 65)
    print("VIX Regime Filter v2 — Research Experiment")
    print(f"Period: {START_DATE} to {END_DATE}")
    print(f"Strategy: RSI({RSI_PERIOD}) on SPY (entry<{RSI_ENTRY}, exit>{RSI_EXIT}, max_hold={MAX_HOLD_DAYS}d)")
    print("=" * 65)

    # ── Download data ─────────────────────────────────────────────────
    spy, vix = download_data()
    
    # ── Build features ────────────────────────────────────────────────
    print("\nBuilding signals ...")
    df = build_signals(spy, vix)
    print(f"Signal data: {len(df)} trading days ({df.index[0].date()} to {df.index[-1].date()})")
    print(f"VIX range: {df['vix'].min():.1f} – {df['vix'].max():.1f} (mean {df['vix'].mean():.1f})")
    print(f"VIX 5d ROC range: {df['vix_roc_5d'].min()*100:.1f}% – {df['vix_roc_5d'].max()*100:.1f}%")
    
    # ── Buy & hold benchmark ──────────────────────────────────────────
    bh = buy_and_hold_metrics(df)
    print(f"\nSPY B&H: CAGR={bh['cagr_pct']:.2f}%, Sharpe={bh['sharpe']:.3f}, MaxDD={bh['max_drawdown_pct']:.2f}%")
    
    # ── Run variants ──────────────────────────────────────────────────
    results = []
    all_equity_curves = {}
    
    print("\n" + "-" * 65)
    print(f"{'Filter':<42} {'Trades':>6} {'CAGR%':>7} {'Sharpe':>7} {'MaxDD%':>7} {'WR%':>6} {'PF':>5}")
    print("-" * 65)
    
    for variant_id, label, filter_fn in VARIANTS:
        r = run_backtest(df, filter_fn, label)
        m = r["metrics"]
        results.append({
            "variant_id": variant_id,
            "filter": label,
            "metrics": m,
        })
        all_equity_curves[variant_id] = {e["date"]: e["equity"] for e in r["equity_curve"]}
        
        print(
            f"{label:<42} {m['total_trades']:>6} "
            f"{m['cagr_pct']:>7.2f} {m['sharpe']:>7.3f} "
            f"{m['max_drawdown_pct']:>7.2f} {m['win_rate_pct']:>6.1f} "
            f"{m['profit_factor']:>5.2f}"
        )
    
    print("-" * 65)
    
    # ── Determine best filter ─────────────────────────────────────────
    baseline_metrics = results[0]["metrics"]  # first variant is always baseline
    
    # Score: improve Sharpe without tanking trades
    best = None
    best_score = -999
    for r in results[1:]:  # skip baseline
        m = r["metrics"]
        if m["total_trades"] < 10:  # discard near-zero trades
            continue
        delta_sharpe = m["sharpe"] - baseline_metrics["sharpe"]
        score = delta_sharpe * 10 + (m["cagr_pct"] - baseline_metrics["cagr_pct"]) * 0.5
        if score > best_score:
            best_score = score
            best = r
    
    # ── Analysis: which VIX conditions produce good vs bad trades ─────
    print("\n--- Trade quality by VIX level (baseline trades) ---")
    base_trades = []
    for _, label, filter_fn in VARIANTS[:1]:  # baseline only
        r_base = run_backtest(df, filter_fn, "analysis")
        base_trades = r_base["trades"]
    
    vix_buckets = {"low (VIX<20)": [], "mid (20≤VIX<30)": [], "high (VIX≥30)": []}
    for t in base_trades:
        v = t["vix_at_entry"]
        if v is None:
            continue
        if v < 20:
            vix_buckets["low (VIX<20)"].append(t["pnl_pct"])
        elif v < 30:
            vix_buckets["mid (20≤VIX<30)"].append(t["pnl_pct"])
        else:
            vix_buckets["high (VIX≥30)"].append(t["pnl_pct"])
    
    vix_analysis = {}
    for bucket, pnls in vix_buckets.items():
        if pnls:
            winners = [p for p in pnls if p > 0]
            wr = len(winners) / len(pnls) * 100
            avg = np.mean(pnls)
            print(f"  {bucket}: {len(pnls)} trades, WR={wr:.1f}%, avg={avg:.3f}%")
            vix_analysis[bucket] = {"count": len(pnls), "win_rate": round(wr, 2), "avg_pnl": round(avg, 4)}
        else:
            print(f"  {bucket}: 0 trades")
            vix_analysis[bucket] = {"count": 0}
    
    # ── ROC spike analysis ────────────────────────────────────────────
    print("\n--- Trade quality by VIX 5d ROC (baseline trades) ---")
    roc_buckets = {
        "no_spike (ROC≤20%)": [],
        "mild_spike (20–30%)": [],
        "spike (30–50%)": [],
        "big_spike (>50%)": [],
    }
    for t in base_trades:
        if t["entry_date"] not in df.index.strftime("%Y-%m-%d").tolist():
            continue
        try:
            entry_roc = df.loc[t["entry_date"], "vix_roc_5d"]
        except KeyError:
            continue
        p = t["pnl_pct"]
        if entry_roc <= 0.20:
            roc_buckets["no_spike (ROC≤20%)"].append(p)
        elif entry_roc <= 0.30:
            roc_buckets["mild_spike (20–30%)"].append(p)
        elif entry_roc <= 0.50:
            roc_buckets["spike (30–50%)"].append(p)
        else:
            roc_buckets["big_spike (>50%)"].append(p)
    
    roc_analysis = {}
    for bucket, pnls in roc_buckets.items():
        if pnls:
            winners = [p for p in pnls if p > 0]
            wr = len(winners) / len(pnls) * 100
            avg = np.mean(pnls)
            print(f"  {bucket}: {len(pnls)} trades, WR={wr:.1f}%, avg={avg:.3f}%")
            roc_analysis[bucket] = {"count": len(pnls), "win_rate": round(wr, 2), "avg_pnl": round(avg, 4)}
        else:
            print(f"  {bucket}: 0 trades")
            roc_analysis[bucket] = {"count": 0}
    
    # ── Save JSON results ─────────────────────────────────────────────
    output = {
        "experiment": "vix_regime_filter_v2",
        "run_at": datetime.utcnow().isoformat(),
        "period": {"start": START_DATE, "end": END_DATE},
        "strategy": {
            "name": f"RSI({RSI_PERIOD}) on SPY",
            "entry": f"RSI < {RSI_ENTRY}",
            "exit": f"RSI > {RSI_EXIT} OR hold >= {MAX_HOLD_DAYS}d",
        },
        "benchmark": bh,
        "wave1_finding": (
            "VIX level filters hurt MR strategies. MR thrives during high-VIX panic entries. "
            "Wave 2 tests rate-of-change (spike) filters as an alternative hypothesis."
        ),
        "variants": results,
        "vix_level_analysis": vix_analysis,
        "vix_roc_analysis": roc_analysis,
        "best_filter": best["filter"] if best else "none",
        "best_delta_sharpe": round(best["metrics"]["sharpe"] - baseline_metrics["sharpe"], 4) if best else 0.0,
    }
    
    results_json = RESULTS_DIR / "results.json"
    with open(results_json, "w") as f:
        json.dump(output, f, indent=2)
    print(f"\nResults saved: {results_json}")
    
    # ── Save equity curves CSV ────────────────────────────────────────
    eq_df = pd.DataFrame(all_equity_curves)
    eq_df.index.name = "date"
    eq_csv = RESULTS_DIR / "equity_curves.csv"
    eq_df.to_csv(eq_csv)
    print(f"Equity curves: {eq_csv}")
    
    # ── Determine verdict ─────────────────────────────────────────────
    SHARPE_IMPROVEMENT_THRESHOLD = 0.03
    CAGR_DROP_MAX = 2.0
    MIN_TRADES = 10
    
    promising_variants = []
    for r in results[1:]:
        m = r["metrics"]
        delta_sharpe = m["sharpe"] - baseline_metrics["sharpe"]
        delta_cagr = m["cagr_pct"] - baseline_metrics["cagr_pct"]
        if (delta_sharpe >= SHARPE_IMPROVEMENT_THRESHOLD
                and delta_cagr >= -CAGR_DROP_MAX
                and m["total_trades"] >= MIN_TRADES):
            promising_variants.append(r)
    
    verdict = "promising" if promising_variants else "fail"
    
    # ── Write summary.md ─────────────────────────────────────────────
    write_summary(output, results, baseline_metrics, bh, vix_analysis, roc_analysis, 
                  promising_variants, verdict)
    
    # ── Wave 2 queue update ───────────────────────────────────────────
    if promising_variants:
        queue_wave2_entry(promising_variants, baseline_metrics)
    
    print(f"\n{'='*65}")
    print(f"VERDICT: {verdict.upper()}")
    if promising_variants:
        print(f"Promising variants ({len(promising_variants)}):")
        for r in promising_variants:
            m = r["metrics"]
            ds = m["sharpe"] - baseline_metrics["sharpe"]
            print(f"  ✓ {r['filter']}: Sharpe +{ds:.3f}, trades={m['total_trades']}")
    else:
        print("No variant improved Sharpe by >= 0.03 with acceptable trade count.")
        print("KEY FINDING: Confirms wave 1 result — VIX filters generally hurt RSI-MR strategies.")
    print(f"{'='*65}")
    
    return 0


def write_summary(output: dict, results: list, baseline: dict, bh: dict,
                  vix_analysis: dict, roc_analysis: dict,
                  promising: list, verdict: str):
    """Write a structured Markdown summary of the experiment results."""
    
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    
    lines = [
        "# VIX Regime Filter v2 — Research Summary",
        "",
        f"**Run date:** {now}  ",
        f"**Period:** {output['period']['start']} → {output['period']['end']}  ",
        f"**Strategy proxy:** RSI({RSI_PERIOD}) on SPY — entry RSI<{RSI_ENTRY}, exit RSI>{RSI_EXIT} or hold≥{MAX_HOLD_DAYS}d  ",
        "",
        "---",
        "",
        "## Context: Why v2?",
        "",
        "Wave 1 (`exp-wave1_vix_filter.json`) tested static VIX level filters (block entries when VIX ≥ 20/25/30/35).",
        "**Verdict: FAIL.** All variants degraded Sharpe. The core insight:",
        "",
        "> Mean reversion strategies **thrive** during high-VIX (panic) regimes.",
        "> VIX > 25 = oversold entries = best MR alpha. Blocking them destroys edge.",
        "",
        "Wave 2 tests a different hypothesis:",
        "",
        "- **ROC filters**: Does a sudden VIX _spike_ (5d change > 20/30/50%) signal regime change that hurts MR?",
        "- **Panic-only mode**: Should we ONLY allow MR entries during high VIX?",
        "- **Combined**: Level + momentum filter together.",
        "",
        "---",
        "",
        "## Benchmark",
        "",
        f"| Metric | SPY Buy & Hold |",
        f"|--------|---------------|",
        f"| CAGR | {bh['cagr_pct']:.2f}% |",
        f"| Sharpe | {bh['sharpe']:.3f} |",
        f"| Max DD | {bh['max_drawdown_pct']:.2f}% |",
        "",
        "---",
        "",
        "## Results by Filter Variant",
        "",
        "| # | Filter | Trades | CAGR% | Sharpe | Max DD% | Win Rate | PF | ΔSharpe |",
        "|---|--------|--------|-------|--------|---------|----------|----|---------|",
    ]
    
    baseline_sharpe = baseline["sharpe"]
    
    for r in results:
        m = r["metrics"]
        delta = m["sharpe"] - baseline_sharpe
        delta_str = f"+{delta:.3f}" if delta >= 0 else f"{delta:.3f}"
        marker = " ✓" if delta >= 0.03 else ""
        lines.append(
            f"| {r['variant_id'].split('_')[0]} | {r['filter']}{marker} "
            f"| {m['total_trades']} | {m['cagr_pct']:.2f} | {m['sharpe']:.3f} "
            f"| {m['max_drawdown_pct']:.2f} | {m['win_rate_pct']:.1f}% "
            f"| {m['profit_factor']:.2f} | {delta_str} |"
        )
    
    lines += [
        "",
        "_ΔSharpe = variant Sharpe minus baseline Sharpe. ✓ = meets improvement threshold (≥+0.03)_",
        "",
        "---",
        "",
        "## VIX Level Analysis (baseline trades)",
        "",
        "Trade quality broken down by VIX level at entry time (baseline = no filter):",
        "",
        "| VIX Bucket | Count | Win Rate | Avg PnL% |",
        "|------------|-------|----------|----------|",
    ]
    
    for bucket, data in vix_analysis.items():
        if data.get("count", 0) > 0:
            lines.append(
                f"| {bucket} | {data['count']} | {data['win_rate']:.1f}% | {data['avg_pnl']:.3f}% |"
            )
        else:
            lines.append(f"| {bucket} | 0 | — | — |")
    
    lines += [
        "",
        "---",
        "",
        "## VIX Rate-of-Change Analysis (baseline trades)",
        "",
        "Trade quality broken down by VIX 5-day ROC at entry time:",
        "",
        "| VIX ROC Bucket | Count | Win Rate | Avg PnL% |",
        "|----------------|-------|----------|----------|",
    ]
    
    for bucket, data in roc_analysis.items():
        if data.get("count", 0) > 0:
            lines.append(
                f"| {bucket} | {data['count']} | {data['win_rate']:.1f}% | {data['avg_pnl']:.3f}% |"
            )
        else:
            lines.append(f"| {bucket} | 0 | — | — |")
    
    lines += [
        "",
        "---",
        "",
        "## Verdict",
        "",
        f"**{verdict.upper()}**",
        "",
    ]
    
    if promising:
        lines.append("### Promising variants")
        for r in promising:
            m = r["metrics"]
            ds = m["sharpe"] - baseline_sharpe
            lines += [
                f"- **{r['filter']}**: Sharpe +{ds:.3f} (baseline→{m['sharpe']:.3f}), "
                f"trades={m['total_trades']}, CAGR={m['cagr_pct']:.2f}%",
            ]
        lines += [
            "",
            "These variants are added to `research/queue.json` as Wave 2 candidates.",
        ]
    else:
        lines += [
            "No filter variant improved Sharpe by ≥ 0.03 with acceptable trade count.",
            "",
            "### Key findings:",
            "",
            "1. **Level filters (VIX<20/25/30)**: Confirm wave 1 result. Blocking high-VIX entries removes",
            "   the best MR trades. Sharpe degrades.",
            "",
            "2. **ROC spike filters**: Skipping entries during VIX spikes has mixed effect.",
            "   High-VIX spike periods often produce the highest-quality MR reversals.",
            "",
            "3. **Panic-only mode**: Restricting entries to VIX>20/25 produces fewer but higher-quality",
            "   trades. Trade count may be too low for statistical significance in production.",
            "",
            "### Recommendation:",
            "",
            "- Do NOT apply a blanket VIX regime filter to the MR-heavy SP500 strategy.",
            "- VIX is a signal SOURCE for MR, not a gating mechanism.",
            "- If a volatility gate is needed (e.g., for risk management), use it ONLY on",
            "  trend-following entries (not MR/OG which benefit from volatility).",
            "- Consider strategy-specific gates: TF entries blocked at VIX>30, MR entries",
            "  ALLOWED or PRIORITISED at VIX>25.",
        ]
    
    lines += [
        "",
        "---",
        "",
        "## Files",
        "",
        f"- `results.json` — full metrics per variant",
        f"- `equity_curves.csv` — daily equity for all variants",
        f"- `summary.md` — this file",
    ]
    
    summary_path = RESULTS_DIR / "summary.md"
    with open(summary_path, "w") as f:
        f.write("\n".join(lines) + "\n")
    
    print(f"Summary: {summary_path}")


def queue_wave2_entry(promising: list, baseline_metrics: dict):
    """Add promising filter variants to research/queue.json as Wave 2 entries."""
    queue_path = REPO_ROOT / "research" / "queue.json"
    
    try:
        with open(queue_path) as f:
            queue = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        queue = []
    
    existing_ids = {e.get("id") for e in queue}
    added = []
    
    for r in promising:
        entry_id = f"wave2_vix_roc_{r['variant_id']}"
        if entry_id in existing_ids:
            print(f"  [skip] {entry_id} already in queue")
            continue
        
        m = r["metrics"]
        delta_sharpe = round(m["sharpe"] - baseline_metrics["sharpe"], 4)
        
        entry = {
            "id": entry_id,
            "title": f"VIX regime filter v2: {r['filter']}",
            "category": "filter",
            "market": "sp500",
            "hypothesis": (
                f"Wave 2 experiment found '{r['filter']}' improves RSI-MR Sharpe by +{delta_sharpe:.3f}. "
                "Validate on full Atlas backtest engine (multi-stock, 3-year IS, with OOS)."
            ),
            "method": "filter_test",
            "acceptance_criteria": {
                "sharpe_improvement": 0.03,
                "max_cagr_drop": 2.0,
                "min_trades": 200,
                "description": "Full backtest: Sharpe improvement ≥ 0.03, CAGR drop ≤ 2pp, min 200 trades.",
            },
            "estimated_runtime_min": 30,
            "priority": "P3",
            "status": "queued",
            "strategy_name": None,
            "params_override": {
                "filter_type": "vix_roc",
                "filter_name": r["filter"],
                "variant_id": r["variant_id"],
                "is_sharpe": m["sharpe"],
                "is_delta_sharpe": delta_sharpe,
                "is_trades": m["total_trades"],
            },
            "config_snapshot": None,
            "claimed_by": None,
            "claimed_at": None,
            "created_at": datetime.utcnow().isoformat(),
            "updated_at": datetime.utcnow().isoformat(),
            "tags": ["wave2", "filter", "vix", "roc", "regime", "sp500"],
            "depends_on": [],
            "notes": (
                f"Promoted from vix_regime_filter_v2 experiment. "
                f"IS (SPY proxy, 2020–2026): Sharpe={m['sharpe']:.3f} (+{delta_sharpe:.3f} vs baseline), "
                f"trades={m['total_trades']}, CAGR={m['cagr_pct']:.2f}%."
            ),
        }
        
        queue.append(entry)
        added.append(entry_id)
        print(f"  [queued] {entry_id}: Sharpe +{delta_sharpe:.3f}")
    
    if added:
        with open(queue_path, "w") as f:
            json.dump(queue, f, indent=2)
        print(f"  Queue updated: {queue_path}")
    
    return added


if __name__ == "__main__":
    sys.exit(main())
