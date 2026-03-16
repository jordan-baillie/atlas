# Dashboard & Disk — State Queries

## Check dashboard is running

```bash
systemctl is-active atlas-dashboard atlas-dashboard-refresh
curl -s -o /dev/null -w '%{http_code}' http://localhost:8501/
```

## Refresh dashboard data manually

```bash
cd /root/atlas && python3 dashboard/generate_data.py
```

Or use `atlas_jobs_run` tool with job `dashboard_generate_data`.

## Dashboard data freshness

```bash
ls -lt dashboard/data/*.json 2>/dev/null | head -5
```

---

## Pi Extension State

### Job runs

```bash
# Use the tool:
atlas_jobs_list_runs  # params: { "limit": 10 }

# Or directly:
ls -lt .pi/atlas-runs/*.json 2>/dev/null | head -10
```

### Key-value state store

```bash
# Use the tool:
atlas_state_list  # params: { "scope": "default" }

# Or directly:
ls .pi/atlas-state/kv/default/ 2>/dev/null
```

### Locks

```bash
# Use the tool:
atlas_state_lock_status  # params: { "name": "heavy-backtest" }

# Or directly:
ls .pi/atlas-state/locks/ 2>/dev/null
cat .pi/atlas-state/locks/*.json 2>/dev/null
```

---

## Disk Usage

```bash
# Project size
du -sh /root/atlas

# Largest directories
du -sh /root/atlas/*/ 2>/dev/null | sort -rh | head -10

# Large log files
find /root/atlas/logs -name "*.log" -size +1M -exec ls -lh {} \;

# Free disk space
df -h /
```
