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

## Phase 2 — Atlas adapter + replace validate_oos (IN PROGRESS)
- [ ] research/cross_oos/adapter.py:
      - equity_curve -> per-period return series
      - trades -> per-ticker / per-sector daily PnL attribution
      - regime labels from benchmark (SPY) series via splitters.regime_labels
      - config grid -> (obs x config) PBO matrix (reuse perturbation grid)
      - ATLAS_DEFAULT_GATES (equities-tuned thresholds)
      - run_cross_oos_battery(result, config, market, grid_results) -> bundle+gates
- [ ] Rewrite scripts/validate_oos.py around the battery as authoritative verdict,
      keeping legacy keys as derived projections (back-compat contract above)
- [ ] Adapter unit tests (synthetic edge PASS / noise FAIL; back-compat keys present)
- [ ] End-to-end smoke run against config/active/sp500.json; verify promotion-gate
      tool still parses the artifact
- [ ] Update atlas-backtest skill doc to describe the new battery

## Phase 3+ (follow-up, not now)
- [ ] Migrate Python consumers + TS extensions to native cross_oos schema; drop shim
- [ ] Pre-registration template + comparative scorecard generator
- [ ] Optional dedicated atlas_jobs_run job `validate_cross_oos`

## Review
(to be filled in after Phase 2)
