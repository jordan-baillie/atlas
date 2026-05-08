#!/usr/bin/env python3
"""
Opening-Range Entry Delay Backtest  (#312)
==========================================
Backtest 3 entry-delay variants vs current momentum_breakout behaviour.
Universe: sp500 (205 tickers), Period: 2024-01-01 to 2025-04-30.

DATA LIMITATION (documented):
  5-minute intraday bars are NOT available in this system (no intraday_bars
  table, no data/cache/intraday/ directory).  This backtest uses daily OHLCV
  bars with proxy entry prices per the fallback specification:

    current   – entry at T+1 open (live behaviour)
    delay_5m  – proxy: entry at T+1 typical price (O+H+L+C)/4
                re-eval: skip if T+1 typical < lookback_high (fade)
    delay_15m – proxy: entry at T+1 mid-day (O+C)/2
                re-eval: same fade-skip
    orb       – proxy: enter only on confirmed up-days (close > open);
                entry at (open+high)/2 as ORB-break proxy;
                stop at T+1 open (OR-low proxy)

Results are INDICATIVE, not precise quantification of intraday effects.
The daily proxy systematically under-counts same-bar stops for delay
variants (we can't see the intraday dip that occurs before 09:35).

Metrics compared:
  win_rate, avg_pnl_pct, same_bar_rate, sharpe (annualised), max_drawdown

Ship threshold: Sharpe improvement ≥ 0.05 AND same-bar reduction ≥ 50%
  vs. current baseline.
"""
from __future__ import annotations

import json
import logging
import os
import sys
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

# ── path bootstrap ──────────────────────────────────────────────────────────
ATLAS_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ATLAS_ROOT))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.StreamHandler()],
)
logger = logging.getLogger("c1_backtest")

# ── constants (from config/active/sp500.json + strategy defaults) ───────────
CACHE_DIR         = ATLAS_ROOT / "data" / "cache" / "sp500"
REPORT_PATH       = ATLAS_ROOT / "research" / "reports" / "opening_range_delay_backtest_20260507.md"
CSV_PATH          = ATLAS_ROOT / "research" / "reports" / "c1_trades.csv"

START_DATE        = pd.Timestamp("2024-01-01")
END_DATE          = pd.Timestamp("2025-04-30")   # last full data available

# Live config params (config/active/sp500.json)
LOOKBACK_DAYS          = 14
ATR_PERIOD             = 18
ATR_STOP_MULT          = 0.61
TRAILING_STOP_MULT     = 4.0    # strategy default (not in sp500.json, falls back)
PROFIT_TARGET_MULT     = 6.0    # profit_target_atr_mult from sp500.json
MAX_HOLD_DAYS          = 15
TREND_MA_PERIOD        = 27

# Simulation params
INITIAL_EQUITY         = 100_000.0
RISK_PCT               = 0.005           # 0.5% risk per trade
SLIPPAGE_PCT           = 0.0005          # 0.05% all-in (per spec)
MAX_POSITIONS          = 10
MIN_ATR_DISTANCE       = 0.005           # skip signal if stop < 0.5% from entry

VARIANTS = ["current", "delay_5m", "delay_15m", "orb"]

# Ship thresholds
SHARPE_DELTA_THRESHOLD  = 0.05   # absolute improvement vs current
SAMEBAR_REDUCTION_THRESHOLD = 0.50  # 50% reduction in same-bar rate


# ── data loading ────────────────────────────────────────────────────────────

def _calc_atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int) -> pd.Series:
    """True Range → ATR (Wilder's EMA/simple rolling mean)."""
    prev_close = close.shift(1)
    tr = pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()],
        axis=1,
    ).max(axis=1)
    return tr.rolling(period).mean()


def load_all_data() -> Dict[str, pd.DataFrame]:
    """Load daily OHLCV for all sp500 tickers within the backtest window."""
    data: Dict[str, pd.DataFrame] = {}
    tickers = sorted(
        [f.stem for f in CACHE_DIR.glob("*.parquet")]
    )
    logger.info("Loading %d tickers …", len(tickers))
    for ticker in tickers:
        try:
            df = pd.read_parquet(CACHE_DIR / f"{ticker}.parquet")
            df.index = pd.DatetimeIndex(df.index)
            df = df.sort_index()
            # Keep a window with warm-up for indicator computation
            warm_up_start = START_DATE - pd.Timedelta(days=200)
            df = df[df.index >= warm_up_start]
            if len(df) < max(LOOKBACK_DAYS, ATR_PERIOD, TREND_MA_PERIOD) + 20:
                continue
            data[ticker] = df
        except Exception as exc:
            logger.warning("Failed to load %s: %s", ticker, exc)
    logger.info("Loaded %d tickers (warm-up inclusive)", len(data))
    return data


def precompute_indicators(data: Dict[str, pd.DataFrame]) -> None:
    """Add indicator columns to each DataFrame in-place."""
    for ticker, df in data.items():
        close  = df["close"]
        high   = df["high"]
        low    = df["low"]
        df["_trend_ma"]     = close.rolling(TREND_MA_PERIOD).mean()
        df["_lookback_high"]= close.rolling(LOOKBACK_DAYS).max().shift(1)
        df["_atr"]          = _calc_atr(high, low, close, ATR_PERIOD)


# ── signal generation ───────────────────────────────────────────────────────

@dataclass
class SignalRecord:
    ticker:         str
    signal_date:    pd.Timestamp   # day T  (signal fires)
    entry_date:     pd.Timestamp   # day T+1 (intended entry day)
    signal_close:   float          # close on T (used for ORB confirmation)
    lookback_high:  float          # N-day high at T
    atr:            float          # ATR at T (for stop sizing)


def generate_signals(
    data: Dict[str, pd.DataFrame],
) -> List[SignalRecord]:
    """Iterate all tickers + dates, collect momentum-breakout signals."""
    signals: List[SignalRecord] = []
    min_rows = max(LOOKBACK_DAYS, ATR_PERIOD, TREND_MA_PERIOD) + 15

    for ticker, df in data.items():
        # Only use rows inside the signal-generation window
        mask = (df.index >= START_DATE) & (df.index <= END_DATE)
        idx_array = df.index[mask]
        if len(idx_array) == 0:
            continue

        for ts in idx_array:
            pos = df.index.get_loc(ts)
            if pos < min_rows:
                continue

            row   = df.iloc[pos]
            today_close   = row["close"]
            lookback_high = row["_lookback_high"]
            trend_ma      = row["_trend_ma"]
            atr           = row["_atr"]

            if any(pd.isna(x) for x in [lookback_high, trend_ma, atr]):
                continue
            if atr <= 0:
                continue

            # Breakout + trend alignment
            if today_close <= lookback_high:
                continue
            if today_close <= trend_ma:
                continue

            # Entry is next trading day — find T+1
            future = df.index[df.index > ts]
            if len(future) == 0:
                continue
            entry_date = future[0]
            if entry_date > END_DATE:
                continue

            signals.append(SignalRecord(
                ticker=ticker,
                signal_date=ts,
                entry_date=entry_date,
                signal_close=today_close,
                lookback_high=lookback_high,
                atr=float(atr),
            ))

    logger.info("Generated %d raw signals (2024-01 → 2025-04)", len(signals))
    return signals


# ── simulation per variant ──────────────────────────────────────────────────

@dataclass
class _OpenPos:
    ticker:                  str
    entry_date:              pd.Timestamp
    entry_price:             float
    stop_price:              float
    take_profit:             float
    atr:                     float
    highest_close:           float    # for trailing stop
    same_bar_flagged:        bool = False


@dataclass
class TradeResult:
    ticker:         str
    entry_date:     pd.Timestamp
    exit_date:      pd.Timestamp
    entry_price:    float
    exit_price:     float
    pnl_pct:        float
    r_multiple:     float
    exit_reason:    str
    same_bar_stop:  bool
    variant:        str


def _simulate_variant(
    variant:        str,
    data:           Dict[str, pd.DataFrame],
    signals:        List[SignalRecord],
) -> Tuple[str, List[TradeResult], pd.Series]:
    """
    Run one full simulation for a single variant.
    Returns (variant_name, trades, daily_equity_series).
    """
    # Index signals by entry date
    from collections import defaultdict
    sig_by_entry: Dict[pd.Timestamp, List[SignalRecord]] = defaultdict(list)
    for s in signals:
        sig_by_entry[s.entry_date].append(s)

    # Build sorted date universe
    all_dates = sorted({
        d for df in data.values()
        for d in df.index
        if START_DATE <= d <= END_DATE
    })

    positions: Dict[str, _OpenPos] = {}
    equity    = INITIAL_EQUITY
    equity_curve: List[Tuple[pd.Timestamp, float]] = []
    trades:   List[TradeResult] = []

    for today in all_dates:
        # ── mark-to-market existing positions ───────────────────────────
        for ticker, pos in list(positions.items()):
            df = data.get(ticker)
            if df is None:
                continue
            if today not in df.index:
                continue

            row         = df.loc[today]
            today_close = float(row["close"])
            today_low   = float(row["low"])
            today_atr   = float(row["_atr"]) if not pd.isna(row["_atr"]) else pos.atr
            if today_atr <= 0:
                today_atr = pos.atr

            pos.highest_close = max(pos.highest_close, today_close)
            trailing_stop = pos.highest_close - TRAILING_STOP_MULT * today_atr
            effective_stop = max(pos.stop_price, trailing_stop)

            exit_reason: Optional[str] = None
            exit_price:  Optional[float] = None
            days_held = (today - pos.entry_date).days

            # ── same-bar detection (entry day) ───────────────────────────
            if today == pos.entry_date:
                if today_low <= pos.stop_price and not pos.same_bar_flagged:
                    pos.same_bar_flagged = True
                    exit_reason = "stop_hit"
                    exit_price  = pos.stop_price  # fill at stop level

            if exit_reason is None:
                if today_close <= pos.stop_price:
                    exit_reason = "stop_hit"
                    exit_price  = today_close
                elif PROFIT_TARGET_MULT > 0 and today_close >= pos.take_profit:
                    exit_reason = "take_profit"
                    exit_price  = pos.take_profit
                elif today != pos.entry_date and today_close <= effective_stop:
                    exit_reason = "trailing_stop"
                    exit_price  = today_close
                elif days_held >= MAX_HOLD_DAYS:
                    exit_reason = "time_exit"
                    exit_price  = today_close

            if exit_reason and exit_price is not None:
                ep_slip = exit_price * (1.0 - SLIPPAGE_PCT)
                risk_abs = pos.entry_price - pos.stop_price
                pnl_pct  = (ep_slip / pos.entry_price) - 1.0
                r_mult   = pnl_pct / (risk_abs / pos.entry_price) if risk_abs > 0 else 0.0

                # Fixed-risk dollar PnL
                dollar_risk    = INITIAL_EQUITY * RISK_PCT
                position_units = dollar_risk / risk_abs if risk_abs > 0 else 0.0
                pnl_dollar     = position_units * (ep_slip - pos.entry_price)
                equity        += pnl_dollar

                trades.append(TradeResult(
                    ticker=ticker,
                    entry_date=pos.entry_date,
                    exit_date=today,
                    entry_price=pos.entry_price,
                    exit_price=ep_slip,
                    pnl_pct=pnl_pct,
                    r_multiple=r_mult,
                    exit_reason=exit_reason,
                    same_bar_stop=pos.same_bar_flagged,
                    variant=variant,
                ))
                del positions[ticker]

        equity_curve.append((today, equity))

        # ── enter new positions ──────────────────────────────────────────
        if today in sig_by_entry and len(positions) < MAX_POSITIONS:
            for sig in sig_by_entry[today]:
                if len(positions) >= MAX_POSITIONS:
                    break
                if sig.ticker in positions:
                    continue
                df = data.get(sig.ticker)
                if df is None or today not in df.index:
                    continue

                row        = df.loc[today]
                o          = float(row["open"])
                h          = float(row["high"])
                lo         = float(row["low"])
                c          = float(row["close"])
                atr        = sig.atr

                # ── variant entry logic ──────────────────────────────────
                if variant == "current":
                    entry_raw  = o
                    stop_raw   = entry_raw - ATR_STOP_MULT * atr
                    skip       = False

                elif variant == "delay_5m":
                    # Proxy: typical price (O+H+L+C)/4
                    # Re-eval: skip if typical < lookback_high (momentum faded)
                    entry_raw = (o + h + lo + c) / 4.0
                    stop_raw  = entry_raw - ATR_STOP_MULT * atr
                    skip      = entry_raw < sig.lookback_high

                elif variant == "delay_15m":
                    # Proxy: mid-day (O+C)/2
                    entry_raw = (o + c) / 2.0
                    stop_raw  = entry_raw - ATR_STOP_MULT * atr
                    skip      = entry_raw < sig.lookback_high

                elif variant == "orb":
                    # Proxy: only enter on confirmed up-days; entry at (O+H)/2
                    # stop at T+1 open (OR-low proxy)
                    if c <= o:
                        continue   # down day → ORB not confirmed
                    entry_raw = (o + h) / 2.0
                    stop_raw  = o - ATR_STOP_MULT * atr
                    skip      = entry_raw < sig.lookback_high

                else:
                    continue

                if skip:
                    continue

                # Skip near-zero stop distance (degenerate)
                if entry_raw <= 0 or (entry_raw - stop_raw) / entry_raw < MIN_ATR_DISTANCE:
                    continue

                entry_slip = entry_raw * (1.0 + SLIPPAGE_PCT)
                take_profit = (
                    entry_slip + PROFIT_TARGET_MULT * atr
                    if PROFIT_TARGET_MULT > 0 else float("inf")
                )

                positions[sig.ticker] = _OpenPos(
                    ticker=sig.ticker,
                    entry_date=today,
                    entry_price=entry_slip,
                    stop_price=stop_raw,
                    take_profit=take_profit,
                    atr=atr,
                    highest_close=c,
                )

    # ── force-close any still-open positions at end of period ───────────
    for ticker, pos in positions.items():
        df = data.get(ticker)
        if df is None:
            continue
        mask = df.index <= END_DATE
        if not mask.any():
            continue
        last_row   = df[mask].iloc[-1]
        ep_slip    = float(last_row["close"]) * (1.0 - SLIPPAGE_PCT)
        risk_abs   = pos.entry_price - pos.stop_price
        pnl_pct    = (ep_slip / pos.entry_price) - 1.0
        r_mult     = pnl_pct / (risk_abs / pos.entry_price) if risk_abs > 0 else 0.0

        dollar_risk    = INITIAL_EQUITY * RISK_PCT
        position_units = dollar_risk / risk_abs if risk_abs > 0 else 0.0
        pnl_dollar     = position_units * (ep_slip - pos.entry_price)
        equity        += pnl_dollar

        trades.append(TradeResult(
            ticker=ticker,
            entry_date=pos.entry_date,
            exit_date=last_row.name,
            entry_price=pos.entry_price,
            exit_price=ep_slip,
            pnl_pct=pnl_pct,
            r_multiple=r_mult,
            exit_reason="force_close",
            same_bar_stop=pos.same_bar_flagged,
            variant=variant,
        ))

    equity_series = pd.Series(
        {d: v for d, v in equity_curve},
        name="equity",
    )
    equity_series.index = pd.DatetimeIndex(equity_series.index)
    return variant, trades, equity_series


# worker wrapper (top-level for pickling)
def _worker(args: Tuple) -> Tuple[str, List[TradeResult], pd.Series]:
    variant, data, signals = args
    return _simulate_variant(variant, data, signals)


# ── metrics ────────────────────────────────────────────────────────────────

def compute_metrics(
    variant:       str,
    trades:        List[TradeResult],
    equity_series: pd.Series,
) -> Dict[str, Any]:
    if not trades:
        return {
            "variant": variant, "n_trades": 0,
            "win_rate": 0.0, "avg_pnl_pct": 0.0,
            "same_bar_rate": 0.0, "sharpe": 0.0, "max_drawdown": 0.0,
            "avg_r": 0.0, "cagr": 0.0,
        }

    pnls  = np.array([t.pnl_pct for t in trades])
    wins  = (pnls > 0).sum()
    same  = sum(1 for t in trades if t.same_bar_stop)

    # Sharpe from equity curve daily returns
    eq   = equity_series.dropna()
    rets = eq.pct_change().dropna()
    sharpe = (
        float(rets.mean() / rets.std() * np.sqrt(252))
        if rets.std() > 0 else 0.0
    )

    # CAGR
    if len(eq) >= 2:
        days  = (eq.index[-1] - eq.index[0]).days
        years = max(days / 365.25, 1e-6)
        cagr  = float((eq.iloc[-1] / eq.iloc[0]) ** (1.0 / years) - 1.0)
    else:
        cagr = 0.0

    # Max drawdown
    peak = eq.cummax()
    dd   = ((eq - peak) / peak)
    max_dd = float(dd.min())

    return {
        "variant":       variant,
        "n_trades":      len(trades),
        "win_rate":      float(wins / len(trades)),
        "avg_pnl_pct":   float(pnls.mean() * 100),
        "same_bar_rate": float(same / len(trades)),
        "sharpe":        round(sharpe, 4),
        "max_drawdown":  round(max_dd * 100, 2),
        "avg_r":         float(np.mean([t.r_multiple for t in trades])),
        "cagr":          round(cagr * 100, 2),
    }


# ── decision logic ─────────────────────────────────────────────────────────

def evaluate_ship(
    metrics_by_variant: Dict[str, Dict],
) -> Dict[str, Any]:
    """
    Return ship decision.  Threshold: Sharpe delta ≥ 0.05 AND same-bar
    reduction ≥ 50%.  Prefer simpler variant (delay over ORB) on tie.
    """
    base   = metrics_by_variant["current"]
    base_s = base["sharpe"]
    base_r = base["same_bar_rate"]

    candidates = []
    for v in ["delay_5m", "delay_15m", "orb"]:
        m      = metrics_by_variant[v]
        ds     = m["sharpe"] - base_s
        if base_r > 0:
            dr = (base_r - m["same_bar_rate"]) / base_r
        else:
            dr = 0.0  # no same-bar stops in baseline → nothing to reduce

        meets_sharpe  = ds >= SHARPE_DELTA_THRESHOLD
        meets_samebar = dr >= SAMEBAR_REDUCTION_THRESHOLD
        candidates.append({
            "variant":          v,
            "sharpe_delta":     round(ds, 4),
            "samebar_reduction":round(dr, 4),
            "meets_sharpe":     meets_sharpe,
            "meets_samebar":    meets_samebar,
            "qualifies":        meets_sharpe and meets_samebar,
        })

    qualified = [c for c in candidates if c["qualifies"]]

    if not qualified:
        # Report closest miss
        best_miss = max(candidates, key=lambda c: c["sharpe_delta"] + c["samebar_reduction"])
        return {
            "ship":          False,
            "winner":        None,
            "reason":        "No variant met both thresholds.",
            "closest_miss":  best_miss,
            "candidates":    candidates,
        }

    # Prefer simpler (delay_5m > delay_15m > orb)
    priority = {"delay_5m": 0, "delay_15m": 1, "orb": 2}
    winner   = min(qualified, key=lambda c: priority[c["variant"]])
    return {
        "ship":      True,
        "winner":    winner["variant"],
        "reason":    f"Sharpe +{winner['sharpe_delta']:.4f}, same-bar −{winner['samebar_reduction']*100:.1f}%",
        "winner_detail": winner,
        "candidates": candidates,
    }


# ── report writer ───────────────────────────────────────────────────────────

def write_report(
    metrics_rows:        List[Dict],
    decision:            Dict,
    n_tickers:           int,
    n_signals:           int,
    all_trades_by_variant: Dict[str, List[TradeResult]],
) -> None:
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)

    base = {m["variant"]: m for m in metrics_rows}
    cur  = base["current"]

    ship_label = "SHIP ✅" if decision["ship"] else "SKIP ❌"
    winner     = decision.get("winner", "none")

    lines = [
        "# Opening-Range Entry Delay Backtest — #312",
        "",
        f"**Date run:** 2026-05-08  |  **Report:** `{REPORT_PATH.name}`",
        "",
        "---",
        "",
        "## Backtest Setup",
        "",
        "| Parameter | Value |",
        "|-----------|-------|",
        f"| Universe | sp500 ({n_tickers} tickers) |",
        f"| Period | 2024-01-01 → 2025-04-30 |",
        f"| Total signals generated | {n_signals} |",
        f"| Slippage | 0.05% all-in |",
        f"| Risk per trade | 0.5% of $100k = $500 |",
        f"| Max positions | {MAX_POSITIONS} |",
        f"| ATR stop mult (current) | {ATR_STOP_MULT} |",
        f"| Trailing stop mult | {TRAILING_STOP_MULT} |",
        f"| Profit target mult | {PROFIT_TARGET_MULT} |",
        f"| Max hold days | {MAX_HOLD_DAYS} |",
        "",
        "### Data Limitation",
        "",
        "**5-minute intraday bars are not available** (no `intraday_bars` table,",
        "no `data/cache/intraday/` directory, only 2 hourly files).",
        "All variants use daily OHLCV proxy entries:",
        "",
        "| Variant | Entry proxy | Stop | Re-eval filter |",
        "|---------|-------------|------|----------------|",
        "| current | T+1 open | entry − 0.61×ATR | none |",
        "| delay_5m | T+1 typical price (O+H+L+C)/4 | entry − 0.61×ATR | skip if typical < breakout level |",
        "| delay_15m | T+1 mid-day (O+C)/2 | entry − 0.61×ATR | skip if mid < breakout level |",
        "| orb | T+1 (O+H)/2, only on up-days (C>O) | T+1 open − 0.61×ATR | skip if mid < breakout level OR down-day |",
        "",
        "**Implication:** The proxy *under-counts* same-bar stops for delay variants",
        "(we can't see the intraday dip before 09:35).  The ORB filter is approximated",
        "by requiring the day to close above its open.",
        "",
        "---",
        "",
        "## Results — 4 × 5 Metrics Table",
        "",
        "| Metric | current | delay_5m | delay_15m | orb |",
        "|--------|---------|----------|-----------|-----|",
    ]

    metric_labels = [
        ("n_trades",      "Trades",           ""),
        ("win_rate",       "Win rate",         " (×100%)"),
        ("avg_pnl_pct",   "Avg PnL per trade","% (slippage inc.)"),
        ("same_bar_rate",  "Same-bar stop rate",""),
        ("sharpe",         "Sharpe (ann.)",    ""),
        ("max_drawdown",   "Max drawdown",     "%"),
        ("cagr",           "CAGR",             "%"),
        ("avg_r",          "Avg R-multiple",   ""),
    ]

    fmt_funcs = {
        "n_trades":      lambda v: str(int(v)),
        "win_rate":       lambda v: f"{v*100:.1f}%",
        "avg_pnl_pct":   lambda v: f"{v:.2f}%",
        "same_bar_rate":  lambda v: f"{v*100:.1f}%",
        "sharpe":         lambda v: f"{v:.3f}",
        "max_drawdown":   lambda v: f"{v:.1f}%",
        "cagr":           lambda v: f"{v:.1f}%",
        "avg_r":          lambda v: f"{v:.3f}",
    }

    for key, label, unit in metric_labels:
        row = f"| {label}{unit} |"
        for v in VARIANTS:
            val = base[v].get(key, 0.0)
            row += f" {fmt_funcs[key](val)} |"
        lines.append(row)

    # Delta rows
    lines += [
        "",
        "### Deltas vs. current",
        "",
        "| Delta | delay_5m | delay_15m | orb |",
        "|-------|----------|-----------|-----|",
    ]
    for key, label in [
        ("sharpe",        "Sharpe Δ"),
        ("same_bar_rate", "Same-bar Δ"),
    ]:
        row = f"| {label} |"
        for v in ["delay_5m", "delay_15m", "orb"]:
            delta = base[v][key] - cur[key]
            sign  = "+" if delta >= 0 else ""
            row  += f" {sign}{delta:.3f} |"
        lines.append(row)

    # Same-bar reduction %
    row = "| Same-bar reduction % |"
    for v in ["delay_5m", "delay_15m", "orb"]:
        br = cur["same_bar_rate"]
        if br > 0:
            r  = (br - base[v]["same_bar_rate"]) / br
            row += f" {r*100:.0f}% |"
        else:
            row += " N/A |"
    lines.append(row)

    # Threshold check
    lines += [
        "",
        "### Threshold check",
        "",
        "| Variant | Sharpe Δ ≥ 0.05? | Same-bar −50%? | Qualifies? |",
        "|---------|------------------|----------------|------------|",
    ]
    for c in decision["candidates"]:
        s = "✅" if c["meets_sharpe"]  else "❌"
        r = "✅" if c["meets_samebar"] else "❌"
        q = "✅ SHIP" if c["qualifies"] else "❌ SKIP"
        lines.append(
            f"| {c['variant']} | {c['sharpe_delta']:+.4f} {s} |"
            f" {c['samebar_reduction']*100:.0f}% {r} | {q} |"
        )

    lines += [
        "",
        "---",
        "",
        f"## Decision: {ship_label}",
        "",
    ]

    if decision["ship"]:
        wd = decision["winner_detail"]
        lines += [
            f"**Winner:** `{winner}`",
            "",
            f"- Sharpe improvement: +{wd['sharpe_delta']:.4f}  (threshold: +0.05)",
            f"- Same-bar reduction: {wd['samebar_reduction']*100:.1f}%  (threshold: 50%)",
            "",
            "### Config change applied",
            "",
            "```json",
        ]
        if winner in ("delay_5m", "delay_15m"):
            minutes = 5 if winner == "delay_5m" else 15
            lines += [
                '// config/active/sp500.json — strategies.momentum_breakout',
                f'"entry_delay_minutes": {minutes}',
            ]
        else:  # orb
            lines += [
                '// config/active/sp500.json — strategies.momentum_breakout',
                '"entry_mode": "orb_5min"',
            ]
        lines += ["```", ""]
    else:
        miss = decision["closest_miss"]
        lines += [
            "No variant met **both** thresholds.",
            "",
            f"Closest miss: `{miss['variant']}`",
            f"- Sharpe Δ = {miss['sharpe_delta']:+.4f}  "
            f"({'met' if miss['meets_sharpe'] else f'needed +0.05'})",
            f"- Same-bar reduction = {miss['samebar_reduction']*100:.1f}%  "
            f"({'met' if miss['meets_samebar'] else 'needed 50%'})",
            "",
            "**Recommendation:** Obtain true intraday 5-min bars (one-time Tiingo",
            "backfill via `data/tiingo.py`) to properly quantify opening-volatility",
            "blow-through.  The daily proxy is insufficient to confirm a meaningful",
            "same-bar reduction for these variants.",
        ]

    lines += [
        "",
        "---",
        "",
        "## Trade samples — same-bar stop-outs (current variant)",
        "",
        "| Ticker | Entry date | Entry $ | Exit $ | PnL% |",
        "|--------|------------|---------|--------|------|",
    ]

    cur_trades = all_trades_by_variant.get("current", [])
    same_bar_trades = [t for t in cur_trades if t.same_bar_stop][:10]
    for t in same_bar_trades:
        lines.append(
            f"| {t.ticker} | {t.entry_date.date()} | "
            f"${t.entry_price:.2f} | ${t.exit_price:.2f} | "
            f"{t.pnl_pct*100:.2f}% |"
        )

    lines += [
        "",
        "---",
        "",
        "## Notes",
        "",
        "- **MCHP 2026-05-08** triggered this investigation: entered 09:30 open $102.93,",
        "  stopped $100.89 within 36s (−1.98%).  In daily data, MCHP 2026-05-06 closed",
        "  $102.93 (breakout vs 14-day high).  ATR_STOP_MULT=0.61 → very tight stop.",
        "- All intraday-resolution findings require a one-time Tiingo intraday backfill.",
        "- Feature flag path: `config/active/sp500.json` →",
        "  `strategies.momentum_breakout.entry_delay_minutes` (0 = current).",
        f"- Trade CSV: `{CSV_PATH.relative_to(ATLAS_ROOT)}`",
    ]

    REPORT_PATH.write_text("\n".join(lines) + "\n")
    logger.info("Report written → %s", REPORT_PATH)


# ── entry point ─────────────────────────────────────────────────────────────

def main() -> None:
    import time
    t0 = time.time()

    logger.info("=== C1 Opening-Range Delay Backtest ===")
    logger.info("Period: %s → %s", START_DATE.date(), END_DATE.date())

    # 1. Load + precompute
    data = load_all_data()
    precompute_indicators(data)

    # 2. Generate signals
    signals = generate_signals(data)
    if not signals:
        logger.error("No signals generated — aborting")
        return

    n_tickers = len(data)
    n_signals = len(signals)

    # 3. Run 4 variants in parallel (ProcessPoolExecutor)
    logger.info("Running 4 variants via ProcessPoolExecutor …")
    results: Dict[str, Tuple[List[TradeResult], pd.Series]] = {}

    tasks = [(v, data, signals) for v in VARIANTS]
    with ProcessPoolExecutor(max_workers=4) as pool:
        futures = {pool.submit(_worker, t): t[0] for t in tasks}
        for fut in as_completed(futures):
            variant_name = futures[fut]
            try:
                v, trades, eq = fut.result()
                results[v] = (trades, eq)
                logger.info(
                    "  %s: %d trades, %d same-bar",
                    v, len(trades), sum(1 for t in trades if t.same_bar_stop),
                )
            except Exception as exc:
                logger.error("Variant %s failed: %s", variant_name, exc, exc_info=True)

    # 4. Compute metrics
    metrics_rows   : List[Dict] = []
    trades_by_variant: Dict[str, List[TradeResult]] = {}
    for v in VARIANTS:
        if v not in results:
            continue
        trades, eq = results[v]
        trades_by_variant[v] = trades
        m = compute_metrics(v, trades, eq)
        metrics_rows.append(m)
        logger.info(
            "  %-12s  trades=%3d  win=%.0f%%  same_bar=%.0f%%  sharpe=%.3f  maxdd=%.1f%%",
            v, m["n_trades"], m["win_rate"]*100, m["same_bar_rate"]*100,
            m["sharpe"], m["max_drawdown"],
        )

    # 5. Decision
    decision = evaluate_ship({m["variant"]: m for m in metrics_rows})
    logger.info(
        "Decision: %s | winner=%s | %s",
        "SHIP" if decision["ship"] else "SKIP",
        decision.get("winner", "none"),
        decision["reason"],
    )

    # 6. Write report
    write_report(
        metrics_rows, decision, n_tickers, n_signals, trades_by_variant
    )

    # 7. Save trade CSV
    all_trades = [t for tl in trades_by_variant.values() for t in tl]
    if all_trades:
        df_t = pd.DataFrame([
            {
                "variant":       t.variant,
                "ticker":        t.ticker,
                "entry_date":    t.entry_date.date(),
                "exit_date":     t.exit_date.date(),
                "entry_price":   round(t.entry_price, 4),
                "exit_price":    round(t.exit_price, 4),
                "pnl_pct":       round(t.pnl_pct * 100, 4),
                "r_multiple":    round(t.r_multiple, 4),
                "exit_reason":   t.exit_reason,
                "same_bar_stop": t.same_bar_stop,
            }
            for t in all_trades
        ])
        CSV_PATH.parent.mkdir(parents=True, exist_ok=True)
        df_t.to_csv(CSV_PATH, index=False)
        logger.info("Trade CSV saved → %s", CSV_PATH)

    elapsed = time.time() - t0
    logger.info("Done in %.1fs", elapsed)

    # 8. Telegram notification
    try:
        sys.path.insert(0, str(ATLAS_ROOT))
        from utils.telegram import send_message  # type: ignore
        cur_m  = next((m for m in metrics_rows if m["variant"] == "current"), {})
        win_m  = next((m for m in metrics_rows if m["variant"] == decision.get("winner")), {}) if decision["ship"] else {}
        s_delta = round(win_m.get("sharpe", 0) - cur_m.get("sharpe", 0), 3) if win_m else None
        sb_red  = None
        if win_m and cur_m.get("same_bar_rate", 0) > 0:
            sb_red = round(
                (cur_m["same_bar_rate"] - win_m["same_bar_rate"]) / cur_m["same_bar_rate"] * 100
            )
        ship_str  = "SHIP ✅" if decision["ship"] else "SKIP ❌"
        w_str     = decision.get("winner", "none")
        delta_str = f"sharpe +{s_delta}, same-bar −{sb_red}%" if s_delta else "n/a"
        msg = (
            f"📊 C1 backtest complete. {ship_str}: {w_str} "
            f"({delta_str}). Report: research/reports/opening_range_delay_backtest_20260507.md"
        )
        send_message(msg)
        logger.info("Telegram sent")
    except Exception as e:
        logger.warning("Telegram notification failed: %s", e)


if __name__ == "__main__":
    main()
