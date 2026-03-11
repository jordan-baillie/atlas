# Backtest Speed Optimization Plan

## Status: PLANNING
## Created: 2026-03-11

---

## Problem Statement

A single backtest (mean_reversion, 50 tickers) takes **313s (5.2 min)**.
Autoresearch throughput is **6 experiments/hour**. This limits research velocity.

## Root Cause Analysis (cProfile on 20 tickers, 190s)

```
84M function calls in 190s

Hot path breakdown:
  _simulate_day (×1,491 days)         169.5s  (89% of total)
  ├── generate_signals (×1,422)         78.2s  (41%)  ← RECOMPUTES INDICATORS EVERY DAY
  │   ├── rolling().mean() (×57,944)    27.5s  (14%)  ← same rolling window, from scratch
  │   └── pandas .loc[] (×598,652)      84.7s  (45%)  ← boolean mask + copy every day
  ├── _process_strategy_exits           51.1s  (27%)  ← also rebuilds exit_data per day
  ├── market_breadth (one-time)         15.2s  ( 8%)
  └── isinstance() (×17.2M)            17.8s  ( 9%)  ← pandas type-checking overhead
```

### The fundamental problem

Every simulated day, the engine:
1. **Rebuilds `signal_data`** — `{ticker: df.loc[df.index <= yesterday]}` for all 50 tickers (boolean mask + DataFrame copy)
2. **Calls `generate_signals()`** — which recomputes `rolling(window).mean()`, `calc_rsi()`, `calc_zscore()`, `rolling(200).mean()` from scratch on the growing window
3. **Rebuilds `exit_data`** — same pattern for exit checks

For day 1000 with 50 tickers: it computes `rolling(14).mean()` on 1000 rows × 50 tickers. For day 1001: same thing on 1001 rows × 50 tickers. This is **O(days² × tickers)** when it should be **O(days × tickers)** with pre-computed indicators.

The rolling windows are computed ~58,000 times (1,491 days × ~39 per day). Pre-computing them once = 58,000 → ~50 calls. **~1,000x fewer rolling computations.**

---

## Optimization Tiers

### Tier 1: Pre-compute indicators (expected: 5-10x speedup)

**What:** Compute all indicators (RSI, z-score, MAs, ATR, IBS, SMA-200, etc.) once per ticker before the day loop starts. Store as columns on the DataFrame or separate arrays.

**How:**
- Add a `precompute_indicators(data)` method to each strategy that adds indicator columns to each ticker DataFrame
- Engine calls `strategy.precompute_indicators(data)` once before the walk-forward loop
- `generate_signals()` reads pre-computed values at `df.iloc[day_idx]` instead of recalculating
- Same for `check_exits()` — read pre-computed exit signals

**Saves:** ~78s of generate_signals + significant .loc overhead = **~120s** of the 190s profile

**Risk:** Medium — requires changing the strategy interface. Each strategy's `generate_signals()` must be refactored. But the indicator logic stays the same, just moved to a one-time pass.

**Approach:** Add a new optional method `precompute(data)` to BaseStrategy. If present, engine calls it once. Then `generate_signals()` checks for pre-computed columns first. This makes it backward-compatible — strategies without `precompute()` work as before.

---

### Tier 2: Eliminate per-day signal_data rebuild (expected: 2-3x on top of Tier 1)

**What:** Stop creating `signal_data = {ticker: df.loc[mask]}` every day. Instead, pass the full data + a `day_idx` or `as_of_date` parameter.

**How:**
- Change `generate_signals(data, equity, positions)` signature to include `as_of_date`
- Strategy reads only `df.loc[:as_of_date]` if needed, or (with Tier 1) just reads `df.at[as_of_date, "precomputed_rsi"]`
- Same for `check_exits()` and `_process_strategy_exits()`

**Saves:** Eliminates 598,652 pandas .loc[] calls (84.7s) — most of which are the signal_data + exit_data rebuilds

**Risk:** Medium — same interface change as Tier 1. Can be done together.

---

### Tier 3: Numpy inner loop (expected: 2x on top of Tier 1+2)

**What:** Convert DataFrames to numpy arrays for the day loop. Use integer indexing.

**How:**
- Before the day loop: `close_arr = {ticker: df["close"].values for ticker, df in data.items()}`
- In the loop: `close_arr[ticker][day_idx]` instead of `df["close"].iloc[-1]`
- Eliminates 17.2M isinstance() calls and pandas indexing overhead

**Saves:** ~18s of isinstance + pandas accessor overhead

**Risk:** Low — doesn't change strategy logic, just how data is accessed in the engine loop.

---

### Tier 4: Parallel sweep lock removal (expected: 2x throughput)

**What:** Allow 2 concurrent sweeps instead of exclusive locking.

**How:**
- Current: 1 sweep × 6 workers = 6 processes, but with 25% CPU steal → effectively 4.5 cores used
- Better: 2 sweeps × 3 workers = 6 processes, both running simultaneously
- OR: with Tier 1-3 making each backtest 10x faster, sweeps finish in 2-3 minutes and lock contention becomes negligible

**Saves:** Eliminates 15-minute lock waits

**Risk:** Low if backtests are fast enough. Medium if trying concurrent sweeps (need to test memory/CPU pressure).

---

### Tier 5: Data loading optimization (expected: minor)

**What:** Cache loaded data between sweeps within the same partition.

**Current:** Each sweep subprocess reloads data from parquet (~2 min for mean_reversion baseline)
**Better:** Keep data in shared memory (mmap'd parquet) or load once per partition cycle

**Saves:** ~2 min per sweep cycle

**Risk:** Low — mostly architecture change in autoresearch.py

---

## Expected Combined Impact

| Metric | Current | After Tier 1+2 | After Tier 1+2+3 |
|--------|---------|-----------------|-------------------|
| Baseline (MR, 50 tickers) | 313s | ~60s | ~30s |
| Baseline (TF, 50 tickers) | 96s | ~20s | ~10s |
| Experiment avg | 296s | ~50s | ~25s |
| Throughput | 6 exp/hr | ~30 exp/hr | ~60 exp/hr |
| Lock wait | 15 min | ~3 min | ~1.5 min |

Conservative estimate: **5-10x faster**. Aggressive estimate: **10-20x faster**.

---

## Implementation Order

### Phase 1: Tier 1 + 2 combined (biggest bang, 1 change)
1. Add `precompute(data: dict) -> None` method to `BaseStrategy`
2. Implement for the 7 active strategies: mean_reversion, trend_following, opening_gap, connors_rsi2, momentum_breakout, short_term_mr, bb_squeeze
3. Modify engine `_simulate_day()` to pass `as_of_date` instead of rebuilding signal_data
4. Modify engine `_process_strategy_exits()` same way
5. Run validation: old vs new must produce identical trades/metrics

### Phase 2: Tier 3 (numpy conversion)
1. Convert DataFrames to numpy arrays + date index before day loop
2. Update engine inner loop to use array indexing
3. Validate identical output

### Phase 3: Tier 4 (lock removal)
1. Reduce workers per sweep to 3
2. Remove exclusive sweep lock, use semaphore(2)
3. Or just let the speed improvement make locks irrelevant

---

## Files Modified

### Phase 1 (Tier 1+2):
- `strategies/base.py` — add `precompute()` interface
- `strategies/mean_reversion.py` — implement `precompute()`
- `strategies/trend_following.py` — implement `precompute()`
- `strategies/opening_gap.py` — implement `precompute()`
- `strategies/connors_rsi2.py` — implement `precompute()`
- `strategies/momentum_breakout.py` — implement `precompute()`
- `strategies/short_term_mr.py` — implement `precompute()`
- `strategies/bb_squeeze.py` — implement `precompute()`
- `backtest/engine.py` — call precompute, pass as_of_date, remove signal_data rebuild
- Tests to validate identical output

### Phase 2 (Tier 3):
- `backtest/engine.py` — numpy conversion in day loop

### Phase 3 (Tier 4):
- `scripts/autoresearch.py` — lock changes

---

## Validation Strategy

**Critical requirement:** optimized engine must produce BIT-IDENTICAL metrics to the current engine for the same inputs. Any change in trade count, Sharpe, drawdown, etc. means a bug was introduced.

Validation script:
1. Run current engine: `mean_reversion` on sp500 top_n=50 → save metrics
2. Run optimized engine: same inputs → compare metrics
3. Must match: total_trades, cagr_pct, sharpe, max_drawdown_pct, win_rate_pct, profit_factor
4. Run for all 7 active strategies

---

## Risks

1. **Strategy interface change** — All strategies must be updated. Risk of subtle bugs if an indicator is pre-computed with slightly different parameters than the per-day version.
   - Mitigation: Bit-identical validation per strategy.

2. **Walk-forward window boundaries** — Pre-computed indicators need to handle train/test window slicing correctly.
   - Mitigation: Pre-compute on full data, then engine only reads within the current window.

3. **Dynamic parameters** — Some strategies might adjust indicator params based on runtime state.
   - Mitigation: Audit each strategy for dynamic indicator params. If found, keep per-day computation for those only.

4. **Memory** — Pre-computing adds columns to DataFrames. 50 tickers × ~6 extra columns × 1758 rows = ~4.2MB. Negligible.
