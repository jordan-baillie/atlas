# scripts/tools/archive/

One-shot and forensic scripts that have been completed and are no longer
referenced by any cron job, import, or documentation. Kept for historical
reference but not part of active codebase.

Archived: 2026-04-29 (Phase A.6.b)

| File | Date Created | Purpose | Reason Archived |
|------|-------------|---------|-----------------|
| `sweep_universes.py` | 2026-04-02 | One-shot universe sweep | Superseded by rebuild_universe.py + cron timer |
| `verify_health_tables.py` | 2026-04-07 | Smoke-test for health tables | One-shot verification, tables confirmed |
| `vol_scaling_check.py` | 2026-04-07 | Vol scaling audit | One-shot audit, completed |
| `optimizer_promote.py` | 2026-04-08 | CLI wrapper for PortfolioOptimizer promote | Replaced by research_promote.py |
| `migrate_add_stop_order_id.py` | 2026-04-13 | Add stop_order_id column migration | Applied; column now in schema.sql |
| `close_mrvl_orphan.py` | 2026-04-13 | One-time MRVL orphan cleanup | Completed (trade #117 closed) |
| `close_mrvl.py` | 2026-04-13 | MRVL position close helper | Superseded by close_mrvl_orphan.py |
| `backfill_fred.py` | 2026-04-13 | One-shot FRED data backfill | Backfill completed |
| `backfill_equity_realized.py` | 2026-04-14 | Backfill equity curve realized P&L | Backfill completed |
| `backtest_universes.py` | 2026-04-20 | Ad-hoc universe backtest runner | Superseded by research_runner.py |
| `investigate_held_stops.py` | 2026-04-22 | Forensic held-stop investigation | Root cause found, fix in sync_protective_orders.py |
| `fix_xly_contamination.py` | 2026-04-22 | Fix XLY universe contamination | Applied; migration 2026-04-22-fix-trade-universe-mismatches.py covers this |
| `backfill_position_protective_orders.py` | 2026-04-29 | Backfill position_protective_orders table | Applied (Phase A.1, 4 rows seeded) |

## Restoring a script

To un-archive a script:
```bash
git mv scripts/tools/archive/<name>.py scripts/<name>.py
```
