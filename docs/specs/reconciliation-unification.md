# Reconciliation Unification (Candidate #4)

**Status:** design sketch — engineering-ready
**Predecessor:** `core/reconcile.py` (shadow run infrastructure, commit `c0fe4f1f`)
**Target:** 3 active reconciliation scripts + 1 executor-embedded path, each with different source-of-truth assumptions and conflict rules
**Goal:** Single `Reconciler` class at `brokers/reconciler.py`; scripts become thin callers; zero conflicting reconcile logic

## Problem

Four independent reconciliation code paths exist today:

| Path | File | What it reconciles | Source of truth | Conflict rule |
|------|------|--------------------|-----------------|---------------|
| Ledger backfill | `scripts/reconcile_ledger.py` (793 L) | Entry fills → `trades` table | Broker fill API → `broker_orders` table → inferred | Skip if ticker already open; unknown strategy → `reconciled` |
| Position drift | `scripts/reconcile_positions.py` (883 L) | Open positions vs `live_*.json` state files | Broker positions | Report only (no auto-fix without `--fix` flag) |
| Order sync | `scripts/sync_broker_orders.py` (340 L) | Alpaca order history → `broker_orders` table | Alpaca API | Upsert on `ON CONFLICT(order_id) DO UPDATE` |
| Executor embedded | `brokers/live_executor.py` `reconcile_entry_fills` / `reconcile_exit_fills` (~514 L) | Fills during live session | Broker fills (15-min window) | EBAY zombie guard, dedup by ticker+date |

`core/reconcile.py` (693 L, shadow mode) exists but is not yet the canonical path — it runs in parallel and sends divergence alerts. This spec describes promoting it.

---

## Pre-investigation: per-path details

### `scripts/reconcile_ledger.py`

Entry point: `reconcile_ledger(market_id, dry_run=False, broker=None, mode_override=None) -> dict`
Dual-pass (live + paper) since commit `a9952764`.
Source of truth priority:
  1. `broker_orders` table (fill_price lookup via `get_broker_fill_price`)
  2. `broker.fill_price` from activities API
  3. `entry_price` from plan (fallback)
Conflict rules:
  - ticker already in `trades` as open → UPDATE protective order IDs, skip re-insert
  - strategy unknown → `reconciled`; stop=0 → write NULL + Telegram WARNING
  - cross-universe tickers: calls `derive_universe(ticker)` (no hint)
Failure modes:
  - SQLite INSERT conflict on `(ticker, universe)` unique index → `ON CONFLICT DO NOTHING`
  - broker API timeout → uses cached `broker_orders` table

### `scripts/reconcile_positions.py`

Entry point: `reconcile_positions(market_id, internal_state, broker_positions, db, dry_run=False) -> dict`
Scoping: loads `universe_tickers | state_file_tickers - other_markets_tickers` (post E2 fix)
Report types: `PHANTOM` (broker has position, Atlas doesn't), `UNTRACKED` (Atlas has, broker doesn't), `MISMATCH` (qty differs), `DRIFT` (price drift)
Conflict rules:
  - `--fix` flag required for writes; defaults to report-only
  - `_fix_phantom` → inserts open trade row + updates state file
  - `_fix_untracked` → closes trade row (zero PnL)
Failure modes:
  - `_STATE_DIR` (module-level constant) must be redirected in tests (added 2026-04-30, commit `01a94810`)
  - Paper pass is report-only (no `--fix` for paper state)

### `scripts/sync_broker_orders.py`

Entry point: `sync_broker_orders(days=90, dry_run=False) -> dict`
Pure mirror: fetches all Alpaca orders for last N days, upserts into `broker_orders` table.
No conflict logic — `ON CONFLICT(order_id) DO UPDATE` replaces every field.
Runs daily at 04:00 UTC (cron). Feeds `reconcile_ledger.py` priority-1 fill price lookup.

### `brokers/live_executor.py` — `reconcile_entry_fills` / `reconcile_exit_fills`

Called from `scripts/sync_protective_orders.py` via `__new__` bypass pattern.
`reconcile_entry_fills` (lines ~1865–2100): scans last 7 days of broker fills; EBAY zombie guard (`_resolve_entry_zombie`); dedup by ticker + date before INSERT.
`reconcile_exit_fills` (lines ~2027–2155): scans broker fills for SELL side; classifies exit reason from `client_order_id`; calls `record_trade_exit` + closes protective record.
**This path is being extracted to `brokers/execution_reconciler.py` by Candidate #2 PR3.** After PR3 ships, `sync_protective_orders.py` will import `reconcile_entry_fills` / `reconcile_exit_fills` directly (no `__new__` bypass). This path MERGES into the unified `Reconciler` as part of this candidate.

---

## Proposed seam: `brokers/reconciler.py`

```python
# brokers/reconciler.py (~500 L)
"""
Unified reconciliation layer.

Three reconcile operations, each idempotent:
  - reconcile_fills:     broker fills → trades table (was reconcile_ledger.py)
  - reconcile_positions: broker positions vs state files (was reconcile_positions.py)
  - reconcile_orders:    Alpaca order history → broker_orders table (was sync_broker_orders.py)

All three accept (market, broker, db, *, dry_run=True) and return a ReconcileReport.
The existing core/reconcile.py becomes the implementation target — promote it from shadow
to canonical by routing all callers through this class.
"""
from __future__ import annotations
import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class ReconcileReport:
    market: str
    operation: str                    # 'fills' | 'positions' | 'orders'
    dry_run: bool
    fills_added: int = 0
    fills_updated: int = 0
    trades_opened: int = 0
    trades_closed: int = 0
    positions_phantom: int = 0        # broker has, Atlas doesn't
    positions_untracked: int = 0      # Atlas has, broker doesn't
    positions_mismatch: int = 0       # qty differs
    orders_upserted: int = 0
    errors: list[str] = field(default_factory=list)


class Reconciler:
    def __init__(self, market: str, broker, db_module=None):
        """
        Args:
            market:     Market ID ('sp500', 'commodity_etfs', etc.)
            broker:     Connected BrokerAdapter instance
            db_module:  db.atlas_db module (defaults to import; injectable for tests)
        """
        self._market = market
        self._broker = broker
        self._db = db_module or _default_db()

    def reconcile_fills(self, *, dry_run: bool = True) -> ReconcileReport:
        """
        Backfill broker fills into trades table.
        Replaces: scripts/reconcile_ledger.reconcile_ledger()
                  brokers/execution_reconciler.reconcile_entry_fills() (after #2 PR3)

        Source-of-truth priority:
          1. broker_orders table (fill_price via get_broker_fill_price)
          2. broker activities API (fill.fill_price)
          3. plan entry_price (fallback)

        Conflict rules (preserve exactly from reconcile_ledger.py):
          - ticker already open in trades → UPDATE protective order IDs, skip INSERT
          - strategy unknown → 'reconciled'
          - stop_price inverted (violates CHECK) → NULL + Telegram WARNING
          - cross-universe: derive_universe(ticker) with no hint
          - paper pass: only when _has_open_paper_trades_for_universe(market) is True
        """
        ...

    def reconcile_positions(self, *, dry_run: bool = True) -> ReconcileReport:
        """
        Detect drift between broker positions and internal state files.
        Replaces: scripts/reconcile_positions.reconcile_positions()

        Report-only by default. When dry_run=False:
          - PHANTOM: insert open trade + update state file
          - UNTRACKED: close trade row (zero PnL)
          - MISMATCH / DRIFT: log only (no auto-fix)

        Scope filter (preserve exactly from reconcile_positions.py post E2 fix):
          universe_tickers | state_file_tickers - other_markets_tickers
        """
        ...

    def reconcile_orders(self, days: int = 90, *, dry_run: bool = True) -> ReconcileReport:
        """
        Mirror Alpaca order history into broker_orders table.
        Replaces: scripts/sync_broker_orders.sync_broker_orders()

        Conflict rule: ON CONFLICT(order_id) DO UPDATE (all fields).
        Pure mirror — no trade table writes.
        """
        ...
```

---

## Migration plan

### Phase 1 — Promote `core/reconcile.py` → `brokers/reconciler.py`

`core/reconcile.py` already implements `reconcile_fills` and `reconcile_positions` as free functions (693 L). The promotion is:

1. Copy `core/reconcile.py` → `brokers/reconciler.py`
2. Wrap the two free functions as methods of `Reconciler` class
3. Add `reconcile_orders` method (extract from `sync_broker_orders.py`, 340 L)
4. Wire `scripts/reconcile_shadow.py` to use the new class (currently it calls both old scripts and `core/reconcile.py` in parallel — drop the old-script path)

### Phase 2 — Route `scripts/reconcile_ledger.py` through `Reconciler`

Replace the body of `reconcile_ledger(market_id, ...)` with:
```python
def reconcile_ledger(market_id: str, dry_run: bool = False, broker=None, mode_override=None) -> dict:
    """Thin wrapper — kept for backward compat with cron callers."""
    from brokers.reconciler import Reconciler
    r = Reconciler(market=market_id, broker=broker or _connect_broker(market_id))
    report = r.reconcile_fills(dry_run=dry_run)
    return _report_to_legacy_dict(report)  # preserve cron caller output shape
```
The cron output dict shape (keyed by `backfilled`, `closed`, `errors`) must remain identical — `_report_to_legacy_dict` converts `ReconcileReport` to that shape.

### Phase 3 — Route `scripts/reconcile_positions.py` through `Reconciler`

Same thin-wrapper pattern. Preserve `--fix` flag semantics via `dry_run=not args.fix`.

### Phase 4 — Route `scripts/sync_broker_orders.py` through `Reconciler`

Replace `sync_broker_orders()` body with `Reconciler(...).reconcile_orders(days=days, dry_run=dry_run)`.

### Phase 5 — Merge executor path (depends on #2 PR3)

After `brokers/execution_reconciler.py` exists (from #2 PR3), fold `reconcile_entry_fills` and `reconcile_exit_fills` into `Reconciler.reconcile_fills`. The executor path's 15-min window and EBAY zombie guard are a strict subset of the ledger backfill path. Merge them into a single implementation with a `window_hours` parameter.

---

## Hard dependencies

| Blocker | Why |
|---------|-----|
| **#5 (broker base promotion)** — soft dependency | `Reconciler.__init__` takes a `BrokerAdapter`. If `get_market_clock` and other helpers exist on `BrokerAdapter` (from #5), the reconciler can call them without checking for `AlpacaBroker`-specific attrs. Without #5, the reconciler must `hasattr`-check or cast. |
| **#2 PR3 (execution_reconciler.py)** — soft dependency | After PR3, executor path is extracted; can merge it into `Reconciler.reconcile_fills`. Without PR3, the `__new__` bypass in `sync_protective_orders.py` must be preserved. |
| **`core/reconcile.py` zero-divergence (7-day window)** — prerequisite | The shadow run has been live since commit `c0fe4f1f`. Check `reconcile_shadow_runs` table: if divergence=0 for 7+ consecutive days, Phase 1 (promote) is safe. If not, investigate divergence before promoting. |

**Ordering recommendation:**
1. Verify `core/reconcile.py` divergence count → if clean, proceed
2. Phase 1 (promote) can ship independently
3. Phases 2-4 can ship in any order after Phase 1
4. Phase 5 blocked on #2 PR3

---

## Existing test files

```
tests/test_reconcile_close_dedup.py           — dedup guard in reconcile_ledger
tests/test_reconcile_commodity_etfs.py        — commodity_etfs market pass
tests/test_reconcile_entry_fills_guard.py     — EBAY zombie guard (executor path)
tests/test_reconcile_exit_fills_none_safety.py — None safety in reconcile_exit_fills
tests/test_reconcile_ledger_backfill_fallback.py — fill price fallback priority
tests/test_reconcile_positions_filter.py      — universe/state scope filter
tests/test_reconcile_positions_state_isolation.py — _STATE_DIR isolation
tests/test_reconcile_preserves_order_ids.py   — order ID preservation
tests/test_reconcile_shadow_timer.py          — shadow alert throttle
tests/test_reconcile_sqlite_orphan_opens.py   — orphan close (scripts/reconcile_sqlite_orphan_opens.py)
tests/test_reconcile_universe_filter.py       — cross-universe exclusion
tests/test_reconciler_no_duplicate_open_position.py — duplicate open position guard
```

### Mapping to new seam

| Existing test | Maps to `Reconciler` method |
|---------------|-----------------------------|
| `test_reconcile_ledger_backfill_fallback.py` | `reconcile_fills` |
| `test_reconcile_entry_fills_guard.py` | `reconcile_fills` (EBAY zombie guard) |
| `test_reconcile_exit_fills_none_safety.py` | `reconcile_fills` (exit side) |
| `test_reconcile_close_dedup.py` | `reconcile_fills` |
| `test_reconcile_positions_filter.py` | `reconcile_positions` |
| `test_reconcile_universe_filter.py` | `reconcile_positions` |
| `test_reconcile_positions_state_isolation.py` | `reconcile_positions` |
| `test_reconcile_preserves_order_ids.py` | `reconcile_fills` |
| `test_reconciler_no_duplicate_open_position.py` | `reconcile_fills` |
| `test_reconcile_shadow_timer.py` | `scripts/reconcile_shadow.py` (orchestrator) |

All existing tests should continue to pass unchanged after Phase 1 (they test the scripts, not `core/reconcile.py`). After each Phase 2-4 migration, run the corresponding test class to confirm the thin-wrapper output matches.

### New tests required for `Reconciler` class
- `tests/test_reconciler_fills.py` — unit tests for `reconcile_fills` via `Reconciler` interface
- `tests/test_reconciler_positions.py` — unit tests for `reconcile_positions`
- `tests/test_reconciler_orders.py` — unit tests for `reconcile_orders`
- Pattern: inject `broker=Mock()`, `db_module=MagicMock()`, verify `ReconcileReport` fields

---

## Gotchas

1. **Cron output shape is a contract.** `pi-cron.sh` checks reconcile output for PHANTOM|UNTRACKED|MISMATCH|DRIFT strings (via `healthz_hourly.sh` hard-gate, commit `21e5f564`). The thin-wrapper `_report_to_legacy_dict` must produce identical string output. Test by capturing before/after with `--dry-run`.

2. **Paper pass dual-routing.** Both `reconcile_ledger.py` and `live_executor.py` have paper pass logic added by commit `a9952764`. `Reconciler.reconcile_fills` must preserve dual-pass behaviour. Gate: `_has_open_paper_trades_for_universe(market)` returns True only when paper_trades is populated.

3. **`core/reconcile.py` is NOT deleted.** It becomes the implementation target. After Phase 1, `core/reconcile.py` is the body of `Reconciler`; the old `reconcile_fills` / `reconcile_positions` free functions are preserved as module-level aliases for any callers that reference them directly (there are none currently, but the shadow runner uses them).

4. **`__new__` bypass in `sync_protective_orders.py`.** The script currently instantiates `LiveExecutor.__new__(LiveExecutor)` to call `reconcile_entry_fills` / `reconcile_exit_fills`. After #2 PR3, this goes away. Until then, Phase 5 (merge executor path) cannot proceed.

5. **`derive_universe` call in `reconcile_fills`.** Must use `derive_universe(ticker)` with NO hint (see FIX-TRADE-UNIV-001 decision, commit `048d7ee5`). Do not pass `market_id` as hint — FCX and other cross-universe tickers get the wrong universe.

---

## Dependency chain

- **Candidate #4 BLOCKED by `core/reconcile.py` divergence check** (must be clean before Phase 1)
- **Candidate #4 Phase 5 BLOCKED by #2 PR3** (executor path extraction)
- **Candidate #5 (broker base)** — soft dependency; proceed without it but add `hasattr` guards
- **Candidate #4 is independent of #3, #6, #9** — can start in parallel with them
