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
- [ ] **#215 — Overlay gate enforcement.** Confirm overlay signals actually
      gate order placement (not just annotate decisions). Needs end-to-end
      trace from overlay engine → plan file → executor.
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

*Last reconciled: 2026-04-17 (Wave 2 close).  Prior counters (e.g.
"253/243") were folklore — do not reinstate.*
