# Post-Incident Review

Investigate and document what went wrong with a system failure. Use after a cron failure, service crash, data issue, or unexpected behavior.

## Instructions
- Time limit: 20 minutes
- Do NOT fix anything — this is investigation only
- Be thorough: check logs, timestamps, service status, recent changes
- Follow the evidence chain: what failed → why → what was the trigger

## Tasks
1. **Identify the incident**
   - If described in the prompt, focus on that
   - Otherwise, scan for recent failures:
     ```bash
     systemctl list-units --failed | grep atlas
     grep -r "ERROR\|CRITICAL\|Traceback" /root/atlas/logs/*.log --include="*.log" -l
     journalctl --since "24 hours ago" -p err | grep atlas | tail -20
     ```

2. **Build timeline**
   - When did it start? (check log timestamps, systemctl show)
   - When was it detected? (Telegram alerts, heartbeat staleness)
   - What changed before the failure? (git log, config changes, system updates)

3. **Root cause analysis**
   - Read the relevant log file end-to-end
   - Identify the exact error/exception
   - Trace the cause chain: error → function → trigger → root cause
   - Check for external factors: disk full, OOM, network timeout, API rate limit

4. **Impact assessment**
   - What was affected? (trading, research, monitoring)
   - Was any data lost or corrupted?
   - Did any trades fail to execute?

5. **Prevention recommendations**
   - What check would have caught this earlier?
   - What config/code change would prevent recurrence?
   - Should a new health check be added?

## Deliverables
- Incident timeline (when, what, impact)
- Root cause (one sentence)
- Evidence (relevant log snippets, error messages)
- Prevention recommendations (actionable items)
- Severity assessment (critical/major/minor)
