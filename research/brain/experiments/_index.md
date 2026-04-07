# Experiments Index

> Last 100 experiments (newest first). Older experiments pruned.

| ID | Strategy | Parameter | Result | Sharpe Δ |
|----|----------|-----------|--------|----------|
| wave5_tf_trail_sweep | ? | TF trailing_stop_atr_mult 3.5 slightly better than 3.0 | discarded | n/a (migrated) |
| wave5_pool_toggle | ? | Allocation pools should not degrade current 3-strategy portfolio | discarded | n/a (migrated) |
| wave5_og_gap_sweep | ? | OG gap_threshold sweep — current -0.02 may be suboptimal | discarded | n/a (migrated) |
| wave5_mr_profit_sweep | ? | MR profit_target_atr_mult sweep should find better exit level | discarded | n/a (migrated) |
| wave5_full_reopt | ? | Post-SMA200 reoptimization should find better params since all were tuned without SMA200 | kept | n/a (migrated) |
| wave5_cdd_solo | ? | CDD captures short-term reversal on large-cap stocks | discarded | n/a (migrated) |
| wave4_mr_strength_exit | ? | The LBR published exit rule (sell when close > yesterday high) captures the first sign of strength recovery. Testing this on existing MR strategy as an alternative to the current profit-target + mean-reversion exit. Expected: faster exits, higher win rate, possibly lower avg profit per trade. | discarded | n/a (migrated) |
| wave4_mr_hold5_oos | ? | Wave 3 found max_hold=5 beats max_hold=10 (Sharpe +0.035, CAGR +3.1pp, PF 4.55 vs 3.64). OOS validation needed before promotion. MR trades resolve quickly — 5-day hold captures most reversion, longer holds add noise. | discarded | n/a (migrated) |
| wave4_lbr_solo_relaxed | ? | Relaxing IBS threshold from 0.3 to 0.5 generates more trades on individual stocks (which have wider IBS distributions than SPY). Tests if the band signal alone carries enough edge without strict IBS filtering. | discarded | n/a (migrated) |
| wave4_lbr_solo | ? | The Quantitativo IBS lower-band strategy (Sharpe 2.11 on SPY) can generate profitable signals when adapted to individual SP500 stocks. Published params: range_lookback=25, high_lookback=10, band_mult=2.5, IBS<0.3, exit on close>prev_high. | discarded | n/a (migrated) |
| wave4_lbr_no_sma200 | ? | SMA-200 filter was a clear win for existing MR (+0.28 Sharpe). But LBR specifically targets extreme dips — which often occur in downtrends. Testing if removing SMA-200 captures more deep-dip opportunities that still revert quickly. Published strategy on SPY used SMA-300 as improvement. | kept | n/a (migrated) |
| wave4_lbr_ibs_sweep | ? | IBS threshold controls signal quality vs quantity. Published used 0.3 for SPY. Individual stocks have different IBS distributions. Testing 0.1-0.6 to find optimal threshold that maximizes risk-adjusted returns. | discarded | n/a (migrated) |
| wave4_lbr_band_sweep | ? | Band multiplier controls selectivity: wider band = fewer but deeper dips. Published used 2.5x. Testing 1.5-4.0x to find optimal trade-off between trade frequency and signal quality on individual stocks. | discarded | n/a (migrated) |
| wave3_vol_sweep | ? | Higher volume threshold for MR entries improves trade quality. Wave 1 proved 1.5x volume on MR solo: Sharpe -0.02→0.38, PF 1.30→1.62. Wave 2 combined test FAILED due to infrastructure bug (nested params). This experiment uses full volume dict sweep to bypass the nested param issue. Expect 1.5x to be optimal in combined mode too. | discarded | n/a (migrated) |
| wave3_trsi_solo | ? | Triple RSI (RSI(5) declining 3 days, below 30, with lookback check) generates rare but high-conviction mean reversion signals on individual SP500 stocks. Published edge on SPY: 90% WR, PF 4.0. Adapted for individual stocks with SMA-200 filter and volume confirmation. Expects fewer but higher-quality trades than existing MR strategy. | discarded | n/a (migrated) |
| wave3_rsi_period | ? | Web research (Triple RSI, Connors, Alvarez) consistently shows RSI(2-5) outperforming RSI(14) for mean reversion signals. Our MR uses RSI(14). Shorter RSI periods may improve entry timing. Combined-mode sweep gives realistic portfolio-level impact unlike unreliable solo sweeps (lesson #30). | discarded | n/a (migrated) |
| wave3_ibs_sweep | ? | Requiring low IBS (close near day's low) for MR entries improves signal quality. Alvarez research shows IBS < 25 gives 58% avg gain improvement on RSI(2) strategy. Our MR has ibs_max=1.0 (disabled). Testing restrictive thresholds should filter out weak MR signals. | discarded | n/a (migrated) |
| wave3_hold_combined | ? | Wave 2 tested max_hold_days in SOLO mode (all negative Sharpe due to fee drag at $4K). Relative ranking showed 10 > 15 > 7 > 5 > 3. Combined-mode sweep gives realistic absolute Sharpe. Short holds (3-5d) may reduce time risk; longer holds (10-15d) capture more reversion. | discarded | n/a (migrated) |
| wave2_vol_combined | ? | Wave 1 proved 1.5x volume filter on MR solo: Sharpe -0.02→0.38, PF 1.30→1.62. Applying to the full combined portfolio should similarly improve signal quality by filtering out low-conviction entries across all strategies. | discarded | n/a (migrated) |
| wave2_tom_filter | ? | The Turn of Month effect (last 5 + first 3 trading days) shows stocks generate virtually all monthly returns in this window (Lakonishok & Smidt 1988, confirmed 2024). Boosting signal confidence during TOM window (or suppressing signals mid-month) should improve trade quality. This is a calendar-based filter, completely uncorrelated with our price-based signals. | discarded | n/a (migrated) |
| wave2_rsi2_solo | ? | Connors RSI(2) with SMA-200 filter generates profitable mean reversion signals | discarded | n/a (migrated) |
| wave2_exit_og | ? | Shorter hold periods better match gap-fill resolution timeline | discarded | n/a (migrated) |
| wave2_exit_mr | ? | Shorter hold periods improve MR risk-adjusted returns | discarded | n/a (migrated) |
| wave2_chandelier_tf | ? | Wider trailing stop ATR multiplier captures more trend profit | discarded | n/a (migrated) |
| ar-20260402_160629 | ? | max_hold_days | kept | +0.0147 |
| ar-20260402_160610 | ? | atr_stop_mult | kept | +0.0429 |
| ar-20260402_160232 | ? | rsi_period | kept | +0.1216 |
| ar-20260402_160036 | ? | max_hold_days | kept | +0.0147 |
| ar-20260402_160016 | ? | atr_stop_mult | kept | +0.0429 |
| ar-20260402_155642 | ? | rsi_period | kept | +0.1216 |
| ar-20260402_155524 | ? | max_hold_days | kept | +0.0147 |
| ar-20260402_155443 | ? | atr_stop_mult | kept | +0.0429 |
| ar-20260402_155118 | ? | rsi_period | kept | +0.1216 |
| ar-20260402_154942 | ? | max_hold_days | kept | +0.0557 |
| ar-20260402_154849 | ? | atr_stop_mult | kept | +0.1034 |
| ar-20260402_154613 | ? | rsi_period | kept | +0.3000 |
| ar-20260402_154524 | ? | max_hold_days | kept | +0.0557 |
| ar-20260402_154429 | ? | atr_stop_mult | kept | +0.1034 |
| ar-20260402_154149 | ? | rsi_period | kept | +0.3000 |
| ar-20260402_151351 | ? | max_hold_days | kept | +0.0557 |
| ar-20260402_151257 | ? | atr_stop_mult | kept | +0.1034 |
| ar-20260402_151019 | ? | rsi_period | kept | +0.3000 |
| ar-20260402_150624 | ? | atr_stop_mult | kept | +0.4826 |
| ar-20260402_143337 | ? | signal_mode | kept | +0.1234 |
| ar-20260402_143037 | ? | atr_stop_mult | kept | +0.3953 |
| ar-20260402_140940 | ? | atr_stop_mult | kept | +0.1542 |
| ar-20260402_140803 | ? | breakout_period | kept | +0.0115 |
| ar-20260316_173747 | ? | max_hold_days | kept | +0.0326 |
| ar-20260316_165857 | ? | rsi_period | kept | +0.0431 |
| ar-20260316_161158 | ? | sma200_filter | kept | +0.0321 |
| ar-20260316_142314 | ? | atr_stop_mult | kept | +1.7520 |
| ar-20260316_135643 | ? | atr_stop_mult | kept | +0.0259 |
| ar-20260316_133154 | ? | nr_lookback | kept | +0.0744 |
| ar-20260316_105145 | ? | max_hold_days | kept | +0.0313 |
| ar-20260316_101102 | ? | adx_period | kept | +1.9172 |
| ar-20260316T230337 | ? | ? | ? | ? |
| ar-20260315_214936 | ? | atr_mult | kept | +0.0986 |
| ar-20260315_211051 | ? | band_mult | kept | +0.6508 |
| ar-20260315_204050 | ? | wr_period | kept | +0.1568 |
| ar-20260315_165945 | ? | wr_entry | kept | +0.0173 |
| ar-20260315_162439 | ? | sma200_filter | kept | +0.2324 |
| ar-20260315_155610 | ? | max_hold_days | kept | +0.1843 |
| ar-20260315_152802 | ? | setup_bars | kept | +1.4958 |
| ar-20260315_133718 | ? | wr_period | kept | +1.4529 |
| ar-20260315_130504 | ? | stoch_entry | kept | +0.2133 |
| ar-20260315_123146 | ? | max_hold_days | kept | +0.1618 |
| ar-20260315_105913 | ? | atr_stop_mult | kept | +0.3455 |
| ar-20260315_102607 | ? | ema_period | kept | +5.5845 |
| ar-20260315_095241 | ? | atr_stop_mult | kept | +0.1345 |
| ar-20260314_215610 | ? | atr_stop_mult | kept | +0.1422 |
| ar-20260314_212128 | ? | decline_days | kept | +0.2497 |
| ar-20260314_204821 | ? | rsi_entry | kept | +0.0380 |
| ar-20260314_170336 | ? | rsi_period | kept | +4.3510 |
| ar-20260314_162839 | ? | atr_stop_mult | kept | +0.0110 |
| ar-20260314_160010 | ? | ibs_threshold | kept | +0.5505 |
| ar-20260314_153036 | ? | wr_period | kept | +1.3176 |
| ar-20260314_134040 | ? | band_mult | kept | +0.0359 |
| ar-20260314_131333 | ? | sma200_filter | kept | +0.4820 |
| ar-20260314_123633 | ? | entry_period | kept | +0.0252 |
| ar-20260314_103939 | ? | max_hold_days | kept | +0.4254 |
| ar-20260314_101112 | ? | atr_stop_mult | kept | +0.4687 |
| ar-20260314_093630 | ? | atr_stop_mult | kept | +0.1199 |
| ar-20260314_005747 | ? | wr_period | kept | +0.8678 |
| ar-20260314_002602 | ? | stoch_period | kept | +0.4601 |
| ar-20260313_235755 | ? | max_hold_days | kept | +0.2044 |
| ar-20260313_233330 | ? | atr_stop_mult | kept | +0.4337 |
| ar-20260313_215942 | ? | entry_period | kept | +0.0329 |
| ar-20260313_213426 | ? | max_hold_days | kept | +0.3198 |
| ar-20260313_210717 | ? | atr_stop_mult | kept | +1.2304 |
| ar-20260313_203414 | ? | adx_period | kept | +0.2015 |
| ar-20260313_162404 | ? | ema_touch_pct | kept | +0.1150 |
| ar-20260313_155952 | ? | adx_threshold | kept | +0.0391 |
| ar-20260312_213643 | ? | adx_threshold | kept | +0.0833 |
| ar-20260312_204612 | ? | rsi_oversold | kept | +0.0938 |
| ar-20260312_203132 | ? | max_hold_days | kept | +0.0426 |
| ar-20260312_170031 | ? | ema_touch_pct | kept | +0.1271 |
| ar-20260312_124147 | ? | rsi_period: 10→14 | kept | n/a (migrated) |
| ar-20260312_110553 | ? | sma200_filter: None→False | kept | n/a (migrated) |
| ar-20260312_104004 | ? | atr_stop_mult: None→1.5 | kept | n/a (migrated) |
| ar-20260312_101555 | ? | adx_period: None→7 | kept | n/a (migrated) |
