# Strategy Correlation Matrix

> Generated: 2026-03-12 17:35
> Strategies analyzed: 10

## Correlation Matrix (Pearson, daily returns)

| | bb_squeeze | connors_rsi2 | consecutive_ | lower_band_r | mean_reversi | momentum_bre | opening_gap | sector_rotat | short_term_m | trend_follow |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| **bb_squeeze** | 1.00 | 0.11 | 0.32 | 0.29 | 0.09 | 0.12 | 0.05 | 0.21 | 0.01 | 0.26 |
| **connors_rsi2** | 0.11 | 1.00 | 0.16 | 0.14 | 0.94 | 0.02 | 0.94 | 0.06 | 0.02 | 0.08 |
| **consecutive_** | 0.32 | 0.16 | 1.00 | 0.42 | 0.10 | 0.11 | 0.05 | 0.20 | 0.01 | 0.34 |
| **lower_band_r** | 0.29 | 0.14 | 0.42 | 1.00 | 0.11 | 0.05 | 0.06 | 0.25 | 0.02 | 0.24 |
| **mean_reversi** | 0.09 | 0.94 | 0.10 | 0.11 | 1.00 | 0.02 | 0.95 | 0.05 | -0.00 | 0.08 |
| **momentum_bre** | 0.12 | 0.02 | 0.11 | 0.05 | 0.02 | 1.00 | 0.01 | 0.08 | -0.00 | 0.06 |
| **opening_gap** | 0.05 | 0.94 | 0.05 | 0.06 | 0.95 | 0.01 | 1.00 | 0.03 | 0.00 | 0.01 |
| **sector_rotat** | 0.21 | 0.06 | 0.20 | 0.25 | 0.05 | 0.08 | 0.03 | 1.00 | -0.00 | 0.14 |
| **short_term_m** | 0.01 | 0.02 | 0.01 | 0.02 | -0.00 | -0.00 | 0.00 | -0.00 | 1.00 | 0.18 |
| **trend_follow** | 0.26 | 0.08 | 0.34 | 0.24 | 0.08 | 0.06 | 0.01 | 0.14 | 0.18 | 1.00 |

## Within-Group Correlations

- **momentum** (2 strategies): avg=0.059, range=[0.059, 0.059]
- **mean_reversion** (6 strategies): avg=0.183, range=[-0.005, 0.944]
- **other** (2 strategies): avg=0.031, range=[0.031, 0.031]

## Cross-Group: Momentum vs Mean Reversion

- Average correlation: **0.126** (hypothesis: < 0.20, Balvers & Wu predict -0.35)
- Range: [-0.001, 0.336] across 12 pairs
- Low cross-group correlation: ✅ VALIDATED
