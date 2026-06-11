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

# ── Remove any root-level cache parquets (should be in subdirs only) ──
ROOT_DUPES=$(find "$CACHE_DIR" -maxdepth 1 -name "*.parquet" 2>/dev/null | wc -l)
if [ "$ROOT_DUPES" -gt 0 ]; then
    find "$CACHE_DIR" -maxdepth 1 -name "*.parquet" -delete
    echo "  Removed $ROOT_DUPES stale root-level cache files"
fi

# ── Clean /tmp atlas files ──
find /tmp -name "atlas-*.log" -mtime +7 -delete 2>/dev/null || true
find /tmp -name "*.lock" -mtime +1 -delete 2>/dev/null || true

# ── Cleanup atlas.db backup files: keep 2 newest only ──
echo "Pruning atlas.db backup files (keeping 2 newest)..."
ls -1t "$PROJECT"/data/atlas.db.bak* "$PROJECT"/data/atlas.db.backup* 2>/dev/null | tail -n +3 | while read -r old; do
    rm -v "$old"
done
echo "  Backup pruning complete"

# ── ANALYZE atlas.db for query planner stats ──
echo "Running SQLite ANALYZE..."
sqlite3 "$PROJECT/data/atlas.db" "PRAGMA analysis_limit=1000; ANALYZE;"
echo "  ANALYZE complete"

echo "$(date -Iseconds) Weekly maintenance complete"
