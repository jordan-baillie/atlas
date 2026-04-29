# scripts/migrations/archive/

Applied migrations that are no longer needed for fresh installs. Archived
here to reduce clutter in the main migrations/ directory.

Archive criterion: migrations >= 14 days old AND confirmed applied.

## Status (2026-04-29)

No migrations archived yet. All 24 current migrations in scripts/migrations/
are < 14 days old (oldest: 2026-04-22, 7 days). 

Next eligible archive window: ~2026-05-06 (when Apr 22 migrations turn 14 days old).

## When archiving

```bash
git mv scripts/migrations/<name>.py scripts/migrations/archive/<name>.py
```

Verify the migration was applied first:
- Check if the table/column it creates exists in atlas.db
- Check git log for the commit that applied it
