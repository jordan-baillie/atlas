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

- [ ] **#192 — Kill JSON trade-ledger dual-write.** Atlas currently writes
      trades to both `data/state.json` and SQLite. Requires data-migration
      script + careful cutover + rollback plan. Out of scope for audit
      waves — needs a dedicated cutover window.
      **Status (2026-04-20):** leak points diagnosed and patched by #250
      (commits 3e3d53a5 / d70ecc52 / aaa025d1 + #252 test-leak fixes
      30a49291 / e3afa30f / c7b17d03 / 857019f5). `verify_dual_write.py`
      Trades check now PASS manually; awaiting 5 consecutive real-cron
      PASSes (gate: 0/5 as of today; schedule `0 10 * * 2-6` UTC;
      earliest close Sat 2026-04-25).
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
- [ ] **#216 — Phase 5 coverage gap.** Research matrix has stale
      `research_best` rows; population requires a dedicated compute window,
      not a code change. Blocks #219.
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
- [ ] Pre-req: Phase B.2 cutover (7-day shadow validation complete)
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

## #PERF-TG-CONSOLIDATE — Telegram wrapper consolidation (round 2)

The 2026-05-01 efficiency audit started consolidating `_send_telegram` wrappers
to `utils.telegram.notify()`. Round 1 replaced the 4 trivial pass-throughs
(check_fred_health, check_regime_features_staleness, research/sweep,
scripts/autoresearch). Round 2 should tackle the 11+ remaining wrappers that
have non-trivial formatting logic. The right move is to:
1. Move formatting into the caller (build the message string before notify())
2. Pass the formatted string to `utils.telegram.notify()` directly
3. Delete the local wrapper

Each wrapper is small (15-40 lines) but they touch live ops — do them one
at a time with regression tests.

Remaining wrappers (all marked with `# TODO(#PERF-TG-CONSOLIDATE)`):
- `scripts/check_config_vs_research_best.py:_send_telegram_alert(analysis: dict)` — builds rich message
- `scripts/healthz_error_remediation.py:send_telegram_alert(failures, summary)` — formats failure list
- `scripts/data_integrity_monitor.py:_send_telegram_alert(hits, window_hours)` — formats hits
- `research/discovery/discovery.py:_send_telegram_digest(report)` — DailyReport formatter
- `research/autoresearch_runner.py:_try_send_telegram(text)` — has retry/dedup logic
- `monitor/evaluator.py:_send_telegram_alerts(alerts)` — alert list formatter
- `scripts/sync_protective_orders.py:send_telegram_summary(...)` — summary formatter
- `scripts/reconcile_positions.py:send_telegram_summary(...)` — summary formatter
- `brokers/price_arbiter.py:_send_telegram_bg(msg)` — threading/dedup logic
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
  
  ⚠️ **Prerequisite investigation before evaluating**: L4 kill-switch shows false-positive
  75.6% drawdown (equity_history peak 2026-04-24 = $5,429 global, vs current sp500-only
  $1,324 after 2026-04-29 per-market equity refactor). If any ASSIST/AUTO_FIX errors ever
  appear, fix dispatch will be silently blocked by L4. Operator should reset the sp500
  equity_history baseline or update `check_l4_drawdown()` to use the correct window
  (start from 2026-04-29 onward only).
