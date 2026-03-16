---
name: atlas-state-queries
description: "How to check every piece of Atlas system state — services, broker, positions, equity, config, research, dashboard, logs, and data freshness. Use when you need to inspect system status, diagnose issues, or verify health before operations."
---

# Atlas State Queries

Quick reference for checking any piece of Atlas system state. Use the Quick Lookup table to find what you need, then load the relevant cookbook for detailed commands.

---

## Quick Lookup

| I want to check... | Do this | Cookbook |
|---------------------|---------|---------|
| Are all services up? | `systemctl is-active atlas-dashboard atlas-dashboard-refresh atlas-telegram-bot` | Load cookbook: cookbooks/services-infra.md |
| What's my equity? | Read `logs/equity_curve_sp500.json` — last entry | Load cookbook: cookbooks/broker-portfolio.md |
| Open positions? | `python3 scripts/cli.py -m sp500 status` | Load cookbook: cookbooks/broker-portfolio.md |
| Broker connected? | `python3 scripts/cli.py -m sp500 broker` | Load cookbook: cookbooks/broker-portfolio.md |
| Config version? | `python3 -c "import json; print(json.load(open('config/active/sp500.json'))['version'])"` | Load cookbook: cookbooks/data-config.md |
| Data freshness? | `ls -lt data/cache/sp500/ \| head -5` | Load cookbook: cookbooks/data-config.md |
| Recent errors? | `journalctl -u atlas-<service> --no-pager -n 30 --since '1 hour ago'` | Load cookbook: cookbooks/services-infra.md |
| Research progress? | `ls research/results/*.tsv \| wc -l` | Load cookbook: cookbooks/research-logs.md |
| Dashboard working? | `curl -s http://localhost:8501/ \| head -5` | Load cookbook: cookbooks/dashboard-disk.md |
| Last trade plan? | `ls -lt plans/plan_sp500_*.json \| head -3` | Load cookbook: cookbooks/research-logs.md |
| Job status? | `atlas_jobs_list_runs` tool | Load cookbook: cookbooks/dashboard-disk.md |
| Stored state? | `atlas_state_list` tool | Load cookbook: cookbooks/dashboard-disk.md |
| Disk usage? | `du -sh /root/atlas` | Load cookbook: cookbooks/dashboard-disk.md |

---

## Cookbook Routing

| Domain | What's covered | Load with |
|--------|---------------|-----------|
| **Services & Infra** | Service health, systemd status, journal logs, restart procedures, service→code mapping | Load cookbook: cookbooks/services-infra.md |
| **Broker & Portfolio** | Broker connection, portfolio status, orders, trade history, equity curve, performance metrics | Load cookbook: cookbooks/broker-portfolio.md |
| **Data & Config** | Cache freshness, universe file, data refresh, active config, candidate comparison, config backups | Load cookbook: cookbooks/data-config.md |
| **Research & Logs** | Research queue, experiment results, brain knowledge, trade plans, log files, error search | Load cookbook: cookbooks/research-logs.md |
| **Dashboard & Disk** | Dashboard status, refresh, Pi extension state, disk usage, job runs, KV state, locks | Load cookbook: cookbooks/dashboard-disk.md |
