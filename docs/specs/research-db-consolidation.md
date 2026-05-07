# Research DB Consolidation (Candidate #9)

**Status:** design sketch — engineering-ready
**Predecessor:** Candidate #3 (`db/atlas_db.py` domain split)
**Target:**
  - `research/db.py` (147 L) — 4 functions, wraps `db.atlas_db.get_db()`
  - `research/migrate_research.py` (202 L) — DDL for `research_sessions` + `research_discoveries` tables (NOT in `db/schema.sql`)
**Goal:** Move all research DB functions into `db/research.py` (created as part of #3 Session 2); `research/db.py` becomes a 6-line re-export shim

## Problem

Research data lives in two partially-disconnected storage layers:

1. **`db/schema.sql`** — defines `research_experiments` and `research_best` tables (canonical, applied by `init_db()`)
2. **`research/migrate_research.py`** — defines `research_sessions` and `research_discoveries` tables (NOT in schema.sql; called separately from `research/migrate_research.py:create_tables()`)
3. **`research/db.py`** — 4 functions that write to `research_experiments` + `research_sessions` + `research_discoveries` tables; imports `get_db` from `db.atlas_db`

This split means:
- A fresh DB (via `init_db()`) doesn't have `research_sessions` or `research_discoveries` tables — `log_session()` fails silently until `create_tables()` is called separately
- Schema drift: `research_experiments` migrations in `scripts/migrations/` are applied to the canonical DB; `research_sessions`/`research_discoveries` DDL is only in `migrate_research.py` with no migration path
- Two separate mental models: "DB functions" live in `db/atlas_db.py` but research DB functions live in `research/db.py`

---

## Pre-investigation: full function catalog

### `research/db.py` (147 L)

Four functions, all non-fatal (wrapped in try/except):

```
log_experiment(strategy, metrics, params_changed, status, description,
               source="sweeper", market="sp500", stage="") -> None
  - Inserts into research_experiments (16-field INSERT)
  - Side-effect: queries regime_history to populate regime_state column
  - Generates exp_id = f"ar-{timestamp}"

log_session(mode, strategy=None, started_at=None) -> int | None
  - Inserts into research_sessions (INSERT, returns lastrowid)

end_session(session_id, experiments_run=0, experiments_kept=0, status="completed") -> None
  - UPDATEs research_sessions with ended_at, duration_minutes

log_discovery(run_date, papers_found, papers_filtered, specs_extracted,
              strategies_generated, paper_titles, status) -> None
  - Inserts into research_discoveries
```

Import sites:
- `research/autoresearch_nightly.py`: `from research.db import log_session, end_session`
- `research/loop.py`: `from research.db import log_experiment` (deferred/lazy import)
- `research/discovery/discovery.py`: `from research.db import log_discovery` (deferred/lazy)
- `tests/test_regime_experiment_logging.py`: `from research.db import log_experiment`

Total import sites: 4 files.

### `research/migrate_research.py` (202 L)

`create_tables()` function: creates `research_sessions` + `research_discoveries` tables and their indexes using `get_db()`. Called from:
- Module `__main__` block
- `backfill_experiments()` and `backfill_brain()` functions (data backfill utilities)
- No automatic call during `init_db()`

**Schema drift finding:**
- `research_sessions` and `research_discoveries` DDL is ONLY in `migrate_research.py`
- `db/schema.sql` does NOT contain these tables
- A fresh test DB (via `init_db()`) will fail `log_session()` / `log_discovery()` unless `create_tables()` is called first
- `test_regime_experiment_logging.py` manually creates a minimal `research_sessions` schema (line 55) — evidence of this exact workaround

---

## Schema comparison: `db/schema.sql` vs `research/migrate_research.py`

### Tables in `db/schema.sql` (canonical)
```sql
-- research_experiments: defined at line 274
CREATE TABLE IF NOT EXISTS research_experiments (
    id TEXT PRIMARY KEY,
    strategy TEXT NOT NULL,
    universe TEXT,
    experiment_type TEXT,
    params_changed TEXT,
    description TEXT,
    sharpe REAL,
    trades INTEGER,
    max_dd_pct REAL,
    profit_factor REAL,
    cagr_pct REAL,
    status TEXT DEFAULT 'pending',
    recommendation TEXT,
    agent_id TEXT,
    completed_at TEXT DEFAULT CURRENT_TIMESTAMP,
    window_coverage_pct REAL,
    regime_state TEXT
);
-- research_best: defined at line 300 (with OOS columns from migration 2026-05-06)
```

### Tables in `research/migrate_research.py` only (NOT in schema.sql)
```sql
-- research_sessions
CREATE TABLE IF NOT EXISTS research_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    mode TEXT NOT NULL,         -- 'sweep', 'llm_loop', 'discovery'
    strategy TEXT,
    ended_at TEXT,
    duration_minutes REAL,
    experiments_run INTEGER DEFAULT 0,
    experiments_kept INTEGER DEFAULT 0,
    status TEXT DEFAULT 'running'
)

-- research_discoveries
CREATE TABLE IF NOT EXISTS research_discoveries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_date TEXT NOT NULL,
    papers_found INTEGER DEFAULT 0,
    papers_filtered INTEGER DEFAULT 0,
    specs_extracted INTEGER DEFAULT 0,
    strategies_generated INTEGER DEFAULT 0,
    paper_titles TEXT,          -- JSON list
    status TEXT DEFAULT 'completed',
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
)
```

**Schema drift risk:** If a migration adds a column to `research_sessions` or `research_discoveries`, there is no migrations path — the only DDL is in `migrate_research.py:create_tables()`.

### Migrations that touch research tables
```
scripts/migrations/2026-04-22-add-window-coverage.py        — research_experiments.window_coverage_pct
scripts/migrations/2026-04-28-research-best-solo-sharpe.py  — research_best.solo_sharpe
scripts/migrations/2026-05-06-add-oos-columns-research-best.py — research_best OOS columns
scripts/migrations/2026-05-06-add-regime-to-research-best.py   — research_best.regime_state + PK rebuild
```

None touch `research_sessions` or `research_discoveries` — they have never been migrated, only created. This is acceptable only because they have never needed schema changes.

---

## Proposed consolidation

### Step 1 — Add `db/research.py` (as part of #3 Session 2)

`db/research.py` (~600 L) contains:
1. All functions currently in `db/atlas_db.py` for research domain (Domains H: `record_experiment`, `get_experiments`, `update_experiment_status`, `upsert_research_best`, `get_research_best`)
2. All functions currently in `research/db.py` (`log_experiment`, `log_session`, `end_session`, `log_discovery`)
3. DDL for `research_sessions` and `research_discoveries` tables (currently only in `migrate_research.py`)

```python
# db/research.py (~600 L)
"""
Research database layer.

Covers:
  - research_experiments (sweeper + LLM loop results)
  - research_best (best params per strategy/universe/regime)
  - research_sessions (autoresearch run tracking)
  - research_discoveries (paper discovery pipeline runs)

All functions fail gracefully (log warning, never crash the research runner).
"""
from __future__ import annotations
import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from db.atlas_db import get_db

logger = logging.getLogger(__name__)

# ── Schema bootstrap ────────────────────────────────────────────────────────

def ensure_research_tables() -> None:
    """Idempotent: create research_sessions + research_discoveries if missing.

    Called lazily by log_session() and log_discovery() so test DBs and fresh
    installs don't need a separate migrate step.
    """
    with get_db() as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS research_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at TEXT NOT NULL,
                mode TEXT NOT NULL,
                strategy TEXT,
                ended_at TEXT,
                duration_minutes REAL,
                experiments_run INTEGER DEFAULT 0,
                experiments_kept INTEGER DEFAULT 0,
                status TEXT DEFAULT 'running'
            )
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS research_discoveries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_date TEXT NOT NULL,
                papers_found INTEGER DEFAULT 0,
                papers_filtered INTEGER DEFAULT 0,
                specs_extracted INTEGER DEFAULT 0,
                strategies_generated INTEGER DEFAULT 0,
                paper_titles TEXT,
                status TEXT DEFAULT 'completed',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        db.execute("CREATE INDEX IF NOT EXISTS idx_sessions_mode ON research_sessions(mode)")
        db.execute("CREATE INDEX IF NOT EXISTS idx_discoveries_date ON research_discoveries(run_date)")

# ── Existing atlas_db Domain H functions (moved here) ──────────────────────
def record_experiment(...) -> None: ...
def get_experiments(...) -> list[dict]: ...
def update_experiment_status(...) -> None: ...
def upsert_research_best(...) -> None: ...
def get_research_best(...) -> list[dict]: ...

# ── Functions moved from research/db.py ────────────────────────────────────
def log_experiment(...) -> None: ...    # non-fatal; calls record_experiment internally
def log_session(...) -> int | None: ... # non-fatal; calls ensure_research_tables() first
def end_session(...) -> None: ...       # non-fatal
def log_discovery(...) -> None: ...     # non-fatal; calls ensure_research_tables() first
```

### Step 2 — `research/db.py` becomes a 6-line re-export shim

```python
# research/db.py (after consolidation — 6 lines)
"""Re-export shim — all implementations moved to db.research."""
from db.research import (  # noqa: F401
    log_experiment,
    log_session,
    end_session,
    log_discovery,
)
```

### Step 3 — Add `research_sessions` + `research_discoveries` to `db/schema.sql`

```sql
-- Add to db/schema.sql after research_best

CREATE TABLE IF NOT EXISTS research_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    mode TEXT NOT NULL,
    strategy TEXT,
    ended_at TEXT,
    duration_minutes REAL,
    experiments_run INTEGER DEFAULT 0,
    experiments_kept INTEGER DEFAULT 0,
    status TEXT DEFAULT 'running'
);
CREATE INDEX IF NOT EXISTS idx_sessions_mode ON research_sessions(mode);

CREATE TABLE IF NOT EXISTS research_discoveries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_date TEXT NOT NULL,
    papers_found INTEGER DEFAULT 0,
    papers_filtered INTEGER DEFAULT 0,
    specs_extracted INTEGER DEFAULT 0,
    strategies_generated INTEGER DEFAULT 0,
    paper_titles TEXT,
    status TEXT DEFAULT 'completed',
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_discoveries_date ON research_discoveries(run_date);
```

After Step 3, `init_db()` creates all 4 research tables on a fresh DB. Test isolation works automatically.

### Step 4 — Add shim re-exports to `db/atlas_db.py` (as part of #3)

```python
# db/atlas_db.py re-export block
from db.research import (
    record_experiment, get_experiments, update_experiment_status,
    upsert_research_best, get_research_best,
    log_experiment, log_session, end_session, log_discovery,
)
```

Existing `from db.atlas_db import upsert_research_best` (30+ sites) continues to work.

---

## Migration: what goes where

| Current location | Function | Target |
|-----------------|----------|--------|
| `db/atlas_db.py` lines 1770–2036 | `record_experiment`, `get_experiments`, `update_experiment_status`, `upsert_research_best`, `get_research_best` | `db/research.py` |
| `research/db.py` | `log_experiment`, `log_session`, `end_session`, `log_discovery` | `db/research.py` |
| `research/migrate_research.py` | `create_tables()` DDL for `research_sessions` + `research_discoveries` | `db/research.py` as `ensure_research_tables()` + `db/schema.sql` |
| `research/migrate_research.py` | `backfill_experiments()`, `backfill_brain()` | Stay in `research/migrate_research.py` (data migration, not schema) |

`research/migrate_research.py` keeps its backfill functions. They become callers of `db/research.py` functions (via the shim or direct import). The module is not deleted.

---

## `ensure_research_tables` lazy-init pattern

The `research_sessions` and `research_discoveries` tables don't exist in `schema.sql` today. Rather than a hard migration (risky), use lazy-init:

```python
_research_tables_ensured = False

def ensure_research_tables() -> None:
    global _research_tables_ensured
    if _research_tables_ensured:
        return
    # ... CREATE TABLE IF NOT EXISTS
    _research_tables_ensured = True
```

`log_session()` and `log_discovery()` call `ensure_research_tables()` at the start. After Step 3 (add to schema.sql), `ensure_research_tables()` becomes a no-op (tables already exist). The flag is a perf optimisation, not a correctness gate.

---

## `research/migrate_research.py` disposition

The file is NOT deleted. It keeps:
- `backfill_experiments()` — scans TSV files, re-inserts into `research_experiments`
- `backfill_brain()` — clears and re-inserts into `research_brain`

These are one-time data migration utilities, not schema helpers. They call `get_db()` directly and are independent of `db/research.py`.

`create_tables()` in `migrate_research.py` is superseded by `ensure_research_tables()` in `db/research.py`. Add a deprecation comment:

```python
def create_tables():
    """Deprecated: tables now created by db.research.ensure_research_tables() + db/schema.sql.
    Call this only for historical backfill operations. Safe to call (IF NOT EXISTS guards).
    """
    ...  # keep body unchanged for safety
```

---

## Pairs with #3: do in Session 2

This candidate is low-risk when done alongside #3 Session 2 (regime + research extraction). The work is:

1. Extract Domain H from `atlas_db.py` → `db/research.py` (part of #3)
2. Append `research/db.py` content → `db/research.py` (this candidate)
3. Add `research_sessions` + `research_discoveries` DDL → `db/research.py` + `db/schema.sql`
4. Make `research/db.py` a re-export shim
5. Add Domain H re-exports to `db/atlas_db.py` shim

One PR covers both #3 Session 2 and this candidate.

---

## Migration: existing migrations still work

All 4 migrations touching research tables (`2026-04-22-add-window-coverage.py`, `2026-04-28-research-best-solo-sharpe.py`, `2026-05-06-add-oos-columns-research-best.py`, `2026-05-06-add-regime-to-research-best.py`) operate directly on the SQLite database via `sqlite3.connect()` or `get_db()`. They do NOT import from `db/atlas_db.py` for their schema changes — they use raw SQL `ALTER TABLE`. After consolidation, these migrations continue to work unchanged.

---

## Testing

### Existing test files
- `tests/test_regime_experiment_logging.py` — tests `log_experiment` from `research.db`; has manual table-creation workaround (line 55: creates `research_sessions` schema inline). After Step 3, this workaround is unnecessary but harmless.
- `tests/test_api_research.py` — tests `/api/research/*` endpoints; creates `research_discoveries` table inline (line 46). Same — after Step 3, the inline CREATE is a no-op.
- `tests/test_oos_columns_research_best.py` — tests `upsert_research_best` / `get_research_best` via `db.atlas_db`. Works via shim after consolidation.
- `tests/test_regime_research_best.py` — same pattern.

### Migration test coverage
After consolidation, add `tests/test_db_research.py`:
```
test_log_experiment_inserts_row           — via research.db shim (backward compat)
test_log_experiment_inserts_row_direct    — via db.research directly
test_log_session_returns_id
test_end_session_updates_duration
test_log_discovery_inserts_row
test_ensure_research_tables_idempotent    — call twice, no error
test_upsert_research_best_round_trip      — upsert + get_research_best
test_get_experiments_filters              — strategy, status, universe filter
test_shim_import_works                    — from research.db import log_session → resolves
test_atlas_db_shim_works                  — from db.atlas_db import upsert_research_best → resolves
```

### Test isolation after Step 3
Test DBs are created by `init_db()` from `db/schema.sql`. After adding `research_sessions` and `research_discoveries` to `schema.sql`, all test DBs automatically include these tables. The manual table-creation workarounds in `test_regime_experiment_logging.py` and `test_api_research.py` become no-ops (CREATE TABLE IF NOT EXISTS is idempotent).

---

## Gotchas

1. **`log_experiment` side-effect: `regime_history` query** — `log_experiment` queries `regime_history ORDER BY date DESC LIMIT 1` to populate `regime_state`. This query is inside a `try/except` that silently degrades. After moving to `db/research.py`, the query still uses `get_db()` (same connection) — no change in behavior.

2. **`research/loop.py` deferred import** — `from research.db import log_experiment` is a deferred import inside a try block:
   ```python
   try:
       from research.db import log_experiment
       upsert_research_best = ...
   except ImportError:
       upsert_research_best = None
   ```
   After the shim, `from research.db import log_experiment` resolves to `from db.research import log_experiment` (via the shim). The deferred import and `ImportError` guard continue to work correctly.

3. **`research/discovery/discovery.py` deferred import** — `from research.db import log_discovery` at line 698 uses a comment `# noqa: PLC0415 — deferred (sys.path)`. This is inside a function body, after `sys.path.insert`. After the shim, it resolves to `db.research.log_discovery`. sys.path context is the same — no change.

4. **`autoresearch_nightly.py` top-level import** — `from research.db import log_session, end_session` is at module top level (line 43). This is NOT deferred. After the shim, this resolves immediately. `research/db.py` imports from `db.research`, which imports from `db.atlas_db` — the import chain is: `autoresearch_nightly.py → research/db.py → db/research.py → db/atlas_db.py`. No circular imports.

5. **`_research_tables_ensured` module-level flag** — after lazy-init and Step 3 (tables in schema.sql), the flag becomes a no-op perf optimisation. Keep it to prevent double-`CREATE` on first call in edge cases (pre-schema.sql fresh DBs).

6. **`research/migrate_research.py:create_tables()` still called by cron or operator** — the function is NOT deleted, so any ad-hoc call is safe. Add the deprecation comment but preserve the body. Future cleanup: delete after 90-day quiet period.

---

## Dependency chain

- **#9 MUST be done in Session 2 of #3** — shares the extraction of `db/research.py`
- **#9 blocks nothing else** — no other candidate depends on research DB consolidation
- **#9 is independent of #2, #4, #5, #6** — no shared code paths
- **Timeline:** cannot start until #3 Session 2 begins (~3-6h after #3 Session 1 is complete and stable)
