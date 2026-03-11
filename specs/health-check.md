# Deep System Health Check

Full Atlas system health audit with auto-fix. More thorough than `#daily-health-check` — use for diagnosing persistent issues or post-incident verification.

## Instructions
- Time limit: 15 minutes. If running longer, wrap up with what you have.
- For each issue found, attempt an auto-fix before reporting.
- Do NOT modify trading configs or strategy parameters.
- Do NOT restart services that are intentionally stopped.

## Tasks
1. Check all atlas systemd services (`systemctl list-units | grep atlas`)
2. Check disk space, memory usage, CPU load
3. Verify market data freshness (snapshots directory)
4. Check autoresearch heartbeat and status
5. Verify cron jobs are registered and recent logs exist
6. Check for stale lock files in /tmp
7. Verify dashboard data is recent (`dashboard-data.json` timestamp)

## Deliverables
- Summary of all checks (pass/fail) sent to this chat
- Any auto-fixes applied with before/after status
- List of issues requiring human attention (if any)
