# LiveExecutor Integration Surface Map
**Generated:** 2026-05-07  
**Source file:** `brokers/live_executor.py` (3,169+ lines)  
**Module-level constant:** `EXECUTION_LOG = PROJECT_ROOT / "logs" / "live_executions.jsonl"`

---

## 1. Public Method Contract

| Method | Signature | Returns | Notes |
|--------|-----------|---------|-------|
| `connect` | `() → bool` | `True` if connected | Must be called before `execute_plan`. Sets `self._connected`, `self._broker`. |
| `disconnect` | `() → None` | — | Releases broker connection. Always call in `finally`. |
| `execute_plan` | `(plan: dict, trade_date: str) → dict` | execution report | **Primary entry point.** Calls `_execute_entry`/`_execute_exit` internally. Handles circuit breaker, vol gate, overlay. |
| `place_order` | `(**kwargs) → OrderResult \| None` | order result | Low-level passthrough to broker. Used by sync_protective directly. |
| `place_protective_stop` | `(ticker, order_id, stop_price, qty, ...) → str \| None` | order_id | Returns new stop order ID or None on failure. |
| `cancel_protective_stop` | `(order_id, ticker) → bool` | True if cancelled | Single-order cancel. |
| `_cancel_open_orders_for_ticker` | `(ticker: str) → int` | cancelled count | **Called via `__new__` pattern** by 4 callers. Cancels ALL open sell-side orders for ticker. |
| `place_take_profit` | `(ticker, order_id, tp_price, qty, ...) → str \| None` | order_id | — |
| `place_stops_for_plan` | `(plan, ...) → dict` | results | Batch stop placement from a plan dict. |
| `get_account_info` | `() → AccountInfo \| None` | account | Delegates to `_get_cached_account_info`. |
| `get_positions` | `() → list[PositionInfo]` | positions | — |
| `get_open_orders` | `() → list[OrderResult]` | orders | — |
| `emergency_halt` | `(reason: str) → None` | — | Sets `_halted=True`, writes halt file. |
| `clear_halt` | `() → None` | — | Clears in-memory halt + halt file. |
| `check_market_state` | `(tickers: list \| None) → dict` | state dict | — |
| `reconcile_entry_fills` | `(plan: dict \| None) → list` | reconciled entries | **Also called via `__new__`** by `sync_protective_orders.py`. Reads `self._mode` — must be set before call. |
| `reconcile_exit_fills` | `() → list` | reconciled exits | **Also called via `__new__`** by `sync_protective_orders.py`. |
| `get_fee_analysis` | `(days: int = 90) → dict` | fee stats | Reads `EXECUTION_LOG`. |
| `get_slippage_analysis` | `(days: int = 90) → dict` | slippage stats | Reads `EXECUTION_LOG`. |
| `get_execution_history` | `(days: int = 30) → dict` | history dict | Reads `EXECUTION_LOG`. |
| `cancel_unfilled_limits` | `(cutoff_hour: int = 12) → list` | cancelled list | — |

**Module-level public functions (NOT class methods):**

| Function | Signature | Called by |
|----------|-----------|-----------|
| `preflight_check_config` | `(config: dict) → list[str]` | Not seen in callers — test/init path |
| `preflight_check_order` | `(order: dict, ...) → list[str]` | Internal only |

**Internal-only (underscored) but accessed by callers via `__new__` pattern:**

| Attribute/Method | Callers using `__new__` |
|-----------------|------------------------|
| `_broker` | sync_protective, eod_settlement, consolidation_close, close_mrvl_orphan |
| `_connected` | all `__new__` sites |
| `_mode` | sync_protective only (also sets `config`) |
| `_cancel_open_orders_for_ticker` | sync_protective, eod_settlement (×2), consolidation_close, close_mrvl_orphan |
| `reconcile_entry_fills` | sync_protective |
| `reconcile_exit_fills` | sync_protective |
| `_halted` | tests/test_execution_integration.py, test_circuit_breaker.py |
| `_circuit_breaker_tripped` | tests/test_execution_integration.py, test_circuit_breaker.py |

---

## 2. Callers — Who Instantiates LiveExecutor

### 2a. Full `LiveExecutor(config)` construction (normal path)

| File | Line | Method(s) Called | Context |
|------|------|-----------------|---------|
| `scripts/execute_approved.py` | 95, 103 | `.connect()`, `.execute_plan()`, `.disconnect()` | Cron at 23:15/23:20 AEST M-F, 3 markets |
| `services/api/approvals.py` | 64, 68 | `.connect()`, `.execute_plan()`, `.disconnect()` | REST API `/api/approve` endpoint |
| `services/telegram_bot.py` | 362, 366 | `.connect()`, `.execute_plan()`, `.disconnect()` | Telegram button approval flow |
| `scripts/cli.py` | 714, 716 | `.connect()`, `.execute_plan()`, `.disconnect()` | `cli execute-plan` command |
| `scripts/cli.py` | 846–847, 856 | `._broker`, `.connect()`, `get_fee_analysis()` | `cli fee-analysis` command |
| `scripts/cli.py` | 901, 911–913 | `._broker`, `._connected`, `get_fee_analysis()`, `get_slippage_analysis()` | `cli slippage-analysis` command |
| `brokers/registry.py` | 155–156 | returns uninit executor | `get_live_executor()` backward-compat wrapper |

### 2b. `LiveExecutor.__new__()` (bypass constructor — shell pattern)

These callers need a single method without full initialization. All manually set `_broker`, `_connected`.

| File | Line | Method(s) Used | Attributes Manually Set | Context |
|------|------|---------------|------------------------|---------|
| `scripts/sync_protective_orders.py` | 1148–1155 | `reconcile_entry_fills()`, `reconcile_exit_fills()` | `_broker`, `_connected`, `config`, `_mode` | Inside `run_sync_for_market()` after broker connect |
| `scripts/eod_settlement.py` | 146–149 | `_cancel_open_orders_for_ticker()` | `_broker`, `_connected` | Stop-hit path: cancel before market sell |
| `scripts/eod_settlement.py` | 266–269 | `_cancel_open_orders_for_ticker()` | `_broker`, `_connected` | TP-hit path: cancel before market sell |
| `scripts/consolidation_close_positions.py` | 119–124 | `_cancel_open_orders_for_ticker()` | `_broker`, `_connected` | Close all protective orders for consolidation |
| `scripts/tools/archive/close_mrvl_orphan.py` | 58–64 | `_cancel_open_orders_for_ticker()` | `_broker`, `_connected` | One-off orphan close (archived) |

### 2c. Import-only (no instantiation)

| File | Line | Purpose |
|------|------|---------|
| `brokers/__init__.py` | 18 | Re-exports `get_live_executor` |
| `brokers/registry.py` | 155 | Imported inside `get_live_executor()` factory |
| `core/fix_worker.py` | 99 | Comment only — lists it as a dependent module |
| `core/reconcile.py` | 552 | Comment only |
| `risk/cross_universe_guard.py` | 11 | Comment only |
| `scripts/backfill_errors_from_logs.py` | 63 | Comment in log format example |

---

## 3. Premarket / Execution Cycle

### Cron Schedule (from `/var/spool/cron/crontabs/root`)

| Time (AEST) | Command | flock | timeout |
|-------------|---------|-------|---------|
| 23:15 M-F | `execute_approved.py -m sp500` | `/tmp/execute_approved.lock` | 10m |
| 23:15 M-F | `execute_approved.py -m commodity_etfs` | `/tmp/execute_approved_commodity.lock` | 10m |
| 23:20 M-F | `execute_approved.py -m sector_etfs` | `/tmp/execute_approved_sector.lock` | 10m |
| :01/:16/:31/:46 every hour | `sync_protective_orders.py --market sp500` | `/tmp/sync_protective.lock` | 5m |
| :02/:17/:32/:47 every hour | `sync_protective_orders.py --market commodity_etfs` | `/tmp/sync_protective_commodity.lock` | 5m |
| :03/:18/:33/:48 every hour | `sync_protective_orders.py --market sector_etfs` | `/tmp/sync_protective_sector.lock` | 5m |

### `execute_approved.py` → `LiveExecutor` flow

```
main()
 ├─ load config + plan
 ├─ BrokerRoutingPolicy.should_skip() — passive/paper routing
 ├─ _is_market_halted() — DB gate
 ├─ policy.split_entries_by_lifecycle() — live vs paper
 └─ _run_executor()
     ├─ LiveExecutor(config)
     ├─ executor.connect()
     ├─ executor.execute_plan(sub_plan, trade_date)  ← ONLY method called
     └─ executor.disconnect()
```

**Surface used:** `.connect()`, `.execute_plan()`, `.disconnect()` only.

### `sync_protective_orders.py` → `LiveExecutor` flow

```
run_sync_for_market()
 ├─ broker.connect() / broker.get_open_orders() / broker.get_positions()
 │   (using broker directly, not executor)
 └─ LiveExecutor.__new__()
     ├─ _exec._broker = broker
     ├─ _exec._connected = True
     ├─ _exec.config = config
     ├─ _exec._mode = config["trading"]["mode"]
     ├─ _exec.reconcile_entry_fills(plan=plan)
     └─ _exec.reconcile_exit_fills()
```

**Surface used via `__new__`:** `reconcile_entry_fills()`, `reconcile_exit_fills()`, implicitly `_broker`, `_connected`, `_mode`.

---

## 4. Execution Journal (`logs/live_executions.jsonl`)

### Write sites (inside `live_executor.py`)

| Event key | Line | Trigger |
|-----------|------|---------|
| `circuit_breaker_tripped` | 421 | Daily loss > `max_daily_loss_pct` |
| `connect_blocked` | 464 | Pre-flight config errors |
| `connect_failed` | 472, 490 | No broker factory / broker init crash |
| `connected` | 480 | Successful broker connect |
| `disconnected` | 502 | `disconnect()` called |
| `entry_filtered_untradable` | 593 | `filter_tradable` rejects ticker |
| `volatility_gate_block` | 650 | Vol spike blocks all entries |
| `plan_executed` | 922 | End of `execute_plan()` — summary |
| `order_blocked` | 1077, 1549 | Preflight / kill-switch / guard rejects |
| `dry_run_entry` | 1105 | Entry in dry-run mode |
| `leverage_gate_blocked` | 1157 | Prospective leverage > cap |
| `live_entry` | 1459 | Successful live order submitted |
| `exit_no_position` | 1517 | Exit attempted with no open position |
| `dry_run_exit` | 1560 | Exit in dry-run mode |
| `exit_deferred_fill` | 1687 | Exit order submitted, fill pending |
| `live_exit` | 1806 | Successful live exit submitted |
| `dry_run_protective_stop` | 1864 | Stop in dry-run mode |
| `protective_stop_failed` | 1903 | Stop placement threw exception |
| `protective_stop_placed` | 1914 | Stop order confirmed at broker |
| `protective_stop_cancelled` | 1929 | Stop successfully cancelled |
| `pre_exit_orders_cancelled` | 1982 | Cancel-before-exit succeeded |

### Readers of `live_executions.jsonl`

| Reader | File | What it reads |
|--------|------|--------------|
| `analyze_slippage` | `research/brain/execution.py:61` | `event in (live_entry, live_exit)` — fill_price vs planned_price |
| `analyze_fill_quality` | `research/brain/execution.py:157` | `event in (live_entry, live_exit)` — fill quality metrics |
| `analyze_stops` | `research/brain/execution.py:234` | stop placement events |
| `load_fills` | `scripts/slippage_calibration.py:83` | `event in (live_entry, live_exit)` where fill_price > 0 |
| `healthz` | `healthz.py:523` | existence + line count + size check only |
| `monitor/strategy_health.py` | `:153` | Legacy path; replaced by SQLite (comment says so) |
| tests | `tests/test_strategy_health.py:223,667` | Test fixture construction |

### Journal file characteristics

- **Path:** `logs/live_executions.jsonl` (absolute: `PROJECT_ROOT/logs/live_executions.jsonl`)
- **Write method:** atomic tmp-file + `ab` append (lines 231–233)
- **Schema per line:** `{"ts": ISO8601, "event": str, "data": dict}`
- **No rotation** — grows unbounded (healthz warns if >50 MB; EXECUTION_LOG is NOT in weekly_maintenance.sh list)

---

## 5. Telegram Alerts (inside `live_executor.py`)

| Line | Import | Message / Intent | Condition |
|------|--------|-----------------|-----------|
| 432–441 | `utils.telegram.send_message` | `🔴 ATLAS CIRCUIT BREAKER TRIPPED\nDaily loss: $X (Y%)\nLimit: Z%\n⛔ All new entries BLOCKED` | `_check_circuit_breaker()` trips; loss ≥ threshold |
| 961–962 | `utils.telegram.send_message` | `🛑 KILL SWITCH ACTIVE — entry blocked: <reason>` | `kill_switch.is_halted()` returns True at entry gate |
| 986 | `risk.cross_universe_guard.telegram_alert` | (delegated to guard module) | Cross-universe position/buying-power guard rejected entry |
| 1027 | `risk.gross_exposure_gate.telegram_alert_gross_exposure` | (delegated to gate module) | Gross exposure cap exceeded |
| 1166–1174 | `utils.telegram.send_message` | `🚫 LEVERAGE GATE BLOCKED <ticker>\nProspective Xx > cap Yx\nCurrent MV: $A | Order MV: $B | Equity: $C` | Prospective leverage exceeds config cap |
| 657–658 | `scripts.volatility_gate.send_volatility_alert` | (delegated) | Volatility gate fires; not a direct `send_message` call |

**Not in `live_executor.py` itself** (fired by callers):
- Circuit breaker: also tested in `test_circuit_breaker.py:219` (`test_sends_telegram_alert_on_trip`)
- `execute_approved.py` sends its own Telegram on: auto-approve, execution summary, broker connect failure, top-level crash
- `eod_settlement.py` sends Telegram on broker connect failure after retries

---

## 6. Test Coverage Summary

### Test files touching `LiveExecutor` (42 files, 334+ tests)

| Test File | Test Count | Primary Methods Exercised |
|-----------|-----------|--------------------------|
| `test_execution_integration.py` | 21 | `execute_plan` (full flow), circuit breaker, `_halted`, journal events |
| `test_circuit_breaker.py` | 24 | `_check_circuit_breaker`, `_reset_circuit_breaker_if_new_day`, `max_daily_loss_pct`, Telegram alert, journal |
| `test_halt_mechanism_complete.py` | 18 | `emergency_halt`, `clear_halt`, kill-switch, `place_order` during halt |
| `test_execute_approved.py` | 14 | `execute_plan` via `_run_executor`, auto-approve, halt gate |
| `test_live_executor_account_cache.py` | 10 | `_get_cached_account_info`, cache reset between plans |
| `test_live_executor_exit_regression.py` | 9 | `_execute_exit` (no ghost exits, duplicate guard, poll exception) |
| `test_sync_protective_executor_init.py` | 3 | `__new__` pattern, `_mode` attribute set before `reconcile_entry_fills` |
| `test_reconcile_entry_fills_guard.py` | 4 | `reconcile_entry_fills` dedup (skip buy if sell already filled) |
| `test_reconcile_exit_fills_none_safety.py` | 13 | `reconcile_exit_fills` None-safety |
| `test_reconcile_close_dedup.py` | 17 | Duplicate close chain detection in `reconcile_exit_fills` |
| `test_exit_record_integration.py` | 3 | `execute_plan` with poll failure, `_execute_exit` attribute error regression |
| `test_rca_phase2a_atomic_bracket.py` | ~10 | `_execute_entry` with atomic bracket (stop + TP in single call) |
| `test_native_bracket_order.py` | 10 | `_execute_entry` bracket, OTO, plain limit paths |
| `test_place_stops_double_placement_guard.py` | 12 | `_is_already_protected`, `place_protective_stop` double-placement guard |
| `test_b0_protective_wireup.py` | 9 | `_execute_entry` writes protective record, `_execute_exit` closes it |
| `test_sync_protective_save_state.py` | 7 | `reconcile_entry_fills` → state file written |
| `test_sync_protective_universe_scoping.py` | 16 | `reconcile_entry_fills` + `reconcile_exit_fills` scoped to state-file tickers |
| `test_buying_power_gate.py` | 7 | `_execute_entry` buying power check (cross-universe guard) |
| `test_max_gross_exposure_cap.py` | 15 | `_execute_entry` gross exposure gate |
| `test_cross_universe_guard.py` | 10 | `_is_already_protected` helper + double-placement |
| `test_pdt_backoff_avgo_ccj.py` | 5 | `_execute_entry` PDT denial → state record, precheck skip |
| `test_broker_retry_coverage.py` | 10 | `reconcile_entry_fills`, `reconcile_exit_fills` OCO submit via `_broker_call` retry |
| `test_overlay_sizing_override.py` | 3 | `execute_plan` with overlay sizing_override + avoid_tickers |
| `test_overlay_shadow_wiring.py` | 3 | `execute_plan` shadow log + DB row, does not alter order size |
| `test_trade_opened_log.py` | 10 | `_execute_entry` trade-opened log emission |
| `test_no_bare_except_in_execution_code.py` | ~5 | Source-code audit: no bare `except Exception` in execution path |
| `test_bare_except_conversion.py` | ~5 | Regression guard on bare except removal |
| `test_auto_remediation_*` (4 files) | 180+ | `fix_worker.py` + error classifier — LiveExecutor referenced as error source |
| `test_fix_worker.py` | 41 | `core/fix_worker.py` which lists `brokers/live_executor.py` as dependent module |
| `test_error_fingerprint.py` | 35 | Error event pipeline fed by journal |
| `test_phase2_dispatch_wiring.py` | 10 | Dispatch wiring that consumes LiveExecutor error events |
| `test_triage_classifier.py` | 88 | Error triage rules, some from executor error patterns |
| `test_classifier_30day_replay.py` | ~10 | Replay of journal events through triage |
| `test_backfill_errors_from_logs.py` | ~5 | `backfill_errors_from_logs.py` which reads live_executor log format |
| `test_execute_approved_paper_routing.py` | ~5 | `execute_approved.py` paper/live routing via `BrokerRoutingPolicy` |

### Methods with NO direct test coverage found

| Method | Risk |
|--------|------|
| `get_execution_history` | No test; reads EXECUTION_LOG directly |
| `cancel_unfilled_limits` | No test found |
| `check_market_state` | No test found |
| `place_stops_for_plan` | Only `place_protective_stop` tested individually |
| `get_fee_analysis` / `get_slippage_analysis` | Tested indirectly via `cli.py` manual path only |

---

## 7. Key Decomposition Constraints

These are the surfaces that a split of `live_executor.py` cannot break:

1. **`__new__` pattern is load-bearing** — 4 production scripts bypass `__init__` and directly set `_broker`, `_connected`, `_mode`, `config`. Any refactor that moves these attributes or renames them will silently break `sync_protective_orders.py`, `eod_settlement.py`, `consolidation_close_positions.py`.

2. **`EXECUTION_LOG` path is global state** — it's a module-level constant referenced by 3 reader modules outside `live_executor.py` (`research/brain/execution.py`, `scripts/slippage_calibration.py`, `healthz.py`). If moved, all readers break.

3. **`execute_plan` return shape** — callers read specific keys: `successful_entries`, `successful_exits`, `total_entries`, `total_exits`, `entries[]`, `exits[]`, `error`, `circuit_breaker_tripped`, `volatility_gate{}`. The Telegram summary in `execute_approved.py` iterates `report["entries"]` for `ticker`, `status`, `price`, `qty`, `success`.

4. **`_check_circuit_breaker` writes the journal and sends Telegram** — tests assert both side effects. If extracted, both must stay wired.

5. **`reconcile_entry_fills` reads `self._mode`** — test `test_sync_protective_executor_init.py::test_sync_protective_source_contains_mode_fix` asserts `_mode` is set *before* the call in `sync_protective_orders.py`. Any refactor that changes attribute name breaks both production code and the guard test.

6. **`_cancel_open_orders_for_ticker` is semi-public** — used by 4 callers via the `__new__` shell pattern. It is underscored but effectively public API for order cleanup.
