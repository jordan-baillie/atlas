# Daily Health Check

Quick daily system health audit. Runs automatically before premarket but can be triggered ad-hoc via `/task #daily-health-check`.

## Instructions
- Time limit: 10 minutes
- Fix safe issues automatically (stale locks, restart crashed services)
- Do NOT modify trading configs, strategy parameters, or cron schedules
- Do NOT restart services that are intentionally disabled (atlas-research-daemon, ASX/HK crons)

## Tasks
1. Check all atlas systemd services: `systemctl list-units --type=service | grep atlas`
   - Restart any that show `failed` (except intentionally disabled ones)
2. Check disk space: `df -h /` — alert if <10GB free
3. Check memory: `free -h` — alert if <2GB available
4. Verify autoresearch heartbeat: `cat /tmp/autoresearch-heartbeat.json`
   - If status=stopped but service is active, the heartbeat is stale — note but don't fix
5. Verify dashboard data freshness: check timestamp in `dashboard/data/dashboard-data.json`
6. Check for stale lock files: `ls -la /tmp/*lock* /tmp/*daemon* 2>/dev/null`
7. Check log sizes: `du -sh logs/*.log | sort -rh | head -5` — note any >50MB

## Deliverables
- Single summary message: all checks pass/fail
- List of auto-fixes applied (if any)
- List of issues needing human attention (if any)
