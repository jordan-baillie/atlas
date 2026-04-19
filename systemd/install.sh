#!/usr/bin/env bash
# /root/atlas/systemd/install.sh
# Idempotent installer for Atlas systemd units.
# Symlinks every *.service/*.timer under this directory into /etc/systemd/system/,
# runs daemon-reload if anything changed, and enables+starts the timers that are
# part of the active production schedule.
#
# Safe to re-run — only prints output when it actually changes something.
set -euo pipefail

if [[ ${EUID:-$(id -u)} -ne 0 ]]; then
    echo "install.sh: must run as root (use sudo)." >&2
    exit 1
fi

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DST_DIR=/etc/systemd/system

# Timers enabled+started by this script.
# Matches current production state (as of 2026-04-19).
# NOTE: atlas-research-window.timer is intentionally NOT enabled here —
# it is the legacy multi-phase timer, currently disabled in production;
# the per-universe timers below replaced it. The file is still mirrored
# for version-control completeness and can be enabled manually if needed.
TIMERS_TO_ENABLE=(
    atlas-heartbeat-watchdog.timer
    atlas-research-window@sp500.timer
    atlas-research-window@commodity_etfs.timer
    atlas-research-window@sector_etfs.timer
    atlas-research-window@gold_etfs.timer
    atlas-research-window@treasury_etfs.timer
    atlas-research-window@defensive_etfs.timer
    atlas-research-window@crypto.timer
)

changed=0

shopt -s nullglob
for src in "$SRC_DIR"/*.service "$SRC_DIR"/*.timer; do
    base="$(basename "$src")"
    dst="$DST_DIR/$base"
    if [[ -L "$dst" && "$(readlink "$dst")" == "$src" ]]; then
        continue  # already a symlink to the same source
    fi
    if [[ -f "$dst" && ! -L "$dst" ]] && cmp -s "$src" "$dst"; then
        continue  # regular file, byte-identical — leave it alone
    fi
    ln -sfn "$src" "$dst"
    echo "linked: $base -> $src"
    changed=1
done

if (( changed )); then
    systemctl daemon-reload
    echo "daemon-reload: done"
fi

for timer in "${TIMERS_TO_ENABLE[@]}"; do
    unit_file="$SRC_DIR/$timer"
    if [[ ! -e "$unit_file" ]]; then
        echo "install.sh: skipping $timer (not present in $SRC_DIR)" >&2
        continue
    fi
    enabled="$(systemctl is-enabled "$timer" 2>/dev/null || true)"
    if [[ "$enabled" != "enabled" ]]; then
        systemctl enable "$timer"
    fi
    active="$(systemctl is-active "$timer" 2>/dev/null || true)"
    if [[ "$active" != "active" ]]; then
        systemctl start "$timer"
    fi
done

echo "install.sh: done."
