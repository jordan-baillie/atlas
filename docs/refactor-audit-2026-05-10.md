# Atlas Refactor Audit — 2026-05-10

Audit window: 2026-05-11. Read-only. Scoring rubric: 5 axes × 1–5 each (sum 5–25).

Axes: **ROI** (simplification benefit) · **Blast** (inverse — isolated=5) · **Tests** (coverage=5) · **Decoupling** (from in-flight #215/#267/#276/#284) · **Effort** (swarmable=5).

## Top 10 Candidates (ranked)

### #1 — Archive dormant `research/sweep.py` legacy sweeper
**Location:** `research/sweep.py` (1,444 LOC), `research/autoresearch_runner.py` (canonical replacement, 1,343 LOC)
**Score:** 23/25 (ROI:5, Blast:5, Tests:4, Decoupling:5, Effort:4)
**Current state:** Two parallel sweep implementations. `sweep.py` is invoked only by `scripts/autoresearch.py:407`, which targets `atlas-autoresearch.service` (status: `not-found`/`inactive` — dead). Production `atlas-research-window@<universe>.timer` routes through `autoresearch_nightly.py` → `autoresearch_runner.py`. Only live dependency is `PARAM_GRIDS` (lines 238–444) imported by `tests/test_dsr_param_grids.py:29`.
**Proposed refactor:** Move `sweep.py` → `research/archive/`. Relocate `PARAM_GRIDS` to new `research/param_grids.py`. Update the one test import. Confirm `_test_combined()` (L538–577) is covered by autoresearch_runner's combined-verify stage before removal.
**Estimated savings:** ~1,444 LOC deleted, ~80 LOC relocated. 3–5 files touched.
**Acceptance criteria:** `pytest tests/test_dsr_param_grids.py research/tests/test_sweep_universe.py` passes (test_sweep_universe will need patch-target update OR archival alongside sweep.py). Manually run one `research_window_universe.sh sp500` cycle to confirm autoresearch_runner unaffected.
**Swarm scoping:** 1 builder. Single concern, low parallelism benefit.

### #2 — Delete dead `backtest/index.py` filesystem scanner
**Location:** `backtest/index.py` (447 LOC), one smoke-import line in `tests/test_pipeline_steps.py:682`
**Score:** 23/25 (ROI:3, Blast:5, Tests:5, Decoupling:5, Effort:5)
**Current state:** CLI tool scanning `backtest/results/*.json` for experiment indexing. Zero production callers. Functionality replaced by `research_experiments` SQLite table (21,245 rows). `backtest/__init__.py` does not import it.
**Proposed refactor:** Delete the file and the single smoke-import line.
**Estimated savings:** 447 LOC, 1 file deleted + 1 line removed.
**Acceptance criteria:** `pytest tests/test_pipeline_steps.py` passes; `python -c "import backtest"` succeeds; grep confirms zero remaining references.
**Swarm scoping:** 1 builder, trivial.

### #3 — Dedup `_find_latest_snapshot` between `research/loop.py` and `research/autoresearch_nightly.py`
**Location:** `research/loop.py:520–563` (44 LOC), `research/autoresearch_nightly.py:87–103` (17 LOC)
**Score:** 21/25 (ROI:2, Blast:5, Tests:4, Decoupling:5, Effort:5)
**Current state:** Identical algorithm in two places (filter `data/snapshots/` by market name substring, sort by mtime, return newest). Already diverged: loop.py has richer error message, nightly is stripped copy. Will keep drifting (loop.py: 11 commits, nightly: 4 commits since Apr-15).
**Proposed refactor:** Extract to `research/snapshots.py:find_latest_snapshot(market)`. Both callers import from there. Keep loop.py's richer docstring.
**Estimated savings:** ~17 LOC eliminated, drift risk gone.
**Acceptance criteria:** `tests/test_research_session_snapshot_fallback.py` (6 tests) passes unchanged.
**Swarm scoping:** 1 builder, 5-minute change.

### #4 — Consolidate 4 shadow OHLCV builders in tests/
**Location:** `tests/conftest.py` (canonical `make_ohlcv_df`, L460–499). Shadows: `tests/test_chart_intel_enhanced.py:17` (`_make_df`), `tests/test_fill_checks.py:50` (`_make_ohlcv`), `tests/test_halt_on_stale_nyse.py:23` (`_make_df`), `tests/test_ohlcv_dual_write_all_universes.py:67` (`_make_ohlcv_df`)
**Score:** 21/25 (ROI:2, Blast:5, Tests:4, Decoupling:5, Effort:5)
**Current state:** Four private OHLCV builders in test files, none enforce the OHLCV invariant (`high >= max(open, close)`) as rigorously as conftest's canonical version. Drift between them already visible.
**Proposed refactor:** Extend `conftest.make_ohlcv_df()` with optional `flat_price` / `dates` params; replace 4 private helpers with imports from conftest.
**Estimated savings:** ~80 LOC of duplicated helpers eliminated.
**Acceptance criteria:** All 4 affected test files pass; spot-check generated DataFrames satisfy `high >= max(open, close)` and `low <= min(open, close)`.
**Swarm scoping:** 1 builder OR 4 parallel builders (one per shadow file) since each shadow is independent.

### #5 — Move indicator functions from `utils/helpers.py` into `indicators/`
**Location:** `utils/helpers.py` (496 LOC, holds `calc_rsi`/`calc_atr`/`calc_zscore`/`calc_volume_ratio`/`calc_wvf`/`calc_ibs` ≈ 91 LOC), `indicators/` package (currently only `vol_cones.py`, with empty `__init__.py`). 9 strategy files import via `from utils.helpers import calc_atr, calc_rsi, ...`
**Score:** 20/25 (ROI:3, Blast:4, Tests:4, Decoupling:5, Effort:4)
**Current state:** Architecture is inverted — `indicators/` is near-empty, `utils/` holds the actual indicators. Misleading to new maintainers; agents grep `indicators/` and find nothing.
**Proposed refactor:** Create `indicators/technical.py` with the 6 `calc_*` functions. Update 9 strategy imports. Leave `utils/helpers.py` with date/format/sizing utilities only.
**Estimated savings:** ~91 LOC moved, ~18 import lines updated. No LOC reduction but major navigability win.
**Acceptance criteria:** All strategies generate-signal/check-exit unchanged numerically (snapshot a one-day backtest before/after, compare). `tests/test_strategies.py` and `tests/test_vol_cones.py` pass unchanged.
**Swarm scoping:** 1 builder (mechanical, single atomic commit) OR 2 builders (1 move + import update, 1 verifies via backtest snapshot diff).

### #6 — Relocate `signals/ev_scorer.py` (it's analytics, not a signal)
**Location:** `signals/ev_scorer.py` (195 LOC); 3 late-import callers: `services/api/dashboard.py:192–203`, `services/api/risk.py:342–347`, `scripts/compute_daily_risk.py:175–177`
**Score:** 20/25 (ROI:1, Blast:5, Tests:4, Decoupling:5, Effort:5)
**Current state:** Module queries historical closed trades, computes bootstrap-CI win rates/profit factors, writes to `signal_ev` table. Has no `generate_signal()`. Not consumed by `overlay/engine.py` or any signal pipeline. Pure reporting/analytics misfiled in `signals/`.
**Proposed refactor:** Move to `analytics/strategy_ev.py` (or `research/ev_scorer.py`). Update 3 late-import paths. No logic changes.
**Estimated savings:** No LOC reduction; correct domain classification.
**Acceptance criteria:** Dashboard EV endpoint still returns data; `scripts/compute_daily_risk.py` runs without import error.
**Swarm scoping:** 1 builder, file move + 3 path updates.

### #7 — Extract `_iter_my_positions()` + `_atr_stop()` helpers into `BaseStrategy`
**Location:** `strategies/base.py` (245 LOC), 9 strategy files in `strategies/` (4,279 LOC total)
**Score:** 20/25 (ROI:4, Blast:3, Tests:4, Decoupling:4, Effort:5)
**Current state:** Identical 7-line guard block at the top of every `check_exits()` across 8 strategies (filter by strategy name, lookup ticker df, skip if empty/missing). Identical ATR-stop formula `entry_price - self.atr_stop_mult * current_atr` repeated 11 times across 8 files. `BaseStrategy` already has `_get_held_tickers()`/`_count_positions()`/`_has_sufficient_data()` — pattern is established.
**Proposed refactor:** Add `_iter_my_positions(data, positions)` generator and `_atr_stop(entry_price, atr)` helper to `BaseStrategy`. Rewrite each strategy's exit loop to use the generator; replace 11 inline ATR-stop expressions with helper calls.
**Estimated savings:** ~56 LOC + 11 inline expressions; consistent logging and skip-semantics across strategies.
**Acceptance criteria:** `tests/test_strategies.py` (683 LOC) passes unchanged. Snapshot a 30-day backtest run before/after; numerical results identical to the cent. Verify no strategy silently changed which positions it owns/skips.
**Swarm scoping:** 2–3 builders, split by strategy file (no overlap). Builder A: base.py + 3 strategies. Builder B: 3 strategies. Builder C: 3 strategies. Single integration verifier merges.

### #8 — Factory for triple-isolation fixtures in `tests/conftest.py`
**Location:** `tests/conftest.py` (952 LOC; ~285 LOC of repeated triples for 8 resources)
**Score:** 19/25 (ROI:4, Blast:4, Tests:3, Decoupling:3, Effort:5)
**Current state:** 8 production artifacts (prod_db, halt_file, state_dir, live_portfolio_state, reconcile_positions_state, chat_db, price_arbiter, reconcile_shadow) each have an identical 3-fixture pattern: session-scope isolate, function-scope autouse isolate, session-end pollution verify. 4 of these triples were added in commits since Apr-15 (#284 and emergency P0 pollution patches) — adding ad-hoc as new bugs surface.
**Proposed refactor:** `make_file_isolation_fixtures(module_path, attr, session_tmp_name, prod_path, extra_attrs?)` factory returning all 3 fixtures. Each resource → ~4-line registration call.
**Estimated savings:** ~195 LOC removed (~20% of conftest.py).
**Acceptance criteria:** **Full pytest suite passes with zero new failures.** Run with `--collect-only` first to confirm no fixture-name collision. Confirm session-end pollution checks still fire (introduce a deliberate write to prod path in a test, expect the assertion to fail).
**Swarm scoping:** 1 builder. Conftest is too central to parallelize safely. **NOTE — Task #284 work landed Apr 25–28 on 3 of these resources (chat_db, price_arbiter, reconcile_shadow); flag for user confirmation that #284 is fully settled before refactoring.**

### #9 — Collapse `_precomputed` dual-path branches across 6 strategies
**Location:** `strategies/connors_rsi2.py`, `strategies/opening_gap.py`, `strategies/mean_reversion.py`, `strategies/short_term_mr.py`, `strategies/trend_following.py`, `strategies/momentum_breakout.py`, `strategies/base.py`. 31 total `if self._precomputed: read_column else: recalculate` branches.
**Score:** 19/25 (ROI:4, Blast:3, Tests:4, Decoupling:4, Effort:4)
**Current state:** Every strategy has 2–8 inline `if self._precomputed` branches inside generate/exit loops. The `else` branch is production dead-code (backtest engine always calls `precompute()` first) but kept for test compatibility. `BaseStrategy` declares `_precomputed` but provides no lookup helper.
**Proposed refactor:** Add `_get_indicator(df, col_name, fallback_fn)` to `BaseStrategy` — reads precomputed column if present, calls `fallback_fn()` otherwise. Collapses 31 branch blocks → 31 single-line calls.
**Estimated savings:** ~150 LOC; uniform precomputed semantics.
**Acceptance criteria:** Snapshot backtest before/after, numerical equivalence to the cent. `tests/test_strategies.py:235,245,258,266,273,280` (which exercise both branches) continues to pass.
**Swarm scoping:** 2 builders, split by strategy file. Pairs nicely with #7 (same files, same coverage surface); recommend landing them in one swarm.

### #10 — Finish telegram wrapper consolidation (`#PERF-TG-CONSOLIDATE`)
**Location:** `research/autoresearch_runner.py:482` (`_try_send_telegram`, 12 LOC), `research/llm_loop_runner.py:297` (`_send_telegram`, 15 LOC). Both have active TODO ticket.
**Score:** 19/25 (ROI:2, Blast:4, Tests:3, Decoupling:5, Effort:5)
**Current state:** Two near-identical `try: get_alert_manager().send() except: logger.warning()` wrappers; previous consolidation attempt stalled. Complex formatters in `autoresearch_nightly.py:308` and `discovery/discovery.py:443` are deliberately kept.
**Proposed refactor:** Delete the 2 simple wrappers. Inline the try/except at 3 call sites (`autoresearch_runner` L632, L1079; `llm_loop_runner` L340). Close TODO `#PERF-TG-CONSOLIDATE`.
**Estimated savings:** ~27 LOC.
**Acceptance criteria:** Trigger a research run; confirm telegram message arrives (or, in test env, that import-failure path is exercised silently).
**Swarm scoping:** 1 builder, 5-minute change.

## Excluded — and why

- **`signals/sector_rotation.py` + `signals/etf_flows.py` SPDR-constants dedup (D2):** `DEFENSIVE_ETFS` has a **live behavioral divergence** (2 members vs 3 — XLV included in one). `overlay/engine.py` imports `DEFENSIVE_ETFS`. Touching this changes overlay behavior → **conflicts with Task #215 stable observation baseline**. Defer until #215 closes.
- **`signals/` private DB SQL loaders (D5):** Same overlay coupling — `overlay/engine.py` consumes these signal outputs. Refactor would alter cache-freshness characteristics (7-day JSON → parquet pipeline). Defer until #215 closes.
- **`research/loop.py` 4-way split (R3):** Tempting (1,157 LOC, 4 concerns, 15+ import sites) BUT it's the **highest-churn research file** at 11 commits since Apr-15 — actively evolving across all 4 concerns simultaneously. Merge-conflict probability is too high right now. Re-evaluate in 2–4 weeks once churn calms.
- **`backtest/engine.py` method extraction (R4):** Hot code path on every backtest. Zero unit tests on the target methods (`_simulate_day` 301 LOC, `run_walkforward` 393 LOC) — integration tests only. Refactor verification would require running multiple full backtests for numerical-equivalence proof. Effort fit for swarm is poor (single class, internal restructure).
- **`data/ingest.py` split (D1):** 1,603 LOC across 5 concerns, but 5 commits since Apr-15 (NYSE calendar fix, AlertManager migration, freshness check). Active churn raises rebase risk. Worth doing eventually with the shim approach; not now.
- **`universe/builder.py` market-cap caching (D6):** P1.1 universe isolation fix landed in the last 2 weeks (3 commits). Wait for that to settle before adding a new cache layer.
- **Auto-remediation test consolidation (~2,670 LOC across 3 files):** All 3 files added in commits since Apr-15; Phase 3 enabled days ago. Right move, wrong time. Defer.
- **`ChangeStateModal.tsx` 976-LOC mega-component split:** UI only, no live trading impact, but requires deep design (state machine for universe vs strategy scope, modal-state hooks) — not mechanically parallelizable. Lower priority than the wins above.
- **`config/schema.py`:** Already clean. 363 LOC of flat DSL tuples + 5 helpers, zero commits since Apr-15. No refactor warranted.

## Summary recommendation

**Pick #1 (sweep.py archive)** — 1,444 LOC deleted, dead code path proven, single-builder swarm, biggest absolute simplification on the audit.

**Pick #2 (backtest/index.py delete)** — pure 447-LOC delete with zero callers. Highest velocity / risk ratio. Ideal warm-up swarm.

**Pick #7+#9 paired (BaseStrategy helpers + `_precomputed` collapse)** — same 6–9 strategy files, same test surface (`test_strategies.py`), ~200 LOC saved combined, swarmable across 2–3 builders splitting by file. Single integration check verifies numerical equivalence. This is the highest-clarity-gain refactor on the live-adjacent code without touching execution.

Optional warm-up batch (all <30 min, very low risk): **#3 + #4 + #6 + #10** can land as a single small-PR sweep when convenient — ~165 LOC saved cumulatively, zero blast radius.
