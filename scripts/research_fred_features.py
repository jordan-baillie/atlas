#!/usr/bin/env python3
"""FRED / Macro feature research for SP500 strategies.

Downloads macro indicators (yields, VIX, dollar, commodities) via yfinance,
aligns with SP500 returns, and computes lead/lag correlations and predictive
signal analysis.

Goal: Identify which macro features have predictive value for:
  1. SP500 next-day/next-week returns
  2. Mean-reversion entry timing (oversold conditions)
  3. Trend-following regime detection
  4. Volatility regime shifts

Output: research report at tasks/research/fred_features_report.md
"""

import os
import sys
import warnings
import json
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import yfinance as yf

warnings.filterwarnings("ignore")

# ── Configuration ─────────────────────────────────────────────

OUTPUT_DIR = Path(__file__).parent.parent / "tasks" / "research"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
REPORT_PATH = OUTPUT_DIR / "fred_features_report.md"

# Start from 2000 — covers dot-com crash, GFC, COVID, 2022 bear
START_DATE = "2000-01-01"

# Macro tickers available via yfinance
MACRO_TICKERS = {
    "^TNX": "10Y_yield",
    "^FVX": "5Y_yield",
    "^IRX": "13W_yield",
    "^TYX": "30Y_yield",
    "^VIX": "VIX",
    "DX-Y.NYB": "USD_index",
    "GC=F": "gold",
    "CL=F": "crude_oil",
    "HG=F": "copper",
}

SP500_TICKER = "^GSPC"

# Derived features to compute
DERIVED_FEATURES = [
    "yield_curve_10y_3m",      # 10Y - 13W spread (classic recession indicator)
    "yield_curve_10y_5y",      # 10Y - 5Y spread (mid-curve)
    "yield_curve_30y_10y",     # 30Y - 10Y spread (long end)
    "VIX_sma20_ratio",         # VIX / 20-day SMA(VIX) — mean-reversion signal
    "VIX_roc_5d",              # VIX 5-day rate of change
    "VIX_roc_1d",              # VIX overnight change
    "gold_copper_ratio",       # Gold/Copper — risk sentiment
    "USD_roc_5d",              # Dollar 5-day momentum
    "oil_roc_5d",              # Oil 5-day momentum
    "10Y_yield_roc_5d",        # Yield 5-day momentum
    "yield_curve_roc_5d",      # Yield curve slope 5-day change
    "VIX_level_quintile",      # VIX regime (1-5)
    "yield_curve_regime",      # Inverted vs normal
]

# Prediction horizons (in trading days)
HORIZONS = {
    "1d": 1,
    "3d": 3,
    "5d": 5,
    "10d": 10,
    "20d": 20,
}


def download_data():
    """Download SP500 and macro data."""
    print("Downloading SP500...")
    sp500 = yf.download(SP500_TICKER, start=START_DATE, progress=False)
    # Handle multi-level columns from yfinance
    if isinstance(sp500.columns, pd.MultiIndex):
        sp500.columns = sp500.columns.get_level_values(0)
    sp500_close = sp500["Close"].rename("SP500")

    macro_data = {}
    for ticker, name in MACRO_TICKERS.items():
        print(f"  Downloading {name} ({ticker})...")
        try:
            data = yf.download(ticker, start=START_DATE, progress=False)
            if isinstance(data.columns, pd.MultiIndex):
                data.columns = data.columns.get_level_values(0)
            if len(data) > 0:
                macro_data[name] = data["Close"].rename(name)
            else:
                print(f"    ⚠️ No data for {ticker}")
        except Exception as e:
            print(f"    ❌ Error: {e}")

    return sp500_close, macro_data


def build_feature_matrix(sp500, macro_data):
    """Build aligned feature matrix with raw + derived features."""
    # Combine all into one DataFrame
    df = pd.DataFrame({"SP500": sp500})
    for name, series in macro_data.items():
        df[name] = series

    # Forward-fill macro data (some series have gaps)
    df = df.ffill()
    df = df.dropna(subset=["SP500"])

    # ── SP500 returns at various horizons ──
    for label, days in HORIZONS.items():
        df[f"SP500_fwd_{label}"] = df["SP500"].pct_change(days).shift(-days)

    # Current returns (for lagged analysis)
    df["SP500_ret_1d"] = df["SP500"].pct_change(1)
    df["SP500_ret_5d"] = df["SP500"].pct_change(5)

    # ── Derived features ──

    # Yield curve spreads
    if "10Y_yield" in df.columns and "13W_yield" in df.columns:
        df["yield_curve_10y_3m"] = df["10Y_yield"] - df["13W_yield"]
    if "10Y_yield" in df.columns and "5Y_yield" in df.columns:
        df["yield_curve_10y_5y"] = df["10Y_yield"] - df["5Y_yield"]
    if "30Y_yield" in df.columns and "10Y_yield" in df.columns:
        df["yield_curve_30y_10y"] = df["30Y_yield"] - df["10Y_yield"]

    # VIX features
    if "VIX" in df.columns:
        df["VIX_sma20"] = df["VIX"].rolling(20).mean()
        df["VIX_sma20_ratio"] = df["VIX"] / df["VIX_sma20"]
        df["VIX_roc_1d"] = df["VIX"].pct_change(1)
        df["VIX_roc_5d"] = df["VIX"].pct_change(5)
        df["VIX_level_quintile"] = pd.qcut(
            df["VIX"], 5, labels=[1, 2, 3, 4, 5], duplicates="drop"
        ).astype(float)
        df["VIX_above_25"] = (df["VIX"] > 25).astype(int)
        df["VIX_above_30"] = (df["VIX"] > 30).astype(int)

    # Gold/Copper ratio (risk sentiment)
    if "gold" in df.columns and "copper" in df.columns:
        df["gold_copper_ratio"] = df["gold"] / df["copper"]
        df["gold_copper_roc_5d"] = df["gold_copper_ratio"].pct_change(5)

    # Rate of change features
    for col, name in [
        ("USD_index", "USD_roc_5d"),
        ("crude_oil", "oil_roc_5d"),
        ("10Y_yield", "10Y_yield_roc_5d"),
    ]:
        if col in df.columns:
            df[name] = df[col].pct_change(5)

    # Yield curve momentum
    if "yield_curve_10y_3m" in df.columns:
        df["yield_curve_roc_5d"] = df["yield_curve_10y_3m"].diff(5)
        df["yield_curve_regime"] = (df["yield_curve_10y_3m"] < 0).astype(int)

    print(f"Feature matrix: {len(df)} rows × {len(df.columns)} columns")
    print(f"Date range: {df.index[0].strftime('%Y-%m-%d')} → {df.index[-1].strftime('%Y-%m-%d')}")

    return df


def correlation_analysis(df):
    """Compute correlations between features and forward SP500 returns."""
    # Feature columns (exclude SP500 price, returns, and forward returns)
    fwd_cols = [c for c in df.columns if "SP500_fwd_" in c]
    exclude = ["SP500", "SP500_ret_1d", "SP500_ret_5d", "VIX_sma20"] + fwd_cols
    feature_cols = [c for c in df.columns if c not in exclude and df[c].dtype in [np.float64, np.int64, float, int]]

    results = {}
    for horizon in fwd_cols:
        corrs = {}
        for feat in feature_cols:
            valid = df[[feat, horizon]].dropna()
            if len(valid) > 100:
                corr = valid[feat].corr(valid[horizon])
                corrs[feat] = round(corr, 4)
        results[horizon] = dict(sorted(corrs.items(), key=lambda x: abs(x[1]), reverse=True))

    return results


def regime_analysis(df):
    """Analyze SP500 returns conditioned on macro regimes."""
    results = {}

    # 1. VIX quintile regime
    if "VIX_level_quintile" in df.columns and "SP500_fwd_5d" in df.columns:
        vix_regimes = {}
        for q in [1, 2, 3, 4, 5]:
            mask = df["VIX_level_quintile"] == q
            subset = df.loc[mask, "SP500_fwd_5d"].dropna()
            if len(subset) > 50:
                vix_regimes[f"Q{int(q)} (VIX {'low' if q<=2 else 'mid' if q==3 else 'high'})"] = {
                    "mean_ret": round(subset.mean() * 100, 3),
                    "median_ret": round(subset.median() * 100, 3),
                    "sharpe": round(subset.mean() / subset.std() * np.sqrt(52) if subset.std() > 0 else 0, 2),
                    "win_rate": round((subset > 0).mean() * 100, 1),
                    "n_obs": len(subset),
                }
        results["VIX quintile → 5d fwd return"] = vix_regimes

    # 2. Yield curve regime (inverted vs normal)
    if "yield_curve_regime" in df.columns and "SP500_fwd_10d" in df.columns:
        yc_regimes = {}
        for regime, label in [(0, "Normal (positive slope)"), (1, "Inverted (negative slope)")]:
            mask = df["yield_curve_regime"] == regime
            subset = df.loc[mask, "SP500_fwd_10d"].dropna()
            if len(subset) > 50:
                yc_regimes[label] = {
                    "mean_ret": round(subset.mean() * 100, 3),
                    "median_ret": round(subset.median() * 100, 3),
                    "sharpe": round(subset.mean() / subset.std() * np.sqrt(26) if subset.std() > 0 else 0, 2),
                    "win_rate": round((subset > 0).mean() * 100, 1),
                    "n_obs": len(subset),
                }
        results["Yield curve regime → 10d fwd return"] = yc_regimes

    # 3. VIX spike regime (VIX > 1.2× its 20-day SMA)
    if "VIX_sma20_ratio" in df.columns and "SP500_fwd_5d" in df.columns:
        spike_regimes = {}
        for label, lo, hi in [
            ("VIX calm (<0.9× SMA)", 0, 0.9),
            ("VIX normal (0.9-1.1× SMA)", 0.9, 1.1),
            ("VIX elevated (1.1-1.3× SMA)", 1.1, 1.3),
            ("VIX spike (>1.3× SMA)", 1.3, 100),
        ]:
            mask = (df["VIX_sma20_ratio"] >= lo) & (df["VIX_sma20_ratio"] < hi)
            subset = df.loc[mask, "SP500_fwd_5d"].dropna()
            if len(subset) > 30:
                spike_regimes[label] = {
                    "mean_ret": round(subset.mean() * 100, 3),
                    "median_ret": round(subset.median() * 100, 3),
                    "sharpe": round(subset.mean() / subset.std() * np.sqrt(52) if subset.std() > 0 else 0, 2),
                    "win_rate": round((subset > 0).mean() * 100, 1),
                    "n_obs": len(subset),
                }
        results["VIX spike regime → 5d fwd return"] = spike_regimes

    # 4. Gold/copper ratio regime
    if "gold_copper_ratio" in df.columns and "SP500_fwd_10d" in df.columns:
        gc = df["gold_copper_ratio"].dropna()
        if len(gc) > 100:
            q33, q67 = gc.quantile(0.33), gc.quantile(0.67)
            gc_regimes = {}
            for label, lo, hi in [
                ("Risk-on (low gold/copper)", gc.min(), q33),
                ("Neutral", q33, q67),
                ("Risk-off (high gold/copper)", q67, gc.max() + 1),
            ]:
                mask = (df["gold_copper_ratio"] >= lo) & (df["gold_copper_ratio"] < hi)
                subset = df.loc[mask, "SP500_fwd_10d"].dropna()
                if len(subset) > 50:
                    gc_regimes[label] = {
                        "mean_ret": round(subset.mean() * 100, 3),
                        "median_ret": round(subset.median() * 100, 3),
                        "win_rate": round((subset > 0).mean() * 100, 1),
                        "n_obs": len(subset),
                    }
            results["Gold/copper regime → 10d fwd return"] = gc_regimes

    return results


def directional_signal_test(df):
    """Test if macro features predict SP500 direction (up/down)."""
    results = {}

    feature_signals = {
        "VIX_roc_1d": {
            "desc": "VIX overnight spike → next-day SP500",
            "threshold_col": "VIX_roc_1d",
            "thresholds": [0.05, 0.10, 0.15, 0.20],
            "target": "SP500_fwd_1d",
            "direction": "spike",  # above threshold
        },
        "VIX_roc_5d": {
            "desc": "VIX 5-day spike → 5d fwd SP500",
            "threshold_col": "VIX_roc_5d",
            "thresholds": [0.10, 0.20, 0.30, 0.50],
            "target": "SP500_fwd_5d",
            "direction": "spike",
        },
        "yield_curve_roc_5d": {
            "desc": "Yield curve flattening → 10d fwd SP500",
            "threshold_col": "yield_curve_roc_5d",
            "thresholds": [-0.20, -0.15, -0.10, -0.05],
            "target": "SP500_fwd_10d",
            "direction": "drop",  # below threshold
        },
        "USD_roc_5d": {
            "desc": "Dollar strength → 5d fwd SP500",
            "threshold_col": "USD_roc_5d",
            "thresholds": [0.01, 0.02, 0.03],
            "target": "SP500_fwd_5d",
            "direction": "spike",
        },
        "oil_roc_5d": {
            "desc": "Oil spike → 5d fwd SP500",
            "threshold_col": "oil_roc_5d",
            "thresholds": [0.05, 0.10, 0.15],
            "target": "SP500_fwd_5d",
            "direction": "spike",
        },
    }

    for signal_name, cfg in feature_signals.items():
        col = cfg["threshold_col"]
        target = cfg["target"]
        if col not in df.columns or target not in df.columns:
            continue

        signal_results = {"description": cfg["desc"], "tests": []}
        for thresh in cfg["thresholds"]:
            if cfg["direction"] == "spike":
                mask = df[col] > thresh
            else:
                mask = df[col] < thresh

            triggered = df.loc[mask, target].dropna()
            baseline = df[target].dropna()

            if len(triggered) >= 20:
                signal_results["tests"].append({
                    "threshold": thresh,
                    "n_triggered": len(triggered),
                    "triggered_mean": round(triggered.mean() * 100, 3),
                    "baseline_mean": round(baseline.mean() * 100, 3),
                    "triggered_median": round(triggered.median() * 100, 3),
                    "triggered_win_rate": round((triggered > 0).mean() * 100, 1),
                    "baseline_win_rate": round((baseline > 0).mean() * 100, 1),
                    "edge": round((triggered.mean() - baseline.mean()) * 100, 3),
                })

        if signal_results["tests"]:
            results[signal_name] = signal_results

    return results


def generate_report(df, corr_results, regime_results, signal_results):
    """Generate markdown research report."""
    lines = []
    lines.append("# FRED / Macro Features Research Report")
    lines.append("")
    lines.append(f"**Generated:** {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    lines.append(f"**Data range:** {df.index[0].strftime('%Y-%m-%d')} → {df.index[-1].strftime('%Y-%m-%d')}")
    lines.append(f"**Observations:** {len(df):,}")
    lines.append("")
    lines.append("---")
    lines.append("")

    # ── Section 1: Correlation Rankings ──
    lines.append("## 1. Feature–Return Correlations")
    lines.append("")
    lines.append("Top features by absolute correlation with forward SP500 returns.")
    lines.append("Correlations > |0.03| are potentially tradeable at daily frequency.")
    lines.append("")

    for horizon, corrs in corr_results.items():
        label = horizon.replace("SP500_fwd_", "")
        lines.append(f"### {label} Forward Return")
        lines.append("")
        lines.append("| Feature | Correlation | |")
        lines.append("|---------|------------|--|")
        for feat, corr in list(corrs.items())[:15]:
            bar = "█" * int(abs(corr) * 100)
            sign = "+" if corr > 0 else "−"
            lines.append(f"| {feat} | {sign}{abs(corr):.4f} | {bar} |")
        lines.append("")

    # ── Section 2: Regime Analysis ──
    lines.append("## 2. Regime Analysis")
    lines.append("")
    lines.append("SP500 forward returns conditioned on macro regimes.")
    lines.append("")

    for regime_name, regimes in regime_results.items():
        lines.append(f"### {regime_name}")
        lines.append("")
        lines.append("| Regime | Mean Ret % | Median % | Win Rate | Sharpe | N |")
        lines.append("|--------|-----------|----------|----------|--------|---|")
        for label, stats in regimes.items():
            sharpe = stats.get("sharpe", "—")
            lines.append(
                f"| {label} | {stats['mean_ret']:+.3f} | "
                f"{stats['median_ret']:+.3f} | {stats['win_rate']:.1f}% | "
                f"{sharpe} | {stats['n_obs']} |"
            )
        lines.append("")

    # ── Section 3: Directional Signal Tests ──
    lines.append("## 3. Directional Signal Tests")
    lines.append("")
    lines.append("Testing if extreme macro moves predict SP500 direction.")
    lines.append("")

    for signal_name, signal in signal_results.items():
        lines.append(f"### {signal['description']}")
        lines.append("")
        lines.append("| Threshold | N | Trig Mean % | Base Mean % | Edge % | Trig WR | Base WR |")
        lines.append("|-----------|---|------------|------------|--------|---------|---------|")
        for test in signal["tests"]:
            lines.append(
                f"| {test['threshold']:+.2f} | {test['n_triggered']} | "
                f"{test['triggered_mean']:+.3f} | {test['baseline_mean']:+.3f} | "
                f"{test['edge']:+.3f} | {test['triggered_win_rate']:.1f}% | "
                f"{test['baseline_win_rate']:.1f}% |"
            )
        lines.append("")

    # ── Section 4: Recommendations ──
    lines.append("## 4. Top Candidate Features for Atlas Integration")
    lines.append("")
    lines.append("Based on correlation, regime, and signal analysis:")
    lines.append("")

    # Find features with highest absolute correlation across horizons
    top_features = {}
    for horizon, corrs in corr_results.items():
        for feat, corr in corrs.items():
            if feat not in top_features or abs(corr) > abs(top_features[feat]):
                top_features[feat] = corr
    
    sorted_features = sorted(top_features.items(), key=lambda x: abs(x[1]), reverse=True)

    lines.append("### Ranked by peak correlation")
    lines.append("")
    lines.append("| Rank | Feature | Peak |r| | Assessment |")
    lines.append("|------|---------|---------|------------|")
    for i, (feat, corr) in enumerate(sorted_features[:10], 1):
        assessment = "🟢 Strong" if abs(corr) > 0.05 else "🟡 Moderate" if abs(corr) > 0.03 else "🔴 Weak"
        lines.append(f"| {i} | {feat} | {abs(corr):.4f} | {assessment} |")
    lines.append("")

    # ── Section 5: Implementation Notes ──
    lines.append("## 5. Implementation Notes")
    lines.append("")
    lines.append("### Data Sources (no FRED API key needed)")
    lines.append("All features above are available via yfinance:")
    lines.append("- Treasury yields: `^TNX`, `^FVX`, `^IRX`, `^TYX`")
    lines.append("- VIX: `^VIX`")
    lines.append("- Dollar index: `DX-Y.NYB`")
    lines.append("- Commodities: `GC=F` (gold), `CL=F` (oil), `HG=F` (copper)")
    lines.append("")
    lines.append("### FRED API Extension (requires free API key)")
    lines.append("Additional series worth testing with FRED API:")
    lines.append("- `FEDFUNDS` — Fed Funds effective rate (daily)")
    lines.append("- `T10Y2Y` — 10Y-2Y spread (pre-computed)")
    lines.append("- `BAMLH0A0HYM2` — High-yield OAS (credit stress)")
    lines.append("- `UMCSENT` — Consumer sentiment (monthly)")
    lines.append("- `ICSA` — Initial jobless claims (weekly)")
    lines.append("- `DTWEXBGS` — Trade-weighted dollar (daily)")
    lines.append("")
    lines.append("### Integration Path")
    lines.append("1. Add daily macro data download to `data/ingest.py`")
    lines.append("2. Store in `data/macro/` as parquet files")
    lines.append("3. Expose as strategy features via config")
    lines.append("4. Backtest with macro features as entry/exit filters")
    lines.append("5. Most promising: regime-based position sizing")
    lines.append("")

    report = "\n".join(lines)
    REPORT_PATH.write_text(report)
    print(f"\n📄 Report written to {REPORT_PATH}")
    return report


def main():
    print("=" * 60)
    print("  FRED / Macro Features Research for SP500")
    print("=" * 60)
    print()

    # Step 1: Download data
    sp500, macro_data = download_data()
    print(f"\n✅ Downloaded SP500 + {len(macro_data)} macro series")

    # Step 2: Build feature matrix
    df = build_feature_matrix(sp500, macro_data)
    print(f"✅ Built {len([c for c in df.columns if 'SP500_fwd' not in c and c != 'SP500'])} features")

    # Step 3: Correlation analysis
    print("\n── Correlation Analysis ──")
    corr_results = correlation_analysis(df)
    for horizon, corrs in corr_results.items():
        label = horizon.replace("SP500_fwd_", "")
        top3 = list(corrs.items())[:3]
        print(f"  {label}: top = {', '.join(f'{f} ({c:+.4f})' for f, c in top3)}")

    # Step 4: Regime analysis
    print("\n── Regime Analysis ──")
    regime_results = regime_analysis(df)
    for name, regimes in regime_results.items():
        print(f"  {name}:")
        for label, stats in regimes.items():
            print(f"    {label:40} mean={stats['mean_ret']:+.3f}%  WR={stats['win_rate']:.1f}%")

    # Step 5: Directional signal tests
    print("\n── Signal Tests ──")
    signal_results = directional_signal_test(df)
    for name, signal in signal_results.items():
        best = max(signal["tests"], key=lambda t: abs(t["edge"]))
        print(f"  {signal['description']}")
        print(f"    Best edge: {best['edge']:+.3f}% at threshold {best['threshold']}")

    # Step 6: Generate report
    print("\n── Generating Report ──")
    generate_report(df, corr_results, regime_results, signal_results)

    print("\n✅ Research complete!")


if __name__ == "__main__":
    main()
