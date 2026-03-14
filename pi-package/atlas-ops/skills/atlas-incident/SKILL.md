---
name: atlas-incident
description: "Diagnose and resolve Atlas system errors, service failures, and operational issues. Covers 20+ known failure patterns with root causes and fixes. Use when something is broken, a service is down, an error appears in logs, a cron job fails, or the user reports an incident. Also use proactively when atlas-context-injector reports failed services."
---

# Atlas Incident Response

Systematic diagnosis and resolution of Atlas system failures.

---

## Triage Procedure

Follow this order every time — don't skip steps:

```
1. IDENTIFY    → What failed? Service? Cron? Backtest? Broker?
2. GATHER      → Check logs, service status, recent changes
3. MATCH       → Compare against known failure patterns below
4. FIX         → Apply the documented fix
5. VERIFY      → Confirm the fix worked
6. RECORD      → Update tasks/lessons.md if new pattern
7. NOTIFY      → Send resolution summary via Telegram
```

### Step 1: Quick Status Scan

```bash
# Services
systemctl is-active atlas-dashboard atlas-dashboard-refresh atlas-telegram-bot atlas-director atlas-research-runner atlas-research-window

# Recent errors across all logs
grep -rli "error\|exception\|traceback\|failed\|killed" /root/atlas/logs/*.log --include="*.log" | head -10

# Disk space
df -h / | tail -1

# Recent systemd failures
systemctl list-units --failed 'atlas-*' --no-pager
```

### Step 2: Gather Logs for Failed Service

```bash
# Service journal (last 50 lines)
journalctl -u atlas-<name> --no-pager -n 50

# With timestamps from last hour
journalctl -u atlas-<name> --no-pager --since "1 hour ago"

# Application logs
tail -100 /root/atlas/logs/<relevant>.log
```

---

## Known Failure Patterns

### Service Failures

#### 1. OOM Kill (research-runner, research-window)
**Symptoms:** Service status `failed`, journal shows `Killed` or `signal=9/KILL`, memory peak near limit.
**Root cause:** Large backtest consuming too much memory (350MB+ for research-runner, 1GB+ for research-window).
**Fix:**
```bash
# Check current memory usage peak
journalctl -u atlas-research-runner --no-pager -n 5 | grep "memory peak"

# Increase memory limit
systemctl edit atlas-research-runner
# Add under [Service]:
# MemoryMax=1G

systemctl daemon-reload
systemctl restart atlas-research-runner
```

#### 2. Timeout Kill (director, research-runner)
**Symptoms:** Journal shows `start operation timed out. Terminating`, `code=killed, status=15/TERM`.
**Root cause:** portfolio_optimizer or backtest took longer than systemd TimeoutStartSec.
**Fix:**
```bash
# Check current timeout
systemctl show atlas-director -p TimeoutStartSec

# Increase timeout
systemctl edit atlas-director
# Add under [Service]:
# TimeoutStartSec=900

systemctl daemon-reload
systemctl restart atlas-director
```

#### 3. Orphan Processes (research-window)
**Symptoms:** `Unit process XXXX remains running after unit stopped`, service fails on next start.
**Root cause:** ProcessPoolExecutor workers not cleaned up on service stop.
**Fix:**
```bash
# Find and kill orphan python processes from research
ps aux | grep "research\|sweep\|autoresearch" | grep -v grep
kill <pids>

# Then restart
systemctl restart atlas-research-window
```

#### 4. Service Won't Start — Port Conflict (dashboard)
**Symptoms:** `Address already in use`, dashboard fails to bind.
**Fix:**
```bash
# Find what's using the port
lsof -i :8501
kill <pid>
systemctl restart atlas-dashboard
```

#### 5. Telegram Bot Connection Error
**Symptoms:** Bot stops responding, journal shows connection timeout.
**Fix:**
```bash
systemctl restart atlas-telegram-bot
sleep 3
systemctl is-active atlas-telegram-bot
```

---

### Broker & Trading Failures

#### 6. Broker Returns $0 Equity
**Symptoms:** Equity curve shows $0, state file corruption.
**Root cause:** Broker API offline but returning valid-looking response.
**Fix:** NEVER write state when equity is $0. Check `broker_data_valid` flag. If state was corrupted:
```bash
# Check broker status
cd /root/atlas && python3 scripts/cli.py -m sp500 broker

# If broker is actually online with non-zero equity, state was corrupted
# Check equity curve for bad entries
python3 -c "
import json
curve = json.load(open('logs/equity_curve_sp500.json'))
bad = [e for e in curve if e['equity'] == 0]
print(f'Bad entries: {len(bad)}')
if bad: print(bad)
"
```

#### 7. Alpaca DNS Resolution Failure
**Symptoms:** Connection timeout to api.alpaca.markets.
**Fix:**
```bash
# Check /etc/hosts for the fix
grep alpaca /etc/hosts
# Should have: 34.x.x.x api.alpaca.markets

# If missing, add it
echo "34.232.237.2 api.alpaca.markets" >> /etc/hosts

# Verify
cd /root/atlas && python3 scripts/cli.py -m sp500 broker
```

#### 8. Protective Order Sync Failure
**Symptoms:** `sync_protective_orders.py` errors in logs.
**Fix:**
```bash
# Check the log
tail -30 /root/atlas/logs/sync_protective.log

# Manual sync
cd /root/atlas && python3 scripts/sync_protective_orders.py --market sp500
```

---

### Data & Cache Failures

#### 9. Stale Data Cache
**Symptoms:** Backtest results look wrong, "stale data" warnings, cache files >24h old.
**Fix:**
```bash
# Check cache age
ls -lt data/cache/sp500/ | head -5

# Refresh
cd /root/atlas && python3 scripts/cli.py -m sp500 ingest
```

#### 10. Corrupted Parquet Files
**Symptoms:** `ArrowInvalid` or `ParquetException` errors.
**Fix:**
```bash
# Find corrupted files
cd /root/atlas
python3 -c "
import pandas as pd
from pathlib import Path
for f in Path('data/cache/sp500').glob('*.parquet'):
    try: pd.read_parquet(f)
    except: print(f'CORRUPT: {f}')
"

# Delete corrupted and re-ingest
rm <corrupted_files>
python3 scripts/cli.py -m sp500 ingest
```

#### 11. Wrong Tickers in Cache (ASX contamination)
**Symptoms:** Backtest includes unexpected tickers, US tickers with .AX suffix.
**Root cause:** Lesson #25 — earlier pipeline bug left US tickers in ASX cache.
**Fix:** Filter loaded tickers against `market.get_formatted_tickers()`.

---

### Strategy & Backtest Failures

#### 12. Strategy Import Error (dormant strategy drift)
**Symptoms:** `ImportError`, `AttributeError`, `TypeError` when running dormant strategy.
**Root cause:** Lesson #15 — dormant strategies accumulate API drift bugs.
**Fix:**
```bash
# Test import
cd /root/atlas
python3 -c "from strategies.<name> import <ClassName>; <ClassName>({})"

# Common issues:
# - generate_signals() signature changed (missing config arg)
# - calc_atr() call pattern changed
# - Series comparison ambiguity (use .item() or .iloc[0])
# - calc_position_size returns dict, not int
```

#### 13. Backtest Returns 0 Trades
**Symptoms:** Sharpe=NaN, trades=0 in backtest output.
**Root cause:** Strategy generates 0 signals (config params too restrictive, or bug).
**Fix:**
```bash
# Quick screen to check signal generation
cd /root/atlas
python3 -c "
from research.quick_screen import screen_strategy
from utils.config import get_active_config
cfg = get_active_config('sp500')
r = screen_strategy('<strategy_name>', cfg, market='sp500')
print(r)
"
```

#### 14. Config Parse Error
**Symptoms:** `json.JSONDecodeError`, `KeyError` on config access.
**Fix:**
```bash
# Validate JSON
python3 -m json.tool config/active/sp500.json > /dev/null && echo "OK" || echo "INVALID"

# Check required sections
python3 -c "
import json
c = json.load(open('config/active/sp500.json'))
for key in ['version', 'trading', 'risk', 'strategies', 'universe', 'backtest']:
    print(f'{key}: {\"present\" if key in c else \"MISSING\"}')"
```

---

### Cron & Automation Failures

#### 15. pi-cron.sh Failed (premarket/postclose)
**Symptoms:** No plan generated, no Telegram notification, log shows error.
**Fix:**
```bash
# Check the log
ls -lt /root/atlas/logs/pi-cron-*.log | head -3
tail -50 $(ls -t /root/atlas/logs/pi-cron-*.log | head -1)

# Re-run manually
cd /root/atlas && bash scripts/pi-cron.sh premarket sp500
```

#### 16. Healthz Autofix Loop
**Symptoms:** healthz-autofix.log growing rapidly, same fixes repeatedly applied.
**Root cause:** Fix doesn't address root cause, healthz keeps detecting failure.
**Fix:** Read the autofix log, identify the cycling issue, apply a permanent fix.

#### 17. Weekly Maintenance Failure
**Symptoms:** Disk filling up, large log files, pycache bloat.
**Fix:**
```bash
# Manual maintenance
cd /root/atlas && bash scripts/weekly_maintenance.sh

# Check disk
du -sh /root/atlas/*/ | sort -rh | head -10
```

---

### Research System Failures

#### 18. Research Queue JSON Corruption
**Symptoms:** `json.JSONDecodeError` when reading queue file.
**Root cause:** Lesson #33 — parallel writes without file locking.
**Fix:**
```bash
# Check queue file
python3 -m json.tool research/queue/*.json 2>&1

# If corrupted, restore from backup or recreate
```

#### 19. stage_candidate() Clobbered Reoptimizer Output
**Symptoms:** OOS validation shows identical metrics to active config.
**Root cause:** Lesson #34 — stage_candidate overwrites candidate with active config copy.
**Fix:** Check if candidate file was clobbered:
```bash
diff <(python3 -m json.tool config/active/sp500.json) <(python3 -m json.tool config/candidates/<candidate>.json)
```
If identical, the candidate was clobbered. Re-run the reoptimization.

#### 20. Double-Multiplication in Metrics Display
**Symptoms:** CAGR showing 3814% instead of 38.14%.
**Root cause:** Lesson #35 — `_pct` metrics already in percent, formatter multiplied again.
**Fix:** Check if the display code uses `_ALREADY_PCT` vs `_DECIMAL_PCT` split.

---

## Escalation Rules

| Severity | Action |
|----------|--------|
| Service down < 1 hour | Auto-fix, restart, verify |
| Service down > 1 hour | Fix + Telegram alert |
| Broker connection failure | Alert immediately, do NOT write state |
| Data corruption | Alert, stop dependent services, fix, re-validate |
| Config corruption | Restore from backup (`atlas_risk_restore_config_backup`), alert |
| Unknown failure pattern | Add to this skill after resolution |

---

## Post-Incident Checklist

After every incident resolution:

- [ ] Root cause identified and documented
- [ ] Fix verified (service running, no errors)
- [ ] Dependent services checked
- [ ] `tasks/lessons.md` updated if new pattern
- [ ] Telegram notification sent with resolution summary
- [ ] This skill updated if pattern was missing
