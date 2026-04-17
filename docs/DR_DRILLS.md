# DR Restore Drills

History of verified restore drills. Each row = one completed drill with evidence.

**Drill protocol:**
1. Identify latest restic snapshot
2. `restic restore <snapshot> --target /tmp/atlas-restore-test --include /root/atlas/data/atlas.db`
3. Run `PRAGMA integrity_check` on restored DB
4. Compare `SELECT count(*) FROM trades` between restored and live
5. Δ ≤ ~1 day of trades is expected (backup runs at 04:00 AEST, trades execute post-open)
6. Record result here

---

| Date (UTC) | Backup Source | Restore Target | Snapshot ID | Live trade count | Restored trade count | Δ | Notes |
|------------|---------------|----------------|-------------|------------------|----------------------|---|-------|
| 2026-04-17 | `/root/backups/restic-repo` (local restic, automated tag) | `/tmp/atlas-restore-test/` | `e74810d5` (2026-04-17 04:03:04 AEST) | 47 | 46 | 1 | First documented drill. Δ=1 expected: snapshot at 04:03, one trade entered at EOD open after backup ran. PRAGMA integrity_check: ok. See runbook `docs/DISASTER_RECOVERY.md`. |

---

## Drill Command Reference

```bash
# Run a drill
mkdir -p /tmp/atlas-restore-test
RESTIC_PASSWORD="atlas-backup-2026" \
restic -r /root/backups/restic-repo restore latest \
    --target /tmp/atlas-restore-test \
    --include /root/atlas/data/atlas.db

RESTORED=/tmp/atlas-restore-test/root/atlas/data/atlas.db

# Verify
sqlite3 "$RESTORED" "PRAGMA integrity_check;"
echo "Restored trade count:"
sqlite3 "$RESTORED" "SELECT count(*) FROM trades;"
echo "Live trade count:"
sqlite3 /root/atlas/data/atlas.db "SELECT count(*) FROM trades;"

# Cleanup
rm -rf /tmp/atlas-restore-test
```

## Known Gaps

- **Offsite:** Restic repo lives on the same host as source data. A full server loss
  would take both. Monthly manual offsite export recommended:
  `tar -czf atlas_offsite_$(date +%Y%m).tar.gz /root/backups/restic-repo/`

- **atlas-heartbeat-watchdog.timer** was added 2026-04-17. First backup including
  its service file will be the next daily snapshot (2026-04-18 04:00 AEST).
