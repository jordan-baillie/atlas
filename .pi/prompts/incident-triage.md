---
description: Diagnose and triage a system incident — quick scan, categorize, suggest fix
---
Read the atlas-incident skill first — it contains the routing index and cookbooks:
/skill:atlas-incident

Run the Quick Status Scan to establish baseline system state:

1. **Services check:**
   systemctl status atlas-intraday-monitor atlas-research-daemon atlas-dashboard-loop 2>/dev/null | grep -E "Active:|Failed:"
   systemctl --failed --no-legend 2>/dev/null | head -20

2. **Recent errors (last 2 hours):**
   grep -E "ERROR|CRITICAL|FATAL|Traceback" logs/pi-cron.log 2>/dev/null | tail -20
   journalctl -u atlas-* --since "2 hours ago" --no-pager -q 2>/dev/null | grep -E "ERROR|Failed|Exception" | tail -20

3. **Disk space:**
   df -h / /root 2>/dev/null | tail -3

4. **Broker connectivity:**
   python3 -c "
import sys; sys.path.insert(0, '/root/atlas')
from brokers.alpaca.client import get_client
try:
    c = get_client()
    acct = c.get_account()
    print(f'Broker OK — equity={acct.equity}')
except Exception as e:
    print(f'Broker ERROR: {e}')
" 2>/dev/null

5. **Categorize the incident** using the atlas-incident skill index:
   Based on what the scan reveals, identify which cookbook applies:
   - Service down → service recovery cookbook
   - Data stale → data freshness cookbook
   - Broker error → broker connectivity cookbook
   - Plan failure → plan generation cookbook
   - Settlement failure → EOD settlement cookbook
   - Code error in research → research error cookbook

6. **Suggest the fix** based on the matching cookbook. Be specific — include the exact commands to run.

7. **Wait for confirmation** before applying any fix.
   Do NOT automatically restart services, modify configs, or execute trades.
   Present the diagnosis and proposed fix, then ask: "Shall I apply this fix?"

If $ARGUMENTS is provided, treat it as additional context about what went wrong (e.g. error message, failed service name, time of failure).
