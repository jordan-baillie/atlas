# Atlas-ASX Optimization & Validation Guide

## Version History
| Version | Date | CAGR | Sharpe | PF | MaxDD | Notes |
|---------|------|------|--------|-----|-------|-------|
| v9.1 | 2026-02-18 | -0.35% | -0.30 | 0.98 | 12.84% | Post data-refresh degradation |
| v9.2 | 2026-02-18 | +11.21% | +0.67 | 1.30 | 7.76% | Full coordinate descent reoptimization |
| v9.3 | 2026-02-19 | +6.65% | +0.29 | 1.14 | 12.32% | 50/50 blend (rejected - lower perf, same stability) |

## Key Learnings from v9.2 Optimization

### 1. What Worked
- **Coordinate descent** across all strategies simultaneously was effective
- **BB Squeeze** was the biggest turnaround: score -2.45 to +8.34
- Key BB Squeeze changes: wider Bollinger bands (bb_std 2.5 to 3.0), tighter Keltner (kc_atr_mult 2.5 to 2.0), wider trailing stop (2.0 to 3.0 ATR), longer hold (10 to 20 days)
- **Trend Following** benefited from slower MAs (fast 10 to 20, slow 30 to 50) and longer holds (20 to 25 days)

### 2. Overfitting Signals Detected
- **OOS degradation**: CAGR dropped from 17.09% (in-sample) to 2.20% (out-of-sample Jun 2025+)
- **Parameter sensitivity**: Mean perturbed CAGR was only 2.67% vs 11.21% baseline (with 15% perturbation)
- 2/10 perturbation trials collapsed to negative CAGR
- Parameters sit at a **sharp peak**, not a robust plateau

### 3. Mitigating Factors
- Walk-forward windows showed 59% positive rate (22 windows, 13 positive)
- No time decay: recent windows actually slightly better than early ones
- Market regime change (Jun-Sep 2025) explains much of OOS degradation

### 4. Why Parameter Blending Did Not Help
- v9.3 (50/50 blend) showed SAME perturbation stability as v9.2
- Mean perturbed CAGR: v9.2=2.67% vs v9.3=2.66% (identical)
- Blending sacrificed 4.5% CAGR without stability gain
- The parameter landscape has a single ridge structure

## Optimization Procedure

### When to Re-Optimize
1. Run health_check.py regularly (scheduled weekly or after data refresh)
2. If health check flags degradation (CAGR drop >50%, Sharpe negative, PF <1.0)
3. After significant market regime changes
4. After adding new tickers to the universe

### Step-by-Step Process
1. Refresh data: python scripts/refresh_all_data.py
2. Run health check: python scripts/health_check.py (exit 0=healthy, 1=degraded)
3. If degraded, re-optimize: python scripts/reoptimize_full_universe.py
4. Validate (CRITICAL): python scripts/validate_oos.py
5. Check stability: python scripts/param_stability_report.py
6. Or run full pipeline: python scripts/auto_reoptimize.py

### Validation Checklist (before accepting new params)
- OOS CAGR > 0% (must be profitable out-of-sample)
- OOS Sharpe > -0.5 (moderate degradation acceptable)
- Perturbation mean CAGR > 50% of baseline
- Less than 3/10 perturbation trials with negative CAGR
- Walk-forward window win rate > 50%
- No time decay in walk-forward windows

## Automation Scripts

| Script | Purpose | Runtime | Schedule |
|--------|---------|---------|----------|
| health_check.py | Quick 6-month performance check | ~90s | Weekly |
| auto_reoptimize.py | Full pipeline: check/optimize/validate/update | ~2hrs | On degradation |
| param_stability_report.py | Perturbation analysis with sensitivity | ~50min | After optimization |
| validate_oos.py | Time-split + perturbation + walk-forward validation | ~55min | After optimization |
| reoptimize_full_universe.py | Coordinate descent across all strategies | ~45min | When needed |

## Future Improvements to Consider
1. Regularized optimization: Add penalty for parameter distance from defaults
2. Bayesian optimization: Replace coordinate descent with GP-based optimization
3. Rolling window reoptimization: Re-optimize on rolling 18-month windows
4. Dynamic sizing validation: Enable and test the existing dynamic_sizing module
5. Cross-validation: Use k-fold time-series CV instead of single train/test split
