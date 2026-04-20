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

# Install shared atlas environment file if missing.
ATLAS_CONF_DIR=/etc/atlas
ATLAS_CONF=$ATLAS_CONF_DIR/atlas.conf
if [[ ! -f "$ATLAS_CONF" ]]; then
    install -d -m 755 "$ATLAS_CONF_DIR"
    install -m 644 "$SRC_DIR/atlas.conf.template" "$ATLAS_CONF"
    echo "installed: $ATLAS_CONF (from atlas.conf.template)"
fi

# Timers enabled+started by this script.
# Matches current production state (as of 2026-04-20).
#
# NOTE: atlas-research-window.timer is intentionally NOT enabled here —
# it is the legacy multi-phase timer, currently disabled in production;
# the per-universe timers below replaced it. The file is still mirrored
# for version-control completeness and can be enabled manually if needed.
#
# NOTE: atlas-research-runner.service is intentionally NOT enabled by
# this script. It is a queue-based research daemon currently disabled
# on the host. The unit file is mirrored for version control, but it
# must be turned on intentionally via `systemctl enable --now
# atlas-research-runner.service` — never automatically by this installer.
#
# NOTE: atlas-discovery.service is a `static` unit (no [Install] section).
# It cannot be `systemctl enable`d directly — it is triggered by
# atlas-discovery.timer, which IS enabled below. Same pattern for
# atlas-silent-failure-watchdog.service.
#
# NOTE: ib-gateway-watchdog.timer is intentionally NOT enabled here —
# it is currently disabled on the host (the watchdog requires IB Gateway
# containers to be present; enabling it on a host without them would produce
# spurious restart attempts). Enable manually with
# `systemctl enable --now ib-gateway-watchdog.timer` when IB containers are
# provisioned. The unit file is mirrored for version-control completeness.
TIMERS_TO_ENABLE=(
    atlas-heartbeat-watchdog.timer
    atlas-silent-failure-watchdog.timer
    atlas-research-window@sp500.timer
    atlas-research-window@commodity_etfs.timer
    atlas-research-window@sector_etfs.timer
    atlas-research-window@gold_etfs.timer
    atlas-research-window@treasury_etfs.timer
    atlas-research-window@defensive_etfs.timer
    atlas-research-window@crypto.timer
    atlas-director.timer
    atlas-discovery.timer
    atlas-backup.timer
    unified-healthcheck.timer
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

echo
echo "=== Preflight: external EnvironmentFile dependencies ==="
missing_env=()
while IFS= read -r env_path; do
    # Strip the leading '-' (systemd syntax for "ignore if missing")
    real_path="${env_path#-}"
    if [[ ! -f "$real_path" ]]; then
        missing_env+=("$real_path")
    fi
done < <(grep -hE '^EnvironmentFile=' "$SRC_DIR"/*.service 2>/dev/null | cut -d= -f2)

if (( ${#missing_env[@]} > 0 )); then
    echo "WARNING: the following EnvironmentFile paths referenced by atlas units do not exist on this host:"
    for f in "${missing_env[@]}"; do echo "  - $f"; done
    echo "Services referencing these files will fail at invocation. Provision them before enabling."
    echo "If any path starts with '-' in the unit, systemd will tolerate its absence."
else
    echo "✓ All EnvironmentFile paths exist."
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
