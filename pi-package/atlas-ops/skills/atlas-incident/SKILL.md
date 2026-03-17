---
name: atlas-incident
description: "Diagnose and resolve Atlas system errors, service failures, and operational issues. Covers 20+ known failure patterns with root causes and fixes. Use when something is broken, a service is down, an error appears in logs, a cron job fails, or the user reports an incident. Also use proactively when atlas-context-injector reports failed services."
type: reference
---

# Atlas Incident Response

Systematic diagnosis and resolution of Atlas system failures.

---

## Triage Procedure

Follow this order every time — don't skip steps:

```
1. IDENTIFY    → What failed? Service? Cron? Backtest? Broker?
2. GATHER      → Check logs, service status, recent changes
3. MATCH       → Use the Cookbook Routing Table below to find the right cookbook
4. FIX         → Load the cookbook and apply the documented fix
5. VERIFY      → Confirm the fix worked (each cookbook has a Verify step)
6. RECORD      → Update tasks/lessons.md if new pattern
7. NOTIFY      → Send resolution summary via Telegram
```

### Quick Status Scan

```bash
# Services
systemctl is-active atlas-dashboard atlas-dashboard-refresh atlas-telegram-bot \
  atlas-director atlas-research-runner atlas-research-window

# Recent errors across all logs
grep -rli "error\|exception\|traceback\|failed\|killed" /root/atlas/logs/*.log | head -10

# Disk space
df -h / | tail -1

# Recent systemd failures
systemctl list-units --failed 'atlas-*' --no-pager
```

### Gather Logs for Failed Service

```bash
# Service journal (last 50 lines)
journalctl -u atlas-<name> --no-pager -n 50

# With timestamps from last hour
journalctl -u atlas-<name> --no-pager --since "1 hour ago"

# Application logs
tail -100 /root/atlas/logs/<relevant>.log
```

---

## Cookbook Routing Table

Match your symptom to the right cookbook, then load it with the read tool.

| Symptom | Cookbook | Load with |
|---------|----------|-----------|
| Service down, won't start, OOM, timeout, orphan process, port conflict, Telegram bot | Service Failures | `Load cookbook: cookbooks/service-failures.md` |
| Broker error, $0 equity, DNS resolution failure, protective order sync | Broker & Trading | `Load cookbook: cookbooks/broker-trading.md` |
| Stale cache, corrupted parquet, wrong tickers, ASX contamination | Data & Cache | `Load cookbook: cookbooks/data-cache.md` |
| Import error, 0 trades, config parse error, JSONDecodeError on config | Strategy & Backtest | `Load cookbook: cookbooks/strategy-backtest.md` |
| Cron failed, no plan generated, healthz autofix loop, disk full, maintenance | Cron & Automation | `Load cookbook: cookbooks/cron-automation.md` |
| Queue corruption, stage_candidate clobber, metrics showing wrong values | Research System | `Load cookbook: cookbooks/research-system.md` |

Cookbooks live at: `pi-package/atlas-ops/skills/atlas-incident/cookbooks/`

---

## Escalation Rules

| Severity | Action |
|----------|--------|
| Service down < 1 hour | Auto-fix, restart, verify |
| Service down > 1 hour | Fix + Telegram alert |
| Broker connection failure | Alert immediately, do NOT write state |
| Data corruption | Alert, stop dependent services, fix, re-validate |
| Config corruption | Restore from backup (`atlas_risk_restore_config_backup`), alert |
| Unknown failure pattern | Add to the relevant cookbook after resolution |

---

## Post-Incident Checklist

After every incident resolution:

- [ ] Root cause identified and documented
- [ ] Fix verified (service running, no errors)
- [ ] Dependent services checked
- [ ] `tasks/lessons.md` updated if new pattern
- [ ] Telegram notification sent with resolution summary
- [ ] Relevant cookbook updated if pattern was missing or steps changed
