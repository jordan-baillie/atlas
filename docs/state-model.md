# Atlas State Model — Canonical Sources of Truth

> Phase B.1 (2026-04-29). Background: `reports/atlas-streamlining-audit-*-2026-04-29.md`

## Architecture Rule

| Data Domain | Canonical Source | Derived / Cache |
|---|---|---|
| Trade lifecycle (open/closed/P&L) | SQLite `trades` table | `brokers/state/live_*.json` |
| Equity & drawdown history | SQLite `equity_curve` | JSON `equity_history` field |
| Protective order metadata | SQLite `position_protective_orders` | JSON `stop_order_id`/`tp_order_id` fields |
| Broker fills / order history | SQLite `broker_orders` | (none) |
| Live holdings / real-time prices | Alpaca broker API | — |
| JSON state files | **Read-only derived caches** (Phase B target) | — |

**SQLite is authoritative for everything Atlas _computes or records_.**  
**The broker is authoritative for everything it _holds or executes_.**  
JSON state files are _convenience caches_ — they should be rebuildable from SQLite
and should never be the only place a trade record lives.

## Current Phase (B.1): Monitored Dual-Write

The JSON files are still written by multiple code paths (`live_executor.py`,
`reconcile_positions.py`, `sync_protective_orders.py`). Phase B.1 does NOT
remove those writes — it:

1. **Enforces DB invariants via CHECK constraints** so SQLite rejects logically
   impossible rows at write time.
2. **Monitors for JSON↔SQLite drift** (observational alert, no auto-fix) via
   `scripts/state_drift_detector.py`.

Phase B.2 will consolidate all reconciliation through `core/reconcile.py` and
demote JSON files to read-only caches rebuilt from SQLite.

## CHECK Constraints on `trades` (Phase B.1)

All five constraints are DB-enforced; violations raise `sqlite3.IntegrityError`.

| ID | Constraint | SQL |
|---|---|---|
| C1 | Closed trades must have exit fields | `status != 'closed' OR (exit_price IS NOT NULL AND exit_date IS NOT NULL)` |
| C2 | Open trades must have valid entry fields | `status != 'open' OR (entry_price > 0 AND shares > 0)` |
| C3 | Stop price direction guard (long) | `stop_price IS NULL OR (long AND stop < entry) OR (short AND stop > entry)` |
| C4 | Exit date after entry date | `exit_date IS NULL OR exit_date >= entry_date` |
| C5 | Status domain | `status IN ('open','closed','cancelled','pending')` |

C3 and C4 were pre-existing (Apr-27 audit, Phase-0 MU close). C1, C2, C5 were
added by migration `scripts/migrations/2026-04-29-trades-check-constraints.py`.

## Table Writers (trades table)

| Column(s) | Primary Writer | Secondary Writers |
|---|---|---|
| `status` | `live_executor._execute_exit` | `eod_settlement`, `reconcile_positions --fix` |
| `exit_price`, `exit_date`, `pnl` | `atlas_db.record_trade_exit()` | `eod_settlement` (via same helper) |
| `stop_price` | `live_executor._execute_entry` | `reconcile_positions`, `reconcile_ledger` |
| `entry_price`, `shares` | `atlas_db.record_trade_entry()` | `reconcile_positions --fix` |
| `stop_order_id`, `tp_order_id` | `sync_protective_orders` | `live_executor` |
| `regime_at_entry/exit` | `live_executor` | `eod_settlement` |
| `mae`, `mfe` | `atlas_db.record_trade_exit()` via `_compute_and_fill_mae_mfe` | `scripts/backfill_trades.py` |

## JSON Drift Detector

`scripts/state_drift_detector.py` compares `brokers/state/live_<market>.json`
against SQLite open trades for `sp500`, `commodity_etfs`, `sector_etfs`.

Drift classes:
- **orphan_in_json** — JSON has ticker, SQLite does not → stale JSON entry
- **orphan_in_sqlite** — SQLite has ticker, JSON does not → MU-class ghost
- **value_drift** — both present but `entry_price`/`shares`/`stop_price` differ

Behaviour: observational only (no auto-fix). Telegram alert on first detection,
6-hour cooldown thereafter. Exit 1 on any drift, exit 0 if clean.

## Phase B.2 Plan (next)

When `core/reconcile.py` (Worker G) is complete:

1. All write paths (`live_executor`, `eod_settlement`, `reconcile_*`) route
   through `core/reconcile.py` which writes SQLite first, JSON second.
2. JSON write step is made optional (flag-gated).
3. `state_drift_detector` cooldown is tightened to 1h; any drift becomes a
   P0 incident (not a monitoring advisory).
4. `live_executor.py` JSON writes are removed; state is re-derived from SQLite
   on startup.
