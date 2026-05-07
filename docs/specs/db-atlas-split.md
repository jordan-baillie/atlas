# `db/atlas_db.py` Domain Split (Candidate #3)

**Status:** design sketch — engineering-ready
**Target:** `db/atlas_db.py` — 3,520 L, 91 public functions, 419 import sites across the codebase
**Goal:** Split into 9-10 focused domain modules; keep `atlas_db.py` as a re-export shim. Zero callers break.

## Problem

`db/atlas_db.py` is the largest file in the codebase (3,520 L). It mixes ~15 distinct domains in a single flat module:
- Any change anywhere requires reading the entire file for context
- Unrelated test failures when one domain's schema helpers change
- Module-level globals (`_db_path_override`, `_state_dir_override`, `_wal_initialized_paths`, `_risk_cache_tables_ensured`) that affect all domains simultaneously
- 419 import sites all pointing to one file — impossible to know what a module actually depends on

---

## Pre-investigation: function catalog by domain

All 91 public functions catalogued. Line numbers are in current `db/atlas_db.py`.

### Domain A — Connection & Schema (lines 1–100, ~100 L)
Functions: `get_db`, `init_db`, `_group_performance`, `_assert_state_file_parity`
Globals: `DB_PATH`, `_db_path_override`, `_state_dir_override`, `_wal_initialized_paths`
**Must stay in `db/atlas_db.py`** — every sub-module imports `get_db` from here.

### Domain B — Trades (lines 279–638, ~500 L)
Functions: `record_trade_entry`, `update_trade_protective_orders`, `_compute_and_fill_mae_mfe`,
`record_trade_exit`, `get_open_positions`, `get_closed_trades`, `performance_summary`
Import sites (~25): `brokers/live_executor.py`, `scripts/eod_settlement.py`,
`scripts/reconcile_ledger.py`, `scripts/reconcile_positions.py`, `monitor/strategy_health.py`,
`services/api/portfolio.py`, `services/api/dashboard.py`

### Domain C — Regime (lines 639–732, ~120 L)
Functions: `record_regime`, `get_current_regime`, `get_current_regime_state`, `get_regime_history`
Import sites (~20): `regime/engine.py`, `brokers/plan.py`, `overlay/engine.py`,
`services/api/dashboard.py`, `research/loop.py`, `scripts/check_regime_features_staleness.py`

### Domain D — OHLCV (lines 733–850, ~110 L)
Functions: `upsert_ohlcv`, `get_ohlcv`, `get_universe_data`
Import sites (~40): `data/ingest.py`, `strategies/*.py`, `backtest/engine.py`, `research/loop.py`

### Domain E — Signals & Plans (lines 851–1107, ~300 L)
Functions: `record_signal`, `get_signals`, `_validate_plan_date`, `record_plan`, `get_plan`,
`get_plans`, `update_plan_status`, `update_plan`, `_decode_plan`
Import sites (~18): `brokers/plan.py`, `brokers/live_executor.py`, `services/api/approvals.py`,
`scripts/execute_approved.py`

### Domain F — Equity & Snapshots (lines 1108–1377, ~280 L)
Functions: `record_equity`, `get_equity_curve`, `get_latest_equity`, `record_position_snapshots`,
`get_position_snapshots`, `record_snapshot`, `record_all_markets_snapshot`, `get_latest_snapshot`,
`get_snapshots`, `_decode_snapshot`
Import sites (~18): `brokers/live_portfolio.py`, `services/api/dashboard.py`,
`services/api/portfolio.py`, `services/api/health.py`, `services/telegram_bot.py`

### Domain G — Overlay & News (lines 1378–1770, ~300 L)
Functions: `record_overlay_decision`, `get_overlay_decisions`, `update_overlay_outcome`,
`insert_overlay_shadow_event`, `get_unevaluated_shadow_events`, `update_shadow_outcome`,
`get_shadow_events`, `upsert_ceasefire_factor`, `get_ceasefire_factors`,
`record_ceasefire_history`, `get_ceasefire_history`, `record_news`, `get_news`
Import sites (~10): `overlay/engine.py`, `overlay/sources/alt_data.py`,
`overlay/sources/news.py`, `services/api/portfolio.py`

### Domain H — Research (lines 1770–2036, ~500 L)
Functions: `record_experiment`, `get_experiments`, `update_experiment_status`,
`upsert_research_best`, `get_research_best`
Import sites (~30): `research/loop.py`, `research/sweep.py`, `research/autoresearch_nightly.py`,
`research/autoresearch_runner.py`, `research/freshness.py`,
`scripts/auto_promote_paper_to_live.py`, `services/api/research.py`
**Note:** pairs with Candidate #9 (`research/db.py` consolidation) — do together in Session 2.

### Domain I — Monitoring (lines 2037–2142, ~70 L)
Functions: `record_heartbeat`, `get_heartbeats`, `record_system_log`, `get_system_logs`
Import sites (~35): `monitor/health_writer.py`, `services/api/health.py`,
`scripts/healthcheck_pipelines.py`, `scripts/sync_protective_orders.py`,
`monitor/strategy_lifecycle.py`
Special: has a mid-file `import logging as _logging` at line 2348.

### Domain J — Macro & Treasury (lines 2143–2352, ~250 L)
Functions: `upsert_macro_indicators`, `batch_upsert_macro_indicators`, `get_macro_indicators`,
`batch_upsert_treasury_curve`, `get_treasury_curve`
Constants: `_MACRO_INDICATOR_COLS`, `_TREASURY_CURVE_COLS`
Import sites (~8): `data/macro.py`, `data/fred.py`, `regime/engine.py`

### Domain K — Risk Cache (lines 2353–2661, ~400 L)
Functions: `_ensure_risk_cache_tables`, `get_cached_regime_transitions`,
`set_cached_regime_transitions`, `get_cached_ruin_probability`,
`set_cached_ruin_probability`, `get_cached_portfolio_risk`
Globals: `_risk_cache_tables_ensured` (lazy-init boolean)
Import sites (~12): `risk/portfolio_var.py`, `risk/ruin_probability.py`, `regime/engine.py`,
`services/api/dashboard.py`, `scripts/compute_daily_risk.py`

### Domain L — Broker Orders (lines 2663–2796, ~200 L)
Functions: `get_broker_fill_price`, `get_broker_orders`, `get_fill_price`
Import sites (~6): `scripts/reconcile_ledger.py`, `scripts/sync_broker_orders.py`,
`core/reconcile.py`

### Domain M — Position Protective Orders (lines 2798–2986, ~220 L)
Functions: `get_protective_record`, `upsert_protective_record`, `close_protective_record`,
`list_active_protective_records`, `list_protective_gaps`
Import sites (~15): `scripts/sync_protective_orders.py`, `brokers/live_executor.py`,
`scripts/backfill_position_protective_orders.py` (many aliased as `_upr`, `_upr_sp`, etc.)

### Domain N — Lifecycle (lines 2987–3131, ~230 L)
Functions: `get_lifecycle_state`, `set_lifecycle_state`, `list_lifecycle_states`
Has mid-file `import logging as _lifecycle_logging` at line 2981 and `_lifecycle_log` global.
Import sites (~5): `monitor/strategy_lifecycle.py`, `services/api/lifecycle.py`

### Domain O — Paper Trades (lines 3132–3520, ~400 L)
Functions: `record_paper_trade_entry`, `update_paper_trade_protective_orders`,
`record_paper_trade_exit`, `get_open_paper_trades`, `get_closed_paper_trades`,
`get_paper_trades_for_universe`, `get_paper_protective_record`, `upsert_paper_protective_record`,
`close_paper_protective_record`, `list_active_paper_protective_records`
Has mid-file `import logging as _paper_logging` at line 3128 and `_paper_log` global.
Import sites (~20): `brokers/live_executor.py`, `brokers/routing_policy.py`,
`scripts/sync_protective_orders.py`, `scripts/intraday_monitor.py`, `scripts/eod_settlement.py`

---

## Proposed file layout

```
db/
├── atlas_db.py          ~150 L  — get_db, init_db, DB_PATH, globals, re-export shim
├── trades.py            ~500 L  — Domain B
├── regime.py            ~120 L  — Domain C
├── ohlcv.py             ~110 L  — Domain D
├── signals.py           ~300 L  — Domain E (signals + plans)
├── equity.py            ~280 L  — Domain F
├── overlay.py           ~300 L  — Domain G
├── research.py          ~600 L  — Domain H + Candidate #9 content
├── monitoring.py         ~70 L  — Domain I
├── macro.py             ~250 L  — Domain J
├── risk_cache.py        ~400 L  — Domain K
├── broker_orders.py     ~200 L  — Domain L
├── protective_orders.py ~220 L  — Domain M  (NOTE: distinct from brokers/protective_orders.py)
├── lifecycle.py         ~230 L  — Domain N
└── paper_trades.py      ~400 L  — Domain O
```

Net: **3,520 L in 1 file → ~4,130 L across 15 files** (slight increase due to per-module docstrings/imports). Every domain is now independently testable, independently importable.

---

## Backward compatibility — the re-export shim

Every existing `from db.atlas_db import X` continues to work unchanged. No sed across 419 sites.

```python
# db/atlas_db.py (after extraction — ~150 L)
"""
Atlas DB — connection layer and re-export shim.

All domain functions live in their sub-modules (db/trades.py, db/regime.py, etc.).
This file re-exports everything so the 419 existing import sites continue to work.
Migrate call sites to sub-modules at your own pace.
"""
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional

# ── Module-level globals (OWNED HERE — sub-modules must not redeclare) ────────
DB_PATH = Path(__file__).resolve().parent.parent / "data" / "atlas.db"
_db_path_override: Optional[str] = None
_state_dir_override: Optional[str] = None
_wal_initialized_paths: set = set()

@contextmanager
def get_db(db_path: Optional[str] = None):
    """Full implementation stays here."""
    ...

def init_db(db_path: Optional[str] = None) -> None:
    """Full implementation stays here."""
    ...

# ── Re-exports (one block per domain) ────────────────────────────────────────
from db.trades import (           # noqa: E402, F401
    record_trade_entry, update_trade_protective_orders, record_trade_exit,
    get_open_positions, get_closed_trades, performance_summary,
)
from db.regime import (
    record_regime, get_current_regime, get_current_regime_state, get_regime_history,
)
from db.ohlcv import upsert_ohlcv, get_ohlcv, get_universe_data
# ... (one block per domain module)
```

Sub-module structure — every domain file follows this pattern:

```python
# db/trades.py
from __future__ import annotations
import json
import logging
from typing import Any, Dict, List, Optional
from db.atlas_db import get_db  # ← sub-module imports get_db from shim

logger = logging.getLogger(__name__)

def record_trade_entry(
    ticker: str,
    strategy: str,
    universe: str,
    side: str,
    entry_price: float,
    qty: int,
    stop_price: float | None = None,
    ...
) -> int:
    with get_db() as db:
        ...
```

**Why no circular imports:** `db/atlas_db.py` provides `get_db` + re-exports. It does not import from sub-modules at module load time — the `from db.trades import ...` lines are deferred imports in practice. Python handles this correctly because `get_db` is defined before the re-export block. If circular import issues arise, move re-exports to a `db/__init__.py` barrel instead.

---

## Alternative: hard-cut migration

If shim is unacceptable, sed-migrate one domain at a time after extraction:

```bash
# Example: migrate heartbeat callers after db/monitoring.py extracted
grep -rln "from db.atlas_db import.*record_heartbeat" /root/atlas --include="*.py" | \
  xargs sed -i 's/from db\.atlas_db import record_heartbeat/from db.monitoring import record_heartbeat/g'
```

**Not recommended:** breaks on multi-name imports (`from db.atlas_db import record_heartbeat, get_db`). Shim approach is strictly safer.

---

## Module-level globals — ownership table

These globals are load-bearing across the entire system. Sub-modules must NOT redeclare them.

| Global | Stays in | Sub-module access pattern |
|--------|----------|--------------------------|
| `_db_path_override` | `db/atlas_db.py` | Transparent via `get_db()` — no direct access needed |
| `_state_dir_override` | `db/atlas_db.py` | Used only by `_assert_state_file_parity` which stays in `atlas_db.py` |
| `_wal_initialized_paths` | `db/atlas_db.py` | Transparent via `get_db()` |
| `_risk_cache_tables_ensured` | `db/risk_cache.py` | Module-level in `risk_cache.py` only — not re-exported |
| `_MACRO_INDICATOR_COLS` | `db/macro.py` | Module-level constant — not re-exported |
| `_TREASURY_CURVE_COLS` | `db/macro.py` | Module-level constant — not re-exported |
| `_lifecycle_log` | `db/lifecycle.py` | Replace with `logger = logging.getLogger(__name__)` |
| `_paper_log` | `db/paper_trades.py` | Replace with `logger = logging.getLogger(__name__)` |

**Test isolation preservation:** `conftest.py` autouse fixture sets `import db.atlas_db as _adb; _adb._db_path_override = str(tmp_path / "test.db")`. After extraction, sub-modules call `get_db()` which reads `_db_path_override` from the `db.atlas_db` namespace — isolation continues to work with zero changes to `conftest.py`.

---

## Migration order (1–2 domains per session, lowest risk first)

### Session 1 — Monitoring + Macro (~3h, no trading path)
**`db/monitoring.py`** (70 L, 35 import sites)
- `record_heartbeat`, `get_heartbeats`, `record_system_log`, `get_system_logs`
- Pure write-then-read. If this breaks, no live trades are affected.
- Move mid-file `import logging as _logging` (line 2348) into the new module as `import logging; logger = logging.getLogger(__name__)`

**`db/macro.py`** (250 L, 8 import sites)
- `_MACRO_INDICATOR_COLS`, `_TREASURY_CURVE_COLS`, `upsert_macro_indicators`, `batch_upsert_macro_indicators`, `get_macro_indicators`, `batch_upsert_treasury_curve`, `get_treasury_curve`
- All callers are in `data/` — isolated subsystem.

**Test gate:** `pytest tests/test_atlas_db.py tests/test_atlas_db_perf_clamp.py -x` green.

### Session 2 — Regime + Research + Candidate #9 (~4h)
**`db/regime.py`** (120 L) + **`db/research.py`** (500 L + Candidate #9 additions)
- See Candidate #9 spec for `research/db.py` consolidation details.
- Research tables (`research_experiments`, `research_sessions`, `research_best`, `research_discoveries`) and their `CREATE TABLE IF NOT EXISTS` DDL all move here.
- `research/db.py` becomes a re-export shim: `from db.research import log_experiment, log_session, end_session, log_discovery`.

### Session 3 — Overlay + Risk Cache + Broker Orders (~4h)
**`db/overlay.py`** (300 L), **`db/risk_cache.py`** (400 L), **`db/broker_orders.py`** (200 L)
- `_ensure_risk_cache_tables` lazy-init: move `_risk_cache_tables_ensured` flag into `db/risk_cache.py`. Verify it initialises correctly when first imported (call `from db.risk_cache import get_cached_regime_transitions; get_cached_regime_transitions()` in isolation).

### Session 4 — Lifecycle + Position Protective Orders (~3h)
**`db/lifecycle.py`** (230 L, 5 import sites — trivially small)
**`db/protective_orders.py`** (220 L)
- **Name warning:** `db/protective_orders.py` stores persistence records; `brokers/protective_orders.py` (from Candidate #6/#2 PR2) places live broker orders. They are different layers. Callers that need both must use fully-qualified imports.

### Session 5 — Trades + Paper Trades + OHLCV + Equity + Signals (~6h, highest risk)
**`db/paper_trades.py`** first (paper path — no real money at risk).
**`db/trades.py`** — highest-risk extraction. Every live trade write goes through here.
  - `_compute_and_fill_mae_mfe` takes `db=` kwarg to share a connection with `record_trade_exit`. Both functions must move together to `db/trades.py`. The `db=` pattern must be preserved exactly.
  - `_assert_state_file_parity` uses `_state_dir_override` — either move it to `db/trades.py` and import `_state_dir_override` from `db.atlas_db`, or keep it in `atlas_db.py` and import it from there.
**`db/equity.py`**, **`db/signals.py`**, **`db/ohlcv.py`**

---

## Testing

### Existing test files
- `tests/test_atlas_db.py` — broad coverage across many domains
- `tests/test_atlas_db_perf_clamp.py` — `get_closed_trades` / `get_regime_history` limit-clamp tests

### Target per-domain test layout
```
tests/
├── test_atlas_db.py            ← shim smoke tests only (import all re-exports, verify they resolve)
├── test_db_trades.py           ← record_trade_entry, _compute_and_fill_mae_mfe, performance_summary
├── test_db_regime.py           ← record_regime, get_current_regime, get_regime_history
├── test_db_monitoring.py       ← record_heartbeat, record_system_log
├── test_db_research.py         ← record_experiment, upsert_research_best (merged with #9 tests)
├── test_db_macro.py            ← batch_upsert_macro_indicators, get_macro_indicators
├── test_db_equity.py           ← record_equity, get_equity_curve
├── test_db_overlay.py          ← record_overlay_decision, get_shadow_events
├── test_db_risk_cache.py       ← get_cached_regime_transitions, get_cached_ruin_probability
├── test_db_protective.py       ← get_protective_record, upsert_protective_record
├── test_db_lifecycle.py        ← get_lifecycle_state, set_lifecycle_state, list_lifecycle_states
└── test_db_paper_trades.py     ← merged from tests/test_paper_db.py (already exists)
```

---

## Gotchas

1. **`_assert_state_file_parity` uses `_state_dir_override` directly** — if `record_trade_entry` moves to `db/trades.py`, the helper either moves with it (importing `_state_dir_override` from `db.atlas_db`) or stays in `atlas_db.py` as a private function imported by `trades.py`. Recommend the latter to keep `_state_dir_override` ownership in one place.

2. **`_compute_and_fill_mae_mfe` transaction coupling** — takes `db=` kwarg so `record_trade_exit` can pass its own connection. Both must live in the same file (`db/trades.py`). Do NOT split them.

3. **Mid-file `import logging as _logging` workarounds** (lines 2348, 2981, 3128) — these are legacy namespace-collision workarounds. When extracting each domain, replace with `import logging; logger = logging.getLogger(__name__)` at the top of the new module.

4. **`_ensure_risk_cache_tables` lazy init** — `_risk_cache_tables_ensured` is module-level in `atlas_db.py` today. When moved to `risk_cache.py`, it initialises correctly on first call. If `importlib.reload` is used in tests, the flag resets — current tests do not reload modules so this is safe.

5. **Aliased imports in `sync_protective_orders.py`** — the script imports `upsert_protective_record` with 4 different aliases (`_upr`, `_upr_sp`, `_upr_dbc`, `_upr_all`). The shim re-exports `upsert_protective_record` once; aliases are defined at the call site and are not affected by extraction.

6. **`conftest.py` autouse fixture compatibility** — `_isolate_prod_db` sets `_adb._db_path_override`. Sub-modules call `get_db()` → reads `_db_path_override` from `db.atlas_db` namespace → isolation continues to work. Verify after first extraction with: `python -c "import db.atlas_db as _adb; _adb._db_path_override='/tmp/t.db'; from db import monitoring; print('OK')"`.

7. **`brokers/protective_orders.py` vs `db/protective_orders.py`** — two different layers, same base name. Use fully-qualified module paths in any code that imports from both. Document this in each module's docstring.

---

## Dependency chain

- **#3 BLOCKS #9** — `db/research.py` must be created as part of the broader `db/atlas_db.py` split; Candidate #9 adds `research/db.py` content to it. Do Session 2 together.
- **#3 is independent of #2, #4, #5, #6** — can start any time after PR1 of #2 ships.
- **Recommended start:** Session 1 (monitoring + macro) — 2-3h, zero trading-path risk, immediately demonstrates the pattern for later sessions.
