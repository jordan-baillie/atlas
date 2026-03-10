# Atlas Research Knowledge Base

> **Auto-generated:** 2026-03-10 05:23 UTC | **Experiments:** 41 unique | **Waves:** 5
>
> This is the AI agent's internal knowledge base. Read this file at session start.
> Regenerate: `python3 scripts/build_obsidian_vault.py --force`

---

## 1. System State

### Active Config (v2.2)
- **Equity:** $4,000 | **Broker:** Moomoo (PAUSED) → Alpaca (PLANNED, $0 commission)
- **Strategies:** Mean Reversion + Trend Following + Opening Gap
- **Max positions:** 10 | **SMA-200 filter:** ON | **Universe:** SP500 (292 tickers)

### Baseline Performance (v2.2, $10K implied)
Sharpe 1.04 | CAGR 15.7% | 425 trades | 56% WR | PF 1.50 | Max DD ~12%
OOS: Sharpe 1.23, 108 trades | Perturbation: 0/10 negative | Walk-forward: 76% profitable

### Status
- **Live trading:** PAUSED (conflict + fee drag)
- **Research pipeline:** ACTIVE (cron-driven)
- **Mode:** Research-first — accumulate evidence for go-live

---

## 2. Strategy Report Cards

### ✅ Active Strategies

#### Mean Reversion
- **Metrics:** Best Sharpe: 0.64 | Worst: -2.10 | 9 experiments (1 pass, 6 fail)
- **Key findings:**
  - RSI(14) is clearly optimal for MR on SP500 — confirmed empirically
  - DO NOT try RSI period optimization again — this is definitive
  - max_hold=5 promotion BLOCKED until OOS can be properly executed

#### Opening Gap
- **Metrics:** Best Sharpe: 0.62 | 2 experiments (0 pass, 1 fail)
- **Key findings:**
  - PATTERN: OG generates very few solo trades on SP500 with current filters

#### Trend Following
- **Metrics:** Best Sharpe: 0.62 | 2 experiments (0 pass, 1 fail)
- **Key findings:**
  - SAME PATTERN: Solo strategy on $4K equity shows negative Sharpe due to fee drag

### ⏸️ Dormant Strategies (passed solo, failed combined)

#### Bollinger Band Squeeze
- **Metrics:** Best Sharpe: -0.38 | Worst: -1.68 | 2 experiments (1 pass, 0 fail)
- **Key findings:**
  - PATTERN: All 3 dormant strategies tried so far are individually marginal after optimization

#### ConnorsRSI2
- **Metrics:** Best Sharpe: -2.63 | 1 experiments (0 pass, 1 fail)
- **Key findings:**
  - ConnorsRSI2 generates 249 trades with 57% WR — signal quality decent
  - But PF=0.78: losses larger than wins. ATR(3.0x) stop gives wide risk while SMA(5) exit captures small gains
  - Edge not statistically significant (p=0.27)
  - ALSO HAD CODE BUG: calc_position_size returns dict, code compared dict <= 0
  - Fixed: pos_result["shares"] extraction. Does not change backtest outcome (only affected expensive stocks)

#### Consecutive Down Days
- **Metrics:** 1 experiments (0 pass, 1 fail)
- **Key findings:**
  - CDD strategy missing check_exits() abstract method — incomplete implementation
  - Fix code before re-queuing

#### Lower Band Reversion
- **Metrics:** Best Sharpe: 0.71 | Worst: -2.08 | 5 experiments (1 pass, 2 fail)
- **Key findings:**
  - KEY INSIGHT: Filters are strategy-dependent. SMA-200 is not universally beneficial.

#### Momentum Breakout
- **Metrics:** Best Sharpe: 0.30 | Worst: -0.99 | 3 experiments (2 pass, 1 fail)
- **Key findings:**
  - Momentum breakout generates 342 trades with 48.5% WR — sufficient signal activity for viability
  - Untuned default params produce negative Sharpe (-0.99) but meet relaxed solo criteria (trades>10, WR>35%, PF>0.7)
  - Strategy is viable for optimization phase — signal exists even if untuned defaults are unprofitable
  - All 5 params changed during optimization
  - Shorter breakout lookback (10 vs 20) works better

#### MTF Momentum
- **Metrics:** Best Sharpe: 0.00 | 1 experiments (0 pass, 1 fail)
- **Key findings:**
  - ROOT CAUSE: confidence=0.50 hardcoded in mtf_momentum.py, filtered by min_confidence=0.75 in config
  - BLOCKED: Do not re-queue until confidence calculation is implemented

#### Sector Rotation
- **Metrics:** Best Sharpe: 0.43 | Worst: -0.11 | 3 experiments (1 pass, 1 fail)
- **Key findings:**
  - DECISION: Worth sending to optimization phase despite partial verdict
  - PATTERN CONFIRMED (4th time): dormant strategies fail combined test due to position contention
  - Root cause: max_open_positions=10 creates zero-sum competition for position slots
  - DECISION: Wave 1 dormant activation theme is CLOSED

#### Short Term MR
- **Metrics:** Best Sharpe: 0.27 | Worst: -0.45 | 3 experiments (2 pass, 1 fail)
- **Key findings:**
  - PATTERN: Both dormant strategies fail the combined test due to position allocation contention

#### Triple RSI
- **Metrics:** Best Sharpe: -2.12 | 1 experiments (0 pass, 1 fail)
- **Key findings:**
  - Confirmed: dormant strategies need combined-mode test first, not solo

### 🔧 Portfolio Filters

#### Combined Portfolio
- **Metrics:** Best Sharpe: 0.75 | Worst: 0.62 | 3 experiments (1 pass, 2 fail)
- **Key findings:**
  - KEY INSIGHT: MR buys oversold stocks during panic, which is the high-VIX regime
  - CLOSED: Do not re-test VIX filters on combined portfolio

#### Portfolio Filter
- **Metrics:** Best Sharpe: -0.64 | Worst: -1.04 | 4 experiments (2 pass, 1 fail)
- **Key findings:**
  - Promoted to /root/atlas/config/versions/asx_v9.3.json
  - Promoted to /root/atlas/config/versions/sp500_v2.1.json

#### SMA-200 Filter
- **Metrics:** Best Sharpe: 0.87 | 1 experiments (1 pass, 0 fail)
- **Key findings:**
  - SMA-200 filter promoted to SP500 active config v2.1

---

## 3. Confirmed Patterns (NEVER violate)

1. **Fee Drag at Low Equity** — At $4K, Moomoo fees eat 74% of profit. Switch to Alpaca or raise equity.
2. **ETF→Stock Adaptation Fails** — ConnorsRSI2 & LBR fail on stocks. Never port ETF strategies directly.
3. **Position Slot Contention** — At max_positions=10, adding any strategy degrades portfolio. Need allocation pools.
4. **Solo vs Combined Divergence** — Solo=viability check only. Combined=promotion decision.
5. **Optimizer Blind Spots** — Coord descent rejected SMA-200 (trade count penalty). Run manual A/B tests for binary decisions.

---

## 4. Research Waves

| Wave | Theme | Experiments | Key Outcome |
|------|-------|-------------|-------------|
| 1 | Dormant Strategy Activation & Portfolio Filters | 17 | SMA-200 promoted (+0.28 Sharpe), all dormant fail combined, VIX filter CLOSED |
| 2 | Enhanced Mean Reversion Alpha — Connors RSI(2) strategy + volume filter promotion + exit optimization | 6 | ConnorsRSI2 fails, exit sweep inconclusive, infra bugs in filter tests |
| 3 | New strategy: Triple RSI + MR alpha stacking — high-conviction signals and entry filter optimization | 5 | RSI(14) optimal, IBS/vol redundant, max_hold=5 promising (p=0.30) |
| 4 | New strategy: Lower Band Reversion — IBS-based mean reversion with published 2.11 Sharpe edge | 7 | LBR fails (ETF→stock), MR hold5 OOS fails, MR strength exit inferior |
| 5 | Full Portfolio Reoptimization + Consecutive Down Days — Maximize Returns and Add Uncorrelated Alpha | 6 | Queued: re-optimization + CDD strategy |

---

## 5. Closed Decisions (Do NOT revisit)

1. ✅ SMA-200 filter ON for all strategies (promoted v2.1)
2. ❌ VIX filter: counterproductive for MR-heavy portfolio
3. ❌ RSI period: RSI(14) is optimal, shorter periods produce random entries
4. ❌ ETF strategy adaptation: don't port ETF strategies to stocks
5. ❌ Wave 1 dormant activation: CLOSED until allocation pools exist
6. ⏸️ max_hold=5 for MR: failed OOS, needs more evidence
7. ⏸️ Volume 1.5x filter: needs combined-mode retest (fix infra bug first)

---

## 6. Infrastructure Issues

| Issue | Status | Impact |
|-------|--------|--------|
| filter_test nested config paths | OPEN | Volume combined & TOM tests invalid |
| MTF Momentum confidence hardcoded=0.50 vs min=0.75 | OPEN | Strategy blocked |
| OG overly selective filters (9 trades) | OPEN | Can't test OG exits |
| Universe stale (200 vs 292 tickers) | OPEN | data/processed/sp500/universe.json |
| OpenD runs in tmux not systemd | OPEN | Reliability risk |

---

## 7. Next Research Priorities

1. **Wave 5 execution** — Re-optimization + CDD strategy (queued)
2. **Allocation pools** — Critical blocker for unlocking dormant strategies
3. **Volume filter combined test** — Fix nested config bug, retest 1.5x
4. **OG filter relaxation** — More trades needed before exit testing
5. **Alpaca integration** — Eliminate fee drag for live trading
6. **Backfill Wave 4 learnings** — 7 experiments with empty learnings

---

## 8. All Learnings by Strategy

Searchable index of every learning extracted from experiments.

### Bollinger Band Squeeze
- BB Squeeze is viable: 322 trades, 45% WR, PF 0.74 with default params
- Clearly unprofitable untuned (Sharpe -1.68, CAGR -12.3%) but signal generates enough trades
- Passed to optimization phase
- BB Squeeze improved dramatically: Sharpe -1.68 → -0.38, PF 0.74 → 1.04
- Best params: bb_period=25, bb_std=1.5 (both changed from defaults)
- But PF 1.04 still below 1.1 threshold, Sharpe still negative
- Near breakeven after optimization is not good enough for portfolio addition
- BB Squeeze on SP500 with current implementation is likely not viable
- PATTERN: All 3 dormant strategies tried so far are individually marginal after optimization

### Combined Portfolio
- VIX filter is counterproductive for this portfolio mix
- Mean reversion thrives during high-VIX (panic) periods — blocking entries there destroys alpha
- All 4 VIX thresholds tested (20/25/30/35) degrade Sharpe
- KEY INSIGHT: MR buys oversold stocks during panic, which is the high-VIX regime
- VIX filter might work for trend-only portfolio but not MR-heavy one
- CLOSED: Do not re-test VIX filters on combined portfolio
- Coordinate descent post-SMA200 yields +0.13 Sharpe — confirms reopt hypothesis
- MR RSI period optimized 14→5; sma200_filter disabled for MR and OG (counterintuitive)
- OG was completely broken at baseline (score -999), now viable at 14.12
- Trade count improved 101→124, good for statistical reliability
- NEEDS OOS VALIDATION before promotion
- Pools are no-op with 3 strategies + 15 max positions — no contention
- Pools only matter when 4+ strategies compete for limited slots

### ConnorsRSI2
- ConnorsRSI2 generates 249 trades with 57% WR — signal quality decent
- But PF=0.78: losses larger than wins. ATR(3.0x) stop gives wide risk while SMA(5) exit captures small gains
- Edge not statistically significant (p=0.27)
- ALSO HAD CODE BUG: calc_position_size returns dict, code compared dict <= 0
- Fixed: pos_result["shares"] extraction. Does not change backtest outcome (only affected expensive stocks)
- HYPOTHESIS REJECTED: RSI(2) solo is unprofitable with current params on SP500 at $4K equity
- POSSIBLE RETRY: With tighter stop (1.5-2x ATR) and higher equity, risk-reward may improve

### Consecutive Down Days
- CDD strategy missing check_exits() abstract method — incomplete implementation
- Fix code before re-queuing

### Lower Band Reversion
- LBR with published SPY params on individual SP500 stocks: Sharpe -2.08, 270 trades, 58% WR, PF 0.85
- Win rate is decent (58%) but average loss exceeds average win — classic ETF-to-stock adaptation issue
- Edge not statistically significant (p=0.25)
- Published Sharpe 2.11 on SPY → -2.08 on individual stocks: dramatic degradation confirms ETF strategies don't transfer
- Relaxing IBS from 0.3 to 0.5 slightly improved Sharpe (-2.08→-1.85) and PF (0.85→0.90)
- Trade count stable at 280 (vs 270) — relaxation adds few extra signals
- WR improved to 59.6% but edge still not significant (p=0.49)
- Minor improvement insufficient to make strategy viable — problem is deeper than parameter tuning
- Band multiplier sweep (1.5x-4.0x): band_mult=3.5 produces anomalously high PF (4.38) with Sharpe 0.71
- Likely overfitting: $19K PnL on 249 trades smells like a few lucky outsized wins
- Monte Carlo test confirms fragility: p95 MC drawdown > 2× actual (trade-sequence dependent)
- All other band values produce negative Sharpe — no robust parameter exists
- Edge not significant at any band level (p=0.01-0.55 range, but MC fragile flags invalidate the low p-values)
- CONCLUSION: LBR band parameter cannot rescue the strategy on individual stocks
- IBS threshold sweep (0.1-0.6): no value produces significant edge (all p>0.25)
- Best Sharpe at IBS=0.4 (-1.21) with highest WR (62.4%) but PF only 1.07
- Higher IBS thresholds increase trade count but don't improve edge quality
- Published IBS=0.3 is not optimal for stocks — but no IBS value works
- CONCLUSION: IBS parameter cannot rescue LBR on individual stocks
- COUNTERINTUITIVE: Removing SMA-200 filter IMPROVES LBR — Sharpe -2.08→-1.44, PnL -$174→-$5
- With SMA-200 OFF: 283 trades, 59% WR, PF 1.00 (near breakeven vs clearly negative with filter)
- SMA-200 filter hurts LBR because LBR targets extreme dips — which often occur below the 200-day MA
- This is the OPPOSITE of what SMA-200 does for MR/TF/OG (where it helps by +0.28 Sharpe)
- KEY INSIGHT: Filters are strategy-dependent. SMA-200 is not universally beneficial.

### Mean Reversion
- 1.5x avg volume filter on MR: Sharpe jumps from -0.02 to 0.38 (massive)
- Mechanism: higher volume entries = more institutional participation = better follow-through
- PF 1.30 → 1.62 (23% improvement), DD 5.24% → 4.03% (1.2pp reduction)
- Trade count 332 → 235 (29% reduction) — acceptable for the quality improvement
- 2x avg is too aggressive: only 115 trades, Sharpe drops to -0.30
- 0.5x/0.8x/1.0x show minimal improvement — 1.5x is the threshold where quality jumps
- NEXT: Test 1.5x volume filter on combined portfolio (all strategies)
- All max_hold_days values produce negative Sharpe in SOLO MR mode
- max_hold_days=10 is relatively best (-1.98), shorter holds are worse
- Current default (15) is slightly worse than 10 (-2.08 vs -1.98)
- NOTE: Solo MR on $4K has inherent negative Sharpe due to fee drag
- Relative ranking useful: 10 > 15 > 7 > 5 > 3 for max_hold_days
- NEEDS COMBINED TEST: solo param sweep is misleading at this equity level
- IBS filter is redundant with RSI+vol in combined mode — adds no alpha
- IBS < 0.15 crashes performance (kills 10% of trades, all good ones)
- Baseline is stable: Sharpe=0.608, 101 trades, CAGR=27.8%
- Volume min_ratio filter has zero effect on MR — never triggers
- MR signals naturally occur on high-vol days (oversold conditions correlate with volume spikes)
- Remove vol_surge from MR acceptance criteria — it adds nothing
- RSI(14) is clearly optimal for MR on SP500 — confirmed empirically
- Shorter RSI periods increase trade count but destroy quality (more false positives)
- RSI(5) and RSI(3) produce near-random entries (Sharpe < -1.5)
- DO NOT try RSI period optimization again — this is definitive
- max_hold=5 beats max_hold=10: Sharpe +0.035, CAGR +3.1pp, PF 4.55 vs 3.64
- MR trades resolve quickly — 5-day hold captures most reversion, longer holds add noise
- max_hold=15 is catastrophic — trades that havent reverted by day 10 are losers
- Promising but needs OOS confirmation before promotion (p=0.30)
- OOS validation of max_hold=5 FAILED: zero OOS trades generated
- Walk-forward OOS window likely too short or parameter configuration prevented trade generation
- In-sample still looks good: Sharpe 0.87, PF 8.16, 53.6% WR — but can't validate out-of-sample
- max_hold=5 promotion BLOCKED until OOS can be properly executed
- POSSIBLE FIX: Extend OOS window or use different validation method (e.g., time-series cross-validation)
- LBR-style strength exit (sell when close > yesterday's high) applied to MR: Sharpe -2.10, 68 trades
- Dramatic trade count reduction (101→68) — exit triggers too early, cutting profitable trades short
- PF barely above 1.0 (1.03) — essentially random after applying this exit rule
- Edge not significant (p=0.88) — the worst p-value of any MR experiment
- CONCLUSION: Simple 'strength exit' is inferior to max_hold_days for MR. Price-based exits add noise, not alpha.
- MR profit target has negligible impact on combined portfolio — noise-level

### Momentum Breakout
- Momentum breakout generates 342 trades with 48.5% WR — sufficient signal activity for viability
- Untuned default params produce negative Sharpe (-0.99) but meet relaxed solo criteria (trades>10, WR>35%, PF>0.7)
- Strategy is viable for optimization phase — signal exists even if untuned defaults are unprofitable
- All 5 params changed during optimization
- Shorter breakout lookback (10 vs 20) works better
- Tighter stops (2.0x vs 3.5x ATR) dramatically improve results
- Longer trend filter (150 vs 50 SMA) eliminates false breakouts
- Momentum breakout solo is modestly profitable after optimization (Sharpe 0.30, CAGR 8.0%)
- But adding it to the active portfolio (MR+TF+OG) HURTS performance dramatically
- Combined Sharpe drops from 0.59 to -0.16, DD increases from 6.6% to 16.5%
- The 460 breakout trades compete with MR/TF signals for the 10 max positions
- Breakout strategy may work better with a separate position allocation

### MTF Momentum
- ROOT CAUSE: confidence=0.50 hardcoded in mtf_momentum.py, filtered by min_confidence=0.75 in config
- 3rd failure — first 2 were API signature bugs (fixed), this is the remaining blocker
- Strategy generates hundreds of signals but none pass confidence gate
- FIX: Add dynamic confidence scoring or per-strategy min_confidence override
- BLOCKED: Do not re-queue until confidence calculation is implemented

### Opening Gap
- Only 9 trades across entire backtest — insufficient for ANY conclusion
- All max_hold_days variants produce essentially identical results (9 trades each)
- PATTERN: OG generates very few solo trades on SP500 with current filters
- SMA-200 filter + gap threshold + RSI < 25 + volume surge = very selective
- Need to relax one filter (remove RSI or lower gap threshold) for more trades before testing exits
- OG generates <10 trades — gap_threshold parameter is meaningless
- OG needs reopt params to become active

### Portfolio Filter
- Promoted to /root/atlas/config/versions/asx_v9.3.json
- Promoted to /root/atlas/config/versions/sp500_v2.1.json
- INFRASTRUCTURE FAILURE: All 3 volume variants produced near-identical results (116/117/111 trades)
- filter_test sets s_cfg['volume_min_ratio'] but strategies read from s_cfg['volume']['min_ratio'] (nested path)
- Need to fix filter_test to handle nested config params before retesting volume filter
- The hypothesis is NOT rejected — test was invalid due to config path mismatch
- Wave 1 solo result (1.5x volume filter: Sharpe -0.02→0.38) remains valid and promising
- INFRASTRUCTURE FAILURE: All 3 TOM variants produced near-identical results (116/77/115 trades)
- filter_test sets s_cfg['turn_of_month'] but no strategy reads this parameter
- TOM filter needs to be IMPLEMENTED in the backtest engine or strategy base class before testing
- Calendar-based filters need engine-level support (check date against TOM window before signal generation)
- The hypothesis is NOT rejected — test was invalid due to missing implementation

### Sector Rotation
- Sector rotation is now functional — generates 251 trades vs 0 previously
- PF 1.24 suggests alpha signal exists but untuned parameters drag Sharpe negative
- Edge p-value 0.13 — not statistically significant, optimization may fix this
- Strategy needs code review: may need rebalance-aware position management
- DECISION: Worth sending to optimization phase despite partial verdict
- Sector rotation viable solo after optimization: Sharpe 0.43, CAGR 9.6%, PF 1.48
- Fewer sectors (2) + tighter stops (2.5x ATR) + longer holds (30d) is optimal
- Edge is statistically significant (p=0.015)
- PATTERN CONFIRMED (4th time): dormant strategies fail combined test due to position contention
- All 4 tested (momentum_breakout, short_term_mr, bb_squeeze, sector_rotation) degrade portfolio when added
- Root cause: max_open_positions=10 creates zero-sum competition for position slots
- SR crowds out MR (-34%) and TF (-45%) — both proven profit drivers
- DECISION: Wave 1 dormant activation theme is CLOSED
- NEXT PRIORITY: Position allocation pools (per-strategy-type max positions) to unlock new strategies

### Short Term MR
- Short-term MR generates 946 trades — highest trade count of any dormant strategy tested
- 58.6% WR suggests signal quality, but PF 0.96 means losses slightly exceed wins
- Massive trade count (946) will create severe slot contention in combined portfolio at max_positions=10
- Viable for optimization — high trade count gives optimizer plenty of data to tune parameters
- Optimization improved Sharpe from -0.45 to +0.27 — significant improvement
- Post-optimization: 697 trades, 63% WR, PF 1.17, CAGR 7.6%
- Trade count reduced 946→697 (26%) through optimization — still very high
- PF improvement 0.96→1.17 shows optimizer found genuine edge in parameter space
- Short-term MR is profitable solo after optimization (Sharpe 0.27, CAGR 7.6%, 63% WR)
- But adding it to the active portfolio degrades Sharpe by 0.29 and CAGR by 2.4pp
- The 697 STMR trades compete with MR/TF for 10 max positions
- With both MR variants active, the portfolio is over-concentrated in mean reversion signals
- PATTERN: Both dormant strategies fail the combined test due to position allocation contention
- Future work: test with increased max_open_positions or separate allocation pools per strategy type

### SMA-200 Filter
- SMA-200 filter promoted to SP500 active config v2.1
- Applied to all 3 active strategies: mean_reversion, trend_following, opening_gap
- Trades reduced 443→270 but quality improvement overwhelms quantity loss
- Human approved via Telegram promotion request

### Trend Following
- Tighter stops (2.0x ATR) slightly better than wider (3.5-4.0x)
- Difference is marginal: -1.09 vs -1.17 Sharpe (all negative in solo mode)
- Current default (2.5x) is near-optimal based on this sweep
- 77 trades across all variants — consistent trade count means stop width only affects P&L per trade
- SAME PATTERN: Solo strategy on $4K equity shows negative Sharpe due to fee drag
- TF trailing stop 3.5 marginally better than 3.0, wider stops worse — confirms Wave 4

### Triple RSI
- Triple RSI (RSI + streak + acceleration) fails on SP500 solo — too restrictive
- Sharpe -2.12 is beyond salvage via optimization
- Confirmed: dormant strategies need combined-mode test first, not solo

---

*For detailed experiment data, see `Experiments/{id}.md`. For strategy details, see `Strategies/{name}.md`.*