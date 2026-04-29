# Atlas Streamlining + Debugging Audit — Planning Lens

**Date**: 2026-04-29
**Author**: Planning Lead (Atlas)
**Scope**: Strategic audit applying architecture/sequencing/opportunity-cost lens. Companion reports from Engineering and Validation teams add code-fragility and bug-class lenses respectively.
**Method**: 4 parallel research analysts ran forensic investigations on bugs (60d git+log+DB), architecture (subsystem inventory + dependency graph), state synchronization (9 write paths, 4 reconcile paths, MU live drift), and infrastructure (cron, systemd, health-checks). All findings cross-cited in source documents:
- `/tmp/atlas-audit-bugs.md`
- `/root/.pi/expertise/research-analyst/atlas-audit-architecture.md`
- `/root/.pi/expertise/research-analyst/atlas-state-sync-audit-2026-04-29.md`
- `/root/.pi/expertise/research-analyst/atlas-audit-infra-2026-04-29.md`

---

## Executive Summary

Atlas is suffering from **state fragmentation**, not from any single broken component. The same fact (a position, a fill price, a stop order id) is independently maintained in SQLite tables, JSON state files, in-memory portfolio objects, and the broker — by code paths that write at different times under different gates. Today's 12 RCA commits closed many symptoms (atomic brackets, cancel-confirm, broker_orders, sector cap, regime confirmation, per-market equity), but the underlying invariant — *exactly one place owns each fact* — is still violated in nine concrete locations. As a result, every reconcile run can find a new way for the system to be inconsistent.

The 60-day forensic record shows ~190 bug-fix commits (21% of all commits). The dominant class is **state sync drift** (44 hits), followed by **plan/reconcile drift** (38+20), then **dual-write divergence** (14) and **ledger phantoms** (13). Bugs are *not* random — they cluster on the same five seams: (a) trades has nine writers; (b) `_assert_state_file_parity()` writes synthetic JSON entries that subsequent code reads as authoritative; (c) reconcile flows in three contradictory directions (broker→JSON, broker→SQLite, JSON→SQLite); (d) live execution and protective orders are decoupled (entry-then-protect-later); (e) operational state is stored as flat JSON config files (`pending_promotions.json` is 52KB and growing).

The strategic recommendation is a three-phase program. **Phase A (1–2 weeks): stabilization** — schedule the two cron jobs that already exist but are missing (`reconcile_ledger`, `sync_broker_orders`), repair MU's currently-invalid state, eliminate ~5 silent-failure paths, and ship the obvious quick wins. **Phase B (2–4 weeks): consolidation** — collapse 4 reconcile paths into 1, make `broker_orders` the single fill-price oracle, demote JSON state files to read-only caches, kill the parallel `_assert_state_file_parity()` writer, unify position sizing/telegram/config-loading. **Phase C (4–8 weeks): structural simplification** — formal trade state machine enforced at DB layer, schema-validated configs, decompose the three god objects (`db/atlas_db.py` 2,609 LOC, `services/chat_server.py` 3,385 LOC, `brokers/live_executor.py` 2,790 LOC), and move operational state out of `config/`.

The single most important architectural decision is **SQLite as canonical, JSON as derived cache, broker_orders as fill-price oracle, atomic-by-default order submission**. This is non-negotiable foundation; everything else is either tactical clean-up or a downstream consequence. ROI: eliminates 6 of the 7 recurring bug classes by construction. Effort: ~3 engineer-weeks for the core change, with the safety net of today's broker_orders ship and atomic bracket pattern already in place.

---

## 2. Bug Inventory

Evidence-based catalog of recurring bug classes observed in Atlas over the past ~60 days. Sources: 914 commits, journalctl atlas-dashboard/telegram-bot, atlas.db `system_log` table, RCA reports, today's atlas.log.

### 2.1 Bug Class Frequency

| Rank | Class | 60-day count | Top weeks | Status |
|------|-------|--------------|-----------|--------|
| 1 | State sync (DB↔broker↔JSON↔memory) | 44 commits | W11, W17, W18 | Multiple acute fixes Apr 20-29; **5 paths still drift-prone** |
| 2 | Reconcile/plan drift | 58 commits (38+20) | W11, W17 | broker_orders just shipped but not scheduled |
| 3 | Dual-write divergence | 14 commits | W16, W17 | Verify gate at 1/5 passes; semantic still ambiguous |
| 4 | Phantom/ghost ledger entries | 13 commits | W11, W17, W18 | Today's broker_orders + UNIQUE index largely closes |
| 5 | OCO/protective order races | 12+ commits | W18 | RCA #2A/#2B today closed cancel-replace race |
| 6 | Dashboard crashes / data binding | 12+ commits | W10, W11, W18 | Crash loop Apr 28; SyntaxError fixed |
| 7 | Research silent failures | 9+ commits | W14-W17 | Director 37d block fixed; discovery still broken |
| 8 | Config drift | ~10 commits | W17, W18 | sector_cap, overlay shadow flipped Apr 29 |
| 9 | Strategy/signal quality bugs | ~8 commits | W11, W16 | calc_atr, mtf_momentum unreachable TP, etc. |
| 10 | Infrastructure (cron/test isolation/heartbeat) | ~12 commits | W16, W17 | Test poisoning fixed; heartbeats table empty |

### 2.2 Root Cause Bucketing

| Bucket | Active Symptoms | Example | Fix Vector |
|--------|----------------|---------|------------|
| **State synchronization** | MU OPEN in SQLite, missing from `live_sp500.json` *right now*; FCX double-claim Apr 28; XLY cross-universe Apr 22; test fixture pollution Apr 24; daily_HWM wrong base Apr 28 | 9 writers to `trades`; 3 JSON writers; `_assert_state_file_parity()` is BOTH a checker and a writer | Single source of truth (Phase B) |
| **Race conditions** | Alpaca 40310 cancel-race (AVGO/XLK/CCJ/MU); LIMIT-sell phantom exit; plan duplicate insert per status flip; OCO bracket missed at entry | Sequential cancel→place; entry then async protect; non-atomic plan saves | Atomic-by-default + cancel-confirm (largely shipped today) |
| **Reconciliation drift** | 6 phantom closes today (ADI/FCX/MU/XLK/CCJ/SLV); CHTR phantom from avg-price inference; 13 superseded rows; `reconciled` poison strategy; UNTRACKED false positives across markets | Reconciler used `avg_entry_price` not actual fills; multiple reconcile paths flow in different directions | broker_orders as oracle (just shipped, not yet scheduled); collapse paths (Phase B) |
| **Data quality** | FRED key missing → 3 of 6 regime dimensions NULL since forever; Alpaca IEX stale mega-caps (fixed); GLD/XLI/XLY TP-naked 5 days; 0-byte autoresearch logs 10 days; signal write silent gap 10 days | No "fail loud" on missing config; warnings-then-continue everywhere | Hard-fail health checks (Phase A); FRED key (Phase A) |
| **Configuration drift** | sector cap silently never enforced for months; overlay stuck in shadow_mode 22 days; `live_enabled=false` on actively-trading sector_etfs; `alpaca.paper` test config leak; ETF execute_approved cron schedule not added when ETF universes went live | Configs not schema-validated; `overlay.mode` field exists but cron hardcodes `--mode log_only` | Schema-validated configs + single config loader (Phase B) |
| **Strategy/signal quality** | Director datetime bug → weights never updated 37 days; discovery 0/12 runs; mean_reversion_crypto -0.0081 Sharpe in research_best; `mtf_momentum` unreachable TP path; signal write 10-day gap | Features running but producing nothing; no viability floor on promotions | Promotion guards + silent-failure watchdog (in progress) |
| **Infrastructure** | Test suite poisoning live atlas.db hourly via healthz; dashboard SyntaxError crash-loop 16 restarts; pi-cron --system-prompt missing → API billing; heartbeats table empty (watchdog blind); supercoach Monday cron silent fail; sandbox-9strats systemd FAILED Apr 29 02:33 with no alert | No CI gate on schema/syntax; many `warn-and-continue` paths; heartbeat write may be no-op | CI guards + loud failure (Phase A) |

### 2.3 Open After Today's 12 Commits

These are the residual issues that today's RCA wave did NOT close:
1. **MU is in invalid state right now** (id=192, OPEN in SQLite, missing from `live_sp500.json`, `stop_order_id=''`). sync_protective doesn't see it because the state-tickers filter misses it. No stop will be placed until the JSON is repaired. *Critical, immediate.*
2. **`reconcile_ledger.py` not in cron** — last ran Apr 21; meant to run 09:30 AEST Tue-Sat per docstring; not installed. Untracked broker positions won't be backfilled to SQLite.
3. **`sync_broker_orders.py` not in cron** — recommended `0 4 * * *` is in script docstring but not in `/etc/cron.d`. Ran manually once today (10:23). The new broker_orders table will go stale after 7 days.
4. **`_assert_state_file_parity()` is a silent writer** — today's MU drift is exactly the failure mode this function was added to prevent; it appears to have failed silently with no alert.
5. **FRED API key still missing** — credit/DXY/yield_curve/fed_funds/unemployment_claims all NULL; regime model running at ~80% capacity.
6. **Discovery pipeline still broken** — `browse_blog.md` prompt missing; arxiv filter blocks all papers; 0 strategies from 12 runs.
7. **CAT held-stop cycling every 15 min** since pre-market — should resolve at open but is generating noise.
8. **stop_price=0 reconcile WARNING for 8 positions** every cycle (AVGO/XLI/SLV/UNG/ADI/FCX/XLK/CCJ) — benign but indicates plans without stops feeding into reconcile.
9. **Dual-write verify gate at 1/5 passes** — functionally passing but gate needs 5 consecutive runs.
10. **TSLA/PLTR plans still in queue, leverage-blocked 22+ times each** — plans need clearing.
11. **autoresearch_mean_reversion 0-byte logs 10 consecutive days** — silent-failure-watchdog detects but root cause undiagnosed.
12. **atlas-sandbox-9strats systemd FAILED at 02:33 today** — no alert path; still in failed state.
13. **Supercoach Monday 19:00 cron** — `/root/supercoach-site` does not exist; bash fails silently every week.
14. **Heartbeats table empty** — `heartbeat_watchdog.py` reports "All services healthy" because 0 rows = 0 violations. The `hb()` calls in pi-cron.sh may not be persisting rows. Watchdog is effectively blind.
15. **"SuperCoach API" healthz check is actually testing Up Bank webhook** on port 8000 — label mismatch.

---

## 3. Architecture Map + Blast Radius

### 3.1 Subsystem Inventory (97K Python LOC across 20 subsystems)

| Subsystem | LOC | Files | Primary Entrypoint | Owns | Depends On |
|-----------|-----|-------|--------------------|------|------------|
| `scripts/` | 34,488 | 74 | pi-cron.sh dispatch | Ops scripts | brokers, db, monitor, data, regime, utils |
| `research/` | 22,947 | 56 | autoresearch_nightly.py | Backtests, LLM loop, sweeps, promoter | backtest, db, strategies, utils |
| `brokers/` | 10,223 | 20 | live_executor.py | Order execution, position state, Alpaca SDK | db, monitor, journal, utils |
| `services/` | 6,836 | 7 | chat_server.py (FastAPI :8899) | Dashboard API, WebSocket chat, 25+ DB endpoints, Telegram bot, Pi session | db, brokers, monitor, overlay, signals, regime |
| `data/` | 7,272 | 17 | ingest.py | OHLCV fetch/cache, macro, FRED, AAII, CBOE, Tiingo | db, utils |
| `regime/` | 6,013 | 15 | model.py | 6-dim regime classification | db, data, utils |
| `utils/` | 5,423 | 16 | helpers.py, telegram.py, logging_config.py | Shared utilities (sizing, logging, config, Telegram) | leaf |
| `overlay/` | 4,488 | 12 | engine.py | Claude AI overlay, alt-data, news, seasonality | db, brokers, signals, data |
| `strategies/` | 4,279 | 11 | base.py | Live strategy signal generation | utils, data |
| `backtest/` | 4,276 | 8 | engine.py | Backtest framework, metrics, vol scaling | strategies, data, regime, utils |
| `db/` | 3,346 | 3 | atlas_db.py | ALL SQLite persistence (2,609 LOC god object) | leaf |
| `tests/` | 52,713 | ~120 | conftest.py | Full test suite (35% of code) | all |
| `monitor/` | 1,724 | 6 | evaluator.py, strategy_health.py | Intraday monitoring | db, brokers |
| `risk/` | 1,602 | 7 | portfolio_var.py, cross_universe_guard.py | VAR, sector cap, gross exposure | db |
| `signals/` | 1,207 | 6 | sector_rotation.py, ev_scorer.py | Supplemental signals (often unused) | db, data |
| `portfolio/` | 1,077 | 7 | constructor.py | Portfolio construction | db, strategies |
| `markets/` | 934 | 6 | sp500.py, etf_markets.py | Market definitions | config |
| `universe/` | 1,752 | 7 | builder.py, definitions.py | Universe construction + filtering | data, db |
| `journal/` | 627 | 2 | logger.py | Trade execution journal (JSONL) | db |
| `indicators/` | 391 | 2 | vol_cones.py | (Single module wrapped in package — fake subsystem) | db |
| `dashboard-ui/` | 6,100 | 19 TSX/TS | App.tsx (React 19+Vite) | React frontend | served by chat_server.py |

### 3.2 Top-Down Dependency Graph

```
EXTERNAL TRIGGERS (cron/systemd)
    │
    ▼
ORCHESTRATION LAYER
  scripts/pi-cron.sh
  scripts/eod_settlement.py
  scripts/execute_approved.py
  scripts/reconcile_positions.py     ◄─┐
  scripts/sync_protective_orders.py    │  ◄── 4 reconcile paths
  scripts/reconcile_ledger.py        ◄─┤      (3 contradictory dirs)
  scripts/reconcile_sqlite_to_broker ◄─┘
  scripts/sync_broker_orders.py        ◄── new (today)
    │
    ▼
EXECUTION CORE
  brokers/live_executor.py (2,790 LOC) ──── brokers/plan.py (958 LOC)
        │  ONLY order-placing file              │
        ▼                                       ▼
  brokers/registry.py                    portfolio/constructor.py
  brokers/live_portfolio.py              portfolio/limits.py
  brokers/alpaca/broker.py (1,921 LOC)   strategies/[11 strats]
  journal/logger.py
  monitor/lifecycle.py
    │
    ▼
PERSISTENCE — GOD OBJECT
  db/atlas_db.py (2,609 LOC) ◄── EVERYTHING (45 importers)
        │
        ├── data/atlas.db (90 MB, WAL)
        └── brokers/state/live_{universe}.json  ◄── parallel JSON
                                                    state
DATA / MARKET DATA
  data/ingest.py (1,578 LOC)        data/macro.py (922 LOC, FRED NULL)
  data/tiingo.py / data/fred.py / data/cboe.py / data/aaii.py / etc.

REGIME / OVERLAY
  regime/model.py (550 LOC)         overlay/engine.py (1,075 LOC)
    Called by: plan.py, evaluator.py, premarket pipeline

RESEARCH (parallel universe)
  research/autoresearch_nightly.py (719 LOC)
  research/loop.py / research/promoter.py / research/llm_loop_runner.py
  backtest/engine.py (1,642 LOC)
  research/strategies/  ◄── 24 research-only strategies (vs 11 live)
  research/discovery/   ◄── DEAD (0 strategies / 12 runs)

SERVICES
  services/chat_server.py (3,385 LOC, FastAPI + WebSocket + 30+ DB calls)
  services/telegram_bot.py (1,602 LOC)

UTILS (leaf)
  utils/helpers.py        — calc_position_size() [35 importers]
  utils/logging_config.py — setup_logger() [15 importers]
  utils/config.py         — load_config() [12 importers]
  utils/telegram.py       — send_message() [3 + 5 inline copies]
```

### 3.3 Load-Bearing 20% (highest blast radius)

| Rank | File | LOC | Importers | Why critical |
|------|------|-----|-----------|--------------|
| 1 | `db/atlas_db.py` | 2,609 | **45** | All persistence. Schema change = 45-file coordination. |
| 2 | `strategies/base.py` | 245 | 43 | Base class for all live strategies. Interface contract. |
| 3 | `utils/helpers.py` (`calc_position_size`) | 496 | 35 | Universal sizing function. |
| 4 | `brokers/live_executor.py` | 2,790 | 5 | **Only** order-placing code path. Real money. |
| 5 | `brokers/base.py` | 309 | 17 | BrokerAdapter ABC. |
| 6 | `services/chat_server.py` | 3,385 | (root) | Dashboard + WebSocket monolith. |
| 7 | `utils/logging_config.py` | 247 | 15 | Logger factory used everywhere. |
| 8 | `utils/config.py` | 263 | 12 | `load_config()` — but bypassed by 4+ direct json.load calls. |
| 9 | `backtest/engine.py` | 1,642 | 11 | Used by all research + live validation. |
| 10 | `brokers/plan.py` | 958 | 5 | Trade plan generation. |

### 3.4 Duplicated Logic (same job, multiple implementations)

| Concept | Implementations | Recommendation |
|---------|----------------|----------------|
| **Reconciliation** | `reconcile_positions.py` (727 LOC, hourly cron, canonical, JSON↔broker), `reconcile_ledger.py` (484 LOC, monthly intent, NOT scheduled, SQLite↔broker), `reconcile_sqlite_to_broker.py` (ad-hoc, JSON→SQLite *reverse direction*), `archive/reconcile.py` (619 LOC, archived but still in tree) | Collapse to ONE pipeline: broker→broker_orders→SQLite→JSON-cache. Phase B. |
| **Position sizing** | `helpers.py:calc_position_size()` (35 importers — canonical), `dynamic_sizing.py:DynamicSizer` (orphan, 0 live importers but config flag misleadingly says enabled=true), `backtest/vol_scaling.py:VolatilityScaler` (used only by plan.py:200-240) | Delete `DynamicSizer`. Rename `VolatilityScaler` to clarify its role. |
| **Telegram notifications** | `utils/telegram.py` (canonical), + 5 inline `_send_telegram()` copies in research/sweep, autoresearch_runner, llm_loop_runner, discovery, monitor/evaluator | Collapse to one. The 5 copies have diverged (different retry logic). |
| **Strategy definitions** | `strategies/` (11 live), `research/strategies/` (24 research). Duplicate names: `mtf_momentum`, `sector_rotation` exist in both. | Prefix research versions `proto_` to disambiguate. |
| **Overlay execution modes** | `overlay/cron.py` accepts `mode="log_only"|"active"`, but `mode="active"` has never been wired to plan.py. pi-cron.sh hardcodes `--mode log_only` ignoring `config.overlay.mode`. `overlay_enforce_validated:true` in config is dead. | Either delete `mode="active"` path or wire it. Consume `config.overlay.mode` from cron dispatcher. |
| **Live state writes** | `live_portfolio.save_state()` (gated, primary), `_assert_state_file_parity()` (post-INSERT silent self-heal — *the cause of Apr 24 test fixture contamination*), `reconcile_positions.save_internal_state()` (with `--fix` only) | Demote `_assert_state_file_parity()` to read-only check + alert. Make JSON state a derived view. |
| **Config loading** | `utils/config.py:load_config()` (canonical), + 4+ direct `json.load(open(...))` calls in broker files, + `markets/etf_markets.py` reads ETF configs directly | Centralize all loads. Add schema validation on every read. |

### 3.5 Cruft (deletion candidates, ranked by safety)

| File / Dir | Size | Classification | Safety |
|------------|------|----------------|--------|
| `data/atlas_backup_20260424_*before_market_id_migration.db` | ~90 MB | Pre-migration backup; migration done Apr 24 | ✅ Safe |
| `scripts/archive/generate_data_legacy.py` | 3,704 LOC | Explicitly archived; replaced | ✅ Safe |
| `scripts/archive/reconcile.py` | 619 LOC | Archived; replaced by reconcile_positions | ✅ Safe |
| `overlay/seasonality.py` | 276 LOC | Not imported anywhere | ✅ Safe |
| `backtest/index.py` | 447 LOC | Not imported; CLI duplicate of engine.py | ⚠️ Verify subprocess callers |
| `regime/run_gate_backtest.py` | 270 LOC | Not imported; superseded | ✅ Safe |
| `research/archive/gate208_runner.py` | 147 LOC | Concluded experiment | ✅ Safe |
| `utils/dynamic_sizing.py` | 303 LOC | Orphan; only diagnostic script imports | ✅ Safe |
| `ops/patch_overlay_sector.py` | 246 LOC | One-time patch | ✅ Safe |
| `~40 scripts/ unreferenced` | ~20K LOC | Forensic/one-shot tools | ⚠️ Move to `scripts/tools/` with README — do NOT delete |
| `research/experiments/` (139 JSONs) | varies | Mar 10 stale eval outputs | ✅ Safe (keep 1 for format) |
| `research/locks/` (186 files) | small | Should be auto-pruned by daily cron | ✅ Safe (>7d files) |
| `logs/reconciliation/` (336 hourly JSONs Mar 23-Apr 8) | varies | Stale | ✅ Safe (>30d) |
| `config/active_config_backup_*.json` + `config/archive/` | small | Never read by code | ✅ Safe |
| `dashboard/data.bak.20260409_*/` | unknown | Apr 9 migration backups | ✅ Safe |
| `indicators/` package wrapper | (1 real file inside) | Fake subsystem | ⚠️ Move `vol_cones.py` to `risk/`, delete package |
| `signals/ev_scorer.py`, `signals/vix_term_structure.py` | 191+194 LOC | Not in live trading pipeline | ⚠️ Verify before delete |
| `research/discovery/` | 748 LOC | Architecture is valid; pipeline is broken | ⚠️ Fix or formally deprecate |

### 3.6 Configs Audit Highlights

- **`config/pending_promotions.json`** is 52 KB and growing — operational database masquerading as a config file. 118 queue items, 40+ days old. Should be a SQLite table.
- **`config/promotion_log.json`** (14 KB) — same: ops audit trail in flat JSON.
- **`config/heartbeat.json`** — operational state, should be DB.
- **`config/regime.json`** — defines 6 dimensions but 3 are permanently NULL (FRED key missing). Config is misleading.
- **`config/global_risk.json`** — read by exactly 1 module (`risk/cross_universe_guard.py`). Portfolio-level risk caps are barely enforced.
- **`config/active/sp500.json` strategy params** — gen-0 from Mar 12 vs research_best gen-1 from Apr 13. 6-week stale; auto-promotion fires but doesn't promote.
- **5 of 10 config files have no schema validation** (`regime.json`, `global_risk.json`, `pending_promotions.json`, `promotion_log.json`, `heartbeat.json`).

---

## 4. Pain Point Hotspots — Top 10 Operational Pain Sources

Ranked by **frequency × severity × time-to-debug**. Each entry: pain → why current patches keep failing → what would actually fix it.

### #1 — Multiple writers to the `trades` table (9 distinct paths)

**Pain**: Every reconcile detects new drift. Phantom rows. Wrong-market closes. Test fixture pollution. Self-heal entries with synthetic dates.

**Why patches keep failing**: Each new bug discovered → new defensive guard added at *one* writer; the other 8 still write under different conditions. CHECK constraints + UNIQUE indexes were added (good), but the application still has 9 different pieces of logic that decide WHEN to write.

**Real fix**: Single canonical writer (`atlas_db.record_trade_entry/exit`). All other paths must go through it. The two raw-INSERT scripts (`reconcile_sqlite_to_broker.py`, `backfill_orphan_trades.py`) must be either deleted or refactored to use the wrapper. **Phase B work, ~4 days.**

### #2 — `_assert_state_file_parity()` is both a checker and a silent writer

**Pain**: Test fixtures from Apr 24 leaked into `live_sp500.json` because `_assert_state_file_parity()` writes synthetic JSON entries on every `record_trade_entry()` call. Synthetic entries lack `stop_order_id`, `tp_order_id`, real `entry_date`. sync_protective then reads these entries as authoritative.

**Why patches keep failing**: The function exists *because* JSON drifts from SQLite. Removing it without first making JSON a derived cache would re-expose the underlying drift. So patches keep adding more guards rather than removing the function.

**Real fix**: Make JSON state a **read-only cache** generated *from* SQLite by a single dedicated emitter. Demote `_assert_state_file_parity()` to a read-only consistency check that emits Telegram on mismatch but never writes. **Phase B work, ~3 days.**

### #3 — Reconcile flows in 3 contradictory directions

**Pain**: 18 of 63 closed trades (29%) have `exit_reason='reconcile_fill'` — meaning ~one in three exits was discovered retroactively by reconcile, not captured at execution time. Six phantoms closed at EOD today alone.

**Why patches keep failing**: `reconcile_positions` fixes JSON from broker; `reconcile_ledger` fixes SQLite from broker; `reconcile_sqlite_to_broker` fixes SQLite from JSON (reverse direction!). When JSON and SQLite diverge, each path tries to fix from a different source of truth, creating oscillating corrections.

**Real fix**: Single direction: **broker → broker_orders (already shipped) → SQLite (record_trade_entry/exit) → JSON cache**. Delete the SQLite←JSON reverse path. Schedule `reconcile_ledger` (currently unscheduled, last run Apr 21). **Phase B, ~5 days; quick win: schedule reconcile_ledger today.**

### #4 — `sync_broker_orders.py` not in cron (just-shipped table going stale)

**Pain**: Today's broker_orders table is the new source-of-truth for fill prices, but the hydrator (`sync_broker_orders.py`) is not scheduled. Recommended cron `0 4 * * *` is in script docstring only. Ran manually once today.

**Why this is dangerous**: After 7 days, the table will have stale data; `reconcile_ledger`'s priority-1 fill-price source returns nothing, falls back to inferred prices, and we re-introduce the CHTR-class bug we just fixed.

**Real fix**: Quick win — add the cron entry. Verify the script handles backlog (>7d gaps) correctly.

### #5 — Silent feature failures (overlay 22d, sector cap months, signal-write 10d, director 37d)

**Pain**: Multiple features were "running" but producing no output. Overlay shadow mode for 22 days with no flip. Sector cap "configured" but never enforced for months. Signal-write path silently failed 10 days. Research director queue gate silently blocked 37 days.

**Why patches keep failing**: There's no architectural pattern for "this feature must be alive AND effective." The silent-failure-watchdog (added Apr 19) is a reactive band-aid that scans journald for known patterns. It only catches what we know to look for.

**Real fix**: Every "feature flag" or "shadow/enforce mode" must have a **dead-man timer**: if no decision/output produced in N hours/days, fire alert. Promotion guards should fail-closed (block, alert) not fail-open (skip, continue). Today's regime confirmation gate (RCA #4C, default OFF) is the right pattern. Apply it everywhere. **Phase A & B work, threaded across improvements.**

### #6 — `live_executor.py` is 2,790 LOC and the only order-placing code

**Pain**: Every protective-order bug, every bracket bug, every PDT bug, every retry bug must be diagnosed in the same monolith. Today's atomic bracket fix (RCA #2A) is in this file. Today's cancel-confirm fix (RCA #2B) is in this file. Tomorrow's bug will be in this file.

**Why patches keep failing**: The file has accreted every concern (entry, exit, bracket synthesis, PDT, OCO, retry, leverage, reconcile-entry-fills, reconcile-exit-fills) over ~40 commits. Surface area is too large to hold in head; each fix risks breaking something else.

**Real fix**: Decompose into: `executor/entry.py`, `executor/exit.py`, `executor/protective.py`, `executor/reconcile.py`, `executor/pdt.py`, `executor/leverage.py`. Each ≤500 LOC, each with its own tests. **Phase C work, ~2 weeks. High risk, high payoff.** Don't do this until Phase B has stabilized state.

### #7 — Multi-market attribution depends entirely on JSON state files

**Pain**: Alpaca account is shared across sp500/sector_etfs/commodity_etfs. The only way Atlas knows which market owns FCX is `live_sector_etfs.json` vs `live_commodity_etfs.json`. FCX double-claim Apr 28 came exactly from this. `reconcile_ledger` doesn't have the `other_market_tickers` exclusion that `reconcile_positions` has, so it can re-introduce the double-claim.

**Why patches keep failing**: The fix is always "remove from one JSON," which is reactive. The architecture has no broker-side market tag.

**Real fix**: Two options. (a) **Cheap, sound**: Make SQLite `trades.market_id` the canonical attribution; JSON files are derived; reconcile uses SQLite as truth. Both `reconcile_positions` and `reconcile_ledger` apply same exclusion logic. (b) **Expensive, pristine**: Per-market Alpaca sub-accounts. **Default to (a) in Phase B; keep (b) as Phase D option after measuring residual collisions.**

### #8 — Operational state stored as flat JSON in `config/`

**Pain**: `pending_promotions.json` is 52 KB and growing. `promotion_log.json` 14 KB. `heartbeat.json` mixed in with config. These are databases pretending to be configs. They're not schema-validated, not transactional, not query-able by SQL.

**Why patches keep failing**: Each operational concern that needed persistence chose the path of least resistance — drop a JSON in `config/`. Now the config directory has both real configs and ops state intermingled.

**Real fix**: Move all of this to SQLite tables: `pending_promotions`, `promotion_log`, `heartbeats`. The schema design is mostly already done (the JSON shape is the schema). **Phase B work, ~2 days each = ~1 week total.**

### #9 — Hardcoded `--mode log_only` in pi-cron.sh ignores `config.overlay.mode`

**Pain**: Overlay was stuck in shadow_mode 22 days. Today's fix (RCA #3A) flipped it for sp500 only — by editing pi-cron.sh. commodity_etfs and sector_etfs remain in shadow mode. Promotion required manual editing of a shell script.

**Why patches keep failing**: `config.overlay.mode` field exists but is not read by the cron dispatcher. `overlay_enforce_validated: true` is dead config (nothing reads it). The flip path is ad-hoc.

**Real fix**: Make pi-cron.sh read `config.overlay.mode`. Add a CI check that validates "every config field has a reader." **Quick win, <1 day.**

### #10 — Test isolation has been a continuous source of production damage

**Pain**: Tests poisoned live atlas.db hourly via healthz (April 20-24). Test fixtures leaked into `live_sp500.json`. Each incident was patched reactively (`_isolate_prod_db` fixture, P0-A/P0-B JSON purges).

**Why patches keep failing**: The pattern is reactive. There's no architectural fence between "test code path" and "live code path." Tests can construct objects that touch production paths.

**Real fix**: (a) `pytest --no-network --readonly-db` flag enforced in CI. (b) `_isolate_prod_db` fixture default-on for `tests/` (not opt-in). (c) Live state files are written ONLY by code under `brokers/state_writer.py` (a single new module), and that module asserts non-test environment via env var or process name. **Phase A work, ~2 days.**

### Honorable mentions

- **Heartbeats table empty** → watchdog blind. Either `hb()` in pi-cron.sh isn't persisting or rows are being pruned.
- **`db/atlas_db.py` is a 2,609-LOC god object**. Phase C decomposition.
- **Discovery pipeline broken** (0/12 strategies). Either fix the missing `browse_blog.md` prompt + arxiv filter, or formally deprecate.
- **PDT day-trade rule blowups** — managed via `pdt_deferred_state.json`. Two-level state. Should be one mechanism.

---

## 5. Simplification Proposals — Ranked by ROI

### Proposal 1 — Single Source of Truth: SQLite canonical, JSON derived

**Statement**: SQLite `trades`, `positions` (new view), and `broker_orders` are the *only* writable representations of live state. `live_{universe}.json` becomes a read-only cache regenerated *from* SQLite by a single emitter on every state change. `_assert_state_file_parity()` is demoted to read-only audit + alert.

**Bug classes eliminated**:
- State sync drift (Class 1) — by construction
- Test fixture pollution into JSON (Apr 24)
- FCX double-claim (Apr 28) — `trades.market_id` is the single attribution authority
- MU-style invalid intermediate state (today)
- `_assert_state_file_parity()` silent failures — function deleted

**Blast radius**: 7 files modified — `db/atlas_db.py`, `brokers/live_portfolio.py`, new `brokers/state/state_emitter.py`, `scripts/sync_protective_orders.py`, `scripts/reconcile_positions.py`, delete `scripts/reconcile_sqlite_to_broker.py`. ~30 tests need updating.

**Effort**: 5-7 engineer-days. Largest item in Phase B.

**Order-of-operations**: Must come AFTER (a) `sync_broker_orders` is scheduled, (b) `reconcile_ledger` is scheduled, (c) MU live-drift is repaired, (d) trades.market_id is verified populated for all open positions.

**Risk**: Existing dashboard reads JSON state files. Mitigation: ship in two stages — (1) emitter writes alongside existing dual-writers, verified for a week; (2) flip emitter to sole writer.

**ROI**: ⭐⭐⭐⭐⭐ — eliminates 4 of the top-10 hotspots in one stroke.

### Proposal 2 — Atomic-by-default order submission (already 80% shipped)

**Statement**: Every entry order MUST submit as a bracket (entry + stop + tp). `executor` synthesizes a 2:1 R/R TP if missing (RCA #2A — shipped today). `sync_protective_orders` becomes a *belt-and-suspenders* periodic check, not the primary protection mechanism.

**Bug classes eliminated**:
- TP-naked windows (GLD/XLI/XLY 5+ days)
- Entry-then-protect-later races
- 40310 cancel-replace races (RCA #2B handled most cases)

**Blast radius**: `brokers/live_executor.py` (already changed today); `scripts/sync_protective_orders.py` (becomes audit-only); `tests/test_rca_phase*` (extend coverage).

**Effort**: 1-2 days remaining. Verify all 11 strategies emit signals with `stop_loss` and either `take_profit` or `r_r_ratio`.

**Order-of-operations**: Independent — can ship Phase A.

**Risk**: A strategy that intentionally has no TP needs explicit opt-out. Add `take_profit_policy: "synthesize"|"trailing_only"` to strategy config.

**ROI**: ⭐⭐⭐⭐⭐ — eliminates Class 5 (race conditions) at the source. Already partly shipped.

### Proposal 3 — broker_orders as the single fill-price oracle

**Statement**: `broker_orders` (shipped today) is the canonical fill price source. All exit-price inference is removed. `record_trade_exit` requires either `broker_orders.fill_price` or explicit `exit_price` arg from execution callback. Reconciler never invents a price.

**Bug classes eliminated**:
- Phantom duplicate from avg-price inference (CHTR)
- Inferred exit prices in reconcile_fill rows (18 of 63 closed trades)
- Future re-introduction of CHTR-class bugs

**Blast radius**: `db/atlas_db.py` (remove inference fallback in `record_trade_exit`), `scripts/reconcile_ledger.py`, `scripts/sync_broker_orders.py`.

**Effort**: 2-3 days.

**Order-of-operations**: Schedule `sync_broker_orders` first; verify >7 days of broker_orders coverage; then enforce.

**Risk**: `sync_broker_orders` going stale (>7d) would break exit recording. Mitigation: monitor row freshness as a healthz check.

**ROI**: ⭐⭐⭐⭐ — closes one of the most-fixed bug classes.

### Proposal 4 — Trade State Machine (formal, DB-enforced)

**Statement**: Trade lifecycle has explicit states: `PROPOSED → APPROVED → SUBMITTED → FILLED → PROTECTED → CLOSING → CLOSED → SETTLED`. State column added to `trades`. CHECK constraints disallow invalid transitions. Every state transition logs to `state_transitions` audit table.

**Bug classes eliminated**:
- Invalid intermediate states (5+ implicit states identified)
- LIMIT-fill limbo (status=SUBMITTED is now a real state)
- "Open trade with stop_order_id=''" (must be PROTECTED to be open-eligible-for-trading)
- 13 superseded rows from re-INSERT chains

**Blast radius**: `db/atlas_db.py` schema migration, `journal/logger.py`, `live_executor.py`, `eod_settlement.py`, `reconcile_*.py`, ~25 tests.

**Effort**: 5-8 engineer-days.

**Order-of-operations**: Phase C, AFTER Phase B has demonstrated state stability.

**Risk**: Migration of existing rows. Two-step migration.

**ROI**: ⭐⭐⭐⭐ — closes implicit-state bugs but only after foundation is stable.

### Proposal 5 — Collapse 4 reconcile paths into 1

**Statement**: One pipeline, one direction. `broker → broker_orders → SQLite (atomic) → JSON cache`. Delete `reconcile_sqlite_to_broker.py`. Merge `reconcile_ledger.py` into `reconcile_positions.py`; rename to `reconcile.py`. Single cron entry.

**Bug classes eliminated**:
- Oscillating corrections from contradictory reconcile paths
- "Reverse direction" JSON→SQLite that was promoting JSON ghosts to SQLite

**Blast radius**: 4 scripts deleted/merged. Cron entries consolidated.

**Effort**: 3-4 days.

**Order-of-operations**: Phase B. Depends on Proposal 1.

**Risk**: Loss of ad-hoc forensic capability. Replace with `scripts/tools/forensic_*.py`.

**ROI**: ⭐⭐⭐⭐ — meaningfully reduces operational confusion.

### Proposal 6 — Operational state out of `config/`, into SQLite

**Statement**: `pending_promotions`, `promotion_log`, `heartbeats` become SQLite tables. `config/` only contains schema-validated, code-readable, version-controlled config.

**Bug classes eliminated**:
- 52KB-and-growing JSON file as ops queue
- "Watchdog reports healthy because heartbeat table is empty"
- Schema drift in ops JSON

**Blast radius**: `research/promoter.py`, `monitor/health_writer.py`, `scripts/health_heartbeat.py`, `services/chat_server.py`, 3 migrations.

**Effort**: 3-4 days total.

**Order-of-operations**: Phase B; independent of Proposals 1-5.

**Risk**: Migration of existing pending_promotions queue. One-time script.

**ROI**: ⭐⭐⭐ — prevents future drift.

### Proposal 7 — Schema-validated configs, single config loader

**Statement**: All configs have JSON schemas. Every reader uses `utils/config.py:load_config()`. Direct `json.load(open(...))` calls are forbidden by lint rule. CI gate fails if config field has no reader.

**Bug classes eliminated**:
- Config drift (Class 5)
- Dead config fields (`overlay_enforce_validated`, `overlay.mode`)
- Configs out of sync between markets

**Blast radius**: 5 broker files, `markets/etf_markets.py`, CI rule, schema files for `regime.json`, `global_risk.json`.

**Effort**: 3-5 days.

**Order-of-operations**: Phase B; semi-independent.

**Risk**: Low. Mostly mechanical.

**ROI**: ⭐⭐⭐.

### Proposal 8 — Decompose god objects

**Statement**: Three monoliths split.
- `db/atlas_db.py` (2,609 LOC, 45 importers) → `db/trades.py`, `db/equity.py`, `db/signals.py`, `db/regime.py`, `db/broker_orders.py`, etc.
- `services/chat_server.py` (3,385 LOC) → `services/api/portfolio.py`, `services/api/research.py`, `services/api/finance.py`, `services/ws/chat.py`.
- `brokers/live_executor.py` (2,790 LOC) → `executor/entry.py`, `executor/exit.py`, `executor/protective.py`, `executor/reconcile.py`, `executor/pdt.py`, `executor/leverage.py`.

**Bug classes eliminated**:
- Surface-area-driven bugs
- Difficulty onboarding new engineers

**Blast radius**: Highest of any proposal. Use `db/__init__.py` re-export shim.

**Effort**: ~2 engineer-weeks for all three.

**Order-of-operations**: Phase C, last.

**Risk**: HIGH. Mitigation: re-export shims preserve old import paths.

**ROI**: ⭐⭐⭐.

### Proposal 9 — Idempotency-by-default for all cron jobs

**Statement**: Every cron-callable script is safe to run twice in a row. Add `idempotency_key` checks to ~10 scripts. Add `cron_runs` table for last-success-key tracking. Replace flock-non-blocking-skip with idempotent re-execution.

**Bug classes eliminated**:
- Plan duplicate insert per status flip
- Reconcile-fill double-recording
- Flock skip silently → job missed entirely

**Effort**: 3-5 days.

**Order-of-operations**: Phase A (incremental).

**Risk**: Low.

**ROI**: ⭐⭐⭐.

### Proposal 10 — Health-checks fail loud (no silent skip-with-warning)

**Statement**: Every health-check has exactly two outcomes: PASS or FAIL+Telegram. No "warn and continue."

**Bug classes eliminated**:
- 22-day overlay shadow drift
- Months of silent sector_cap non-enforcement
- 10-day signal-write gap
- 0-byte autoresearch logs lasting 10 days
- atlas-sandbox-9strats FAILED with no alert

**Blast radius**: ~15 health-check call sites get alerting.

**Effort**: 2-3 days.

**Order-of-operations**: Phase A.

**Risk**: Telegram noise. Mitigation: 4h cooldown per alert hash.

**ROI**: ⭐⭐⭐⭐.

### Proposal 11 (REJECT) — Per-market Alpaca sub-accounts

**Why reject**: Capital fragmentation breaks portfolio-level VaR and gross-exposure caps. Three minimum buying-power floors and three FINRA day-trade buckets ($25k PDT threshold each). Operational complexity (3 broker connections, 3 secret rotations). Proposal 1 eliminates 95% of the collision risk for far less cost.

**Revisit when**: AUM > $50K and PDT no longer binding; OR if Proposal 1 + state machine fail to eliminate cross-market collisions.

### Proposal 12 (DEFER) — Reduce live strategy count

**Why defer**: Need Validation team's strategy-level performance data first. Premature pruning could remove regime-diversifying strategies. Add to Phase B exit criteria: Validation provides strategy P&L attribution; Planning recommends pruning candidates.

---

## 6. Phased Execution Plan

### Phase A — Stabilization (1-2 weeks, ~10 working days)

**Goal**: Eliminate active drift loops and make existing safety machinery actually run. No architectural changes.

**Tickets**:

| # | Item | Effort | Acceptance criteria |
|---|------|--------|---------------------|
| A1 | Add `sync_broker_orders.py` cron entry `0 4 * * *` | 0.5h | Cron file diff applied; manual test triggers a fresh sync; broker_orders rows updated daily |
| A2 | Add `reconcile_ledger.py` cron entry `30 9 * * 2-6` | 0.5h | Cron file diff applied; first run logs `reconcile_ledger` lines; healthz drift count drops |
| A3 | Repair MU live state (id=192) | 1h | MU appears in JSON; stop_order_id populated; sync_protective places stop on next cycle |
| A4 | Remove SuperCoach Monday cron entry | 0.25h | crontab no longer references `/root/supercoach-site` |
| A5 | Fix "SuperCoach API" healthz label | 0.25h | Healthz log says "up_bank_webhook" |
| A6 | Add Telegram alerts on: `verify_dual_write`, `classify_and_record`, `compute_daily_risk`, `validate_oos`, atlas-sandbox-9strats failures | 1d | All 5 paths emit Telegram on failure |
| A7 | Make `_assert_state_file_parity()` failure emit Telegram | 0.5d | Today's MU drift would have alerted |
| A8 | Add log rotation for unrotated logs | 0.5d | All five files truncate weekly |
| A9 | Add `--after` parameter to `get_broker_fill_price()` | 0.25d | Re-entered tickers return correct fill price |
| A10 | Audit heartbeats table — fix `hb()` calls in pi-cron.sh | 0.5d | Heartbeats table has rows after one cron cycle |
| A11 | Schedule `reconcile_ledger.py` AFTER A1 verifies broker_orders is populated 7+ days deep | 0.25d | reconcile_ledger uses broker_orders priority-1 |
| A12 | FRED API key — register, populate, verify | 0.5d | regime model at full 6 dims |
| A13 | Clear stale TSLA/PLTR plans | 0.25d | No leverage_gate_blocked spam |
| A14 | Discovery pipeline: fix or formally deprecate | 1d | Either 12 runs produce non-zero strategies, or both schedules disabled |
| A15 | Convert all health-checks to fail-loud (Proposal 10) | 1.5d | Every "warn and continue" path either rises or is documented as deliberately silent |
| A16 | CI gate: `pytest --no-network`, schema-validated configs, no `json.load(open(...))` outside `utils/config.py` | 1d | CI blocks PRs that violate |

**Phase A Exit Criteria**:
- Healthz drift count = 0 for 5 consecutive runs
- `verify_dual_write` gate at 5/5 passes
- All 12 active issues from §2.3 resolved or downgraded to Phase B
- No silent failures in last 48h
- `broker_orders` row freshness <24h, monitored

**Rollback plan**: Each ticket is independent. State changes (MU JSON edit) backed up first.

### Phase B — Consolidation (2-4 weeks, ~15-20 working days)

**Goal**: One source of truth. One reconcile direction. One config loader. Operational state out of `config/`. Eliminate redundant code paths.

**Tickets**:

| # | Item | Effort | Depends on | Acceptance criteria |
|---|------|--------|------------|---------------------|
| B1 | Single state-file emitter; demote `_assert_state_file_parity()` to read-only | 5d | Phase A | One module owns JSON state writes |
| B2 | Make `record_trade_exit()` require non-NULL `exit_price`; reconcile_ledger reads `broker_orders` priority-1 | 2d | A1, A11 | All 18 historical reconcile_fill rows validated; no new "inferred" exits |
| B3 | Collapse 4 reconcile paths to 1 | 4d | B1 | One cron entry; one log file; one source of truth |
| B4 | Move `pending_promotions.json` → SQLite | 2d | — | Promoter reads/writes to DB; JSON deleted |
| B5 | Move `promotion_log.json` → SQLite | 1d | B4 | DB write-only; JSON deleted |
| B6 | Move `heartbeat.json` operational state → SQLite | 1d | A10 | Heartbeats are queryable by SQL |
| B7 | Schema-validated configs everywhere | 3d | — | All configs schema-validated; no orphan fields |
| B8 | Make `pi-cron.sh` consume `config.overlay.mode` | 1d | B7 | Overlay mode is config-driven |
| B9 | Consolidate Telegram callers | 1d | — | One Telegram code path |
| B10 | Consolidate position sizing | 0.5d | — | One canonical `calc_position_size()` |
| B11 | Idempotency-by-default for cron scripts | 3d | — | Each cron script safe to rerun |
| B12 | Cruft cleanup | 0.5d | Verification | ~150MB reclaimed; ~5K LOC pruned |
| B13 | Move 40 unreferenced `scripts/*.py` to `scripts/tools/` | 0.5d | — | `scripts/` only contains cron-targets |

**Phase B Exit Criteria**:
- One canonical writer for trades, positions, equity. JSON state is derived.
- One reconcile cron path; phantom-close detections drop to <1/week
- Operational state is out of `config/`
- All configs schema-validated; CI enforces it
- Cruft pruned
- Validation team's strategy-level P&L attribution lands → input for Phase C

**Rollback plan**: Per-ticket git revert. State emitter (B1) ships behind feature flag.

### Phase C — Architectural Simplification (4-8 weeks, ~30-40 working days)

**Goal**: Structural changes. Decompose monoliths. Formal state machine. Strategy pruning based on Validation data.

**Tickets**:

| # | Item | Effort | Depends on | Acceptance criteria |
|---|------|--------|------------|---------------------|
| C1 | Trade state machine | 6d | Phase B | All open positions have valid state; CHECK constraints catch invalid transitions |
| C2 | Decompose `db/atlas_db.py` (~6 files of 400-500 LOC) | 5d | Regression test pass | Each new module ≤500 LOC; all 45 importers still work |
| C3 | Decompose `services/chat_server.py` | 4d | C2 | Each router ≤500 LOC; dashboard works unchanged |
| C4 | Decompose `brokers/live_executor.py` (6 files) | 7d | C1 | Each module ≤500 LOC; live shadow mode 1 week before cutover |
| C5 | Strategy pruning based on Validation P&L | 2d | Validation report | Live strategy count justified |
| C6 | Decision: per-market Alpaca sub-accounts (Y/N) | 1d analysis + 5d if Y | Phase B clean for ≥4 weeks | Decision documented |
| C7 | Logrotate.d-managed log rotation | 1d | — | Standard logrotate |
| C8 | Standardize broker error handling | 2d | — | One choke-point for broker calls; counters in dashboard |

**Phase C Exit Criteria**:
- All three god objects decomposed
- Trade state machine enforced at DB layer
- Strategy count is justified
- Decision on per-market sub-accounts made
- Codebase total LOC reduced by ≥15%

**Rollback plan**: Each decomposition ships with re-export shim and feature flag.

---

## 7. Quick Wins (ship today or tomorrow, <1 day each)

In recommended order:

1. **Schedule `sync_broker_orders.py`** — `0 4 * * *`. **30 min.** Without this, broker_orders table goes stale in 7 days; we re-introduce the CHTR bug we just fixed.
2. **Schedule `reconcile_ledger.py`** — `30 9 * * 2-6` (after `reconcile_positions` 09:00–09:05). **30 min.** Stops the "29% of exits captured retroactively" pattern.
3. **Repair MU live state right now** — id=192 is OPEN in SQLite, missing from `live_sp500.json`, no stop. **1 hour.** Active real-money exposure.
4. **Remove SuperCoach Monday cron entry** — `/root/supercoach-site` does not exist. **15 min.**
5. **Fix the "SuperCoach API" healthz label** — actually testing Up Bank webhook on port 8000. **15 min.**
6. **Make `_assert_state_file_parity()` failure emit Telegram** — today's MU drift would have alerted. **2 hours.**
7. **Add Telegram alert on `verify_dual_write` failure** — currently logs only. **30 min.**
8. **Add `--after` parameter to `get_broker_fill_price()`** — re-entered tickers can return stale prices. **2 hours.**
9. **Clear stale TSLA/PLTR plans** — leverage-gated 22+ times each. **30 min.**
10. **Add `sync_protective.log` to `weekly_maintenance.sh` rotation list** — currently growing unbounded. **15 min.**

Total: ~7 hours of work. Eliminates 3 critical exposures and 7 nuisance fixes. **Ship Phase A1-A10 today**.

---

## 8. Things to STOP Doing

### 8.1 Stop maintaining dual implementations

| What | Why stop |
|------|----------|
| **JSON state files as a writable source of truth** | They're a cache. Dual-write has cost more bug-fix time than it saved. |
| **`_assert_state_file_parity()` write side** | Function deepens the dual-writer pattern that *causes* drift. |
| **`reconcile_sqlite_to_broker.py`** | Reverse-direction reconcile promotes JSON ghosts to SQLite. |
| **24 research strategies parallel to 11 live strategies in two directories** | Causes confusion (`mtf_momentum` exists in both). |
| **5 inline `_send_telegram()` copies** | Diverged retry logic. |
| **3 position sizing implementations** | `DynamicSizer` is orphan; `VolatilityScaler` is portfolio-level EWMA, not position sizing. |
| **4 reconcile paths** | One direction. One pipeline. |

### 8.2 Stop running features that aren't earning their complexity

| Feature | Status | Recommendation |
|---------|--------|----------------|
| **`research/discovery/`** | 0 strategies / 12 runs; `browse_blog.md` missing | Fix in 1 day or formally deprecate `atlas-discovery.timer` AND daily cron entry |
| **Overlay shadow mode** | 22+ days of decisions; 0 outcomes_evaluated | Ship Proposal 8; investigate evaluator outcome=0 bug |
| **`research/sweep.py`** | Legacy; only file calling `auto_promote()` | Move to autoresearch_nightly or delete |
| **`atlas-sandbox-9strats.service`** | One-shot disabled unit; FAILED today | Decide if needed; delete unit file if not |
| **`overlay/seasonality.py`** | 276 LOC; not imported anywhere | Delete |
| **`utils/dynamic_sizing.py`** | 303 LOC; orphan | Delete |
| **`signals/ev_scorer.py`, `signals/vix_term_structure.py`** | Not in live trading pipeline | Verify and delete |

### 8.3 Stop using configs/flags that have no good "off" state

| Item | Why bad |
|------|---------|
| `dynamic_sizing.enabled=true` | Nothing reads it; misleading |
| `overlay_enforce_validated: true` | Dead config field |
| `overlay.mode` (in config) | Field exists; cron hardcodes `--mode log_only` |
| `regime.dimensions.{credit,dxy,yield_curve,fed_funds,unemployment}` | All NULL because FRED key missing |

### 8.4 Stop these operational practices

| Practice | Replace with |
|----------|--------------|
| **Storing operational state as flat JSON in `config/`** | SQLite tables |
| **Hardcoding flags in pi-cron.sh that override config** | Read config |
| **`warn and continue` health-checks** | Fail loud |
| **Running `verify_dual_write.py` mid-session at 10:00 UTC** | Move to post-close |
| **Test-fixture contamination via `_isolate_prod_db` opt-in** | Default-on; opt-out only |
| **Configs without schema validation** | Schemas + CI gate |
| **Direct `json.load(open(...))` calls bypassing `utils/config.py`** | Lint rule blocks |

---

## 9. Cross-Cutting Improvements

### 9.1 Test Coverage Gaps

The `tests/` directory is 52,713 LOC (35% of codebase) — **size is not the problem**. Coverage gaps are concentrated:

| Gap | Why it matters | Fix |
|-----|---------------|-----|
| **`_assert_state_file_parity()` has no failure-mode test** | Today's MU drift is exactly this failure | `test_parity_check_alerts_on_missing_position` |
| **No integration test for "9 trades writers must produce same final state"** | Drift accumulates between writers | Property test |
| **No test for cross-market FCX-double-claim** | Apr 28 incident slipped through | `test_no_ticker_in_two_state_files_simultaneously` |
| **No test for `reconcile_ledger` priority-1 broker_orders → priority-2 inference fallback** | Today's flow is untested | Mock empty broker_orders → assert behavior |
| **No test that all configs declared in JSON have a reader** | Dead config silently lingers | CI lint rule |
| **No test that all cron entries point to existing scripts** | SuperCoach Monday cron silently fails | `test_cron_integrity.py` |
| **State machine transition tests** | 5+ implicit states untested | Phase C C1 ships with full tests |

### 9.2 Observability Gaps

| Gap | Symptom | Fix |
|-----|---------|-----|
| **Heartbeats table empty** | Watchdog reports healthy because 0 rows = 0 violations | A10 |
| **`verify_dual_write` log-only** | Inconsistencies don't surface | A6 |
| **`classify_and_record` failures non-fatal `logger.warning`** | Regime can become stale silently | A6 |
| **Flock-non-blocking-skip is silent** | Cron job missed entirely | Idempotent re-execution |
| **`overlay_decisions.outcome_evaluated=0` for all 33 rows** | Evaluator not closing the loop | Audit `overlay/cron.py:run_overlay_evaluator` |
| **`broker_orders` row freshness not monitored** | Going stale would re-introduce CHTR-class bugs | Add to healthz |
| **Log truncation to 0 bytes by healthz_hourly when log >50MB** | Loses recent diagnostic history instantly | logrotate.d (Phase C C7) |
| **Pi agent costs not surfaced** | $10+ runaway loops possible | Cost-per-day in dashboard |

### 9.3 Documentation Gaps (re-discovered repeatedly)

| Topic | Where it should live | Last re-discovered |
|-------|---------------------|--------------------|
| **9 distinct write paths to `trades`** | `docs/state-model.md` | This audit |
| **3 conflicting reconcile directions** | `docs/reconcile.md` | This audit + Apr 22 SOT consolidation draft |
| **Multi-market attribution depends on JSON state files** | `docs/multi-market.md` | Apr 28 (FCX double-claim) |
| **OAuth `--system-prompt` requirement** | `/root/AGENTS.md` (good — recently documented) | Apr 17 |
| **"Enter first, protect later" anti-pattern** | `docs/order-execution.md` | RCA #2A this morning |
| **`broker_orders` is now the fill-price oracle** | `docs/broker-orders.md` (NEW) | Today |
| **Operational state lives in SQLite, not config/** | `docs/state-model.md` | Phase B B6 |

### 9.4 Dev Workflow Improvements

| Improvement | Effort | ROI |
|-------------|--------|-----|
| **CI gate: `pytest --no-network --readonly-prod-db`** | 0.5d | Prevents test-poisoning |
| **CI gate: lint rule against `json.load(open(...))` outside `utils/config.py`** | 0.25d | Forces config consolidation |
| **CI gate: every cron entry points to existing script** | 0.25d | Prevents SuperCoach-style silent-cron |
| **CI gate: every config field has a reader** | 0.5d | Prevents dead-config drift |
| **Pre-commit `py_compile` hook** | already shipped (Apr 28) | Caught Apr 28 dashboard SyntaxError |
| **Schema migration tool** | 1d | Replace ad-hoc `scripts/migrations/*.py` with Alembic-style framework |
| **`tasks/lessons.md` indexed** | 0.5d | Currently per-project; no global learning ledger |
| **Single `make` target for "is it safe to deploy?"** | 0.5d | Combines tests pass, dual-write 5/5, no drift, broker_orders fresh |

---

## 10. Recommended Next 3 Actions

If only three things ship from this audit, ship these:

1. **Today (1-2 hours): Quick wins #1, #2, #3 from §7.** Schedule `sync_broker_orders` cron, schedule `reconcile_ledger` cron, repair MU live state. These three close active drift loops that exist *right now*. The first two protect today's broker_orders investment from going stale; the third resolves a real-money invalid state.

2. **This week (2-3 days): Phase A health-check overhaul (tickets A6, A7, A10, A15).** Convert all silent `warn-and-continue` paths to fail-loud Telegram. Audit the heartbeats table (currently empty, so the watchdog is blind). Make `_assert_state_file_parity()` failures alert. This stops new silent failures from accumulating while Phase B is in flight.

3. **Weeks 2-4 (Phase B B1 + B2 + B3): Single source of truth.** SQLite canonical, JSON derived, broker_orders as fill-price oracle, reconcile collapsed to one path. This is the architectural decision that eliminates 4 of the top-10 hotspots by construction. Without this, every individual bug fix is a patch on a fundamentally fragmented state model and the patch rate (currently 21% of all commits) will not meaningfully drop.

These three actions, in order, take Atlas from "21% of commits are bug fixes" to a posture where the bugs that remain are interesting (strategy quality, model regime detection, market-microstructure edge cases) rather than self-inflicted (state drift, missing schedules, silent feature failures).
