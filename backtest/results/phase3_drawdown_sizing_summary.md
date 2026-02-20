# Phase 3: Drawdown-Based Position Sizing - Results

**Date:** 2026-02-20
**Test:** Graduated drawdown scaling vs fixed risk
**Tiers:** DD<2%=1.0x | DD 2-4%=0.85x | DD 4-6%=0.70x | DD 6%+=0.55x

## Results

| Metric        | Arm A (Fixed) | Arm B (DDScale) | Delta    |
|---------------|---------------|-----------------|----------|
| Total Trades  | 199           | 187             | -12      |
| CAGR          | 8.34%         | 7.82%           | -0.52%   |
| Sharpe        | 0.522         | 0.474           | -0.048   |
| Profit Factor | 1.639         | 1.589           | -0.049   |
| Win Rate      | 54.3%         | 52.9%           | -1.3%    |
| Max Drawdown  | 7.46%         | 6.83%           | -0.63% ✅ |

## Verdict: DISABLED (1/4 metrics improved)

## Analysis
- Drawdown scaling reduced max DD by 0.63% (the only improvement)
- But Sharpe WORSENED despite lower drawdown - counterintuitive
- Root cause: mean reversion best opportunities OCCUR DURING drawdowns
  (buying oversold stocks after market stress = the core strategy edge)
  Scaling down at exactly these moments removes the highest-quality trades
- 12 fewer trades: small positions fell below $500 minimum during scaling
- This confirms the ASX mean reversion system should NOT reduce exposure during drawdowns

## Why Sharpe Worsened
Sharpe = mean(daily_returns) / std(daily_returns)
- Scaling down during drawdowns reduces both the mean return AND std
- But the best recovery period returns (after DD troughs) are also scaled down
- Net effect: worse return-to-volatility ratio despite lower peak drawdown

## Phase 3 Complete - Final Scorecard
1. Fee-aware signal filter: ❌ DISABLED (over-filtered marginally profitable trades)
2. Regime filter (IOZ MA + breadth): ❌ DISABLED (-5.19% CAGR, 46% fewer trades)
3. Volume spike confirmation: ❌ DISABLED (0/4 metrics improved)
4. Drawdown-based position sizing: ❌ DISABLED (Sharpe worsened despite lower DD)

## System at Optimum
All Phase 3 enhancements rejected. The baseline system at CAGR=8.34%,
Sharpe=0.522, MaxDD=7.46% represents a robust local optimum for ASX
mean reversion + trend following + opening gap reversal strategies.

The real path forward is track record accumulation:
- Need 717 days for MinTRL (currently ~503 days)
- Target: December 2026 for Phase 2 re-validation
