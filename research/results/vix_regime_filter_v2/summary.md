# VIX Regime Filter v2 — Research Summary

**Run date:** 2026-03-03 03:41 UTC  
**Period:** 2020-01-01 → 2026-03-01  
**Strategy proxy:** RSI(2) on SPY — entry RSI<10, exit RSI>90 or hold≥10d  

---

## Context: Why v2?

Wave 1 (`exp-wave1_vix_filter.json`) tested static VIX level filters (block entries when VIX ≥ 20/25/30/35).
**Verdict: FAIL.** All variants degraded Sharpe. The core insight:

> Mean reversion strategies **thrive** during high-VIX (panic) regimes.
> VIX > 25 = oversold entries = best MR alpha. Blocking them destroys edge.

Wave 2 tests a different hypothesis:

- **ROC filters**: Does a sudden VIX _spike_ (5d change > 20/30/50%) signal regime change that hurts MR?
- **Panic-only mode**: Should we ONLY allow MR entries during high VIX?
- **Combined**: Level + momentum filter together.

---

## Benchmark

| Metric | SPY Buy & Hold |
|--------|---------------|
| CAGR | 14.47% |
| Sharpe | 0.568 |
| Max DD | 33.72% |

---

## Results by Filter Variant

| # | Filter | Trades | CAGR% | Sharpe | Max DD% | Win Rate | PF | ΔSharpe |
|---|--------|--------|-------|--------|---------|----------|----|---------|
| 01 | Baseline (no VIX filter) | 66 | 8.18 | 0.327 | 30.44 | 68.2% | 1.65 | +0.000 |
| 02 | Level: VIX < 20 (block VIX≥20) | 22 | 0.56 | -0.727 | 9.36 | 63.6% | 1.18 | -1.054 |
| 03 | Level: VIX < 25 (block VIX≥25) | 42 | 3.26 | -0.070 | 17.21 | 66.7% | 1.50 | -0.398 |
| 04 | Level: VIX < 30 (block VIX≥30) | 55 | 3.76 | 0.026 | 17.09 | 67.3% | 1.41 | -0.301 |
| 05 | ROC: skip when VIX 5d spike > 20% ✓ | 48 | 10.95 | 0.616 | 6.81 | 66.7% | 2.76 | +0.289 |
| 06 | ROC: skip when VIX 5d spike > 30% ✓ | 52 | 9.84 | 0.515 | 5.58 | 63.5% | 2.26 | +0.188 |
| 07 | ROC: skip when VIX 5d spike > 50% ✓ | 58 | 12.27 | 0.689 | 7.00 | 67.2% | 2.52 | +0.362 |
| 08 | ROC (abs): skip |VIX 5d chg| > 30% ✓ | 52 | 9.84 | 0.515 | 5.58 | 63.5% | 2.26 | +0.188 |
| 09 | Panic-only: enter ONLY when VIX > 20 | 50 | 8.18 | 0.330 | 30.44 | 68.0% | 1.78 | +0.003 |
| 10 | Panic-only: enter ONLY when VIX > 25 | 29 | 5.33 | 0.159 | 30.44 | 65.5% | 1.81 | -0.169 |
| 11 | Combined: VIX<30 AND spike≤30% | 45 | 2.53 | -0.149 | 12.01 | 62.2% | 1.36 | -0.476 |

_ΔSharpe = variant Sharpe minus baseline Sharpe. ✓ = meets improvement threshold (≥+0.03)_

---

## VIX Level Analysis (baseline trades)

Trade quality broken down by VIX level at entry time (baseline = no filter):

| VIX Bucket | Count | Win Rate | Avg PnL% |
|------------|-------|----------|----------|
| low (VIX<20) | 21 | 61.9% | 0.148% |
| mid (20≤VIX<30) | 33 | 69.7% | 0.669% |
| high (VIX≥30) | 12 | 75.0% | 2.571% |

---

## VIX Rate-of-Change Analysis (baseline trades)

Trade quality broken down by VIX 5-day ROC at entry time:

| VIX ROC Bucket | Count | Win Rate | Avg PnL% |
|----------------|-------|----------|----------|
| no_spike (ROC≤20%) | 39 | 69.2% | 1.675% |
| mild_spike (20–30%) | 8 | 62.5% | 0.214% |
| spike (30–50%) | 10 | 70.0% | 0.880% |
| big_spike (>50%) | 9 | 66.7% | -2.201% |

---

## Verdict

**PROMISING**

### Promising variants
- **ROC: skip when VIX 5d spike > 20%**: Sharpe +0.289 (baseline→0.616), trades=48, CAGR=10.95%
- **ROC: skip when VIX 5d spike > 30%**: Sharpe +0.188 (baseline→0.515), trades=52, CAGR=9.84%
- **ROC: skip when VIX 5d spike > 50%**: Sharpe +0.362 (baseline→0.689), trades=58, CAGR=12.27%
- **ROC (abs): skip |VIX 5d chg| > 30%**: Sharpe +0.188 (baseline→0.515), trades=52, CAGR=9.84%

These variants are added to `research/queue.json` as Wave 2 candidates.

---

## Files

- `results.json` — full metrics per variant
- `equity_curves.csv` — daily equity for all variants
- `summary.md` — this file
