# Atlas — Streamlining & De-Bugging Audit (Engineering Lens)

**Date:** 2026-04-29
**Author:** Engineering Lead
**Scope:** Full-system architectural and engineering audit. Read-only investigation. Companion to Planning and Validation lenses.
**Source basis:** git log (last 60 days, 913 commits / 248 fix commits), live `data/atlas.db`, current `logs/atlas.log` and `logs/sync_protective.log`, recent RCA reports (Apr 27 + Apr 29), `tasks/lessons.md`, `tasks/todo.md`, code reads of `brokers/live_executor.py`, `brokers/live_portfolio.py`, the four reconcile scripts, `scripts/sync_protective_orders.py`, `scripts/eod_settlement.py`.

---

## 1. Executive Summary

Atlas is not buggy because of bad individual code — it is buggy because **the same data lives in 4+ stores with no canonical owner, and ~12 different reconcile/sync code paths each enforce their own merge order**. Every new fix adds a 13th path or another defensive guard, and the next class of drift surfaces a week later. The user's exhaustion is not a perception problem; it is the rational response to a 248-fix-commit / 60-day cadence (4.1 fixes/day, sustained).

The **single largest engineering lever** is to declare a canonical state model and demote everything else to derived caches. Concretely: `data/atlas.db` becomes the canonical store for trades, equity, and protective-order metadata; `broker_orders` (shipped today, RCA #4A) becomes the canonical fill-history cache; the broker (Alpaca) is canonical for "what we hold right now"; the JSON files in `brokers/state/live_*.json` and the per-day `plans/*.json` files become **read-derived caches that are never written-to by reconcile paths**.

This collapses ~12 reconcile paths to 2 (broker→broker_orders, broker→positions) and ~7 state stores to 2 (broker, SQLite). The current "trinity drift" (broker / SQLite / JSON disagreeing) becomes structurally impossible because there is nowhere to drift from.

**Highest-ROI recommendations**, in order:
1. **Phase A (1 week, low risk):** kill the recurring `stop_price=0` warning class with a per-position "protective-orders ledger" (3 tables, 1 cron, 4 reads to update). Wires today's `broker_orders` work into the live path. Eliminates 3 of the 4 worst recurring bugs immediately.
2. **Phase B (3 weeks, medium risk):** consolidate the 4 reconcile scripts into 1 canonical `reconcile.py` with mode flags. Delete the rest. Remove the JSON dual-write of position state.
3. **Phase C (6 weeks, structural):** introduce a strict trade state machine (PROPOSED → APPROVED → SUBMITTED → FILLED → PROTECTED → CLOSED → SETTLED) enforced by the SQLite schema; per-market broker accounts to eliminate cross-market attribution drift entirely.

Recommended next 3 actions are listed at the very bottom of this report.

---

## 2. Bug Inventory (evidence-based)

Source: git log, live logs, RCA reports. Last 60 days only.

### 2.1 Volume

| Window | Commits | `fix(...)` commits | % fix | Daily fix rate |
|---|---|---|---|---|
| Last 60d | 913 | 248 | 27.2% | 4.1/day |
| Last 30d | ~530 | ~140 | 26.4% | 4.7/day |
| Last 14d | 280 | 63 | 22.5% | 4.5/day |

The fix rate is steady at ~4.5/day. It is not improving.

### 2.2 Bug class taxonomy

| Class | Examples (commits) | Frequency (60d) | Severity | Affected subsystems |
|---|---|---|---|---|
| **State-sync drift (broker ↔ SQLite ↔ JSON)** | `562aac16` retire JSON dual-write • `61b5545f` universe-membership guard • `00f0b634` FCX double-claim • `7b014c66` per-market state file scope • `2d0cfcc3` recover XLK • `aaa025d1` 18 leak-regression tests • `aba29975` closed-trade dup index | **~25 commits** | High (capital risk) | live_portfolio, reconcile_*, sync_protective, atlas_db, executor |
| **Reconciliation inferred prices (phantom rows)** | `461bf376` CHTR forensic correction • `42e216b1` broker_orders table (RCA #4A) • `3e3d53a5` reconcile_ledger strategy lookup | **~8** | High ($ correctness) | reconcile_ledger, journal, atlas_db |
| **Race conditions (sequential where atomic needed)** | `bd3c3077` synthesize 2:1 R/R TP for atomic bracket • `743c0488` sync_protective cancel-confirm • `0ea95f8b` GLD/XLI/XLY 5+ days TP-naked | **~7** | High (could open uncovered positions) | live_executor, sync_protective, alpaca/broker |
| **Protective-order desync (stop_price=0, TP missing, stale order_ids)** | `c8c5f018` persist stop/tp_order_id on cancel-replace • `12e720ab` restore stops on 11 uncovered • `8331989a` regen stops_held_state from broker | **~6** | High (live capital uncovered) | sync_protective, executor |
| **Plan/signal data missing in downstream paths** | `d0b939d0` sp500 signal write silent-fail since 2026-04-14 (8 days dark) • `2b7ba7a3` plan duplicate row per status transition • `f482a31f` plan year bug + inverted dates | **~6** | Med-High | plan, signals, executor |
| **Configuration drift** | `d61d26f6` add earnings_blackout • `e35eb630` enable auto_approve • `91d6afc1` flip sector_etfs live_enabled • `e87497f2` overlay enforce-path completeness • the `alpaca.paper` mismatch (2026-04-25 sector_etfs blackout) | **~10** | Med (silent block of execution) | config/active, schema, executor |
| **Test pollution writing prod DB** (#251, #252) | `30a49291`, `e3afa30f`, `c7b17d03`, `857019f5` | **~6** | Med (silent corruption of prod state) | tests, atlas_db, conftest |
| **Cron / systemd timing & coverage gaps** | `7b014c66` per-market scope • `1f593c3b` close sector_etfs cron gap • `0c3694e8` portfolio_snapshots per-market • `0e42f652` sweep timer boundary | **~8** | Med | crontab, systemd/, scripts |
| **Universe membership / cross-market contamination** | `0f29427c` derive_universe live-verify hint • `00f0b634` FCX double-claim • `2d0cfcc3` XLK recovery • `61b5545f` XLY membership guard | **~6** | High (one position counted in 2 markets) | universe/membership, reconcile_*, live_portfolio |
| **Regime / overlay timing & enforcement** | `64bc3379` N-day confirmation gate • `576ac913` overlay flip out of shadow • `5dcae3e0` overlay dedup guard • `e87497f2` overlay shadow-mode wiring | **~7** | Med | regime, overlay, plan, executor |
| **PDT / leverage / risk gates** | `a3b59712` leverage gate • `8b700cd9` PDT backoff • `49ba3a61` sector_cap | **~4** | Med | executor, sync_protective |
| **Telegram noise / observability** | `d7dfee85` suppress empty notifications • `cda785ac` HTML-escape • `35428ab0` html_escape interpolated | **~4** | Low | telegram, services |
| **Schema evolution / dedup** | 21 migrations under `scripts/migrations/` since 2026-04-22 alone — superseded col, UNIQUE indexes, CHECK constraints, broker_orders table, market_state, equity_history, regime_state column, etc. | **~21 migrations** | Med | db, atlas_db |

### 2.3 Active bugs as of right now (read from logs/atlas.log + SQLite)

**A. Phantom-pending positions: 7 tickers cycle this warning every 15 minutes:**
```
reconcile_entry_fills: skipping {XLK,UNG,SLV,FCX,CCJ,AVGO,ADI} — stop_price=0 (not in plan).
Run sync_protective_orders to place stop first.
```
These are positions whose entries filled but where the plan that birthed them did not carry through `stop_price` metadata to the post-fill reconcile step. The warning has been firing all day on Apr 29.

**B. SQLite/JSON state drift right now:**
- `data/atlas.db:trades WHERE status='open'` → 5 positions: GLD (commodity_etfs), XLY+XLI (sector_etfs), CAT+MU (sp500)
- `brokers/state/live_sp500.json` → 1 position (CAT only)
- MU was opened today at 09:54 UTC and is in SQLite; not yet in the JSON state file.
- CAT in SQLite has `take_profit=NULL` and `tp_order_id=''` → **CAT is currently TP-naked** (today's RCA #1A fixed GLD/XLI/XLY but CAT remains uncovered).

**C. `save_state() skipped — broker_data_valid is False` fires 30+ times every 15 minutes, every market.** This is the broker-offline guard firing constantly because `LivePortfolio` is being instantiated by short-lived processes (cron jobs) that don't always have a fresh broker connection. The guard correctly prevents corruption, but the volume of warnings indicates the architecture is wrong: state-write code and broker-read code are interleaved in the same hot path instead of being separable.

**D. `reconcile_positions` runs three times in 90 seconds with the same 1-position discrepancy** (10:00:15 dry, 10:01:46 fix-dry, 10:01:54 fix-real on Apr 29). Each run "corrects" the same position. This is the cycle of self-correction with no convergence.

---

## 3. Architecture Map + Blast Radius

### 3.1 State model (the actual source of bugs)

```
┌────────────────────────────────────────────────────────────────┐
│                      BROKER (Alpaca)                            │
│  ground truth for: open positions, open orders, fill history   │
│  ground truth for: cash, equity, buying power                  │
└───────────────┬──────────────┬──────────────────────┬──────────┘
                │              │                      │
                ▼              ▼                      ▼
   ┌───────────────────┐ ┌─────────────┐ ┌───────────────────────┐
   │ broker_orders     │ │ live_*.json │ │ in-memory             │
   │ (NEW, today,      │ │ (per-market │ │ LivePortfolio object  │
   │ source of truth   │ │ state JSON) │ │ (ephemeral, per-cron) │
   │ for fills)        │ │             │ │                       │
   └───────────────────┘ └─────────────┘ └───────────────────────┘
                │              │                      │
                └──────┬───────┴──────────────────────┘
                       ▼
               ┌──────────────────┐
               │   data/atlas.db  │
               │  trades table    │   ← canonical for trade ledger
               │  market_state    │
               │  equity_history  │
               │  plans, signals  │
               │  + 32 more tables│
               └──────────────────┘
                       │
                       ▼
               ┌──────────────────┐
               │ plans/*.json     │   ← per-market per-day decision log
               │ (read by sync_   │      (also dual-written from
               │ protective &     │       atlas_db.record_plan)
               │ reconcile)       │
               └──────────────────┘
```

Plus four more on-disk state files:
- `data/stops_held_state.json` (held-stop retry counter — per-ticker)
- `data/pdt_deferred_state.json` (PDT-deferred stop tickers)
- `journal/*.json` (decision_journal, recently retired from dual-write — `562aac16`)
- `brokers/state/live_*.pre-*` backups proliferating

**The fragmentation is the bug.** No matter who claims to be canonical, every reconcile script today does a different priority merge:

| Field | `live_portfolio._enrich_from_plans` priority | `reconcile_ledger._lookup_strategy` priority | `sync_protective.load_plan` priority |
|---|---|---|---|
| strategy | SQLite trades → plan files → state file | state file → plan files → "reconciled" | n/a (uses plan only) |
| stop_price | broker open orders → SQLite → plan → state | n/a | today's plan → recent plans → position data |

These three paths enforce three different orderings on the SAME field. That's the bug class.

### 3.2 Subsystem inventory & dependency graph

```
              (cron / systemd timers — 49 cron + 17 timers)
                              │
       ┌──────────────────────┼──────────────────────────────────┐
       ▼                      ▼                                  ▼
   pi-cron.sh           direct cron ops              systemd-only ops
   (premarket,          (sync_protective,            (research-window@*,
    postclose,            reconcile_*,                 backup, watchdogs,
    research,             execute_approved,            heartbeat,
    reconcile,            intraday_monitor,            silent-failure,
    health-check,         eod_settlement,              dashboard-refresh,
    calibrate)            verify_dual_write)           canary-check)
       │                      │                                  │
       └──────────────────────┴──────────────────────┬───────────┘
                                                     ▼
                          ┌──────────────────────────┴─────────────┐
                          │            CORE PIPELINE                │
                          └────────────────────────────────────────┘

  ┌─ data/ingest ──┐    ┌─ signals ─┐    ┌─ plan ─┐    ┌─ executor ──┐
  │ Tiingo/Alpaca/ ├───►│ strategy/ ├───►│ regime ├───►│ live_executor│
  │ FRED/parquet   │    │ filters   │    │ overlay│    │ live_portf.  │
  │ ingest →SQLite │    │ → SQLite  │    │ →plans │    │ →broker      │
  └────────────────┘    └───────────┘    └────────┘    └──────────────┘
        ▲                       ▲              ▲              │
        │                       │              │              ▼
        │                       │              │       ┌──────────────┐
        │                       │              │       │ reconcile_*  │
        │                       │              │       │ sync_protect │
        │                       │              │       │ eod_settle   │
        │                       │              │       └──────────────┘
        │                       │              │              │
  ┌─ universe ─┐         ┌─ research ─┐  ┌─ overlay ┐         ▼
  │ membership │         │ autoresear │  │ shadow/  │    ┌──────────┐
  │ rebuild    │         │ promoter   │  │ enforce  │    │ services │
  │ filtering  │         │ discovery  │  │ engine   │    │ chat/dash│
  └────────────┘         └────────────┘  └──────────┘    │ telegram │
                                                         └──────────┘
```

### 3.3 Load-bearing 20% (changes here ripple everywhere)

| Subsystem | LOC | Why load-bearing | Dependents |
|---|---|---|---|
| `db/atlas_db.py` | ~1100 | Sole writer for trades/plans/equity/system_log; every cron and service touches it | All scripts, services, executor, portfolio, reconcile, monitor |
| `brokers/live_executor.py` | 2790 | Order placement, ALL reconcile-fill paths, halt mechanism, circuit breaker, plan execution | execute_approved, intraday_monitor, eod_settlement, sync_protective callers |
| `brokers/live_portfolio.py` | 1095 | All position-state read paths, broker-data-valid guard, save_state, equity tracking, drawdown gate | executor, monitor, reconcile, eod_settlement, dashboard |
| `brokers/alpaca/broker.py` | 1921 | The only live broker; OCO bracket logic; protective-orders sync | executor, portfolio, reconcile_*, sync_protective |
| `services/chat_server.py` | 3385 | Dashboard API (FastAPI); also has SQL queries that drifted into business logic | dashboard-ui, telegram_bot |
| `scripts/sync_protective_orders.py` | 1453 | THE protective-orders authority; held-stop / PDT / cancel-confirm logic | All open positions depend on this firing every 15 minutes |
| `brokers/plan.py` | 958 | Plan generation entry point; regime/overlay/sizing all flow through here | plan cron, executor |
| `overlay/engine.py` | 1075 | LLM-mediated decision engine; shadow vs enforce branching | plan, executor (via plan annotations) |

These ~13,800 LOC across 8 files contain ~75% of the bug surface based on commit-touch analysis.

### 3.4 Cruft / deletion candidates

| Path | Why | Verification before delete |
|---|---|---|
| `brokers/state/live_sector_etfs.json.pre-xlk-recovery-20260424T004819` | One-off backup file checked into VCS by accident | `git log` shows no commits depending |
| `brokers/state/live_sp500.db` (0 bytes) | Empty SQLite leftover | grep src for it; nothing reads |
| `tests/archive/test_reconcile.py` | Already in `archive/` | Confirm not collected by pytest |
| `AGENTS.md.bak` | Backup of agents.md | Confirm not symlinked |
| `tasks/atlas-lessons.md` (24 lines) vs `tasks/lessons.md` (139 lines) | Two lessons files; the 24-line one only covers the alpaca.paper mismatch | Merge into lessons.md |
| `dashboard/generate_data.py` | The cron comment says "retired in Phase 5"; file likely still present | grep for any remaining import |
| `scripts/reconcile_ledger.py` (484 LOC) AND `scripts/reconcile_positions.py` (727 LOC) AND `scripts/reconcile_sqlite_to_broker.py` (282 LOC) | Three separate reconcile scripts whose semantics overlap. **Merge into one canonical `scripts/reconcile.py` with `--mode {ledger,positions,sqlite}` flags, delete the others.** | Phase B work |
| `live_executor.reconcile_entry_fills` and `reconcile_exit_fills` (lines 2368, 2566 — totaling ~400 LOC inside the executor god-file) | These overlap with `scripts/reconcile_ledger.py` semantics | Phase B — merge into one reconcile module imported by both cron and executor |
| `live_portfolio.reconcile_broker_fills` (line 569) | Yet another reconcile path inside the portfolio class | Phase B |
| `live_portfolio._enrich_from_plans` AND `_enrich_from_broker_stops` | Two separate enrichment passes that both rewrite `pos.stop_price`. The second always wins. The first is dead code in any session where the broker call succeeded. | Audit, then collapse into one `_resolve_position_metadata()` |
| Disabled `atlas-research-runner.service` (deleted via `dcd0688d`) — confirm not still on filesystem | One-time check | `find /etc/systemd -name 'atlas-research-runner*'` |

### 3.5 Duplication map (same logic implemented 2+ times)

| What | Where | Recommendation |
|---|---|---|
| **Reconcile broker positions vs Atlas state** | `scripts/reconcile_ledger.py` (broker→SQLite); `scripts/reconcile_positions.py` (broker→JSON); `scripts/reconcile_sqlite_to_broker.py` (JSON→SQLite); `live_executor.reconcile_entry_fills/exit_fills`; `live_portfolio.reconcile_broker_fills`; `live_portfolio._refresh_from_broker` | **6 paths, must collapse to 1** |
| **Look up "what is this position's strategy/stop_price?"** | `live_portfolio._enrich_from_plans` (SQLite→plans→state); `reconcile_ledger._lookup_strategy` (state→plans→fallback); `sync_protective.load_plan` (today's plan→recent plans); `reconcile_entry_fills` plan_by_ticker | **4 implementations of the same lookup** |
| **Filter broker positions by universe** | `live_portfolio._refresh_from_broker` (markets.get_market); `reconcile_positions` (universe.builder + state-file union); `reconcile_ledger` (similar); `live_portfolio._update_state_positions` (defence-in-depth re-filter) | **4 places** |
| **Place / cancel protective orders** | `live_executor.place_protective_stop` / `cancel_protective_stop`; `live_executor.place_take_profit`; `live_executor.place_stops_for_plan`; `sync_protective_orders.sync_market`; `live_executor._cancel_open_orders_for_ticker` | **5 places** |
| **Equity / drawdown calculation** | `live_portfolio.equity()`; `live_portfolio.broker_equity`; `live_portfolio.check_daily_drawdown`; `portfolio/market_equity_attribution.py`; new RCA #4D pro-rata | **5 paths**; today's pro-rata is the right answer — the others should consume it |
| **Plan loading** | `live_portfolio._enrich_from_plans` (last 30 plans); `sync_protective.load_plan` (today→recent 3); `reconcile_ledger` plans glob; `plan.load_plan`; reconcile_entry_fills plan parameter | **5 places** |
| **HALT enforcement** | `live_portfolio.halted` flag; `market_state.halted` (SQLite, dual-write); `kill_switch.py`; `live_executor.emergency_halt`; `volatility_gate` blocking | **5 places**; today's `market_state` table is the new canonical store |

---

## 4. Pain Point Hotspots — Top 10

Ranked by frequency × severity × time-to-debug, derived from commit log + active warnings.

### #1 — Plan-derived stop_price/strategy reaches downstream too late, or not at all
- **Symptom:** the recurring `reconcile_entry_fills: skipping XYZ — stop_price=0` warning. Active right now for 7 tickers.
- **Root cause:** the position lifecycle spans many days, but `plan_{market}_{date}.json` files are per-day. Once a position is held for >1 day, `sync_protective` and `reconcile_entry_fills` look back through "recent plans" for `stop_price`. If the original plan's status was anything other than EXECUTED/APPROVED/PENDING_APPROVAL, the lookup fails. Or if a fill didn't make it into a plan in the first place (because it was a manual order or a previous plan rolled to next day).
- **Why patches fail:** every fix has been "look at one more place" — SQLite, then state file, then 30 most recent plans. There is no canonical per-position record for stop_price.
- **What actually fixes it:** **A `position_protective_orders` table in SQLite, INSERT-ed at fill time and never re-derived from plans.** Today's `broker_orders` cache (RCA #4A) is the prerequisite. This becomes the source of truth for "what was the configured stop for this fill."

### #2 — `reconcile_positions` cycle of self-correction
- **Symptom:** it runs three times in 90 seconds, each finds the same 1 discrepancy, "corrects" it, and the next run still finds it.
- **Root cause:** `--fix` writes to `live_*.json` but the reconciler re-reads the state file from disk each invocation. The "fix" doesn't propagate to other readers (sync_protective, executor in-memory state, dashboard).
- **What actually fixes it:** delete the JSON state file as a writable store. Make it read-only-derived from SQLite + broker.

### #3 — `sync_protective_orders` cancel-replace OCO race (40310s)
- **Symptom:** Apr 29 RCA #2B — broker rejects new stop because the old one is still ACTIVE, even though we just cancelled it. We shipped cancel-then-confirm-then-place today.
- **Root cause:** Alpaca is an eventually-consistent system. Cancel returns 202 Accepted before the order is actually GONE. We previously waited 0ms.
- **Why the patch may not be enough:** the "confirm" loop polls `get_open_orders` with a deadline. If the deadline is too short for one Alpaca slow path, we still race. If too long, sync_protective's 5-min cron timeout is at risk.
- **What actually fixes it:** **OCO brackets at submission time** (eliminates the cancel-replace path entirely for new entries). Today's `bd3c3077` synthesizes a 2:1 R/R TP when the strategy doesn't supply one — this is good. But sync_protective still needs to cancel-replace on the trailing-stop ratchet path. That can move to "always atomic via Alpaca's `replace_order` API instead of cancel+place."

### #4 — `LivePortfolio.broker_data_valid=False` warning storm
- **Symptom:** "save_state() skipped — broker_data_valid is False (would corrupt live_sp500.json)" — 30+/15min, every market.
- **Root cause:** `LivePortfolio` is constructed by short-lived cron processes (sync_protective, intraday_monitor, eod_settlement) that may or may not have a connected broker. The instance is created early, broker fails to connect, save_state is called anyway later, the guard fires.
- **Why the patches haven't worked:** the guard is correct — it prevents corruption. But the guard fires SO OFTEN that it has become noise. The fix is structural: split read-only state inspection (`get_state`, `get_positions_view`) from write-back (`save_state`) into separate classes/methods. Read-only callers never accidentally trigger save_state.

### #5 — Cross-market position attribution drift (FCX, XLY, XLK pattern)
- **Symptom:** Apr 28 `00f0b634` — FCX held in commodity_etfs JSON, but actually opened by sp500 connors_rsi2. P&L attribution wrong for both markets.
- **Root cause:** All universes share ONE Alpaca account. Each market's reconcile script claims broker positions whose ticker matches its universe. If a ticker is in multiple universes (FCX is in both sp500 and commodity_etfs implicitly), both markets claim it.
- **What actually fixes it:** the **strategy-tagged client_order_id** Atlas already uses (`atlas_entry_<universe>_<strategy>_...`). When parsing broker fills, the universe MUST come from the order's client_order_id, never from "which universe's reconcile script ran first." Today's `0f29427c` derive_universe is moving in the right direction but the broker fill itself should carry the universe tag.

### #6 — Inferred fill prices (CHTR class)
- **Symptom:** Apr 29 RCA #1B. CHTR was recorded with an inferred fill price taken from Alpaca's position avg_entry_price, which Alpaca had averaged across multiple fills incorrectly.
- **Root cause:** reconcile_ledger inferred when it could have JOIN-ed.
- **Status:** today's `42e216b1` `broker_orders` cache + reconcile_ledger now reads from broker_orders FIRST. **This is the right fix.** Just needs to be EXTENDED to all other inference points (8+ places where `pos.entry_price` is used as authoritative).

### #7 — Multi-day TP-naked positions (GLD/XLI/XLY/CAT class)
- **Symptom:** Apr 29 RCA #1A — three positions held 5+ days with no take-profit. CAT still TP-naked as of right now.
- **Root cause:** `_execute_entry` did not synthesize a TP if the strategy didn't provide one, and the OCO bracket on submit was not enforced. Today's `bd3c3077` synthesizes 2:1 R/R; CAT predates this fix.
- **What's still needed:** a "TP-coverage" healthcheck that runs on every `sync_protective` invocation and SCREAMS if any position has been TP-naked for >1 day.

### #8 — Plan-status mutation chain duplicates rows (`2b7ba7a3`)
- **Symptom:** `_save_plan` was INSERT-ing instead of UPDATE-ing on each status transition (DRAFT → PENDING_APPROVAL → APPROVED → EXECUTED). One real plan = up to 4 rows in `plans` table.
- **Root cause:** `INSERT` instead of `UPSERT ON CONFLICT(date,market_id) DO UPDATE`.
- **Status:** fixed today's `2b7ba7a3`. But the pattern (use ON CONFLICT for any state-machine mutation) is not enforced anywhere in atlas_db.py. **Need a single helper `upsert_state(table, key, fields)` that every state-machine writer uses.**

### #9 — Cron timing chaos (49 cron entries + 17 systemd timers, partly overlapping)
- **Symptom:** sync_protective runs every 15min × 3 markets × 8h window = 90+ runs/day. reconcile_positions runs at 0/2/5 minutes past 9 AEST for 3 markets. intraday_monitor runs every hour × 3 markets. They all hit the same broker, same DB, same state file. The sequence ordering is implicit (assumed by minute offsets, not enforced).
- **Root cause:** decisions to add a new cron entry have been made tactically, never strategically.
- **What fixes it:** **a single "atlas_orchestrator" systemd timer** that owns the per-market 15-min cycle and runs the steps in the correct sequence (sync_broker_orders → reconcile → sync_protective → emit healthz) atomically. No more flock-based coordination.

### #10 — Test pollution writing prod DB (#251, #252)
- **Symptom:** running pytest dirties `data/atlas.db`. Took two rounds of conftest.py fixtures to actually fix.
- **Root cause:** the production DB path is module-level config; tests didn't override it; `INSERT OR REPLACE` calls leaked through module-scope fixtures that resolved before function-scope autouse fixtures.
- **Status:** fixed via session-scope `_isolate_prod_db_session`. **But the deeper issue is structural:** `db.atlas_db.get_db()` resolves the DB path from a module global. Any code that does NOT honor the test override (e.g. ad-hoc scripts, future code paths) re-introduces the leak. **Move to dependency injection: every function that writes takes a `db_path` parameter.**

---

## 5. Simplification Proposals (Ranked by ROI)

### P1 — Declare a canonical state model and make everything else a derived cache

**Statement:** SQLite (`data/atlas.db`) is canonical for trades, equity, plans, signals, market_state, and protective-order metadata. Broker is canonical for "what we hold right now" and fill prices (read via `broker_orders` cache). The JSON files in `brokers/state/live_*.json` become **read-only views regenerated from SQLite + broker on demand by the dashboard**. Plan files (`plans/*.json`) are append-only audit logs only — no reconcile/sync code reads from them as a source of truth.

**Bug classes eliminated:**
- All "JSON ↔ SQLite drift" bugs (#1, #2, #5 above)
- All "which plan file should we read for this position's stop_price?" bugs (#1, #7)
- All "the state file lags SQLite by N minutes" bugs (entire class)
- Test-pollution leakage class (no JSON to dirty)

**Blast radius:**
- Touches: `live_portfolio.py` (rewrite save_state/load_local_state), all 4 reconcile scripts, sync_protective, eod_settlement, dashboard `chat_server.py` queries
- ~12 files modified, ~8 new tests, ~6 deleted scripts/methods
- No downtime if done in two phases: (a) make SQLite write-canonical while JSON remains read-write fallback; (b) flip JSON to read-only once SQLite is verified at parity for 1 week
- Tests required: state-file parity contract test (already exists: `tests/test_state_file_sqlite_parity.py`); extend to cover all fields

**Effort:** 3-4 weeks (1 senior engineer, full-time)

**Order:** must come AFTER P2 (`broker_orders` extension) so SQLite has all the fill-history data it needs to be canonical for fill prices. Today's RCA #4A is the prerequisite.

**Risk:** medium. Mitigation: 1 week of dual-write parity verification before flipping JSON to read-only. The verify_dual_write.py harness already exists; extend it.

**ROI:** kills 5 of the top 10 hotspots. Eliminates ~25 commits worth of bug-class.

---

### P2 — Extend `broker_orders` to be the universal fill-price source

**Statement:** Every code path that today reads "fill price" from `Position.avg_entry_price`, `pi.cost_basis / pi.shares`, or any inferred derivation must instead JOIN against the `broker_orders` table by order_id. Where order_id is not available (legacy positions), backfill it from broker fill history at next sync.

**Bug classes eliminated:**
- Inferred fill prices (CHTR class — hotspot #6)
- "Position carries averaged entry price" subtle slippage attribution bugs
- Equity history rows with rounded/inferred prices

**Blast radius:**
- Touches: `live_portfolio._refresh_from_broker` (fill-price column), `reconcile_ledger.py` (already done today), `reconcile_positions.py` (entry_price drift detection), `eod_settlement` (slippage_bps calculation), every "TRADE_OPENED" log line
- ~7 files modified
- Zero new tables (broker_orders shipped today)
- Tests: extend `test_rca_phase4a_broker_orders.py` (already 18/18 passing) to cover the consumer code paths

**Effort:** 1 week

**Order:** can start immediately. Independent of P1 but a prerequisite for P1's "SQLite canonical for fills" claim.

**Risk:** low. Each consumer is a one-line code change (read from JOIN instead of from Position object). 100% reversible.

**ROI:** eliminates an entire bug class (inferred prices) that has surfaced 4-5 times.

---

### P3 — Collapse 12 reconcile paths to 2

**Statement:** Replace `scripts/reconcile_ledger.py`, `scripts/reconcile_positions.py`, `scripts/reconcile_sqlite_to_broker.py`, `live_executor.reconcile_entry_fills`, `live_executor.reconcile_exit_fills`, `live_portfolio.reconcile_broker_fills`, and `live_portfolio._refresh_from_broker`'s reconcile-y bits with a SINGLE module `core/reconcile.py` exposing two functions:

```python
def reconcile_fills(market_id: str, broker, db, dry_run=False) -> ReconcileReport:
    """Sync broker fills → broker_orders → trades. Idempotent."""

def reconcile_positions(market_id: str, broker, db, dry_run=False) -> ReconcileReport:
    """Compare broker positions vs trades(status='open'). Reconcile drift. Idempotent."""
```

Cron calls these. Executor calls these. Portfolio reads from `db.get_open_trades(market_id)` directly, never reconciles.

**Bug classes eliminated:**
- All "which reconcile path runs first wins" inconsistencies (hotspot #2, #5)
- All "reconcile A says X, reconcile B says Y" race conditions
- Reduces 11 places-where-strategy-is-resolved to 1

**Blast radius:**
- New file: `core/reconcile.py` (~600 LOC, replacing ~2200 LOC across 5 files)
- Deleted: 3 scripts, ~400 LOC inside `live_executor.py`, ~200 LOC inside `live_portfolio.py`
- Net code reduction: ~70%
- Tests: write contract tests against the canonical interface; current tests can be ported

**Effort:** 3 weeks. Highest design-cost item — needs spec-first.

**Order:** AFTER P1 + P2.

**Risk:** medium-high. This is the kind of refactor that breaks subtle invariants. Mitigation: behavior-preserving migration — write the new module, run BOTH old and new in parallel for 1 week, alert on divergence, then flip.

**ROI:** 70% LOC reduction in the most bug-dense area of the codebase. Eliminates the duplication map's #1 entry.

---

### P4 — Atomic-by-default order submission (kill the cancel-replace pattern)

**Statement:** Every order submission must be either (a) an OCO bracket including stop + TP at submit time, or (b) a single `replace_order` call (atomic at the broker). The cancel-then-place pattern in `sync_protective_orders.cancel-confirm-place` is a workaround for a class of broker race we can avoid entirely with `replace_order`.

**Bug classes eliminated:**
- 40310 races (hotspot #3)
- 5+ days TP-naked (hotspot #7) — every entry ships with TP atomically
- "Cancelled the stop, broker still has stop, new stop rejects" cycle

**Blast radius:**
- Touches: `live_executor._execute_entry` (already partially done today via `bd3c3077`), `sync_protective_orders` cancel-replace path, `alpaca/broker.py` OCO logic
- ~4 files modified
- Tests: extend `test_rca_phase2b_sync_cancel_confirm.py`; new tests for `replace_order` path

**Effort:** 1 week

**Order:** can run in parallel with P2.

**Risk:** medium. Alpaca's `replace_order` has subtleties for OCO legs. Need to test against live broker (paper account) before flipping.

**ROI:** eliminates 2 of the top 10 hotspots permanently.

---

### P5 — Trade state machine enforced by SQLite schema

**Statement:** Add a `state` column to `trades` with CHECK constraint `state IN ('PROPOSED','APPROVED','SUBMITTED','FILLED','PROTECTED','CLOSED','SETTLED')`. Add transitions audit table `trade_state_transitions(trade_id, from_state, to_state, ts, actor)`. Every state-changing call goes through `db.transition_trade(trade_id, to_state)` which validates the transition.

**Bug classes eliminated:**
- "phantom exit recording when LIMIT sell unfilled" (`a3f0a7e0` and similar fixes)
- "plan-status duplicate rows" (hotspot #8)
- Every "the trade is in two states at once" bug class

**Blast radius:**
- New table + migration
- Touches every code path that mutates `trades.status` — currently `live_portfolio.execute_exit`, `journal/logger.py`, `reconcile_*` scripts, `live_executor._execute_entry`
- ~10 call sites need to use the new transition helper
- Tests: state-machine round-trip test (PROPOSED→APPROVED→...→SETTLED)

**Effort:** 2 weeks

**Order:** AFTER P3 (reconcile collapse) so there's only one place per state-transition to update.

**Risk:** low-medium. Well-trodden pattern in finance systems. Mitigation: ship in shadow mode (write to new column without enforcing transitions) for 1 week, then flip.

**ROI:** eliminates a long tail of "trade in inconsistent state" bugs that don't have a single common pattern.

---

### P6 — Per-market broker accounts

**Statement:** Today all 7 universes share one Alpaca account. This forces every reconcile to filter-by-universe (which produces hotspot #5). Move sp500 + commodity_etfs + sector_etfs to separate sub-accounts. Each market's reconcile script then claims ALL positions under its account, no filter.

**Bug classes eliminated:**
- All cross-market attribution bugs (hotspot #5)
- All "FCX held in two state files" bugs
- The `derive_universe` lookup chain entirely

**Blast radius:**
- Operational: get 3 Alpaca sub-accounts (operator action, not code)
- Code: each `LivePortfolio.connect()` connects to its market-specific account
- Equity attribution becomes account-level — RCA #4D's pro-rata logic is no longer needed

**Effort:** 1 week of code + operational lead-time for Alpaca account creation

**Order:** independent. Can defer until other simplifications are done.

**Risk:** low (code) / depends-on-Alpaca (ops). Alpaca offers paper sub-accounts trivially; live sub-accounts may need broker dialogue.

**ROI:** eliminates an entire bug class permanently. Removes the need for several universe-membership guards.

---

### P7 — Plan files: append-only audit log only

**Statement:** Today plan files at `plans/plan_{market}_{date}.json` are read by sync_protective, reconcile_ledger, and live_portfolio.\_enrich_from_plans as a source of truth for stop_price/strategy. They are also dual-written from `atlas_db.record_plan`. Move to: SQLite `plans` table is canonical, plan files are append-only audit logs (write once at plan-generation, never overwritten on status transition).

**Bug classes eliminated:**
- "which plan file is the right one for this 5-day-old position?" (hotspot #1)
- Plan-status row duplication (hotspot #8 cause)
- The `cleanup_stale_plans.py` cron job (deletes after 14 days) becomes irrelevant since stale plan files are harmless audit logs

**Blast radius:**
- Touches: `brokers/plan.py:_save_plan`, `sync_protective.load_plan`, `live_portfolio._enrich_from_plans`, `reconcile_ledger`
- ~5 files modified, mostly deletions
- Tests: extend `test_plan_generator` to assert write-once semantic

**Effort:** 1 week

**Order:** depends on P1.

**Risk:** low.

**ROI:** removes 1 hotspot, simplifies multiple lookup paths.

---

### P8 — Single orchestrator timer, no flock-based coordination

**Statement:** Replace 49 cron entries + 17 systemd timers with: one cron-driven orchestrator (`atlas_cycle.sh`) per market that runs every 15 min and sequences `sync_broker_orders → reconcile → sync_protective → emit_healthz` atomically. Daily orchestrators (premarket, postclose) become a separate one. Research timers remain on systemd.

**Bug classes eliminated:**
- "two cron jobs hit the same lock" races
- "reconcile ran before broker_orders synced, so it inferred prices" timing bugs
- The flock-based 12-job-per-market coordination problem

**Blast radius:**
- New scripts: `atlas_cycle.sh`, `atlas_daily.sh` (~150 LOC each)
- Crontab pruned from 49 entries to ~8
- Tests: orchestrator step-sequencing test

**Effort:** 1 week

**Order:** after P1+P2+P3 (otherwise the orchestrator just shuffles broken sub-jobs).

**Risk:** medium — operational change. Mitigation: run new orchestrator alongside old crontab for 3 days with diff alerts, then flip.

**ROI:** eliminates a chronic source of timing-related bugs and dramatically simplifies operational mental model.

---

### P9 — Reduce strategy count

**Statement:** Atlas runs 9 strategies in research, but only ~4-5 actually contribute material P&L per the recent leaderboards in `tasks/lessons.md`. Consider retiring or moving to "research-only" any strategy that:
- Hasn't passed the combined OOS gate in last 60 days, AND
- Hasn't generated >1% of recent P&L

**Bug classes eliminated:**
- Strategy-specific bugs (mtf_momentum trailing-elif, sector_rotation rebalance-aware, opening_gap dict-comparison)
- ~5 commits in the 60-day window are strategy-specific bug fixes

**Blast radius:**
- Configuration only (`strategies.{name}.enabled = false`)
- Tests: existing strategy tests stay
- Operational: research keeps running these as "candidates"; live execution skips

**Effort:** 0.5 day (config change) + monitoring window

**Order:** independent. Can do in Phase A.

**Risk:** very low.

**ROI:** small but cumulative. Reduces config surface and maintenance burden.

---

### P10 — Standardize broker call wrapping (every call retried, logged, error-classified)

**Statement:** Today some Alpaca calls go through `_broker_call` (retry wrapper), others are direct. Standardize so EVERY broker network call goes through one wrapper that: (a) classifies errors into transient/permanent/auth/rate-limit; (b) retries with backoff for transient; (c) emits a structured log line per call; (d) updates broker_call_metrics table.

**Bug classes eliminated:**
- "Alpaca threw a 503 once, we crashed instead of retrying" (multiple recent fixes)
- Inability to debug broker-related issues (today: errors are spread across 5 logging styles)
- The PDT/leverage/halt/circuit-breaker patchwork of pre-call guards

**Blast radius:**
- Touches: `brokers/alpaca/broker.py` (~10 call sites still bypass the wrapper)
- Tests: contract test that asserts every alpaca-py method call is wrapped

**Effort:** 1 week

**Order:** independent.

**Risk:** low.

**ROI:** moderate. Improves observability and reliability of every broker interaction.

---

## 6. Phased Execution Plan

### Phase A — Stabilization (1-2 weeks, no architectural changes)

**Goal:** kill the highest-frequency active bugs without changing the architecture. Buy time for Phase B.

**Work items:**
- A.1 (1 day) — **Position-protective ledger:** add `position_protective_orders(trade_id, ticker, market_id, stop_order_id, stop_price, tp_order_id, tp_price, last_synced_at)` table + INSERT at fill-time + UPDATE on cancel-replace. `reconcile_entry_fills` reads from THIS table for stop_price, no longer from plan files. Kills the `stop_price=0` warning class entirely. Hotspot #1.
- A.2 (1 day) — **TP-coverage healthcheck:** every `sync_protective` invocation, query `SELECT * FROM trades WHERE status='open' AND tp_order_id=''`. If any row is older than 1 day, emit a CRITICAL Telegram alert. Hotspot #7. Fixes CAT today.
- A.3 (0.5 day) — **Dedup `_save_plan` UPSERT pattern:** today's `2b7ba7a3` fixed plans table; audit `signals`, `equity_history`, `market_state` for the same INSERT-instead-of-UPSERT pattern.
- A.4 (1 day) — **broker_orders cron entry:** the new `sync_broker_orders.py` (commit `42e216b1` today) is in code but not yet in cron. Add `0 4 * * *` entry. Verify daily fill-completeness over a week.
- A.5 (1 day) — **Reconcile loop convergence:** make `reconcile_positions.py --fix` idempotent in the obvious way: if `state.positions` already matches broker positions byte-for-byte, exit 0 without writing. Eliminates the cycle of self-correction (hotspot #2).
- A.6 (1 day) — **Broker-data-valid logging fix:** `LivePortfolio.save_state()` should warn ONCE per process invocation, not 30+ times. Throttle via instance flag.
- A.7 (1 day) — **Stop-the-bleed:** retire 3 of the 9 strategies (P9 above) — pick the bottom 3 by 60-day P&L contribution.
- A.8 (1 day) — **Crontab cleanup:** remove dead entries, dedupe redundant lock files, verify each lock file has exactly one writer.

**Exit criteria:**
- No `stop_price=0` warning in 7 consecutive days
- TP-coverage healthcheck PASS for 7 consecutive days
- `verify_dual_write.py` green for 7 consecutive days
- No `broker_data_valid is False` warning storm

**Rollback:** every item is reversible. A.1's new table is additive (nothing reads from it until you flip the flag). A.2 is a notification-only change.

**Effort:** ~7-8 working days, can be done by 1 engineer or split across 2 in parallel (A.1+A.2 on one stream, A.3-A.8 on the other).

---

### Phase B — Consolidation (2-4 weeks, reduce code paths)

**Goal:** eliminate the duplication map. Same behavior, fewer paths.

**Work items:**
- B.1 (3 days) — **Extend broker_orders consumers (P2):** every `pos.entry_price` / `pos.cost_basis` read becomes a JOIN against broker_orders. ~7 file changes.
- B.2 (5 days) — **Single reconcile module (P3):** ship `core/reconcile.py`. Run in parallel with old paths for 1 week with divergence alerts. Then delete old paths. ~70% LOC reduction.
- B.3 (5 days) — **JSON state files become read-only-derived (P1):** `LivePortfolio.save_state()` becomes a no-op for positions (still writes equity_history). `reconcile_positions.py` no longer writes JSON. Dashboard reads from SQLite. State JSON regenerated by a single nightly job from SQLite + broker (purely derived).
- B.4 (3 days) — **Plans → append-only (P7):** plans table UPSERTs by (date, market_id); plan files are write-once audit logs, no `_save_plan` overwrites.
- B.5 (3 days) — **Atomic order submission (P4):** every entry includes OCO bracket; sync_protective uses `replace_order` instead of cancel+place.

**Exit criteria:**
- 4 reconcile scripts collapsed to 1 module
- Net code reduction ≥ 1,500 LOC
- No "JSON state ≠ SQLite trades" discrepancy in 7 consecutive days
- All broker submissions are atomic (no cancel-replace observed in logs)

**Rollback:** B.2 has parallel-run safety net. B.3 can be reverted by re-enabling JSON writes (one config flag). B.5 has known-good fallback to cancel-replace.

**Effort:** ~19 working days. Best done by 1 engineer end-to-end (each item depends on the previous).

---

### Phase C — Architectural simplification (4-8 weeks)

**Goal:** structural changes that eliminate entire bug classes from the design.

**Work items:**
- C.1 (10 days) — **Trade state machine (P5):** schema change + 10 call-site updates + transition audit table.
- C.2 (5 days) — **Single orchestrator timer (P8):** crontab pruned to ~8 entries; `atlas_cycle.sh` per market.
- C.3 (5 days) — **Per-market broker accounts (P6):** assumes operational lead-time for Alpaca sub-account provisioning. Code change is small.
- C.4 (5 days) — **Standardized broker-call wrapping (P10):** every alpaca-py call goes through `_broker_call`. Add metrics + retry classification.
- C.5 (10 days) — **God-file decomposition:** split `live_executor.py` (2790 LOC) into `executor/` package: `entry.py`, `exit.py`, `protective.py`, `circuit_breaker.py`, `preflight.py`. Same for `chat_server.py` (3385 LOC) → `services/dashboard/`.

**Exit criteria:**
- All trade rows go through state machine. CHECK constraint enforced.
- Crontab ≤ 10 atlas-related entries.
- Per-market broker accounts. Cross-market reconcile drift impossible by construction.
- No file in the codebase exceeds 800 LOC.

**Rollback:** C.1 has shadow-mode safety net. C.2 has 3-day parallel run. C.3 is reversible by reverting the connect-string mapping. C.5 is structural; rollback = revert the merge.

**Effort:** ~35 working days. Probably needs 2 engineers in parallel. Highest design cost.

---

## 7. Quick Wins (≤ 1 day each, do immediately)

| # | Item | Effort | Bug class eliminated |
|---|---|---|---|
| QW1 | **Add TP to CAT now (manual one-shot).** Currently TP-naked since 2026-04-24. Verify against today's `bd3c3077` fix and ensure no other open trade is TP-naked. | 30 min | Active capital risk |
| QW2 | **Throttle `save_state() skipped` warning** to 1/process. Currently 30+/15min storm. | 1 hour | Log noise eclipsing real signals |
| QW3 | **Schema-validate every config/active/*.json on cron boot,** fail loud if invalid. The `alpaca.paper` mismatch (atlas-lessons.md) wouldn't have happened. | 2 hours | Configuration drift class |
| QW4 | **Add `broker_orders` daily cron entry** (commit message lists the cron line but it isn't in crontab yet). | 5 min | Catches data gap before P2 work |
| QW5 | **Dedupe crontab:** the file has 49 entries, several are redundant pre/post comments. Sort + audit. | 1 hour | Operational confusion |
| QW6 | **Delete 6 dead files:** `live_sp500.db` (0 bytes), `live_sector_etfs.json.pre-xlk-recovery-...`, `tests/archive/test_reconcile.py`, `AGENTS.md.bak`, `tasks/atlas-lessons.md` (merge into lessons.md), `dashboard/generate_data.py` if nothing imports. | 1 hour | Cruft, future confusion |
| QW7 | **Add `INSERT OR IGNORE` to `equity_history`** writes (already done for some, audit for completeness). | 30 min | Equity-curve dup rows |
| QW8 | **Idempotency assertion test** for `reconcile_positions.py --fix`: run twice, second run should be a no-op. Currently it isn't (hotspot #2). | 1 hour | Self-correction cycle |
| QW9 | **Disable 3 worst-performing strategies** in live configs (P9). | 30 min (config edits) | Reduces strategy-specific bug surface |
| QW10 | **Healthz: `tp_naked_positions = SELECT COUNT(*) FROM trades WHERE status='open' AND (tp_order_id='' OR take_profit IS NULL) AND DATE(entry_date) < DATE('now','-1 day')`.** Wire to Telegram alert if > 0. | 1 hour | Hotspot #7 catches future occurrences immediately |

**All 10 quick wins can be batched into 2 commits** (one for code, one for ops/config) and shipped today.

---

## 8. Things to STOP doing

| # | Stop doing | Why | Replace with |
|---|---|---|---|
| 1 | **Adding new reconcile scripts.** | We have 12 reconcile paths. Every new "fix" tends to add a 13th. | Modify the canonical `core/reconcile.py` (Phase B) |
| 2 | **Reading `stop_price` from plan files in any post-fill path.** | Plans are decision logs, not state. Their lifecycle ends at fill. | A `position_protective_orders` ledger row INSERT-ed at fill (Phase A) |
| 3 | **Dual-writing JSON state files from `LivePortfolio.save_state` and `reconcile_*.py`.** | Two writers = guaranteed drift. | SQLite is canonical; JSON is read-only-derived (Phase B) |
| 4 | **Inferring fill prices from `Position.avg_entry_price` or `cost_basis / shares`.** | Alpaca averages can be stale or wrong (CHTR class). | `broker_orders` JOIN by order_id (Phase B) |
| 5 | **Bare `except Exception: pass` blocks anywhere in execution paths.** Wave 1+2 fixed top-17. There are still ~10 in eod_settlement, execute_approved, reconcile_positions, sync_protective. | Exceptions in execution paths are LOUD, not SILENT. | Logged exception with context (already the project policy; just enforce) |
| 6 | **Adding new `--auto-fix` modes to scripts.** | Today auto-fix means "rewrite JSON state from broker." That's the cycle in hotspot #2. | Detect-and-alert default; manual `--apply` for one-shot fixes |
| 7 | **`INSERT OR REPLACE` in any state-machine table.** | Silently overwrites valid rows. Lessons learned 2026-04-20: even bumps `rowid` and tricks COUNT(*)-only checks. | `INSERT ... ON CONFLICT(...) DO UPDATE SET ...` (UPSERT proper) |
| 8 | **Configuring strategies with `live_enabled=true` for universes with 0 P&L over 60d** (currently asx, possibly crypto). | They consume research budget and operational complexity. | Move to research-only until they prove material |
| 9 | **Running 9 separate research timers** (one per universe). | If one fails, you don't know unless you check 9 logs. | Single research orchestrator, per-universe sub-jobs, single failure pipeline |
| 10 | **Inline subprocess calls for telegram alerts at the bottom of every `if-error:` block.** | ~15 files do this; some bare-except'd. | One `telegram.alert(level, ctx)` helper; called at top of error class. |
| 11 | **Adding new SQLite columns ad-hoc without migrations.** Today there are 21 migrations in 7 days. | Velocity is right, but each is a separate file with its own conventions. | One `migrations/` framework with up/down + version table (already exists; just consolidate) |

---

## 9. Cross-cutting Improvements

### 9.1 Test coverage gaps

| Gap | Risk | Recommendation |
|---|---|---|
| No end-to-end test for "fill arrives, position appears in trades + broker + JSON, all three agree." | Highest | Write `test_e2e_fill_to_state_parity.py` — submits a paper trade, asserts all 3 stores converge in <30s |
| No test for `reconcile_positions.py --fix` idempotency | Hotspot #2 | `test_reconcile_idempotent.py` — run twice, second run = 0 changes |
| No test for "TP-naked position triggers alert" | Hotspot #7 | `test_tp_coverage_healthcheck.py` |
| No test for "sync_protective works against a broker that returns 40310" | Hotspot #3 | Mock alpaca returning 40310, assert cancel-confirm-place succeeds within deadline |
| No test for cross-market client_order_id parsing (which universe does this fill belong to?) | Hotspot #5 | Already covered by `test_reconcile_universe_filter.py`; extend to OCO leg parsing |
| Tests don't run against a fresh DB schema migrated from scratch | Schema regression | CI step: `rm test.db && python -m scripts.migrate && pytest` |

### 9.2 Observability gaps

| What we can't see now | Recommendation |
|---|---|
| **"Where did this position come from?"** (which signal, which plan, which strategy, which broker order_id) for any open trade | Single `dashboard/position_provenance` view JOINs trades + plans + signals + broker_orders by order_id |
| **"What changed since last reconcile?"** | Reconcile output → new `reconcile_runs(ts, market, n_drift, n_fixed, drift_detail_json)` table; dashboard timeline view |
| **"Was sync_protective effective for this position-day?"** | Per-position-day boolean: `had_stop_at_eod`, `had_tp_at_eod`. Alert if false. |
| **"Is this strategy actually producing P&L?"** | Already in dashboard; needs a 7/30/60-day rolling chart so retirement decisions are obvious |
| **"What's the broker-account utilization?"** | sum(market_value) / equity per market; today only as raw equity attribution |
| **"Why did this fix-commit happen?"** | A `RCA #N → fix commit hash` mapping table. Already partially in commit messages; formalize in `docs/rca-index.md` |

### 9.3 Documentation gaps

| Re-discovered N+ times | Document it once |
|---|---|
| The `live_*.json` filename prefix rule (must be `live_`, never bare `{market}.json`) | `brokers/state/README.md` |
| The `alpaca.paper` ↔ secrets.json invariant | Already in `tasks/atlas-lessons.md`; extract to `config/README.md` |
| Cron timezone is AEST (`TZ=Australia/Brisbane`); UTC ↔ AEST conversion table | `scripts/README.md` |
| The 7-step daily lifecycle (premarket → executor → intraday → eod → reconcile → research → review) | `docs/daily-lifecycle.md` (none today) |
| The trinity-of-state model that this report describes | `docs/state-model.md` |
| When to run reconcile_ledger vs reconcile_positions vs reconcile_sqlite_to_broker | Will be irrelevant after Phase B |

### 9.4 Dev workflow improvements

| Improvement | Current pain | Effort |
|---|---|---|
| **CI: schema migration smoke test on every PR** (rm test.db; migrate; pytest) | Schema regressions slip through | 2 hours |
| **CI: `verify_dual_write.py --source=ci` on every PR** | Dual-write leaks shipped 4+ times in 60 days | 1 hour |
| **CI: `find . -name "*.py" -exec grep -l 'INSERT OR REPLACE' {} \;` and fail on hits in state-machine tables** | Lessons learned anti-pattern | 30 min |
| **`pre-commit` hook: `python3 -m py_compile` on staged .py files** (commit `134c1d63` shipped this) | Already done — extend with mypy --strict on the executor files | done + extend |
| **Domain-violation linter:** detect calls to `_save_state()` from read-only paths | Hotspot #4 cause | 4 hours |
| **Test fixture: "spawn isolated atlas DB" helper** | Every test file has its own variation; lessons learned showed module-scope vs session-scope vs function-scope are all subtly different | 4 hours; standardize |

---

## 10. Recommended Next 3 Actions

If I could only ship 3 things in the next week:

### Action 1 (TODAY, ≤1 hour) — Ship Quick Wins QW1, QW4, QW10
- Add TP to CAT (the one open TP-naked position right now)
- Add the `sync_broker_orders` cron entry (the new feature shipped today but not yet wired)
- Add the TP-coverage healthcheck to sync_protective

These three are atomic, reversible, and immediately reduce the active bug surface.

### Action 2 (THIS WEEK, 2 days) — Phase A.1 + A.2: position-protective ledger
- New table `position_protective_orders(trade_id, ticker, stop_order_id, stop_price, tp_order_id, tp_price, last_synced_at)`
- `_execute_entry` INSERTs after fill confirmation
- `sync_protective` UPDATEs on every cancel-replace
- `reconcile_entry_fills` reads from THIS, no longer from plan files

This is a single-table addition that kills the "stop_price=0 in plan" warning class (hotspot #1) and structurally prevents the next class of TP-naked positions (hotspot #7) from being undetectable.

### Action 3 (NEXT 2 WEEKS, 5 days) — Phase B.1 + B.2 ramp: parallel-run new reconcile module
- Implement `core/reconcile.py` skeleton with two functions matching the canonical interface
- Wire it to run alongside the existing reconcile scripts in shadow mode (compare outputs, alert on divergence)
- This is the long lead-time work for the architectural simplification; starting now means Phase B is unblocked when we get there

These three actions, in order, attack the 3 highest-frequency bug classes (stop_price=0 warning, TP-naked positions, reconcile drift) while seeding the larger refactor.

---

*End of Engineering-Lens audit. Companions: Planning lens (architecture & sequencing) and Validation lens (test gaps & invariants) deliver complementary views.*
