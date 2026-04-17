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

- [ ] **#192 — Kill JSON trade-ledger dual-write.** Atlas currently writes
      trades to both `data/state.json` and SQLite. Requires data-migration
      script + careful cutover + rollback plan. Out of scope for audit
      waves — needs a dedicated cutover window.
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
- [ ] **#239 — MRVL orphan fix audit.** Fix was shipped earlier; audit
      note: **entry/exit dates on the reconciliation record are
      inverted**. Needs a quick data-fix + a test that prevents inverted
      dates from landing again. Flag from audit reviewer.

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
