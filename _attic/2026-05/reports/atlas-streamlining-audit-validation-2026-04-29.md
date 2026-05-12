
# Atlas Streamlining Audit — Validation Lens

**Date**: 2026-04-29
**Author**: Validation Lead (with parallel sub-audits from Bug Pattern Miner, Invariant & Reconcile Auditor, Test Coverage Gap Analyst, and Architecture & Cruft Auditor)
**Scope**: Past 60 days of Atlas operations (~913 commits, 73 `fix(...)` commits, 632 ERROR log entries, 427 circuit breaker trips, 13 superseded trades, 49 test skips, 170K LOC across 18 packages)
**Status**: Read-only audit. No code changed. No state mutated.

---

## 1. Executive Summary

Atlas has shipped 73 `fix(...)` commits in the last 60 days — roughly one production bug every 20 hours. The bug rate is not random: it concentrates on **two architectural diseases** that no individual fix can cure.

**Disease A — Silent-Failure Infection.** Bare-except clauses, fallback values, and "log-and-continue" patterns mask defects until they compound. Concrete instances: research director silently blocked **37 days**, signal writes silently dropped **10 days**, sync_protective_orders ran sp500-only for **weeks** while ETF positions sat unprotected, pi-cron silently routed to paid API billing, BRAVE/AAII data sources silently degraded. Four separate bare-except cleanup passes (`31fdde25`, `57d964ff`, `ef7b0018`, `57e062e8`) are still finding new instances. **No CI lint enforces logged exceptions.** No alert fires on "days since X happened" for X ∈ {experiment generated, signal written, equity reconciled, regime changed}.

**Disease B — Multi-Writer Ledger.** The `trades` table has **5+ writers** (`record_trade_entry` from `journal/logger`, `reconcile_ledger`, `reconcile_positions`, `live_executor.reconcile_entry_fills`, plus a raw INSERT in `reconcile_sqlite_to_broker`) with divergent guards. The `stop_order_id` field is written from **3 independent paths** with no ordering guarantee. JSON state files, SQLite tables, broker truth, and in-memory caches all claim authority over different slices of the same entities. Every reconcile bug, ghost trade, phantom row, and TP-naked position traces to "two writers disagreed and no one was the canonical owner." The 4 reconcile/sync scripts (3,036 LOC combined) exist *because* of this disease — they're symptoms, not solutions.

**Recommendation.** Phase A (1–2 weeks): kill silent failures, finish the broker_orders source-of-truth migration, repair the live invariant gaps that just produced today's `MU` and `CAT` TP-naked positions. Phase B (2–4 weeks): collapse the 4 reconcilers into 1, designate SQLite as canonical state, remove ~12K LOC of cruft. Phase C (4–8 weeks): trade-state machine, unified config schema, per-market account isolation, observability for invariants. The next 3 actions, in order: (1) audit-and-bracket the 2 currently TP-naked positions, (2) ship a CI lint banning new bare-except, (3) instrument "days-since" alerts for all cron-driven invariants.

**Forecast.** If only Phase A ships, expect bug rate to drop ~30%. If A+B ship, expect ~60%. If A+B+C ship, expect ~80% reduction *and* materially shorter time-to-resolution per remaining bug.

---

## 2. Bug Inventory (Evidence-Based)

### 2.1 Headline metrics (60-day window)

| Metric | Value |
|---|---|
| Total commits | 913 |
| `fix(...)` commits | 73 (8.0%) |
| Distinct subsystems with fixes | 40 |
| Circuit breaker incidents | 6 dates, 427 total trips (max 216 in single day) |
| `system_log` ERROR entries (live_executor) | 632 |
| Open positions TP-naked at audit time | 2 (`CAT` open since Apr 24 = 5d, `MU` opened Apr 29) |
| Superseded / phantom trade rows | 13 |
| Bug classes recurring 3+ times despite fixes | 5 (architectural) |
| Bug classes with zero regression tests | 8 |
| Days research silently blocked (`P0.3`) | 37 |
| Days signal writes silently failed (`P1-9`) | 10 |
| Reconcile/sync/eod LOC across 6 scripts | 4,151 |
| Strategies with `enabled: false` everywhere | 3 (~1,180 LOC) |
| Removable cruft LOC | ~11,500–12,000 |

### 2.2 Bug classification by root cause bucket

#### A. State synchronization (DB ↔ broker ↔ JSON ↔ in-memory)

| Bug | Freq 60d | Severity | Last Seen | Evidence |
|---|---|---|---|---|
| sync_protective OCO 40310 race | 3+ | P0 | 2026-04-29 | `743c0488`, `c8c5f018`; CAT/MU empty tp_order_id today |
| stop/tp_order_id not persisted after cancel-replace (#274) | 2+ | P0 | 2026-04-29 | `c8c5f018`; MU id=192 has both fields empty |
| FCX double-claim across sp500 + commodity_etfs | 1 | P1 | 2026-04-28 | `00f0b634`; `markets/etf_markets.py:111` + `markets/sp500.py:131` overlap remains in code |
| XLK state file loss → unmanaged position | 1 | P0 | 2026-04-24 | `2d0cfcu3`; `live_sector_etfs.json.pre-xlk-recovery` backup file |
| sync_protective scope sp500-only (weeks) | 1 (long-running) | P0 | 2026-04-27 | `440e7412`; commodity_etfs/sector_etfs unprotected for weeks |
| daily_high_water no session reset | Ongoing | P1 | 2026-04-28 | `666d0283`; HWM carried across sessions |
| XLY cross-contamination in reconcile | 1 | P1 | 2026-04-22 | `61b5545f` |
| Internal vs broker equity divergence | Structural | P1 | 2026-04-27 | `calc_position_size` uses internal equity ≠ broker equity |

#### B. Race conditions

| Bug | Freq 60d | Severity | Last Seen | Evidence |
|---|---|---|---|---|
| Plan duplicate insert on status transition | Multiple | P1 | 2026-04-24 | `2b7ba7a3` |
| CCJ duplicate open position (UNIQUE index missing) | 1 | P1 | 2026-04-24 | `1cecf4dd` |
| Reconcile pre-insert duplicate | Multiple | P1 | 2026-04-24 | `ad94751b` |
| Overlay decision dedup failure (backlog) | 1 | P2 | 2026-04-28 | `5de73ec6` |

#### C. Reconciliation drift

| Bug | Freq 60d | Severity | Last Seen | Evidence |
|---|---|---|---|---|
| CHTR phantom ledger entry (RCA #1B) | 1 | P1 | 2026-04-29 | `461bf376`; required Alpaca FILL audit |
| Ghost ledger rows (Phase 1.1 purge) | Multiple | P1 | 2026-04-22 | `28230ceb` |
| SLV/XLY/UNG wrong universe (6 trades) | 6 | P1 | 2026-04-22 | `ca3fc598`+`3821080b` |
| Persistent reconcile discrepancies | Daily | P1 | 2026-04-29 | Apr 29 manual `fix=True` at 10:01 |
| UNG reconcile_phantom entry | 1 | P2 | 2026-04-22 | id=154 |
| Phantom load_state() in reconcile path | 1 | P1 | 2026-04-20 | `7d98c74f` |
| Phantom exit on unfilled LIMIT | 1 | P1 | 2026-04-04 | `a35e400a` |

#### D. Data quality

| Bug | Freq 60d | Severity | Last Seen | Evidence |
|---|---|---|---|---|
| TP-naked positions 5+ days (RCA #1A) | 2+ | P0 | 2026-04-29 | `0ea95f8b`; CAT and MU active right now |
| Stop price missing → 5% fallback | Dozens/day | P1 | 2026-04-22 | Apr 22 logs: 4 positions on synthetic 5% fallback for full session |
| Inverted stop CHECK constraint absent | 1 (struct.) | P0 | 2026-04-27 | `84690726` |
| Alpaca IEX feed stale for mega-caps | Weeks | P1 | 2026-04-28 | `7ad48f37` + `a445662b` (re-apply!) |
| AAII sentiment 403 Forbidden | Persistent | P2 | 2026-04-27 | Logs every overlay run |
| Dividend API 90-day range violation | Every EPS run | P2 | 2026-04-26 | Logs |
| Good Friday 2026-04-03 missing in regime_history | 1 | P2 | 2026-04-28 | `5c837b94` |

#### E. Configuration drift

| Bug | Freq 60d | Severity | Last Seen | Evidence |
|---|---|---|---|---|
| pi-cron missing `--system-prompt` | Weeks | P1 | 2026-04-28 | `cc2b95b1` + 2 follow-ups; paid API billing silently |
| BRAVE_API_KEY missing | Every overlay run | P2 | 2026-04-27 | Logs |
| market enum incomplete | 1 (struct.) | P2 | 2026-04-28 | `392c5050` |
| ib-gateway-watchdog wrong OnCalendar | 1 | P2 | 2026-04-24 | `08108fe2` |
| Research director 37-day silent block | 1 (37d outage) | P1 | 2026-04-22 | `686545a8` |

#### F. Strategy / signal quality

| Bug | Freq 60d | Severity | Last Seen | Evidence |
|---|---|---|---|---|
| sp500 signal write 10-day silent failure (P1-9) | 10d silent | P1 | 2026-04-24 | `d0b939d0`; `constructed.rejected` not injected |
| mtf_momentum time_exit shadowed | 1 (struct.) | P2 | 2026-04-28 | `7f326c50` |
| Equity sizing uses internal not broker equity | Structural | P1 | 2026-04-27 | Leverage audit |
| Research Sharpe disambiguation (solo vs portfolio) | 1 | P1 | 2026-04-28 | `42f1dd27` |
| Universe isolation missing in non-sp500 sweeps | 1 | P1 | 2026-04-22 | `c08a2c94` |
| Profit factor Infinity JSON crash | 1 | P2 | 2026-04-22 | `03d2929d` |

#### G. Infrastructure

| Bug | Freq 60d | Severity | Last Seen | Evidence |
|---|---|---|---|---|
| Circuit breaker cascade | 6 incidents / 427 trips | P0 | 2026-04-20 | 3 equity baselines (5000/10000/4850) racing |
| Halt enforcement gap | 1 | P0 | 2026-04-28 | `d168e25b`; halt + circuit breaker not synchronized |
| eod_settlement broker connection failure | 2 | P1 | 2026-04-24 | sector_etfs Apr 24 22:00–22:03 |
| sector_etfs cron gap (weeks) | 1 long-running | P1 | 2026-04-27 | `1f593c3b` |
| Production DB test pollution | Multiple | P1 | 2026-04-23 | `3d9ac6d5`, `857019f5`, etc. |
| Bare-except swallowing exceptions | 4 cleanup passes | P1 | 2026-04-28 | `57d964ff` (15), `31fdde25` (17), `ef7b0018`, `57e062e8` |
| Pi CLI LLM loop timeout (1800s) | 4 | P2 | 2026-04-29 | Apr 26×2, Apr 27, Apr 29 |
| Dashboard SyntaxError (SQL `--` comment) | 1 | P2 | 2026-04-28 | `195968e4` |

### 2.3 Architecturally recurrent bug classes (3+ recurrences)

These are **not** implementation bugs — they are missing system-level invariants. Each will recur indefinitely until the architecture changes:

1. **TP-naked positions** (≥3 episodes: GLD/XLI/XLY pre-Apr-29; CAT Apr 24–today; MU today). No mandatory atomic-bracket gate at execution; no audit cron asserting TP coverage.
2. **Stop price not propagated plan → state** (recurring across tickers). Three stores (plan, JSON state, DB) with no canonical sync. Fallback masks failure.
3. **Bare-except swallowing bugs** (4 cleanup passes, still finding more). No CI lint. No "days-since-X-happened" alarm to surface silent dropouts.
4. **New universe added without infra coverage** (commodity_etfs sync gap, sector_etfs cron gap — both detected weeks late). Universe onboarding has no completion checklist.
5. **Dual-write phantom rows** (Phase 1.1 ghost purge, CHTR RCA #1B, UNG phantom, SLV/XLY/UNG wrong universe). Multiple writers to `trades` table with divergent guards; no canonical `TradeLedger.record_*` API enforced.

### 2.4 Bug classes with zero regression tests

- Circuit breaker cascade — no test for multi-equity-baseline conflict
- Stop price plan→state propagation — no test asserting JSON has stop_price after plan execution
- pi-cron `--system-prompt` presence — no CI check that all subprocess calls include the flag
- Universe onboarding completeness — no test that new universe has cron + sync_protective + reconcile coverage
- Equity / leverage aggregate cap — no test for multi-signal batch exceeding 2× cap
- eod_settlement retry on broker disconnect — 2 failures, no test
- AAII / BRAVE data fallback quality — overlay degrades silently
- LLM loop timeout recovery — 4 timeouts, no graceful-degradation test

---

## 3. Architecture Map + Blast Radius

### 3.1 Subsystem LOC inventory

| Subsystem | LOC | Role |
|---|---|---|
| `tests/` | 52,713 | Test suite (grows faster than production) |
| `scripts/` | 34,488 | Cron drivers, one-shot migrations, backfills, forensics |
| `research/` | 22,947 | Param optimization loop, sweep, promoter |
| `brokers/` | 10,223 | Alpaca adapter, executor, plan, portfolio |
| `data/` | 7,272 | OHLCV ingest, macro refresh, market-data utilities |
| `services/` | 6,836 | Dashboard server, Telegram, job server |
| `regime/` | 6,013 | Macro regime model |
| `overlay/` | 4,488 | LLM overlay, evaluator, alt-data |
| `strategies/` | 4,279 | 9 strategy classes (3 disabled everywhere) |
| `backtest/` | 4,276 | Vectorized backtester |
| `db/` | 3,346 | `atlas_db.py` god-object (2,609) + migrations |
| `risk/` | 1,602 | Position-level checks |
| `universe/` | 1,752 | Universe construction, point-in-time membership |
| `monitor/` | 1,724 | Lifecycle, alerts |
| `signals/` | 1,207 | Signal log/retrieve |
| `portfolio/` | 1,077 | Portfolio analytics |
| `indicators/` | 391 | TA helpers |
| `plans/` | 0 | (empty package) |
| **Total** | **~170K** | |

### 3.2 Dependency graph (text)

```
                   ┌──────────────────────────────────────┐
                   │   scripts/pi-cron.sh  (orchestrator) │
                   └──┬──────────────────────────┬─────────┘
                      │                          │
        ┌─────────────▼─────────┐    ┌───────────▼────────────┐
        │ services/chat_server  │    │ scripts/*.py (cron)    │
        │  (FastAPI dashboard)  │    │ vol_gate, reconcile,   │
        └─────────────┬─────────┘    │ sync_protective, eod   │
                      │              └───────────┬────────────┘
                      │                          │
        ┌─────────────▼──────────────────────────▼──────┐
        │           brokers/live_executor.py            │
        │      (imported by 18 modules — true hub)      │
        └──┬──────────────────────────────────────┬─────┘
           │                                      │
   ┌───────▼──────────┐                ┌──────────▼─────────┐
   │  brokers/plan.py │                │ brokers/           │
   │                  │                │ live_portfolio.py  │
   └────────┬─────────┘                └─────────┬──────────┘
            │                                    │
   ┌────────▼────────────────────────────────────▼──────────┐
   │                  db/atlas_db.py                         │
   │  (imported by 120 modules — universal data layer)       │
   └───┬─────────────────┬──────────────────┬───────────────┘
       │                 │                  │
  ┌────▼────┐    ┌───────▼─────┐    ┌──────▼────────┐
  │ regime/ │    │  overlay/   │    │   research/   │
  │ model.py│    │  engine.py  │    │   loop.py     │
  └─────────┘    └─────────────┘    └───────────────┘

Parallel (dual-write) paths:
  data/ingest.py ───────▶  db/atlas_db.py ─────▶  data/atlas.db (35 tables)
  brokers/state/*.json ◀──▶ trades_active SQLite (drift!)
  dashboard-data.json (legacy, stale Apr 2) ◀── archive/generate_data_legacy.py
```

### 3.3 Load-bearing 20% (where most pain originates)

| File | LOC | Why load-bearing | Coordination cost to change |
|---|---|---|---|
| `brokers/live_executor.py` | 2,790 | Execution hub, 18 importers, owns order lifecycle | **Highest** — touches every trade |
| `db/atlas_db.py` | 2,609 | Imported by **120 modules**, every read/write goes through it | **Highest** — schema changes ripple everywhere |
| `services/chat_server.py` | 3,385 | Dashboard API + WS chat + Telegram in one file (3 concerns) | High — split before changing |
| `scripts/sync_protective_orders.py` | 1,453 | OCO bracket reconcile every 15 min during market hours | High — frequent execution path |
| `brokers/live_portfolio.py` | 1,095 | 3-source equity calc, PDT guard, position tracking | High |
| `brokers/plan.py` | 958 | Research output → executable plan | Medium |
| `regime/model.py` | 550 | Gates execution for sp500 | Medium |
| `scripts/volatility_gate.py` | 534 | Premarket go/no-go for every trading day | Medium |

### 3.4 Cruft (deletion candidates, blast radius low)

| Category | Files | LOC | Notes |
|---|---|---|---|
| Dead strategies (`enabled: false` everywhere) | 4 (`bb_squeeze`, `mtf_momentum`, `trend_following`, `entry_optimizer`) | ~1,400 | Plus their tests |
| Zero-import scripts (one-shots, never re-run) | 14 (backfill_*, fix_ledger_sync, forensic_chtr_fills, etc.) | ~1,750 | All single-use |
| Archived files | `scripts/archive/generate_data_legacy.py` | 3,704 | Superseded by `chat_server._build_dashboard_data` |
| One-shot migrations | `scripts/migrations/*.py` | ~600 | 24 files, never re-run |
| Empty SQLite file | `brokers/state/live_sp500.db` | 0 rows | Abandoned migration |
| Stale data files | `ceasefire_factors.json`, `gate208_result_*.json`, `config/active_config_backup_*.json`, `AGENTS.md.bak` | — | |
| Duplicate research orchestrators | `autoresearch_nightly.py`, `autoresearch_runner.py`, `loop.py` | ~1,500 (overlap) | Only `loop.py` runs via cron |
| Universally-False feature flags | 13 flags (trailing_stop, intraday, macro_regime, confidence_scaling, etc.) | ~500 (dead branches) | |
| Defunct dashboard endpoints | `/api/portfolio`, `/api/trades` (TODO: unused) | ~200 | Marked unused in chat_server.py |
| `overlay_shadow_log` table (collapse with `overlay_decisions`) | ~100 | | |
| **Total removable** | | **~11,500–12,000 LOC** | ~7% of codebase |

### 3.5 Dual-implementations (the same thing, two ways)

| Concept | Implementation 1 | Implementation 2 | Implementation 3 |
|---|---|---|---|
| Portfolio equity | `live_portfolio.equity()` from JSON | `broker_equity()` from Alpaca | `market_equity_history` SQLite |
| Open position state | `brokers/state/live_*.json` | `trades` table WHERE status=open | `position_snapshots` SQLite |
| Overlay decisions | `overlay_decisions` table | `overlay_shadow_log` table | In-memory evaluator |
| Reconcilers | `reconcile_positions.py` | `reconcile_ledger.py` | `reconcile_sqlite_to_broker.py` |
| Research queue | `research/queue.json` | `research_sessions` table | `research_experiments` table |
| Dashboard data | `/api/dashboard-data` (live SQLite) | `dashboard-data.json` (stale Apr 2) | `/api/portfolio`+`/api/trades` (zombie) |
| Equity tables | `equity_curve` | `equity_history` | `market_equity_history` |
| Trade exit recording | `portfolio.execute_exit()` (which calls record_trade_exit) | direct `atlas_db.record_trade_exit()` | (same trade gets both — fires WARN) |

### 3.6 State writers per canonical entity

**`trades` table writers** (5+ paths, divergent guards):
| Writer | Location | Guards |
|---|---|---|
| `record_trade_entry` (live path) | `journal/logger.py:219` ← `live_executor.execute_entry` | UNIQUE partial index; returns None on dup |
| `record_trade_entry` (backfill) | `reconcile_ledger.py:343` | Pre-insert dup check + inverted-stop guard + no-zero-stop guard |
| `record_trade_entry` (fix mode) | `reconcile_positions.py:449` | Same as reconcile_ledger |
| Raw INSERT | `reconcile_sqlite_to_broker.py:~225` | **Only `_is_open_in_sqlite()` — missing inverted-stop guard, missing no-zero-stop guard** |
| `record_trade_entry` (deferred fill) | `live_executor.reconcile_entry_fills` | SQLite dedup only |

**`stop_order_id` writers** (3 independent paths, no ordering guarantee):
- `live_executor.py:1263` (at order fill)
- `sync_protective_orders.py:_apply_db_consistency:694` (every 15 min)
- `live_portfolio.py:496` (`_enrich_from_broker_stops` — every `connect()`)

**`live_{market}.json` writers**:
- `live_portfolio.save_state()` (EOD + every exit)
- `live_portfolio._update_state_positions()` (every connect, enrichment)
- `reconcile_positions.save_internal_state()` (cron `--fix`)
- `atlas_db._assert_state_file_parity()` (post every INSERT, "emergency self-heal")
- `eod_settlement` indirect via `portfolio.save_state()`

### 3.7 Inferred-price call sites (where Atlas guesses prices the broker actually knows)

| Site | What's inferred | Risk |
|---|---|---|
| `reconcile_ledger.py:232-234` | Entry price = `bp.entry_price` (Alpaca VWAP of partial fills) | Wrong if partials at different prices (CHTR) |
| `reconcile_ledger.py:397-400` | Exit price = entry_price | P&L = 0 |
| `reconcile_positions.py:316` | Stop = `entry * 0.95` | Synthetic, propagates to DB |
| `brokers/alpaca/broker.py:925` | Stop = `entry * 0.95` placed as **live broker order** | 🔴 Real execution at wrong price |
| `eod_settlement.py:116` | Stop-hit price = `pos.stop_price` | Overstates PnL on gap-down |
| `eod_settlement.py:218` | TP price = `pos.take_profit` | Understates PnL on gap-up |
| `live_portfolio.py:648` | No-fill exit = `entry_price` ("breakeven") | Real loss → recorded breakeven |
| `live_executor.py:1109-1115` (RCA #2A) | Synthesized TP = `order_price + 2*(order_price - stop)` | Uses LIMIT order price not actual fill |

### 3.8 Cross-market attribution (where mis-attribution can happen)

| Mechanism | How it works | Gap |
|---|---|---|
| JSON state files per market | `brokers/state/live_{market}.json` per-market | `reconcile_sqlite_to_broker` unions ALL files with no exclusion |
| `trades.universe` column | UNIQUE partial index on `(ticker, universe)` for open | Doesn't prevent same ticker in two universes |
| `derive_universe()` mapping | maps tickers → markets | FCX in both `etf_markets.py:111` and `sp500.py:131` (still!) |
| Cross-market exclusion | `reconcile_positions` builds `other_market_tickers` | If one state file stale/missing, exclusion fails silently |
| Pro-rata equity attribution | `eod_settlement.py:~795` | `derive_universe()` returns None → silently dropped from sum |
| `equity_curve ALL` row | "last writer wins" per day | Concurrent EOD overwrites earlier market's snapshot |
| `market_equity_history` (RCA #4D) | Per-market virtual equity | Only updated when EOD runs attribution; per-market cron desyncs |

---

## 4. Pain Point Hotspots — Top 10 Ranked

Ranked by `frequency × severity × time-to-debug`:

### 1. Circuit breaker cascade (🔴🔴🔴 score: ~maximum)
**Evidence**: 427 trips across 6 dates; 216 in single day (Apr 20). **Root**: 3 equity baselines (5000/10000/4850) in use simultaneously, generating different loss% calculations at different sites. `start_equity` hardcoded at plan-generation time, not reset dynamically. Sessions from different dates compete. **Why patches keep failing**: each fix is applied at one site while the equity is read from a different source at the next site. **Real fix**: single `BrokerEquityProvider` class as the *only* source of `current_equity` and `start_equity`; remove all hardcoded values; unit-test the invariant.

### 2. TP-naked open positions (🔴🔴🔴)
**Evidence**: RCA #1A fixed GLD/XLI/XLY on Apr 29. **As of this audit, CAT (5 days) and MU (today) are TP-naked.** **Root**: synthesize-TP fix only runs at entry synthesis time; if executor skips (PDT, OCO rejection, bracket failure), no catch-up runs. No periodic "TP coverage audit" cron. **Real fix**: (a) atomic OCO bracket as the *only* allowed entry path — entry is rejected if bracket fails; (b) hourly cron that asserts every open position has both `stop_order_id` and `tp_order_id` non-empty AND verified at broker; alerts on violation; (c) DB CHECK constraint preventing `status='open' AND (stop_order_id='' OR tp_order_id='')`.

### 3. Reconcile persistent discrepancies (🔴🔴)
**Evidence**: Daily `discrepancies=True` in logs; Apr 29 needed manual `fix=True` at 10:01. **Root**: JSON state file is ground truth for internal ops, but broker positions diverge whenever fills happen outside normal flow. No atomic write between broker fill → DB → JSON. **Real fix**: SQLite as single source of truth, JSON regenerated on read; eliminate JSON dual-write for state.

### 4. Bare-except silent-failure plague (🔴🔴)
**Evidence**: 4 cleanup passes, still finding more. Direct cause of 37-day research block AND 10-day signal-write failure. **Root**: no CI lint; no convention enforced. **Real fix**: AST-level lint banning new bare excepts (`except:` and `except Exception:` without `logger.exception`); make CI green-gate fail on violation; instrument "days-since" alerts for every cron-driven invariant.

### 5. Research director 37-day block (🔴🔴)
**Evidence**: `MIN_QUEUE_DEPTH=5` gate with 53 stale Mar-16 queue items. Zero new research generated for 37 days. **Why undetected**: exception silently swallowed. Damage: 37 days of compounding strategy drift. **Real fix**: alert on "no new experiments in N days"; same pattern for every cron-driven artifact (`signals_written_today_count`, `regimes_observed_today`, `equities_recorded_today`).

### 6. sp500 signal write 10-day silent failure (🔴🔴)
**Evidence**: Since Apr 14, `_run_regime_aware_plan()` silently dropped 40+ rejected signals. `constructed.rejected` was a list never injected into the dict. **Why undetected**: silent except + no "signals were missing today" alert. Same root as #4 and #5.

### 7. OCO 40310 race / stop_order_id loss (🔴🔴)
**Evidence**: 2 sequential fixes (`743c0488` + `c8c5f018`); MU id=192 today still has both `stop_order_id=''` AND `tp_order_id=''`. **Root**: `stop_order_id` written from 3 paths (executor at fill, sync_protective every 15 min, live_portfolio enrich) with no ordering. The fix landed in sync_protective's `_apply_db_consistency` but the entry path doesn't write IDs into the trade row at fill time reliably. **Real fix**: trade-state machine where `PROTECTED` state requires both IDs; entry transitions through `SUBMITTED → FILLED → PROTECTED`; only the executor writes IDs at fill; sync_protective only writes during cancel-replace transitions; explicit ownership.

### 8. Stop price 5% fallback / plan→state propagation (🔴🔴)
**Evidence**: Apr 22 logs show 4 positions on synthetic 5% fallback for entire trading day, every 15 min. **Root**: stop_price exists in plan, JSON state, and DB — three stores with no canonical sync. **Real fix**: trade row in SQLite is canonical; plan and state read FROM trades; eliminate independent stop_price fields.

### 9. Production DB test pollution (🔴)
**Evidence**: Multiple cleanup PRs across 2 weeks (#252 series). Tests created synthetic tickers, dummy OHLCV. Some persisted into prod DB, corrupted health checks. **Root**: zero isolation discipline before April. **Real fix**: autouse `_isolate_prod_db` fixture (already added), plus CI check that no test imports `data/atlas.db` path directly.

### 10. Equity / leverage divergence (🔴)
**Evidence**: `calc_position_size` uses Atlas-internal equity (≠ broker equity). Multi-signal plan batches check each signal independently. Apr 27 leverage was 1.747×. **Root**: no aggregate MV check at plan generation; only execution-time gate (just landed). **Real fix**: plan-generation aggregate check; pre-submit leverage gate (already shipped) is belt; the suspenders are missing.

**Honorable mentions** (lower frequency but high blast radius):
- pi-cron `--system-prompt` missing → paid API billing silently for weeks
- Halt enforcement gap (halt + circuit breaker not synchronized)
- Alpaca IEX feed stale for mega-caps (had to be re-applied — fix regressed)
- New universe added without infra coverage (sync_protective scope, sector_etfs cron)
- Phantom load_state() / phantom exit on unfilled LIMIT
- FCX in both universe definitions in code

---

## 5. Simplification Proposals (RANKED)

### Proposal 1 — SQLite as Single Source of Truth (canonical state)
**Statement**: Designate SQLite (`data/atlas.db`) as the **only** authoritative store for trades, positions, equity, signals, and decisions. JSON state files (`brokers/state/live_*.json`) become **derived caches** regenerated on read. Eliminate dual-write.

**Bug classes eliminated**:
- Reconcile persistent discrepancies (#3 above)
- FCX double-claim
- XLK state file loss
- Equity divergence (broker vs internal vs market_equity)
- Phantom load_state()
- `_assert_state_file_parity` "emergency self-heal" entirely removable

**Blast radius**: HIGH. Touches `live_portfolio.py`, `live_executor.py`, all 4 reconcile scripts, eod_settlement, monitor/, dashboard reads. ~30 files.

**Effort**: 2–3 weeks engineering, 1 week test stabilization. Requires careful migration with parity verification (read both, compare, alert on divergence; flip after 7 days clean).

**Order**: Must come AFTER trade-state-machine (Proposal 4) and BEFORE reconciler unification (Proposal 2). The trade-state machine defines what state means; canonical store implements it; reconcilers collapse onto it.

**Risk**: Bugs during migration could cause real money loss. Mitigation: dual-read with divergence alarm for 1 week before flipping authority. Keep JSON write path until parity proven.

**ROI**: Eliminates an entire bug class (dual-write drift), removes ~700 LOC of self-heal scaffolding, makes reconcile trivially correct.

---

### Proposal 2 — Collapse 4 Reconcilers Into 1 Canonical Path
**Statement**: Replace `reconcile_ledger.py` (484), `reconcile_positions.py` (727), `reconcile_sqlite_to_broker.py` (282), and the reconcile blocks in `eod_settlement.py` and `live_executor.reconcile_entry_fills` with one `Reconciler` class with three idempotent operations: `reconcile_fills()`, `reconcile_positions()`, `reconcile_protective()`. Each reads from broker_orders + Alpaca live, writes via `TradeLedger.record_*` only.

**Bug classes eliminated**:
- Divergent guards across reconcilers (`reconcile_sqlite_to_broker` missing inverted-stop, no-zero-stop)
- Multi-writer trades table → 5 paths → 1
- `reconcile_sqlite_to_broker.REOPEN` wiping P&L without recalc
- Pre-insert dup mismatch (`market_id` vs `derive_universe()` discrepancy)
- Phantom rows from divergent INSERT logic

**Blast radius**: MEDIUM. Touches all reconcilers + their cron jobs. ~10 files. Tests need consolidation.

**Effort**: 1–2 weeks. Architecturally simpler than it looks because each reconciler does similar things — the diversion is in implementation detail, not design.

**Order**: After trade-state-machine. Before per-market account isolation.

**Risk**: Missing edge case from one of the legacy paths. Mitigation: shadow-mode the new reconciler for 7 days, compare outputs row-by-row, alarm on divergence.

**ROI**: Removes ~1,500 LOC, eliminates the "which reconciler ran last?" debugging mystery, makes recovery from incidents 10× faster.

---

### Proposal 3 — Trade State Machine (TYPED, ENFORCED)
**Statement**: Introduce explicit trade states: `PROPOSED → APPROVED → SUBMITTED → FILLED → PROTECTED → CLOSED → SETTLED`. Each transition is a method on `TradeLedger`; the only way to change state. DB CHECK constraints enforce invariants per state (e.g., `status='PROTECTED' → stop_order_id != '' AND tp_order_id != ''`).

**Bug classes eliminated**:
- TP-naked positions (PROTECTED state requires both IDs)
- OCO 40310 race / stop_order_id loss (only one writer per state transition)
- Phantom exit on unfilled LIMIT (`SUBMITTED → FILLED` transition gated by broker confirmation)
- Plan duplicate INSERT
- Deferred fill ambiguity (`SUBMITTED` → `FILLED` is explicit)
- "Reopen" path that wipes P&L (would be illegal transition)

**Blast radius**: HIGH. Touches every place that updates a trade row. New schema migration. ~25 files.

**Effort**: 2–3 weeks. Requires schema migration, consumer rewrites, careful test coverage of each transition.

**Order**: First architectural change — everything else builds on top.

**Risk**: Bugs during migration. Mitigation: extensive transition-table tests; deploy in shadow mode (writes new state field but old code ignores it); flip enforcement after 7 days clean.

**ROI**: Eliminates the entire class of "state inconsistency" bugs. Makes invariants enforceable at DB level. Required prerequisite for Proposals 1 and 2.

---

### Proposal 4 — Atomic Bracket Entry (Mandatory)
**Statement**: Entry orders are **atomic OCO brackets**. If the bracket fails (any leg), the entry is rejected and the parent order is cancelled. No fallback to "synthesize TP later." Place the entry through Alpaca's native bracket order API (already exists). Reject any code path that places `entry → wait → stop → wait → tp`.

**Bug classes eliminated**:
- TP-naked positions (cannot exist by construction)
- Synthesize-TP-on-entry edge cases (RCA #2A)
- `entry_price * 0.95` synthetic stop placed as live order
- `eod_settlement.py:579` dead `if False:` protective sync block (becomes irrelevant)

**Blast radius**: MEDIUM. Touches `live_executor._execute_entry`, `brokers/alpaca/broker.place_order`, sync_protective (most of its work goes away). ~5 files.

**Effort**: 1 week (Alpaca's native bracket already supports OCO; mostly reorganizing existing logic).

**Order**: Can ship in Phase A independently of state machine. Pairs with state machine but doesn't require it.

**Risk**: If Alpaca rejects the bracket, signals are dropped. Mitigation: log reason, queue for next cycle if recoverable (retryable error vs hard reject).

**ROI**: Eliminates the TP-naked bug class outright. Reduces sync_protective_orders.py from 1,453 LOC to ~400 LOC (just stop-tightening / trailing logic).

---

### Proposal 5 — Eliminate Inferred Prices (broker_orders extension)
**Statement**: Extend the just-shipped `broker_orders` table (RCA #4A) to be the source of truth for ALL prices: entry price, exit price, stop fill price, TP fill price. Remove every site that uses `entry * 0.95`, `pos.stop_price` as if it were a fill price, or `entry_price` as a phantom exit. Reads from `broker_orders.filled_avg_price` first; only fall back to inferred WITH explicit alarm and audit trail.

**Bug classes eliminated**:
- CHTR-style phantom prices
- 5% synthetic stop placed at broker (`broker.py:925`)
- EOD stop/TP price overstatement on gaps
- "Conservative breakeven" exit that converts real loss to zero

**Blast radius**: MEDIUM. Touches all reconcilers + eod_settlement. ~8 files.

**Effort**: 1 week. Fix #275 and RCA #4A already laid the groundwork; this is finishing the migration.

**Order**: After state machine; can ship in parallel with reconciler unification.

**Risk**: If broker_orders is empty (early environment), price resolution fails. Mitigation: explicit fallback chain with alarm at each step.

**ROI**: Trustworthy P&L records. Removes "but the broker said it was X" debugging cycles.

---

### Proposal 6 — Per-Market Account Isolation (or virtual ledger with rigid attribution)
**Statement**: Either (a) one Alpaca account per market (sp500, sector_etfs, commodity_etfs each independent), or (b) keep single account but enforce strict virtual ledger: every position attributed to exactly ONE market via `markets/{market}.py` membership; conflicts (FCX) surfaced as hard failures at config load.

**Bug classes eliminated**:
- FCX double-claim (architectural elimination via market disjointness check)
- Cross-market attribution drift in `equity_curve ALL` row
- Pro-rata equity silently dropping unmapped tickers
- New universe added without infra (universe creation gated on completeness)

**Blast radius**: VERY HIGH if (a) — broker connection rewiring; LOWER if (b) — config/lint changes.

**Effort**: (b) 1 week (config validation + membership disjointness check). (a) 4–6 weeks.

**Order**: (b) can ship in Phase B; (a) is Phase C only if (b) proves insufficient.

**Risk**: Splitting accounts could affect margin/buying-power calculations. Migration (a) is operationally heavy. Mitigation: try (b) first.

**ROI**: Eliminates an entire class of cross-market bugs. (b) is cheap; do it first.

---

### Proposal 7 — Universe Onboarding Checklist (config schema)
**Statement**: Adding a new universe requires a single PR that includes: market config JSON, cron registration, sync_protective coverage, reconcile coverage, eod_settlement coverage, dashboard tab. Enforce via schema test that fails CI if any are missing.

**Bug classes eliminated**:
- New universe added without infra (sync_protective scope, sector_etfs cron, both detected weeks late)

**Blast radius**: LOW. New CI check + docs.

**Effort**: 2–3 days.

**Order**: Phase A quick-win.

**Risk**: None.

**ROI**: Permanently eliminates a recurrent cause of weeks-long silent gaps.

---

### Proposal 8 — Silent-Failure Lint + Days-Since Alerts
**Statement**: (a) AST-level lint banning new `except:` and `except Exception:` without `logger.exception(...)`. CI green-gate. (b) Heartbeat alerts on "days since X happened" for every cron-driven invariant (signals_written_today, experiment_generated, regime_observed, equity_recorded, sync_protective_completed, reconcile_completed).

**Bug classes eliminated**:
- Bare-except plague (architectural — no new bare-excepts can land)
- Research director 37-day silent block (would have alerted on day 1)
- 10-day signal write silent failure (would have alerted on day 1)
- pi-cron `--system-prompt` missing (could include "all pi calls had system_prompt today" check)
- BRAVE/AAII silent degradation
- Tiingo authority regression

**Blast radius**: LOW (lint), LOW–MEDIUM (alerts).

**Effort**: 3–5 days each.

**Order**: Phase A quick-win.

**Risk**: Existing bare-excepts trigger lint en-masse. Mitigation: grandfather existing; ban new only.

**ROI**: Permanently eliminates Disease A. Single highest-leverage process change.

---

### Proposal 9 — Decommission Cruft (~12K LOC removed)
**Statement**: Delete (in a single, well-documented PR per category):
- 3 dead strategies (`bb_squeeze`, `mtf_momentum`, `trend_following`) — 1,180 LOC + tests
- `scripts/archive/generate_data_legacy.py` and the static-JSON fallback path — 4,000 LOC
- 14 zero-import one-shot scripts — 1,750 LOC
- 24 `scripts/migrations/*.py` — 600 LOC
- Duplicate research orchestrators — 1,500 LOC
- Universally-False feature flags + dead branches — 500 LOC
- Defunct `/api/portfolio` + `/api/trades` endpoints — 200 LOC
- `overlay_shadow_log` table (collapse with `overlay_decisions`) — 100 LOC
- Empty `brokers/state/live_sp500.db` and stale data files

**Bug classes eliminated**:
- "Which version of generate_data am I looking at?" debugging
- Confusion about whether disabled features can be re-enabled
- Stale dashboard data served on misconfig

**Blast radius**: LOW per category.

**Effort**: 1–3 days per category, 2 weeks total.

**Order**: Phase A or B (parallel).

**Risk**: Deleting something used. Mitigation: each PR includes a grep showing zero importers.

**ROI**: 7% codebase shrink, faster onboarding, fewer footguns.

---

### Proposal 10 — Configuration Schema (single source, validated)
**Statement**: One `config_schema.json` (JSON Schema). All `config/*.json` validated at startup. Feature flags must declare both `enabled: bool` and `since_version`. Universally-False flags removed at next config bump.

**Bug classes eliminated**:
- Configuration drift (`market enum incomplete`, etc.)
- Stale flags accumulating
- "What does this flag actually do?" debugging

**Blast radius**: MEDIUM. Touches every config consumer.

**Effort**: 1–2 weeks.

**Order**: Phase B.

**Risk**: Strict validation could reject a legitimate config. Mitigation: validate-only-warn for 1 week.

**ROI**: Catches configuration bugs at startup, not at 09:30 ET when trading begins.

---

### Proposal 11 — Split `services/chat_server.py` (3,385 LOC, 3 concerns)
**Statement**: Break into `services/dashboard_api.py`, `services/chat_relay.py`, `services/static_serving.py`. Independent failure domains.

**Bug classes eliminated**:
- WebSocket chat crash → dashboard API down
- SQL `--` comment SyntaxError pattern (smaller files = better-reviewed)

**Blast radius**: LOW (internal refactor).

**Effort**: 2–3 days.

**Order**: Phase B.

**ROI**: Better fault isolation. Easier code review.

---

### Proposal 12 — Atomic-by-Default Order Submission
**Statement**: Establish at the broker adapter layer that ALL order submissions are either (a) atomic OCO brackets or (b) explicitly single-leg with documented justification. Any sequential `place(X) → wait → place(Y)` pattern requires written approval in PR.

**Bug classes eliminated**: Any future `entry → wait → stop → wait → tp` race.

**Blast radius**: LOW (enforcement, not refactor).

**Effort**: 1–2 days for review + lint.

**Order**: Phase A.

**ROI**: Prevents recurrence of the entire OCO-race bug class.

---

## 6. Phased Execution Plan

### Phase A — Stabilization (1–2 weeks)
**Goal**: Kill highest-frequency bugs without architectural changes. Stop new instances of recurring patterns.

| Item | Effort | Dep | Exit criterion |
|---|---|---|---|
| A1. Audit-and-bracket the 2 currently TP-naked positions (CAT, MU) | 1 hour | none | broker_orders shows OCO bracket for both |
| A2. Atomic Bracket Entry (Proposal 4) | 1 week | none | All entries go via native bracket; `bd3c3077` synthesize-TP path retained as belt-suspenders only |
| A3. Silent-Failure Lint (Proposal 8a) | 2 days | none | CI green-gate fails on new bare-except |
| A4. Days-Since Alerts (Proposal 8b) | 3 days | none | Alerts wired for: signals_written, experiment_generated, regime_observed, equity_recorded, sync_protective_completed, reconcile_completed |
| A5. Atomic-by-Default Order Submission (Proposal 12) | 1 day | A2 | Lint catches `place_order(stop)` not in bracket context |
| A6. Universe Onboarding Checklist (Proposal 7) | 2 days | none | CI fails if new universe missing infra |
| A7. Quick wins (Section 7 below) | 3 days | none | All shipped |
| **Phase A exit** | **2 weeks** | | TP-naked bug class eliminated. Silent-failure pattern blocked. Days-since alerts live. CAT/MU resolved. |

**Rollback plan**: Each item is independently revertable. A2 is the riskiest; keep `bd3c3077` synthesize-TP path active for 1 week as belt-and-suspenders.

### Phase B — Consolidation (2–4 weeks)
**Goal**: Eliminate redundant code paths. Unify state model. Remove cruft.

| Item | Effort | Dep | Exit criterion |
|---|---|---|---|
| B1. Trade State Machine (Proposal 3) | 2 weeks | A2 | All trade rows have explicit `state` column with CHECK constraints; transitions go through `TradeLedger` |
| B2. SQLite as SoT (Proposal 1, dual-read shadow) | 2 weeks | B1 | Dual-read enabled; divergence alarm clean for 7 days |
| B3. Eliminate Inferred Prices (Proposal 5) | 1 week | A6 done, broker_orders mature | All inferred-price call sites removed or alarmed |
| B4. Per-Market Attribution Strict (Proposal 6b) | 1 week | none | Membership disjointness check; FCX overlap removed |
| B5. Cruft Decommission (Proposal 9) | 2 weeks (parallel) | none | ~12K LOC removed |
| B6. Configuration Schema (Proposal 10) | 1.5 weeks | none | Schema validated at startup |
| **Phase B exit** | **4 weeks** | | One canonical state store. One canonical price source. ~12K LOC less code. Schema-validated configs. |

**Rollback plan**: B2 is the riskiest. Keep JSON dual-write throughout; only flip authority at end of Phase B with parity proof. B5 deletions are individually revertable from git.

### Phase C — Architectural Simplification (4–8 weeks)
**Goal**: Structural changes that prevent recurrence permanently.

| Item | Effort | Dep | Exit criterion |
|---|---|---|---|
| C1. Collapse 4 Reconcilers → 1 (Proposal 2) | 2 weeks | B1, B2 | One `Reconciler` class; ~1,500 LOC deleted; shadow-mode for 7 days |
| C2. Split chat_server.py (Proposal 11) | 3 days | none | 3 independent service files |
| C3. Per-Market Account Isolation (Proposal 6a, only if 6b insufficient) | 4–6 weeks | B4 mature | Optional: separate Alpaca accounts per market |
| C4. Observability for invariants | 1 week | A4 mature | Dashboard widget showing every invariant green/red |
| C5. CPCV / deflated Sharpe in research (per mental model) | 2 weeks | none | Research outputs include CPCV + DSR |
| **Phase C exit** | **6–8 weeks** | | One reconciler. Independent services. (Optionally) per-market broker. Invariant observability. |

**Rollback plan**: C1 ships in shadow mode first. C3 is opt-in (only if C4 reveals 6b is insufficient).

---

## 7. Quick Wins (each <1 day, ship immediately, parallel-safe)

1. **Audit & bracket CAT and MU now** (currently TP-naked). 1 hour.
2. **Delete `scripts/archive/generate_data_legacy.py`** (3,704 LOC; superseded weeks ago, not in any cron). 1 hour.
3. **Delete `bb_squeeze`, `mtf_momentum`, `trend_following`** strategy files (`enabled: false` everywhere). Half a day.
4. **Delete defunct `/api/portfolio` and `/api/trades` endpoints** in chat_server.py (already marked TODO: unused). 30 min.
5. **Add CI lint banning new bare-except** (grandfather existing). Half a day.
6. **Delete `markets/etf_markets.py:111` FCX entry** (or `markets/sp500.py:131`) and add a startup-time disjointness check. Half a day.
7. **Fix `reconcile_sqlite_to_broker.py`'s missing inverted-stop and no-zero-stop guards** to match the other 2 reconcilers (or deprecate the script). Half a day.
8. **Wrap `eod_settlement.py:579` `if False:` block**: either remove (it's dead) or fix it (it shouldn't be dead). Half a day.
9. **Schedule `sync_broker_orders.py`**: it currently feeds Priority-1 fill-price resolution but isn't visibly cron'd; verify schedule + add fallback alarm. Half a day.
10. **Add nightly assertion**: every `trades.status='open'` row has `stop_order_id != ''` AND `tp_order_id != ''`. Telegram on violation. 2 hours.
11. **Add CI check**: `grep -r "subprocess.*pi.*-p"` in repo, every match must include `--system-prompt`. 1 hour.
12. **Delete 14 zero-import one-shot scripts** (one PR per category, with grep evidence of zero importers). 1 day total.

---

## 8. Things to STOP Doing

1. **Stop placing real broker orders at synthetic prices** (`brokers/alpaca/broker.py:925`'s `entry * 0.95` stop). It's the worst inference site — a synthetic stop becomes a real exit at the wrong price.
2. **Stop adding new state writers without canonical owner declaration**. The `trades` table has 5 writers; the next universe will add the 6th. Lock writers to `TradeLedger.record_*`.
3. **Stop trusting `live_*.json` as ground truth**. SQLite is canonical. JSON is cache.
4. **Stop `entry → place(stop) → place(tp)` sequential ordering**. Atomic OCO bracket only.
5. **Stop bare-except**. CI lint enforces.
6. **Stop adding feature flags without tests for both states**. The 13 universally-False flags are dead branches that nobody tests.
7. **Stop adding cron entries without "days-since" alerts**. If cron silently stops, you have to know.
8. **Stop maintaining 3 research orchestrators**. Pick one (`loop.py`).
9. **Stop the dashboard's static-JSON fallback path**. Dashboard reads SQLite live; the legacy path is a foot-gun (it served Apr 2 data after the live read failed).
10. **Stop adding strategies without an `enabled_in: [markets]` field**. The bb_squeeze/mtf_momentum/trend_following confusion comes from each universe having its own copy of the same flag.
11. **Stop manual reconcile-with-fix=True as a daily ritual**. If you need it daily, the system is broken — fix the system, not the symptom.
12. **Stop running tests against the production DB**. The autouse `_isolate_prod_db` fixture exists; enforce it.

---

## 9. Cross-cutting Improvements

### 9.1 Test coverage gaps (priority order)

**Must exist before Phase A is declared done**:
1. `test_rca_phase2a_atomic_bracket.py::test_bracket_tp_placement_failure_rolls_back_stop` — atomic rollback.
2. `test_rca_phase1a_open_position_tp.py` fixture-seeded CI replacement for the 6 live-DB-skip tests. The current tests skip in CI when `trade_id 135/185/167 not in DB` — meaning **CI offers zero protection against TP-naked regressions**.
3. `test_fcx_double_claim_runtime.py` — runtime cross-universe guard test (current FCX test only checks JSON config).
4. `test_cron_idempotency_integration.py` — full cycle re-run, assert no duplicates.
5. `test_equity_reconciliation_threshold.py` — internal vs broker drift alert.
6. `test_regime_confirmation_blocks_executor.py` — integration test (current tests are unit-level).

**Must exist before Phase B is declared done**:
7. `test_state_machine_transitions.py` — every legal/illegal transition.
8. `test_sqlite_sot_parity.py` — JSON cache and SQLite agree.
9. `test_universe_onboarding_completeness.py` — every universe has cron + sync + reconcile + dashboard.
10. `test_no_synthetic_broker_orders.py` — assert no synthetic prices reach broker.

**Process**: Remove the live-DB-skip pattern entirely. Every skipped test is a hidden failure. 20 tests currently skip when trade_ids aren't in production DB — those are the most important post-incident invariants.

### 9.2 Observability gaps

- **No invariant dashboard.** No widget shows "all open positions have stop+TP", "no inferred prices in last 24h", "reconciler ran clean today".
- **No "days since X" panel.** This single addition would have caught the 37-day research block on day 1.
- **No equity reconciliation widget.** Internal vs broker equity divergence is not surfaced anywhere visible.
- **No bracket-fill latency tracking.** OCO brackets that fail silently have no observability.
- **No pi-cron success heartbeat.** A failed cron silently misses a day; only Telegram alerts (which themselves can fail) catch this.

### 9.3 Documentation gaps (re-discovered repeatedly)

- **The 4 reconcilers' purpose** — re-derived in every incident.
- **Which tables are canonical vs cached** — re-derived.
- **What `enabled: false` means** for each strategy — re-derived (turns out: dead in all configs).
- **The bracket order failure modes** — re-derived in RCA #2A.
- **Cross-market attribution rules** — re-derived in RCA #4D.
- **What `--system-prompt` does for pi CLI billing** — re-discovered AND captured in `/root/AGENTS.md`. Good. Replicate this pattern.

### 9.4 Dev workflow improvements

- **Pre-commit hook for AST bare-except check** (lint).
- **Pre-commit hook ensuring tests don't import `data/atlas.db`** directly.
- **CI gate: every PR touching `live_executor.py` requires reviewer approval.** It's the load-bearing hub.
- **CI gate: schema migrations require both forward and rollback scripts**, tested in CI on a copy of prod.
- **Mandatory PR checklist for "new universe" / "new strategy"**: matching cron, sync_protective, reconcile, tests, dashboard.
- **Weekly "is it still in shadow mode?" audit** — overlay_shadow vs enforce; dual-read state; etc. Drift accumulates silently.
- **Quarterly cruft sweep** — flag `enabled: false` strategies for deletion if no PR re-enabled them in 90 days.

### 9.5 Data quality gates

- **Tiingo authority assertion at startup**: code checks config and refuses to boot if Alpaca-IEX is authoritative for sp500. Prevents the `7ad48f37 → a445662b` regression cycle.
- **Macro freshness threshold alert** (already shipped via `check_macro_freshness.py`); add equivalent for OHLCV freshness, regime freshness, overlay-decision freshness.
- **Zero-byte parquet alarm** (already shipped via `0e42f652`); generalize to "row count regression" alarm.

---

## 10. Recommended Next 3 Actions

(After this audit is reviewed; intended for immediate execution)

### Action 1 — Audit & bracket CAT and MU positions NOW (1 hour, today)
The audit found that `CAT (id=187, opened 2026-04-24)` and `MU (id=192, opened today)` have empty `tp_order_id` — meaning they are running TP-naked, the exact bug class RCA #1A was supposed to fix. **This is real money currently at unmanaged upside risk.** Action: open Alpaca, place OCO brackets manually, then update `trades.tp_order_id` accordingly. Then run a one-time audit cron against the live DB asserting the invariant for all open positions.

### Action 2 — Ship the silent-failure lint + days-since alerts (Phase A3 + A4, 1 week)
This is the single highest-leverage change. It permanently prevents the bug class that produced the 37-day research block, the 10-day signal write failure, the BRAVE/AAII silent degradations, and (likely) several future incidents we can't predict. Specifically:
- AST lint banning new `except:` and `except Exception:` without `logger.exception(...)`. Grandfather existing instances.
- Heartbeat alerts on "days since X happened" for every cron-driven invariant. Initial set: `signals_written_today`, `experiment_generated`, `regime_observed`, `equity_recorded`, `sync_protective_completed_today`, `reconcile_completed_today`, `pi_cron_premarket_success`, `pi_cron_postclose_success`.

### Action 3 — Begin Trade State Machine + Atomic Bracket Entry (Phase A2 + B1, 2-3 weeks)
This is the architectural unlock for Phase B. Without it, "SQLite as SoT" and "1 reconciler" can't ship safely. With it, the entire bug class of "stop_order_id race" / "TP-naked" / "phantom exit" disappears by construction. Sequence: ship Atomic Bracket Entry first (Phase A2, 1 week, low blast radius); then State Machine (Phase B1, 2 weeks, schema migration with shadow-write).

**Do not start Phase B/C until Action 1 (TP-naked positions resolved) and Action 2 (silent-failure lint + alerts) are live.** The audit found that we are still actively producing the bug class that prompted the audit (MU TP-naked today) — Phase A items must land before architectural work begins, or we will keep paper-cutting ourselves while the surgery is in progress.

---

*End of report. Companion sub-audits available in worker output stream (Bug Pattern Miner, Invariant & Reconcile Auditor, Test Coverage Gap Analyst, Architecture & Cruft Auditor).*
