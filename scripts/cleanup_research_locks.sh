#!/bin/bash
# Atlas research lock-file cleanup.
# Removes research/locks/*.json older than 7 days. Idempotent.
# Run from cron daily. Safe — these files are SHA-256 integrity checksums,
# not process locks.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT="$(dirname "$SCRIPT_DIR")"
LOCKS_DIR="$PROJECT/research/locks"
LOG_FILE="$PROJECT/logs/cleanup_research_locks.log"

mkdir -p "$PROJECT/logs"

if [ ! -d "$LOCKS_DIR" ]; then
    echo "$(date -Iseconds) No $LOCKS_DIR — nothing to clean" >> "$LOG_FILE"
    exit 0
fi

before=$(find "$LOCKS_DIR" -maxdepth 1 -type f -name '*.json' 2>/dev/null | wc -l)
find "$LOCKS_DIR" -maxdepth 1 -type f -name '*.json' -mtime +7 -delete 2>/dev/null || true
after=$(find "$LOCKS_DIR" -maxdepth 1 -type f -name '*.json' 2>/dev/null | wc -l)

removed=$((before - after))
echo "$(date -Iseconds) research_locks: removed=$removed, remaining=$after (was $before)" >> "$LOG_FILE"
exit 0
