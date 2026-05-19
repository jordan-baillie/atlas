# Same-Bar Stop Mitigation Decision — 2026-05-20

## Context

6 same-bar stops in the last 30 days, **all on momentum_breakout**, 27.3%
per-strategy rate. The tight 1.98% stop buffer (`atr_stop_mult=0.61`) is being
blown through by opening volatility on entry day.

**Goal**: backtest mitigations and ship a winning configuration, or recommend
defer if no variant meets the bar.

---

## Option A — Entry Delay (15 min) — DEFERRED

**Option A (entry_delay_minutes=15) cannot be evaluated on daily bars.**

Atlas backtests use daily OHLCV bars. On daily resolution, a 15-minute entry
delay has no measurable effect: the entry still fires on the same bar's open.
This option requires **5-minute intraday bars** (Task #316). Until that
prerequisite is complete, Option A remains blocked at the data layer and is
excluded from this comparison.

---

## Variants Tested

| Variant  | atr_stop_mult | Approx stop buffer |
|----------|:-------------:|:------------------:|
| Baseline | 0.61          | ~1.98%             |
| B-2.5    | 0.77          | ~2.5%              |
| B-3.0    | 0.92          | ~3.0%              |
| B-3.5    | 1.08          | ~3.5%              |

**Note on same-bar proxy**: The backtest runs on daily bars; minimum hold
period is 1 day (not 0). The same-bar stop proxy used is:
`hold_days == 1 AND exit_reason == "stop_hit"` for momentum_breakout trades.
This captures "stop hit on first available day after entry" — the daily-bar
equivalent of opening-volatility stop outs. Numbers labelled **SB-proxy** are
daily-bar approximations.

---

## Backtest Results

*Walk-forward, sp500, 198 tickers, full data window. Run 2026-05-20.*

| Variant  | atr_mult | Sharpe  | CAGR%  | MaxDD%  | Win% | Trades | SB-proxy% |
|----------|:--------:|--------:|-------:|--------:|-----:|-------:|----------:|
| Baseline | 0.61     |  0.6495 | 13.87% | 23.79%  | 23.4%|    214 |     12.2% |
| B-2.5    | 0.77     | -0.0003 |  3.42% | 17.39%  | 22.2%|    189 |     10.6% |
| B-3.0    | 0.92     |  0.1269 |  4.92% | 24.04%  | 24.4%|    164 |      6.7% |
| B-3.5    | 1.08     | -0.2651 |  1.65% | 18.26%  | 25.5%|    145 |      5.5% |

*Full results persisted to `data/same_bar_mitigation_comparison_20260520T095305.json`.*

---

## Decision Criteria

1. Reduce same-bar proxy rate by ≥50% (baseline 12.2% → target ≤ 6.1%)
2. Preserve Sharpe within 80% of baseline (≥ 0.5196)
3. MaxDD must not exceed 1.20× baseline (≤ 28.55%)

### Per-variant assessment

**B-2.5 (0.77)**
- ✗ SB-proxy 10.6% > target 6.1% — insufficient reduction
- ✗ Sharpe −0.0003 (Sharpe collapses entirely)

**B-3.0 (0.92)**
- ✗ SB-proxy 6.7% > target 6.1% — just misses the 50% reduction bar
- ✗ Sharpe 0.127 (only 19.5% of baseline — fails 80% retention)

**B-3.5 (1.08)**
- ✓ SB-proxy 5.5% ≤ target 6.1%
- ✗ Sharpe −0.265 (massively negative — fails 80% retention)

---

## Decision: **NO SHIP**

**No variant meets all 3 decision criteria simultaneously.**

The key finding is that `momentum_breakout` relies heavily on its tight stop
for risk management and profitability. Widening the stop degrades performance
dramatically:

- At 0.77 (B-2.5): Sharpe collapses from 0.65 → ≈ 0. The tight stop is doing
  meaningful work filtering losing trades.
- At 0.92 (B-3.0): SB proxy just misses the 50% target (6.7% vs 6.1%), and
  Sharpe drops to 0.13 (20% retention only).
- At 1.08 (B-3.5): Achieves the SB reduction but Sharpe turns negative.

The underlying issue is that `atr_stop_mult=0.61` is already at the edge of
the strategy's profitable parameter space. A wider stop causes the backtest to
take larger losses on the same momentum trades, and the 6× profit target ATR
multiplier does not compensate.

---

## Forward Recommendations

### 1. Monitor (immediate)
Keep `atr_stop_mult=0.61`. Track same-bar stops weekly. If the 27.3% rate
persists or worsens over another 30-day window, escalate.

### 2. Option A (medium-term) — after Task #316
Once 5-minute intraday bars are available, re-run the comparison with
`entry_delay_minutes=15`. A delayed entry avoids the opening-minute spike that
causes same-bar stops without requiring a wider daily stop. This preserves the
tight stop discipline that the strategy needs.

### 3. Earnings blackout tuning (alternative)
Several same-bar stops may coincide with post-earnings volatility (already
partially mitigated by `earnings_blackout.days_before=5`). Consider extending
to `days_before=7` and running a targeted backtest.

### 4. Signal-entry filter (alternative)
Add a minimum gap-down filter at signal generation: skip entries where
`open < prev_close × 0.98` (i.e., skip opening gap-down days which are the
most common same-bar stop trigger).

---

## Config Version

**No change.** `sp500.json` remains at `v3.2.4`.

A sentinel regression test in `tests/test_same_bar_mitigation.py` asserts
`atr_stop_mult == 0.61` and `version == "v3.2.4"` to prevent silent drift.

---

## References

- Backtest runner: `scripts/backtest_same_bar_mitigation_comparison.py`
- Results JSON: `data/same_bar_mitigation_comparison_20260520T095305.json`
- Sentinel tests: `tests/test_same_bar_mitigation.py`
- Task #316: 5-min intraday backfill (prerequisite for Option A)
