# Walk-Forward Fragility Analysis

## Symptom
v2.2 validated Mar 2 with Sharpe 0.983, 299 trades. Same code on Mar 5 gives Sharpe -1.85, 95 trades.

## Root Causes

### 1. Adjusted price drift (PRIMARY)
yfinance `adj_close` changes retroactively when dividends are paid. Example drifts:
- PFE: 5.0% over 9 months
- XOM: 2.4%
- JPM: 1.4%
- Mean across sample: ~2%

This means ALL historical indicator values (RSI, SMA, z-score) shift every time we re-download data. A 2% price drift can flip a signal from triggering to not triggering (e.g., RSI goes from 34 → 36, crossing the rsi_oversold=35 threshold).

With 284 tickers × 500 test days × 2% average drift, hundreds of signals flip.

### 2. Walk-forward window alignment (SECONDARY)
Data start shifted Feb 27 → Mar 6 (5 days). With step=21, all window boundaries moved by 5 days. Signals near window boundaries flip in/out.

### 3. No data pinning (ENABLER)
Data cache is updated incrementally. Each refresh changes:
- End date (new days added)
- Start date (yfinance returns fixed-width history)
- ALL historical adjusted prices (dividend adjustments)

No snapshot was saved at validation time, making reproduction impossible.

## Fixes

### Fix A: Pin data snapshots at validation time
Save a complete data snapshot when promoting a config version. Use this pinned snapshot for all subsequent backtests until the next re-validation.

### Fix B: Use unadjusted OHLC + explicit adjustment
Store raw (unadjusted) prices. Apply split adjustments manually (easy, infrequent). Ignore dividend adjustments for backtesting — they create noise, not signal. This eliminates the retroactive drift problem entirely.

### Fix C: Multi-offset walk-forward (robustness check)
During OOS validation, run the walk-forward with 5+ different start offsets (0, 5, 10, 15, 20 days). Report the MEDIAN Sharpe across offsets. If variance is high (>30%), the strategy is fragile to window alignment — do not promote.

### Fix D: Stability test in OOS validation
Add a "data stability" test: re-download fresh data, run backtest, compare to pinned data result. If trade count changes >20% or Sharpe changes >0.3, flag as unstable.

### Fix E: Regression test on every strategy change
tests/test_baseline_regression.py — already created. Run before any research experiment or after any strategy code change.

## Recommendation
Implement Fix A (data pinning) immediately — it's the simplest and most impactful.
Implement Fix C (multi-offset) as part of the OOS validation pipeline.
Consider Fix B long-term for maximum reproducibility.
