#!/usr/bin/env python3
"""
Atlas Data Scientist — automated analysis of trading system data.

Runs a suite of analyses on Atlas's internal data (decision journal,
research journal, price cache, broker state) and produces actionable
reports. Designed to be called by a pi agent or cron job.

Analyses:
  signal_accuracy   — Forward-test proposed signals against actual prices
  strategy_mix      — Diagnose strategy imbalance and coverage gaps
  confidence_model  — Evaluate confidence scoring quality
  rejection_impact  — Quantify opportunity cost of rejected signals
  regime_state      — Current market regime classification
  alpha_decay       — Track rolling strategy performance vs expectations
  weekly_digest     — Full weekly summary combining all analyses

Usage:
  python3 scripts/data_scientist.py --analysis signal_accuracy
  python3 scripts/data_scientist.py --analysis weekly_digest --json
  python3 scripts/data_scientist.py --analysis all
"""

import argparse
import json
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd


PROJECT = Path(__file__).resolve().parent.parent
CACHE_DIR = PROJECT / "data" / "cache" / "sp500"
JOURNAL_PATH = PROJECT / "journal" / "decision_journal.json"
RESEARCH_JOURNAL = PROJECT / "research" / "journal.json"
STATE_PATH = PROJECT / "state" / "live_sp500.json"
CONFIG_PATH = PROJECT / "config" / "active" / "sp500.json"


def load_journal() -> list[dict]:
    """Load decision journal entries."""
    if not JOURNAL_PATH.exists():
        return []
    data = json.loads(JOURNAL_PATH.read_text())
    return data if isinstance(data, list) else data.get("entries", data.get("signals", []))


def load_price(ticker: str) -> pd.DataFrame | None:
    """Load cached price data for a ticker."""
    path = CACHE_DIR / f"{ticker}.parquet"
    if not path.exists():
        return None
    try:
        return pd.read_parquet(path)
    except Exception:
        return None


def load_config() -> dict:
    """Load active SP500 config."""
    if not CONFIG_PATH.exists():
        return {}
    return json.loads(CONFIG_PATH.read_text())


# ── Analysis: Signal Accuracy ──────────────────────────────────

def analyze_signal_accuracy(entries: list[dict], lookahead_days: list[int] = None) -> dict:
    """
    Forward-test proposed signals against actual price data.
    
    For each proposed signal, check if price moved favorably within N days.
    This is the #1 most valuable analysis — tells you if your signal
    generation actually predicts profitable moves.
    """
    if lookahead_days is None:
        lookahead_days = [1, 3, 5, 10, 20]
    
    proposed = [e for e in entries if e.get("action") == "proposed"]
    
    results_by_period = {}
    signal_details = []
    
    for days in lookahead_days:
        wins = 0
        losses = 0
        total_return = 0.0
        signals_tested = 0
        
        for entry in proposed:
            ticker = entry.get("ticker")
            entry_price = entry.get("entry_price")
            direction = entry.get("direction", "long")
            timestamp = entry.get("timestamp", "")[:10]
            stop_price = entry.get("stop_price")
            
            if not ticker or not entry_price or not timestamp:
                continue
            
            df = load_price(ticker)
            if df is None:
                continue
            
            # Find the signal date in price data
            try:
                signal_date = pd.Timestamp(timestamp)
                # Get rows after signal date
                future = df[df.index > signal_date].head(days)
                if len(future) == 0:
                    continue
            except Exception:
                continue
            
            signals_tested += 1
            
            # Calculate return
            exit_price = future["close"].iloc[-1]
            if direction == "long":
                ret = (exit_price - entry_price) / entry_price
            else:
                ret = (entry_price - exit_price) / entry_price
            
            total_return += ret
            
            if ret > 0:
                wins += 1
            else:
                losses += 1
            
            # Check if stop was hit
            stop_hit = False
            if stop_price and direction == "long":
                stop_hit = future["low"].min() <= stop_price
            elif stop_price and direction == "short":
                stop_hit = future["high"].max() >= stop_price
            
            # Max favorable excursion (MFE) / Max adverse excursion (MAE)
            if direction == "long":
                mfe = (future["high"].max() - entry_price) / entry_price
                mae = (entry_price - future["low"].min()) / entry_price
            else:
                mfe = (entry_price - future["low"].min()) / entry_price
                mae = (future["high"].max() - entry_price) / entry_price
            
            if days == max(lookahead_days):
                signal_details.append({
                    "ticker": ticker,
                    "date": timestamp,
                    "strategy": entry.get("strategy"),
                    "confidence": entry.get("confidence"),
                    "entry_price": entry_price,
                    "exit_price": exit_price,
                    "return_pct": round(ret * 100, 2),
                    "mfe_pct": round(mfe * 100, 2),
                    "mae_pct": round(mae * 100, 2),
                    "stop_hit": stop_hit,
                    "direction": direction,
                })
        
        win_rate = wins / signals_tested if signals_tested > 0 else 0
        avg_return = total_return / signals_tested if signals_tested > 0 else 0
        
        results_by_period[f"{days}d"] = {
            "signals_tested": signals_tested,
            "wins": wins,
            "losses": losses,
            "win_rate": round(win_rate, 3),
            "avg_return_pct": round(avg_return * 100, 3),
            "total_return_pct": round(total_return * 100, 3),
        }
    
    # Strategy breakdown (using longest lookahead)
    strategy_perf = defaultdict(lambda: {"wins": 0, "losses": 0, "returns": []})
    for s in signal_details:
        strat = s["strategy"]
        if s["return_pct"] > 0:
            strategy_perf[strat]["wins"] += 1
        else:
            strategy_perf[strat]["losses"] += 1
        strategy_perf[strat]["returns"].append(s["return_pct"])
    
    strategy_summary = {}
    for strat, perf in strategy_perf.items():
        n = perf["wins"] + perf["losses"]
        strategy_summary[strat] = {
            "signals": n,
            "win_rate": round(perf["wins"] / n, 3) if n > 0 else 0,
            "avg_return_pct": round(np.mean(perf["returns"]), 3) if perf["returns"] else 0,
            "sharpe": round(np.mean(perf["returns"]) / np.std(perf["returns"]), 3) if len(perf["returns"]) > 1 and np.std(perf["returns"]) > 0 else 0,
        }
    
    return {
        "analysis": "signal_accuracy",
        "total_proposed": len(proposed),
        "results_by_period": results_by_period,
        "strategy_breakdown": strategy_summary,
        "top_signals": sorted(signal_details, key=lambda x: -x["return_pct"])[:10],
        "worst_signals": sorted(signal_details, key=lambda x: x["return_pct"])[:10],
    }


# ── Analysis: Strategy Mix ─────────────────────────────────────

def analyze_strategy_mix(entries: list[dict]) -> dict:
    """
    Diagnose strategy imbalance — why is 93% trend_following?
    Check if other strategies are misconfigured or genuinely quiet.
    """
    strat_counts = Counter(e.get("strategy") for e in entries)
    total = len(entries)
    
    # Per-strategy confidence distributions
    strat_confs = defaultdict(list)
    for e in entries:
        strat_confs[e.get("strategy", "?")].append(e.get("confidence", 0))
    
    # Per-strategy action rates
    strat_actions = defaultdict(Counter)
    for e in entries:
        strat_actions[e.get("strategy", "?")][e.get("action", "?")] += 1
    
    # Ticker coverage per strategy
    strat_tickers = defaultdict(set)
    for e in entries:
        strat_tickers[e.get("strategy", "?")].add(e.get("ticker", "?"))
    
    strategies = {}
    for strat in strat_counts:
        confs = strat_confs[strat]
        actions = strat_actions[strat]
        strategies[strat] = {
            "signal_count": strat_counts[strat],
            "pct_of_total": round(100 * strat_counts[strat] / total, 1),
            "unique_tickers": len(strat_tickers[strat]),
            "proposed": actions.get("proposed", 0),
            "rejected": actions.get("rejected", 0),
            "confidence_mean": round(np.mean(confs), 3),
            "confidence_std": round(np.std(confs), 3),
            "confidence_min": round(min(confs), 3),
            "confidence_max": round(max(confs), 3),
        }
    
    # Check config for disabled strategies
    cfg = load_config()
    strat_cfg = cfg.get("strategies", {})
    disabled = [s for s, v in strat_cfg.items() if isinstance(v, dict) and not v.get("enabled", True)]
    enabled = [s for s, v in strat_cfg.items() if isinstance(v, dict) and v.get("enabled", True)]
    
    # Diagnosis
    diagnoses = []
    for strat, info in strategies.items():
        if info["pct_of_total"] > 80:
            diagnoses.append(f"WARNING: {strat} dominates at {info['pct_of_total']}% of all signals — likely over-sensitive or other strategies are under-generating")
        if info["signal_count"] < 5 and strat in enabled:
            diagnoses.append(f"WARNING: {strat} is enabled but generated only {info['signal_count']} signals — check parameter sensitivity")
        if info["confidence_std"] < 0.01:
            diagnoses.append(f"WARNING: {strat} has near-zero confidence variance ({info['confidence_std']}) — confidence scoring may be hardcoded")
    
    return {
        "analysis": "strategy_mix",
        "total_signals": total,
        "strategies": strategies,
        "enabled_strategies": enabled,
        "disabled_strategies": disabled,
        "diagnoses": diagnoses,
    }


# ── Analysis: Confidence Model ─────────────────────────────────

def analyze_confidence_model(entries: list[dict]) -> dict:
    """
    Evaluate whether confidence scores actually predict profitability.
    If high-confidence signals aren't more profitable than low-confidence
    ones, the confidence model is broken.
    """
    proposed = [e for e in entries if e.get("action") == "proposed"]
    
    # Bucket by confidence quartile
    confs = [e.get("confidence", 0) for e in proposed]
    if not confs:
        return {"analysis": "confidence_model", "error": "No proposed signals"}
    
    quartiles = np.percentile(confs, [25, 50, 75])
    
    buckets = {"Q1_low": [], "Q2": [], "Q3": [], "Q4_high": []}
    for e in proposed:
        c = e.get("confidence", 0)
        if c <= quartiles[0]:
            buckets["Q1_low"].append(e)
        elif c <= quartiles[1]:
            buckets["Q2"].append(e)
        elif c <= quartiles[2]:
            buckets["Q3"].append(e)
        else:
            buckets["Q4_high"].append(e)
    
    # Forward-test each bucket
    bucket_results = {}
    for bucket_name, bucket_entries in buckets.items():
        wins = 0
        total_ret = 0.0
        tested = 0
        
        for entry in bucket_entries:
            ticker = entry.get("ticker")
            entry_price = entry.get("entry_price")
            timestamp = entry.get("timestamp", "")[:10]
            direction = entry.get("direction", "long")
            
            if not ticker or not entry_price or not timestamp:
                continue
            
            df = load_price(ticker)
            if df is None:
                continue
            
            try:
                signal_date = pd.Timestamp(timestamp)
                future = df[df.index > signal_date].head(10)  # 10-day forward
                if len(future) == 0:
                    continue
            except Exception:
                continue
            
            tested += 1
            exit_price = future["close"].iloc[-1]
            ret = (exit_price - entry_price) / entry_price if direction == "long" else (entry_price - exit_price) / entry_price
            total_ret += ret
            if ret > 0:
                wins += 1
        
        bucket_results[bucket_name] = {
            "signals": len(bucket_entries),
            "tested": tested,
            "win_rate": round(wins / tested, 3) if tested > 0 else 0,
            "avg_return_pct": round(100 * total_ret / tested, 3) if tested > 0 else 0,
            "confidence_range": f"{min(e.get('confidence',0) for e in bucket_entries):.3f}-{max(e.get('confidence',0) for e in bucket_entries):.3f}" if bucket_entries else "N/A",
        }
    
    # Diagnosis: does higher confidence = better returns?
    q1_ret = bucket_results.get("Q1_low", {}).get("avg_return_pct", 0)
    q4_ret = bucket_results.get("Q4_high", {}).get("avg_return_pct", 0)
    
    if q4_ret <= q1_ret:
        diagnosis = "BROKEN: High-confidence signals (Q4) underperform low-confidence (Q1). Confidence scoring adds no value — needs rebuilding."
    elif q4_ret > q1_ret * 1.5:
        diagnosis = "GOOD: High-confidence signals significantly outperform low-confidence. Threshold tuning could improve results."
    else:
        diagnosis = "WEAK: High-confidence signals slightly outperform. Confidence scoring has marginal predictive value."
    
    return {
        "analysis": "confidence_model",
        "quartile_thresholds": [round(float(q), 3) for q in quartiles],
        "bucket_results": bucket_results,
        "diagnosis": diagnosis,
        "recommendation": f"Optimal threshold likely near {quartiles[1]:.3f} (median) — test in backtest before changing live config.",
    }


# ── Analysis: Rejection Impact ─────────────────────────────────

def analyze_rejection_impact(entries: list[dict]) -> dict:
    """
    Quantify the opportunity cost of rejected signals.
    Were the 44 'max positions exceeded' rejections actually good trades?
    """
    rejected = [e for e in entries if e.get("action") == "rejected"]
    proposed = [e for e in entries if e.get("action") == "proposed"]
    
    # Group rejections by reason
    reason_groups = defaultdict(list)
    for e in rejected:
        reason = e.get("action_reason", "unknown")
        # Normalize
        if "Confidence" in reason and "below threshold" in reason:
            reason = "confidence_below_threshold"
        elif "Max positions" in reason:
            reason = "max_positions_exceeded"
        elif "Risk" in reason and "exceeds max" in reason:
            reason = "risk_exceeds_max"
        elif "sector concentration" in reason:
            reason = "sector_concentration"
        else:
            reason = reason[:50]
        reason_groups[reason].append(e)
    
    # Forward-test each rejection group
    group_results = {}
    for reason, group in reason_groups.items():
        wins = 0
        total_ret = 0.0
        tested = 0
        
        for entry in group:
            ticker = entry.get("ticker")
            entry_price = entry.get("entry_price")
            timestamp = entry.get("timestamp", "")[:10]
            direction = entry.get("direction", "long")
            
            if not ticker or not entry_price or not timestamp:
                continue
            
            df = load_price(ticker)
            if df is None:
                continue
            
            try:
                signal_date = pd.Timestamp(timestamp)
                future = df[df.index > signal_date].head(10)
                if len(future) == 0:
                    continue
            except Exception:
                continue
            
            tested += 1
            exit_price = future["close"].iloc[-1]
            ret = (exit_price - entry_price) / entry_price if direction == "long" else (entry_price - exit_price) / entry_price
            total_ret += ret
            if ret > 0:
                wins += 1
        
        group_results[reason] = {
            "rejected_count": len(group),
            "tested": tested,
            "win_rate": round(wins / tested, 3) if tested > 0 else 0,
            "avg_return_pct": round(100 * total_ret / tested, 3) if tested > 0 else 0,
            "total_missed_return_pct": round(100 * total_ret, 3) if tested > 0 else 0,
        }
    
    # Compare proposed vs rejected
    prop_returns = []
    for entry in proposed:
        ticker = entry.get("ticker")
        entry_price = entry.get("entry_price")
        timestamp = entry.get("timestamp", "")[:10]
        direction = entry.get("direction", "long")
        if not ticker or not entry_price or not timestamp:
            continue
        df = load_price(ticker)
        if df is None:
            continue
        try:
            signal_date = pd.Timestamp(timestamp)
            future = df[df.index > signal_date].head(10)
            if len(future) == 0:
                continue
            exit_price = future["close"].iloc[-1]
            ret = (exit_price - entry_price) / entry_price if direction == "long" else (entry_price - exit_price) / entry_price
            prop_returns.append(ret)
        except Exception:
            continue
    
    return {
        "analysis": "rejection_impact",
        "total_rejected": len(rejected),
        "total_proposed": len(proposed),
        "rejection_groups": group_results,
        "proposed_avg_return_pct": round(100 * np.mean(prop_returns), 3) if prop_returns else 0,
        "proposed_win_rate": round(sum(1 for r in prop_returns if r > 0) / len(prop_returns), 3) if prop_returns else 0,
    }


# ── Analysis: Regime State ──────────────────────────────────────

def analyze_regime_state() -> dict:
    """
    Classify current market regime using SPY as proxy.
    
    Regimes:
      - Trending Up: price > 50MA > 200MA, ADX > 25
      - Trending Down: price < 50MA < 200MA, ADX > 25
      - Mean-Reverting: price oscillating around 50MA, ADX < 20
      - Volatile: VIX > 25 or recent drawdown > 5%
    """
    # Use SPY as market proxy
    spy = load_price("SPY")
    if spy is None:
        return {"analysis": "regime_state", "error": "No SPY data"}
    
    latest = spy.tail(1)
    price = latest["close"].iloc[0]
    
    ma50 = spy["close"].rolling(50).mean().iloc[-1]
    ma200 = spy["close"].rolling(200).mean().iloc[-1]
    
    # Simple ATR-based volatility
    spy["tr"] = np.maximum(
        spy["high"] - spy["low"],
        np.maximum(
            abs(spy["high"] - spy["close"].shift(1)),
            abs(spy["low"] - spy["close"].shift(1))
        )
    )
    atr14 = spy["tr"].rolling(14).mean().iloc[-1]
    atr_pct = atr14 / price * 100
    
    # Drawdown from 52-week high
    high_52w = spy["high"].rolling(252).max().iloc[-1]
    drawdown = (price - high_52w) / high_52w * 100
    
    # 20-day returns
    ret_20d = (price / spy["close"].iloc[-21] - 1) * 100 if len(spy) > 21 else 0
    
    # Breadth proxy: how many of last 20 days were up?
    last_20 = spy["close"].tail(21).pct_change().dropna()
    up_days = (last_20 > 0).sum()
    breadth = up_days / len(last_20) * 100
    
    # Classify
    if price > ma50 > ma200 and ret_20d > 0:
        regime = "TRENDING_UP"
        strategy_recommendation = "Favor trend_following, reduce mean_reversion"
    elif price < ma50 < ma200 and ret_20d < 0:
        regime = "TRENDING_DOWN"
        strategy_recommendation = "Favor short strategies or cash. Reduce long-only exposure."
    elif atr_pct > 2.0 or drawdown < -5:
        regime = "VOLATILE"
        strategy_recommendation = "Reduce position sizes. Tighter stops. Favor mean_reversion on oversold bounces."
    else:
        regime = "MEAN_REVERTING"
        strategy_recommendation = "Favor mean_reversion and opening_gap. Reduce trend_following."
    
    return {
        "analysis": "regime_state",
        "date": latest.index[0].strftime("%Y-%m-%d"),
        "spy_price": round(price, 2),
        "ma50": round(ma50, 2),
        "ma200": round(ma200, 2),
        "atr_pct": round(atr_pct, 2),
        "drawdown_from_52w_high": round(drawdown, 2),
        "return_20d": round(ret_20d, 2),
        "breadth_20d": round(breadth, 1),
        "regime": regime,
        "strategy_recommendation": strategy_recommendation,
    }


# ── Analysis: Alpha Decay ──────────────────────────────────────

def analyze_alpha_decay() -> dict:
    """
    Check if strategy parameters are degrading.
    Compare recent signal accuracy to historical.
    """
    entries = load_journal()
    if len(entries) < 20:
        return {"analysis": "alpha_decay", "error": "Not enough data (need 20+ signals)"}
    
    # Split into halves
    mid = len(entries) // 2
    first_half = entries[:mid]
    second_half = entries[mid:]
    
    def calc_forward_returns(subset):
        returns = []
        for entry in subset:
            if entry.get("action") != "proposed":
                continue
            ticker = entry.get("ticker")
            entry_price = entry.get("entry_price")
            timestamp = entry.get("timestamp", "")[:10]
            direction = entry.get("direction", "long")
            if not ticker or not entry_price or not timestamp:
                continue
            df = load_price(ticker)
            if df is None:
                continue
            try:
                signal_date = pd.Timestamp(timestamp)
                future = df[df.index > signal_date].head(10)
                if len(future) == 0:
                    continue
                exit_price = future["close"].iloc[-1]
                ret = (exit_price - entry_price) / entry_price if direction == "long" else (entry_price - exit_price) / entry_price
                returns.append(ret)
            except Exception:
                continue
        return returns
    
    first_returns = calc_forward_returns(first_half)
    second_returns = calc_forward_returns(second_half)
    
    first_wr = sum(1 for r in first_returns if r > 0) / len(first_returns) if first_returns else 0
    second_wr = sum(1 for r in second_returns if r > 0) / len(second_returns) if second_returns else 0
    
    first_avg = np.mean(first_returns) if first_returns else 0
    second_avg = np.mean(second_returns) if second_returns else 0
    
    decay_detected = second_avg < first_avg * 0.5 and len(second_returns) > 5
    
    return {
        "analysis": "alpha_decay",
        "first_half": {
            "signals": len(first_half),
            "tested": len(first_returns),
            "win_rate": round(first_wr, 3),
            "avg_return_pct": round(first_avg * 100, 3),
        },
        "second_half": {
            "signals": len(second_half),
            "tested": len(second_returns),
            "win_rate": round(second_wr, 3),
            "avg_return_pct": round(second_avg * 100, 3),
        },
        "decay_detected": decay_detected,
        "recommendation": "Re-optimize strategy parameters" if decay_detected else "No significant decay detected",
    }


# ── Weekly Digest ───────────────────────────────────────────────

def generate_weekly_digest() -> dict:
    """Combine all analyses into a weekly summary."""
    entries = load_journal()
    
    return {
        "analysis": "weekly_digest",
        "generated_at": datetime.now().isoformat(),
        "signal_accuracy": analyze_signal_accuracy(entries),
        "strategy_mix": analyze_strategy_mix(entries),
        "confidence_model": analyze_confidence_model(entries),
        "rejection_impact": analyze_rejection_impact(entries),
        "regime_state": analyze_regime_state(),
        "alpha_decay": analyze_alpha_decay(),
    }


# ── Human-readable formatting ───────────────────────────────────

def format_report(result: dict) -> str:
    """Format analysis result as human-readable text."""
    analysis = result.get("analysis", "unknown")
    lines = []
    
    if analysis == "signal_accuracy":
        lines.append("📊 SIGNAL ACCURACY REPORT")
        lines.append(f"   {result['total_proposed']} proposed signals analyzed\n")
        for period, r in result["results_by_period"].items():
            icon = "🟢" if r["win_rate"] > 0.55 else "🔴" if r["win_rate"] < 0.45 else "🟡"
            lines.append(f"   {icon} {period}: {r['win_rate']:.0%} win rate, {r['avg_return_pct']:+.2f}% avg return ({r['signals_tested']} tested)")
        
        lines.append("\n   Strategy breakdown:")
        for strat, s in result.get("strategy_breakdown", {}).items():
            lines.append(f"     {strat}: {s['win_rate']:.0%} WR, {s['avg_return_pct']:+.2f}% avg ({s['signals']} signals)")
    
    elif analysis == "strategy_mix":
        lines.append("📊 STRATEGY MIX REPORT")
        lines.append(f"   {result['total_signals']} total signals\n")
        for strat, info in result["strategies"].items():
            bar = "█" * int(info["pct_of_total"] / 5)
            lines.append(f"   {strat:25s} {info['signal_count']:4d} ({info['pct_of_total']:5.1f}%) {bar}")
        lines.append(f"\n   Enabled: {', '.join(result['enabled_strategies'])}")
        lines.append(f"   Disabled: {', '.join(result['disabled_strategies'])}")
        for d in result.get("diagnoses", []):
            lines.append(f"   ⚠️  {d}")
    
    elif analysis == "confidence_model":
        lines.append("📊 CONFIDENCE MODEL REPORT")
        lines.append(f"   Quartile thresholds: {result.get('quartile_thresholds', [])}\n")
        for bucket, r in result.get("bucket_results", {}).items():
            icon = "🟢" if r["win_rate"] > 0.55 else "🔴" if r["win_rate"] < 0.45 else "🟡"
            lines.append(f"   {icon} {bucket}: {r['win_rate']:.0%} WR, {r['avg_return_pct']:+.2f}% ({r['tested']} tested) [{r['confidence_range']}]")
        lines.append(f"\n   Diagnosis: {result.get('diagnosis', '?')}")
        lines.append(f"   Recommendation: {result.get('recommendation', '?')}")
    
    elif analysis == "rejection_impact":
        lines.append("📊 REJECTION IMPACT REPORT")
        lines.append(f"   {result['total_rejected']} rejected vs {result['total_proposed']} proposed\n")
        lines.append(f"   Proposed signals: {result['proposed_win_rate']:.0%} WR, {result['proposed_avg_return_pct']:+.2f}% avg\n")
        for reason, r in result.get("rejection_groups", {}).items():
            icon = "💰" if r["avg_return_pct"] > 0 else "✅"
            lines.append(f"   {icon} {reason}: {r['rejected_count']} rejected, {r['win_rate']:.0%} WR, {r['avg_return_pct']:+.2f}% avg")
            if r["avg_return_pct"] > 0.5:
                lines.append(f"      ^ MISSED OPPORTUNITY: {r['total_missed_return_pct']:+.2f}% total return left on table")
    
    elif analysis == "regime_state":
        lines.append("📊 MARKET REGIME REPORT")
        lines.append(f"   Date: {result.get('date', '?')}")
        lines.append(f"   SPY: ${result.get('spy_price', 0):.2f}")
        lines.append(f"   50MA: ${result.get('ma50', 0):.2f}  200MA: ${result.get('ma200', 0):.2f}")
        lines.append(f"   ATR%: {result.get('atr_pct', 0):.2f}%  Drawdown: {result.get('drawdown_from_52w_high', 0):.1f}%")
        lines.append(f"   20d Return: {result.get('return_20d', 0):+.1f}%  Breadth: {result.get('breadth_20d', 0):.0f}% up days")
        regime = result.get("regime", "?")
        icon = {"TRENDING_UP": "📈", "TRENDING_DOWN": "📉", "VOLATILE": "🌊", "MEAN_REVERTING": "↔️"}.get(regime, "?")
        lines.append(f"\n   {icon} Regime: {regime}")
        lines.append(f"   💡 {result.get('strategy_recommendation', '?')}")
    
    elif analysis == "alpha_decay":
        lines.append("📊 ALPHA DECAY REPORT")
        for half in ["first_half", "second_half"]:
            h = result.get(half, {})
            label = "Earlier" if half == "first_half" else "Recent"
            lines.append(f"   {label}: {h.get('win_rate', 0):.0%} WR, {h.get('avg_return_pct', 0):+.2f}% avg ({h.get('tested', 0)} tested)")
        decay = result.get("decay_detected", False)
        lines.append(f"\n   {'🔴 DECAY DETECTED' if decay else '🟢 No significant decay'}")
        lines.append(f"   {result.get('recommendation', '?')}")
    
    elif analysis == "weekly_digest":
        lines.append("═" * 60)
        lines.append("📊 ATLAS WEEKLY DATA SCIENCE DIGEST")
        lines.append(f"   Generated: {result.get('generated_at', '?')[:19]}")
        lines.append("═" * 60)
        for key in ["regime_state", "signal_accuracy", "confidence_model", "strategy_mix", "rejection_impact", "alpha_decay"]:
            if key in result:
                lines.append("")
                lines.append(format_report(result[key]))
    
    else:
        lines.append(json.dumps(result, indent=2))
    
    return "\n".join(lines)


# ── Main ────────────────────────────────────────────────────────

ANALYSES = {
    "signal_accuracy": lambda: analyze_signal_accuracy(load_journal()),
    "strategy_mix": lambda: analyze_strategy_mix(load_journal()),
    "confidence_model": lambda: analyze_confidence_model(load_journal()),
    "rejection_impact": lambda: analyze_rejection_impact(load_journal()),
    "regime_state": analyze_regime_state,
    "alpha_decay": analyze_alpha_decay,
    "weekly_digest": generate_weekly_digest,
}


def main():
    parser = argparse.ArgumentParser(description="Atlas Data Scientist")
    parser.add_argument("--analysis", "-a", choices=list(ANALYSES.keys()) + ["all"], default="weekly_digest",
                        help="Which analysis to run (default: weekly_digest)")
    parser.add_argument("--json", action="store_true", help="Output raw JSON")
    parser.add_argument("--telegram", action="store_true", help="Send to Telegram")
    args = parser.parse_args()
    
    os.chdir(PROJECT)
    
    if args.analysis == "all":
        results = {name: fn() for name, fn in ANALYSES.items() if name != "weekly_digest"}
    else:
        results = ANALYSES[args.analysis]()
    
    if args.json:
        print(json.dumps(results, indent=2, default=str))
    else:
        if isinstance(results, dict) and "analysis" in results:
            print(format_report(results))
        else:
            for name, result in results.items():
                print(format_report(result))
                print()
    
    if args.telegram:
        try:
            sys.path.insert(0, str(PROJECT))
            from utils.telegram import send_message
            text = format_report(results) if isinstance(results, dict) and "analysis" in results else "\n\n".join(format_report(r) for r in results.values())
            # Truncate for Telegram (4096 char limit)
            if len(text) > 4000:
                text = text[:3900] + "\n\n... (truncated)"
            send_message(text)
        except Exception as e:
            print(f"Telegram send failed: {e}", file=sys.stderr)


if __name__ == "__main__":
    main()
