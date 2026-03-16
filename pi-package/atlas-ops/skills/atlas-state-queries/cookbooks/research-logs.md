# Research & Logs — State Queries

## Research queue

```bash
ls research/queue/ 2>/dev/null
cat research/queue/*.json 2>/dev/null | python3 -m json.tool | head -40
```

## Experiment results

```bash
# List all results
ls -lt research/results/ | head -20

# Read a TSV result
head -5 research/results/<experiment>.tsv
```

## Brain knowledge base

```bash
cat memory/SUMMARY.md
```

## Check what's been tested

```bash
# Research results by strategy/experiment
ls research/results/*.tsv | sed 's|.*/||;s|\.tsv||'
```

---

## Trade Plans

### Latest plan

```bash
ls -lt plans/plan_sp500_*.json | head -3
```

### Read plan summary

```bash
python3 -c "
import json
from pathlib import Path
plans = sorted(Path('plans').glob('plan_sp500_*.json'), reverse=True)
if plans:
    p = json.load(open(plans[0]))
    print(f'Date: {p.get(\"trade_date\")}')
    print(f'Status: {p.get(\"status\")}')
    print(f'Entries: {len(p.get(\"proposed_entries\", []))}')
    print(f'Exits: {len(p.get(\"proposed_exits\", []))}')
    print(f'Rejections: {len(p.get(\"rejected_entries\", []))}')
"
```

Or use `atlas_artifacts_summarize` tool with the plan path.

### Check plan gate

Use `atlas_risk_check_plan_gate` tool:
```
Tool: atlas_risk_check_plan_gate
Params: { "date": "2026-03-14", "action": "evaluate" }
```

---

## Log file locations

| Log | Path | What |
|-----|------|------|
| Health check | `logs/healthz-autofix.log` | Healthz cron output |
| Intraday monitor | `logs/intraday_sp500.log` | Position monitoring during market hours |
| Protective orders | `logs/sync_protective.log` | Stop-loss/take-profit sync |
| Maintenance | `logs/maintenance.log` | Weekly cleanup |
| Ceasefire monitor | `logs/ceasefire-cron.log` | Geopolitical monitor |
| Iran monitor | `logs/iran-monitor-cron.log` | Iran situation tracker |
| Dashboard refresh | (systemd journal) | `journalctl -u atlas-dashboard-refresh` |

### Tail recent logs

```bash
# Application logs
tail -50 logs/healthz-autofix.log
tail -50 logs/intraday_sp500.log

# Service logs
journalctl -u atlas-telegram-bot --no-pager -n 30
journalctl -u atlas-research-runner --no-pager -n 30
```

### Search for errors

```bash
grep -i "error\|exception\|traceback\|failed" logs/*.log | tail -20
```
