#!/bin/bash
# Atlas Research Cron — time-boxed, CPU-throttled sweep sessions.
#
# Runs sweep.py with reduced parallelism and nice priority so it doesn't
# trigger Hostinger VPS compute limits. Called by systemd timer or cron.
#
# Usage:
#   scripts/research_cron.sh              # default: 2h, 2 workers, 30 tickers
#   scripts/research_cron.sh --quick      # 30min, 2 workers, 20 tickers
#   scripts/research_cron.sh --deep       # 3h, 3 workers, 50 tickers
#
# The script:
#   1. Checks for lock (no concurrent sessions)
#   2. Runs sweep.py under nice -n 15 with reduced workers
#   3. Sends Telegram summary on completion
#   4. Cleans up lock

set -euo pipefail

ATLAS_ROOT="/root/atlas"
cd "$ATLAS_ROOT"

LOCK_FILE="/tmp/atlas-research-cron.lock"
LOG_DIR="$ATLAS_ROOT/logs"
LOG_FILE="$LOG_DIR/research-cron-$(date +%Y%m%d_%H%M%S).log"

# ─── Configuration Profiles ─────────────────────────────────────────────────

# Default: 2 hours, 6 workers, 30 tickers — use the cores, time-box the session
MAX_RUNTIME=7200
WORKERS=6
TOP_N=30
MAX_FAILS=5
PROFILE="default"

case "${1:-}" in
    --quick)
        MAX_RUNTIME=1800   # 30 min
        WORKERS=6
        TOP_N=20
        MAX_FAILS=3
        PROFILE="quick"
        ;;
    --deep)
        MAX_RUNTIME=10800  # 3 hours
        WORKERS=6
        TOP_N=50
        MAX_FAILS=8
        PROFILE="deep"
        ;;
    --*)
        echo "Unknown option: $1. Use --quick or --deep."
        exit 1
        ;;
esac

# ─── Lock ────────────────────────────────────────────────────────────────────

if [ -f "$LOCK_FILE" ]; then
    LOCK_PID=$(cat "$LOCK_FILE" 2>/dev/null || echo "unknown")
    if kill -0 "$LOCK_PID" 2>/dev/null; then
        echo "Research already running (PID $LOCK_PID). Skipping."
        exit 0
    else
        echo "Stale lock (PID $LOCK_PID dead). Removing."
        rm -f "$LOCK_FILE"
    fi
fi
echo $$ > "$LOCK_FILE"
trap 'rm -f "$LOCK_FILE"' EXIT

# ─── Pre-flight cleanup ─────────────────────────────────────────────────────

# Kill any orphaned python workers from a previously killed research window.
# systemd's SIGKILL sometimes misses ProcessPoolExecutor children.
for pid in $(pgrep -f "research/sweep.py|_backtest_worker" 2>/dev/null); do
    if [ "$pid" != "$$" ]; then
        echo "Killing orphaned research process: PID $pid"
        kill -9 "$pid" 2>/dev/null || true
    fi
done

# ─── Run ─────────────────────────────────────────────────────────────────────

echo "$(date '+%Y-%m-%d %H:%M:%S') — Research cron starting ($PROFILE profile)"
echo "  Workers: $WORKERS, Top-N: $TOP_N, Max runtime: $((MAX_RUNTIME / 60))min"

# nice -n 15: low CPU scheduling priority (yields to trading crons)
# ionice -c 2 -n 7: best-effort I/O with low priority
nice -n 15 ionice -c 2 -n 7 \
    python3 research/sweep.py \
        --market sp500 \
        --top-n "$TOP_N" \
        --workers "$WORKERS" \
        --max-fails "$MAX_FAILS" \
        --max-runtime "$MAX_RUNTIME" \
        --cycles 0 \
        --log-file "$LOG_FILE" \
    2>&1 | tail -50

EXIT_CODE=${PIPESTATUS[0]}

# ─── Summary ─────────────────────────────────────────────────────────────────

RUNTIME_MIN=$(( ($(date +%s) - $(date -r "$LOCK_FILE" +%s 2>/dev/null || date +%s)) / 60 ))

# Count experiments from log
EXPERIMENTS=$(grep -c "^20.*keep\|^20.*discard" "$LOG_FILE" 2>/dev/null || echo "0")
KEPT=$(grep -c "^20.*keep" "$LOG_FILE" 2>/dev/null || echo "0")

echo ""
echo "$(date '+%Y-%m-%d %H:%M:%S') — Research cron finished (exit $EXIT_CODE)"
echo "  Profile: $PROFILE, Runtime: ${RUNTIME_MIN}min, Experiments: $EXPERIMENTS, Kept: $KEPT"

# Send Telegram summary (best-effort, don't fail the script)
if [ "$EXPERIMENTS" -gt 0 ] || [ "$EXIT_CODE" -ne 0 ]; then
    STATUS_EMOJI="✅"
    [ "$EXIT_CODE" -ne 0 ] && STATUS_EMOJI="⚠️"

    timeout 30 python3 -c "
from utils.telegram import send_message
send_message(
    '🔬 <b>Research Cron Complete</b> $STATUS_EMOJI\n'
    'Profile: $PROFILE | Runtime: ${RUNTIME_MIN}m\n'
    'Experiments: $EXPERIMENTS | Kept: $KEPT\n'
    'Workers: $WORKERS | Top-N: $TOP_N'
)
" 2>/dev/null || true
fi

# Rotate old research cron logs (keep last 7 days)
find "$LOG_DIR" -name "research-cron-*.log" -mtime +7 -delete 2>/dev/null || true

exit $EXIT_CODE
