#!/bin/bash
# Atlas Weekly Maintenance — runs Sunday 06:00 AEST via atlas-weekly-maintenance.timer
# Prevents log/cache/tmp clutter from accumulating.
set -euo pipefail

PROJECT="/root/atlas"
LOG_DIR="$PROJECT/logs"
CACHE_DIR="$PROJECT/data/cache"

echo "$(date -Iseconds) Weekly maintenance starting"

# ── Log rotation: truncate large logs, keeping last 2000 lines ──
for logfile in atlas.log cleanup_sediment.log weekly_maintenance.log; do
    f="$LOG_DIR/$logfile"
    [ -f "$f" ] || continue
    lines=$(wc -l < "$f")
    if [ "$lines" -gt 5000 ]; then
        tail -2000 "$f" > "$f.tmp" && mv "$f.tmp" "$f"
        echo "  Rotated $logfile: $lines -> 2000 lines"
    fi
done

# ── Trim the forward-paper cycle log (append-only, grows daily) ──
FP_LOG="$PROJECT/data/live/forward_paper.log"
if [ -f "$FP_LOG" ] && [ "$(wc -l < "$FP_LOG")" -gt 5000 ]; then
    tail -2000 "$FP_LOG" > "$FP_LOG.tmp" && mv "$FP_LOG.tmp" "$FP_LOG"
    echo "  Rotated forward_paper.log"
fi

# ── Purge __pycache__ (regenerated on next import) ──
find "$PROJECT" -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null
echo "  Purged __pycache__ dirs"

# ── Root-level cache parquets are CANONICAL, do NOT delete (fixed 2026-06-14, #36 cascade) ──
# The crucible adapters (sdk/adapters.py, added Jun 10-12) deliberately write their base
# caches to the cache ROOT: sep_long_v2.parquet, sf1_long.parquet, futcurve_*.parquet.
# The old "root parquets are stale dupes" assumption is dead; deleting them forced an
# expensive (~1-2min sep + sf1) cold rebuild every Monday and tripped sentinel S2.
# These self-invalidate via _stale() (mtime vs source) — let the adapters manage them.

# ── Clean /tmp atlas files ──
find /tmp -name "atlas-*.log" -mtime +7 -delete 2>/dev/null || true
find /tmp -name "*.lock" -mtime +1 -delete 2>/dev/null || true

# ── Cleanup atlas.db backup files: keep 2 newest only ──
# Empty-glob-safe (fixed 2026-06-14): a bare `ls glob*` exits 2 under set -euo pipefail
# when nothing matches, which killed the whole script mid-run after the morning sweep
# removed the last atlas.db.bak files. find handles the empty case cleanly.
echo "Pruning atlas.db backup files (keeping 2 newest)..."
find "$PROJECT/data" -maxdepth 1 \( -name 'atlas.db.bak*' -o -name 'atlas.db.backup*' \) -printf '%T@ %p\n' 2>/dev/null \
    | sort -rn | tail -n +3 | cut -d' ' -f2- | while read -r old; do
    rm -v "$old"
done
echo "  Backup pruning complete"

# ── ANALYZE atlas.db for query planner stats ──
echo "Running SQLite ANALYZE..."
sqlite3 "$PROJECT/data/atlas.db" "PRAGMA analysis_limit=1000; ANALYZE;"
echo "  ANALYZE complete"

echo "$(date -Iseconds) Weekly maintenance complete"
