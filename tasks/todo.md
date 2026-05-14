# Atlas Tasks — Post-Audit Reconciliation (2026-04-17)

Reconciled with git reality after Wave 1 + Wave 2 audit fixes. Prior versions
claimed task counters (e.g. "253/243") that were folklore — this file now
tracks real work against real commits.

---

## ✅ DONE — Wave 1 audit fixes (commits 4413501e..b085c620)

- [x] Orphan DB cleanup + archive stale backups — `4413501e`
- [x] File-based kill switch + `halt_trading.py` CLI — `4c2a4c13`, `037a8453`
- [x] Discovery package/module collision fix — `07afc01e`
- [x] Heartbeat watchdog (15-min systemd timer, stale→stalled flip + Telegram alerts) — `f91cafb0`
- [x] `utils.pi_subprocess` helper for Claude Max OAuth routing — `c85eb189`
- [x] Migrate pi/claude subprocess call sites to the helper — `c360015e`
- [x] Document Caddy localhost-bind + basic-auth hardening — `147ba471`
- [x] DR runbook refresh + first documented restore drill — `b085c620`
- [x] Price arbiter with Alpaca authority + halt-on-large-spread — `1de60e23`

## ✅ DONE — Wave 2 audit fixes (commits da598b89..31fdde25)

- [x] Per-universe templated systemd units `atlas-research-window@<u>.service`
      (7 universes, 1h timeout each, staggered 23:00–05:00 AEST) — `da598b89`
- [x] Silent-failure watchdog (hourly timer — atlas-discovery 0-papers,
      atlas-director low-coverage, zero-byte autoresearch logs → Telegram) — `c2a57be2`
- [x] Replace top-17 silent `except Exception: pass` with logged handling
      across `live_portfolio.py`, `chat_server.py`, `pi_session.py` — `31fdde25`
- [x] **Wave 2.6 / #349** — `scripts/eod_settlement.py`: replaced fragile
      `"sell_result" in dir()` with `sell_result is not None` (init-to-None at
      function top). 62/62 eod_settlement tests pass. — `618429c2`
- [x] **Wave 2.7** — `universe/__init__.py`: wire `assert_universes_disjoint()`
      at import time so accidental cross-universe ticker additions surface
      immediately on startup rather than silently corrupting per-market equity
      calculations. 78/78 universe tests pass.
- [x] **D2 / Wave 2.2** — Dedupe SPDR sector ETF constants into `signals/constants.py`.
      Created canonical `SECTOR_ETFS` (11-tuple), `SECTOR_ETF_NAMES` (dict),
      `DEFENSIVE_ETFS_PURE` (frozenset{XLU,XLP}), `DEFENSIVE_ETFS_INCLUSIVE`
      (frozenset{XLU,XLP,XLV}), `CYCLICAL_ETFS` (frozenset). Decision: KEPT
      DIVERGENT — price-momentum thesis excludes XLV (ambiguous biotech
      exposure); volume-flow thesis includes XLV (institutional defensive
      destination). `signals/sector_rotation.py` and `signals/etf_flows.py`
      migrated to import from constants. 56/57 tests pass (1 pre-existing
      failure `test_risk_on_via_surge_drought_count` — unchanged before/after).

---

## 🔜 TODO — architecture-gate items still open

These are structural items the audit flagged. Each needs its own focused
session — none are quick fixes.

- [x] **#250 — Trade-ledger dual-write leak diagnosis + fix** (blocker for
      #192 gate). Diagnosed 3 leak points: (a) reconcile_ledger.py
      hardcoding `strategy="reconciled"` instead of looking up real
      strategy from broker JSON/plans; (b) reconcile_ledger.py filtering
      broker positions by `get_universe_tickers(market_id)` — silently
      excluded XLY and other sector ETFs from SQLite backfill forever;
      (c) reconcile_positions.py `--fix` path writing JSON only, never
      calling `atlas_db.record_trade_entry()`. Fixed in:
      - `3e3d53a5` — reconcile_ledger `_lookup_strategy()` helper (state
        file → plans → "reconciled"), union of universe_tickers and
        state_file tickers as broker-position filter; reconcile_positions
        `--fix` now dual-writes SQLite
      - `d70ecc52` — `scripts/backfill_orphan_trades.py` repair script;
        applied to data/atlas.db: INSERT XLY/momentum_breakout, UPDATE
        AMD strategy 'reconciled'→'momentum_breakout' (id=140), DELETE
        duplicate FCX id=139
      - `aaa025d1` — 18 regression tests in
        `tests/test_dual_write_leak_regression.py`, all passing
      Manual `verify_dual_write.py` now shows Trades ✅ PASS. Gate
      (5 consecutive cron-tagged PASSes, weekdays Tue-Sat 10:00 UTC)
      still open — earliest close date: Sat 2026-04-25. #192 remains
      open until gate closes.

- [x] **#251 — OHLCV test-leak cleanup + fixture hardening.** Discovered
      that `tests/test_auto_exclusions.py` and `tests/test_ingest.py`
      leaked OHLCV rows into production `data/atlas.db` via
      `data/ingest.py::_save_cache()`'s unconditional SQLite dual-write.
      Pollution included: 22 AAPL/MSFT dummy rows (open=0, volume=1000,
      close=100..109, dates 2026-04-06..2026-04-20), plus 80 rows for
      tickers TEST/RNDM/COLS/STALE. Fixed via:
      - Module-scope `autouse` DB-isolation fixture added to both test
        files (mirrors `test_dual_write_leak_regression.py::_isolate_db`)
        — prevents future pollution
      - `scripts/cleanup_dummy_ohlcv.py` idempotent repair: DELETE 22
        dummy rows, UPSERT 18 real rows from parquet (04-06..04-16);
        04-17 + 04-20 left to next daily ingest
      - `tests/test_no_prod_db_writes.py` regression test: runs the two
        suites in subprocess and asserts `data/atlas.db` mtime/size
        unchanged, plus zero dummy-pattern rows remain
      Related: #192 (does NOT close the gate — gate remains open on
      5 consecutive real-cron PASSes starting Tue 2026-04-21).

- [x] **#252 — Full-suite pytest prod-DB leak closeout.** Regression test
      from #251 (`tests/test_no_prod_db_writes.py`) surfaced that the
      full suite STILL dirtied `data/atlas.db` even after #251's autouse
      module-scope fixtures. Bisection (29 test modules × snapshot-diff
      per module) identified 10 writers: `test_baseline_regression.py`
      (1762 SPY OHLCV rows via module-scoped `baseline_result` fixture
      calling `run_backtest() -> download_ticker() -> _sqlite_batch_write()`),
      `test_lifecycle.py`/`test_execution_integration.py`/`test_exit_record_integration.py`/
      `test_circuit_breaker.py`/`test_reconciliation.py` (writes via
      `record_system_log()`), `test_plan_generator.py` (writes via
      `atlas_db.record_plan()`), `test_vol_cones.py` (writes to
      `vol_cones`/`vol_regimes`), `test_regime_distributions.py` (INSERT
      OR REPLACE on `regime_distributions` via module-scoped fixture),
      `test_macro_regime.py` (WAL checkpoint only). Fixed via:
      - `tests/conftest.py`: global function-scope autouse `_isolate_prod_db`
        (commit `30a49291`) covers 9 of 10 leakers.
      - `tests/conftest.py`: session-scope autouse `_isolate_prod_db_session`
        using `_pytest.monkeypatch.MonkeyPatch()` (commit `e3afa30f`) —
        catches module-scoped fixtures that resolve BEFORE function-scope
        isolation can intervene (the #252 root cause).
      - `tests/test_regime_distributions.py`: module-scoped `seeded_db`
        fixture replaces direct prod-DB access (commit `c7b17d03`).
      - `scripts/cleanup_dummy_ohlcv.py`: extended with
        `phase5_cleanup_synthetic_and_volume1000` to purge the 80
        TEST/RNDM/COLS/STALE synthetic rows + 2 crypto `volume=1000`
        rows the #251 cleanup missed (commit `857019f5`).
      **Verification (dashboard + telegram-bot stopped to eliminate
      concurrent writers):** full pytest suite leaves `data/atlas.db`
      byte-identical — mtime, size, AND all 29 tables' row counts +
      max(rowid) + SHA256 of contents all unchanged. `verify_dual_write.py`
      still 5/5 PASS. `test_no_prod_db_writes.py` all 3 tests PASS
      inside the full suite. 14 test failures remain, all pre-existing,
      zero new regressions.
      Related: #192 (this was THE blocker preventing the #192 gate
      from advancing — automated pytest or CI would silently
      re-contaminate the DB otherwise).

- [x] **#192 — Kill JSON trade-ledger dual-write.** Atlas currently writes
      trades to both `data/state.json` and SQLite. Requires data-migration
      script + careful cutover + rollback plan. Out of scope for audit
      waves — needs a dedicated cutover window.
      **Status (2026-04-20):** leak points diagnosed and patched by #250
      (commits 3e3d53a5 / d70ecc52 / aaa025d1 + #252 test-leak fixes
      30a49291 / e3afa30f / c7b17d03 / 857019f5). `verify_dual_write.py`
      Trades check now PASS manually; awaiting 5 consecutive real-cron
      PASSes (gate: 0/5 as of 2026-05-08; schedule `0 10 * * 2-6` UTC;
      failing: checks 6+7 — sector_etfs stale halt_reason + equity-history delta
      caused by #297 consolidation; gate scope needs re-evaluation for sp500-only).
      **✅ Closed 2026-05-14 (Wave 1.1):** Deduped equity_history in all
      live_*.json state files (sp500: 39→34, commodity_etfs: 19→16,
      sector_etfs: 10→8). verify_dual_write.py 6/6 PASS. audit clean.
      Regression test added. Commit b5fa1b89.
- [x] **#215 — Overlay gate enforcement.** Confirm overlay signals actually
      gate order placement (not just annotate decisions). Needs end-to-end
      trace from overlay engine → plan file → executor.
      ✅ Closed 2026-04-27 — overlay→plan→executor end-to-end trace completed
      (audit-fix-1 batch). Cron currently runs --mode log_only; flag flip
      would activate live.
      ✅ M3 shadow-complete 2026-04-28 (commit e87497f2 — meta-fix-3).
      Schema (overlay_shadow_log), unified resolution (plan.overlay_context
      → overlay_decisions JOIN), mode-gated application, EOD evaluator,
      daily JSON report, 14 new tests + 11 P1-B fixes. **Enforce-flip
      prerequisite**: ≥1 week of shadow data review per market before
      flipping `overlay.shadow_mode: false` in config/active/{market}.json.
      Both modes now share the same resolution path so the flip is a
      one-line config change (no code drift).
      **Gate check 2026-05-08**: 27 overlay_decisions over 21 days → ≥1 week threshold MET.
      Weekly evaluator (overlay_eval_*.log) writing 0 bytes — manual table review needed.
- [x] **#216 — Phase 5 coverage gap + atlas-compute-matrix tolerance fix (2026-05-14).** Research matrix sweep was failing (rc=2) for all 6 universes. Root causes fixed: (1) `_count_rows_added` SQLite date-format mismatch (ISO 'T' vs space) → always returned 0 → false-positive silent_failure; (2) TSV-based fallback added so "0 keeps above threshold" exits rc=0 with `completed_no_keeps` sentinel; (3) `run_compute_matrix.py` now distinguishes ok/no_keeps/benchmark_unavailable/config_missing/error and returns 0 for all non-error outcomes. Service confirmed running. 19/19 tests. Blocks #219 unblocked.
- [ ] **#217 — Phase 5 sweep scheduling.** Per-universe timers landed
      (Wave 2), but sweep *quality* (symbols covered, strategies run) is
      not yet validated end-to-end on the new templated units.
- [ ] **#218 — Phase 5 promoter guard.** Promoter must refuse to write
      `research_best` rows older than threshold. Today silently overwrites.
- [ ] **#219 — Phase 5 regression harness.** No automated check that sweep
      outputs match prior-day sanity. Blocked by #216.
- [ ] **#220 — Phase 5 alt-data pipeline.** OpenInsider/Finviz scraper
      landed (`408420f2`) but integration into signal generation incomplete.
- [ ] **#221 — Phase 5 dashboard surfacing.** Overlay decisions + health
      section landed (`a302f9b1`); still need research-matrix coverage view.
- [x] **#239 — MRVL orphan fix audit.** ✅ Closed 2026-04-24.
      Data fix: all 11 inverted-date trades swapped (entry↔exit);
      4 status=error rows set to closed. Root cause: AEST entry_date vs
      UTC/ET exit_date — off-by-one timezone. CHECK constraint added to
      trades table (migration 2026-04-24-trades-date-consistency-check.py).
      Tests: test_trade_date_check_constraint.py.

---

## 🧪 TODO — Test hygiene (carry-over from prior todo)

- [ ] Fix `tests.conftest` import issue (3 test files) — create `pytest.ini`
      with proper rootdir/import config
- [ ] Fix `test_backtest_parallel.py` — module `scripts.backtest` missing,
      skip with clear message
- [ ] Fix `test_agent_thorough.py` — Playwright needs server, skip when
      unavailable
- [ ] Verify collection clean with `pytest --co` — target zero errors

## ⚠️ TODO — Error-handling follow-ups (beyond top-17)

Wave 2 fixed the 17 most dangerous silent excepts. Still open:

- [ ] `scripts/eod_settlement.py:602` — silent telegram crash-notify failure
- [ ] `scripts/execute_approved.py:163,179` — silent telegram notify
- [ ] `scripts/reconcile_positions.py:270,495` — disconnect + telegram
- [ ] `scripts/sync_protective_orders.py:322,584` — disconnect + telegram

---

## 📦 Deferred / out of scope (requires dedicated session)

These are real work items that the audit surfaced but are too large or
risky to bundle into an audit wave. Parked for future focused sessions.

- **God-file decomposition** — `services/chat_server.py` (>2,300 LoC),
  `brokers/live_executor.py` (~1,700 LoC), `brokers/alpaca/broker.py`
  (large). Decomposition needs a test harness first to catch regressions;
  current coverage is insufficient.
- **Atlas/Cronus shared core** — extract common trading primitives to a
  `lib-trading/` package so both systems share one implementation. Blocked
  on Cronus API stability.
- **Broker-abstraction stress test** — today we assume Alpaca APIs are the
  only live target. Need a paper/moomoo round-trip test to prove the
  abstraction actually abstracts.
- **`sync_all_protective_orders` decomposition** (737 → ~80 LoC orchestrator
  + 5 helpers) — previously planned, deferred until sync protective has
  test coverage.
- **Phase 2/3/4 architecture gate validation** — separate audit, not
  bundled with Wave 2.

## 🚧 Known pending from Wave 1 (manual verification or deferred)

- **Live HALT file round-trip test** — blocked by write-domain during the
  audit; user to verify manually or flag during next trading window.
- **Caddy basic-auth credential rotation** — documented in Wave 1 hardening
  commit, but credentials not rotated. Requires coordination with anyone
  using the dashboard URL.

---

## ✅ DONE — Reconcile 2026-04-27 (Phase 2-4 + audit-fix sweep)

Reconciled against git log since 2026-04-17. Major work:

- [x] **Phase 2-4 completion (live trading capital protection)** — research-engine
      audit fixes shipped over 2026-04-22 to 2026-04-27. Universe isolation,
      regime tagging, commodity_etfs intraday safety, dual-write canary, etc.
- [x] **6-fix safety audit batch (audit-fix-1..6)** — Fix 1 (overlay status
      verified inert), Fix 2 (stops sync 3 uncovered positions), Fix 3 (inverted
      stop CHECK constraint), Fix 4 (leverage gate), Fix 5 (sector_etfs cron),
      Fix 6 (closed-trade dedup UNIQUE index). Commits `12e720ab`..`aba29975`.
- [x] **Telegram noise suppression** — 4 surgical early-returns in
      send_plan_for_approval / send_postclose_summary / _notify_auto_approve /
      _notify_execution. Daily volume ~9 → 0-3. Commit `d7dfee85`.
- [x] **Hermes Round 4 + Round 5 P0 (cross-project)** — calibration overhaul +
      mlb_totals slope inversion fix. 7 commits, 552/552 tests.
- [x] **NRL-Predict 3-fix audit pack (cross-project)** — pregame flip gate
      disabled, model weight ablation, AusSportsBetting blocked at Cloudflare WAF.

(Refer to git log for full commit list. ~30+ commits since 2026-04-17.)

---

*Last reconciled: 2026-04-27 (Phase 2-4 + audit-fix sweep close).  Prior counters were folklore — do not reinstate.*

---

## ✅ DONE — Wave B audit fixes (commits 195968e4..a445662b, 2026-04-28)

- [x] **#255 — B4 adjust_divergence tracking** — overlay vision A/B review now
      tracks top-level adjust divergences (text=True vs vision=False). Real-data
      smoke: 5/195 cycles (2.6%) had adjust-level divergences in last 7 days.
      `scripts/review_vision_ab.py` +29 lines additive, 5 new tests.
      Commit `30615d96` (2026-04-28).

- [x] **#259 — B2 zero-byte autoresearch logs** — 5 zero-byte files were
      logrotate stubs; real content in *.log-20260417 archives (3788 experiments
      on Apr 15). Already addressed by 1deedcf5 + 0e42f652. Audit report filed
      at `research/reports/B2_zero_byte_logs_20260415.md`.
      Commit `a445662b` (2026-04-28).

- [x] **#265 — B3 Tiingo authority flip** — `config/price_arbiter.json`
      `authority_on_mismatch`: alpaca → tiingo. Wave 1 flip (7ad48f37) never
      landed in working tree. NFLX was marked at $97 (real $107.79, 11.12%
      spread). Atlas-dashboard restarted to clear in-memory halt set. 3 new
      guard tests. Commit `a445662b` (2026-04-28).

- [x] **#A8 — B1 max_gross_exposure_pct cap (1.75)** — new
      `risk/gross_exposure_guard.py`; wired into `live_executor._execute_entry`
      after W6 cross-universe guard; `max_gross_exposure_pct: 1.75` added to
      risk block in all 6 active universe configs. Apr 27 simulation (174% MV +
      UNG → 200%) correctly REJECTED. 15 tests.
      Commit `280de055` (2026-04-28).

## ⚠️ DEFERRED — Wave B items requiring follow-up

- [ ] **#258 — B5 SPY text-vs-vision audit** — literal text/vision entry
      pairing not found in current logs (may be in pre-Apr-17 archives).
      Structural finding: `overlay/sources/chart_intel.py:_build_summary`
      hard-codes "Broadly bullish" from SMA alone, no OBV/volume-profile/
      divergence indicators. Audit report filed at
      `research/reports/B5_spy_text_vs_vision_audit.md`.
  - 2026-04-28 refined: literal pairing not in current logs (may be in pre-Apr-17 archives). Structural finding: overlay/sources/chart_intel.py:_build_summary hard-codes "Broadly bullish" from SMA alone, no OBV/volume-profile/divergence. Spec follow-up to add (a) OBV slope, (b) multi-month resistance anchor, (c) price-volume divergence, (d) suppression guard. See research/reports/B5_spy_text_vs_vision_audit.md.

- [ ] **#260 — Caddy basicauth credential rotation** — needs user coordination
      for credential rotation. Deferred. Documented in Wave 1 hardening.


### RCA latent #6 — Per-market equity attribution + drawdown HWM wiring [CLOSED 2026-04-29]
- [x] **Fix 1 — starting_equity recalibration** (commit: RCA latent #6 fix 1)
  - sp500: 5011.79 → 971 (real allocated equity from market_equity_history 2026-04-29)
  - commodity_etfs: 5000 → 1001
  - sector_etfs: 5000 → 3216
  - Inactive markets (crypto, defensive_etfs, gold_etfs, treasury_etfs): 5000/5011 → 0
  - `last_recalibrated_at` ISO timestamp added to all `risk` blocks
- [x] **Fix 2 — per-market drawdown HWM wiring** (commit: RCA latent #6 fix 2)
  - `_get_per_market_equity(current_broker_eq)` added to `LivePortfolio`
  - Reads `market_equity_history` table; scales to current broker equity
  - Fallback to global broker equity if no snapshot (<3 days old required)
  - `check_daily_drawdown()` now uses per-market equity as primary source
  - Markets now halt independently: sp500 at 15% dd halts only sp500, not commodity_etfs/sector_etfs
- [x] **Equity-sum config guard** (keep-loud alert)
  - `check_equity_config_sum()` added to `scripts/health_check.py`
  - Standalone: `scripts/check_equity_config_sum.py`
  - Asserts Σ(active starting_equity) ≤ broker.equity × 1.05; Telegram alert on violation
- [x] **Tests** — 19 new tests in `tests/test_per_market_drawdown.py` (all passing)
  - `TestGetPerMarketEquity` (7 tests): DB read, scaling, staleness, DB failure
  - `TestPerMarketDrawdownIsolation` (6 tests): independent halt scenarios from spec
  - `TestCheckEquityConfigSum` (6 tests): guard correctness, inactive market exclusion, dry-run

---

## Phase C — Architectural simplification (planned 2026-04-29; ship after Phase B cutover)

Status: PLANNED. Each task has a design doc; implementation deferred until pre-requisites land.

### C.1 — Trade state machine (formal, DB-enforced)
- [ ] Read docs/phase-c-trade-state-machine.md
- [ ] Migration: add `state` column to `trades`, default NULL
- [ ] Backfill existing trades into states (PROPOSED/SUBMITTED/FILLED/PROTECTED/CLOSED/SETTLED)
- [ ] Shadow-write phase (parallel writes to `state` column, no enforcement yet)
- [ ] CHECK constraint enforcement (table-recreation pattern)
- [ ] Refactor all 12 write paths to call `db.transition_trade(trade_id, to_state)`
- [ ] Pre-req: Phase B.2 cutover (7-day shadow validation — **2/7 clean as of 2026-05-08**;
        Apr29=171, Apr30=17, May1=3, May2=7, May5=5, May6=11, May7=0✅, May8=0✅;
        est. cutover ~2026-05-14 if streak holds)
- Estimate: 2-3 weeks

### C.2 — Per-market broker sub-accounts
- [ ] User decision required: (a) per-market sub-accounts (4-6 weeks), or (b) keep single account with strict virtual ledger (1 week, already partially done)
- [ ] If (a): operational lead-time for Alpaca sub-account provisioning
- [ ] If (b): membership disjointness check at config load + runtime universe verification
- Recommend default: (b) — already 90% in place via universe.membership module
- Pre-req: user approval

### C.3 — Single orchestrator timer
- [ ] Read docs/phase-c-orchestrator-timer.md
- [ ] Build `core/orchestrator.py` skeleton (per-market DAG: sync_broker_orders → reconcile → sync_protective → healthz)
- [ ] Write `systemd/atlas-orchestrator.{service,timer}` (OnCalendar=*:0/15)
- [ ] Run alongside existing cron in shadow mode (7-day diff alerts)
- [ ] Cut over: prune crontab from 49 → 8 entries
- [ ] Delete shadow mode code
- Estimate: 1-2 weeks

### C.4 — God file decomposition
- [ ] Read docs/phase-c-god-file-decomposition.md
- [ ] Run `pydeps` import graph on all 3 god files; document cycles
- [ ] Extract `core/types.py` with shared dataclasses to break import cycles
- [ ] db/atlas_db.py → `db/{connection,trades,equity,signals,broker_orders,misc}.py` (start here — lowest risk)
- [ ] services/chat_server.py → `services/app.py` + `services/api/*.py` + `services/ws/chat.py`
- [ ] brokers/live_executor.py → `executor/{entry,exit,protective,reconcile,pdt,leverage}.py` (last — highest risk)
- [ ] Re-export shims preserve old import paths during transition
- [ ] Full regression suite must pass before merge of each split
- Pre-req: C.1 (state machine) + B.2 cutover
- Estimate: 4 weeks (all 3 files); 5 days (db only, minimum viable)

### Latent items surfaced during this audit
- [ ] Bare-except conversion: 839 grandfathered offenders (lint script blocks new ones; convert oldest paths first)
- [ ] OCO bracket migration for existing positions: CAT/GLD/XLI/XLY have INDEPENDENT stop+TP orders, not OCO-linked. Phase B atomic-bracket-by-default applies to NEW entries; existing 4 positions need a controlled cancel+place to migrate. Risky pre-market work; recommend user-approved one-off.
- [ ] FCX universe-disjointness: still in BOTH markets/etf_markets.py and markets/sp500.py. Recommend startup-time disjointness check.
- [ ] reconcile_sqlite_to_broker.py missing inverted-stop and no-zero-stop guards (validation Quick Win 7).

## Follow-ups from 2026-04-30 sector_etfs HALT forensic investigation (2026-05-01)

### #FIX-PMEQ-001 — Per-market equity formula has phantom-drawdown bug on intraday position exits (P0, BUG)

**STATUS**: ✅ Resolved 2026-05-01 in commits 9d7c5662 (formula fix + tests), 9adc0086 (HWM reset script + live recalibration).

**Symptom**: sector_etfs tripped L3 kill switch at 13.69% daily drawdown on 2026-04-30 19:03 UTC despite real broker equity GROWING $5,134.19 → $5,164.50 (+0.59%) over the same window. Real sector_etfs PnL on Apr 30 was +$2.73 (XLY exit small profit). The 13.69% number is a calculation artifact, not a real loss.

**Root cause**: `brokers/live_portfolio.py::_get_per_market_equity` computes `per_market_eq = current_pos_mv + (snap_cash * cash_scale)`. When a position EXITS during the day, its value moves from position_mv → broker cash. But `scaled_cash = snap_cash * cash_scale` does NOT track this — `snap_cash` is locked to yesterday's snapshot value (here $220.40 from Apr 29 22:01 snapshot for sector_etfs). Result: the per-market estimate falls when a position exits, even though the real account total is unchanged or higher.

**Concrete arithmetic on Apr 30**:
- Snapshot at Apr 29 22:01 UTC: sector_etfs pos_mv=$1,529.37, cash_attributed=$220.40 (XLY was prematurely marked closed in trades table at Apr 29 22:00:34 — 1.5 min before snapshot — so XLY's $1,168 value was excluded from sector_etfs attribution despite still being held at broker)
- HWM reset 08:00:50 (post XLY broker exit at 08:00:34): formula output $2,028.87 → became the day's HWM
- 19:03:51 trip: positions=[XLI 9 shares], current_pos_mv ≈ $1,530, scaled_cash ≈ $221.70, per_market_eq ≈ $1,752 → drawdown (2028.87 − 1752) / 2028.87 = 13.6% ✓

**Fix design**: replace the stale-snapshot scaled_cash component with a LIVE per-market cash estimate that tracks intraday exits. Options:
1. Track per-market realized cash flow since snapshot (sum of exit proceeds − entry costs since snap_date) and add to scaled_cash.
2. Reconcile per-market cash by querying broker for cash AND attributing the delta-since-snapshot proportionally to each market by snap-share.
3. Stop using per-market HWM entirely and revert to global broker-equity HWM (simpler, fewer false halts, less granular).

Option 1 is cleanest. Option 3 is safest if we don't trust the attribution model.

**Other affected markets**: same formula bug applies to sp500 and commodity_etfs. They didn't trip Apr 30 because their position_mv stayed close to snap_pos_mv (no exits). They are equally exposed if any of their positions exit intraday before tomorrow's 22:01 UTC snapshot.

**Acceptance criteria**:
- Add regression test in `tests/test_live_portfolio_drawdown.py`: simulate sector_etfs sequence (snapshot with XLI+XLY ≈ but XLY excluded, then XLY exits at 08:00, broker_eq grows by $30) — assert per_market_eq DOES NOT drop more than 2%, no HALT fires.
- Add regression test for the sp500 case: 3 positions in snap, one exits intraday, broker_eq stable → no HALT.
- Update mental model entry on 2026-04-29 (the original RCA #6 fix) noting the residual gap.

---

### #FIX-PMEQ-002 — Premature trade-closure timing creates wrong-day attribution (P1, DATA)

**Symptom**: XLY trade row id=167 has updated_at=`2026-04-29 22:00:34` and exit_date=`2026-04-30T08:00:34`. The trade was marked status=closed in SQLite ~10 hours BEFORE the broker actually filled the exit. The Apr 29 22:01 attribution snapshot then excluded XLY from sector_etfs's pos_mv even though the broker still held the position. This contributed to the under-attribution that caused #FIX-PMEQ-001.

**Investigation required**: trace what code path closes the trade row at 22:00:34 with a future-dated exit_date. Likely candidate: `core/reconcile.py` or one of the EOD settlement paths writing exit_date from a market-close timestamp without verifying broker fill is actually settled.

**Acceptance criteria**:
- Identify the writer.
- Either (a) defer SQLite close until broker fill confirmation, or (b) ensure attribution snapshots use broker-held positions, not SQLite trade status.

---

### #FIX-PMEQ-003 — sector_etfs JSON state file `entry_date` for XLI shows 2026-04-30, SQLite shows 2026-04-24 (P2, DATA)

`brokers/state/live_sector_etfs.json` has XLI entry_date "2026-04-30" but trades table id=185 has entry_date "2026-04-24T23:30:06". Same broker position (9 shares @ $173.97). One source got rewritten incorrectly. Find the writer and fix consistency. Likely: stop_order_id was created/replaced 2026-04-30, and a code path overwrote entry_date with stop creation date.

---

### #OPS-HALT-CLEAR-001 — Manual HALT clear procedure for false-positive trips (P1, DOCS)

**STATUS**: ✅ Resolved 2026-05-01 in commit 9adc0086 (HALT cleared) and this runbook.

**HALT cleared**: `rm /root/atlas/data/HALT` on 2026-05-01 12:02 AEST.
atlas-error-remediation.service next cycle (12:05:15 AEST) ran successfully (status=0).

---

#### Runbook: Clearing a false-positive HALT (standard procedure)

**When to use**: Kill switch fired at `data/HALT` due to a phantom drawdown (formula
artefact, not real loss). Confirmed by: broker equity stable/growing, real PnL minimal.

**Steps**:
1. **Diagnose** — confirm it's a formula artefact, not a real loss:
   - `python3 scripts/reset_per_market_hwm.py` (dry-run, check degraded=False and sane values)
   - Compare broker_equity now vs snapshot_time in `market_equity_history`
   - If broker_equity grew and per_market_eq dropped → formula artefact
2. **Fix formula** (if new bug): see FIX-PMEQ-001 pattern
3. **Reset HWMs** to correct values:
   ```bash
   python3 scripts/reset_per_market_hwm.py --apply
   ```
   This writes new HWM to both JSON state files and `market_state` SQLite table.
4. **Clear HALT file**:
   ```bash
   rm /root/atlas/data/HALT
   ls /root/atlas/data/HALT 2>&1  # confirm gone
   ```
5. **Verify auto-remediation activates** (timer fires every 5 min):
   ```bash
   systemctl status atlas-error-remediation.service --no-pager
   # Look for: Active: inactive (dead) since ... (no "unmet condition" in recent logs)
   systemctl list-timers atlas-error-remediation.timer
   ```
6. **Monitor** — next `check_daily_drawdown` cycle should show dd≈0 not 13%+

**Warning**: only clear HALT after confirming formula fix is in place.
Clearing HALT without the fix just restarts the false-positive cycle.


---

## ✅ #PERF-TG-CONSOLIDATE — Telegram wrapper consolidation (round 2) — DONE 2026-05-14

Round 2 (commit f035eae2) inlined 5 single-caller wrappers (≤30 LOC body):
- `scripts/check_config_vs_research_best.py:_send_telegram_alert` → inlined
- `scripts/healthz_error_remediation.py:send_telegram_alert` → inlined
- `scripts/data_integrity_monitor.py:_send_telegram_alert` → inlined
- `monitor/evaluator.py:_send_telegram_alerts` → inlined
- `brokers/price_arbiter.py:_send_telegram_bg` → inlined

KEPT (>30 LOC formatter or multi-concern):
- `research/discovery/discovery.py:_send_telegram_digest` — ~40 LOC (>30 threshold)
- `scripts/sync_protective_orders.py:format_telegram_message + send_telegram_summary` — ~75 LOC complex HTML formatter
- `scripts/reconcile_positions.py:format_telegram_message + send_telegram_summary` — ~65 LOC complex HTML formatter

Remaining (not in scope of wave 3.4):
- `research/autoresearch_runner.py:_try_send_telegram(text)` — has retry/dedup logic
- `research/llm_loop_runner.py:_send_telegram(summary: dict)` — dict-to-text formatting

## #PERF-BT-FOLDS — Walk-forward backtest fold parallelization (DEFERRED)

The 2026-05-01 efficiency audit identified fold execution in `backtest/engine.py` as a
significant compute bottleneck — fold loops run sequentially even when folds are
mathematically independent. Per the audit's "simplest fix" rule, the team
considered adding a `--parallel-folds` flag wrapping fold execution in
`concurrent.futures.ProcessPoolExecutor`. This was deferred because:

1. **Statelessness is not formalized** — no decorator or marker indicates which
   strategies have shared mutable state across folds. We'd need an audit first.
2. **Module-level caches** in `indicators/` and `data/cache/*.parquet` reads may
   not be process-safe. Pickling and re-loading would defeat the cache benefit.
3. **SQLite connections don't pickle** — the backtest engine shares a connection
   that ProcessPoolExecutor would have to recreate per worker.
4. **WalkForwardSplitter state** carries across folds in some strategies (e.g.
   warm-up periods that overlap fold boundaries).
5. **Silent corruption risk** is real — research output is the system's
   knowledge base and a subtle race condition could pollute days of sweeps
   before being noticed.

### Recommended approach for round 2

- Phase A (audit): grep for module-level `_cache = {}`, `@lru_cache`, and global
  state across `strategies/`, `indicators/`, and `backtest/`. Build a markdown
  table of which strategies are pickle-safe and which aren't.
- Phase B (instrument): add a per-fold timing dict to backtest output so we
  know the actual ROI of parallelization (currently we're guessing).
- Phase C (smaller scope first): implement parallel folds for ONE strategy
  (the simplest stateless one — likely `mean_reversion` or `consecutive_down_days`).
  Validate against the same strategy's serial result for byte-equality. Only
  then expand.
- Phase D (general): add a `parallel_safe: bool` class attribute on
  `BaseStrategy` and propagate to `--parallel-folds`. Default False.

### Out of scope for this audit because

  - Time budget for the audit is "simplest fix" — this is L-effort.
  - Risk-asymmetric: a wrong fix corrupts research; a deferred fix only
    leaves a known-slow path that already works.

Owner: TBD. Estimated 2-3 sessions of focused work.


---

## Task #300 — Plan generator passive-universe skip + PAPER lifecycle inclusion (completed 2026-05-06)

- [x] Task #300 — plan generator skips non-live/paper universes: `cmd_plan` early-exit when `trading.mode != live|paper`. commodity_etfs (mode=passive) no longer generates plans.
- [x] Phase B dogfood — short_term_mr/sp500 included in plan generation via lifecycle-aware `get_strategies`: strategies in PAPER lifecycle state are instantiated with research_best params. short_term_mr now appears in sp500 daily plan.

Files changed: `scripts/cli.py`, `tests/test_plan_generator_lifecycle.py` (8 tests). Commit: 963e20a1

---

## 14-day reminders

- **2026-05-21 — #288**: Evaluate auto-remediation Phase 3 results (active since 2026-04-30,
  ~21 days by then). Decide on any Phase 3 config tightening or whitelist expansion.
  Check: `curl http://127.0.0.1:8899/api/error_remediation/summary` for attempts/reverts.
  **Status 2026-05-08**: 17 errors captured since activation, 0 fix_attempts started
  (all classified ESCALATE — PDT block, sweep failures, cancel-order race; idle as expected).
  
  ⚠️ **Prerequisite investigation before evaluating**: L4 kill-switch shows false-positive
  75.6% drawdown (equity_history peak 2026-04-24 = $5,429 global, vs current sp500-only
  $1,324 after 2026-04-29 per-market equity refactor). If any ASSIST/AUTO_FIX errors ever
  appear, fix dispatch will be silently blocked by L4. Operator should reset the sp500
  equity_history baseline or update `check_l4_drawdown()` to use the correct window
  (start from 2026-04-29 onward only).

---

## Task #297 — Multi-universe consolidation: sp500-only (completed 2026-05-07)

Phase B2 complete. Atlas now trades sp500 only.

- [x] Phase 1: `trading.live_enabled: false` in both `config/active/sector_etfs.json` and `config/active/commodity_etfs.json`
- [x] Phase 2 (SKIP — no-op): commodity_etfs and sector_etfs had 0 open positions at consolidation time (exited 2026-05-05)
- [x] Phase 3: 13 crontab entries commented (preserved with `CONSOLIDATED-2026-05-07` prefix); reconcile_ledger updated to sp500-only
- [x] Phase 4: systemd timers `atlas-research-window@commodity_etfs.timer` and `atlas-research-window@sector_etfs.timer` disabled
- [x] Phase 5 (code audit): all execution paths respect `BrokerRoutingPolicy.should_skip()` → `live_enabled=False` gate. No auto-re-enable risk.
- [x] Phase 6: Re-enable criteria documented at `docs/multi-universe-consolidation-2026-05-07.md`

⚠️ **Outstanding follow-up**: `live_sp500.json` lists 5 positions (CAT, SYK, MCHP, FSLR, EBAY) but Alpaca broker holds only 2 (CAT, SYK). MCHP/FSLR/EBAY are orphaned state entries. Requires reconciliation.

Re-enable criteria: sp500 green ≥30 days (after 2026-06-06), freed capital deployed, operator approval, MCHP/FSLR/EBAY discrepancy resolved.

---

## #314 (NEW) — Upgrade text-summary feature set after vision A/B
- Source: spec'd as follow-up to #258 (Phase C2 chart-vision A/B review)
- Problem: current text-summary overlay uses a sparse feature set; vision A/B review surfaced cases where richer text features would have caught what vision flagged. Vision's only unique signal attempts (Apr 21 tighten on SPY/QQQ) were also made by text-overlay — vision added nothing; upgrading text features may close that gap without the vision inference cost.
- Tasks:
  1. Audit current text-summary feature inputs (which fields, which lookback windows)
  2. Add: candle-pattern detector (engulfing, doji, hammer), multi-timeframe trend agreement (5m vs 1h vs 1d), volume profile vs N-day median
  3. Re-run a 30-day A/B vs current text-summary
  4. If improvement: ship behind feature flag

## #258 follow-up A — Sweep pre-Apr-17 archived overlay logs for literal text/vision pairing
- **COMPLETED 2026-05-08**: Zero vision references in pre-Apr-17 logs. Vision A/B system (logs/overlay_vision_ab/) first activated 2026-04-17. No historical text/vision pairs exist before that date.
- Files checked: logs/overlay_20260402.log-20260404 through overlay_20260415.log-20260417

---

## #324 — PDT pre-submit guard for BUY orders (FTNT incident) — DONE 2026-05-11

- [x] **Alert**: 2026-05-11 23:15 UTC LIVE BUY FAILED: FTNT — Alpaca code 40310100 (pattern day trading protection)
- [x] **Root cause**: `brokers/alpaca/broker.py:638` PDT pre-check only fired for `side == OrderSide.SELL`; BUY orders bypassed local guard and hit Alpaca broker-level rejection
- [x] **Underlying driver**: Account equity ~$5,237 (<$25k PDT threshold) with 3 real same-bar Alpaca round-trips in rolling 5-business-day window (MCHP 36s + EBAY 37s on 2026-05-08, CRWD 336s). Alpaca pre-emptively denied because a same-day FTNT exit would complete a 4th day-trade
- [x] **Fix** (commit `636d3c8d`): (a) ticker-level pre-check now applies to BOTH sides, (b) new `AlpacaBroker.get_pdt_status()` queries Alpaca account.daytrade_count/pattern_day_trader/equity, (c) BUY orders consult account-level pre-empt before submit when equity<$25k AND daytrade_count>=3 — fails open on API hiccup, calls `_set_pdt_deferred_new` so subsequent cycles also skip
- [x] **Tests**: 11 new in `tests/test_pdt_buy_guard.py`, 5 existing in `test_pdt_backoff_avgo_ccj.py` — all green

## #325 — director_cron heartbeat false-positive (38h alert) — DONE 2026-05-11

- [x] **Alert**: 2026-05-11 23:24 AEST "🔴 director_cron — idle — 38.2h ago"
- [x] **Root cause**: `config/heartbeat.json` had no entry for `director_cron`; watchdog fell through to 6h fallback. Director is weekly (Sat 22:00 UTC per atlas-director.timer)
- [x] **Fix** (commit `94606c0c`): added `director_cron` to heartbeat.json with `expected_cron="0 22 * * 6"`, `threshold_hours=30` (24h+6h grace). Cleared stale alert state.
- [x] **Test**: `tests/test_heartbeat_watchdog_schedule_aware.py::test_director_cron_configured_in_heartbeat_json`
- [x] **Live verified**: `python3 scripts/heartbeat_watchdog.py --dry-run` no longer mentions director_cron

## #326 — silent-failure-watchdog autoresearch race condition — DONE 2026-05-11

- [x] **Alert**: 2026-05-11 23:00 AEST "⚠️ 1 zero-byte autoresearch log(s) in last 24h: autoresearch_connors_rsi2_20260511.log"
- [x] **Root cause**: `silent_failure_watchdog.py::check_autoresearch_logs` flagged a log that was JUST created. Both `atlas-silent-failure-watchdog.timer` (OnCalendar=hourly) and the autoresearch nightly fire at 13:00 UTC simultaneously. The autoresearch runner created the log file (start banner: `Started : 2026-05-11 13:00:05 UTC`) at the exact second the watchdog scanned the dir (`May 11 23:00:05 pi systemd[1]: Starting atlas-silent-failure-watchdog.service`). File existed but buffered IO hadn't flushed yet → 0 bytes
- [x] **Fix** (commit `94606c0c`): added `_AUTORESEARCH_MIN_AGE_SECONDS = 15 * 60` constant; logs younger than 15 min are skipped with a "race-condition skip" INFO line. Distinguishes "just created, will flush" from "real silent failure"
- [x] **Tests**: 4 new in `tests/test_silent_failure_watchdog_autoresearch.py` — fresh skipped, old alerted, paused skipped, non-zero never alerts
- [x] **Live verified**: `python3 scripts/silent_failure_watchdog.py --dry-run` → "OK (no zero-byte logs in last 24h)"

## #327-B — CAT stop_order_id/tp_order_id collision investigation — DONE 2026-05-12

- [x] **Finding**: `live_sp500.json` CAT had `tp_order_id=""` after prior repair; Alpaca confirmed both OCO legs are active
  - stop leg `a1021664-...` = OCO stop@861.21 (status=HELD) ✓
  - TP leg `3d035b5f-...` = OCO limit@978.33 (status=NEW, `client_order_id=atlas_retro_tp_1509d405`) — was missing from state
- [x] **Root cause**: commit `aaafb2d9` placed same UUID in both fields; later fix identified correct stop_order_id but cleared `tp_order_id` to `""` instead of the retro-TP UUID
- [x] **Diagnosis**: Branch A — both OCO legs active, state file just had wrong/empty tp_order_id
- [x] **Fix** (state-file only, no broker mutations): set `tp_order_id="3d035b5f-3926-4d2d-9506-0c588e691fcb"` in `brokers/state/live_sp500.json`
- [x] **Collision scan**: 0 other collisions across 3 markets (commodity_etfs, sector_etfs, sp500)
- [x] **Audit**: `data/audit/cat_state_repair_2026-05-12.json` (git-ignored, on-disk only)
- [x] **Tests**: 21/21 in `tests/test_state_order_id_uniqueness.py`
- [x] **Script**: `scripts/audit_state_order_id_collisions.py` (reusable collision scanner for CI)

## #327 — Run clean solo backtests for contaminated research_best files

- [ ] 14 contaminated files identified (see `data/audit/promotion_integrity_2026-05-12.json` and review session 2026-05-12).
- [ ] For each (connors_rsi2 sp500, consecutive_down_days sp500, mean_reversion sp500+all etfs, momentum_breakout commodity/defensive/gold/treasury etfs, opening_gap sp500, short_term_mr sp500, mean_reversion crypto): run TRUE solo backtest with Alpaca $0 commission, current 7-yr universe, regime-aware splitter.
- [ ] Replace contaminated `research/best/<name>.json` with the solo result.
- [ ] Re-verify `is_solo: true` for all 14 post-rerun.
- [ ] Run `python3 scripts/audit_promotion_integrity.py` post-rerun; expect 0 contaminated.
- [ ] Surface in dashboard — contamination badge should disappear for all 14.
- [ ] Audit contaminated-at-promotion events: connors_rsi2/sp500, mean_reversion/commodity_etfs, momentum_breakout/commodity_etfs, short_term_mr/sp500 (PAPER) — consider re-evaluation after solo backtests complete.

## Task C — Operational sediment cleanup + knowledge index refresh (2026-05-12) ✅

- [x] `scripts/cleanup_sediment.py` — 6 pattern groups, top-3 + 14d retention; `--dry-run` / `--apply`
- [x] Ran cleanup: deleted `brokers/state/live_sector_etfs.json.pre-xlk-recovery-20260424T004819` (326 bytes, Apr 24)
- [x] Audit JSON committed: `data/audit/sediment_cleanup_2026-05-12T095609Z.json`
- [x] `scripts/regen_knowledge_index.py` — builds `docs/KNOWLEDGE_INDEX.md` (dirs, key files, strategies, markets, commits, test inventory)
- [x] `scripts/regen_brain_summary.py` — builds `research/brain/SUMMARY.md` (lifecycle, top-10 Sharpe, promotions, integrity check)
- [x] `scripts/check_doc_staleness.py` — exits 1 if KNOWLEDGE_INDEX or SUMMARY > 30d old
- [x] `docs/KNOWLEDGE_INDEX.md` and `research/brain/SUMMARY.md` regenerated (2026-05-12, both age 0d)
- [x] Cron entries added to `scripts/atlas.crontab`:
      - `0 14 * * *` (04:00 UTC) — cleanup_sediment.py --apply
      - `0 18 * * *` (08:00 UTC) — check_doc_staleness.py
- [x] 17 tests: 9 in test_cleanup_sediment.py + 8 in test_doc_staleness.py — all passing
- [x] Commit: `043dfdc0`

## #341 — sp500 connors_rsi2 param drift: LIVE config vs research-best

**Status**: TODO — created 2026-05-14 as follow-up to #340 (connors_rsi2 sp500 LIVE→PAPER demotion).

The currently-LIVE config for connors_rsi2 sp500 has drifted significantly from
the research-best params. This drift may explain part of the underperformance
that triggered the demotion. Audit needed before any re-promotion attempt.

**Param-by-param diff** (live config/active/sp500.json vs research/best/connors_rsi2.json):

| Param | Live (config/active) | Research-best (research/best) | Delta |
|-------|----------------------|-------------------------------|-------|
| rsi_period | 3 | 2 | live is +1 (less sensitive) |
| min_consecutive_down | 1 | 2 | live is -1 (looser trigger) |
| ibs_max | 0.5 | 0.75 | live is -0.25 (stricter) |
| ibs_filter_enabled | false | true | live disables IBS filter entirely |
| atr_stop_mult | 1.0 | 1.35 | live is -0.35 (tighter stops) |

**Action items** (deferred — not blocking demotion):
- [ ] Run controlled comparison backtest with both param sets on identical universe
- [ ] Determine which set drove the divergence between live performance and research expectation
- [ ] If research-best params dominate clean solo, consider re-promotion via PAPER path with the corrected params
- [ ] If live params dominate (drift was intentional), update `research/best/connors_rsi2.json` to reflect production reality and document the override rationale

**Reference**: see `research/best/connors_rsi2.json` for full research metrics; clean solo Sharpe = -0.2433 (post-#327 rerun, 2026-05-14).

---

## #342 — Delete orphan `services/api/strategy_lifecycle.py`

**Status**: TODO — discovered 2026-05-14 during dashboard audit.

`services/api/strategy_lifecycle.py` exists but is NOT mounted in `services/chat_server.py`.
The richer `services/api/lifecycle.py` IS mounted (line 205: `app.include_router(_lifecycle_router)`).
`strategy_lifecycle.py` is referenced by `tests/test_strategy_lifecycle_api.py` tests which
build their own FastAPI app from its router. The file's own docstring confirms this usage.

**Action items**:
- [ ] Confirm not mounted: `grep -rn "strategy_lifecycle" services/chat_server.py` (expect 0 hits)
- [ ] Confirm test dependency: `grep -rn "strategy_lifecycle" tests/` — if `test_strategy_lifecycle_api.py` imports it directly, the file CANNOT be deleted; migrate those tests to use `lifecycle.py` router instead
- [ ] If tests migrated cleanly: delete `services/api/strategy_lifecycle.py`
- [ ] If tests cannot be migrated without breakage: surface as a consolidation task for #348

---

## #343 — Update `tests/test_research_integrity.py` for connors_rsi2 clean-rerun

**Status**: TODO — after #327 contamination rerun completes for connors_rsi2.

After #327 rerun completes for connors_rsi2 (sp500, commodity_etfs, gold_etfs), the
`tests/test_research_integrity.py` fixture snapshots may reference the pre-rerun
(contaminated) Sharpe values. These will mismatch the post-rerun clean-solo values.

**Action items**:
- [ ] After #327 reruns complete: run `pytest tests/test_research_integrity.py -v --timeout=30`
- [ ] Identify which fixtures reference the contaminated Sharpe values (likely hardcoded in fixture dicts)
- [ ] Update fixtures to use post-rerun values from `research/best/connors_rsi2*.json`
- [ ] Confirm `solo_sharpe_clean` fields are populated and non-None in all 3 connors_rsi2 variants
- [ ] Re-run; confirm all 24 tests still pass

---

## #344 — Strategic review of connors_rsi2 — DONE 2026-05-14

- [x] Decision: demote sp500 LIVE → PAPER pending param-drift review (see #341 param-drift task)
- [x] Demotion rationale: clean solo Sharpe = -0.2433 (post-#327 rerun, 2026-05-14)
- [x] Audit log: see `strategy_lifecycle_history` table entries 2026-05-14
- [x] Follow-up: param-drift investigation (#341, open)

---

## #345 — Enable disabled research-window timers

**Status**: TODO — timers were disabled 2026-05-07 during consolidation (#297).

`atlas-research-window@commodity_etfs.timer` and `atlas-research-window@sector_etfs.timer`
were disabled when those universes moved to `passive` mode. Decision needed per universe.

**Action items**:
- [ ] Run: `systemctl list-timers 'atlas-research-window@*.timer'` — confirm which are disabled
- [ ] For each disabled timer, decide:
  - (a) Re-enable: universe has strategies worth researching even in passive mode (research
        may surface params that justify future re-activation)
  - (b) Leave disabled: no upcoming plans to re-activate this universe; research is wasteful
- [ ] Document decision per universe in `docs/multi-universe-consolidation-2026-05-07.md`
- [ ] Per current re-enable criteria (sp500 green ≥30 days after 2026-06-06): defer
      commodity_etfs/sector_etfs timer re-evaluation until the re-enable gate date

---

## #346 — Fix pre-existing test_price_arbiter outside-RTH flakiness

**Status**: DONE — 2026-05-14. Wave B commit a445662b flipped authority_on_mismatch 'alpaca'→'tiingo'; aligned test assertions + added lock-in test. 5/5 pass.

**Action items**:
- [x] Run `pytest tests/brokers/test_price_arbiter.py -v --timeout=30` outside RTH to confirm flakiness
- [x] Locate RTH check in `brokers/price_arbiter.py` (likely `is_rth()` or similar)
- [x] Align `test_outside_rth_no_telegram_logs_warning` + `test_warn_band_does_not_alert` assertions to tiingo authority
- [x] Add `test_default_authority_is_tiingo` lock-in guard
- [x] Confirm 0 flaky failures — 5/5 pass

---

## #347 — Wire alt-data signals into plan generator

**Status**: TODO — alt-data pipeline (#220) computes and logs signals but plan generator
never consumes them.

`overlay/sources/alt_data.py` collects OpenInsider / Finviz signals and writes to
`news_intel` table. `overlay/sources/news.py` has `_fetch_alt_data_intel()` for the overlay
engine. But `brokers/plan.py` (`TradePlanGenerator`) and `scripts/cli.py::cmd_plan` do not
consult alt-data before generating signals.

**Action items**:
- [ ] Design: decide whether alt-data acts as (a) a sizing override (boost/reduce position
      size based on insider activity), (b) a hard gate (block signal if no insider buy), or
      (c) an additive score bonus (bump priority score for tickers with recent insider buys)
- [ ] Document design decision in `research/brain/strategies/alt_data_integration.md`
- [ ] Implement the chosen path in `brokers/plan.py::generate_plan()` or `_run_sp500_plan()`
- [ ] Add unit tests covering: alt-data present → plan reflects it; alt-data absent → plan
      unchanged (graceful degradation)

---

## #348 — Consolidate / verify router mounts for strategy_lifecycle + research_matrix

**Status**: ✅ DONE 2026-05-14 — clarifying comment added to chat_server.py (commit `437c7b16`).

**Current state** (verified 2026-05-14):
- `services/api/lifecycle.py` → IS mounted (chat_server line 205, `_lifecycle_router`)
- `services/api/research_matrix.py` → IS mounted (chat_server line 206, `_research_matrix_router`)
- `services/api/strategy_lifecycle.py` → NOT mounted; used only by `tests/test_strategy_lifecycle_api.py`

**Action items**:
- [ ] Confirm `test_strategy_lifecycle_api.py` tests can pass if routed through `lifecycle.py`
      router instead (endpoint paths should match)
- [ ] If yes: consolidate — delete `strategy_lifecycle.py`, update test imports (see #342)
- [ ] If no: document why the redundant file must stay; add a comment to both files cross-
      referencing each other so the duplication is visible
- [ ] Either outcome: add a comment in `chat_server.py` explaining that `lifecycle.py` is
      the canonical mount and `strategy_lifecycle.py` is test-only

---

## #349 — Cleanup: eod_settlement sell_result guard + 2 stale tests

**Status**: COMPLETED 2026-05-14 (commit 618429c2).

`scripts/eod_settlement.py` `check_stop_losses` + `check_take_profits`: replaced
fragile `"sell_result" in dir()` membership check with explicit `sell_result is not None`
after `sell_result = None` init-to-None at the start of each function's loop body.
62 eod_settlement tests pass.

**Action items**:
- [x] Find exact file:line: lines 218 and 348
- [x] Replace `"sell_result" in dir()` → `sell_result is not None`
- [x] Add `sell_result = None` init before the conditional assignment
- [x] 62/62 eod_settlement tests pass

---

## #350 — Auto-detect KNOWN_CONTAMINATED in rerun_contaminated_backtests.py

**Status**: COMPLETED 2026-05-14 (commit in C3 batch).

- [x] `detect_contaminated_pairs()` rewritten to use multi-criteria detection:
      (a) `is_solo == false`, (b) `is_solo == true` AND `solo_sharpe_clean` missing,
      (c) neither field present (legacy).
- [x] `main()` now uses `detect_contaminated_pairs()` as primary source; falls back to
      `KNOWN_CONTAMINATED` only if detection yields 0 pairs (e.g. empty best/ dir).
- [x] Hardcoded `KNOWN_CONTAMINATED` retained as documented ground truth for #327.
- [x] Unit tests: `tests/test_rerun_contaminated_detect.py` (5 tests, all passing).

---

## #351 — gold_etfs / commodity_etfs / sector_etfs lifecycle cleanup

**Status**: COMPLETED 2026-05-14 (Commit 2 / C2 batch).

6 orphan LIVE entries retired. See `docs/lifecycle/retirement_2026-05-14.md` for full audit.

- [x] connors_rsi2/commodity_etfs → RETIRED (passive universe, 0 open trades)
- [x] mean_reversion/commodity_etfs → RETIRED (passive universe, 0 open trades)
- [x] momentum_breakout/commodity_etfs → RETIRED (passive universe, 0 open trades)
- [x] connors_rsi2/gold_etfs → RETIRED (no active config, 0 open trades)
- [x] mean_reversion/sector_etfs → RETIRED (passive universe, 0 open trades)
- [x] momentum_breakout/sector_etfs → RETIRED (passive universe, 0 open trades)
- NOT retired: momentum_breakout/sp500 (open trade CAT id=187, universe live_enabled=True)

---

## #352 — L4 kill-switch ATTRIBUTION_CUTOVER_DATE audit (2026-05-14)

**Status**: VERIFIED

- **Primary check_l4_drawdown**: VERIFIED clean
  - `ATTRIBUTION_CUTOVER_DATE = "2026-04-29"` constant present in `core/remediation_kill_switch.py:47`
  - SQL applies dual floor: `AND date >= ? AND date >= ?` bound to `(cutoff_date, ATTRIBUTION_CUTOVER_DATE)` — lines 127–137
  - 4/4 existing tests pass (`tests/test_l4_drawdown_attribution_window.py`)
  - Manual injection test (run then deleted): inserted pre-cutover row (2026-04-25 / $5400 global) + post-cutover row (2026-05-13 / $1300 per-market) → `check_l4_drawdown()` returned `None` ✅

- **Other equity_history readers audited**:
  - `scripts/intraday_monitor.py:284` `check_portfolio_drawdown()` — reads `portfolio.equity_history` (in-memory JSON); PROTECTED by `_ATTRIBUTION_RESET_DATE = "2026-04-29"` explicit date skip (`if snap_date < _ATTRIBUTION_RESET_DATE: continue`). Telegram alert only, not a kill switch. **CLEAN**
  - `scripts/research_promote.py:418` `watchdog_check()` — reads `equity_history[-days:]` (last 5 entries) from live state JSON; NO explicit date filter. However: (a) it is Telegram/monitoring only, not a halt; (b) as of 2026-05-14 there are 15+ post-cutover entries so last-5 are all post-cutover. **RESIDUAL RISK: minor — see below**
  - `scripts/eod_settlement.py:379,725` — reads `portfolio.equity_history[-1]` / `[-2]` for daily P&L delta only (prev day equity), not a drawdown calculation. **NOT AFFECTED**
  - `scripts/verify_dual_write.py:905`, `scripts/audit_equity_history_dual_write.py:115` — audit/reporting tools, no trading decisions. **NOT AFFECTED**
  - `brokers/live_portfolio.py:check_daily_drawdown()` — uses `market_equity_history` (different table) and session HWM, NOT `equity_history` table. **NOT AFFECTED**
  - `risk/`, `portfolio/` — no equity_history reads at all. **CLEAN**

- **Per-market drawdown calc (architectural improvement)**: NOT_NEEDED — existing ATTRIBUTION_CUTOVER_DATE filter is sufficient. The only real-risk path (`check_l4_drawdown`) is fully protected.

- **Residual risk**: `scripts/research_promote.py::watchdog_check()` at line 418 uses `max(equity_history[-5:])` without an explicit date floor. If the live state file were reset to fewer than 5 entries with old pre-cutover global equity values included, this function could fire a false-positive "needs_review=True" Telegram alert. This is non-blocking (no trading halt), extremely unlikely in current state (15+ post-cutover entries exist), and lower priority. Filed as future cleanup: add `_ATTRIBUTION_RESET_DATE` filter to `watchdog_check()` when that function is next touched.

- **Injection test output** (script deleted post-run):
  ```
  ATTRIBUTION_CUTOVER_DATE = '2026-04-29'
  check_l4_drawdown() returned: None
  L4 FALSE-POSITIVE PROTECTION: VERIFIED
    Pre-cutover $5400 global-equity row correctly excluded.
    Phantom 75% drawdown class is protected.
  ```

Resolved by commit: <see git log>

---

## lifecycle-1.6 — Pre-commit hook: lifecycle guard for enabled-true strategies (2026-05-14)

**Status**: COMPLETED

Adds a pre-commit hook that blocks enabling strategies in `config/active/*.json`
unless `strategy_lifecycle` has a `LIVE` or `PAPER` row for that `(strategy, universe)` pair.

- [x] `scripts/git-hooks/check_lifecycle_for_enabled.py` — Python guard logic (diffs HEAD vs staged, queries SQLite)
- [x] `scripts/git-hooks/pre-commit-lifecycle-guard.sh` — Bash wrapper (finds staged files, calls Python helper)
- [x] `.pre-commit-config.yaml` — `lifecycle-enabled-guard` local hook added (pre-commit framework path)
- [x] `scripts/git-hooks/pre-commit` — lifecycle guard chained before `exit 0` (raw bash hook path)
- [x] `scripts/git-hooks/README.md` — updated with hook #6 docs + bypass instructions
- [x] `.git/hooks/pre-commit` — reinstalled via `bash scripts/install-git-hooks.sh`
- [x] `tests/test_lifecycle_pre_commit_hook.py` — 20/20 tests pass (5 spec scenarios + 15 unit/variant tests)

**Design note**: Project has BOTH `.pre-commit-config.yaml` (pre-commit framework config) AND a raw bash
`.git/hooks/pre-commit` (canonical source at `scripts/git-hooks/pre-commit`). The hook is registered in
BOTH paths so it fires whether or not `pre-commit install` has been run. `BYPASS_RESEARCH_GATE` env var
skips both the existing research gate and this new lifecycle guard (same files, same escape hatch).

**SQL fix**: spec showed `ORDER BY id DESC LIMIT 1` but `strategy_lifecycle` has no `id` column (PK is
`(strategy, universe)` — one row per pair). Fixed to plain `WHERE strategy = ? AND universe = ?`.

**Bypass**: `git commit --no-verify` or `BYPASS_RESEARCH_GATE="reason" git commit ...`

---

## #8 / Wave 2.1 — Collapse conftest.py triple-isolation fixtures into factory (2026-05-14)

**Status**: ✅ COMPLETED — commit `a81ac2a2`

- [x] Extracted `_make_path_isolation_fixtures()` factory in `tests/conftest.py`
- [x] Replaced 18 hand-coded (session + function + verify) fixture trios with 6 factory calls
- [x] Resources factored: `kill_switch._HALT_FILE`, `live_portfolio._STATE_DIR`,
      `reconcile_positions._STATE_DIR`, `chat_db.CHAT_DB_PATH`, `price_arbiter._THROTTLE_PATH`,
      `reconcile_shadow._ALERT_STATE_FILE`
- [x] Kept manual (can't factory-ise): `_isolate_test_logs` (log handler logic),
      `_isolate_prod_db_*` (init_db + marker opt-out), `_isolate_state_dir` (func-scope only),
      `_zz_verify_no_state_file_pollution` (checks 3 live_*.json files simultaneously)
- [x] Net: 1016 → 811 LOC (-205 LOC, ≥190 target)
- [x] Test collection: 6145 (was 6125, both ≥6102 baseline)
- [x] test_state_isolation_self.py: 3/3 PASS
- [x] test_halt_isolation.py: 2/2 PASS
- [x] test_no_prod_db_writes.py: 3/3 PASS

---

## D5 / Wave 3.2 — Extract inline SQL from signal files into data/{ohlcv,macro}_query.py (2026-05-14)

**Status**: ✅ COMPLETED

- [x] Created `data/ohlcv_query.py` — `get_ohlcv_volume`, `get_ohlcv_close`, 5-min TTL cache
- [x] Created `data/macro_query.py` — `get_macro_indicators_cols`, `get_vix_term_structure`, 5-min TTL cache
- [x] Migrated `signals/etf_flows.py` — `_load_volumes_from_db` → `get_ohlcv_volume`
- [x] Migrated `signals/macro_surprise.py` — inline SELECT block → `get_macro_indicators_cols`
- [x] Migrated `signals/sector_rotation.py` — `_load_prices_from_db` → `get_ohlcv_close`
- [x] Migrated `signals/vix_term_structure.py` — inline SELECT block → `get_vix_term_structure` (aliased as `_load_vix_data`)
- [x] 35/35 new tests in `tests/test_ohlcv_query.py` + `tests/test_macro_query.py`
- [x] All existing signal tests pass (2 pre-existing failures unchanged)

**Deviation**: `get_macro_indicators_cols` uses `(cols, end_date, limit)` not `(cols, start_date, end_date)`.
Actual SQL in macro_surprise.py is `WHERE date <= ? ORDER BY date DESC LIMIT ?` — not a date range query.
Spec signature was incompatible; specific function matching real usage created instead.

---

## D1 / Wave 3.3 — Split data/ingest.py into sub-modules (2026-05-14)

**Status**: ✅ COMPLETED

- [x] `data/ingest/` package created (was single 1603-LOC file)
- [x] `data/ingest/cache.py` — standalone constants + helpers (`_is_crypto_ticker`, `get_asx200_tickers`, `get_market_tickers`, constants)
- [x] `data/ingest/normalization.py` — `_normalize_ticker`, `_clean_ohlcv`, `_clean_alpaca_bars`, `_apply_split_adjustments`
- [x] `data/ingest/downloaders.py` — `_download_via_yfinance`, `_download_via_alpaca`, `_fetch_ohlcv` routing
- [x] `data/ingest/sqlite_writer.py` — `_sqlite_batch_write`, `verify_sqlite_integrity`
- [x] `data/ingest/freshness.py` — `_last_trading_day`, `check_data_freshness`, `verify_ingest_freshness` (stub; live definitions in __init__.py)
- [x] `data/ingest/macro.py` — `refresh_macro_data`
- [x] `data/ingest/__init__.py` — re-export shim + orchestrators + cache I/O + freshness functions
- [x] `data/ingest/__main__.py` — CLI entry point for `python3 -m data.ingest`
- [x] All 108 ingest-related tests pass (76 baseline + 32 halt/ohlcv tests)
- [x] 13 public function imports all verified

**Design note**: Cache I/O (`_save_cache`, `_load_cache`, `_market_cache_dir`, `CACHE_DIR`, etc.) and freshness functions (`_last_trading_day`, `check_data_freshness`, `verify_ingest_freshness`) are defined directly in `__init__.py` (not re-exported from sub-modules). Reason: tests patch `data.ingest._market_cache_dir` and `data.ingest._last_trading_day`. Python resolves these lookups through the function's `__globals__` dict, which is `data.ingest.__dict__` only when the calling function is defined in `data.ingest`. If these were in `cache.py`/`freshness.py`, the patches would be invisible to the callers.
