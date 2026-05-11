# LiveExecutor Method Map
> `brokers/live_executor.py` — 3,183 lines  
> Purpose: decomposition reference. Maps every significant method to its concern bucket, dependencies, and callers.  
> Generated: 2026-05-07

---

## TODO: Refactor Comments (verbatim)

**Line 25:**
```
# TODO: Refactor — 2190 lines. Split into: PlanLoader, OrderRouter, ExecutionReporter modules.
```
Context (lines 22–31):
```python
    executor.disconnect()
"""
# TODO: Refactor — 2190 lines. Split into: PlanLoader, OrderRouter, ExecutionReporter modules.
# TODO: Split into preflight.py, protective_orders.py, execution_journal.py

from __future__ import annotations

import json
import logging
```
Note: comment says 2190 lines but file is now 3,183 lines — the decomposition debt has grown ~45% since the comment was written.

**Line 26:**
```
# TODO: Split into preflight.py, protective_orders.py, execution_journal.py
```
(Same context block — the two TODOs are adjacent.)

---

## Module-Level State

| Name | Line | Type | Description |
|------|------|------|-------------|
| `_regime_model` | 65 | `None` (lazy global) | Cached `RegimeModel` singleton. Set on first `_get_regime_model()` call. |
| `EXECUTION_LOG` | 75 | `Path` | `PROJECT_ROOT/logs/live_executions.jsonl` — the JSONL execution journal file. |
| `HALT_FILE` | 85 | `Path` | `PROJECT_ROOT/.live_halt` — persisted kill-switch file. |
| `PreflightError` | 145 | Exception class | Raised when a preflight check fails (currently unused by callers — errors returned as lists instead). |

---

## Module-Level Functions

| Function | Lines | Concern | Description |
|----------|-------|---------|-------------|
| `_fmt(x, spec)` | 46–63 | RECONCILIATION | None-safe numeric formatter. Prevents `TypeError` on `None` fill prices in reconcile log messages. |
| `_get_regime_model()` | 67–73 | READ_ONLY_ANALYTICS | Returns cached `RegimeModel` singleton. Used for regime enrichment on every entry/exit record. |
| `_health_log(level, msg, detail)` | 77–84 | TELEGRAM_ALERTS / DB_WRITE_ROUTING | Writes to `system_log` table via `monitor.health_writer`. Non-fatal — swallows all exceptions. |
| `_protective_ledger_enabled()` | 88–100 | DB_WRITE_ROUTING | Reads env var `PROTECTIVE_LEDGER_WRITE_ENABLED` (default true). Guards all Phase B.0 ledger writes. Rollback toggle. |
| `_is_already_protected(broker, ticker)` | 103–143 | PROTECTIVE_ORDERS | Checks if ticker already has a SELL stop/trailing_stop at broker. Used in `place_stops_for_plan` to prevent double-placement (RCA #7). Returns False on error (conservative). |
| `preflight_check_config(config)` | 150–171 | SAFETY_GATING | Validates config has `live_enabled`, `live_safety` section, `max_order_value > 0`, `max_daily_orders > 0`. Returns list of error strings. |
| `preflight_check_order(ticker, side, qty, price, safety, daily_order_count)` | 173–208 | SAFETY_GATING | Per-order value cap, daily order count, qty/price sanity. Returns list of error strings. Called by `_execute_entry` and `_execute_exit`. |
| `_journal_entry(event, data)` | 210–243 | EXECUTION_JOURNAL | **The module-level JSONL journal writer.** Atomic write via `.tmp` staging. Any failure is caught and logged — never interrupts execution. Appends to `EXECUTION_LOG`. |

---

## `__init__` — Instance Attributes

All state that must be threaded through any extracted sub-module:

| Attribute | Type | Set at | Description |
|-----------|------|--------|-------------|
| `self.config` | `dict` | `__init__:254` | Full market config dict. Read by many methods. |
| `self._broker` | `BrokerAdapter \| None` | `__init__:255` | Connected broker instance. Set by `connect()`. |
| `self._connected` | `bool` | `__init__:256` | True after successful `connect()`. |
| `self._daily_order_count` | `int` | `__init__:257` | Counter reset each trading day. Checked by `preflight_check_order`. |
| `self._daily_date` | `str` | `__init__:258` | YYYY-MM-DD of last execution day. Drives daily reset. |
| `self._halted` | `bool` | `__init__:259` | In-process halt flag (also checked via `HALT_FILE`). |
| `self._halt_reason` | `str` | `__init__:260` | Human-readable halt reason. |
| `self._circuit_breaker_tripped` | `bool` | `__init__:262` | True when daily loss % has exceeded `max_daily_loss_pct`. Resets each new day. |
| `self._daily_start_equity` | `float` | `__init__:263` | Broker equity captured at start of each execution day. Used for circuit breaker P&L math. |
| `self._account_info_cache` | `AccountInfo \| None` | `__init__:265` | Per-`execute_plan` cache. Reset to None at top of `execute_plan`, prevents repeated broker RPCs in same plan. |
| `self._mode` | `str` | `__init__:267` | `"live"` / `"paper"` / `"passive"`. Derived from `config.trading.mode`. |
| `self._policy` | `BrokerRoutingPolicy` | `__init__:268` | **Routing policy object.** Provides `is_paper` and `trade_table()`. Constructed with `config` + `market_id`. |

---

## Method Map

### Concern Legend
`1=ORDER_DISPATCH` `2=SAFETY_GATING` `3=DB_WRITE_ROUTING` `4=PROTECTIVE_ORDERS`  
`5=RECONCILIATION` `6=TELEGRAM_ALERTS` `7=READ_ONLY_ANALYTICS` `8=EXECUTION_JOURNAL` `9=ORCHESTRATION`

| Method | Lines | Vis | Primary | Secondary | Key Deps | External Callers | Notes |
|--------|-------|-----|---------|-----------|----------|------------------|-------|
| `__init__` | 253–271 | pub | 9 | 3 | `BrokerRoutingPolicy` | — | Constructs `self._policy`. Does NOT connect broker. |
| `is_live_enabled` | 273–275 | pub (prop) | 2 | — | `self.config` | — | Read-only config gate. |
| `is_dry_run` | 278–283 | pub (prop) | 2 | — | `self.config` | — | When True, all orders are logged but not submitted. |
| `safety` | 285–287 | pub (prop) | 2 | — | `self.config` | — | Returns `trading.live_safety` dict. |
| `max_daily_loss_pct` | 289–300 | pub (prop) | 2 | — | `self.safety` | — | Circuit breaker threshold (default 0.02 = 2%). |
| `_reset_circuit_breaker_if_new_day` | 302–306 | priv | 2 | — | `self._daily_date` | `execute_plan` (internal) | Resets `_circuit_breaker_tripped` and `_daily_start_equity` on new `trade_date`. |
| `_get_cached_account_info` | 308–327 | priv | 7 | 2 | `self._broker.get_account_info()` | `_capture_start_equity`, `_check_circuit_breaker`, `_execute_entry` | Caches within a single `execute_plan` call. Returns None on broker error (fail-soft). |
| `_capture_start_equity` | 329–351 | priv | 2 | — | `_get_cached_account_info` | `execute_plan` (internal) | Called once per plan if not dry-run. Establishes circuit breaker P&L baseline. No-op if already captured today. |
| `_check_circuit_breaker` | 353–447 | priv | 2 | 6, 8 | `_get_cached_account_info`, `_journal_entry`, `send_message` | `execute_plan` (internal) | DAILY DRAWDOWN breaker. Trips when `(start_equity - current_equity) / start_equity > max_daily_loss_pct`. Sends Telegram + journal on trip. Returns `True` = BLOCKED. |
| `connect` | 449–494 | pub | 9 | 2, 8 | `HALT_FILE`, `preflight_check_config`, `brokers.registry.get_live_broker`, `_journal_entry` | `services/api/approvals.py`, `services/telegram_bot.py`, `scripts/sync_protective_orders.py` | Checks halt file → preflight → registry → broker.connect(). Journals connect/fail events. |
| `disconnect` | 496–505 | pub | 9 | 8 | `self._broker.disconnect()`, `_journal_entry` | Callers of connect() | Cleans up broker connection. Always journals. |
| `place_order` | 507–547 | pub | 1 | 2 | `brokers.kill_switch.is_halted()`, `self._broker.place_order()` | `_execute_entry` (internal) | **Kill-switch TOCTOU guard.** Thin wrapper over broker. Re-checks kill switch immediately before every broker call, closing the gap between top-of-method check and actual submission. |
| `execute_plan` | 549–952 | pub | 9 | 2, 4, 6 | `_execute_exit`, `_execute_entry`, `_run_volatility_gate`, `_check_circuit_breaker`, `place_stops_for_plan`, `check_market_state`, `_capture_start_equity`, `filter_tradable`, `_journal_entry` | `services/api/approvals.py:73`, `services/telegram_bot.py:382` | **Top-level orchestrator.** Order: exits → vol gate → circuit breaker → overlay resolve → entries → place stops. Manages overlay shadow/enforce mode and M3 sizing multiplier. Returns `report` dict. |
| `_execute_entry` | 953–1461 | priv | 1 | 2, 3, 4, 6, 8 | `place_order`, `risk.cross_universe_guard`, `risk.gross_exposure_guard`, `brokers.price_arbiter`, `preflight_check_order`, `TradeLedger.record_entry`, `atlas_db.record_paper_trade_entry`, `atlas_db.update_trade_protective_orders`, `atlas_db.upsert_protective_record`, `_journal_entry` | `execute_plan` (internal) | **509 lines.** All pre-entry guards (kill-switch, cross-universe, gross-exposure, price-arbiter, preflight, leverage gate). Dry-run short-circuit. Limit order placement with 15s fill poll. On fill: routes to paper vs live ledger via `self._policy.is_paper`. Records bracket legs. B.0 protective ledger upsert. |
| `_execute_exit` | 1462–1810 | priv | 1 | 2, 3, 4, 6, 8 | `_cancel_open_orders_for_ticker`, `cancel_protective_stop`, `preflight_check_order`, `self._broker.place_order()`, `TradeLedger.record_exit`, `atlas_db.record_paper_trade_exit`, `atlas_db.close_protective_record`, `LivePortfolio.record_closed_trade`, `RoundTripStore.build_and_record`, `_journal_entry` | `execute_plan` (internal) | **348 lines.** Cancels all open SELL-side orders first (1s settle). Chooses MARKET for stop-triggered exits, LIMIT (−1% buffer) for signal exits. 60s fill poll. Guards unfilled exit from ledger. Routes paper/live via `self._policy.is_paper`. B.0 ledger close on exit. |
| `place_protective_stop` | 1811–1920 | pub | 4 | 8 | `self._broker.place_order()`, `_journal_entry` | `place_stops_for_plan` (internal) | Places STOP or TRAILING_STOP SELL GTC. `trailing_atr > 0` → `TRAILING_STOP`; else → `STOP`. Returns order_id or None. |
| `cancel_protective_stop` | 1921–1938 | pub | 4 | 8 | `self._broker.cancel_order()`, `_journal_entry` | `_execute_exit` (internal) | Cancels a single stop by order_id. Returns bool success. |
| `_cancel_open_orders_for_ticker` | 1939–1985 | priv | 4 | 8 | `self._broker.get_open_orders()`, `self._broker.cancel_order()`, `_journal_entry` | `_execute_exit` (internal) | Cancels ALL open SELL-side orders for a ticker. Prevents "insufficient qty" rejection on subsequent exit order. Returns count cancelled. |
| `place_take_profit` | 1987–2057 | pub | 4 | 8 | `self._broker.place_order()`, `_journal_entry` | `place_stops_for_plan` (internal) | Places LIMIT SELL GTC at take_profit price. Returns order_id or None. |
| `place_stops_for_plan` | 2059–2237 | pub | 4 | 8, 3 | `_is_already_protected`, `place_protective_stop`, `place_take_profit`, `atlas_db.upsert_protective_record`, `_journal_entry` | `execute_plan` (internal), `tests/test_overlay_shadow_wiring.py` (mocked) | Dispatches SL+TP or trailing stop after each filled entry. RCA #7 double-placement guard. B.0 protective ledger upsert for all three stop types (bracket/oco/trailing). |
| `get_account_info` | 2239–2242 | pub | 7 | — | `self._broker.get_account_info()` | — | Thin delegation to broker. Returns `AccountInfo` or None. |
| `get_positions` | 2244–2247 | pub | 7 | — | `self._broker.get_positions()` | — | Thin delegation. Returns list of `PositionInfo`. |
| `get_open_orders` | 2249–2253 | pub | 7 | — | `self._broker.get_open_orders()` | — | Thin delegation. Returns list of `OrderResult`. |
| `emergency_halt` | 2256–2276 | pub | 2 | 8 | `HALT_FILE.write_text()`, `self._broker.cancel_all_orders()`, `_journal_entry`, `_health_log` | Telegram bot (indirectly via commands) | Sets `_halted`, writes `HALT_FILE` (persists across restarts), cancels all open orders. Irreversible until `clear_halt()`. |
| `clear_halt` | 2278–2287 | pub | 2 | 8 | `HALT_FILE.unlink()`, `_journal_entry` | Manual / admin path | Removes halt file, resets in-process flags. Requires explicit human action. |
| `check_market_state` | 2289–2333 | pub | 2 | 8 | `self._broker.get_market_states()`, `_journal_entry` | `execute_plan` (internal) | Checks if market is OPEN/TRADEABLE for given tickers. REST/OVERNIGHT/AFTER_HOURS_END → not tradeable. Failure is non-blocking (logs warning, proceeds). |
| `get_fee_analysis` | 2335–2409 | pub | 7 | 8 | `self._broker.get_history_orders()`, `self._broker.get_order_fees()`, `_journal_entry` | — (analytics / manual use) | Compares actual broker fees vs config assumptions. Returns calibration delta. No live trading path. |
| `get_slippage_analysis` | 2411–2469 | pub | 7 | 8 | `self._broker.get_slippage_report()`, `_journal_entry` | — (analytics / manual use) | Compares actual slippage vs config `fees.slippage_pct`. Returns per-side stats + calibration recommendation. |
| `get_execution_history` | 2471–2531 | pub | 7 | — | `self._broker.get_history_orders()`, `self._broker.get_history_deals()`, `self._broker.get_order_fees()` | — (analytics / manual use) | Full order history with VWAP, fees, PnL. No write path. |
| `cancel_unfilled_limits` | 2533–2627 | pub | 1 | 2, 8 | `self._broker.get_open_orders()`, `self._broker.cancel_order()`, `_journal_entry` | Midday cron (not currently in crontab — manual) | Cancels unfilled BUY limit orders after ET cutoff (floor=noon ET). Safety clamp prevents pre-noon cancellation. Protective STOP SELLs are never touched. 2026-04-10 incident note in docstring. |
| `reconcile_entry_fills` | 2629–2892 | pub | 5 | 3, 8 | `TradeLedger`, `self._broker._broker_call(get_orders, CLOSED, 7d)`, `atlas_db.get_db`, `atlas_db.record_paper_trade_entry`, `_get_regime_model`, `self._policy.trade_table()`, `self._policy.is_paper` | `scripts/sync_protective_orders.py:1155` | **264 lines.** Scans last 7 days of CLOSED broker orders. Records FILLED BUY fills missed at submission. EBAY zombie guard: skip BUY if bracket SELL also filled (sell_ts >= buy_ts). SQLite dedup via `self._policy.trade_table()`. Paper/live routing via `self._policy.is_paper`. |
| `reconcile_exit_fills` | 2894–3143 | pub | 5 | 3, 8 | `TradeLedger`, `self._broker._broker_call(get_orders, CLOSED, 7d)`, `atlas_db.record_paper_trade_exit`, `atlas_db.close_protective_record`, `LivePortfolio.record_closed_trade`, `_get_regime_model`, `self._policy.is_paper` | `scripts/sync_protective_orders.py:1163` | **250 lines.** Catches trailing stop fills, signal exit fills, any atlas_* SELL fills not yet in ledger. Infers exit reason from `client_order_id`. Hoists `LivePortfolio` construction outside loop. Phase B.0 protective ledger close. Paper/live routing via `self._policy.is_paper`. |
| `_run_volatility_gate` | 3144–3167 | priv | 2 | — | `scripts.volatility_gate.check_volatility_gate` | `execute_plan` (internal) | Lazy import of volatility gate module. Returns safe `action="none"` fallback on any error. |
| `_error_report` | 3169–3183 | priv | 9 | 8 | `_journal_entry`, `_health_log` | `execute_plan`, `connect` (internal) | Builds error report dict, logs to health + journal. Shared abort path. |

---

## `self._policy` (BrokerRoutingPolicy) — Full Usage Map

| Line | Method | Call | What it does |
|------|--------|------|--------------|
| 268 | `__init__` | constructor | `BrokerRoutingPolicy(config, market_id=...)` — initialised once |
| 1310 | `_execute_entry` | `self._policy.is_paper` | If True → `atlas_db.record_paper_trade_entry` instead of `TradeLedger.record_entry` |
| 1721 | `_execute_exit` | `self._policy.is_paper` | If True → `atlas_db.record_paper_trade_exit` instead of `TradeLedger.record_exit` |
| 2782 | `reconcile_entry_fills` | `self._policy.trade_table()` | Returns `"paper_trades"` or `"trades"` — used in SQLite dedup query |
| 2835 | `reconcile_entry_fills` | `self._policy.is_paper` | If True → `atlas_db.record_paper_trade_entry` instead of `TradeLedger.record_entry` |
| 3054 | `reconcile_exit_fills` | `self._policy.is_paper` | If True → `atlas_db.record_paper_trade_exit` instead of `TradeLedger.record_exit` |

**Summary:** `self._policy` gates every DB write path in the three execution methods. The gate is always a binary paper/live fork — no partial routing implemented yet. `trade_table()` is the only method returning a string (used in raw SQL); `is_paper` is a bool property for the other 4 sites.

---

## External Callers (from grep)

| Caller | File | What it calls |
|--------|------|---------------|
| `approvals.py:68-73` | `services/api/approvals.py` | `LiveExecutor(config)` → `connect()` → `execute_plan(plan, trade_date)` |
| `telegram_bot.py:366-382` | `services/telegram_bot.py` | `LiveExecutor(config)` → `connect()` → `execute_plan(plan, trade_date)` |
| `sync_protective_orders.py:1155,1163` | `scripts/sync_protective_orders.py` | `executor.reconcile_entry_fills(plan)` then `executor.reconcile_exit_fills()` — called from `sync_market()` per-universe |
| Tests (shape-checks) | `tests/test_rca_phase2a_atomic_bracket.py` | `LiveExecutor(cfg)` → `_execute_entry` internals (atomic bracket) |
| Tests (execute_plan) | `tests/test_overlay_shadow_wiring.py` | `execute_plan(plan, trade_date)` — overlay shadow/enforce wiring |
| Tests (mock) | `tests/test_sync_protective_universe_scoping.py` | `reconcile_entry_fills` and `reconcile_exit_fills` mocked as stubs |
| Tests (mock) | `tests/test_pdt_backoff_avgo_ccj.py` | Same mocked stubs |

**`sync_all_protective_orders` is NOT a method on `LiveExecutor`** — it is on the broker adapter (`AlpacaBroker`). `sync_protective_orders.py` calls it directly on the broker, separately from the LiveExecutor reconcile methods.

---

## Suggested Decomposition Boundaries

Based on the concern map above, the two TODOs suggest these natural cuts:

| Proposed Module | Methods to extract | ~Lines freed |
|-----------------|-------------------|-------------|
| `brokers/preflight.py` | `preflight_check_config`, `preflight_check_order`, `_protective_ledger_enabled`, `_is_already_protected`, `PreflightError` | ~130 |
| `brokers/protective_orders.py` | `place_protective_stop`, `cancel_protective_stop`, `_cancel_open_orders_for_ticker`, `place_take_profit`, `place_stops_for_plan` | ~430 |
| `brokers/execution_journal.py` | `_journal_entry`, `EXECUTION_LOG` module-level | ~35 |
| `brokers/execution_analytics.py` | `get_fee_analysis`, `get_slippage_analysis`, `get_execution_history` | ~200 |
| `brokers/fill_reconciler.py` | `reconcile_entry_fills`, `reconcile_exit_fills` | ~520 |

Remaining in `live_executor.py` after extraction: `__init__`, lifecycle (`connect`/`disconnect`), `execute_plan`, `_execute_entry`, `_execute_exit`, `place_order`, circuit-breaker cluster, `check_market_state`, `cancel_unfilled_limits`, `emergency_halt`/`clear_halt`, `_run_volatility_gate`, `_error_report` — approximately **1,850 lines**, down from 3,183.

`self._policy` and `self._broker` must be accessible to `fill_reconciler.py` — constructor injection or a context object is cleaner than re-reading config.
