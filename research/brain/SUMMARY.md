# Research Brain Summary

*Auto-regenerated 2026-05-12. Re-run via `python3 scripts/regen_brain_summary.py`.*

*This file is overwritten on each regen — do not edit by hand.*

---

## 1. Strategy Lifecycle State Distribution

| State | Count |
|-------|-------|
| `RESEARCH` | 65 |
| `LIVE` | 8 |
| `PAPER` | 1 |
| **Total** | **74** |

## 2. Active Strategies per Market

**asx** ⚪ (mode=`passive`, live_enabled=`False`)
  *(no strategies configured)*

**commodity_etfs** ⚪ (mode=`passive`, live_enabled=`False`)
  `bb_squeeze`, `connors_rsi2`, `dividend_capture`, `mean_reversion`, `momentum_breakout`, `mtf_momentum`, `opening_gap`, `sector_rotation`, `short_term_mr`, `trend_following`

**crypto** ⚪ (mode=`paper`, live_enabled=`False`)
  `connors_rsi2`, `mean_reversion`, `momentum_breakout`, `trend_following`

**defensive_etfs** ⚪ (mode=`passive`, live_enabled=`False`)
  `bb_squeeze`, `connors_rsi2`, `dividend_capture`, `mean_reversion`, `momentum_breakout`, `mtf_momentum`, `opening_gap`, `sector_rotation`, `short_term_mr`, `trend_following`

**gold_etfs** ⚪ (mode=`passive`, live_enabled=`False`)
  `bb_squeeze`, `connors_rsi2`, `dividend_capture`, `mean_reversion`, `momentum_breakout`, `mtf_momentum`, `opening_gap`, `sector_rotation`, `short_term_mr`, `trend_following`

**regime** ⚪ (mode=`?`, live_enabled=`False`)
  *(no strategies configured)*

**sector_etfs** ⚪ (mode=`passive`, live_enabled=`False`)
  `bb_squeeze`, `connors_rsi2`, `dividend_capture`, `mean_reversion`, `momentum_breakout`, `mtf_momentum`, `opening_gap`, `sector_rotation`, `short_term_mr`, `trend_following`

**sp500** 🟢 (mode=`live`, live_enabled=`True`)
  `bb_squeeze`, `connors_rsi2`, `dividend_capture`, `mean_reversion`, `momentum_breakout`, `mtf_momentum`, `opening_gap`, `sector_rotation`, `short_term_mr`, `trend_following`

**treasury_etfs** ⚪ (mode=`passive`, live_enabled=`False`)
  `bb_squeeze`, `connors_rsi2`, `dividend_capture`, `mean_reversion`, `momentum_breakout`, `mtf_momentum`, `opening_gap`, `sector_rotation`, `short_term_mr`, `trend_following`

## 3. Top-10 Strategies by Sharpe Ratio

| # | Strategy | Universe | Regime | Sharpe | Trades | Max DD% | Updated |
|---|----------|----------|--------|--------|--------|---------|---------|
| 1 | `connors_rsi2` | `commodity_etfs` | `bull_risk_on` | 1.4978 | 651 | 20.4% | 2026-05-06 |
| 2 | `mean_reversion` | `commodity_etfs` | `cross` | 1.4414 | 537 | 18.8% | 2026-05-07 |
| 3 | `short_term_mr` | `sp500` | `cross` | 1.4386 | 263 | 26.9% | 2026-05-08 |
| 4 | `mean_reversion` | `commodity_etfs` | `bull_risk_on` | 1.4141 | 598 | 16.8% | 2026-05-06 |
| 5 | `mean_reversion` | `sp500` | `bull_risk_on` | 1.3720 | 77 | 10.7% | 2026-05-06 |
| 6 | `momentum_breakout` | `commodity_etfs` | `bull_risk_on` | 1.3160 | 556 | 13.7% | 2026-05-06 |
| 7 | `short_term_mr` | `sp500` | `bull_risk_on` | 1.2781 | 292 | 24.9% | 2026-05-06 |
| 8 | `momentum_breakout` | `commodity_etfs` | `cross` | 1.2140 | 585 | 22.3% | 2026-05-07 |
| 9 | `momentum_breakout` | `sp500` | `bull_risk_on` | 1.2031 | 232 | 18.4% | 2026-05-06 |
| 10 | `connors_rsi2` | `sp500` | `bull_risk_on` | 1.1504 | 269 | 28.5% | 2026-05-06 |

## 4. Recent Transitions & Promotions

*Source: strategy_lifecycle_history table*

| Strategy | Universe | Transition | Date | Reason |
|----------|----------|------------|------|--------|
| `short_term_mr` | `sp500` | `RESEARCH` → `PAPER` | 2026-05-06 | Phase B dogfood activation 2026-05-06: paper-trading rollout |
| `connors_rsi2` | `commodity_etfs` | `—` → `LIVE` | 2026-05-06 | Migration: pre-existing live strategy at lifecycle rollout 2 |
| `connors_rsi2` | `gold_etfs` | `—` → `LIVE` | 2026-05-06 | Migration: pre-existing live strategy at lifecycle rollout 2 |
| `connors_rsi2` | `sp500` | `—` → `LIVE` | 2026-05-06 | Migration: pre-existing live strategy at lifecycle rollout 2 |
| `mean_reversion` | `commodity_etfs` | `—` → `LIVE` | 2026-05-06 | Migration: pre-existing live strategy at lifecycle rollout 2 |
| `mean_reversion` | `sector_etfs` | `—` → `LIVE` | 2026-05-06 | Migration: pre-existing live strategy at lifecycle rollout 2 |
| `momentum_breakout` | `commodity_etfs` | `—` → `LIVE` | 2026-05-06 | Migration: pre-existing live strategy at lifecycle rollout 2 |
| `momentum_breakout` | `sector_etfs` | `—` → `LIVE` | 2026-05-06 | Migration: pre-existing live strategy at lifecycle rollout 2 |
| `momentum_breakout` | `sp500` | `—` → `LIVE` | 2026-05-06 | Migration: pre-existing live strategy at lifecycle rollout 2 |
| `adx_trend_pullback` | `sp500` | `—` → `RESEARCH` | 2026-05-06 | Migration: research-discovered strategy at lifecycle rollout |

*Source: data/promotion_log.json (auto-promotion runs)*

| Strategy | Universe | Paper Sharpe | Research Sharpe | Date |
|----------|----------|-------------|-----------------|------|
| `clean_strategy` | `sp500` | 0.6198 | 0.6500 | 2026-05-11 |
| `clean_strategy` | `sp500` | 0.6198 | 0.6500 | 2026-05-11 |
| `clean_strategy` | `sp500` | 0.6198 | 0.6500 | 2026-05-11 |
| `clean_strategy` | `sp500` | 0.6198 | 0.6500 | 2026-05-08 |
| `clean_strategy` | `sp500` | 0.6198 | 0.6500 | 2026-05-07 |

## 5. Research Integrity Check

**19 contaminated file(s) found:**
  - research/best/inside_bar_nr7.json
  - research/best/sector_rotation.json
  - research/best/triple_rsi.json
  - research/best/adx_trend_pullback.json
  - research/best/dividend_capture.json
  - research/best/keltner_reversion.json
  - research/best/donchian_breakout.json
  - research/best/connors_rsi2_commodity_etfs.json
  - research/best/williams_percent_r.json
  - research/best/momentum_breakout.json
  - research/best/mean_reversion_sector_etfs.json
  - research/best/lower_band_reversion.json
  - research/best/connors_rsi2_gold_etfs.json
  - research/best/demark_sequential.json
  - research/best/bb_squeeze.json
  - research/best/connors_rsi2_sector_etfs.json
  - research/best/trend_following.json
  - research/best/momentum_breakout_sector_etfs.json
  - research/best/stochastic_oversold.json

*research_best rows with non-portfolio metric_type: 20*

| Strategy | Universe | Metric Type | Sharpe |
|----------|----------|-------------|--------|
| `adx_trend_pullback` | `sp500` | `legacy_portfolio` | 0.4423 |
| `adx_trend_pullback` | `sp500` | `unknown` | 0.9517 |
| `adx_trend_pullback` | `sp500` | `unknown` | 0.5793 |
| `bb_squeeze` | `crypto` | `unknown` | 0.0000 |
| `bb_squeeze` | `sp500` | `legacy_portfolio` | 0.4859 |
| `bb_squeeze` | `sp500` | `unknown` | 0.6868 |
| `bb_squeeze` | `sp500` | `unknown` | 0.4859 |
| `connors_rsi2` | `commodity_etfs` | `both` | 0.9681 |
| `connors_rsi2` | `commodity_etfs` | `unknown` | 1.4978 |
| `connors_rsi2` | `gold_etfs` | `both` | 0.9865 |


