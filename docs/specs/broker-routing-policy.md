# BrokerRoutingPolicy — Design Sketch

**Status:** Approved for engineering. Ships before LiveExecutor decomposition.
**Owner:** Engineering. Review: Planning Lead.
**Replaces:** 5× duplicated routing boilerplate in ops scripts + 3× forked write sites in `brokers/live_executor.py`.

## Why

Five scripts (`reconcile_ledger.py`, `sync_protective_orders.py`, `intraday_monitor.py`, `eod_settlement.py`, `execute_approved.py`) each carry near-identical routing boilerplate: skip-gate, paper-pass detection, paper-config patch, and lifecycle split. Three write sites inside `live_executor.py` fork on `self._mode == "paper"`. The same `_has_open_paper_trades_for_universe` helper is defined verbatim three times. The paper-config dict-patch `{**config, "trading": {**config.get("trading", {}), "mode": "paper"}}` appears five times. The skip-gate diverges between `execute_approved.py` and the other four — a latent bug.

`BrokerRoutingPolicy` consolidates all of this behind one small interface. After migration, callers express *what they want* (`policy.should_skip()`, `policy.needs_paper_pass()`, `policy.split_entries_by_lifecycle(entries)`) and never re-derive routing logic.

Style reference: `regime/model.py` — small public surface, deep behind it, all decisions concentrated.

## Interface

New file: `brokers/routing_policy.py`.

```python
"""brokers/routing_policy.py — Broker-routing policy for a (config, universe) tuple.

Encapsulates every routing decision needed by ops scripts and the live executor:
when to skip a universe, when to run a paper-pass alongside the live pass, how
to derive the paper-mode config, how to split plan entries by lifecycle state,
and which DB tables to target for the current mode.

Replaces 5x duplicated routing boilerplate (reconcile_ledger, sync_protective_orders,
intraday_monitor, eod_settlement, execute_approved) and 3x forked write sites
inside brokers/live_executor.py.

Usage
-----
    from brokers.routing_policy import BrokerRoutingPolicy

    policy = BrokerRoutingPolicy(config, market_id="sp500")

    if policy.should_skip():
        return  # universe disabled

    # Live pass
    run_live(policy)

    # Paper pass (only if there are open paper trades for this universe)
    if policy.needs_paper_pass():
        run_paper(policy.for_paper())
"""
from __future__ import annotations

import functools
import logging
from typing import Iterable

logger = logging.getLogger("atlas.broker_routing")


class BrokerRoutingPolicy:
    """Resolves broker-routing decisions for one (config, universe) tuple.

    Construction is cheap (two dict lookups). DB-backed methods
    (`needs_paper_pass`, `split_entries_by_lifecycle`) lazy-load and memoize.

    A policy is *immutable* once constructed. To switch to paper mode, call
    `for_paper()` — returns a new policy; never mutates the original.

    Parameters
    ----------
    config : dict
        Active config dict (output of `utils.config.get_active_config`).
    market_id : str
        Universe / market identifier (e.g. `"sp500"`, `"commodity_etfs"`).
    """

    def __init__(self, config: dict, market_id: str):
        self.config: dict = config
        self.market_id: str = market_id
        trading = config.get("trading", {})
        self.mode: str = trading.get("mode", "live")
        self.live_enabled: bool = bool(trading.get("live_enabled", False))

    # ── Mode predicates ───────────────────────────────────────────────────

    @property
    def is_paper(self) -> bool:
        """True if `trading.mode == "paper"`."""
        return self.mode == "paper"

    @property
    def is_live(self) -> bool:
        """True if `trading.mode == "live"`."""
        return self.mode == "live"

    @property
    def is_passive(self) -> bool:
        """True if `trading.mode == "passive"`."""
        return self.mode == "passive"

    # ── Decisions ─────────────────────────────────────────────────────────

    def should_skip(self) -> bool:
        """True if this universe is disabled and ops should bail out.

        A universe is skipped when:
          - mode == "passive", OR
          - mode == "live" AND live_enabled is False (safety gate).

        Paper mode runs regardless of `live_enabled` (paper account, no
        real capital at risk).
        """
        if self.is_passive:
            return True
        if self.is_live and not self.live_enabled:
            return True
        return False

    @functools.cached_property
    def _has_open_paper_trades(self) -> bool:
        """DB-backed: any open paper trade for this universe? Memoized."""
        try:
            from db.atlas_db import get_open_paper_trades
            return any(r.get("universe") == self.market_id for r in get_open_paper_trades())
        except Exception as exc:  # noqa: BLE001 — DB read; non-fatal
            logger.debug("needs_paper_pass DB error (non-fatal): %s", exc)
            return False

    def needs_paper_pass(self) -> bool:
        """True if a paper-mode pass should run *in addition* to the live pass.

        Returns True iff:
          - this is NOT already a paper-mode policy (`is_paper == False`), AND
          - there is at least one open paper trade for this universe in the DB.

        Result is memoized for the policy's lifetime.
        """
        if self.is_paper:
            return False
        return self._has_open_paper_trades

    # ── Derived configs / policies ────────────────────────────────────────

    @property
    def paper_config(self) -> dict:
        """The paper-mode config patch.

        Equivalent to `{**self.config, "trading": {**trading, "mode": "paper"}}`.
        Used by callers that need to construct paper executors / brokers
        directly (e.g. `LivePortfolio(policy.paper_config, market_id=...)`).
        """
        trading = self.config.get("trading", {})
        return {**self.config, "trading": {**trading, "mode": "paper"}}

    def for_paper(self) -> "BrokerRoutingPolicy":
        """Return a derived policy with mode forced to `"paper"`.

        Used between live and paper passes:

            run_live(policy)
            if policy.needs_paper_pass():
                run_paper(policy.for_paper())
        """
        return BrokerRoutingPolicy(self.paper_config, self.market_id)

    # ── Per-entry routing ─────────────────────────────────────────────────

    def split_entries_by_lifecycle(self, entries: Iterable[dict]) -> tuple[list, list]:
        """Split entry/exit records into `(live, paper)` by strategy lifecycle.

        Entries whose `strategy` is in PAPER lifecycle state for this universe
        route to paper. All others (LIVE / RESEARCH / RETIRED / missing
        strategy) route to live.

        On import failure of `monitor.strategy_lifecycle`, returns
        `(list(entries), [])` — safe fallback (everything stays live).

        Delegates to `monitor.strategy_lifecycle.split_trades_by_lifecycle`,
        which is the canonical implementation. This method exists so callers
        don't need to know the canonical location.
        """
        try:
            from monitor.strategy_lifecycle import split_trades_by_lifecycle
        except ImportError as exc:
            logger.warning(
                "split_entries_by_lifecycle: strategy_lifecycle import failed (%s) — "
                "routing all entries to live (safe fallback)",
                exc,
            )
            return list(entries), []
        return split_trades_by_lifecycle(list(entries), self.market_id)

    # ── DB target resolution ──────────────────────────────────────────────

    def trade_table(self) -> str:
        """Return the trades table name for the current mode.

        `"paper_trades"` if paper, `"trades"` otherwise.
        """
        return "paper_trades" if self.is_paper else "trades"

    def protective_table(self) -> str:
        """Return the position_protective_orders table name for the current mode."""
        return "paper_position_protective_orders" if self.is_paper else "position_protective_orders"
```

### Notes on the interface

- **No `route_db_write(strategy, side)`.** By the time `live_executor` reaches a write site, `self._mode` is already fixed (the executor instance is paper or live, decided at construction). Per-strategy routing happens earlier — in `execute_approved.py`'s lifecycle split, before two LiveExecutor instances are spawned. Adding `route_db_write` would force redundant args at every call site. `policy.is_paper` + `policy.trade_table()` is sufficient.
- **`needs_paper_pass()` is memoized.** Multiple calls in a single script run hit the DB once. `functools.cached_property` on `_has_open_paper_trades`.
- **`for_paper()` returns a new policy.** Immutable transition. Avoids state-bleed between live and paper passes.
- **`paper_config` is a property, not a method.** It's a pure derivation, no side effects, no args.

## Migration plan

| File | Lines | Change |
|------|-------|--------|
| `scripts/execute_approved.py` | 60–96 | DELETE local `_split_by_lifecycle` |
| `scripts/execute_approved.py` | ~196 (after config load) | INSERT `policy = BrokerRoutingPolicy(config, market_id)`; replace `if mode == "passive": return` with `if policy.should_skip(): return` |
| `scripts/execute_approved.py` | 348–349 | Replace `_split_by_lifecycle(entries, market_id)` → `policy.split_entries_by_lifecycle(entries)` (same for exits) |
| `scripts/execute_approved.py` | ~365 | Replace inline `paper_config = {**config, "trading": {...}}` → `paper_config = policy.paper_config` |
| `scripts/reconcile_ledger.py` | 80–87 | Replace inline `config = {**_rl_config, "trading": {...}}` → `policy = BrokerRoutingPolicy(_rl_config, market_id); policy = policy.for_paper() if mode_override == "paper" else policy; config = policy.config` |
| `scripts/reconcile_ledger.py` | main, ~720 | Replace `_paper_open = [r for r in _gopt() ...]; if _paper_open:` → `if BrokerRoutingPolicy(load_config(args.market), args.market).needs_paper_pass():` |
| `scripts/sync_protective_orders.py` | 89–99 | DELETE local `_has_open_paper_trades_for_universe` |
| `scripts/sync_protective_orders.py` | ~1100 (sync_market) | INSERT `policy = BrokerRoutingPolicy(config, market_id)`; replace `if not (live_enabled or _mode == "paper")` → `if policy.should_skip()` |
| `scripts/sync_protective_orders.py` | `_run_paper_sync_pass` | Replace inline `paper_config = {**base_config, ...}` → `paper_config = policy.paper_config` (pass `policy` in instead of `base_config`) |
| `scripts/intraday_monitor.py` | 349–356 | DELETE local `_has_open_paper_trades_for_universe` |
| `scripts/intraday_monitor.py` | 380–386 | Replace skip gate → `policy = BrokerRoutingPolicy(config, market_id); if policy.should_skip(): return` |
| `scripts/intraday_monitor.py` | 405, 473, 530 | Replace `_has_open_paper_trades_for_universe(market_id)` → `policy.needs_paper_pass()` |
| `scripts/intraday_monitor.py` | 533 | Replace `paper_config = {**config, ...}` → `policy.paper_config` |
| `scripts/eod_settlement.py` | 1006–1015 | DELETE local `_has_open_paper_trades_for_universe` |
| `scripts/eod_settlement.py` | 540–546 | Replace skip gate → `policy = BrokerRoutingPolicy(config, market_id); if policy.should_skip(): return` |
| `scripts/eod_settlement.py` | 963 | Replace `_has_open_paper_trades_for_universe(market_id)` → `policy.needs_paper_pass()` |
| `scripts/eod_settlement.py` | 965 | Replace `_paper_config_eod = {**config, ...}` → `policy.paper_config` |
| `brokers/live_executor.py` | 265 | Append after `self._mode = ...`: `self._policy = BrokerRoutingPolicy(config, market_id=config.get("market_id", "sp500"))` |
| `brokers/live_executor.py` | 1305 | Replace `if self._mode == "paper":` → `if self._policy.is_paper:` |
| `brokers/live_executor.py` | 1716 | Replace `if self._mode == "paper":` → `if self._policy.is_paper:` |
| `brokers/live_executor.py` | 2830 | Replace `if self._mode == "paper":` → `if self._policy.is_paper:` |
| `brokers/live_executor.py` | 2777 | Replace `_dedup_table = "paper_trades" if self._mode == "paper" else "trades"` → `_dedup_table = self._policy.trade_table()` |

**Imports to add** (each script + `live_executor.py`): `from brokers.routing_policy import BrokerRoutingPolicy`.

**Imports to remove**: none (nothing else depends on the local `_has_open_paper_trades_for_universe` helpers; `_split_by_lifecycle` in `execute_approved.py` was only called twice in the same file).

**Out of scope for this migration**: the `mode_override` parameter on `reconcile_ledger.reconcile_ledger()`. Keep the parameter — the function is also called by tests. Inside the function, the policy replaces the inline config patch; the parameter remains a valid input.

## Test strategy

### New unit tests — `tests/test_broker_routing_policy.py`

| # | Test | Expectation |
|---|------|-------------|
| 1 | `test_should_skip_passive` | `mode=passive` → `True` |
| 2 | `test_should_skip_live_disabled` | `mode=live, live_enabled=False` → `True` |
| 3 | `test_should_skip_live_enabled` | `mode=live, live_enabled=True` → `False` |
| 4 | `test_should_skip_paper_no_live_enabled` | `mode=paper, live_enabled=False` → `False` (paper runs without live_enabled) |
| 5 | `test_should_skip_paper_with_live_enabled` | `mode=paper, live_enabled=True` → `False` |
| 6 | `test_needs_paper_pass_already_paper` | `mode=paper`, paper trades exist → `False` |
| 7 | `test_needs_paper_pass_no_open_paper_trades` | `mode=live`, DB empty → `False` |
| 8 | `test_needs_paper_pass_has_open_paper_trades` | `mode=live`, DB has paper trade for `sp500` → `True` |
| 9 | `test_needs_paper_pass_other_universe` | `mode=live`, DB has paper trade for `commodity_etfs` only, policy for `sp500` → `False` |
| 10 | `test_needs_paper_pass_db_error_non_fatal` | DB raises → `False` (warning logged, no exception) |
| 11 | `test_needs_paper_pass_memoized` | Two consecutive calls → DB hit once (use mock counter) |
| 12 | `test_paper_config_patches_mode` | `paper_config["trading"]["mode"] == "paper"` |
| 13 | `test_paper_config_preserves_other_trading_keys` | `live_enabled, broker, live_safety` carried through |
| 14 | `test_paper_config_does_not_mutate_original` | `policy.config["trading"]["mode"]` unchanged after `policy.paper_config` |
| 15 | `test_for_paper_returns_new_policy` | `policy.for_paper().is_paper == True`; original `policy.is_paper` unchanged |
| 16 | `test_for_paper_preserves_market_id` | `policy.for_paper().market_id == policy.market_id` |
| 17 | `test_split_entries_all_live` | All strategies in LIVE state → `(entries, [])` |
| 18 | `test_split_entries_mixed_lifecycle` | One PAPER, one LIVE → split as expected |
| 19 | `test_split_entries_unknown_strategy` | Missing/empty `strategy` key → routed to live |
| 20 | `test_split_entries_empty_input` | `[]` → `([], [])` |
| 21 | `test_split_entries_import_failure_routes_all_to_live` | Mock `monitor.strategy_lifecycle` import to raise → all to live |
| 22 | `test_trade_table_paper` | `is_paper` → `"paper_trades"` |
| 23 | `test_trade_table_live` | `is_live` → `"trades"` |
| 24 | `test_protective_table_paper` | → `"paper_position_protective_orders"` |
| 25 | `test_protective_table_live` | → `"position_protective_orders"` |

### Existing tests — must still pass

- `tests/test_strategy_lifecycle.py` — unchanged. Tests the canonical `split_trades_by_lifecycle`.
- `tests/test_ops_scripts_lifecycle_routing.py` — unchanged. Tests `monitor.strategy_lifecycle.split_trades_by_lifecycle` integration with ops-script call patterns.
- `tests/test_execute_approved_paper_routing.py` — **must be updated**: tests reference `mod._split_by_lifecycle`, which is being deleted. Replace with `policy.split_entries_by_lifecycle(...)` calls. Behavior is identical (same canonical splitter underneath); the 9 existing test cases all map 1:1.

### Acceptance criteria

1. All 25 new policy tests pass.
2. All existing routing tests pass (after the `test_execute_approved_paper_routing.py` rename).
3. Full pytest suite green: `pytest tests/ --timeout=30`.
4. Smoke test: `python3 scripts/execute_approved.py --market sp500 --dry-run` produces the same lifecycle-split log output as before migration.
5. Smoke test: `python3 scripts/sync_protective_orders.py --market sp500 --dry-run` runs both passes when paper trades exist, single pass otherwise.

## Decision points (recommendations)

1. **`execute_approved.py` skip-gate divergence — normalize to which behaviour?**
   - **Recommendation: STRICTER (the 4-script behaviour).** `should_skip` returns True if `mode == passive` OR (`mode == live` AND `live_enabled == False`). Rationale: `live_enabled = False` is an operator-flippable safety gate; `execute_approved` ignoring it is a latent bug — if an operator disables live trading mid-day with `mode` still set to `"live"`, the other scripts correctly defer but `execute_approved` would still try to fire orders. Migration: this changes `execute_approved` behaviour — guard with a one-time check (does any production config have `mode=live, live_enabled=False`? Probably not, but verify before merge).

2. **Cache the policy per script run, or instantiate per-call?**
   - **Recommendation: INSTANTIATE PER-CALL.** Construction is two dict lookups — sub-microsecond. The DB-backed `needs_paper_pass()` is memoized via `cached_property`, so multiple calls within a single policy instance hit DB once. If a script wants the cache to span the whole run, it just keeps the same instance (the natural pattern).

3. **`route_db_write(strategy, side) -> Literal["live", "paper"]` (per audit) — include or drop?**
   - **Recommendation: DROP.** The audit listed it but the call sites don't need per-strategy routing at the write layer — the executor instance's mode is fixed, and per-strategy routing is resolved upstream by `split_entries_by_lifecycle`. `policy.is_paper` + `policy.trade_table()` covers every actual call site.

4. **Add the term "broker routing policy" to `docs/architecture/strategy-lifecycle.md` now or after engineering ships?**
   - **Recommendation: AFTER.** The interface might shift slightly during implementation (e.g. the `mode_override` interaction in `reconcile_ledger`). Update `docs/architecture/strategy-lifecycle.md` in the same PR as the migration so docs and code land together.

5. **Mutability — should `for_paper()` mutate or return new?**
   - **Recommendation: RETURN NEW.** Already specified above. Avoids state-bleed between live and paper passes; `policy.config` and `policy.paper_config` can both be inspected in the same scope without confusion.

6. **`needs_paper_pass()` memoization granularity — per-instance, per-process, or per-call?**
   - **Recommendation: PER-INSTANCE** (via `functools.cached_property`). Per-process would require a global cache that doesn't invalidate between markets; per-call (no memo) would re-query the DB up to 4× per script run (intraday_monitor calls it 3×, eod_settlement 1×). Per-instance is the natural midpoint.

## Out of scope

- Decomposing `live_executor.py` itself (#2 — comes after this ships).
- Any change to `monitor.strategy_lifecycle.split_trades_by_lifecycle` — the canonical splitter stays put. The policy just delegates.
- Migrating `LivePortfolio` to take a policy. (`LivePortfolio` reads mode from config the same way; could be tightened later but no immediate boilerplate to consolidate.)
- Removing the `mode_override` parameter from `reconcile_ledger.reconcile_ledger()` — it stays for test ergonomics; only the *implementation* uses the policy.

## Glossary

- **Broker routing policy** — the (mode, live_enabled, market_id, lifecycle) decisions that route execution and DB writes between live and paper paths.
- **Live pass** — execution against the real-money broker (`mode=live`).
- **Paper pass** — execution against the Alpaca paper account (`mode=paper`), used for PAPER-lifecycle strategies running alongside LIVE strategies in the same universe.
- **Lifecycle split** — partitioning plan entries by the originating strategy's promotion state (PAPER → paper executor; LIVE/RESEARCH/RETIRED/unknown → live executor).
- **Skip gate** — the universe-level check that bails out before any broker connection (`mode=passive` or `mode=live AND live_enabled=False`).
