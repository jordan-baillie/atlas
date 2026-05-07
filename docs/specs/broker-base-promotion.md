# Broker Base Promotion (Candidate #5)

**Status:** design sketch — engineering-ready
**Target:** `brokers/base.py` (309 L) + `brokers/alpaca/broker.py` (2,119 L)
**Goal:** Promote 3 Alpaca-specific capabilities to `BrokerAdapter` ABC so ops scripts can call them on any broker instance without casting to `AlpacaBroker`

## Problem

Three groups of broker operations are currently implemented only on `AlpacaBroker`:

1. **Market clock / open-hours check** — `broker._broker_call(broker._trade_client.get_clock)` is called in at least 3 scripts (`services/api/dashboard.py`, `scripts/consolidation_close_positions.py`, `scripts/sync_protective_orders.py` indirectly). Each caller has bespoke `hasattr` / `_broker_call` wrappers. `scripts/consolidation_close_positions.py` even defines a private `_broker_is_open(broker)` helper to paper over the missing abstraction.

2. **Rich order history** — `get_history_orders(days)` and `get_history_deals(days)` exist on `BrokerAdapter` as default no-ops (return `[]`). They are meaningfully implemented only on `AlpacaBroker`. Callers in `brokers/execution_analytics.py` and `scripts/sync_broker_orders.py` use them via the abstract interface but the contract is informal.

3. **`sync_all_protective_orders` business logic** — 870 L of OCO/stop/trailing-stop sync logic lives in `AlpacaBroker.sync_all_protective_orders` (lines 846–1715 of `broker.py`). `scripts/sync_protective_orders.py` calls it via `broker.sync_all_protective_orders(positions, plan)`. This is broker-agnostic business logic that happens to be implemented in the Alpaca class.

---

## Pre-investigation: methods to promote

### `brokers/base.py` current state (309 L)

`BrokerAdapter` ABC defines 18 abstract methods + 8 concrete defaults. Key concrete defaults that are already "promoted":
- `get_prices` — returns `{}` (override in Alpaca to use Tiingo)
- `get_history_orders` — returns `[]`
- `get_history_deals` — returns `[]`
- `get_today_deals` — returns `[]`
- `get_order_fees` — returns `[]`
- `get_market_states` — returns `[]`
- `get_trading_days` — returns `[]`
- `get_slippage_report` — returns `[]`

Missing from `BrokerAdapter` (exists only on `AlpacaBroker`):
- `get_market_snapshot(ticker) -> Optional[dict]` (line 1766 of broker.py)
- `sync_all_protective_orders(positions, plan, *, trade_date, dry_run) -> dict` (line 846)
- No clock/open-hours method at all (callers reach into `_trade_client.get_clock`)

### Market clock callers (need `is_market_open() -> bool`)

```python
# scripts/consolidation_close_positions.py lines 77–93
def _broker_is_open(broker) -> bool:
    try:
        if hasattr(broker, "get_clock") and callable(broker.get_clock):
            clock = broker.get_clock()
            if hasattr(clock, "is_open"):
                return bool(clock.is_open)
    except Exception:
        pass
    try:
        clock = broker._broker_call(broker._trade_client.get_clock)
        return bool(clock.is_open)
    except Exception:
        return False

# services/api/dashboard.py lines 338, 551
broker._broker_call(broker._trade_client.get_clock)
```

### `sync_all_protective_orders` callers

```python
# scripts/sync_protective_orders.py line 1339
sync_result = broker.sync_all_protective_orders(
    positions=my_market_positions,
    plan=plan,
    trade_date=trade_date,
    dry_run=dry_run,
)
```

This is the *only* external caller of `sync_all_protective_orders`. After Candidate #6 ships, the call site moves to `brokers/protective_orders.ensure_protected()`. The 870 L of OCO logic inside `AlpacaBroker.sync_all_protective_orders` is then decomposed into the `brokers/protective_orders.py` module (see Candidate #6 spec). At that point, the method on `AlpacaBroker` becomes a thin delegation.

---

## Proposed promotions to `BrokerAdapter`

### Promotion 1 — `is_market_open() -> bool`

**Add to `BrokerAdapter` as concrete default (not abstract):**

```python
# brokers/base.py
def is_market_open(self) -> bool:
    """Return True if the primary exchange is currently in regular trading hours.

    Default implementation returns False (conservative — safe for paper/backtest).
    Override in live broker implementations.
    """
    return False
```

**Implement on `AlpacaBroker`:**

```python
# brokers/alpaca/broker.py
def is_market_open(self) -> bool:
    """Query Alpaca clock for market open status."""
    try:
        clock = self._broker_call(self._trade_client.get_clock)
        return bool(clock.is_open)
    except Exception as e:
        logger.warning("is_market_open: clock query failed: %s", e)
        return False
```

**Migrate callers:**
- `scripts/consolidation_close_positions.py`: replace `_broker_is_open(broker)` with `broker.is_market_open()`
- `services/api/dashboard.py`: replace both `_broker_call(...get_clock)` sites with `broker.is_market_open()`
- Delete `_broker_is_open` helper in `consolidation_close_positions.py` (12 L)

Risk: LOW. Read-only. Default `False` is safe (conservative — blocks trading when unknown).

### Promotion 2 — `get_market_snapshot(ticker: str) -> dict | None`

Already exists on `AlpacaBroker` (line 1766). Needs to be declared on `BrokerAdapter`:

```python
# brokers/base.py
def get_market_snapshot(self, ticker: str) -> dict | None:
    """Get latest quote/trade snapshot for a ticker.

    Returns dict with keys: ask_price, bid_price, last_price, volume, timestamp.
    Returns None if unavailable.

    Default: None (not implemented in base).
    """
    return None
```

No change to `AlpacaBroker.get_market_snapshot` — it already implements the right signature.
Risk: LOW. Read-only. Adds to ABC without breaking existing code.

### Promotion 3 — `sync_protective_orders(positions, plan, *, trade_date, dry_run) -> dict`

**Declare on `BrokerAdapter` as concrete default (not abstract — paper/backtest brokers may not need it):**

```python
# brokers/base.py
def sync_protective_orders(
    self,
    positions: list,
    plan: dict | None = None,
    *,
    trade_date: str = "",
    dry_run: bool = False,
) -> dict:
    """Sync stop-loss and take-profit orders for all open positions.

    Returns summary dict: sl_placed, sl_already_exists, tp_placed, tp_already_exists,
    errors, pdt_deferred, per_ticker.

    Default implementation: no-op (returns zeroed summary).
    Override in live broker implementations.
    """
    return {"sl_placed": 0, "sl_already_exists": 0, "tp_placed": 0,
            "tp_already_exists": 0, "errors": 0, "pdt_deferred": 0, "per_ticker": {}}
```

On `AlpacaBroker`: rename `sync_all_protective_orders` → `sync_protective_orders` (the `_all_` prefix is redundant) OR keep the old name and add `sync_protective_orders` as an alias:

```python
# brokers/alpaca/broker.py — option A (alias, zero risk)
def sync_protective_orders(self, positions, plan=None, *, trade_date="", dry_run=False) -> dict:
    """Delegate to sync_all_protective_orders (Candidate #6 will merge them)."""
    return self.sync_all_protective_orders(positions, plan, trade_date=trade_date, dry_run=dry_run)
```

After Candidate #6 ships, `sync_all_protective_orders` is decomposed; `sync_protective_orders` becomes the thin entry point calling `brokers.protective_orders.ensure_protected` per position.

Risk: MEDIUM. Adds a new method name. Callers currently use `sync_all_protective_orders` — migrate them to `sync_protective_orders` only after the alias is in place (no breaking change).

---

## What stays on `AlpacaBroker` only

These methods are Alpaca-specific with no general abstraction value:

- `_broker_call(func, *args, **kwargs)` — Alpaca retry wrapper; not an ABC concern
- `_wait_for_cancel_confirmed(order_id, timeout_s, poll_interval_s)` — Alpaca OCO cancel polling; too protocol-specific
- `account_number`, `mode`, `market_id` — Alpaca-specific account metadata
- `verify_shorting_enabled()` — Alpaca account feature check
- `get_market_snapshot` — declared on base above; implementation is Alpaca-specific

---

## Migration order (lowest risk first)

### Step 1 — `is_market_open` (read-only, no order flow)
1. Add `is_market_open() -> bool` to `BrokerAdapter` (concrete default `False`)
2. Implement on `AlpacaBroker`
3. Migrate `consolidation_close_positions.py` and `services/api/dashboard.py`
4. Run: `pytest tests/ -x --timeout=30` — verify zero failures
5. Commit: `feat: promote is_market_open to BrokerAdapter`

### Step 2 — `get_market_snapshot` (declare on ABC only)
1. Add `get_market_snapshot(ticker) -> dict | None` to `BrokerAdapter` (concrete default `None`)
2. No change to `AlpacaBroker` (already implements it)
3. Run: `pytest tests/ -x --timeout=30`
4. Commit: `feat: declare get_market_snapshot on BrokerAdapter`

### Step 3 — `sync_protective_orders` alias (MED risk — involves live order placement path)
1. Add `sync_protective_orders(...)` concrete default to `BrokerAdapter`
2. Add alias method on `AlpacaBroker` (delegates to `sync_all_protective_orders`)
3. Update `scripts/sync_protective_orders.py` call site to use `broker.sync_protective_orders(...)` instead of `broker.sync_all_protective_orders(...)`
4. Run full suite + confirm `tests/test_cron_idempotency.py::test_sync_protective_orders_dry_run_idempotent` passes
5. Commit: `feat: promote sync_protective_orders to BrokerAdapter`

**Do NOT do Step 3 before the premarket window.** This touches the live order sync path.

---

## Backward compatibility: the `__new__` bypass pattern

5 ops scripts instantiate `LiveExecutor` via `LiveExecutor.__new__(LiveExecutor)` and manually set `_broker`, `_connected`, `_mode`, `config`. The promoted methods are on `BrokerAdapter` — they are accessed via `self._broker` in `LiveExecutor`. The bypass pattern sets `self._broker` to a real `AlpacaBroker` instance. After promotion, calling `self._broker.is_market_open()` works because `AlpacaBroker` inherits from `BrokerAdapter` and overrides it.

**Preserve exactly:** `_broker`, `_connected`, `_mode`, `config` attribute names on `LiveExecutor`. Candidate #2 spec has these as de-facto public attributes.

---

## Paper broker stub requirement

There is no `PaperBroker` class today (the paper execution path uses `AlpacaBroker` with `mode='paper'` and a paper account). If a true paper broker is added in the future, it needs stubs:

```python
class PaperBroker(BrokerAdapter):
    def is_market_open(self) -> bool:
        return True  # paper market is always "open"

    def sync_protective_orders(self, positions, plan=None, *, trade_date="", dry_run=False) -> dict:
        return {"sl_placed": 0, "sl_already_exists": 0, "tp_placed": 0,
                "tp_already_exists": 0, "errors": 0, "pdt_deferred": 0, "per_ticker": {}}
```

For now, since paper uses `AlpacaBroker`, no stub is needed.

---

## Testing

### Tests to write per step

**Step 1 — `is_market_open`:**
```
tests/brokers/test_broker_base_is_market_open.py
  - test_default_returns_false: BrokerAdapter() subclass with no override returns False
  - test_alpaca_calls_get_clock: AlpacaBroker.__new__ bypass + mock _trade_client.get_clock
  - test_alpaca_returns_false_on_exception: clock call raises → False returned, warning logged
  - test_consolidation_close_uses_broker_method: patch broker.is_market_open, verify no direct _trade_client access
```

**Step 2 — `get_market_snapshot`:**
```
tests/brokers/test_broker_base_market_snapshot.py
  - test_default_returns_none: base class returns None
  - test_alpaca_returns_dict: AlpacaBroker returns dict with ask_price/bid_price/last_price
  - test_alpaca_logs_error_on_failure: SDK call raises → None returned
```

**Step 3 — `sync_protective_orders`:**
```
tests/brokers/test_broker_sync_protective_promotion.py
  - test_default_returns_zeroed_summary: base class returns correct zero-value dict
  - test_alpaca_alias_delegates: AlpacaBroker.sync_protective_orders calls sync_all_protective_orders
  - test_sync_protective_script_uses_method: scripts/sync_protective_orders.py call site uses method not attribute
```

### Existing tests that cover the call sites being migrated
- `tests/test_consolidation_close_positions.py` (20 tests) — includes clock mock; update to use `broker.is_market_open()` mock
- `tests/test_cron_idempotency.py::test_sync_protective_orders_dry_run_idempotent` — covers sync_protective_orders call

---

## Gotchas

1. **`sync_all_protective_orders` is 870 L.** Do NOT move it to `BrokerAdapter`. Add an alias (`sync_protective_orders`) that delegates. The actual 870 L decomposition is Candidate #6 work.

2. **`_broker_call` is not on `BrokerAdapter`.** Some callers reach `broker._broker_call(fn)` directly. This is an implementation detail of `AlpacaBroker`. After promotion, callers use the method (`is_market_open`, etc.) not `_broker_call`. Never add `_broker_call` to `BrokerAdapter`.

3. **`get_market_snapshot` key contract.** Dashboard code (`services/api/dashboard.py`) and `brokers/alpaca/broker.py` both read specific keys from the snapshot dict. The base-class docstring should enumerate the required keys: `ask_price`, `bid_price`, `last_price`, `volume`, `timestamp`. Add this to the docstring, not to the type system (avoid breaking callers that add extra keys).

4. **Alpaca SDK `get_clock` return type.** The `clock` object from `_trade_client.get_clock()` is an SDK-specific type. `is_market_open` must access only `clock.is_open` (a bool) — do not expose the raw clock object through the abstract method.

5. **Step 3 must not ship before premarket window.** The alias change in `sync_protective_orders.py` touches the live order sync path. Schedule Step 3 after the 23:30 AEST trading window and before the 09:05 AEST reconcile window.

---

## Dependency chain

- **#5 is independent of all other candidates** — can start at any time
- **#4 (reconciliation unification) has soft dependency on #5** — without promoted methods, `Reconciler` needs `hasattr` guards
- **#6 (protective orders unification) has hard dependency on #5** — `ensure_protected` calls `broker.sync_protective_orders` via the promoted interface
- **Step 3 (sync_protective_orders alias) is soft-blocked by #2 PR2** — ideally after `brokers/protective_orders.py` exists, but alias can ship independently
