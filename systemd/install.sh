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
# NOTE: atlas-research-window.timer (legacy multi-phase) was removed 2026-04-28.
# Use the per-universe atlas-research-window@<universe>.timer set below.
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
# RETIRED 2026-06-08 — durable disable (survives a reinstall):
#   * Legacy research pipeline (atlas-director, atlas-discovery,
#     atlas-research-window@*) is SUPERSEDED by the Hephaestus forge
#     (/root/hephaestus → hephaestus-cycle.timer).
#   * Nothing trades live (board closed retail edge-hunting; Atlas paper parked
#     to 2026-08-01), so atlas-canary-check + atlas-universe-rebuild are off.
#   * Per-service watchdogs (heartbeat / silent-failure) + atlas-fred-health
#     disabled; system health is covered by unified-healthcheck.timer.
#   These were `systemctl disable`d on the host and removed here so a reinstall
#   does NOT silently re-enable them. Unit files are kept under version control
#   for deliberate revival: `systemctl enable --now <unit>`.
TIMERS_TO_ENABLE=(
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
