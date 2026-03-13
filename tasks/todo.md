# v3.0 Multi-Strategy Portfolio — Implementation Plan

## Phase 1: Fix Foundations
- [ ] Fix TF best params bug (fast_ma=50 > slow_ma=20 inverted)
- [ ] Re-run portfolio optimizer with corrected TF params

## Phase 2: Build Candidate Config v3.0
- [ ] Create config v3.0 with:
  - 6 strategies enabled (OG, MB, MR, CR2, SR, STMR) + TF at optimal weight
  - Best params from research/best/
  - max_open_positions raised to 10 (6 strategies need room)
  - Allocation pools enabled with weight-proportional position caps

## Phase 3: Validation Backtest
- [ ] Run full walk-forward backtest: v3.0 vs v2.2 baseline
- [ ] Verify Sharpe, drawdown, trade count, per-strategy contribution

## Phase 4: Fix Promotion Pipeline
- [ ] Ensure sweep runs all enabled strategies (reset staleness)
- [ ] Ensure promoter handles multi-strategy configs correctly
- [ ] Ensure portfolio optimizer runs periodically and updates weights
- [ ] Connect portfolio weight updates to config promotion

## Phase 5: Go Live
- [ ] Promote v3.0 if validation passes
- [ ] Verify plan generation with new config
- [ ] Update memory/SUMMARY.md
