# Atlas Cross-OOS Validation Battery — Port from Midas

Replace the hand-rolled 3-test OOS suite (`scripts/validate_oos.py`) with the
Midas-style strategy-agnostic cross-OOS battery (CPCV / PBO / DSR / multi-axis
splitters / regime / declarative gates), reusing Atlas's existing BacktestEngine.

## Decision: REPLACE (not additive)
The cross-OOS battery becomes the **authoritative** validation logic and verdict.
The legacy JSON keys are retained as **derived projections** so the existing
consumers keep working without a simultaneous TS recompile:
- `research/promoter.py`  → reads test1/test2 + overall_verdict
- `scripts/auto_reoptimize.py` → reads test1_time_period_split
- `scripts/research_runner.py`, `scripts/research_promote.py`
- TS `atlas_risk_check_reopt_promotion` → reads
  test1_time_period_split.out_of_sample.{sharpe,profit_factor},
  test1.degradation_pct.cagr_pct, test2_perturbation.robust,
  test3_walkforward_consistency.window_analysis.win_rate_windows_pct,
  summary.overall_verdict (== "PASS")
- TS `atlas_artifacts_summarize` kind=validate_oos

## Phase 1 — Port the battery (DONE)
- [x] Copy 5 modules + __init__ + 6 tests into research/cross_oos/
- [x] Verify 32 tests green under Atlas stack (np 2.4.2 / pd 3.0.1 / scipy 1.17.1)
- [x] Re-docstring __init__ to Atlas context

## Phase 2 — Atlas adapter + replace validate_oos (DONE)
- [x] research/cross_oos/adapter.py: daily_returns, group_daily_pnl (ticker/regime),
      leave_one_ticker_group_out (5 seeded groups), top_group_frac, regime_attribution
      (uses each trade's entry_regime), build_pbo_matrix, assemble_bundle, evaluate,
      ATLAS_DEFAULT_GATES (equities-tuned, 252-day annualisation, cost-stress gate dropped)
- [x] Rewrite scripts/validate_oos.py: cross-OOS battery is the authoritative verdict;
      legacy keys (test1/test2.robust<-PBO+DSR/test3/summary.overall_verdict) retained as
      derived projections (test1+test3 are REAL; test2.robust projected). --grid-size added.
- [x] Adapter unit tests (7) + smoke test of validate_oos orchestration (back-compat
      contract asserted) — 40/40 cross_oos tests green; promoter_oos_floors test green.
- [x] Real-engine integration verified against config/active/sp500.json (momentum_breakout):
      310 trades, regimes stratify, CPCV median 0.64 / 93% paths +ve / LOO ok.
- [x] FIXED pre-existing bug: make_strategies() hardcoded 4 strategies (none enabled in the
      live config) -> 0 trades / meaningless validation. Now driven by STRATEGY_REGISTRY so
      ANY enabled strategy is validated ("backtest more strategies in a similar way").
- [x] Update atlas-backtest skill doc to describe the new battery

## Phase 3+ (follow-up, not now)
- [ ] Migrate Python consumers + TS extensions to native cross_oos schema; drop shim
- [ ] Pre-registration template + comparative scorecard generator
- [ ] Optional dedicated atlas_jobs_run job `validate_cross_oos`

## Review (Phase 1 + 2)

**Shipped.** Atlas now has the Midas-style strategy-agnostic cross-OOS battery as the
authoritative OOS validator, reusing the existing BacktestEngine.

- Phase 1: ported research/cross_oos (cpcv, overfitting, splitters, metrics, gates) +
  32 tests verbatim. Pure functions; green under np2.4.2/pd3.0.1/scipy1.17.1. (commit 6e66b1e6)
- Phase 2: adapter.py bridges BacktestResult -> battery; validate_oos.py rewritten around it.
  Verdict = declarative gate table (missing==FAIL). Legacy JSON keys kept as derived
  projections => zero consumer/TS-extension breakage (no recompile needed this phase).
- Bonus fix: make_strategies() is now registry-driven, repairing OOS validation for the
  live momentum_breakout config (previously 0 trades).

**Tests:** 40 cross_oos tests + existing promoter_oos_floors pass. One unrelated pre-existing
failure (test_canary_promote_top3: research-DB solo_sharpe state, needs a re-sweep/migration).

**Divergences from Midas (documented):** 252-day annualisation; cross-VENUE axis replaced by
leave-one-ticker-group-out; 10bps cost-stress gate dropped (Atlas runs net-of-fees).

**Next (Phase 3, not started):** migrate Python consumers + TS extensions to the native
cross_oos schema and drop the shim; PRE_REGISTRATION template + comparative scorecard;
optional dedicated validate_cross_oos job. Tune ATLAS_DEFAULT_GATES thresholds after a few
real runs (current values are reasonable defaults, not calibrated to a target hit-rate).
