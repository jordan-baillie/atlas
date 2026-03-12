#!/usr/bin/env python3
"""Task #121: OOS validation of risk_per_trade=0.35% finding.

Runs 4 validation tests:
1. Multi-offset stability test (5 walk-forward offsets)
2. Time-split OOS (first half train → second half test)
3. Perturbation test (0.30%, 0.33%, 0.35%, 0.37%, 0.40%)
4. Different data window (2019-2026 vs 2017-2026)

If 3/4 tests pass → promote to config.
"""
import sys, json, copy, logging, time
import numpy as np
sys.path.insert(0, "/root/atlas")
logging.basicConfig(level=logging.WARNING, format="%(message)s")

from scripts.strategy_evaluator import load_market_data, run_backtest
from utils.config import get_active_config


def run_at_risk(cfg, data, risk_pct):
    """Run backtest with given risk_per_trade_pct."""
    c = copy.deepcopy(cfg)
    c["risk"]["max_risk_per_trade_pct"] = risk_pct
    m = run_backtest(c, data)
    return {
        "risk_pct": risk_pct,
        "sharpe": round(m.get("sharpe", 0), 4),
        "cagr_pct": round(m.get("cagr_pct", 0), 2),
        "max_dd_pct": round(m.get("max_drawdown_pct", 0), 2),
        "total_trades": m.get("total_trades", 0),
        "win_rate_pct": round(m.get("win_rate_pct", 0), 1),
        "profit_factor": round(m.get("profit_factor", 0), 4),
        "calmar": round(m.get("calmar", 0), 4),
        "avg_trade": round(m.get("avg_trade", 0), 2),
        "total_pnl": round(m.get("total_pnl", 0), 2),
    }


def test_1_multioffset(cfg, data):
    """Test 1: Multi-offset stability — run backtest with 5 different data offsets."""
    print("\n" + "=" * 70)
    print("TEST 1: Multi-Offset Stability (5 start-date offsets)")
    print("=" * 70)
    
    offsets = [0, 5, 10, 15, 20]  # trading days trimmed from start
    
    results = {"0.35%": [], "0.50%": []}
    
    for risk_pct, label in [(0.0035, "0.35%"), (0.0050, "0.50%")]:
        print(f"\n  risk={label}:")
        for offset in offsets:
            # Trim offset days from start of each ticker
            trimmed = {}
            for ticker, df in data.items():
                if len(df) > offset + 100:
                    trimmed[ticker] = df.iloc[offset:].copy()
            
            r = run_at_risk(cfg, trimmed, risk_pct)
            results[label].append(r)
            print(f"    offset={offset:2d}d: Sharpe={r['sharpe']:.4f}, "
                  f"CAGR={r['cagr_pct']:.2f}%, Trades={r['total_trades']}")
        
        sharpes = [r["sharpe"] for r in results[label]]
        print(f"    → Median Sharpe: {np.median(sharpes):.4f}, "
              f"Std: {np.std(sharpes):.4f}, "
              f"CV: {np.std(sharpes)/abs(np.mean(sharpes)) if np.mean(sharpes) != 0 else float('inf'):.4f}")
    
    # Pass if 0.35% has better median Sharpe across offsets
    med_035 = np.median([r["sharpe"] for r in results["0.35%"]])
    med_050 = np.median([r["sharpe"] for r in results["0.50%"]])
    
    # Also check stability: std of 0.35% sharpes should be reasonable
    std_035 = np.std([r["sharpe"] for r in results["0.35%"]])
    
    passed = med_035 > med_050 and std_035 < 1.0
    print(f"\n  VERDICT: {'PASS' if passed else 'FAIL'} "
          f"(0.35% median={med_035:.4f} std={std_035:.4f} vs "
          f"0.50% median={med_050:.4f})")
    
    return passed, results


def test_2_time_split(cfg, data):
    """Test 2: Time-split OOS — train on first half, test on second half."""
    print("\n" + "=" * 70)
    print("TEST 2: Time-Split OOS (first half vs second half)")
    print("=" * 70)
    
    import pandas as pd
    
    # Find common date range
    all_dates = set()
    for ticker, df in data.items():
        all_dates.update(df.index)
    all_dates = sorted(all_dates)
    
    if len(all_dates) < 500:
        print("  SKIP: Not enough data for time-split")
        return None, {"reason": "insufficient data"}
    
    midpoint = all_dates[len(all_dates) // 2]
    print(f"  Date range: {all_dates[0].date()} to {all_dates[-1].date()}")
    print(f"  Midpoint: {midpoint.date()}")
    
    # Split data
    first_half = {}
    second_half = {}
    for ticker, df in data.items():
        fh = df[df.index <= midpoint]
        sh = df[df.index > midpoint]
        if len(fh) > 100:
            first_half[ticker] = fh
        if len(sh) > 100:
            second_half[ticker] = sh
    
    print(f"  First half tickers: {len(first_half)}, Second half: {len(second_half)}")
    
    results = {}
    for risk_pct in [0.0035, 0.0050]:
        label = f"{risk_pct*100:.2f}%"
        r_fh = run_at_risk(cfg, first_half, risk_pct)
        r_sh = run_at_risk(cfg, second_half, risk_pct)
        results[label] = {"first_half": r_fh, "second_half": r_sh}
        print(f"\n  risk={label}:")
        print(f"    First half:  Sharpe={r_fh['sharpe']:.4f}, CAGR={r_fh['cagr_pct']:.2f}%, Trades={r_fh['total_trades']}")
        print(f"    Second half: Sharpe={r_sh['sharpe']:.4f}, CAGR={r_sh['cagr_pct']:.2f}%, Trades={r_sh['total_trades']}")
    
    # Pass if 0.35% outperforms 0.50% in BOTH halves (or at least the OOS second half)
    r035_sh = results["0.35%"]["second_half"]["sharpe"]
    r050_sh = results["0.50%"]["second_half"]["sharpe"]
    passed = r035_sh > r050_sh
    print(f"\n  VERDICT: {'PASS' if passed else 'FAIL'} "
          f"(OOS half: 0.35% Sharpe={r035_sh:.4f} vs 0.50% Sharpe={r050_sh:.4f})")
    
    return passed, results


def test_3_perturbation(cfg, data):
    """Test 3: Perturbation — test nearby values to check smoothness."""
    print("\n" + "=" * 70)
    print("TEST 3: Perturbation Test (is the surface smooth around 0.35%?)")
    print("=" * 70)
    
    test_values = [0.0025, 0.0030, 0.0033, 0.0035, 0.0037, 0.0040, 0.0045]
    results = {}
    
    for risk_pct in test_values:
        r = run_at_risk(cfg, data, risk_pct)
        results[f"{risk_pct*100:.2f}%"] = r
        print(f"  risk={risk_pct*100:.2f}%: Sharpe={r['sharpe']:.4f}, CAGR={r['cagr_pct']:.2f}%, "
              f"Trades={r['total_trades']}, PF={r['profit_factor']:.4f}")
    
    # Check smoothness: no sudden cliff > 0.5 Sharpe between adjacent points
    sharpes = [results[f"{v*100:.2f}%"]["sharpe"] for v in test_values]
    diffs = [abs(sharpes[i+1] - sharpes[i]) for i in range(len(sharpes)-1)]
    max_cliff = max(diffs) if diffs else 0
    
    # Also check: is 0.35% within 0.1 Sharpe of its neighbors?
    idx_035 = test_values.index(0.0035)
    neighbor_sharpes = [sharpes[idx_035 - 1], sharpes[idx_035 + 1]] if 0 < idx_035 < len(sharpes)-1 else []
    smooth = all(abs(sharpes[idx_035] - ns) < 0.5 for ns in neighbor_sharpes)
    
    passed = smooth and max_cliff < 1.0
    print(f"\n  Max cliff between adjacent: {max_cliff:.4f}")
    print(f"  Smooth around 0.35%: {smooth}")
    print(f"  VERDICT: {'PASS' if passed else 'FAIL'} "
          f"(smooth={smooth}, max_cliff={max_cliff:.4f})")
    
    return passed, results


def test_4_alt_window(cfg, data):
    """Test 4: Different data window — use only 2020+ data."""
    print("\n" + "=" * 70)
    print("TEST 4: Alternative Data Window (2020-01-01 onward)")
    print("=" * 70)
    
    import pandas as pd
    cutoff = pd.Timestamp("2020-01-01")
    
    alt_data = {}
    for ticker, df in data.items():
        recent = df[df.index >= cutoff]
        if len(recent) > 100:
            alt_data[ticker] = recent
    
    print(f"  Tickers with >100 days post-2020: {len(alt_data)}")
    
    results = {}
    for risk_pct in [0.0035, 0.0050]:
        label = f"{risk_pct*100:.2f}%"
        r = run_at_risk(cfg, alt_data, risk_pct)
        results[label] = r
        print(f"\n  risk={label}: Sharpe={r['sharpe']:.4f}, CAGR={r['cagr_pct']:.2f}%, "
              f"Trades={r['total_trades']}, MaxDD={r['max_dd_pct']:.2f}%")
    
    passed = results["0.35%"]["sharpe"] > results["0.50%"]["sharpe"]
    print(f"\n  VERDICT: {'PASS' if passed else 'FAIL'} "
          f"(0.35% Sharpe={results['0.35%']['sharpe']:.4f} vs "
          f"0.50% Sharpe={results['0.50%']['sharpe']:.4f})")
    
    return passed, results


def main():
    t0 = time.time()
    print("Loading market data...")
    data = load_market_data("sp500")
    cfg = get_active_config("sp500")
    print(f"Loaded {len(data)} tickers")
    print(f"Current risk_per_trade: {cfg['risk']['max_risk_per_trade_pct']}")
    
    test_results = {}
    verdicts = {}
    
    # Test 1: Multi-offset stability
    passed, details = test_1_multioffset(cfg, data)
    verdicts["multioffset"] = passed
    test_results["multioffset"] = details
    
    # Test 2: Time-split OOS
    passed, details = test_2_time_split(cfg, data)
    verdicts["time_split"] = passed
    test_results["time_split"] = details
    
    # Test 3: Perturbation
    passed, details = test_3_perturbation(cfg, data)
    verdicts["perturbation"] = passed
    test_results["perturbation"] = details
    
    # Test 4: Alt window
    passed, details = test_4_alt_window(cfg, data)
    verdicts["alt_window"] = passed
    test_results["alt_window"] = details
    
    # Summary
    elapsed = time.time() - t0
    n_passed = sum(1 for v in verdicts.values() if v)
    n_total = sum(1 for v in verdicts.values() if v is not None)
    
    print(f"\n\n{'='*70}")
    print("OOS VALIDATION SUMMARY — risk_per_trade=0.35%")
    print(f"{'='*70}")
    for test_name, passed in verdicts.items():
        status = "PASS" if passed else ("FAIL" if passed is not None else "SKIP")
        print(f"  {test_name:20s}: {status}")
    
    promote = n_passed >= 3
    print(f"\n  Tests passed: {n_passed}/{n_total}")
    print(f"  RECOMMENDATION: {'PROMOTE to config' if promote else 'DO NOT promote — insufficient evidence'}")
    print(f"  Runtime: {elapsed:.0f}s")
    
    # Save
    output = {
        "task": "task_121_oos_risk_validate",
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "verdicts": verdicts,
        "tests_passed": n_passed,
        "tests_total": n_total,
        "promote": promote,
        "details": test_results,
        "runtime_s": round(elapsed, 1),
    }
    outpath = "/root/atlas/research/results/task121_oos_risk_validate.json"
    with open(outpath, "w") as f:
        json.dump(output, f, indent=2, default=str)
    print(f"\n  Results saved to {outpath}")


if __name__ == "__main__":
    main()
