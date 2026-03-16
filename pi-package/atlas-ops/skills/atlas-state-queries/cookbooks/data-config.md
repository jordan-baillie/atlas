# Data & Config — State Queries

## Cache file age

```bash
# SP500 cache — check most recent files
ls -lt data/cache/sp500/ 2>/dev/null | head -5

# Count cached tickers
ls data/cache/sp500/*.parquet 2>/dev/null | wc -l

# Check if cache is stale (>24h)
find data/cache/sp500/ -name "*.parquet" -mmin +1440 | wc -l
```

## Refresh data

```bash
cd /root/atlas && python3 scripts/cli.py -m sp500 ingest
```

Or use `atlas_jobs_run` tool with job `cli_ingest`.

## Universe file

```bash
python3 -c "
import json
u = json.load(open('data/universe_sp500.json', 'r') if __import__('os').path.exists('data/universe_sp500.json') else open('universe/sp500.json', 'r'))
print(f'Tickers: {len(u.get(\"tickers\", u if isinstance(u, list) else []))}')
" 2>/dev/null || echo "Universe file not found at expected path"
```

---

## Read active config

```bash
python3 -c "
import json
cfg = json.load(open('config/active/sp500.json'))
print(f'Version: {cfg[\"version\"]}')
print(f'Mode: {cfg[\"trading\"][\"mode\"]}')
print(f'Approval: {cfg[\"trading\"][\"approval_required\"]}')
strats = [k for k,v in cfg['strategies'].items() if v.get('enabled')]
print(f'Strategies ({len(strats)}): {strats}')
print(f'Max positions: {cfg[\"risk\"][\"max_open_positions\"]}')
"
```

## Compare candidate vs active

Use the `atlas_risk_check_config_promotion` tool:
```
Tool: atlas_risk_check_config_promotion
Params: { "candidatePath": "config/candidates/<file>.json" }
```

## List config backups

Use the `atlas_risk_list_config_backups` tool, or:
```bash
ls -lt config/versions/active_config_pre_reopt_*.json | head -10
```

## Diff two configs

```bash
diff <(python3 -m json.tool config/active/sp500.json) <(python3 -m json.tool config/candidates/<candidate>.json) | head -60
```
