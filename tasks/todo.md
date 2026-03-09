# Atlas Research Ecosystem — Build Plan

## Goal
Shift Atlas from live-trading focus to **research-first** mode. Build a self-contained document ecosystem of all research tests, results, and metrics that accumulates knowledge over time. When ready to trade again, decisions will be driven by this body of evidence.

## Phase 1: Consolidate Existing Knowledge

- [ ] **1.1 Backfill missing learnings on 20 experiment envelopes**
  - 20/34 experiments have empty `learnings` arrays
  - Read each experiment's outputs + verdict, write 2-3 bullet learnings
  - These are the building blocks for the knowledge base

- [ ] **1.2 Backfill delta_vs_baseline on journal entries**
  - Only 17% have delta_vs_baseline populated
  - For param_sweep and combined experiments, calculate delta vs active config
  
- [ ] **1.3 Create `research/KNOWLEDGE.md` — the master findings document**
  - Strategy-by-strategy section: what was tested, what worked, what failed, why
  - Organized by strategy, not by wave (waves are execution, knowledge is cumulative)
  - Cross-reference experiment IDs for traceability
  - Include: confirmed patterns, dead ends, open questions, fee analysis
  
- [ ] **1.4 Create `research/STRATEGY_CARDS.md` — one-page per strategy**
  - For each of the 13 strategies: hypothesis, parameters tested, best result, verdict
  - Include: optimal params, market regime sensitivity, fee sensitivity, OOS status
  - Living document updated after each wave

## Phase 2: Improve Research Infrastructure

- [ ] **2.1 Add fee-sensitivity analysis to backtest output**
  - Run each backtest at $0, $1.10, $2.20 commission levels
  - Record `fee_breakeven_equity` — minimum equity where strategy is profitable
  - Critical for knowing when to activate each strategy at a given capital level

- [ ] **2.2 Add regime-tagged metrics to experiment output**
  - Tag each trade with market regime (trending/mean-reverting/volatile)
  - Report per-regime Sharpe/WR so we know WHEN each strategy works
  - Uses existing VIX/breadth data

- [ ] **2.3 Standardize experiment metrics**
  - 71 different metric names across experiments — many are duplicates/variants
  - Define canonical metric set: sharpe, cagr, max_dd, win_rate, profit_factor, 
    total_trades, avg_trade, expectancy_r, edge_p_value, calmar, sortino
  - Migrate old experiments to use canonical names

- [ ] **2.4 Add strategy correlation matrix to weekly reports**
  - Which strategies' trades overlap? Which are truly uncorrelated?
  - Critical for portfolio construction when going live

## Phase 3: Run Pending Research

- [ ] **3.1 Execute Wave 5 (10 queued experiments)**
  - Track A: Full reoptimization post-SMA200 (6 experiments)
  - Track B: Consecutive Down Days new strategy (4 experiments)
  - Already queued, just needs the daily research cron to run

- [ ] **3.2 Re-run base backtest at Alpaca fee levels ($0 commission)**
  - The $10K/$4K fee analysis showed Moomoo kills the edge
  - Re-run with $0 commission to see the "true" edge at $4K equity
  - This becomes the reference baseline for Alpaca go-live

- [ ] **3.3 Multi-equity-level backtest sweep**
  - Run at $2K, $4K, $10K, $25K, $50K starting equity
  - Shows how edge scales with capital and at what level each strategy becomes viable
  - Record results in KNOWLEDGE.md

## Phase 4: Expand Research Scope

- [ ] **4.1 Regime-conditional strategy activation**
  - Hypothesis: activate TF in trending regimes, MR in mean-reverting
  - Backtest with regime switching vs always-on
  - If positive: add to live config as optional mode

- [ ] **4.2 Walk-forward re-optimization cadence study**
  - How often should parameters be re-optimized? Monthly? Quarterly?
  - Run walk-forward with different re-opt frequencies
  - Determines the maintenance burden for live trading

- [ ] **4.3 Portfolio-level research**
  - Allocation pools (TF:5, MR:5, OG:3) — Wave 5 already queued
  - Max positions sweep (5, 10, 15, 20)
  - Risk-per-trade sweep (0.25%, 0.5%, 1.0%, 2.0%)

## Non-Goals (explicitly paused)
- Live trading execution
- Alpaca account funding
- Real-time monitoring/alerts for positions
- Dashboard real-time updates
