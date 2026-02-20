#!/usr/bin/env python3
"""Atlas-ASX DividendCapture Strategy Validation"""
import sys, json, os, logging
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path("/a0/usr/projects/atlas-asx")
sys.path.insert(0, str(PROJECT_ROOT))
os.chdir(str(PROJECT_ROOT))

import pandas as pd
import numpy as np

from utils.config import get_active_config
from utils.dividends import (
    fetch_dividend_calendar, estimate_franking_pct,
    calc_grossed_up_yield, get_sector_for_ticker,
)
from strategies.dividend_capture import DividendCapture
from backtest.engine import BacktestEngine

logging.basicConfig(level=logging.WARNING,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("div_validation")
logger.setLevel(logging.INFO)

TEST_TICKERS = [
    "BHP.AX", "CBA.AX", "NAB.AX", "WBC.AX", "WES.AX",
    "TLS.AX", "FMG.AX", "RIO.AX", "WOW.AX", "ANZ.AX",
]

def load_ticker_data(tickers):
    data = {}
    cache_dir = PROJECT_ROOT / "data" / "cache"
    for ticker in tickers:
        safe = ticker.replace(".", "_")
        path = cache_dir / f"{safe}.parquet"
        if path.exists():
            df = pd.read_parquet(path)
            df.columns = [c.lower() for c in df.columns]
            if len(df) > 0:
                data[ticker] = df
    return data

def test_dividend_data(tickers):
    print("\n" + "=" * 65)
    print("  TEST 1: Dividend Data Quality")
    print("=" * 65)
    results = []
    for ticker in tickers:
        divs = fetch_dividend_calendar(ticker)
        sector = get_sector_for_ticker(ticker)
        franking = estimate_franking_pct(ticker, sector)
        recent = [d for d in divs if pd.Timestamp(d["ex_date"]) >= pd.Timestamp("2021-01-01")]
        avg_div = np.mean([d["amount"] for d in recent]) if recent else 0
        results.append({
            "ticker": ticker, "sector": (sector[:20] if sector else "unknown"),
            "total": len(divs), "recent": len(recent),
            "avg_div": avg_div, "franking": franking,
        })
    hdr = f"  {'Ticker':<10} {'Sector':<22} {'Total':>6} {'Recent':>7} {'AvgDiv':>9} {'Frank':>6}"
    print(hdr)
    print("  " + "-" * 62)
    for r in results:
        print(f"  {r['ticker']:<10} {r['sector']:<22} {r['total']:>6} "
              f"{r['recent']:>7} ${r['avg_div']:>7.4f} {r['franking']*100:>5.0f}%")
    valid = sum(1 for r in results if r["recent"] >= 4)
    print(f"\n  PASS: {valid}/{len(tickers)} tickers have >= 4 recent dividends")
    return results


def test_signal_generation(config, data):
    print("\n" + "=" * 65)
    print("  TEST 2: Signal Generation")
    print("=" * 65)
    strat = DividendCapture(config)
    total_signals = 0
    signal_details = []
    for ticker, df in data.items():
        divs = fetch_dividend_calendar(ticker)
        recent_divs = [d for d in divs
                       if df.index[60].strftime("%Y-%m-%d") <= d["ex_date"] <= df.index[-1].strftime("%Y-%m-%d")]
        ticker_sigs = 0
        for div in recent_divs:
            ex_date = pd.Timestamp(div["ex_date"])
            window_end = ex_date - pd.Timedelta(days=1)
            mask = df.index <= window_end
            if mask.sum() < 60:
                continue
            test_df = df[mask].copy()
            try:
                signals = strat.generate_signals({ticker: test_df}, 100000.0, [])
                if signals:
                    ticker_sigs += len(signals)
                    for s in signals:
                        signal_details.append({
                            "ticker": ticker, "ex_date": div["ex_date"],
                            "div": div["amount"], "entry": s.entry_price,
                            "stop": s.stop_price, "conf": s.confidence,
                            "gu_yield": s.features.get("grossed_up_yield", 0),
                        })
            except Exception as e:
                logger.warning("%s: %s", ticker, e)
        total_signals += ticker_sigs
        if ticker_sigs > 0:
            print(f"  {ticker}: {ticker_sigs} signals from {len(recent_divs)} div events")
    print(f"\n  Total signals: {total_signals}")
    if signal_details:
        print(f"\n  {'Ticker':<10} {'ExDate':<12} {'Div$':>8} {'Entry$':>9} {'Stop$':>9} {'Conf':>6} {'GU%':>7}")
        print("  " + "-" * 65)
        for s in signal_details[:15]:
            print(f"  {s['ticker']:<10} {s['ex_date']:<12} ${s['div']:>7.4f} "
                  f"${s['entry']:>8.2f} ${s['stop']:>8.2f} {s['conf']:>5.2f} "
                  f"{s['gu_yield']*100:>6.2f}%")
    return signal_details


def test_exdate_price_behavior(data):
    print("\n" + "=" * 65)
    print("  TEST 3: Ex-Date Price Behavior Analysis")
    print("=" * 65)
    results = []
    for ticker, df in data.items():
        divs = fetch_dividend_calendar(ticker)
        sector = get_sector_for_ticker(ticker)
        franking = estimate_franking_pct(ticker, sector)
        recent = [d for d in divs if pd.Timestamp(d["ex_date"]) >= pd.Timestamp("2021-01-01")]
        for div in recent:
            ex_dt = pd.Timestamp(div["ex_date"])
            try:
                ex_idx = df.index.get_indexer([ex_dt], method="nearest")[0]
            except Exception:
                continue
            if ex_idx < 5 or ex_idx >= len(df) - 5:
                continue
            pre5 = float(df["close"].iloc[ex_idx - 5])
            pre1 = float(df["close"].iloc[ex_idx - 1])
            on_ex = float(df["close"].iloc[ex_idx])
            post5 = float(df["close"].iloc[min(ex_idx + 5, len(df) - 1)])
            div_amt = div["amount"]
            drop_pct = (pre1 - on_ex) / pre1 * 100 if pre1 > 0 else 0
            div_pct = div_amt / pre1 * 100 if pre1 > 0 else 0
            gu_yield = calc_grossed_up_yield(div_amt, pre1, franking)
            recovery_5d = (post5 - on_ex) / on_ex * 100 if on_ex > 0 else 0
            total_return = (post5 - pre5 + div_amt) / pre5 * 100 if pre5 > 0 else 0
            results.append({
                "ticker": ticker, "ex_date": div["ex_date"],
                "div_pct": div_pct, "drop_pct": drop_pct,
                "gu_yield": gu_yield * 100, "recovery_5d": recovery_5d,
                "total_ret": total_return, "franking": franking,
            })
    if results:
        rdf = pd.DataFrame(results)
        print(f"\n  Analyzed {len(rdf)} ex-dividend events across {len(data)} tickers")
        print(f"\n  Average metrics:")
        print(f"    Dividend yield at ex:    {rdf['div_pct'].mean():>+.2f}%")
        print(f"    Price drop on ex-date:   {rdf['drop_pct'].mean():>+.2f}%")
        print(f"    Grossed-up yield:        {rdf['gu_yield'].mean():>+.2f}%")
        print(f"    5-day post-ex recovery:  {rdf['recovery_5d'].mean():>+.2f}%")
        print(f"    Total return (5d+5d+div):{rdf['total_ret'].mean():>+.2f}%")
        drop_ratio = rdf['drop_pct'].mean() / rdf['div_pct'].mean() if rdf['div_pct'].mean() != 0 else 0
        print(f"\n  Drop/Dividend ratio: {drop_ratio:.2f}x")
        print(f"  (< 1.0 means stocks drop LESS than dividend = alpha opportunity)")
        franked = rdf[rdf["franking"] >= 0.75]
        if len(franked) > 0:
            print(f"\n  Fully-franked subset ({len(franked)} events):")
            print(f"    Avg drop/div ratio: {franked['drop_pct'].mean() / franked['div_pct'].mean():.2f}x")
            print(f"    Avg total return:   {franked['total_ret'].mean():>+.2f}%")
    else:
        print("  No ex-dividend events found in data")
    return results

def test_walkforward(config, data):
    print("\n" + "=" * 65)
    print("  TEST 4: Walk-Forward Backtest (DividendCapture only)")
    print("=" * 65)
    import copy
    test_cfg = copy.deepcopy(config)
    for sname in test_cfg["strategies"]:
        test_cfg["strategies"][sname]["enabled"] = False
    test_cfg["strategies"]["dividend_capture"]["enabled"] = True
    engine = BacktestEngine(test_cfg)
    strategies = [DividendCapture(test_cfg)]
    print(f"  Running on {len(data)} tickers...")
    print(f"  Train: {test_cfg['backtest']['train_window_days']}d, "
          f"Test: {test_cfg['backtest']['test_window_days']}d, "
          f"Step: {test_cfg['backtest']['step_days']}d")
    result = engine.run_walkforward(data, strategies)
    metrics = result.metrics if hasattr(result, "metrics") else result.get("metrics", {})
    trades = result.trades if hasattr(result, "trades") else result.get("trades", [])
    bench = result.benchmark_metrics if hasattr(result, "benchmark_metrics") else result.get("benchmark_metrics", {})
    print(f"\n  Strategy Performance:")
    print(f"    CAGR:            {metrics.get('cagr', 0)*100:>+.2f}%")
    print(f"    Max Drawdown:    {metrics.get('max_drawdown', 0)*100:>.2f}%")
    print(f"    Sharpe Ratio:    {metrics.get('sharpe', 0):>.3f}")
    print(f"    Sortino Ratio:   {metrics.get('sortino', 0):>.3f}")
    print(f"    Win Rate:        {metrics.get('win_rate', 0)*100:>.1f}%")
    print(f"    Profit Factor:   {metrics.get('profit_factor', 0):>.2f}")
    print(f"    Total Trades:    {metrics.get('total_trades', len(trades))}")
    print(f"    Avg Trade:       ${metrics.get('avg_trade', 0):>.2f}")
    if bench:
        print(f"\n  Benchmark:")
        print(f"    CAGR:            {bench.get('cagr', 0)*100:>+.2f}%")
        print(f"    Max Drawdown:    {bench.get('max_drawdown', 0)*100:>.2f}%")
    if trades:
        pnls = []
        for t in trades:
            pnl = t.get("pnl", t.get("realized_pnl", 0))
            if isinstance(pnl, (int, float)):
                pnls.append(pnl)
        if pnls:
            wins = [p for p in pnls if p > 0]
            losses = [p for p in pnls if p <= 0]
            print(f"\n  Trade Breakdown:")
            print(f"    Winners: {len(wins)}, Losers: {len(losses)}")
            if wins:
                print(f"    Avg Win:   ${np.mean(wins):>.2f}")
            if losses:
                print(f"    Avg Loss:  ${np.mean(losses):>.2f}")
            print(f"    Total P&L: ${sum(pnls):>.2f}")
    return metrics, trades


def test_config_sweep(config, data):
    print("\n" + "=" * 65)
    print("  TEST 5: Parameter Sensitivity Sweep")
    print("=" * 65)
    import copy
    configs = [
        {"label": "Baseline",       "days_before": 5, "days_after": 5, "min_frank": 75, "min_gu": 1.5, "atr_mult": 3.0},
        {"label": "Early Entry",    "days_before": 7, "days_after": 5, "min_frank": 75, "min_gu": 1.5, "atr_mult": 3.0},
        {"label": "Late Entry",     "days_before": 3, "days_after": 5, "min_frank": 75, "min_gu": 1.5, "atr_mult": 3.0},
        {"label": "Quick Exit",     "days_before": 5, "days_after": 3, "min_frank": 75, "min_gu": 1.5, "atr_mult": 3.0},
        {"label": "Slow Exit",      "days_before": 5, "days_after": 8, "min_frank": 75, "min_gu": 1.5, "atr_mult": 3.0},
        {"label": "Low Yield",      "days_before": 5, "days_after": 5, "min_frank": 50, "min_gu": 1.0, "atr_mult": 3.0},
        {"label": "Tight Stop",     "days_before": 5, "days_after": 5, "min_frank": 75, "min_gu": 1.5, "atr_mult": 2.0},
        {"label": "Wide Stop",      "days_before": 5, "days_after": 5, "min_frank": 75, "min_gu": 1.5, "atr_mult": 4.0},
    ]
    results = []
    for cfg in configs:
        tc = copy.deepcopy(config)
        for sname in tc["strategies"]:
            tc["strategies"][sname]["enabled"] = False
        tc["strategies"]["dividend_capture"]["enabled"] = True
        tc["strategies"]["dividend_capture"]["days_before_ex"] = cfg["days_before"]
        tc["strategies"]["dividend_capture"]["days_after_ex"] = cfg["days_after"]
        tc["strategies"]["dividend_capture"]["min_franking_pct"] = cfg["min_frank"]
        tc["strategies"]["dividend_capture"]["min_grossed_up_yield"] = cfg["min_gu"]
        tc["strategies"]["dividend_capture"]["atr_stop_mult"] = cfg["atr_mult"]
        engine = BacktestEngine(tc)
        strats = [DividendCapture(tc)]
        try:
            result = engine.run_walkforward(data, strats)
            m = result.metrics if hasattr(result, "metrics") else result.get("metrics", {})
            t = result.trades if hasattr(result, "trades") else result.get("trades", [])
            results.append({
                "label": cfg["label"],
                "cagr": m.get("cagr", 0) * 100,
                "mdd": m.get("max_drawdown", 0) * 100,
                "sharpe": m.get("sharpe", 0),
                "win_rate": m.get("win_rate", 0) * 100,
                "pf": m.get("profit_factor", 0),
                "trades": m.get("total_trades", len(t)),
            })
            print(f"  {cfg['label']:<16} done: {results[-1]['trades']} trades")
        except Exception as e:
            print(f"  {cfg['label']:<16} ERROR: {e}")
            results.append({"label": cfg["label"], "cagr": 0, "mdd": 0, "sharpe": 0, "win_rate": 0, "pf": 0, "trades": 0})
    print(f"\n  {'Config':<16} {'CAGR':>8} {'MaxDD':>8} {'Sharpe':>8} {'WinR%':>8} {'PF':>8} {'#Trades':>8}")
    print("  " + "-" * 62)
    for r in results:
        print(f"  {r['label']:<16} {r['cagr']:>+7.2f}% {r['mdd']:>7.2f}% {r['sharpe']:>7.3f} "
              f"{r['win_rate']:>7.1f}% {r['pf']:>7.2f} {r['trades']:>8}")
    return results


def main():
    print("\n" + "#" * 65)
    print("  ATLAS-ASX: DividendCapture Strategy Validation")
    print("  " + datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    print("#" * 65)

    config = get_active_config()
    print(f"\n  Loading data for {len(TEST_TICKERS)} test tickers...")
    data = load_ticker_data(TEST_TICKERS)
    print(f"  Loaded {len(data)}/{len(TEST_TICKERS)} tickers")
    for t, df in data.items():
        print(f"    {t}: {len(df)} bars ({df.index[0].strftime('%Y-%m-%d')} to {df.index[-1].strftime('%Y-%m-%d')})")

    # Run all tests
    test_dividend_data(TEST_TICKERS)
    test_signal_generation(config, data)
    test_exdate_price_behavior(data)
    test_walkforward(config, data)
    test_config_sweep(config, data)

    # Save results
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = f"backtest/results/dividend_capture_validation_{ts}.json"
    print(f"\n  Validation complete. Output: {out_path}")
    print("\n" + "#" * 65)


if __name__ == "__main__":
    main()
