# Atlas Reconciliation — Design Doc

**Phase:** B.2 (shadow mode setup)
**Date:** 2026-04-29
**Status:** Shadow — 7-day validation before cutover
**References:** `reports/atlas-streamlining-audit-engineering-2026-04-29.md` (§3, §4.1),
               `reports/atlas-streamlining-audit-planning-2026-04-29.md` (Proposal 5)

---

## 1. Why We Need Consolidation

Atlas currently has **4+ reconcile code paths** that each implement their own merge
order and write to overlapping stores:

| Script | Direction | Writes | Issues |
|---|---|---|---|
| `reconcile_ledger.py` | broker→SQLite | SQLite trades, JSON state | Uses inferred prices; writes JSON |
| `reconcile_positions.py` | broker→JSON→SQLite | JSON state, SQLite trades | Cross-market ghost rows (FCX class, 2026-04-22) |
| `reconcile_sqlite_to_broker.py` | JSON→SQLite | SQLite trades | Wrong direction; reopens stale closed trades |
| `live_executor.reconcile_entry_fills` | broker→SQLite | SQLite trades | stop=0 ghost rows; fires every 15m |

The root cause of the persistent drift (4.1 fix-commits/day over 60 days) is that
**the same data has 4 canonical sources** with no enforced write order. Every conflict
between them generates a new fix commit and a new defensive guard.

---

## 2. The New Model: One Direction

```
broker (Alpaca)
  │
  ▼ sync_broker_orders.py (hourly)
broker_orders table  ← source-of-truth for historical fill prices
  │
  ▼ core/reconcile.py (reconcile_fills)
SQLite trades table  ← source-of-truth for open/closed position state
  │
  ▼ (read-only derived)
brokers/state/live_*.json ← derived cache; NEVER written to by reconcile paths
```

**Key invariants:**
1. **broker_orders is written only by `sync_broker_orders.py` and `reconcile_fills`** —
   never by executor, never by EOD settlement.
2. **SQLite `trades` is written only by `reconcile_fills` and `live_executor`** —
   reconcile_positions is REPORT-ONLY.
3. **JSON state files are never written by reconcile paths** — they are written only by
   `live_executor` and read by the dashboard.

---

## 3. The Two New Functions

### `reconcile_fills(market_id, broker, db, dry_run=True)`

Syncs broker order history → `broker_orders` → `trades`.

Algorithm:
1. Pull `broker.get_history_orders(days=30)`; filter to market's tickers
2. For each order: upsert into `broker_orders`; track what's new (`fills_added`)
   vs what changed (`fills_updated`)
3. For newly FILLED BUY orders with no open `trades` row: `record_trade_entry`
   (`strategy='reconcile_fill'`; stop/TP null — filled in by `sync_protective_orders`)
4. For newly FILLED SELL orders with a matching open `trades` row: `record_trade_exit`

**OCO note:** only FILLED orders trigger trade actions; CANCELLED/ACCEPTED orders
are ignored. The canceled leg of an OCO pair is never status=filled, so it's safe.

### `reconcile_positions(market_id, broker, db, dry_run=True)`

Report-only comparison of live broker positions vs SQLite `trades WHERE status='open'`.

Detects:
- `BROKER_ORPHAN` — broker holds a position, no open SQLite trade
- `SQLITE_ORPHAN` — SQLite has open trade, broker has no position (MU class)
- `QTY_DRIFT` — both present, share counts differ

**No auto-fix in Phase B.2.** Phase B.3+ will add `--fix` mode once shadow
validation confirms detection accuracy.

---

## 4. Shadow Mode

For 7 days, both functions run **alongside the existing scripts** in `dry_run=True`
mode. Results are compared and divergences alerted.

### Reading divergence reports

```sql
-- View last 7 days of shadow runs
SELECT ts, market, new_drift_count, old_drift_count, divergence_count,
       divergence_detail_json
FROM reconcile_shadow_runs
ORDER BY ts DESC LIMIT 50;
```

```bash
tail -100 logs/reconcile_shadow.log
```

**Divergence = 0** means the new module and the old scripts agree on the state of
the world. This is the success criterion.

**Divergence > 0** means one of:
- New module finds drift the old scripts missed (new detections — usually good)
- New module misses drift the old scripts found (regression — investigate)
- Log parsing of old scripts failed (false positive — check log format)

### Shadow cron entry (add manually after merge)

```cron
# Shadow reconcile — runs every 30 min during US market window (UTC 0-7, Tue-Sat)
*/30 0-7 * * 2-6  /usr/bin/flock -n /tmp/reconcile_shadow.lock \
    bash -c 'cd /root/atlas && timeout 5m python3 scripts/reconcile_shadow.py \
    --once' >> /root/atlas/logs/reconcile_shadow.log 2>&1
```

---

## 5. Cutover Plan

**Day 0 (today, 2026-04-29):** Phase B.2 deployed. Shadow mode active.

**Day 1–6:** Shadow runs every 30 minutes. Review `reconcile_shadow_runs` table daily.
Zero divergence required for 7 consecutive days.

**Day 7 (if clean):** Phase B.3 cutover:
1. Disable cron entries for `reconcile_ledger.py`, `reconcile_positions.py`,
   `reconcile_sqlite_to_broker.py`
2. Replace their cron entries with `core/reconcile.py` calls (non-dry_run)
3. Delete (or archive to `scripts/archive/`) the old scripts
4. Add `--fix` mode to `reconcile_positions` (auto-corrects JSON state from broker)

**Phase B.3+ (auto-fix + JSON state retirement):**
- `reconcile_positions` gets `--fix` flag that updates JSON state from broker truth
- `brokers/state/live_*.json` transitions to read-derived cache (no longer written
  by executor paths)
- `reconcile_sqlite_to_broker.py` (wrong-direction) deleted

---

## 6. Files

| File | Purpose |
|---|---|
| `core/reconcile.py` | Canonical module (~530 LOC) |
| `scripts/reconcile_shadow.py` | Shadow runner + divergence comparison |
| `scripts/migrations/2026-04-29-add-reconcile-shadow-runs.py` | DB migration |
| `tests/test_core_reconcile.py` | 12 tests (mock broker, real isolated DB) |
| `docs/reconcile.md` | This document |

---

## 7. Known Limitations (Phase B.2)

- `reconcile_fills` creates trades with `strategy='reconcile_fill'` and `stop=None`;
  `sync_protective_orders` must be run afterward to place stops
- Log parsing of old scripts is regex-based (brittle); divergence from log format
  changes is a known false-positive source  
- `reconcile_positions` is report-only; `BROKER_ORPHAN` detection disabled when
  universe data is unavailable (weekend/stale cache)
- Partial fills: broker_orders upsert handles fill_qty progression correctly, but
  if the same ticker has multiple concurrent orders (scale-in), only one `trades`
  row is created (UNIQUE constraint); second fill is silently skipped
