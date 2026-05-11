# Dashboard Universe & Strategy Toggles — Spec

**Status:** Draft for engineering review
**Author:** Planning Lead
**Date:** 2026-05-05
**Owner (impl):** TBD (Backend + Frontend)
**Target ship:** Phase 1 (DB + API + audit) within 1 week of approval; Phase 2 (UI behind flag) within 2 weeks; Phase 3 (GA) once dogfood complete.
**Final location:** copy to `/root/atlas/docs/specs/dashboard-universe-strategy-toggles.md` once available (Planning Lead write-domain blocks `/root/atlas/docs/**`).

---

## 1. Problem statement

Operators currently flip universe and strategy on/off by editing `config/active/*.json` files by hand and waiting for the next cron pickup. This is fragile (typos), invisible (no audit trail), slow (SSH + edit + commit), and doesn't compose with the new safety levers from the consolidation work (`mode: passive` vs `live_enabled: false`, which mean different things). The dashboard already shows current effective state; it must also let an authenticated operator change it — safely, reversibly, with a complete audit trail — at both universe and per-(universe × strategy) granularity.

## 2. Goals & non-goals

### Goals
1. Toggle a **universe** between three explicit states from the dashboard: `live`, `passive`, `disabled` — preserving the operational distinction shipped tonight (passive = block new entries, maintain stops; disabled = full kill switch, no OCO maintenance).
2. Toggle a **strategy on a specific universe** between `enabled` / `disabled` — independent of other universes (e.g., `connors_rsi2` off on `commodity_etfs` while staying on for `sp500`).
3. Every change writes an immutable audit row: who, when, why, from-state, to-state, source (dashboard / telegram / cli), expiry.
4. Every change is one-click revertable to the prior effective state.
5. Optional auto-expiry (default 30 days) so toggles don't silently linger past their original justification.
6. Read-side integration is centralized — exactly one config-loader path consults overrides; no script reads JSON directly and bypasses the override layer.
7. Research / backtest / sweep code paths are explicitly **excluded** from override consultation — they read raw JSON so we can compare "what we'd do if enabled" vs "what we're doing".
8. Live-trading guardrails: type-to-confirm for production universes, mandatory reason field, "I understand this affects live trading" checkbox, can't mark `disabled` while open positions exist on that universe.

### Non-goals
- Bulk strategy enable/disable across all universes in one click. Each toggle is per (universe, strategy) and must be performed individually. (Cuts blast radius.)
- Editing arbitrary config keys (risk parameters, ATR multipliers, weights, etc.) — that's a separate, harder problem with research validation. This spec is binary on/off only.
- Mobile UX. Dashboard is desktop-only; we're not adding mobile-specific layouts.
- Multi-user permissions / RBAC. Single Basic Auth user (`atlas:`) shared by all operators. We log `human:<username>` as actor but don't enforce role-based gates.
- Replacing the JSON config files. JSON remains the canonical "what would we do if no overrides existed" baseline; overrides layer **on top** of it.

## 3. Background — what exists today

### 3.1 Config files
- `config/active/{market}.json` — one file per universe (`sp500`, `commodity_etfs`, `sector_etfs`, `asx`, `crypto`, `gold_etfs`, `treasury_etfs`, `defensive_etfs`).
- Every config has:
  - `trading.mode` ∈ {`"live"`, `"passive"`, `"paper"`}
  - `trading.live_enabled` ∈ {`true`, `false`}
  - `strategies.{name}.enabled` ∈ {`true`, `false`} (per strategy)
  - `strategies.{name}.weight` (used by allocation; 0 = effectively off but `enabled` still gates it)
- Tonight's consolidation introduced `mode: passive` as a soft entry blocker (no new positions, but stop-maintenance / OCO sync continues until positions close). `live_enabled: false` is the hard kill — drops out of broker/registry resolution entirely.

### 3.2 Existing read-side logic (audit results)

Confirmed call sites (full table in §6.3):

| Lever | Read by | What it gates |
|---|---|---|
| `trading.mode` | `execute_approved.py:68`, `cli.py:535` | New entry execution. Telegram message renders mode badge. |
| `trading.live_enabled` | `intraday_monitor.py:362`, `sync_protective_orders.py:847`, `eod_settlement.py:540`, `live_executor.py:153,263`, `registry.py:78,111,138`, `health_check.py:60,346`, `regen_stops_held_state.py:50` | All position maintenance + execution. **The actual kill switch.** |
| `strategies.*.enabled` | `cli.py:80–100` (`get_strategies()`), `health_check.py:126–133` (`build_strategies` for backtest probe), research/backtest code | Whether a strategy is constructed at all in the live trading path. |

**Three scripts bypass the canonical config loader** — they call `json.load(open("config/active/{market}.json"))` directly:
- `scripts/eod_settlement.py:53–56`
- `scripts/sync_protective_orders.py:101–108`
- `scripts/reconcile_ledger.py:28–35`

The dashboard health API (`services/api/health.py:145, 326`) also reads `config/active/*.json` via `glob` for the universe status display.

### 3.3 Database
- `data/atlas.db` (~80 MB, WAL mode). 39 tables + 1 view. Schema version 28.
- Canonical accessor: `db.atlas_db.get_db()` context manager (WAL, `foreign_keys=ON`, 30s busy timeout).
- Schema lives in `db/schema.sql` + idempotent migrations under `scripts/migrations/YYYY-MM-DD-*.py`. No automated runner — operator runs migrations manually in date order, with `--apply` flag.
- Existing audit-log gold standard: **`fix_audit_log`** — immutable append-only with BEFORE-UPDATE/DELETE triggers raising ABORT, `actor` field uses `human:<username>` convention.
- Existing closest analog: **`market_state`** table with `mode` and `halted` columns — but it has no audit, no expiry, no reason, no per-strategy granularity. We will **leave `market_state` alone** for now (it's referenced elsewhere) and add a parallel override layer; consolidation is a future concern.

### 3.4 Dashboard frontend
- `dashboard-ui/` — React 19 + Vite + Tailwind v4 + TanStack Query v5. No router (state-based tab switching). No component library, no form library, no modal library, no toast library — everything is hand-rolled with Tailwind classes and `useState`.
- 4 tabs today: Portfolio, Finance, Research, Remediation. Adding a tab requires 5 changes: TabId union, `lazy()` import, preload export, `TabBar.tabs` entry, App.tsx render branch.
- Closest action-pattern: `PendingPromotionsWidget.tsx` — POSTs with optimistic invalidation, inline busy state, inline coloured `<div>` toast. We mirror this style exactly.
- **No existing admin/controls/settings tab.** The proposed Controls tab would be the first.

### 3.5 Auth
- HTTP Basic Auth via `services/auth.py:check_auth` dependency.
- Credentials in `~/.atlas-secrets.json` (`dashboard_user`, `dashboard_pass`).
- We reuse this exactly. Username available as `_auth.username` in handlers.

## 4. Design overview

### 4.1 Architectural choice: DB override layer (not JSON mutation)

We add a **DB-resident override layer** that takes precedence over `config/active/*.json` at config-read time. Reasons:

| Concern | DB override | JSON mutation |
|---|---|---|
| Audit trail | Native (append-only triggers) | Requires git commits + parallel log table |
| Expiry / TTL | Native (`expires_at` column, periodic sweep) | Requires cron + JSON edits |
| Atomicity vs research recalibration scripts | DB transaction; no race | Race with sweep scripts that rewrite the JSON |
| Reversibility | Soft-delete row → JSON wins again | Manual git revert |
| v2.0 SOT direction | Aligned (DB is canonical) | Contradicts SOT plan |
| Distinction from JSON | Clear: JSON = "what we'd do", overrides = "what humans forced" | Muddled |

Cost: every read site must consult the override layer. We address this in §6 (centralization) and §6.3 (call-site table).

### 4.2 Three-state universe model

| Dashboard label | Override state | Effective `trading.mode` | Effective `trading.live_enabled` | Behavior |
|---|---|---|---|---|
| 🟢 LIVE | `live` | `live` | `true` | Normal — entries, monitoring, OCO, EOD all active. |
| 🟡 PASSIVE | `passive` | `passive` | `true` | No new entries. Intraday monitor + OCO sync + EOD settlement continue maintaining existing positions. |
| ⚫ DISABLED | `disabled` | `passive` | `false` | Full kill: broker registry returns `None`, executor blocks, monitor skips, OCO sync skips, EOD skips. Stops on broker remain (won't be cancelled by us; they'll fire if hit). |

The override state is the **single thing the operator sets**. The two underlying flags are **derived**, never edited independently from the dashboard. (A future v2.0 migration may collapse the two flags into a single `state` column on `market_state`, but that's out of scope.)

### 4.3 Strategy state model

Strategies are simpler — binary `enabled`/`disabled`, scoped to (universe, strategy). When disabled:

- `cli.py:get_strategies()` skips the strategy at construction time → no signals → no plans → no entries.
- Existing positions in that strategy are NOT touched. They continue to be monitored, stopped, settled. (Disabling a strategy ≠ closing its positions; that's an operator decision, separate.)
- When re-enabled: signals resume next cycle.

## 5. Data model

### 5.1 New tables

```sql
-- ─────────────────────────────────────────────────────────────────────────────
-- config_overrides
-- Active overrides that layer on top of config/active/*.json at read-time.
-- One active row per (scope, key); historical rows kept with active=0.
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS config_overrides (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  scope        TEXT    NOT NULL CHECK(scope IN ('universe','strategy')),
  -- universe: market_id (e.g. 'sp500')
  -- strategy: 'market_id.strategy_name' (e.g. 'commodity_etfs.connors_rsi2')
  key          TEXT    NOT NULL,
  -- For scope='universe': 'live' | 'passive' | 'disabled'
  -- For scope='strategy': 'enabled' | 'disabled'
  state        TEXT    NOT NULL,
  reason       TEXT,                           -- mandatory at API layer; nullable at DB
  created_by   TEXT    NOT NULL,               -- 'human:<username>' | 'system' | 'telegram'
  created_at   TEXT    NOT NULL DEFAULT (datetime('now')),
  -- Optional auto-expiry. NULL = never expires.
  -- When set, a sweep job marks active=0 once datetime('now') > expires_at.
  expires_at   TEXT,
  -- Effective state immediately before this override was applied — used for one-click revert.
  -- For 'universe' scope: 'live'/'passive'/'disabled' (derived from JSON config or prior override)
  -- For 'strategy' scope: 'enabled'/'disabled'
  prev_state   TEXT,
  -- Lifecycle: 1=active (consulted by readers), 0=superseded/reverted/expired (kept for history).
  active       INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
  -- When active=0: which event ended this row.
  ended_at     TEXT,
  ended_reason TEXT CHECK(ended_reason IN ('reverted','expired','superseded') OR ended_reason IS NULL)
);

-- Only one ACTIVE override per (scope, key). Historical rows are unconstrained.
CREATE UNIQUE INDEX IF NOT EXISTS uq_config_overrides_active
  ON config_overrides(scope, key) WHERE active = 1;

-- Sweep job index (find rows due for expiry).
CREATE INDEX IF NOT EXISTS idx_config_overrides_expires
  ON config_overrides(expires_at) WHERE active = 1 AND expires_at IS NOT NULL;

-- Lookup index for read-side resolution (override scoped to a universe or strategy).
CREATE INDEX IF NOT EXISTS idx_config_overrides_lookup
  ON config_overrides(scope, key, active);


-- ─────────────────────────────────────────────────────────────────────────────
-- config_override_audit
-- Immutable append-only log of every override mutation event.
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS config_override_audit (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  ts           TEXT NOT NULL DEFAULT (datetime('now')),
  override_id  INTEGER REFERENCES config_overrides(id),  -- nullable for cross-row events
  scope        TEXT NOT NULL,
  key          TEXT NOT NULL,
  action       TEXT NOT NULL CHECK(action IN ('create','revert','expire','supersede')),
  from_state   TEXT,                                     -- prior effective state
  to_state     TEXT,                                     -- new effective state
  reason       TEXT,
  actor        TEXT NOT NULL,                            -- 'human:<username>' | 'system' | 'telegram'
  source       TEXT NOT NULL CHECK(source IN ('dashboard','cli','telegram','sweep')),
  remote_ip    TEXT,                                     -- best-effort, dashboard only
  payload_json TEXT                                      -- full request body for forensic replay
);

CREATE INDEX IF NOT EXISTS idx_config_override_audit_ts ON config_override_audit(ts DESC);
CREATE INDEX IF NOT EXISTS idx_config_override_audit_key ON config_override_audit(scope, key, ts DESC);

-- Immutability — model copied verbatim from fix_audit_log.
CREATE TRIGGER IF NOT EXISTS config_override_audit_no_update
  BEFORE UPDATE ON config_override_audit
  BEGIN SELECT RAISE(ABORT, 'config_override_audit is immutable (append-only)'); END;

CREATE TRIGGER IF NOT EXISTS config_override_audit_no_delete
  BEFORE DELETE ON config_override_audit
  BEGIN SELECT RAISE(ABORT, 'config_override_audit is immutable (append-only)'); END;
```

### 5.2 Migration

- File: `scripts/migrations/2026-05-05-add-config-overrides.py`
- Pattern: standard idempotent migration (mirror of existing files). DDL above + bump `schema_version` to **29**.
- Also add the same DDL to `db/schema.sql` for fresh installs.
- Apply: `python3 scripts/migrations/2026-05-05-add-config-overrides.py --apply`

### 5.3 Override resolution rules (read-time)

Pseudocode, lives in `utils/config.py` (alongside existing `get_active_config`):

```python
def resolve_universe_state(market_id: str, raw_config: dict) -> tuple[str, bool]:
    """Returns (mode, live_enabled) after applying any active universe override."""
    override = _query_active_override('universe', market_id)
    if override is None:
        return raw_config['trading']['mode'], raw_config['trading']['live_enabled']
    if override['state'] == 'live':
        return 'live', True
    if override['state'] == 'passive':
        return 'passive', True
    if override['state'] == 'disabled':
        return 'passive', False  # mode kept passive so no entries; live_enabled false kills maintenance
    raise ValueError(f"unknown override state: {override['state']}")


def resolve_strategy_enabled(market_id: str, strategy: str, raw_config: dict) -> bool:
    """Returns effective enabled flag after applying any active strategy override."""
    raw_enabled = raw_config['strategies'].get(strategy, {}).get('enabled', False)
    override = _query_active_override('strategy', f'{market_id}.{strategy}')
    if override is None:
        return raw_enabled
    return override['state'] == 'enabled'


def _query_active_override(scope: str, key: str) -> dict | None:
    """Query DB for active, non-expired override. Returns row dict or None."""
    with get_db() as db:
        row = db.execute(
            """SELECT * FROM config_overrides
               WHERE scope=? AND key=? AND active=1
                 AND (expires_at IS NULL OR expires_at > datetime('now'))
               LIMIT 1""",
            (scope, key)
        ).fetchone()
    return dict(row) if row else None
```

### 5.4 Expiry sweep

A periodic job marks `active=0` on overrides past their `expires_at`. Two implementation options:

**Option A — Lazy** (recommended): every read of `_query_active_override` checks `expires_at > now()`. No background job needed. Cost: expired rows linger as `active=1` until rewritten or noticed; UI must filter.

**Option B — Eager**: cron every 5 minutes runs `expire_due_overrides()` which sets `active=0`, `ended_at=now()`, `ended_reason='expired'` on each due row, plus writes an `expire` audit event.

We ship **A first** (zero infra), then add **B** in Phase 2 once the table grows enough to justify the audit cleanliness.

## 6. Read-side integration

### 6.1 Centralization first

`utils/config.get_active_config(market_id)` is the canonical loader. ~80% of call sites already use it. We modify it to apply overrides:

```python
def get_active_config(market_id: str | None = None, apply_overrides: bool = True) -> dict:
    raw = _load_raw_config(market_id)  # = the current load_config body
    if not apply_overrides:
        return raw
    return _apply_overrides(raw, market_id)


def _apply_overrides(raw: dict, market_id: str) -> dict:
    cfg = copy.deepcopy(raw)
    mode, live_enabled = resolve_universe_state(market_id, raw)
    cfg['trading']['mode'] = mode
    cfg['trading']['live_enabled'] = live_enabled
    for strat_name in list(cfg.get('strategies', {}).keys()):
        cfg['strategies'][strat_name]['enabled'] = resolve_strategy_enabled(
            market_id, strat_name, raw
        )
    cfg['_overrides_applied'] = True  # diagnostic marker
    return cfg
```

Two opt-out paths for research code:

1. `get_active_config(market_id, apply_overrides=False)` — returns raw JSON, used by backtest / sweep code paths.
2. `get_raw_config(market_id)` — explicit alias to make intent obvious in research code.

Research/backtest code paths must use `apply_overrides=False` or `get_raw_config`. We document this in the function docstrings.

### 6.2 Bypass-fix sub-task (Phase 1 prerequisite)

Replace direct `json.load(open(...))` calls with `get_active_config()` in:

| File | Line | Current | Target |
|---|---|---|---|
| `scripts/eod_settlement.py` | 53–56 | `json.load(open(path))` | `from utils.config import get_active_config; cfg = get_active_config(market_id)` |
| `scripts/sync_protective_orders.py` | 101–108 | same | same |
| `scripts/reconcile_ledger.py` | 28–35 | same (read-only audit — see note below) | same |
| `services/api/health.py` | 145, 326 | `Path('config/active').glob('*.json')` | iterate market IDs, call `get_active_config(m)` for each |

**Special case — `reconcile_ledger.py`**: this is a read-only audit. It should compare actual broker state against expected effective config, so it MUST consult overrides (otherwise it'll report false positives like "stop missing on disabled universe X — except yes, X is disabled, of course there's no stop maintenance"). Decision: it consults overrides.

### 6.3 Read-side consultation matrix

For each call site, decide: does it consult overrides at runtime?

| Category | File | Line | Reads | Consults overrides? | Notes |
|---|---|---|---|---|---|
| **A. Entry / plan generation** | | | | | |
| | `scripts/execute_approved.py` | 68–73 | `mode` | **Yes** | Already uses `get_active_config`. After §6.1 change, automatic. |
| | `scripts/cli.py:cmd_live_run` | 535–543 | `mode` | **Yes** | Same. |
| | `scripts/cli.py:get_strategies` | 80–100 | `strategies.*.enabled` | **Yes** | Same. |
| | Plan generation (signals → plans) | various | `strategies.*.enabled` | **Yes** | Via `get_active_config`. |
| **B. Position maintenance** | | | | | |
| | `scripts/intraday_monitor.py` | 362–369 | `live_enabled` | **Yes** | Already canonical. |
| | `scripts/sync_protective_orders.py` | 847–851 | `live_enabled` | **Yes** | After §6.2 fix. |
| | `brokers/live_executor.py` | 153, 263 | `live_enabled` | **Yes** | Receives config as arg from caller; caller uses canonical. |
| | `brokers/registry.py` | 78, 111, 138 | `live_enabled` | **Yes** | Same. |
| **C. EOD / settlement / reconciliation** | | | | | |
| | `scripts/eod_settlement.py` | 540–542 | `live_enabled` | **Yes** | After §6.2 fix. |
| | `scripts/health_check.py:_is_inactive` | 60–65 | `live_enabled` | **Yes** | Already canonical. |
| | `scripts/health_check.py` (equity sum loop) | 346–347 | `live_enabled` | **Yes** | Same. |
| | `scripts/regen_stops_held_state.py` | 50–51 | `live_enabled` | **Yes** | Same. |
| | `scripts/reconcile_ledger.py` | — | (full config) | **Yes** | After §6.2 fix. |
| **D. Research / backtest / sweep** | | | | | |
| | `backtest/engine.py:241,248` | | internal `macro_mode` (not `trading.mode`) | N/A | Unrelated. |
| | `backtest/pipeline.py:198–212` | | same | N/A | Unrelated. |
| | `regime/run_gate_backtest.py:36` | | raw file open | **No** | Research path — leave as-is. Use `get_raw_config`. |
| | `regime/backtest.py:28` | | raw file open | **No** | Same. |
| | `scripts/tools/archive/backtest_universes.py:107–108` | | overwrites mode=paper, live_enabled=false | **No** | Already explicitly forces these — leave alone. |
| | `research/portfolio_optimizer.py:478,769,929` | | weight | **No** | Research only. |
| **E. Health checks / heartbeat** | | | | | |
| | `pi-package/atlas-ops/skills/atlas-healthz/scripts/healthz.py` | 269, 271 | mode, live_enabled (display only) | **Yes** | Currently reads raw JSON. Change to canonical. Operator sees effective state. |
| | `scripts/heartbeat_watchdog.py` | 49–62 | (heartbeat only, not config) | N/A | Unaffected. |
| **F. Dashboard / API / display** | | | | | |
| | `services/api/health.py` (universe list) | 145, 326 | mode, live_enabled | **Yes** | After §6.2 fix. Dashboard shows effective state. |
| | `services/api/dashboard.py:_build_dashboard_data` | reads sp500.json | full config | **Yes** | Already canonical (uses raw `json.load` but only to read max_positions etc., not the toggle flags — verify in impl). |
| **G. Telegram bot** | | | | | |
| | `services/telegram_bot.py:build_plan_message` | 270–282 | mode | **Yes** | Already canonical via `get_active_config`. **Bug found inline:** passive mode renders as "🔴 LIVE" — separate fix in §11.4. |

### 6.4 Performance

A naive implementation queries the override DB on every config read. Atlas reads configs frequently in hot loops (intraday monitor, plan generation). To avoid latency:

- **In-process cache** in `utils/config.py` — TTL 5 seconds. Each `get_active_config(market_id)` call within the TTL window returns memoized result. Cache key: `(market_id, apply_overrides_bool)`. Invalidated on TTL expiry only — write APIs do NOT invalidate (5-second eventual consistency is acceptable for toggles; most call sites are cron-driven anyway).
- DB queries are sub-millisecond on the override table (active rows ≤ 30 in steady state); cache is mostly an optimization, not a correctness requirement.

## 7. API surface

All routes mounted under existing `chat_server.py` FastAPI app. Auth: `Depends(check_auth)`. Body validation: Pydantic.

### 7.1 Read endpoints

#### `GET /api/admin/universes`

Returns one row per universe: effective state, source (config | override), open positions, last trade, expiry.

**Response** (200):
```json
{
  "universes": [
    {
      "market_id": "sp500",
      "effective_state": "live",
      "config_state": "live",
      "override": null,
      "open_positions": 3,
      "last_trade_at": "2026-05-04T14:32:01Z",
      "starting_equity": 971,
      "current_equity": 1023.55,
      "version": "v3.2.1"
    },
    {
      "market_id": "commodity_etfs",
      "effective_state": "passive",
      "config_state": "passive",
      "override": null,
      "open_positions": 1,
      "last_trade_at": "2026-04-30T13:30:00Z",
      "starting_equity": 1001,
      "current_equity": 998.20,
      "version": "v1.3-consolidation-passive"
    },
    {
      "market_id": "asx",
      "effective_state": "disabled",
      "config_state": "passive",
      "override": {
        "id": 17,
        "state": "disabled",
        "reason": "ASX market closed for testing toggle UX",
        "created_by": "human:operator",
        "created_at": "2026-05-05T22:00:00Z",
        "expires_at": "2026-06-04T22:00:00Z"
      },
      "open_positions": 0,
      "last_trade_at": null,
      "starting_equity": 1000,
      "current_equity": 1000,
      "version": "v1.0.1-passive"
    }
  ]
}
```

#### `GET /api/admin/strategies`

Returns one row per (universe, strategy) pair where strategy exists in that universe's config. Includes effective state, source, recent PnL contribution, lifecycle status.

**Response** (200):
```json
{
  "strategies": [
    {
      "market_id": "sp500",
      "strategy": "momentum_breakout",
      "effective_enabled": true,
      "config_enabled": true,
      "weight": 0.5,
      "override": null,
      "open_positions": 2,
      "trades_30d": 12,
      "pnl_30d": 47.32,
      "lifecycle": "ACTIVE"
    },
    {
      "market_id": "sp500",
      "strategy": "connors_rsi2",
      "effective_enabled": true,
      "config_enabled": true,
      "weight": 0.5,
      "override": null,
      "open_positions": 1,
      "trades_30d": 18,
      "pnl_30d": -3.10,
      "lifecycle": "WATCH"
    },
    {
      "market_id": "commodity_etfs",
      "strategy": "connors_rsi2",
      "effective_enabled": false,
      "config_enabled": true,
      "weight": 0.2,
      "override": {
        "id": 23,
        "state": "disabled",
        "reason": "Underperforming since recalibration",
        "created_by": "human:operator",
        "created_at": "2026-05-05T22:30:00Z",
        "expires_at": "2026-06-04T22:30:00Z"
      },
      "open_positions": 0,
      "trades_30d": 5,
      "pnl_30d": -8.20,
      "lifecycle": "WATCH"
    }
  ]
}
```

`lifecycle` is read from the existing health-check classification (ACTIVE / WATCH / RETIRED). If unavailable, omit or return `"UNKNOWN"`.

#### `GET /api/admin/override-audit`

Query params: `since` (ISO 8601, optional), `scope` (universe|strategy, optional), `key` (optional), `limit` (default 100, max 500).

**Response** (200):
```json
{
  "audit": [
    {
      "id": 42,
      "ts": "2026-05-05T22:30:00Z",
      "override_id": 23,
      "scope": "strategy",
      "key": "commodity_etfs.connors_rsi2",
      "action": "create",
      "from_state": "enabled",
      "to_state": "disabled",
      "reason": "Underperforming since recalibration",
      "actor": "human:operator",
      "source": "dashboard"
    },
    {
      "id": 43,
      "ts": "2026-05-05T22:00:00Z",
      "override_id": 17,
      "scope": "universe",
      "key": "asx",
      "action": "create",
      "from_state": "passive",
      "to_state": "disabled",
      "reason": "ASX market closed for testing toggle UX",
      "actor": "human:operator",
      "source": "dashboard"
    }
  ],
  "next_cursor": null
}
```

### 7.2 Write endpoints

#### `POST /api/admin/universe/{market_id}/state`

**Request body:**
```json
{
  "state": "passive",
  "reason": "Halting commodity_etfs while we investigate stop slippage",
  "expires_at": "2026-06-05T22:00:00Z",
  "confirm_token": "commodity_etfs"
}
```

- `state`: required, ∈ `{live, passive, disabled}`.
- `reason`: required, min 10 chars, max 500 chars.
- `expires_at`: optional. If omitted, defaults to now + 30 days. To set "permanent", caller passes `expires_at: null` explicitly.
- `confirm_token`: required for production universes (defined in §9), must equal the `market_id` in the URL path. Type-to-confirm pattern.

**Validation errors (HTTP 400)**:
- `state == "disabled"` and there are open positions on that universe → reject with helpful error: `"Cannot disable {market_id} — 3 open positions. Set state=passive first, close positions, then disable."`
- `state` not in allowed set
- `reason` too short/long
- `confirm_token` mismatch (production universes only)
- Same `state` as currently effective → 409 Conflict (no-op rejected to keep the audit log meaningful)

**Auth errors (HTTP 401)**: as `check_auth`.

**Success response (HTTP 200)**:
```json
{
  "ok": true,
  "override_id": 24,
  "market_id": "commodity_etfs",
  "from_state": "passive",
  "to_state": "passive",
  "expires_at": "2026-06-05T22:00:00Z"
}
```

**Side effects:**
1. If a prior active override exists for `(universe, market_id)`, mark it `active=0`, `ended_at=now()`, `ended_reason='superseded'`, write a `supersede` audit event.
2. Insert new `config_overrides` row with `prev_state` = the old effective state.
3. Insert `config_override_audit` row with `action='create'`.
4. Invalidate the in-process config cache (publish a global cache-bump counter).

#### `POST /api/admin/strategy/{market_id}/{strategy}/state`

**Request body:**
```json
{
  "state": "disabled",
  "reason": "Underperforming since 2026-04-29 recalibration; revisit at 30d",
  "expires_at": "2026-06-05T22:30:00Z"
}
```

- `state`: required, ∈ `{enabled, disabled}`.
- `reason`: required as above.
- `expires_at`: optional, defaults to now + 30 days. Pass `null` for permanent.
- No `confirm_token` for strategy-level (lower blast radius — universe still active, just one strategy off).

**Validation errors (HTTP 400)**:
- `strategy` not in `config['strategies']` for that universe → 404 Not Found.
- Same as universe endpoint otherwise.

**Success response (HTTP 200)**: same shape as universe.

**Side effects:** same as universe.

#### `POST /api/admin/override/{override_id}/revert`

Soft-revert: marks override row `active=0`, `ended_at=now()`, `ended_reason='reverted'`, writes audit event with `action='revert'`. The next read of effective state falls back to either the most recent prior override (if any was superseded by this one within the last N days) or to the JSON config.

**Behavior**: pure revert to JSON config — does NOT chain back through superseded overrides. Simpler model. If operator wants to restore a prior override, they create it fresh.

**Request body:**
```json
{
  "reason": "Toggle UX test complete — reverting"
}
```

**Success response (HTTP 200)**:
```json
{
  "ok": true,
  "reverted_override_id": 17,
  "scope": "universe",
  "key": "asx",
  "from_state": "disabled",
  "to_state": "passive",
  "source": "config"
}
```

### 7.3 Idempotency

Not full idempotency keys (FastAPI convention here is "POST = action"). Two safeguards instead:
1. **Same-state rejection** (§7.2): can't create an override matching current effective state. Prevents accidental double-clicks creating duplicate rows.
2. **Type-to-confirm token** for production universes — means a stuck retry can't accidentally re-disable.

### 7.4 Auth

All `/api/admin/*` routes require `Depends(check_auth)`. Usernames captured as `f"human:{_auth.username}"` for actor field. No further authorization (single-user system).

### 7.5 Error response shape

Match existing FastAPI conventions across the codebase: `HTTPException(status_code=N, detail="...")`. The dashboard client unwraps `.detail` for display.

## 8. UI / UX

### 8.1 Tab placement — new "Controls" tab

Add 5th tab to `App.tsx` and `TabBar.tsx`. Position: rightmost (after Remediation). Label: **Controls**. Icon: `⚙` (or whatever Tailwind+inline icon style is used elsewhere).

Rationale: Portfolio / Finance / Research / Remediation are all read-mostly. Controls is the only write-heavy tab. Keeping it visually separated reduces accidental clicks while reading status. Naming "Controls" rather than "Admin" because there's no role distinction — every operator who can see the dashboard can use it.

### 8.2 Tab structure

```
┌──────────────────────────────────────────────────────────────────────────┐
│ Header bar (existing — regime badge, market clock, theme toggle)         │
├──────────────────────────────────────────────────────────────────────────┤
│ Portfolio │ Finance │ Research │ Remediation │ Controls ← new           │
├──────────────────────────────────────────────────────────────────────────┤
│                                                                          │
│ ┌──────────────────────────────────────────────────────────────────────┐ │
│ │ Universes                                                            │ │
│ │ ──────────────────────────────────────────────────────────────────── │ │
│ │ sp500            🟢 LIVE     [config]  $1,023 (3 pos)  [change ▾]   │ │
│ │ sector_etfs      🟡 PASSIVE  [config]  $3,201 (0 pos)  [change ▾]   │ │
│ │ commodity_etfs   🟡 PASSIVE  [config]  $998   (1 pos)  [change ▾]   │ │
│ │ asx              ⚫ DISABLED [override exp 2026-06-04] [revert ↺]   │ │
│ │ crypto           🟡 PASSIVE  [config]  $0     (0 pos)  [change ▾]   │ │
│ │ gold_etfs        🟡 PASSIVE  [config]  $0     (0 pos)  [change ▾]   │ │
│ │ treasury_etfs    🟡 PASSIVE  [config]  $0     (0 pos)  [change ▾]   │ │
│ │ defensive_etfs   🟡 PASSIVE  [config]  $0     (0 pos)  [change ▾]   │ │
│ └──────────────────────────────────────────────────────────────────────┘ │
│                                                                          │
│ ┌──────────────────────────────────────────────────────────────────────┐ │
│ │ Strategies (grouped by universe)                                     │ │
│ │ ──────────────────────────────────────────────────────────────────── │ │
│ │ ▼ sp500                                                              │ │
│ │     momentum_breakout    ✓ ENABLED    w=0.50   ACTIVE  [toggle]     │ │
│ │     connors_rsi2         ✓ ENABLED    w=0.50   WATCH   [toggle]     │ │
│ │     mean_reversion       — disabled   w=0      RETIRED [toggle]     │ │
│ │     ... (collapsed)                                                  │ │
│ │ ▶ commodity_etfs                                                     │ │
│ │ ▶ sector_etfs                                                        │ │
│ │ ▶ asx                                                                │ │
│ └──────────────────────────────────────────────────────────────────────┘ │
│                                                                          │
│ ┌──────────────────────────────────────────────────────────────────────┐ │
│ │ Recent changes (last 20)                                             │ │
│ │ ──────────────────────────────────────────────────────────────────── │ │
│ │ 22:30  operator  strategy commodity_etfs.connors_rsi2 enabled→disabled │
│ │        Reason: "Underperforming since recalibration"  [revert]      │ │
│ │ 22:00  operator  universe asx passive→disabled                       │ │
│ │        Reason: "ASX market closed for testing toggle UX" [reverted] │ │
│ │ ...                                                                  │ │
│ └──────────────────────────────────────────────────────────────────────┘ │
└──────────────────────────────────────────────────────────────────────────┘
```

### 8.3 State badges

| Effective state | Badge | Tailwind classes |
|---|---|---|
| LIVE | 🟢 LIVE | `bg-green-500/15 text-green-400 border-green-500/30` |
| PASSIVE | 🟡 PASSIVE | `bg-yellow-500/15 text-yellow-400 border-yellow-500/30` |
| DISABLED | ⚫ DISABLED | `bg-zinc-500/15 text-zinc-400 border-zinc-500/30` |
| ENABLED (strategy) | ✓ | `text-green-400` |
| disabled (strategy) | — | `text-zinc-500` |

### 8.4 Source pill

Next to each state badge:
- `[config]` — neutral grey pill, means JSON config is the source.
- `[override exp YYYY-MM-DD]` — amber pill if expiry within 7 days, otherwise neutral. Tooltip on hover shows full reason + creator + created_at.

### 8.5 Change-state modal (universe)

Triggered by clicking `[change ▾]`. Inline modal (no library — follow PendingPromotionsWidget convention; absolute-positioned div with backdrop).

```
┌─ Change state for sp500 ─────────────────────────────────────┐
│                                                              │
│ Current effective: 🟢 LIVE  (source: config)                │
│                                                              │
│ New state:                                                   │
│   ( ) 🟢 LIVE       — Normal trading                         │
│   ( ) 🟡 PASSIVE    — No new entries; maintain existing      │
│   (•) ⚫ DISABLED   — Full kill, no maintenance              │
│                                                              │
│ Reason (required, ≥10 chars):                                │
│ ┌──────────────────────────────────────────────────────────┐│
│ │ Halting sp500 entries while investigating overnight gap  ││
│ │ behavior; positions will continue to be monitored.       ││
│ └──────────────────────────────────────────────────────────┘│
│                                                              │
│ Auto-expire after:                                           │
│   (•) 30 days  ( ) 7 days  ( ) 24 hours  ( ) Never          │
│                                                              │
│ ⚠ Setting DISABLED is blocked while there are open          │
│   positions. Currently: 3 open. Set PASSIVE first, close,    │
│   then DISABLED.                                             │
│                                                              │
│ ☐ I understand this affects live trading.                    │
│                                                              │
│ Type the universe name to confirm: ┌────────────┐            │
│                                    │            │            │
│                                    └────────────┘            │
│                                                              │
│ [Cancel]                                  [Apply change]    │
└──────────────────────────────────────────────────────────────┘
```

**Apply button enabled only when:**
- A different state is selected.
- Reason has ≥10 chars.
- Checkbox is ticked.
- Confirm-text equals `market_id` (production universes only — see §9.1).
- No open-position blocker (for `disabled` choice).

**On submit:**
- POST to `/api/admin/universe/{market_id}/state`.
- Inline busy state on button (`'…'`).
- Success: invalidate `qk.admin.universes()` query, close modal, show inline green toast "✓ sp500 → DISABLED".
- Error: show inline red toast with `error.detail`, leave modal open.

### 8.6 Change-state modal (strategy)

Smaller, no type-to-confirm:

```
┌─ Toggle commodity_etfs.connors_rsi2 ─────────────────────────┐
│                                                              │
│ Current effective: ✓ ENABLED  (source: config)              │
│ Recent (30d):    5 trades, -$8.20 PnL, lifecycle WATCH      │
│                                                              │
│ New state: (•) DISABLED                                      │
│                                                              │
│ Reason (required):                                           │
│ ┌──────────────────────────────────────────────────────────┐│
│ │ Underperforming since recalibration; revisit at 30d      ││
│ └──────────────────────────────────────────────────────────┘│
│                                                              │
│ Auto-expire after: (•) 30 days  ( ) 7 days  ( ) Never        │
│                                                              │
│ ☐ I understand this affects live trading.                    │
│                                                              │
│ [Cancel]                                  [Disable]         │
└──────────────────────────────────────────────────────────────┘
```

### 8.7 Revert button

Inline next to override pill. One-click → confirmation toast: "Revert override on commodity_etfs.connors_rsi2? (will fall back to config-enabled)" with [Confirm revert] [Cancel]. POST to `/api/admin/override/{id}/revert`, invalidate queries.

### 8.8 Open-positions guard (UI)

When operator picks `DISABLED` for a universe with open positions, the modal:
- Disables the Apply button.
- Shows inline red banner: "⚠ Cannot disable while {N} positions are open. Set PASSIVE first, close positions, then return to disable."
- Provides a quick link to the Portfolio tab filtered by that universe.

This duplicates the API-side check (§7.2) for instant feedback; the API check remains authoritative.

### 8.9 Recent changes panel

Read from `/api/admin/override-audit?limit=20`. Each row:
- Timestamp (relative, e.g. "5m ago"), absolute on hover.
- Actor (strip `human:` prefix for display).
- Scope + key.
- `from→to` state transition.
- Truncated reason (full on hover).
- `[revert]` button if action was `create` and override is still active. Greyed `[reverted]` label if already reverted.

### 8.10 Polling

`useQuery` for `/api/admin/universes` and `/api/admin/strategies`: `staleTime: 30_000`, `refetchInterval: 30_000`. Mutation `onSuccess` invalidates immediately so the operator sees their change reflected within ~1s.

## 9. Safety guardrails

### 9.1 Production-universe list

Type-to-confirm required for these (computed dynamically from current effective state):

```python
# Pseudocode — compute at endpoint time, not hardcoded.
def is_production(market_id: str) -> bool:
    cfg = get_active_config(market_id)
    return cfg["trading"]["mode"] == "live" and cfg["trading"]["live_enabled"]
```

If a universe is currently `live`, it's production → type-to-confirm required. `passive` and `disabled` universes don't require type-to-confirm (lower blast radius — re-enabling a passive universe to live still requires the checkbox + reason).

Today (2026-05-05) only `sp500` is production. The check is dynamic so this scales as more universes go live.

### 9.2 Open-positions block on `disabled`

Both API and UI enforce this. Rationale: setting `live_enabled=false` while positions are open creates a "zombie state" where:
- Positions exist on broker.
- OCO brackets exist on broker.
- But Atlas's monitor / EOD / sync scripts skip the universe.
- If broker-side stops fire, fills happen but Atlas doesn't reconcile until the override is lifted — creating PnL accounting confusion.

By forcing `passive` first (which keeps stops + monitoring + EOD running), then close positions (executes manual exit), THEN `disabled`, we maintain accounting integrity. The 3-step flow is intentional friction.

### 9.3 In-flight plans on universe disable

Plans that have been generated but not executed live in `plans/{market_id}/{trade_date}.json` until executed by `execute_approved.py`. When a universe goes `live` → `passive` or `live` → `disabled`:

- `execute_approved.py` already checks `mode != "live"` at line 68 and returns early. Existing plans for today simply won't execute.
- We do NOT delete the plan files. They remain on disk for audit; they're harmless.
- When the universe returns to `live`, those plan files are likely stale (date-mismatched). The next plan-generation cycle creates fresh ones.

Decision: no special handling. The existing entry-blocker behavior is sufficient. Document this in the runbook.

### 9.4 OCO brackets on universe disable

When `live` → `disabled`:
- Broker-side OCO brackets remain. They will fire if hit.
- Atlas skips the universe entirely → no monitoring → no maintenance → if a stop fires, Atlas doesn't see it until reconcile runs (and reconcile runs always per §6.2 fix).

When `live` → `passive`:
- Broker-side OCO brackets remain.
- Intraday monitor + sync_protective_orders + EOD all continue.
- Stops may be re-priced by the monitor (trailing stop logic).
- This is the SAFE transition. Recommended for any state change away from `live`.

When `passive` → `disabled`:
- Broker-side OCO brackets remain.
- No more maintenance.
- Operator MUST close positions before setting `disabled` (enforced by §9.2 guard).

Document this state-transition matrix in the runbook.

### 9.5 Heartbeat watchdog interaction

`scripts/heartbeat_watchdog.py` reads `heartbeat.json`, not the trading config. It alerts when expected services don't heartbeat within their SLA window.

When a universe goes `disabled`, its associated services (intraday_monitor for that market, etc.) skip work — but they still need to emit heartbeats so the watchdog doesn't false-alarm. Verify in implementation: each early-return path should call `record_heartbeat()` (or equivalent) before returning.

Action item for impl: audit the early-return paths in:
- `scripts/intraday_monitor.py:362`
- `scripts/sync_protective_orders.py:847`
- `scripts/eod_settlement.py:540`
- `scripts/regen_stops_held_state.py:50`

Each should record a `skipped_disabled` heartbeat or equivalent so the watchdog doesn't false-alarm.

### 9.6 Reversibility

Every change is one-click revertable via the audit panel `[revert]` button OR via `POST /api/admin/override/{id}/revert`. Revert behavior:
- Marks the override `active=0`.
- Falls back to JSON config (does NOT chain back through prior overrides — see §7.2 rationale).
- Writes audit event.
- The change is itself reversible: re-create the same override.

### 9.7 Override TTL default

30 days. Rationale: most operational toggles are diagnostic ("disable while we investigate X") or temporary ("pause sector_etfs during consolidation"). 30 days forces re-confirmation, prevents toggles from silently lingering past their original justification.

Operator can pick 24h / 7d / 30d / Never from the modal. "Never" is allowed but discouraged via UI styling (last option, dimmer). Audit log preserves the chosen expiry.

### 9.8 Concurrent edits

If two operators hit the modal simultaneously and both submit, the second submit hits the same-state check (§7.3) and may succeed if the first changed state, or get a 409 if a second supersede tries to apply when the override is already at the desired state. Either is acceptable — the audit log shows both attempts, the final state is well-defined.

DB-level: the `UNIQUE INDEX uq_config_overrides_active` ensures we never have two active overrides for the same `(scope, key)`. The supersede-then-insert dance happens inside a single DB transaction in the API handler.

### 9.9 Audit log integrity

`config_override_audit` is append-only via BEFORE-UPDATE/DELETE triggers. No code path can modify or delete history. Operator can only add new events.

If we need to "undo" a recorded event due to a bug, it's done via INSERT of a corrective `revert` event with reason explaining the correction — never by mutating history.

## 10. Phased rollout

### Phase 1 — Backend (DB + API + audit, no UI)

**Scope:**
- Migration `2026-05-05-add-config-overrides.py` adds the two tables + indices + triggers; bumps schema_version to 29.
- `db/schema.sql` updated for fresh installs.
- `utils/config.py` extended with `_apply_overrides`, `resolve_universe_state`, `resolve_strategy_enabled`, in-process cache. `get_active_config(market_id, apply_overrides=True)` is the new signature.
- 3 bypass scripts (`eod_settlement.py`, `sync_protective_orders.py`, `reconcile_ledger.py`) refactored to use canonical loader.
- `services/api/health.py` glob-reads replaced with canonical loader iterations.
- `pi-package/.../healthz.py` switched to canonical.
- New `services/api/admin.py` router with all 5 endpoints.
- Router included in `chat_server.py`.
- Heartbeat audit (§9.5): each early-return path records a `skipped_disabled` heartbeat or equivalent.
- **Drive-by:** `services/telegram_bot.py:270–282` passive-mode rendering fix (§11.4).

**Tests:**
- Unit: `utils/config.py` override resolution — table-driven covering all 9 combinations of (override_state, json_state, expiry).
- Unit: API endpoint validation (state value, reason length, confirm_token, open-positions block).
- Integration: full flip flow — POST override, read effective state via `get_active_config`, verify mode/live_enabled/strategy.enabled all reflect; revert, verify fallback; expire (set `expires_at` in past, read).
- Integration: audit row written with correct from/to/reason/actor for each of `create`, `revert`, `supersede`.
- Smoke: with `commodity_etfs` set to `passive` in JSON, override to `disabled`, run `intraday_monitor.py` against test broker — verify it skips the universe. Then `live` override, verify it runs.

**Acceptance:**
- All existing tests still pass.
- New tests pass.
- Manual: hit each new endpoint with `curl` from the dashboard host, verify shape matches §7.
- No regressions: run `python3 scripts/health_check.py` — output unchanged from baseline (no overrides set yet).

### Phase 2 — UI behind feature flag

**Scope:**
- New `ControlsTab.tsx` + supporting components (`UniverseRow.tsx`, `StrategyRow.tsx`, `ChangeStateModal.tsx`, `RevertButton.tsx`, `RecentChangesPanel.tsx`).
- New `api/queries.ts` hooks: `useAdminUniverses`, `useAdminStrategies`, `useOverrideAudit`, `useChangeUniverseState`, `useChangeStrategyState`, `useRevertOverride`.
- New `qk.admin.*` keys.
- TabBar 5th tab; App.tsx integration.
- Feature flag: env var `VITE_ENABLE_CONTROLS_TAB=true` at build time; tab hidden if false.
- Build with flag off → ship to production. No regression in existing tabs.
- Build with flag on → dogfood on staging / local dev.

**Dogfood plan:**
- Use `asx` as the test universe (already passive; low blast radius).
- Operator (manual): toggle `asx` to `disabled`, verify dashboard reflects it within 30s, verify `get_active_config('asx')` returns `live_enabled=False`. Revert. Verify it falls back to JSON config.
- Operator (manual): toggle one (universe, strategy) pair to `disabled`. Pick a real one (e.g. `commodity_etfs.mean_reversion`). Same flow.
- Audit log inspection: every action results in exactly one audit row.

**Acceptance:**
- 1 week of dogfooding without surprises.
- Operator confidence: "I can use this for sp500 if I needed to."

### Phase 3 — GA

**Scope:**
- Flip the build flag to `true` for production.
- Document in runbook: `docs/runbook/dashboard-controls.md` covering:
  - When to use passive vs disabled.
  - State-transition matrix (live→passive→disabled→passive→live).
  - How to handle in-flight positions when transitioning.
  - How to read the audit log.
  - How to revert.
- Telegram alert on every override creation (low priority — informational).
- (Optional) Add the eager expiry sweep job (Phase 2 → 3 transition).

**Acceptance:**
- All operators trained on the workflow.
- One real production flip done with the dashboard (e.g., disabling a research-only universe), audit row captured, no incidents.

## 11. Open questions for engineering / human review

### 11.1 In-process cache invalidation strategy

We propose a 5-second TTL with no write-side invalidation (§6.4). Engineering pushback expected: "the operator clicks toggle, refreshes the dashboard, and still sees the old state for 5 seconds." Acceptable? Or invest in cache-bump on write?

**Recommendation:** ship with TTL only; revisit if dogfooding reveals confusion. The dashboard already polls every 30s — a 5-second cache window is invisible at that cadence. If operator latency UX matters, add a `bump_cache()` call in the API write handlers — cheap and isolated.

### 11.2 Should overrides also have a "comment thread"?

In the current design, the only way to annotate an override is the initial `reason` field. No way to add updates ("tried fix X, didn't work, leaving disabled for another week"). Would a comments table add value, or YAGNI?

**Recommendation:** YAGNI. If operators want to comment, they create a fresh override with an updated reason — supersedes the old one, audit log captures both.

### 11.3 Multi-step "schedule a re-enable"?

Operator might want: "disable now, auto-enable in 7 days" rather than the current "auto-expire to JSON state in 7 days." The auto-expire works for most cases (JSON state is usually `live`), but if JSON is `passive` and operator wants the override to expire to `live`, they're stuck.

**Recommendation:** ship without this. If it comes up, add an `expires_to_state` column later.

### 11.4 Telegram bug fix (drive-by)

Audit found: `services/telegram_bot.py:270–282` `build_plan_message()` renders `passive` mode as "🔴 LIVE" because the `elif` chain falls through. This is a pre-existing bug unrelated to this spec but uncovered during the audit. Recommend fixing as part of Phase 1 (one-line patch):

```python
elif mode == "passive":
    mode_str = "⏸ PASSIVE"
```

Add to Phase 1 acceptance.

### 11.5 Strategy lifecycle source

The Strategies UI shows `lifecycle: ACTIVE/WATCH/RETIRED`. Where does this come from? `scripts/health_check.py` produces this classification, but it's not currently persisted to a queryable table — it's emitted to logs / Telegram. Does this spec need to:
- (a) Persist health-check classifications to a new `strategy_lifecycle` table?
- (b) Compute lifecycle on-demand in the `/api/admin/strategies` handler?
- (c) Skip lifecycle column in v1 (just show effective state + 30d PnL)?

**Recommendation:** (c) for v1. Adding lifecycle persistence is a separate, larger task that the health-check rework should own.

### 11.6 Per-strategy override granularity beyond enabled/disabled

Per-strategy overrides currently are binary on/off. Operators might want "disable only for new entries, but maintain stops on existing positions for this strategy" — symmetric with the universe-level passive vs disabled distinction.

**Recommendation:** out of scope for v1. The universe-level passive/disabled distinction handles 95% of the operational cases. If a strategy needs partial disable, it's currently achievable by setting weight=0 in JSON (no new entries) while keeping enabled=true. We don't need a UI for that.

### 11.7 Should `crypto.json` be excluded from the universe list?

Crypto is currently passive and has no live integration with Alpaca (Alpaca is equities-only). Showing it in the Controls tab might confuse operators. Options:
- (a) Show all 8 universes.
- (b) Show only universes with `live_enabled=true` ever in their history (roughly: sp500, sector_etfs, commodity_etfs).
- (c) Operator-configurable hide-list.

**Recommendation:** (a) for transparency. Operators see everything; the state badges make passive/disabled status obvious.

### 11.8 Backward-compat on local config loaders

Phase 1 refactors 3 scripts to use `get_active_config`. Risk: subtle behavior change if those scripts relied on a specific raw-file behavior (e.g., reading a config that doesn't exist returns a specific error).

**Recommendation:** during refactor, preserve the original error semantics by wrapping `get_active_config(market_id)` calls in the same try/except shape as the original `json.load(open(...))` block. Test each refactored script against its current cron run output (diff before/after).

## 12. Appendix

### 12.1 File inventory — new and modified

**New files:**
- `scripts/migrations/2026-05-05-add-config-overrides.py`
- `services/api/admin.py` (FastAPI router)
- `dashboard-ui/src/components/controls/ControlsTab.tsx`
- `dashboard-ui/src/components/controls/UniverseRow.tsx`
- `dashboard-ui/src/components/controls/StrategyRow.tsx`
- `dashboard-ui/src/components/controls/ChangeStateModal.tsx`
- `dashboard-ui/src/components/controls/RevertButton.tsx`
- `dashboard-ui/src/components/controls/RecentChangesPanel.tsx`
- `tests/test_config_overrides.py`
- `tests/test_admin_api.py`
- `docs/runbook/dashboard-controls.md` (Phase 3)

**Modified files:**
- `db/schema.sql` — append new DDL.
- `utils/config.py` — extend `get_active_config`, add resolution helpers + cache.
- `services/chat_server.py` — include admin router.
- `scripts/eod_settlement.py` — switch to canonical loader.
- `scripts/sync_protective_orders.py` — same.
- `scripts/reconcile_ledger.py` — same.
- `services/api/health.py` — switch to canonical loader.
- `pi-package/atlas-ops/skills/atlas-healthz/scripts/healthz.py` — switch to canonical.
- `services/telegram_bot.py:270–282` — passive mode rendering fix (drive-by).
- `dashboard-ui/src/App.tsx` — 5th tab integration.
- `dashboard-ui/src/components/layout/TabBar.tsx` — 5th tab entry.
- `dashboard-ui/src/api/keys.ts` — `qk.admin.*`.
- `dashboard-ui/src/api/queries.ts` — new hooks.

### 12.2 Out-of-scope follow-ups

- Editing arbitrary config keys beyond on/off (risk parameters, ATR multipliers, weights).
- Bulk strategy enable/disable across all universes.
- RBAC / multi-user permissions.
- Mobile-friendly UI.
- Telegram bot integration to flip toggles via chat command.
- Strategy lifecycle persistence (see §11.5).
- Eager expiry sweep job (see §5.4).
- Consolidating `market_state` table with `config_overrides` (see §3.3).

### 12.3 Reference: existing patterns to mirror

- DB audit: `fix_audit_log` table (immutability triggers, actor convention).
- API write+confirm: `services/api/promotions.py` (read endpoint + 2 write endpoints with HTTP semantics 400/404/409/500).
- Auth: `services/auth.py:check_auth` dependency.
- Frontend action panel: `dashboard-ui/src/components/research/PendingPromotionsWidget.tsx` (POST, busy state, inline toast, query invalidation).
- Migration: `scripts/migrations/*.py` (idempotent DDL + schema_version bump + `--apply` flag).

---

**END OF SPEC.**
