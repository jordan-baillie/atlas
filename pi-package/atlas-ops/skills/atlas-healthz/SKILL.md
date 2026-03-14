---
name: atlas-healthz
description: "Run a complete Atlas system health audit covering infrastructure, data, config, broker, portfolio, cron, research, logging, disk, and backtest performance. Use when asked to check system health, diagnose issues, run a status audit, verify Atlas is working, or troubleshoot why something failed. Also use proactively before major operations (deployments, config promotions, re-optimizations)."
---

# Atlas Health Check

Complete audit of all Atlas subsystems via `scripts/healthz.py`. Produces structured verdicts (ok/warn/fail) with actionable messages.

## Running the check

```bash
cd /root/atlas && python3 pi-package/atlas-ops/skills/atlas-healthz/scripts/healthz.py --market sp500
```

Options:
- `--market sp500|asx` — target market (default: sp500)
- `--section <name>` — run single section: `infra`, `data`, `config`, `broker`, `portfolio`, `cron`, `research`, `logging`, `disk`, `backtest`
- `--json` — raw JSON output for programmatic use
- `--project /path` — override project root

Exit codes: `0` = healthy, `1` = warnings, `2` = failures.

## Sections checked

| Section | What it verifies |
|---------|-----------------|
| **infra** | Telegram bot service, dashboard service, secrets file |
| **data** | Cache directory, parquet count/freshness/integrity, universe file |
| **config** | Active config exists, required sections present, strategies enabled/disabled, risk params, trading mode, optimization metadata |
| **broker** | Alpaca connection, account equity/cash, open positions, pending orders |
| **portfolio** | Live state file, equity history, closed trades, halt status, latest plan |
| **cron** | Crontab jobs installed (premarket/postclose/research/dashboard/maintenance), last run recency, recent recovery events |
| **research** | Queue status counts, journal verdicts, experiment result files |
| **logging** | Decision journal (field completeness), trade ledger, execution journal, EOD summaries, dashboard data freshness |
| **disk** | Project size, large log files, atlas.log size, \_\_pycache\_\_ cleanup, free disk space |
| **backtest** | Optimized Sharpe/CAGR, OOS validation ratio, perturbation trials, walk-forward profitability |

## Interpreting results

Run the script, then:

1. **Read the summary line** — overall verdict (healthy/degraded/unhealthy) + counts.
2. **Scan for ⚠️ and ❌** — focus on failures first, then warnings.
3. **Act on messages** — each check includes what's wrong and what to do.

Common fixes:
- `cache_freshness warn` → `cd /root/atlas && python3 scripts/cli.py -m sp500 ingest`
- `telegram_bot fail` → `systemctl restart atlas-telegram-bot`
- `pycache warn` → `bash scripts/weekly_maintenance.sh`
- `halt_status fail` → Check drawdown, then `python3 -c "from brokers.live_portfolio import LivePortfolio; ..."`
- `dj_market_id warn` → Old entries, self-heals on next plan generation

## When to use

- **Routine check**: Run daily or before approving plans
- **After failures**: First step when a cron job fails or Telegram alerts fire
- **Before promotions**: Verify system health before promoting a research candidate to active config
- **After changes**: Confirm nothing broke after code edits or config updates
- **With `--json`**: Pipe to downstream tools or Telegram alerts
