# Atlas Migrations

## Overview

Migrations live in `scripts/migrations/` and follow the naming pattern:

```
YYYY-MM-DD-description.py
```

Each migration is a standalone Python script with `--dry-run` (default) and
`--apply` modes.  See `2026-04-29-add-broker-orders-table.py` as the exemplar.

## How to apply a migration

```bash
# Preview (dry-run, safe):
python3 scripts/migrations/2026-XX-XX-description.py

# Apply against the live DB:
python3 scripts/migrations/2026-XX-XX-description.py --apply
```

Migrations must be **idempotent** — re-running them must be safe.  Use:
- `CREATE TABLE IF NOT EXISTS`
- `CREATE INDEX IF NOT EXISTS`
- `INSERT OR IGNORE INTO`
- `ALTER TABLE ADD COLUMN` wrapped in a `try/except OperationalError`

## There is no migration runner

There is no automated migration runner.  Migrations are applied manually, in
date order, whenever a schema change is needed.  The `archive/` subdirectory
holds migrations that have been superseded or rolled back.

## schema_version — informational only

`schema_version` (in `atlas.db`) is **informational** — it does not drive any
application logic.  The actual schema state is determined by:

1. Running `db/schema.sql` (all `CREATE TABLE IF NOT EXISTS` statements) against
   a fresh or existing database.
2. Applying all migrations in `scripts/migrations/` in date order.

**Policy**: each migration that introduces a new schema construct (table,
index, column, constraint) should also insert a new row into `schema_version`
with the next sequential version number and `applied_at = datetime('now')`.

```sql
INSERT OR IGNORE INTO schema_version (version, applied_at)
VALUES (<next_version>, datetime('now'));
```

Version history:
- `1` — Initial schema (`db/schema.sql`, 2026-03-31)
- `28` — All migrations applied through 2026-05-01 (28 migration files)

When you write a new migration, bump the version by 1 and update the history
table above.
