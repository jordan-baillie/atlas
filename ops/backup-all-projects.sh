#!/bin/bash
# Atlas Backup System - Comprehensive backup of all projects
# Created: 2026-03-24
# Backs up configs, data, state, and critical files to restic repo

set -euo pipefail

# Configuration
RESTIC_REPOSITORY="/root/backups/restic-repo"
RESTIC_PASSWORD="atlas-backup-2026"
LOG_FILE="/root/logs/backup.log"
TIMESTAMP=$(date '+%Y-%m-%d %H:%M:%S')

# Export restic password
export RESTIC_PASSWORD

# Logging function
log() {
    echo "[$TIMESTAMP] $1" | tee -a "$LOG_FILE"
}

log "========================================="
log "Starting backup run"
log "========================================="

# === Defensive: remove stale locks before operations ===
# Stale locks accumulate if a previous restic invocation died mid-operation
# (e.g. SIGKILL from OOM, host reboot, timeout). `restic unlock` (without
# --remove-all) only removes locks whose creator PID is dead OR which are
# older than 30 min, so it is safe during concurrent reads but will clear
# the wreckage of crashed writes.
log "Defensive: removing any stale restic locks..."
if ! restic -r "$RESTIC_REPOSITORY" unlock 2>&1 | tee -a "$LOG_FILE"; then
    log "WARN: restic unlock failed (non-fatal, continuing)"
fi

# Export crontab before backup
log "Exporting crontab..."
crontab -l > /tmp/crontab-backup.txt 2>/dev/null || echo "# No crontab" > /tmp/crontab-backup.txt

# Run restic backup with explicit includes and excludes
log "Running restic backup..."
restic -r "$RESTIC_REPOSITORY" backup \
    --tag automated \
    --exclude "/root/atlas/data/cache/earnings/" \
    --exclude "/root/atlas/data/cache/backtest/" \
    --exclude "/root/atlas/data/cache/bars/" \
    --exclude "/root/atlas/data/cache/historical/" \
    --exclude "/root/_archive/cronus-2026-05-18/data/cache/" \
    --exclude "/root/NRL-Predict/data/cache/" \
    --exclude "**/__pycache__/" \
    --exclude "**/*.pyc" \
    --exclude "**/node_modules/" \
    --exclude "**/.git/" \
    --exclude "**/venv/" \
    /root/atlas/config/ \
    /root/atlas/data/ \
    /root/atlas/journal/ \
    /root/atlas/brokers/state/ \
    /root/atlas/tasks/ \
    /root/atlas/docs/ \
    /root/_archive/cronus-2026-05-18/config/ \
    /root/_archive/cronus-2026-05-18/data/ \
    /root/_archive/cronus-2026-05-18/tasks/ \
    /root/_archive/cronus-2026-05-18/docs/ \
    /root/NRL-Predict/data/ \
    /root/NRL-Predict/models/ \
    /root/NRL-Predict/tasks/ \
    /root/midas/ \
    /root/.atlas-secrets.json \
    /root/.pi/agent/ \
    /root/ceo-board/ \
    /tmp/crontab-backup.txt \
    /etc/systemd/system/atlas-*.service \
    /etc/systemd/system/cronus-*.service \
    /etc/systemd/system/nrl-*.service \
    2>&1 | tee -a "$LOG_FILE"

BACKUP_EXIT_CODE=${PIPESTATUS[0]}

if [ $BACKUP_EXIT_CODE -eq 0 ]; then
    log "✓ Backup completed successfully"
else
    log "✗ Backup failed with exit code $BACKUP_EXIT_CODE"
    exit $BACKUP_EXIT_CODE
fi

# Apply retention policy: keep 7 daily, 4 weekly, 3 monthly
log "Applying retention policy..."
restic -r "$RESTIC_REPOSITORY" forget \
    --keep-daily 7 \
    --keep-weekly 4 \
    --keep-monthly 3 \
    --prune \
    2>&1 | tee -a "$LOG_FILE"

FORGET_EXIT_CODE=${PIPESTATUS[0]}

if [ $FORGET_EXIT_CODE -eq 0 ]; then
    log "✓ Retention policy applied successfully"
else
    log "✗ Retention policy failed with exit code $FORGET_EXIT_CODE"
fi

# Show repository stats
log "Repository statistics:"
restic -r "$RESTIC_REPOSITORY" stats --mode raw-data 2>&1 | tee -a "$LOG_FILE"

log "========================================="
log "Backup run completed"
log "========================================="
log ""

exit 0
