#!/bin/bash
# deploy/deploy_timers.sh
# Install and enable all Cronus systemd service/timer units.
#
# Usage:
#   bash deploy/deploy_timers.sh           # deploy all timers
#   bash deploy/deploy_timers.sh --status  # show timer status only
#
# Run as root (requires write access to /etc/systemd/system/).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ── Colour helpers ──
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
ok()   { echo -e "${GREEN}  ✅ $*${NC}"; }
warn() { echo -e "${YELLOW}  ⚠️  $*${NC}"; }
err()  { echo -e "${RED}  ❌ $*${NC}"; }

# ── All timer units managed by this script ──
TIMERS=(
    cronus-gateway-health
    cronus-watchdog
    cronus-rollover
    cronus-scanner
    cronus-fundamentals
    cronus-calendar
)

# ── Status-only mode ──
if [[ "${1:-}" == "--status" ]]; then
    echo "══════════════════════════════════════════"
    echo "  CRONUS TIMER STATUS"
    echo "══════════════════════════════════════════"
    for name in "${TIMERS[@]}"; do
        status=$(systemctl is-active "${name}.timer" 2>/dev/null || echo "inactive")
        next=$(systemctl show "${name}.timer" --property=NextElapseUSecRealtime --value 2>/dev/null | head -1)
        if [[ "$status" == "active" ]]; then
            ok "${name}.timer: ${status}"
        else
            warn "${name}.timer: ${status}"
        fi
    done
    echo ""
    echo "Detailed view:"
    systemctl list-timers 'cronus-*' --no-pager 2>/dev/null || true
    exit 0
fi

# ── Require root ──
if [[ $EUID -ne 0 ]]; then
    err "This script must be run as root (sudo bash deploy/deploy_timers.sh)"
    exit 1
fi

echo "══════════════════════════════════════════"
echo "  CRONUS TIMER DEPLOYMENT"
echo "══════════════════════════════════════════"
echo "Source:      $SCRIPT_DIR"
echo "Destination: /etc/systemd/system/"
echo ""

# ── Step 1: Copy unit files ──
echo "── Step 1: Installing unit files ──"
for name in "${TIMERS[@]}"; do
    svc_file="${SCRIPT_DIR}/${name}.service"
    timer_file="${SCRIPT_DIR}/${name}.timer"

    if [[ ! -f "$svc_file" ]]; then
        err "Missing: ${name}.service"
        exit 1
    fi
    if [[ ! -f "$timer_file" ]]; then
        err "Missing: ${name}.timer"
        exit 1
    fi

    cp "$svc_file"   /etc/systemd/system/
    cp "$timer_file" /etc/systemd/system/
    ok "Installed: ${name}.service + ${name}.timer"
done
echo ""

# ── Step 2: Reload systemd ──
echo "── Step 2: Reloading systemd daemon ──"
systemctl daemon-reload
ok "daemon-reload complete"
echo ""

# ── Step 3: Enable and start timers ──
echo "── Step 3: Enabling and starting timers ──"
for name in "${TIMERS[@]}"; do
    timer="${name}.timer"

    # Disable old cron equivalents (idempotent — ignore errors)
    case "$name" in
        cronus-scanner)
            # Previously run via cron: 0 6 * * 1-5
            warn "Ensure 'cronus-scanner' cron entry removed from crontab" ;;
        cronus-rollover)
            warn "Ensure 'cronus-rollover' cron entry removed from crontab" ;;
        cronus-fundamentals)
            warn "Ensure 'cronus-fundamentals' cron entry removed from crontab" ;;
        cronus-calendar)
            warn "Ensure 'cronus-calendar' cron entry removed from crontab" ;;
    esac

    systemctl enable "$timer" 2>/dev/null
    systemctl start  "$timer"

    if systemctl is-active --quiet "$timer"; then
        ok "${timer}: active"
    else
        err "${timer}: failed to start"
        journalctl -u "$timer" --no-pager -n 5
    fi
done
echo ""

# ── Step 4: Show status ──
echo "── Step 4: Timer status ──"
systemctl list-timers 'cronus-*' --no-pager 2>/dev/null || true
echo ""

echo "══════════════════════════════════════════"
echo "  Deployment complete."
echo ""
echo "  Monitor: journalctl -f -u 'cronus-*'"
echo "  Status:  bash deploy/deploy_timers.sh --status"
echo "  Logs:    journalctl -u cronus-scanner --no-pager -n 50"
echo "══════════════════════════════════════════"
