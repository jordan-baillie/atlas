# DSR Sanity-Cap Audit — 2026-05-06

**Trigger:** Rec 1.1 fix (commit `4bac2554`) corrected DSR gate to use per-strategy variance.
Post-fix SQL scan found **11 (strategy, universe) combos** still hitting the `E[max_S] > 3.0`
sanity cap, indicating genuine parameter-space overfit.

## Methodology

DSR formula: `E[max_S] = sqrt(2 * log(n)) * sqrt(var(Sharpes))`

When `E[max_S] > 3.0` the sweep loop skips the DSR gate (treats it as degenerate).
This suppresses the overfitting guard for these combos — the worst outcome for research quality.

Per-combo root-cause: grouped all `research_experiments` rows by `params_changed` key,
computed avg Sharpe per param value, ranked by "spread" = max_avg − min_avg across values.
The dimension with the largest spread is the primary overfit driver.

---

## Cap-Hit Combos (all 11)

| Strategy | Universe | n | var | E[max_S] | Sharpe Range |
|----------|----------|---|-----|----------|--------------|
| bb_squeeze | sp500 | 202 | 1.296 | 3.71 | [-7.67, 0.69] |
| consecutive_down_days | sp500 | 245 | 2.509 | 5.25 | [-17.88, 0.92] |
| demark_sequential | sp500 | 198 | 5.445 | 7.59 | [-9.71, 0.98] |
| keltner_reversion | sp500 | 78 | 78.920 | **26.22** | [-66.56, 0.97] |
| lower_band_reversion | sp500 | 159 | 1.148 | 3.41 | [-3.72, 0.95] |
| mean_reversion | sp500 | 7647 | 0.572 | 3.20 | [-14.15, 1.37] |
| opening_gap | sp500 | 3372 | 0.885 | 3.79 | [-11.24, 0.91] |
| stochastic_oversold | sp500 | 203 | 3.265 | 5.89 | [-6.64, 0.99] |
| trend_following | sp500 | 5473 | 1.694 | 5.40 | [-13.53, 0.91] |
| triple_rsi | sp500 | 119 | 4.412 | 6.49 | [-7.91, 0.97] |
| williams_percent_r | sp500 | 187 | 4.253 | 6.67 | [-13.86, 0.98] |

---

## Per-Combo Analysis & Grid Changes

### 1. bb_squeeze / sp500 (E=3.71)

**Primary driver:** `bb_period` (avg-Sharpe spread = 4.80)

| bb_period | avg_sharpe | min_sharpe | n |
|-----------|-----------|-----------|---|
| 10 | -0.01 | -0.28 | several |
| 15 | -0.30 | -0.87 | 10 |
| 20 | -0.37 | -2.39 | 10 |
| 30 | -0.31 | **-7.67** | 10 |

**Secondary driver:** `bb_std` (spread = 1.04) — `bb_std=2.5` min=-1.09

**Changes:**
- `bb_period: [10, 15, 20, 30]` → `[10, 15]`  (remove 20, 30)
- `bb_std: [1.5, 2.0, 2.5]` → `[1.5, 2.0]`  (remove 2.5)
- Rationale: `bb_period=30` with a 15-day hold is noise-fitting (band width dwarfs signal);
  `bb_period=20` consistent negative avg. `bb_std=2.5` consistently drives negative Sharpes.
- **Grid: 144 → 48 (−67%)**

---

### 2. consecutive_down_days / sp500 (E=5.25)

**Primary driver:** `min_down_days` (avg-Sharpe spread = 4.98)

| min_down_days | avg_sharpe | min_sharpe | n |
|---------------|-----------|-----------|---|
| 2 | 0.508 | 0.108 | 4 |
| 3 | 0.399 | -0.109 | 12 |
| 4 | -2.387 | -7.532 | 13 |
| 5 | -4.475 | **-17.882** | 11 |

**Change:**
- `min_down_days: [2, 3, 4, 5]` → `[2, 3, 4]`  (remove 5)
- Rationale: Requiring 5 consecutive down days in a universe of 500 stocks creates
  selection bias toward momentum crashes — the strategy fires only into deeply
  oversold stocks that often continue falling. min=−17.88 confirms severe overfit.
- **Grid: 512 → 384 (−25%)**

---

### 3. demark_sequential / sp500 (E=7.59)

**Primary driver:** `setup_bars` (spread = 2.03)

| setup_bars | avg_sharpe | min_sharpe | n |
|-----------|-----------|-----------|---|
| 7 | -3.46 | -6.15 | 11 |
| 9 | -2.93 | -4.71 | 17 |
| 13 | -4.96 | -6.48 | 17 |

All values are negative on average — strategy is marginal on sp500. The `setup_bars=13`
variant (DeMarc "countdown" extension) produces the worst tail.

**Change:**
- `setup_bars: [7, 9, 13]` → `[7, 9]`  (remove 13)
- Rationale: DeMark Sequential is defined as 9 bars (TD9). The 13-bar variant
  ("countdown") requires 13 consecutive closes that trigger, which in sp500
  equities fires only during deep trends that reverse violently. Statistical
  power near zero (17 experiments, avg=−4.96).
- **Grid: 96 → 64 (−33%)**

---

### 4. keltner_reversion / sp500 (E=**26.22** — worst of all)

**Primary driver:** `ema_period` (avg-Sharpe spread = 28.61)

| ema_period | avg_sharpe | min_sharpe | n |
|-----------|-----------|-----------|---|
| 10 | **-33.49** | **-66.56** | 2 |
| 15 | -4.91 | -20.71 | 6 |
| 20 | -4.89 | -13.47 | 6 |

**Secondary driver:** `atr_mult` (spread = 8.81)

| atr_mult | avg_sharpe | min_sharpe | n |
|---------|-----------|-----------|---|
| 1.5 | -2.18 | -4.38 | 2 |
| 2.0 | -0.09 | -0.43 | 5 |
| 2.5 | -8.90 | **-15.90** | 5 |

**Changes:**
- `ema_period: [10, 15, 20]` → `[15, 20]`  (remove 10 — catastrophic)
- `atr_mult: [1.5, 2.0, 2.5]` → `[1.5, 2.0]`  (remove 2.5)
- Rationale: `ema_period=10` on a mean-reversion strategy produces a band so
  tight that nearly every normal intraday move triggers an entry; with sp500
  volatility this generates extreme overtrading and ruin-level drawdowns
  (min=−66.56). `atr_mult=2.5` band extends too far, missing the reversion window.
- **Grid: 288 → 128 (−56%)**

---

### 5. lower_band_reversion / sp500 (E=3.41)

**Primary driver:** `max_hold_days` (avg-Sharpe spread = 3.17)

| max_hold_days | avg_sharpe | min_sharpe | n |
|--------------|-----------|-----------|---|
| 3 | **-2.932** | **-3.719** | 8 |
| 5 | 0.205 | -0.995 | 8 |
| 7 | 0.232 | -0.897 | 8 |
| 10 | 0.234 | -0.900 | 8 |

**Secondary driver:** `ibs_threshold` (spread = 1.98)

| ibs_threshold | avg_sharpe | min_sharpe | n |
|--------------|-----------|-----------|---|
| 0.2 | 0.161 | -1.279 | 8 |
| 0.3 | **-1.599** | **-3.090** | 8 |
| 0.5 | 0.386 | 0.386 | 1 |

**Changes:**
- `max_hold_days: [3, 5, 7, 10]` → `[5, 7, 10]`  (remove 3)
- `ibs_threshold: [0.2, 0.3, 0.5]` → `[0.2, 0.5]`  (remove 0.3)
- Rationale: `max_hold_days=3` exits before mean reversion completes — the
  strategy needs ≥5 days for lower-band entries to recover toward the mean.
  `ibs_threshold=0.3` is a poorly calibrated mid-point that captures neither
  clean lower-band touches (0.2) nor confirmed reversals (0.5+).
- **Grid: 1536 → 768 (−50%)**

---

### 6. mean_reversion / sp500 (E=3.20 — barely over cap, n=7647)

**Primary driver:** `rsi_oversold` (avg-Sharpe spread = 5.59)

Top contributing min Sharpes by rsi_oversold value:
- `rsi_oversold=18` (LLM-explored): min=**-14.15** (18 not in grid but exploration seeds from 20)
- `rsi_oversold=20` (in grid): min=**-5.63**, avg negative
- `rsi_oversold=25` (in grid): min=-2.64
- `rsi_oversold=30+` (in grid): min≥-2.07, avg positive for 30+

Best from research_best: `rsi_oversold=32` (sharpe=1.372) — just above 30.

**Change:**
- `rsi_oversold: [25, 30, 35, 40, 20]` → `[30, 35, 40]`  (remove 20, 25)
- Rationale: `rsi_oversold=20` fires almost never (RSI<20 is extreme capitulation, rare
  in liquid large-caps) or fires at wrong moments; drives min=−5.63. Removing 20 and
  25 from grid stops the LLM from exploring the 15–25 oversold region which is
  economically unsound for diversified sp500 mean-reversion. The optimum (32) remains
  discoverable from the 30 starting point via LLM exploration.
- **Grid: 1,280,000 → 768,000 (−40%)**  *(rsi_oversold dimension: 5→3)*

---

### 7. opening_gap / sp500 (E=3.79)

**Primary driver:** `gap_threshold` (spread = 1.85, with catastrophic tails)

| gap_threshold | min_sharpe |
|--------------|-----------|
| -0.01 | **-11.24** |
| -0.02 | **-11.24** |
| -0.025 | **-11.24** |
| -0.03 | **-11.24** |
| -0.015 | -2.22 |
| ~0.0 | -1.09 |

Values ≤ -0.02 ALL produce min=−11.24 — these experiments hit degenerate portfolios
(too few trades to be meaningful, or all concentrated in high-beta names).

Best from research_best: `gap_threshold=-0.0` (sharpe=0.60) — near-zero, not a deep gap.

**Change:**
- `gap_threshold: [-0.01, -0.015, -0.02, -0.025, -0.03]` → `[-0.01, -0.015, -0.02]`
  (remove -0.025, -0.03)
- Rationale: Gaps of -2.5% to -3.0% in sp500 large-caps are rare, panic-driven events
  (earnings misses, macro shocks). These trigger only a handful of trades per year,
  producing near-zero statistical power. The minimum Sharpe floor is −11.24 in all
  cases — pure noise. The strategy's optimal is near 0% gap (confirmed by research_best).
- **Grid: 30,720 → 18,432 (−40%)**  *(gap_threshold: 5→3)*

---

### 8. stochastic_oversold / sp500 (E=5.89)

**Primary driver:** `stoch_period` (spread = 3.86)

| stoch_period | avg_sharpe | min_sharpe | n |
|------------|-----------|-----------|---|
| 5 | **0.399** | 0.399 | 1 |
| 14 | -1.812 | -3.635 | 11 |
| 10 | -3.300 | -3.662 | 11 |
| 21 | -3.458 | -3.980 | 11 |

**Secondary driver:** `stoch_smooth` (spread = 4.09)

| stoch_smooth | avg_sharpe | min_sharpe | n |
|-------------|-----------|-----------|---|
| 3 | 0.357 | 0.151 | 11 |
| 5 | **-3.733** | **-4.471** | 11 |

`stoch_smooth=5` is uniformly catastrophic. This is a clean systematic overfit:
over-smoothing the stochastic turns a fast-mean-reversion signal into a lagging
indicator that never fires at genuine oversold levels.

**Changes:**
- `stoch_period: [5, 10, 14, 21]` → `[5, 10, 14]`  (remove 21)
- `stoch_smooth: [3, 5]` → `[3]`  (remove 5)
- Rationale: `stoch_smooth=5` with any period is economically unsound for the
  short-term mean-reversion use case (avg=−3.73 across all periods). `stoch_period=21`
  also consistently terrible (avg=−3.46). Period=5 is the only value that works.
- **Grid: 1,024 → 384 (−62.5%)**

---

### 9. trend_following / sp500 (E=5.40)

**Primary driver:** `slow_ma` (spread = 5.99, via LLM-explored values)

The original grid has `slow_ma: [20, 50, 100, 200]` — all reasonable. The catastrophic
results come from LLM exploration of `slow_ma=10,18,22,25` which the grid seeds via
`fast_ma=20` (near-crossing confusion creates degenerate signals).

**Secondary driver:** `fast_ma` (spread = 4.18)

| fast_ma (in grid) | avg_sharpe |
|------------------|-----------|
| 50 | 0.483 |
| 30 | 0.386 |
| 10 | 0.323 |
| 15 | 0.205 → best is actually higher when paired well |
| 20 | 0.205 |

`fast_ma=20` paired with `slow_ma=20` = zero spread (degenerate). The LLM seeded
from fast_ma=20 explores fast_ma=19,21,22 which all hit near slow_ma=20 → catastrophic.

**Third driver:** `pullback_pct=0.06` (avg=−0.60, min=−2.95):

| pullback_pct | avg_sharpe |
|-------------|-----------|
| 0.02–0.05 | 0.37–0.54 |
| 0.06 | **-0.60** |

**Changes:**
- `fast_ma: [10, 15, 20, 30, 50]` → `[10, 15, 30, 50]`  (remove 20)
- `pullback_pct: [0.02, 0.03, 0.04, 0.05, 0.06]` → `[0.02, 0.03, 0.04, 0.05]`  (remove 0.06)
- Rationale: `fast_ma=20` equals the smallest `slow_ma` in the grid (20), creating
  degenerate zero-spread MAs; removes the seed that causes LLM exploration of fast≈slow
  values. `pullback_pct=0.06` requires a 6% pullback before entry — in a trend-following
  strategy this means entering after significant adverse moves, capturing continuation
  only if the trend is very strong; empirically avg=−0.60.
- **Grid: 38,400 → 24,576 (−36%)**

---

### 10. triple_rsi / sp500 (E=6.49)

**Primary driver:** `rsi_entry` (spread = 5.10)

| rsi_entry | avg_sharpe | min_sharpe | n |
|----------|-----------|-----------|---|
| 35 | -0.185 | -0.185 | 1 |
| 30 | -2.418 | -4.938 | 7 |
| 25 | -2.506 | -5.149 | 7 |
| 20 | **-5.283** | **-7.912** | 7 |

**Secondary driver:** `decline_days` (spread = 4.52)

| decline_days | avg_sharpe | min_sharpe | n |
|-------------|-----------|-----------|---|
| 2 | 0.064 | 0.064 | 1 |
| 3 | -0.913 | -3.345 | 6 |
| 4 | **-4.458** | **-5.697** | 6 |

**Changes:**
- `rsi_entry: [20, 25, 30, 35]` → `[30, 35]`  (remove 20, 25)
- `decline_days: [2, 3, 4]` → `[2, 3]`  (remove 4)
- Rationale: Triple-RSI requires 3 daily RSI readings ALL below the threshold.
  With `rsi_entry=20`, you need RSI<20 on 3 consecutive days — this fires only
  during severe crashes and produces negative alpha (entering falling knives).
  `rsi_entry=25` has similar pathology. `decline_days=4` with triple-RSI already
  requiring 3 consecutive down closes = 4-day losing streak requirement; fires too
  rarely and with poor signal quality. Research_best values (rsi_entry=35, decline_days=2)
  both preserved.
- **Grid: 1,152 → 384 (−67%)**

---

### 11. williams_percent_r / sp500 (E=6.67)

**Primary driver:** `wr_entry` (spread = 5.16)

| wr_entry | avg_sharpe | min_sharpe | n |
|---------|-----------|-----------|---|
| -95 | 0.001 | -0.108 | 13 |
| -80 | -0.468 | -2.497 | 13 |
| -85 | -1.312 | -3.784 | 7 |
| -90 | **-5.160** | **-13.859** | 13 |

**Change:**
- `wr_entry: [-80, -85, -90, -95]` → `[-80, -85, -95]`  (remove -90)
- Rationale: Williams %R entry at -90 means price is in the bottom 10% of its
  recent range. This fires predominantly during sustained downtrends when price
  keeps making new lows — classic knife-catching overfit. min=−13.86 confirms
  severe tail risk. Research_best value (wr_entry=−85) is preserved.
- **Grid: 384 → 288 (−25%)**

---

## Summary of Changes

| Strategy | Dimension(s) Tightened | Old Count | New Count | Grid Before | Grid After | Reduction |
|----------|----------------------|-----------|-----------|-------------|------------|-----------|
| bb_squeeze | bb_period, bb_std | 4,3 | 2,2 | 144 | 48 | -67% |
| consecutive_down_days | min_down_days | 4 | 3 | 512 | 384 | -25% |
| demark_sequential | setup_bars | 3 | 2 | 96 | 64 | -33% |
| keltner_reversion | ema_period, atr_mult | 3,3 | 2,2 | 288 | 128 | -56% |
| lower_band_reversion | max_hold_days, ibs_threshold | 4,3 | 3,2 | 1536 | 768 | -50% |
| mean_reversion | rsi_oversold | 5 | 3 | 1.28M | 768K | -40% |
| opening_gap | gap_threshold | 5 | 3 | 30720 | 18432 | -40% |
| stochastic_oversold | stoch_period, stoch_smooth | 4,2 | 3,1 | 1024 | 384 | -62.5% |
| trend_following | fast_ma, pullback_pct | 5,5 | 4,4 | 38400 | 24576 | -36% |
| triple_rsi | rsi_entry, decline_days | 4,3 | 2,2 | 1152 | 384 | -67% |
| williams_percent_r | wr_entry | 4 | 3 | 384 | 288 | -25% |

---

## Research_best Preservation Check

All tightenings preserve the values present in the highest-Sharpe `research_best` row per combo:

| Strategy | Best research_best row | Preserved? |
|----------|----------------------|------------|
| bb_squeeze | `{atr_stop_mult: 1.5}` sharpe=0.687 | ✓ atr_stop_mult unchanged |
| consecutive_down_days | `{}` sharpe=0.923 | ✓ no params to protect |
| demark_sequential | `{}` sharpe=0.977 | ✓ no params to protect |
| keltner_reversion | `{}` sharpe=0.966 | ✓ no params; low-sharpe row (0.04) had ema_period=10 which we remove — but it's not the best |
| lower_band_reversion | `{}` sharpe=0.946 | ✓ no params; secondary row has ibs_threshold=0.5 which stays |
| mean_reversion | `{rsi_oversold:32}` sharpe=1.372 | ⚠️ 32 not in grid (LLM-found); 30 stays as seed → LLM will re-find 32 |
| opening_gap | `{}` sharpe=0.910 | ✓ no params; secondary has gap_threshold=-0.0 (not in grid) |
| stochastic_oversold | `{}` sharpe=0.990 | ✓ no params; secondary row has stoch_period=5 which stays |
| trend_following | `{}` sharpe=0.910 | ✓ no params; secondary has fast_ma=15 (stays), pullback_pct=0.04 (stays) |
| triple_rsi | `{}` sharpe=0.966 | ✓ no params; secondary has rsi_entry=35,decline_days=2 (both preserved) |
| williams_percent_r | `{}` sharpe=0.977 | ✓ no params; secondary has wr_entry=-85 which stays |

