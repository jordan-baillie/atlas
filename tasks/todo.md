# Task: Top 3 Codebase Improvements

## Task 1: Fix 5 Broken Test Collections
- [ ] 1a. Fix `tests.conftest` import issue (3 test files) — create `pytest.ini` with proper rootdir/import config
- [ ] 1b. Fix `test_backtest_parallel.py` — module `scripts.backtest` doesn't exist, skip with clear message
- [ ] 1c. Fix `test_agent_thorough.py` — Playwright needs server, skip when unavailable
- [ ] 1d. Verify all 5 now collect successfully with `pytest --co`

## Task 2: Audit `except: pass` in Critical Code (9 patterns)
- [ ] 2a. `brokers/live_portfolio.py:304` — silent metadata load failure (DANGEROUS)
- [ ] 2b. `scripts/eod_settlement.py:602` — silent telegram crash notify failure
- [ ] 2c. `scripts/execute_approved.py:163,179` — silent telegram notify failures
- [ ] 2d. `scripts/reconcile_positions.py:270,495` — disconnect + telegram
- [ ] 2e. `scripts/sync_protective_orders.py:322,584` — disconnect + telegram
- [ ] 2f. Verify no regressions

## Task 3: Decompose `sync_all_protective_orders` (737 lines → 6 focused functions)
- [ ] 3a. Extract `_fetch_existing_orders()` — scan open orders, build stop/tp maps
- [ ] 3b. Extract `_normalize_plan()` — normalize plan dict to {ticker: entry}
- [ ] 3c. Extract `_resolve_stop_and_tp()` — resolve stop/tp for a single position
- [ ] 3d. Extract `_sync_oco_position()` — Path A (position has take-profit)
- [ ] 3e. Extract `_sync_trailing_position()` — Path B (no TP, use trailing stop)
- [ ] 3f. Slim down `sync_all_protective_orders()` to orchestrator (~80 lines)
- [ ] 3g. Verify existing sync_protective_orders tests still pass

## Review
- [ ] Run full `pytest --co` — zero collection errors
- [ ] Run affected tests — all pass
- [ ] Git commit
