# Service Restart

Restart a specific Atlas service and verify it's running correctly. Use when a service is stuck, crashed, or needs a fresh start after config changes.

## Instructions
- Time limit: 5 minutes
- If a service name was provided in the prompt, restart that one
- Otherwise, restart all failed services
- Always verify the service is healthy after restart
- Do NOT restart atlas-telegram-bot (that's us!)

## Tasks
1. **Check current service status**
   ```bash
   systemctl list-units --type=service | grep atlas
   ```

2. **Restart the target service(s)**
   ```bash
   systemctl restart <service-name>
   sleep 5
   systemctl status <service-name> --no-pager | head -15
   ```

3. **Verify health**
   - Check heartbeat files in /tmp if applicable
   - Check logs for startup errors:
     ```bash
     journalctl -u <service-name> --since "2 minutes ago" --no-pager | tail -20
     ```

4. **If restart fails:**
   - Read the full error from journalctl
   - Check for port conflicts, lock files, missing dependencies
   - Report the root cause — don't keep retrying blindly

## Deliverables
- Service name and previous status
- Restart result (success/failure)
- Post-restart verification (healthy/unhealthy)
- Error details if restart failed
