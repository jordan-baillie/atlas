# Phase 7C: Market Breadth Empirical Analysis Report
## Atlas-ASX Trading System
## Date: 2026-02-16

---

## Executive Summary

**Market breadth indicators show STRONG, statistically significant predictive power
for trade outcomes.** Unlike volume (Phase 7A, no significance), breadth data reveals
that our strategies perform dramatically better when market breadth is WEAK.

This is the first data enhancement to show genuine alpha-generation potential.

---

## Data
- **Sample**: 46 trades (28 winners, 18 losers)
- **Period**: Walk-forward backtest across ~2 years of ASX data
- **Strategies**: Trend Following (24 trades), Mean Reversion (22 trades)
- **Breadth Universe**: 100 ASX stocks (same as trading universe)

---

## Key Finding: breadth_pct_above_50ma (% of stocks above 50-day MA)

### Winners vs Losers
| Metric | Winners (n=28) | Losers (n=18) | Difference |
|--------|---------------|---------------|------------|
| Mean   | 50.5%         | 60.4%         | -9.9 pts   |
| p-value (t-test) | | | **0.006 *** |
| p-value (Mann-Whitney) | | | **0.005** |

### Correlation with P&L
| Test | Coefficient | p-value |
|------|------------|--------|
| Pearson r | -0.356 | 0.015 ** |
| Spearman r | -0.492 | 0.0005 *** |

### Bucketed Performance
| Breadth Regime | Trades | Win Rate | Total P&L | Profit Factor |
|---------------|--------|----------|-----------|---------------|
| Low (<48%)    | 20     | **90.0%** | **$313.79** | **22.12** |
| Medium (48-58%) | 8    | 37.5%    | $11.06    | 1.25 |
| High (>58%)   | 18     | 38.9%    | **-$18.35** | **0.85** |

**Interpretation**: Trades in low-breadth environments capture $313.79 (102% of total system
profit of $306.50). High-breadth trades are NET LOSERS.

---

## Secondary Finding: breadth_thrust

### Winners vs Losers
| Metric | Winners | Losers | Difference |
|--------|---------|--------|------------|
| Mean   | 54.4    | 62.0   | -7.5 pts   |
| p-value (t-test) | | | **0.011 ** |

### Bucketed Performance
| Thrust Level | Trades | Win Rate | Total P&L | Profit Factor |
|-------------|--------|----------|-----------|---------------|
| Low (<52)   | 16     | **87.5%** | **$225.85** | **16.20** |
| Medium (52-60) | 10  | 50.0%    | $49.52    | 2.89 |
| High (>60)  | 20     | 45.0%    | $31.13    | 1.22 |

---

## Other Breadth Indicators

| Indicator | Pearson r(pnl) | p-value | Significant? |
|-----------|---------------|---------|-------------|
| pct_above_50ma | -0.356 | 0.015 | **Yes** |
| breadth_momentum | -0.302 | 0.042 | **Yes** |
| breadth_thrust | -0.281 | 0.059 | Marginal |
| pct_above_200ma | -0.250 | 0.094 | Marginal |
| net_new_highs_pct | -0.196 | 0.191 | No |
| ad_ratio | +0.028 | 0.854 | No |

---

## Strategy-Level Analysis

### Trend Following (24 trades)
- breadth_pct_above_50ma: Winners 48.3 vs Losers 63.4 (p=**0.006 ***)
- breadth_thrust: Winners 54.2 vs Losers 64.9 (p=**0.009 ***)
- **Trend following is highly sensitive to market breadth**

### Mean Reversion (22 trades)
- breadth_pct_above_50ma: Winners 53.1 vs Losers 57.3 (p=0.363)
- breadth_thrust: Winners 54.7 vs Losers 59.1 (p=0.325)
- Mean reversion shows same directional effect but not statistically significant

---

## Interpretation

The counterintuitive finding (lower breadth = better trades) makes strategic sense:

1. **Trend Following**: Works best catching recovery/early-trend moves when the market
   is depressed. When breadth is already high, trend signals are late/crowded.

2. **Mean Reversion**: Somewhat benefits from weak breadth as oversold conditions
   are more genuine when the broader market is also weak.

3. **Market Cycle Position**: Low breadth = early recovery = best entry timing.
   High breadth = extended/crowded = poor entry timing.

---

## Recommendations

### Confidence Modifier Proposal
Based on the empirical data, implement a breadth-based confidence modifier:

```
if pct_above_50ma < 48:
    confidence_boost = +0.05 to +0.10  (more aggressive)
elif pct_above_50ma > 58:
    confidence_penalty = -0.05 to -0.10  (more cautious)
```

### Specifically for Trend Following:
- Consider a stronger modifier given p=0.006 significance
- Potentially SKIP trend signals when breadth > 65% (0% of those won profitably)

### Caveats
1. **Sample size**: 46 trades is small. Effect is strong (p<0.01) but could shift.
2. **Overfitting risk**: Must validate on out-of-sample data before production use.
3. **Regime dependency**: These results span ~2 years; longer periods needed.
4. **Recommended approach**: Start with SOFT confidence modifiers, not hard filters.

---

## Comparison with Phase 7A (Volume)

| Metric | Volume (7A) | Breadth (7C) |
|--------|------------|-------------|
| Best p-value | 0.78 (not significant) | **0.006 (highly significant)** |
| Correlation with P&L | None | **r = -0.49 (Spearman)** |
| Actionable? | No - info only | **Yes - confidence modifier candidate** |
| Effect size | Negligible | **Large (90% vs 39% win rate by regime)** |

**Market breadth is the first data enhancement to show genuine predictive value.**
