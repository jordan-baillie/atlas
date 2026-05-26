# Phase C — God-File Decomposition: Detailed Implementation Plan

**Task:** #226 — Engineering-ready decomp spec for the three god modules
**Intended location:** `/root/atlas/docs/phase-c-god-file-decomposition-detail.md`
*(Written to `/root/specs/` — Spec Writer domain constraint; Planning Lead should cp to atlas/docs/ to publish.)*
**DOC-ONLY:** No code changes, no config changes in this document.
**Companion specs:**
- `docs/specs/live-executor-decomposition.md` — PR1/PR2/PR3 plan (PR1 complete)
- `docs/specs/protective-orders-unification.md` — Candidate #6, `ensure_protected` API
- `docs/phase-c-god-file-decomposition.md` — historical planning doc (partially stale)

---

## Status Snapshot

| File | Actual LoC | Status |
|------|-----------|--------|
| `brokers/live_executor.py` | **3,192** | 🟡 PR1 ✅ DONE (commits `fd1633ee`, `a862313c`, `6fa43668`, `404778e7` — 2026-05-07, 605 LoC extracted). PR2 + PR3 outstanding (~1,652 LoC across 8 god methods). Spec source: `docs/specs/live-executor-decomposition.md`. |
| `brokers/alpaca/broker.py` | **2,211** | 🔴 PRE-DECOMP. Entirely undecomposed. Largest single method (`sync_all_protective_orders`) is **2× the briefed size** — 868 LoC, not ~400. Cross-ref `docs/specs/protective-orders-unification.md`. |
| `services/chat_server.py` | **220** | ✅ DECOMPOSITION COMPLETE. 17 routers in `services/api/`, 1 WS module in `services/ws/`. Landed 2026-04-29 through 2026-04-30 (commits `c50a98ee` → `c106a54e`). Wave 2 task #285 already executed; any remaining #285 scope should be re-scoped or closed. |

**Total remaining engineering effort:** ~22 days (live_executor PR2+PR3: 10 days; alpaca/broker.py: 12 days; chat_server: 0.5 days documentation only).

---

## A. `brokers/live_executor.py`

### A.1 Current State

**Actual LoC:** 3,192 (confirmed by `wc -l`).

#### Top-level methods with LoC

| Method | Lines | LoC | Flag |
|--------|-------|-----|------|
| Module-level helpers (`_fmt`, `_get_regime_model`, `_health_log`) | L1–L108 | 108 | — |
| `__init__` | L117–136 | 19 | — |
| Properties (4 props) | L137–163 | 27 | — |
| `_reset_circuit_breaker_if_new_day` | L164–169 | 5 | — |
| `_get_cached_account_info` | L170–190 | 20 | — |
| `_capture_start_equity` | L191–214 | 23 | — |
| `_check_circuit_breaker` | L215–310 | 95 | — |
| `connect` | L311–357 | 46 | — |
| `disconnect` | L358–368 | 10 | — |
| `place_order` | L369–410 | 41 | thin wrapper → broker |
| **`execute_plan`** | L411–835 | **424** | ⚠️ >150 LoC — orchestrator monolith |
| **`_execute_entry`** | L836–1344 | **508** | ⚠️ >150 LoC — largest method |
| **`_execute_exit`** | L1345–1693 | **348** | ⚠️ >150 LoC |
| `place_protective_stop` | L1694–1803 | 109 | — |
| `cancel_protective_stop` | L1804–1821 | 17 | — |
| `_cancel_open_orders_for_ticker` | L1822–1869 | 47 | — |
| `place_take_profit` | L1870–1941 | 71 | — |
| **`place_stops_for_plan`** | L1942–2121 | **179** | ⚠️ >150 LoC |
| `get_account_info` / `get_positions` / `get_open_orders` | L2122–2138 | 17 | thin delegations |
| `emergency_halt` / `clear_halt` | L2139–2171 | 32 | — |
| `check_market_state` | L2172–2218 | 46 | — |
| `get_fee_analysis` / `get_slippage_analysis` / `get_execution_history` | L2219–2238 | 19 | lazy-import delegations to `execution_analytics` |
| `cancel_unfilled_limits` | L2239–2334 | 95 | bulk cancel, stays in core |
| **`reconcile_entry_fills`** | L2335–2630 | **295** | ⚠️ >150 LoC |
| **`reconcile_exit_fills`** | L2631–2953 | **322** | ⚠️ >150 LoC |
| **`_record_same_bar_round_trip`** | L2954–3152 | **198** | ⚠️ >150 LoC — called only from `reconcile_exit_fills` |
| `_run_volatility_gate` | L3153–3177 | 24 | — |
| `_error_report` | L3178–3192 | 14 | — |

**Methods >150 LoC (7):** `execute_plan` (424), `_execute_entry` (508), `_execute_exit` (348), `place_stops_for_plan` (179), `reconcile_entry_fills` (295), `reconcile_exit_fills` (322), `_record_same_bar_round_trip` (198).

#### Already extracted (PR1 — 2026-05-07)

| Module | LoC | Commit | Concern extracted |
|--------|-----|--------|-------------------|
| `brokers/execution_journal.py` | 59 | `fd1633ee` (PR1.1) | `EXECUTION_LOG` constant + `journal_entry()` JSONL appender |
| `brokers/preflight.py` | 145 | `a862313c` (PR1.2) | `preflight_check_config`, `preflight_check_order`, `is_already_protected`, `protective_ledger_enabled` |
| `brokers/execution_analytics.py` | 214 | `6fa43668` (PR1.3) | `get_fee_analysis`, `get_slippage_analysis`, `get_execution_history` |
| `brokers/routing_policy.py` (extended) | 187 | `404778e7` (PR1.4) | `is_dry_run` added to `BrokerRoutingPolicy` |

**PR1.5 deferred** — dropping `self._mode` deferred to PR3 (4 `__new__` bypass scripts set `_mode` not `_policy`; lines 2801/2806 in `reconcile_entry_fills` reachable from bypass paths).
**PR1.6 no-op** — zero inline `paper_position_protective_orders` ternaries confirmed by grep.

**Remaining debt:** ~1,652 LoC across 8 flagged methods still inside the class.

#### Dependency graph

**Imports (atlas-relative):**
```
from brokers.base import (AccountInfo, OrderResult, OrderSide, OrderStatus, OrderType, PositionInfo)
from brokers.routing_policy import BrokerRoutingPolicy
from brokers.execution_journal import (EXECUTION_LOG, journal_entry)
from brokers.preflight import (PreflightError, preflight_check_config, preflight_check_order,
                               is_already_protected, protective_ledger_enabled)
```
`execution_analytics` imported lazily inside 3 getter methods (L2221–2231), not at module level.

**Reverse dependencies (8 production callers):**

| File | Usage |
|------|-------|
| `services/api/approvals.py` | `LiveExecutor` (lazy, inside function) |
| `services/telegram_bot.py` | `LiveExecutor` (lazy, inside function) |
| `scripts/execute_approved.py` | `LiveExecutor` |
| `scripts/eod_settlement.py` | `LiveExecutor` (×2 lazy, inside conditionals) |
| `scripts/sync_protective_orders.py` | `LiveExecutor` (via `__new__` bypass) |
| `scripts/cli.py` | `LiveExecutor` (×3 lazy) |
| `scripts/consolidation_close_positions.py` | `LiveExecutor` |
| `brokers/registry.py` | `LiveExecutor` |

**49 test files** import `LiveExecutor` or its sub-symbols.

---

### A.2 Decomposition Plan

**Endorsement:** The PR2/PR3 plan in `docs/specs/live-executor-decomposition.md` is current and actionable. Do not re-design; implement per that spec. This section cross-references it and adds one gap.

#### Proposed module boundaries (cross-reference)

| Target module | LoC estimate | Methods | PR |
|---------------|-------------|---------|-----|
| `brokers/protective_orders.py` | ~430 | `place_protective_stop`, `cancel_protective_stop`, `_cancel_open_orders_for_ticker`, `place_take_profit`, `place_stops_for_plan` | PR2 |
| `brokers/execution_reconciler.py` | ~820 | `reconcile_entry_fills`, `reconcile_exit_fills`, **`_record_same_bar_round_trip`** | PR3 |
| Slim `LiveExecutor` core | ~450–500 | Orchestration + circuit breaker + connect/disconnect | Post-PR3 |

See `docs/specs/live-executor-decomposition.md` §New modules for the full public-surface contract of each module.

#### Gap not covered by prior spec: `_record_same_bar_round_trip` (L2954–3152, 198 LoC)

**Recommendation: extract into `brokers/execution_reconciler.py`.**

Rationale: `_record_same_bar_round_trip` has exactly one caller — `reconcile_exit_fills`. It is a pure reconcile concern: detect a same-bar round-trip fill and write a synthetic journal entry. Co-locating it with its sole caller keeps the reconciler self-contained and avoids a cross-module private-method call pattern. Move as-is in PR3 commit 9 alongside `reconcile_entry_fills` and `reconcile_exit_fills`. No behavior change.

---

### A.3 Migration Sequence

See `docs/specs/live-executor-decomposition.md` §Migration strategy for the full commit-level plan. This section summarizes with test-point checkpoints.

#### PR2 — Protective orders (MED risk)

| Commit | Description | Test checkpoint |
|--------|-------------|-----------------|
| 7 | `feat: extract protective_orders module` — 5 methods become 2-line delegations | `pytest -x` green; dry-run identity test passes |
| 8 | `test: protective_orders unit tests` — RCA #7 double-stop guard, trailing branch, OCO/bracket paths | `pytest tests/test_protective_orders.py -v` full pass |

See `docs/specs/live-executor-decomposition.md` §PR2 for commit detail.

#### PR3 — Reconciler + slim core (MED-HIGH risk)

| Commit | Description | Test checkpoint |
|--------|-------------|-----------------|
| 9 | `feat: extract execution_reconciler module` — `reconcile_entry_fills`, `reconcile_exit_fills`, `_record_same_bar_round_trip` → `execution_reconciler.py` | `pytest -x` green; dry-run identity test + `sync_protective_orders.py` dry-run snapshot match |
| 10 | `refactor: sync_protective_orders.py imports reconciler functions directly` — eliminates `__new__` bypass from this script | `test_sync_protective_source_contains_mode_fix` updated + passing |
| 11 | `refactor: slim _execute_entry` — guards run as composable list | `pytest -x` green; `test_execution_integration.py` full pass |
| 12 | `refactor: slim _execute_exit` similarly | `pytest -x` green; dry-run identity test |
| 13 | `refactor: extract execute_plan phase helpers` — `_run_exits_phase`, `_run_entries_phase`, `_place_stops_phase` | `pytest -x` green; dry-run identity test |

See `docs/specs/live-executor-decomposition.md` §PR3 for commit detail.

**Hard rule (premarket window):** PR3 commits 11–13 must be merged before 12:00 AEST same day, leaving ≥11 hours of paper-validation runway before the 23:15 AEST live execution window. If a commit ships and the night cycle errors, kill-switch via `HALT_FILE` write, then `git revert <sha>` + redeploy (~5 min).

---

### A.4 Risk Assessment

#### Non-negotiable constraints

1. **`__new__` bypass pattern** — 5 ops scripts bypass `__init__` and manually set `_broker`, `_connected`, `_mode`, `config`. These four attributes are de-facto public. **Never rename.** PR1.5 (drop `_mode`) remains deferred until all 5 scripts are updated.

2. **`EXECUTION_LOG` path stability** — `research/brain/execution.py`, `scripts/slippage_calibration.py`, and `healthz.py` independently resolve `PROJECT_ROOT / "logs" / "live_executions.jsonl"`. Stabilized in `brokers/execution_journal.py` (PR1.1). Do not move or rename.

3. **`execute_plan` return dict contract** — Shape `{successful_entries, successful_exits, entries[], exits[], error, circuit_breaker_tripped, volatility_gate{}}` consumed by `execute_approved.py` (cron 23:15–23:20 AEST), `/api/approve`, and Telegram approval. Cannot change.

#### Per-PR risk table

| PR | Step | Blast radius | Rollback |
|----|------|-------------|---------|
| PR2 | Commit 7 (extract protective_orders) | 5 protective methods → 2-line wrappers; mis-wire = stops not placed = uncapped loss | `git revert <sha>` + redeploy ~5 min; `HALT_FILE` write stops all trading immediately |
| PR2 | Commit 8 (tests only) | None | Trivial revert |
| PR3 | Commit 9 (reconciler extract) | Fills not backfilled → orphaned positions in ledger | `git revert <sha>` + redeploy; reconciler has strong existing coverage |
| PR3 | Commit 10 (remove `__new__` bypass from sync script) | `sync_protective_orders.py` fails at startup → no sync for the night cycle | `git revert <sha>`; test with `ATLAS_DRY_RUN=1` before merge |
| PR3 | Commits 11–12 (slim `_execute_entry`/`_execute_exit`) | Guard ordering error could permit bad trades | Each commit independently revertable; 11h pre-live window required |
| PR3 | Commit 13 (slim `execute_plan`) | Orchestration ordering; phase helpers must preserve exit-before-entry invariant | `git revert <sha>`; dry-run identity test is the acceptance gate |

---

### A.5 Test Strategy

#### Dry-run identity test (primary acceptance gate)

Capture a pre-refactor baseline once, before PR2 commit 7:
```
ATLAS_DRY_RUN=1 python -m scripts.execute_approved sp500 --capture-output baseline.json
```
After every commit:
```
ATLAS_DRY_RUN=1 python -m scripts.execute_approved sp500 --capture-output current.json
diff baseline.json current.json   # must be empty
```
Same test for `sync_protective_orders.py`:
```
ATLAS_DRY_RUN=1 python -m scripts.sync_protective_orders sp500 --capture-output sync_baseline.json
```
JSONL journal output must be byte-identical (or differ only in timestamp field). Cited from `docs/specs/live-executor-decomposition.md` §Test strategy.

#### Unit tests per new module

| New module | Test file | Key categories |
|-----------|-----------|---------------|
| `brokers/protective_orders.py` | `tests/test_protective_orders.py` (new) | RCA #7 double-stop guard; trailing vs static branch; OCO/bracket leg construction; cancel on already-filled order |
| `brokers/execution_reconciler.py` | Migrate from `test_reconcile_entry_fills_guard.py`, `test_reconcile_exit_fills_idempotent.py`, `test_same_bar_round_trip.py` | EBAY zombie guard; fill dedup; `_record_same_bar_round_trip` idempotency |
| `brokers/execution_analytics.py` | `tests/test_execution_analytics.py` (new — first coverage ever) | Per-side slippage math; fee aggregation; calibration recommendation thresholds |

#### Test migration

- `test_sync_protective_source_contains_mode_fix` — must be **updated** at PR3 commit 10 to assert the new contract: reconciler functions called with correct `(broker, policy)` args, rather than asserting `__new__` ordering.
- 49 `LiveExecutor` test files — majority stay on LiveExecutor; reconcile/protective/preflight tests move with their target modules.

---

### A.6 Prerequisites

| Prerequisite | Why | Gate |
|-------------|-----|------|
| **#267 — dual-write bridge cut (5/5)** | `execution_reconciler.py` depends on canonical reconcile path being active | Before PR3 commit 9 |
| **#276 — reconcile cutover** | `reconcile_entry_fills`/`reconcile_exit_fills` extraction requires `core/reconcile.py` to be the canonical path; old `reconcile_ledger.py` interaction must be retired | Before PR3 commit 9 |
| **#278 — trade state machine** | Slimming `_execute_entry`/`_execute_exit` (PR3 commits 11–12) requires state transitions to be in `db.transition_trade()`, not inline | Before PR3 commits 11–12 |
| **Test suite green baseline** | 839 grandfathered bare-except offenders must not be increased; establish clean baseline before each PR | Before every PR |

---

### A.7 Effort Estimate

| Phase | Days |
|-------|------|
| PR2: protective_orders extract (commits 7–8) | 3 |
| PR3: reconciler + slim core (commits 9–13) | 5 |
| Regression + integration validation | 2 |
| **live_executor.py subtotal** | **10** |

---

## B. `brokers/alpaca/broker.py`

### B.1 Current State

**Actual LoC:** 2,211 (confirmed by `wc -l`). **Entirely undecomposed — zero extractions to date.**

#### Module-level helpers (L1–L213)

| Function | Lines | LoC |
|----------|-------|-----|
| `_is_pdt_error(message)` | L84–129 | 45 |
| `_map_order_status`, `_map_side`, `_map_tif` | L130–160 | 28 |
| `_order_to_result(order, atlas_ticker, side)` | L161–213 | 52 |

#### `AlpacaBroker` class methods (L214–1984)

| Method | Lines | LoC | Flag |
|--------|-------|-----|------|
| `__init__` | L229–260 | 31 | — |
| Properties (5) | L261–294 | 33 | — |
| `_broker_call` | L295–315 | 20 | retry wrapper |
| `connect` | L316–392 | 76 | — |
| `disconnect` | L393–399 | 6 | — |
| `verify_shorting_enabled` | L400–418 | 18 | — |
| `get_account_info` | L419–467 | 48 | — |
| `get_pdt_status` | L468–517 | 49 | — |
| `get_positions` | L518–602 | 84 | — |
| **`place_order`** | L603–747 | **144** | ⚠️ order-type dispatch: market/limit/stop/bracket/OCO |
| `cancel_order` | L748–792 | 44 | — |
| `_wait_for_cancel_confirmed` | L793–857 | 64 | Phase 2C cancel-confirm loop |
| `cancel_all_orders` | L858–896 | 38 | — |
| `get_open_orders` | L897–937 | 40 | OCO leg flattening |
| **`sync_all_protective_orders`** | L938–1806 | **868** | ⚠️⚠️ the beast — 2× prior briefed size |
| `get_order_status` | L1807–1836 | 29 | — |
| `get_prices` | L1837–1857 | 20 | — |
| `get_market_snapshot` | L1858–1879 | 21 | — |
| `get_today_deals` / `get_history_deals` / `get_history_orders` | L1880–1974 | 92 | — |
| `_require_connected` | L1975–1980 | ~5 | guard |

#### Module-level helpers after class (L1986–2211)

| Function | Lines | LoC | Flag |
|----------|-------|-----|------|
| `_prices_match` | L1986–1996 | 10 | — |
| `_summarise_ticker_action` | L1997–2030 | 33 | — |
| **`_build_order_request`** | L2031–2167 | **136** | ⚠️ zero direct tests today |
| `_orders_to_deals` | L2168–2211 | 43 | — |

**Methods >150 LoC (2):** `sync_all_protective_orders` (868), `place_order` (144†).

†`place_order` is just under 150 LoC but logically overloaded: handles market/limit/stop/bracket/OCO dispatch and the OCO atomicity guarantee (both legs in one API call).

#### `sync_all_protective_orders` internal phases (L938–1806)

1. **Fetch + normalize** (L938–1024): positions, flatten OCO legs, build `plan_by_ticker`
2. **Classify existing orders** (L1025–1172): `tickers_with_stop`, `tickers_with_tp`; trailing vs static
3. **Per-position dispatch loop** (L1173–1806): PDT skip → resolve stop/TP prices → Path A (OCO bracket) vs Path B (trailing stop) → tightening check → cancel-confirm → place

#### Dependency graph

**Imports (atlas-relative):**
```
from brokers.base import (AccountInfo, OrderResult, OrderSide, OrderStatus, OrderType, PositionInfo)
from brokers.alpaca import mapper
from brokers.alpaca.market_data import AlpacaMarketData
from brokers.retry import with_retry
from brokers.secrets import get_secret
from brokers.pdt_state import (…)
```

**Reverse dependencies (3 production callers):**

| File | Usage |
|------|-------|
| `brokers/registry.py` | Sole factory — constructs `AlpacaBroker` |
| `scripts/sync_protective_orders.py` | `broker.sync_all_protective_orders(positions, plan)` — only external caller of this method |
| `services/api/dashboard_builder.py` | Docstring reference only |

**13 test files** are `AlpacaBroker`-specific. Key tests for decomposition-safety:

| Test file | What it guards |
|-----------|---------------|
| `test_oco_trailing_upgrade_preserves_tp.py` | Path A (OCO bracket) → Path B (trailing stop) upgrade; TP preservation |
| `test_sync_protective_stuck_held.py` | HELD stop state machine (13 tests) |
| `test_rca_phase2b_sync_cancel_confirm.py` | Cancel-confirm Phase 2C/2B integration |
| `test_alpaca_broker.py` | 12 classes: full broker smoke suite |
| `test_cancel_order_idempotency.py` | Alpaca 42210000 race condition |
| `test_sync_protective_db_consistency.py` | DB consistency pass post-sync |

---

### B.2 Decomposition Plan

#### Proposed module boundaries (4 seams)

| Candidate module | Responsibility | Methods / content | LoC estimate |
|-----------------|----------------|------------------|-------------|
| `brokers/alpaca/market_reader.py` | Market data + history (read-only) | `get_prices`, `get_market_snapshot`, `get_today_deals`, `get_history_deals`, `get_history_orders`, `_prices_match`, `_summarise_ticker_action`, `_orders_to_deals` | ~280 |
| `brokers/alpaca/account_reader.py` | Account state queries (read-only) | `get_account_info`, `get_pdt_status`, `get_positions`, `verify_shorting_enabled` | ~200 |
| `brokers/alpaca/order_manager.py` | Order placement + cancellation | `place_order`, `cancel_order`, `_wait_for_cancel_confirmed`, `cancel_all_orders`, `get_open_orders`, `get_order_status`, `_build_order_request`, `_order_to_result`, `_is_pdt_error`, `_map_*` helpers | ~550 |
| Residual `AlpacaBroker` shell (AlpacaConnect) | Auth + SDK client lifecycle | `__init__`, `connect`, `disconnect`, `_broker_call`, `_require_connected`, 5 properties | ~200 |

After all 4 seams plus Step 3 (Candidate #6 extraction): `AlpacaBroker.sync_all_protective_orders` becomes a ~50-LoC thin delegator calling `ensure_protected` per position. Total reduction: ~1,850 LoC from the original 2,211 LoC.

#### Relationship between Candidate #6 and `AlpacaOrderManager` — these are orthogonal

**Candidate #6** (`docs/specs/protective-orders-unification.md`) lifts the 868-LoC `sync_all_protective_orders` business logic into a broker-agnostic `ensure_protected(broker, ticker, position, plan, ...)` API in `brokers/protective_orders.py`. This eliminates duplicated protective-order decision logic (OCO vs trailing, tightening threshold, PDT skip) that currently exists across three files.

**`AlpacaOrderManager`** handles the remaining ~550 LoC of Alpaca SDK mechanics: `place_order`, `cancel_order`, `_build_order_request`, and the cancel-confirm loop. These are Alpaca-specific implementation details, not business-logic decisions.

The boundary is the `BrokerAdapter` interface: `ensure_protected` calls `broker.place_order()` and `broker.cancel_order()` — methods that `AlpacaOrderManager` implements. There is no conflict:

- Candidate #6 owns **what** to do (OCO vs trailing decision, tightening threshold, PDT skip)
- `AlpacaOrderManager` owns **how** to do it (SDK call construction, retry wrapping, Alpaca 422 handling)

Both decompositions proceed independently and combine cleanly. They are not alternatives.

---

### B.3 Migration Sequence

**Recommended extraction order (lowest to highest risk):**

#### Step 1 — `AlpacaMarketReader` (read-only, lowest risk)

Extract `get_prices`, `get_market_snapshot`, `get_today_deals`, `get_history_deals`, `get_history_orders`, and module-level helpers `_prices_match`, `_summarise_ticker_action`, `_orders_to_deals` into `brokers/alpaca/market_reader.py`.

`AlpacaBroker` methods become 1-line delegations: `def get_prices(self, ...): return self._market_reader.get_prices(...)`.

Test gate: `pytest tests/test_alpaca_broker.py -k "deal or history or price" -v` green.

#### Step 2 — `AlpacaAccountReader` (read-only)

Extract `get_account_info`, `get_pdt_status`, `get_positions`, `verify_shorting_enabled` into `brokers/alpaca/account_reader.py`.

Test gate: `pytest tests/test_alpaca_broker.py -k "account or position or pdt" -v` green; `test_pdt_buy_guard.py` and `test_pdt_backoff_avgo_ccj.py` green.

#### Step 3 — Extract `sync_all_protective_orders` per Candidate #6 (highest value)

Priority extraction — 868 LoC, single external caller. Implement `ensure_protected` in `brokers/protective_orders.py` per `docs/specs/protective-orders-unification.md` §Migration plan (4 phases):

1. **Phase 1:** Add `ensure_protected` to `brokers/protective_orders.py` — no callers routed yet. Write 16-test matrix per Candidate #6 §Testing. Safe to ship any time.
2. **Phase 2:** Route `LiveExecutor.place_stops_for_plan` through `ensure_protected` (depends on Candidate #2 PR2 shipped).
3. **Phase 3:** Route `scripts/sync_protective_orders.py` `sync_market` through `ensure_protected`. Do NOT ship during 23:15–06:00 AEST window.
4. **Phase 4:** `AlpacaBroker.sync_all_protective_orders` becomes a ~50-LoC thin delegator per-position. Do NOT ship during 23:15–06:00 AEST window.

**Hard dependency:** Candidate #2 PR2 (`brokers/protective_orders.py` file) must ship before Steps 3 Phases 2–4.

Test gate: `test_oco_trailing_upgrade_preserves_tp.py`, `test_sync_protective_stuck_held.py` (13 tests), `test_sync_protective_db_consistency.py`, `tests/test_ensure_protected.py` (new, 16+ tests) all green.

#### Step 4 — `AlpacaOrderManager` (highest risk)

Extract `place_order`, `cancel_order`, `_wait_for_cancel_confirmed`, `cancel_all_orders`, `get_open_orders`, `get_order_status`, `_build_order_request`, and mapping helpers into `brokers/alpaca/order_manager.py`.

**Pre-condition: write `_build_order_request` golden-file tests BEFORE moving the function** (see §B.5). It has zero direct tests today.

`place_order` (144 LoC) handles market/limit/stop/bracket/OCO dispatch. The OCO atomicity guarantee (both legs in a single API call) must be preserved exactly. Move the method as-is — do not restructure internal branching during this step.

Test gate: `test_alpaca_broker.py` full suite green; `test_cancel_order_idempotency.py` green; `test_rca_phase2b_sync_cancel_confirm.py` green; `test_rca_phase2a_atomic_bracket.py` green.

#### Step 5 — `AlpacaConnect` residual + full regression

After Steps 1–4, the residual `AlpacaBroker` contains `__init__`, `connect`, `disconnect`, `_broker_call`, `_require_connected`, and 5 properties (~200 LoC). This is the natural "AlpacaConnect" concern. Optionally formalize as a named sub-object or leave as the residual class — cosmetic decision.

Full regression: `pytest tests/test_alpaca_broker.py tests/test_broker_retry_coverage.py tests/test_sync_protective_*.py tests/test_oco_trailing_upgrade_preserves_tp.py -v` green.

---

### B.4 Risk Assessment

| Step | Risk level | Blast radius | Mitigation |
|------|-----------|-------------|-----------|
| Step 1: MarketReader | LOW | Dashboard loses historical deal data if delegation mis-wires | Revert single commit; dashboard continues with stale data gracefully |
| Step 2: AccountReader | LOW | Account info falls back to error response | Revert single commit |
| Step 3 Phase 1: add `ensure_protected` | NEGLIGIBLE | No callers routed — zero blast radius | — |
| Step 3 Phases 2–4: route callers through `ensure_protected` | HIGH | Live order placement via new code path | Do NOT ship during 23:15–06:00 AEST; dry-run identity test required; premarket dry-run passes before merge |
| Step 4: OrderManager | HIGH | `place_order` handles live orders; mis-wire = order not placed or wrong type | Golden-file tests must pre-exist; merge only before 12:00 AEST; dry-run smoke test |
| Step 4: `_build_order_request` move without golden-file tests | CRITICAL | Silent behavioral change in order construction — wrong order type submitted to Alpaca | Write golden-file tests FIRST; this is a hard gate |

**`sync_all_protective_orders` Path A/B equivalence (critical test invariant):**

`test_oco_trailing_upgrade_preserves_tp.py` covers the OCO bracket (Path A) → trailing stop (Path B) upgrade path, including TP preservation. `test_sync_protective_stuck_held.py` (13 tests) covers the HELD stop state machine. After Step 3 Phase 4, both must pass with `ensure_protected` as the implementation. Preserve exactly:
- Tightening threshold: `abs(new - current) / current > 0.005` (0.5%)
- Cancel-confirm timeout: standardize to single env var `ATLAS_PROTECTIVE_CANCEL_TIMEOUT_SEC` (default 5.0s), resolving the two-defaults gotcha documented in Candidate #6 §Gotchas.

**Rollback:** Per-step `git revert <sha>` + redeploy (~5 min). Atlas trades live capital ($5,289 equity, 7 open positions). The 23:15 AEST execution window is the hard cutoff.

---

### B.5 Test Strategy

#### New: `_build_order_request` golden-file tests (pre-condition for Step 4)

Write `tests/brokers/test_build_order_request.py` with one parameterized test case per order type. This function has **zero direct tests today** — write these before moving it.

| Order type | Key assertion |
|------------|--------------|
| Market buy | `type=market`, no `limit_price`, correct `side` and `qty` |
| Limit sell | `type=limit`, `limit_price` present and correct, `time_in_force` set |
| Stop sell | `type=stop`, `stop_price` present, no `limit_price` |
| Bracket buy | `order_class=bracket`, both `stop_loss` and `take_profit` legs present |
| OCO | `order_class=oco`, both legs present, `order_class != bracket` |

These are behavioral lock-in tests: if the move silently changes any field, the golden-file diff catches it.

#### Dry-run identity test for `sync_protective_orders.py`

```
ATLAS_DRY_RUN=1 python -m scripts.sync_protective_orders sp500 --capture-output sync_baseline.json
```
After each Step 3 phase: re-run and `diff sync_baseline.json current.json` (must be empty). Cited from `docs/specs/live-executor-decomposition.md` §Test strategy.

#### New test files required

| Test file | Covers |
|-----------|--------|
| `tests/brokers/test_build_order_request.py` | Golden-file per order type (5 cases) — write BEFORE Step 4 |
| `tests/test_ensure_protected.py` | 16-case state-machine matrix per `docs/specs/protective-orders-unification.md` §Testing |
| `tests/test_protective_orders_migration.py` | `place_stops_for_plan` routes through `ensure_protected` (integration) |

Existing tests to extend: `test_sync_protective_stuck_held.py` — verify `_handle_held_stops` still works after `sync_market` refactor (Step 3 Phase 3).

---

### B.6 Prerequisites

| Prerequisite | Why | Gate |
|-------------|-----|------|
| **Candidate #2 PR2 shipped** (`brokers/protective_orders.py` exists) | Step 3 Phases 2–4 extend this module | Before Step 3 Phase 2 |
| **`_build_order_request` golden-file tests written** | Hard gate — zero tests exist; write before moving the function | Before Step 4 |
| **Test suite green baseline** | Do not start Step 3 or 4 with pre-existing failures; check bare-except count (currently 839, must not increase) | Before Steps 3 and 4 |
| **`docs/specs/broker-base-promotion.md` alignment check** | `ensure_protected` calls `broker.get_open_orders()`, `broker.place_order()`, `broker.cancel_order()` — verify abstract methods are present on `BrokerAdapter` | Before Step 3 Phase 3 |
| **#267 dual-write bridge cut** | Indirect: Candidate #6 `_apply_db_consistency` calls `upsert_protective_record`; canonical write path should be active | Soft gate before Step 3 Phase 4 |

---

### B.7 Effort Estimate

| Phase | Days |
|-------|------|
| Step 1: AlpacaMarketReader | 1 |
| Step 2: AlpacaAccountReader | 1 |
| Step 3: `sync_all_protective_orders` extract — Candidate #6, all 4 phases | 4 |
| Step 4: AlpacaOrderManager (incl. golden-file tests for `_build_order_request`) | 4 |
| Step 5: AlpacaConnect residual + full regression suite | 2 |
| **alpaca/broker.py subtotal** | **12** |

---

## C. `services/chat_server.py`

### C.1 Current State — Achieved Split

**Actual LoC:** 220 (confirmed by `wc -l`). The decomposition is **complete.**

**What remains in `services/chat_server.py` (220 LoC):**

| Section | Content |
|---------|---------|
| App setup + lifespan | `FastAPI(...)` + startup: chat DB init + `targets.json` stub |
| `MaxBodySizeMiddleware` | Rejects Content-Length > 1 MB |
| `CSPMiddleware` | Content-Security-Policy header |
| `add_security_headers` | X-Frame-Options, X-Content-Type-Options, Referrer-Policy |
| 17 router imports + mounts | All business logic delegated to sub-packages |
| Backward-compat re-exports | `_calc_alpaca_intraday_pnl`, `_calc_tiingo_daily_pnl`, `_build_dashboard_data`, `_approve_and_execute`, `_execute_live`, `_reject_plan`, `PlanRequest`, `check_auth` |
| Entry point | `uvicorn.run(app, ...)` |

**Extracted to `services/api/` (17 routers):**

| Router file | Responsibility |
|-------------|---------------|
| `api/finance.py` | `/api/finance` |
| `api/regime.py` | `/api/regime/*` |
| `api/portfolio.py` | Portfolio, trades, performance, equity-curve |
| `api/health.py` | System health, macro gauges |
| `api/risk.py` | Positions risk |
| `api/research.py` | Research experiments |
| `api/research_matrix.py` | Coverage matrix |
| `api/promotions.py` | Research promotion approvals |
| `api/dashboard.py` | `/api/dashboard-data` main payload |
| `api/approvals.py` | `/api/approve`, `/api/reject` |
| `api/chat_sessions.py` | `/api/chat/sessions/*` CRUD |
| `api/monitor_legacy.py` | `/api/monitor/*` → 410 Gone stubs |
| `api/admin.py` | Admin endpoints |
| `api/lifecycle.py` | Strategy lifecycle (orphan `strategy_lifecycle.py` deleted `edfe6efa` 2026-05-14) |
| `api/static_serve.py` | Catch-all `/{path:path}` — **MUST be last** (order-dependent) |
| `api/error_remediation.py` | Error remediation |
| `api/dashboard_builder.py` | Supporting: `build_dashboard_data()` complex payload builder |

**Extracted to `services/ws/` (1 module):**

| Module | Responsibility |
|--------|---------------|
| `ws/chat.py` | WebSocket `/ws/chat?token=<tok>` handler |

**Supporting modules:**

| Module | Responsibility |
|--------|---------------|
| `services/auth.py` | `check_auth` (HTTP Basic, timing-safe) |
| `services/chat_db.py` | `init_db()` for `data/chat.db` |

---

### C.2 Decomposition Plan

**N/A — Decomposition complete.** No further structural work in scope for task #226.

**Achieved in 11 commits, 2026-04-29 through 2026-04-30:**

| Commit | Date | Description |
|--------|------|-------------|
| `c50a98ee` | 2026-04-29 | Phase 1+2: extract `/api/finance` + `/api/regime/*` |
| Task #285 phases 3–7 | 2026-04-30 | Extract `/api/research`, `/api/promotions`, `/api/risk`, `/api/health`, `/api/portfolio` |
| `3c2ee08d` | 2026-04-30 | Task #285 final: strip 1,565 LoC — wire 5 new routers |
| `9338ff66` | 2026-04-30 | Phase 8: extract dashboard data + approvals |
| `fb786f61` | 2026-04-30 | Phase 9: extract chat sessions + monitor stubs |
| `2dd6a28d` | 2026-04-30 | Phase 10: extract WebSocket handler + static serving |
| `c106a54e` | 2026-04-30 | Phase 11: clean up `chat_server.py` — bootstrap-only |
| `edfe6efa` | 2026-05-14 | Delete orphan `strategy_lifecycle.py`; migrate empty-history test |

**Wave 2 task #285 status:** Original scope was "strip 1,565 LoC from chat_server.py — wire 5 new API routers." Executed 2026-04-30 (`3c2ee08d`). Any remaining items labelled #285 in the backlog should be **re-scoped or closed** — the decomposition is complete.

---

### C.3 Migration Sequence

N/A — Decomposition complete as of 2026-04-30. Timeline documented in §C.2.

---

### C.4 Risk Assessment

No ongoing risk — decomposition is stable in production. One standing constraint:

**`api/static_serve.py` must remain last in mount order.** The catch-all `/{path:path}` route captures any unmatched path. If a future router is appended after `static_serve.py` in `chat_server.py`, it will be silently shadowed. This is enforced by a comment in `chat_server.py`; no code change should reorder router mounts without verifying this.

---

### C.5 Test Strategy

Decomposition correctness is verified by the following existing test files:

| Test file | What it validates |
|-----------|------------------|
| `tests/test_api_smoke.py` | All routers respond; HTTP 200 on key endpoints |
| `tests/test_api_health.py` | `/api/health` endpoint correctness |
| `tests/test_api_portfolio.py` | Portfolio and equity-curve endpoints |
| `tests/test_api_research.py` | Research experiments endpoints |
| `tests/test_api_risk.py` | Positions risk endpoint |
| `tests/test_api_promotions.py` | Promotions endpoint |
| `tests/test_dashboard_builder.py` | `build_dashboard_data()` isolation |
| `tests/test_dashboard_e2e.py` | End-to-end dashboard payload |
| `tests/services/test_finance_router_extraction.py` | Finance router extraction regression |
| `tests/test_chat_server_exceptions.py` | Exception handling |
| `tests/test_chat_server_p2.py` | Chat sessions CRUD |

No new test files required.

---

### C.6 Prerequisites

None. Decomposition complete.

---

### C.7 Effort Estimate

| Phase | Days |
|-------|------|
| Documentation update (this spec; confirm task #285 closed) | 0.5 |
| **chat_server.py subtotal** | **0.5** |

---

## Summary Effort Table

| File | Phase | Days |
|------|-------|------|
| `live_executor.py` | PR2: protective_orders extract (commits 7–8) | 3 |
| `live_executor.py` | PR3: reconciler + slim core (commits 9–13) | 5 |
| `live_executor.py` | Regression + integration | 2 |
| `live_executor.py` | **Subtotal** | **10** |
| `alpaca/broker.py` | Steps 1–2: Reader splits (Market + Account) | 2 |
| `alpaca/broker.py` | Step 3: `sync_all_protective_orders` extract (Candidate #6) | 4 |
| `alpaca/broker.py` | Step 4: AlpacaOrderManager split | 4 |
| `alpaca/broker.py` | Step 5: AlpacaConnect residual + regression | 2 |
| `alpaca/broker.py` | **Subtotal** | **12** |
| `chat_server.py` | Documentation only | 0.5 |
| | **Total** | **~22.5 days (~4.5 weeks, 1 engineer)** |

---

## Cross-Cutting Rules

1. **Premarket window is inviolable.** Any commit touching live order placement (`place_order`, `place_stops_for_plan`, `ensure_protected`, reconciler writes) must be merged before 12:00 AEST, leaving ≥11 hours of paper-validation runway before the 23:15 AEST live execution cycle.

2. **Dry-run identity test gates every PR.** See §A.5 for exact commands. Capture a baseline before PR2 commit 7; reuse for all subsequent PR2/PR3 commits. Apply the equivalent snapshot test for `sync_protective_orders.py` at Step 3.

3. **Never rename `_broker`, `_connected`, `_mode`, `config` on `LiveExecutor`.** Five scripts bypass `__init__` via `__new__` and manually set these. Test `test_sync_protective_executor_init.py` asserts this ordering.

4. **`_build_order_request` golden-file tests must pre-exist before the function moves.** It has zero direct tests today. Write `tests/brokers/test_build_order_request.py` (5 order types) as the first action of `alpaca/broker.py` Step 4. This is a hard gate, not a suggestion.

5. **Candidate #6 Phases 2–4 and `alpaca/broker.py` Step 4 are the two highest-risk moments.** Both route live order placement through new code paths. Requirements for each: (a) test suite fully green, (b) dry-run identity test passing, (c) merge before 12:00 AEST, (d) a named engineer monitoring the 23:15 AEST execution cycle.

6. **Bare-except count must not increase.** Currently 839 grandfathered offenders. Verify with `grep -r "except:" brokers/ services/ scripts/ --include="*.py" | wc -l` before each PR.

7. **`static_serve.py` must stay last in mount order** in `chat_server.py`. This is an operational constraint, not a lint rule.

---

*Authored: 2026-05-18 | Primary source of truth: `/tmp/god-file-dep-map.md` | Engineering spec: `docs/specs/live-executor-decomposition.md` | Protective orders API: `docs/specs/protective-orders-unification.md` | Historical context: `docs/phase-c-god-file-decomposition.md`*
