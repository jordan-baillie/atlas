#!/bin/bash
# Atlas Weekly Maintenance — runs Sunday 06:00 AEST via cron
# Prevents log/cache/tmp clutter from accumulating.
set -euo pipefail

PROJECT="/root/atlas"
LOG_DIR="$PROJECT/logs"
CACHE_DIR="$PROJECT/data/cache"

echo "$(date -Iseconds) Weekly maintenance starting"

# ── Log rotation: truncate large logs, keeping last 2000 lines ──
for logfile in atlas.log telegram_bot.log eod_settlement.log intraday_monitor.log dashboard-refresh.log auto_recover.log; do
    f="$LOG_DIR/$logfile"
    [ -f "$f" ] || continue
    lines=$(wc -l < "$f")
    if [ "$lines" -gt 5000 ]; then
        tail -2000 "$f" > "$f.tmp" && mv "$f.tmp" "$f"
        echo "  Rotated $logfile: $lines -> 2000 lines"
    fi
done

# ── Delete pi-cron logs older than 14 days ──
find "$LOG_DIR" -name "pi-cron-*.log" -mtime +14 -delete -print 2>/dev/null | sed 's/^/  Deleted: /'
find "$LOG_DIR" -name "pi-cron-*.md" -mtime +14 -delete -print 2>/dev/null | sed 's/^/  Deleted: /'
find "$LOG_DIR" -name "research_*.log" -mtime +14 -delete -print 2>/dev/null | sed 's/^/  Deleted: /'
find "$LOG_DIR" -name "wave_plan_*.log" -mtime +14 -delete -print 2>/dev/null | sed 's/^/  Deleted: /'
find "$LOG_DIR" -name "recover_*.log" -mtime +14 -delete -print 2>/dev/null | sed 's/^/  Deleted: /'

# ── Delete EOD reports older than 30 days ──
find "$LOG_DIR" -name "eod_*.txt" -mtime +30 -delete -print 2>/dev/null | sed 's/^/  Deleted: /'
find "$LOG_DIR" -name "eod_summary_*.json" -mtime +30 -delete -print 2>/dev/null | sed 's/^/  Deleted: /'

# ── Delete stale intraday alert state files older than 7 days ──
find "$LOG_DIR/intraday" -type f -mtime +7 -delete -print 2>/dev/null | sed 's/^/  Deleted: /'

# ── Purge __pycache__ (regenerated on next import) ──
find "$PROJECT" -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null
echo "  Purged __pycache__ dirs"

# ── Remove any root-level cache parquets (should be in subdirs only) ──
ROOT_DUPES=$(find "$CACHE_DIR" -maxdepth 1 -name "*.parquet" | wc -l)
if [ "$ROOT_DUPES" -gt 0 ]; then
    find "$CACHE_DIR" -maxdepth 1 -name "*.parquet" -delete
    echo "  Removed $ROOT_DUPES stale root-level cache files"
fi

# ── Clean /tmp atlas files (except research lock) ──
find /tmp -name "atlas-*.log" -mtime +1 -delete 2>/dev/null
find /tmp -name "atlas-*.txt" -mtime +1 -delete 2>/dev/null
# Clean stale lock files older than 1 day (but preserve atlas-research.lock if fresh)
find /tmp -name "*.lock" -not -name "atlas-research.lock" -mtime +1 -delete 2>/dev/null

# ── Trim decision_journal.json to last 500 entries ──
JOURNAL="$PROJECT/journal/decision_journal.json"
if [ -f "$JOURNAL" ]; then
    ENTRIES=$(python3 -c "import json; j=json.load(open('$JOURNAL')); print(len(j))" 2>/dev/null || echo 0)
    if [ "$ENTRIES" -gt 500 ]; then
        python3 -c "
import json
j = json.load(open('$JOURNAL'))
json.dump(j[-500:], open('$JOURNAL', 'w'), indent=2)
print(f'  Trimmed decision_journal: {len(j)} -> 500 entries')
"
    fi
fi

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
