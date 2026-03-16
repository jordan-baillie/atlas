# Cron & Automation — Incident Cookbook

Covers: pi-cron.sh failures, healthz autofix loops, weekly maintenance failures.

---

## Pattern 15: pi-cron.sh Failed (premarket/postclose)

**Symptoms:** No plan generated, no Telegram notification, log shows error.

**Fix:**
```bash
# Check the log
ls -lt /root/atlas/logs/pi-cron-*.log | head -3
tail -50 $(ls -t /root/atlas/logs/pi-cron-*.log | head -1)

# Re-run manually
cd /root/atlas && bash scripts/pi-cron.sh premarket sp500
```

**Verify:**
```bash
tail -20 $(ls -t /root/atlas/logs/pi-cron-*.log | head -1)
# Should end with "Done" or success message — no stack traces
```

---

## Pattern 16: Healthz Autofix Loop

**Symptoms:** `healthz-autofix.log` growing rapidly, same fixes repeatedly applied.

**Root cause:** Fix doesn't address root cause — healthz keeps detecting the same failure condition.

**Fix:**
```bash
# Read the autofix log to identify what's cycling
tail -100 /root/atlas/logs/healthz-autofix.log | grep -E "FIX|APPLY|loop"

# Identify the root cause from the repeated pattern
# Apply a permanent fix (see the relevant pattern in this skill's cookbooks)

# Temporarily pause autofix while fixing
systemctl stop atlas-healthz-autofix
# ... apply permanent fix ...
systemctl start atlas-healthz-autofix
```

**Verify:**
```bash
tail -20 /root/atlas/logs/healthz-autofix.log
# Fixes should not repeat — log should be quiet after permanent fix
```

---

## Pattern 17: Weekly Maintenance Failure

**Symptoms:** Disk filling up, large log files, `pycache` bloat.

**Fix:**
```bash
# Manual maintenance
cd /root/atlas && bash scripts/weekly_maintenance.sh

# Check disk
du -sh /root/atlas/*/ | sort -rh | head -10
```

**Verify:**
```bash
df -h / | tail -1
# Free space should be >2GB after maintenance

du -sh /root/atlas/logs/
# Should be <500MB for normal operations
```
