# BrokerRoutingPolicy — Integration Map for LiveExecutor Decomposition

> **Purpose:** Pre-decomposition reference for `brokers/live_executor.py`.
> Describes what `BrokerRoutingPolicy` already owns so those surfaces are not
> re-extracted. Identifies remaining forks that are decomposition candidates.
>
> **Commits landed:** `e9154e02` (module), `0e97ca4f` (migration), `1dcd2aa0` (arch docs)
> **Source:** `brokers/routing_policy.py` (181 lines)

---

## 1. BrokerRoutingPolicy Public Interface

| Method / Property | Return type | Description |
|---|---|---|
| `__init__(config, market_id)` | — | Cheap construction; reads `trading.mode` + `trading.live_enabled`. Immutable after init. |
| `.mode` | `str` | Raw mode string (`"live"`, `"paper"`, `"passive"`). |
| `.is_live` | `bool` | `mode == "live"` |
| `.is_paper` | `bool` | `mode == "paper"` |
| `.is_passive` | `bool` | `mode == "passive"` |
| `.live_enabled` | `bool` | `trading.live_enabled` flag. |
| `should_skip()` | `bool` | Universe-level bail-out: True when passive, or live+!live_enabled. |
| `needs_paper_pass()` | `bool` | DB-backed (memoized): True if ≥1 open paper trade exists for this universe AND `is_paper` is False. Delegates to `db.atlas_db.get_open_paper_trades()`. |
| `.paper_config` | `dict` | Returns `{**config, "trading": {**trading, "mode": "paper"}}`. Never mutates original. |
| `for_paper()` | `BrokerRoutingPolicy` | New policy with mode forced to `"paper"`. Used to instantiate paper-path executors/portfolios. |
| `split_entries_by_lifecycle(entries)` | `(live[], paper[])` | Partitions plan entries by strategy promotion state. Delegates to `monitor.strategy_lifecycle.split_trades_by_lifecycle`. Falls back to `(all, [])` on import error. |
| `trade_table()` | `str` | `"paper_trades"` if paper, else `"trades"`. |
| `protective_table()` | `str` | `"paper_position_protective_orders"` if paper, else `"position_protective_orders"`. |

---

## 2. LiveExecutor Construction

```python
# live_executor.py:267-270
self._mode: str = config.get("trading", {}).get("mode", "live")   # ← redundant duplicate
self._policy: BrokerRoutingPolicy = BrokerRoutingPolicy(
    config, market_id=config.get("market_id", "sp500"),
)
```

`self._mode` is still set on the executor and used in 2 log-message string interpolations
(lines 2801, 2806). It is otherwise redundant with `self._policy.mode`.

---

## 3. Call-Site Map — every `self._policy` access in `live_executor.py`

| Line | Expression | Context / purpose |
|---|---|---|
| 268 | `BrokerRoutingPolicy(config, …)` | Construction in `__init__` |
| 1310 | `self._policy.is_paper` | Branch gate for inline entry write after fill; routes to `record_paper_trade_entry` vs `TradeLedger.record_entry` |
| 1721 | `self._policy.is_paper` | Branch gate for inline exit write after fill; routes to `record_paper_trade_exit` vs `TradeLedger.record_exit` |
| 2782 | `self._policy.trade_table()` | Dedup guard inside `reconcile_entry_fills`: selects `trades` or `paper_trades` for `SELECT … WHERE status='open'` |
| 2835 | `self._policy.is_paper` | Reconcile-entry branch: routes to `record_paper_trade_entry` vs `TradeLedger.record_entry` |
| 3054 | `self._policy.is_paper` | Reconcile-exit branch: routes to `record_paper_trade_exit` vs `TradeLedger.record_exit` |

**Total policy call-sites: 5** (1 construction + 4 `is_paper` guards + 1 `trade_table()` call).

---

## 4. Already Owned by Policy

| Domain | What the policy owns | Where expressed |
|---|---|---|
| **Mode predicates** | `is_paper`, `is_live`, `is_passive` | Used at all 4 write-site branches |
| **DB table routing** | `trade_table()` → `"trades"` / `"paper_trades"` | Line 2782 dedup guard |
| **DB table routing** | `protective_table()` → live/paper protective-orders table | Available but **not yet called** in live_executor (still used by ops scripts only) |
| **Skip gate** | `should_skip()` | Used in all 5 ops scripts; **not called** in live_executor (executor is already inside the live pass by construction) |
| **Paper-pass detection** | `needs_paper_pass()` | Used in all 5 ops scripts; not applicable to executor directly |
| **Paper config derivation** | `paper_config` / `for_paper()` | Used in ops scripts; not applicable to executor directly |
| **Lifecycle split** | `split_entries_by_lifecycle()` | Used in `execute_approved.py`; not yet called in executor |

---

## 5. Still Forked in LiveExecutor — Decomposition Opportunities

These are `if/else` mode-forks that live_executor still owns itself, not routed through `self._policy`.

### 5a. `is_dry_run` — 20 sites, 4 structural forks

`self.is_dry_run` reads `trading.live_safety.dry_run_first`. Used as a
**pre-broker guard** at 4 methods:

| Method | Line | Pattern |
|---|---|---|
| `execute_entry` | ~1091 | `if self.is_dry_run: return {…, dry_run: True}` |
| `execute_exit` | ~1552 | `if self.is_dry_run: return {…, dry_run: True}` |
| `_place_protective_stop` | ~1863 | `if self.is_dry_run: _journal_entry(…); return None` |
| `_place_take_profit` | ~2020 | `if self.is_dry_run: _journal_entry(…); return None` |

**Opportunity:** `dry_run` is a sub-dimension of routing policy (it affects whether the
broker call is real). Could add `policy.is_dry_run` to centralise the flag, or fold
it into an `ExecutionGate` abstraction that checks dry_run + circuit_breaker together.

### 5b. Paper vs live write sites — 4 sites still inline

The 4 `if self._policy.is_paper:` branches (lines 1310, 1721, 2835, 3054) each
contain ~20 lines of inline `record_paper_trade_entry` / `record_paper_trade_exit`
logic. The routing decision is delegated to policy correctly, but the *write logic*
is not encapsulated. These are the primary **decomposition targets** for extracting
an `EntryRecorder` / `ExitRecorder` or a `TradeWriter` abstraction.

### 5c. `protective_table()` not yet called in executor

The policy already has `protective_table()` but `live_executor.py` does not call it.
The `_protective_ledger_enabled()` / `close_protective_record` path (lines ~1749-1770)
uses the `position_protective_orders` table name hardcoded. When paper-path protective
orders are needed, this will need `policy.protective_table()`.

### 5d. `self._mode` string still used as log label

Lines 2801 and 2806 interpolate `self._mode` directly into log messages rather than
`self._policy.mode`. Minor inconsistency; `self._mode` attribute on the executor is
redundant and could be removed once log lines are updated.

### 5e. Circuit breaker / halted — not a routing concern (correct as-is)

`_circuit_breaker_tripped`, `_halted`, `emergency_halt()`, `max_daily_orders`,
`max_order_value` are operational **safety gates**, not routing decisions. They live
correctly on the executor. Do not route through `BrokerRoutingPolicy`.

---

## 6. Summary: Decomposition Surface

```
BrokerRoutingPolicy OWNS (don't re-extract):
  ✅ mode predicates (is_paper / is_live / is_passive)
  ✅ skip gate (should_skip)
  ✅ paper-pass detection (needs_paper_pass, cached)
  ✅ paper config derivation (paper_config, for_paper)
  ✅ lifecycle split (split_entries_by_lifecycle)
  ✅ DB table routing (trade_table, protective_table)

LiveExecutor still owns (decomposition targets for next pass):
  🔲 dry_run gate (4 methods × ~20 lines each) → candidate: ExecutionGate
  🔲 paper vs live write logic (4 inline if/else blocks ~80 lines total) → candidate: TradeWriter
  🔲 protective_table() not wired into executor yet → 1-line fix when paper protective stops needed
  🔲 self._mode redundant string attribute → cleanup (2 log sites)

NOT routing concerns (leave on executor):
  ✅ circuit breaker state + trip logic
  ✅ emergency_halt / _halted
  ✅ max_order_value / max_daily_orders safety checks
```
