#!/usr/bin/env bash
# cron_stderr_capture.sh — wraps a cron command and captures stderr/exit code
# to the errors table on non-zero exit.
#
# Usage in crontab:
#   */5 * * * * /root/atlas/scripts/cron_stderr_capture.sh some_job_name /path/to/cmd arg arg
#
# On exit-0:    behaves transparently (no DB writes)
# On exit-N>0:  inserts a row into errors with source='cron', service=$JOB_NAME,
#               level='ERROR' (or CRITICAL if exit_code in 137,139,134), captures
#               last 50 lines of stderr.
#
# Pass-through mode: stderr is also written to original stderr (so existing logs
# still see it), so this is non-disruptive.
set -uo pipefail

if [ $# -lt 2 ]; then
    echo "Usage: $0 <job_name> <command> [args...]" >&2
    exit 2
fi

JOB_NAME="$1"; shift
PROJECT="${ATLAS_PROJECT:-/root/atlas}"
CAPTURE_PY="$PROJECT/scripts/cron_stderr_to_errors.py"
STDERR_BUF="$(mktemp /tmp/cron-stderr-${JOB_NAME//\//_}-XXXXXX.log)"

# Run command, tee stderr to both buffer and original stderr.
# Process substitution tees stderr: writes to file AND passes through to
# parent's stderr so existing log captures are not disrupted.
{ "$@" 2> >(tee "$STDERR_BUF" >&2); }
EXIT_CODE=$?

if [ "$EXIT_CODE" -ne 0 ]; then
    # Non-zero — emit to errors table (best-effort, never fail the parent)
    if [ -f "$CAPTURE_PY" ]; then
        python3 "$CAPTURE_PY" \
            --job "$JOB_NAME" \
            --exit-code "$EXIT_CODE" \
            --stderr-file "$STDERR_BUF" \
            --command "$*" \
            >/dev/null 2>&1 || true
    fi
fi

rm -f "$STDERR_BUF"
exit "$EXIT_CODE"
