# Protective Orders Unification (Candidate #6)

**Status:** design sketch — engineering-ready
**Predecessor:** `brokers/protective_orders.py` (~430 L, Candidate #2 PR2 — not yet shipped)
**Targets:**
  - `brokers/live_executor.py` — `place_protective_stop`, `cancel_protective_stop`, `place_take_profit`, `place_stops_for_plan` (lines 1673–2100, ~430 L)
  - `brokers/alpaca/broker.py` — `sync_all_protective_orders` (lines 846–1715, ~870 L)
  - `scripts/sync_protective_orders.py` — `sync_market` orchestrator + `_handle_held_stops` + `_apply_db_consistency` + PDT state logic (~800 L of business logic)
**Goal:** Single `ensure_protected(broker, ticker, position, plan, *, dry_run) -> ProtectiveResult` API — idempotent, safe to call repeatedly from any context

## Problem

Protective order placement logic is split across three files with overlapping responsibilities:

| Location | What it does | Lines | Risk if wrong |
|----------|-------------|-------|---------------|
| `brokers/live_executor.py` | Place stop after entry; place TP; place both for plan | ~430 L | Position has no stop → uncapped loss |
| `brokers/alpaca/broker.py` `sync_all_protective_orders` | Check existing stops; detect tightening; cancel+replace; OCO/bracket handling; PDT skip | ~870 L | Duplicate stops, missed tightening, PDT violation |
| `scripts/sync_protective_orders.py` `sync_market` | Load plan, call broker method, apply DB consistency, handle held stops, format Telegram | ~800 L | Stale state, missed reconciliation |

There is no single function that answers: "Given a position and a plan, is this position fully protected? If not, make it so."

---

## Pre-investigation: catalog of protective order placement sites

### Site 1 — `LiveExecutor.place_stops_for_plan` (lines 1921–2100)

Called from `execute_plan` after entries are placed. Iterates plan entries; calls `place_protective_stop` + `place_take_profit` per ticker. Has `_is_already_protected` guard (from commit `ff6f0720`) to skip tickers that already have a stop (prevents double-placement from bracket OCO fill).

```python
def place_stops_for_plan(self, broker, policy, plan: dict, trade_date: str) -> dict:
    for ticker, signal in plan.items():
        if _is_already_protected(self._broker, ticker):
            continue
        sl_id = self.place_protective_stop(ticker, qty, stop_price, ...)
        tp_id = self.place_take_profit(ticker, qty, take_profit, ...)
```

### Site 2 — `LiveExecutor._execute_entry` (line ~1079)

Atomic bracket: synthesizes `_atomic_take_profit = entry + 2×risk` when signal has stop but no TP. Passes both `stop_loss_price` and `take_profit_price` to `self._broker.place_order(order_class='bracket')`. This is NOT redundant with Site 1 — bracket orders are placed at the broker atomically; Sites 1 + `sync_all_protective_orders` handle the case where the bracket wasn't requested or failed.

### Site 3 — `LiveExecutor.place_protective_stop` (lines 1673–1782)

Places a single STOP or TRAILING_STOP SELL GTC. Returns order ID or None. Dry-run aware via `self.is_dry_run`.

Signature:
```python
def place_protective_stop(self, ticker, qty, stop_price, strategy="",
                          trailing_atr=0.0, trade_date="", direction="long") -> str | None
```

### Site 4 — `LiveExecutor.cancel_protective_stop` (lines 1783–1848)

Cancels an existing stop order. Returns bool. Dry-run aware.

### Site 5 — `LiveExecutor.place_take_profit` (lines 1849–1920)

Places a LIMIT SELL GTC. Returns order ID or None.

Signature:
```python
def place_take_profit(self, ticker, qty, limit_price, strategy="", trade_date="") -> str | None
```

### Site 6 — `AlpacaBroker.sync_all_protective_orders` (lines 846–1715)

The 870 L beast. For each position:
1. Fetch all open orders for the ticker (stop / stop_limit / trailing_stop / limit_sell)
2. Classify existing orders into: stop, tp, trailing
3. Check plan for `stop_price` / `take_profit` / `trailing_atr`
4. Decision tree:
   - Has stop AND tp → check if tightening needed
   - Has stop, no tp → add TP or upgrade to OCO
   - No stop → place fresh stop (STOP or TRAILING_STOP)
   - Price already past TP → skip TP
   - PDT flag → skip with _pdt_should_skip
5. For tightening: cancel old stop → wait for cancel confirm → place new stop

Key constants from this method (preserve exactly in `ensure_protected`):
- OCO tightening threshold: 0.5% tighter (hardcoded at line ~1132)
- Cancel-confirm timeout: `ATLAS_BROKER_CANCEL_CONFIRM_TIMEOUT_SEC` env var (default 5.0s for sync_protective_orders, 10.0s for broker.py — see RCA 2B and 2C)
- PDT skip: `_pdt_should_skip(ticker, market_id, pdt_state)` (RTH hours ≥14 UTC)

### Site 7 — `scripts/sync_protective_orders.py` `_handle_held_stops` (lines 309–600)

Detects Alpaca `HELD` status stops (placed pre-market, not yet activated). Loads `_HELD_STATE_FILE`. For stops held >4h: cancel + re-place as MARKET-adjacent. 13 unit tests in `test_sync_protective_stuck_held.py`.

### Site 8 — `scripts/sync_protective_orders.py` `_apply_db_consistency` (lines 662–840)

After `sync_all_protective_orders` returns, writes stop/TP order IDs back to SQLite (`upsert_protective_record` / `update_trade_protective_orders`). Dual-pass: live + paper.

---

## Proposed module: extending `brokers/protective_orders.py`

Candidate #2 PR2 creates `brokers/protective_orders.py` with these functions:
```python
def place_protective_stop(broker, ticker, qty, stop_price, *, trailing_atr=0.0) -> str | None
def cancel_protective_stop(broker, order_id, ticker="") -> bool
def cancel_open_orders_for_ticker(broker, ticker) -> int
def place_take_profit(broker, ticker, qty, limit_price) -> str | None
def place_stops_for_plan(broker, policy, plan, trade_date) -> dict
```

This candidate adds the capstone:

```python
# brokers/protective_orders.py (extension to ~600 L total)

from __future__ import annotations
import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class ProtectiveResult:
    ticker: str
    action: str           # 'no_op' | 'placed_stop' | 'placed_tp' | 'placed_both' |
                          #  'tightened_stop' | 'tightened_tp' | 'tightened_both' |
                          #  'skip_pdt' | 'error'
    stop_order_id: str | None = None
    tp_order_id: str | None = None
    previous_stop_order_id: str | None = None   # set when tightening
    previous_tp_order_id: str | None = None     # set when tightening
    error: str | None = None
    dry_run: bool = False


def ensure_protected(
    broker,
    ticker: str,
    position: Any,            # PositionInfo or dict with qty, entry_price, strategy
    plan: dict | None,        # plan entry for this ticker with stop_price, take_profit
    *,
    market_id: str = "sp500",
    dry_run: bool = False,
    pdt_state: dict | None = None,
    trade_date: str = "",
) -> ProtectiveResult:
    """Idempotent: ensure `ticker` has stop + TP protection matching `plan`.

    Safe to call repeatedly — if protection is already in place and correct,
    returns ProtectiveResult(action='no_op').

    Decision flow:
      1. Fetch open orders for ticker
      2. Classify existing orders (stop, tp, trailing) via _classify_orders()
      3. Check plan for required stop_price / take_profit
      4. Apply state machine (see table below)
      5. Write results to DB via _apply_db_consistency()
      6. Return ProtectiveResult

    Args:
        broker:     Connected BrokerAdapter (AlpacaBroker or any adapter with
                    get_open_orders() + place_order() + cancel_order())
        ticker:     Position ticker (.AX format)
        position:   PositionInfo or dict; needs qty, entry_price, strategy
        plan:       Plan entry dict; None if no plan available (use state file stop only)
        market_id:  For DB consistency writes and PDT state lookup
        dry_run:    Log intent, no broker calls
        pdt_state:  PDT deferred state dict (from _load_pdt_state); None = no PDT skip
        trade_date: YYYY-MM-DD for order remarks

    Returns:
        ProtectiveResult with action and order IDs
    """
    ...
```

---

## State machine

For each (existing_protection_state) × (plan_state) combination:

| Existing Stop | Existing TP | Plan stop_price | Plan take_profit | Action |
|---------------|-------------|-----------------|------------------|--------|
| None | None | present | present | `placed_both` |
| None | None | present | absent | `placed_stop` |
| None | None | absent | absent | `no_op` (no plan guidance) |
| present (correct) | present (correct) | same | same | `no_op` |
| present (stale/loose) | present | tighter | same | `tightened_stop` |
| present (correct) | present (stale/wrong) | same | tighter | `tightened_tp` |
| present (stale) | present (stale) | tighter | tighter | `tightened_both` |
| present | None | present | present | `placed_tp` |
| None | present | present | present | `placed_stop` |
| present (correct) | None | present | absent | `no_op` (trailing mode OK) |
| present | None | absent | absent | `no_op` |
| PDT deferred | any | any | any | `skip_pdt` |
| any | any | any | any (exception) | `error` |

**Tightening threshold (preserve from `sync_all_protective_orders`):**
- Stop: `abs(new_stop - current_stop) / current_stop > 0.005` (0.5%)
- TP: `abs(new_tp - current_tp) / current_tp > 0.005` (0.5%)
- If within threshold: `no_op` (avoid churning orders)

**PDT rule (preserve exactly from `_pdt_should_skip`):**
- During RTH (hour ≥ 14 UTC): skip if ticker is in `pdt_deferred_state.json`
- During pre-market (hour < 14 UTC): allow retry (process normally)

---

## Private helpers (internal to `ensure_protected`)

```python
def _classify_orders(open_orders: list, ticker: str) -> dict:
    """Classify open SELL orders for ticker into stop/tp/trailing buckets.
    Returns: {'stop': OrderResult|None, 'tp': OrderResult|None, 'trailing': OrderResult|None}
    """

def _is_tightening_needed(current_price: float, new_price: float, threshold: float = 0.005) -> bool:
    """True if abs(new - current)/current > threshold."""

def _cancel_with_confirm(broker, order_id: str, ticker: str, timeout_s: float = 5.0) -> bool:
    """Cancel order and poll for confirmation. Returns True if confirmed cancelled.
    Reads ATLAS_SYNC_PROTECTIVE_CANCEL_TIMEOUT_SEC env var.
    Mirrors _wait_for_cancel_confirm from sync_protective_orders.py (Phase 2B).
    """

def _apply_db_consistency(
    broker, market_id: str, ticker: str, result: ProtectiveResult,
    pass_label: str = "live"
) -> None:
    """Write stop/TP order IDs back to SQLite after placement.
    Calls upsert_protective_record / update_trade_protective_orders.
    Dual-pass: live writes to trades + position_protective_orders;
    paper writes to paper_trades + paper_position_protective_orders.
    """
```

---

## Migration plan

### Phase 1 — Add `ensure_protected` to existing `brokers/protective_orders.py` (after #2 PR2)

Write `ensure_protected` as a pure function. Do NOT route any callers through it yet. Write tests.

### Phase 2 — Route `place_stops_for_plan` through `ensure_protected`

`LiveExecutor.place_stops_for_plan` (Site 1) becomes:
```python
def place_stops_for_plan(self, broker, policy, plan, trade_date) -> dict:
    from brokers.protective_orders import ensure_protected
    results = {}
    for ticker, signal in plan.items():
        position = _find_position(broker, ticker)
        if position is None:
            continue
        result = ensure_protected(broker, ticker, position, signal,
                                  market_id=policy.market_id,
                                  dry_run=self.is_dry_run,
                                  trade_date=trade_date)
        results[ticker] = result
    return _summarize_results(results)
```

### Phase 3 — Route `sync_market` through `ensure_protected`

`scripts/sync_protective_orders.py` `sync_market` currently calls `broker.sync_all_protective_orders`. After Phase 3:
```python
# sync_protective_orders.py sync_market — simplified
from brokers.protective_orders import ensure_protected

for position in my_market_positions:
    result = ensure_protected(
        broker, position.ticker, position, plan_entry,
        market_id=market_id, dry_run=dry_run,
        pdt_state=pdt_state, trade_date=trade_date,
    )
    # aggregate results for Telegram summary
```

The `_handle_held_stops` logic (Site 7) remains in `sync_protective_orders.py` — it's an operational concern (re-placing HELD orders), not a placement concern. It can be moved to `brokers/protective_orders.py` as `handle_held_stops(broker, market_id, positions, *, state_file=None)` in a later phase.

### Phase 4 — `sync_all_protective_orders` becomes a thin delegator

After Phase 3, `AlpacaBroker.sync_all_protective_orders` iterates positions and calls `ensure_protected` per position. The 870 L of business logic is now in `ensure_protected`. The method becomes ~50 L of glue.

---

## Hard dependencies

| Blocker | Why |
|---------|-----|
| **#2 PR2 must ship first** | `brokers/protective_orders.py` must exist for this candidate to extend it |
| **#5 (broker base)** — Step 3 prerequisite | `ensure_protected` calls `broker.get_open_orders()` which is on `BrokerAdapter`. Without Step 3 of #5, `broker.sync_protective_orders` doesn't exist as an abstract method. This is fine — `ensure_protected` calls lower-level methods (`get_open_orders`, `place_order`, `cancel_order`) that ARE on `BrokerAdapter`. No hard block. |
| **Premarket window** | Phases 2-4 touch live order placement. Do NOT ship during the 23:15–06:00 AEST trading window. |

---

## Testing: matrix coverage

Each row is a test case for `ensure_protected`. Mock `broker.get_open_orders()` and `broker.place_order()` / `broker.cancel_order()`.

| Test | Existing Stop | Existing TP | Plan | Expected action |
|------|---------------|-------------|------|-----------------|
| 1 | None | None | stop + tp | `placed_both` |
| 2 | None | None | stop only | `placed_stop` |
| 3 | None | None | no plan | `no_op` |
| 4 | correct stop | correct tp | same stop + tp | `no_op` |
| 5 | loose stop | correct tp | tighter stop | `tightened_stop` |
| 6 | correct stop | wrong tp | same stop, tighter tp | `tightened_tp` |
| 7 | loose stop | wrong tp | tighter both | `tightened_both` |
| 8 | correct stop | None | stop + tp | `placed_tp` |
| 9 | None | correct tp | stop + tp | `placed_stop` |
| 10 | correct stop | None | stop only | `no_op` |
| 11 | PDT deferred ticker | any | any | `skip_pdt` |
| 12 | cancel_confirm timeout during tightening | — | tighter stop | `error` (cancel timeout) |
| 13 | place_order raises | — | stop + tp | `error` (placement failed) |
| 14 | dry_run=True | None | stop + tp | `placed_both` (no real calls) |
| 15 | stop within 0.3% threshold | correct tp | marginally tighter stop | `no_op` (within threshold) |
| 16 | price already past plan TP | correct stop | stop + expired tp | `no_op` (TP stale) |

Test file: `tests/test_ensure_protected.py` (16+ tests)

### Additional tests
- `test_protective_orders_migration.py`: integration test — calls `place_stops_for_plan` via old interface, verifies it routes through `ensure_protected`
- Extend `tests/test_sync_protective_stuck_held.py`: verify `_handle_held_stops` still works after `sync_market` refactor

---

## Gotchas

1. **Cancel-confirm timeout** — two different defaults exist today: 5.0s in `sync_protective_orders.py` (env `ATLAS_SYNC_PROTECTIVE_CANCEL_TIMEOUT_SEC`) and 10.0s in `broker.py` (env `ATLAS_BROKER_CANCEL_CONFIRM_TIMEOUT_SEC`). `ensure_protected` should read a single env var: `ATLAS_PROTECTIVE_CANCEL_TIMEOUT_SEC` (default 5.0s). Document the rename.

2. **`_apply_db_consistency` dual-pass** — currently in `sync_protective_orders.py` as a separate function. Must move into `ensure_protected` so every call site automatically writes back to SQLite. The dual-pass (live/paper routing) is preserved by checking `_policy.is_paper` or `pass_label`.

3. **`place_stops_for_plan` calls `_is_already_protected`** (commit `ff6f0720`). After migration, `ensure_protected` subsumes this check — it calls `_classify_orders` which finds existing stops and returns `no_op`. The external `_is_already_protected` function in `live_executor.py` can be deleted after Phase 2.

4. **`sync_all_protective_orders` PDT state** — `_pdt_should_skip` reads from `data/pdt_deferred_state.json`. `ensure_protected` accepts `pdt_state: dict | None` to allow callers to load it once and pass it for all tickers (avoids N file reads per sync cycle).

5. **`HELD` orders vs normal orders** — Alpaca `HELD` status means the order is queued but not yet active (pre-market). `_classify_orders` must handle `HELD` status by treating it as `present` (not missing). The separate `_handle_held_stops` path (re-place after 4h hold) remains in `sync_protective_orders.py` as an operational concern, NOT in `ensure_protected`.

6. **Bracket orders placed at entry** — Site 2 (`_execute_entry` atomic bracket) is NOT replaced by this candidate. Bracket orders are placed at order time via `place_order(order_class='bracket')`. `ensure_protected` handles the case where the bracket failed or wasn't requested.

7. **`brokers/protective_orders.py` name vs `db/protective_orders.py`** — see Candidate #3 gotcha 7. They are different layers. Any code that needs both must use fully-qualified imports.

---

## Dependency chain

- **#6 HARD BLOCKED by #2 PR2** — `brokers/protective_orders.py` must exist first
- **#6 soft dependency on #5 Step 3** — `broker.sync_protective_orders` abstract method
- **#4 (reconciliation) does NOT depend on #6** — independent
- **#6 is independent of #3, #9** — no shared modules
- **Timeline note:** Do not ship Phases 2-4 during the premarket window (23:15–06:00 AEST). Only Phase 1 (add `ensure_protected`, write tests, no callers routed) is safe to ship any time.
