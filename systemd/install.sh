#!/usr/bin/env bash
# /root/atlas/systemd/install.sh
# Idempotent installer for Atlas systemd units.
#
# 1. Durably retires units that no longer exist in this directory (disable +
#    remove stray links) — a reinstall can never silently re-enable them.
# 2. Symlinks every *.service/*.timer under this directory into /etc/systemd/system/.
# 3. Enables + starts the active production schedule.
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

# ── Durable retirement ────────────────────────────────────────────────────────
# Units removed in the 2026-06 great-deletion refactor. Disabled + unlinked on
# every install so a host that missed a deploy converges to the same state.
# (atlas-telegram-bot: command bot retired — outbound notify lives in
#  atlas.kernel.notify. atlas-dashboard-refresh: host-local unit, retired.)
RETIRED_UNITS=(
    atlas-telegram-bot
    atlas-dashboard-refresh
    atlas-canary-check
    atlas-consolidation-closure
    atlas-director
    atlas-discovery
    atlas-error-remediation
    atlas-fred-health
    atlas-heartbeat-watchdog
    atlas-intraday-backfill
    atlas-orchestrator
    atlas-reconcile-shadow
    atlas-risk-precompute
    atlas-sandbox-9strats
    atlas-silent-failure-watchdog
    atlas-universe-rebuild
)

retired_changed=0
for unit in "${RETIRED_UNITS[@]}"; do
    for suffix in service timer; do
        name="$unit.$suffix"
        if systemctl list-unit-files "$name" --no-legend 2>/dev/null | grep -q .; then
            systemctl disable --now "$name" 2>/dev/null || true
        fi
        if [[ -e "$DST_DIR/$name" || -L "$DST_DIR/$name" ]]; then
            rm -f "$DST_DIR/$name"
            echo "retired: $name"
            retired_changed=1
        fi
    done
done
# Templated research-window units
for f in "$DST_DIR"/atlas-research-window@*; do
    [[ -e "$f" || -L "$f" ]] || continue
    systemctl disable --now "$(basename "$f")" 2>/dev/null || true
    rm -f "$f"
    echo "retired: $(basename "$f")"
    retired_changed=1
done

# Remove dangling symlinks left by deleted unit files.
for f in "$DST_DIR"/atlas-*.service "$DST_DIR"/atlas-*.timer "$DST_DIR"/unified-healthcheck.*; do
    if [[ -L "$f" && ! -e "$f" ]]; then
        rm -f "$f"
        echo "removed dangling link: $(basename "$f")"
        retired_changed=1
    fi
done

# ── Link current units ────────────────────────────────────────────────────────
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

if (( changed || retired_changed )); then
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
else
    echo "✓ All EnvironmentFile paths exist."
fi

# ── Active production schedule ────────────────────────────────────────────────
SERVICES_TO_ENABLE=(
    atlas-dashboard.service
)
TIMERS_TO_ENABLE=(
    atlas-live-shadow.timer
    atlas-backup.timer
    unified-healthcheck.timer
    atlas-weekly-maintenance.timer
    atlas-sediment-cleanup.timer
    atlas-sp500-flatten.timer    # transitional — delete once the retired SP500 paper account is flat
)

for unit in "${SERVICES_TO_ENABLE[@]}" "${TIMERS_TO_ENABLE[@]}"; do
    unit_file="$SRC_DIR/$unit"
    if [[ ! -e "$unit_file" ]]; then
        echo "install.sh: skipping $unit (not present in $SRC_DIR)" >&2
        continue
    fi
    enabled="$(systemctl is-enabled "$unit" 2>/dev/null || true)"
    if [[ "$enabled" != "enabled" ]]; then
        systemctl enable "$unit"
    fi
    active="$(systemctl is-active "$unit" 2>/dev/null || true)"
    if [[ "$active" != "active" ]]; then
        systemctl start "$unit"
    fi
done

echo "install.sh: done."
