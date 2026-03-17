---
name: atlas-healthz
description: "Run a complete Atlas system health audit covering infrastructure, data, config, broker, portfolio, cron, research, logging, disk, and backtest performance. Use when asked to check system health, diagnose issues, run a status audit, verify Atlas is working, or troubleshoot why something failed. Also use proactively before major operations (deployments, config promotions, re-optimizations)."
type: reference
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

---

## Section Deep-Dive

### `infra` -- Infrastructure
**Run with:** `--section infra`

**Checks:**
- `alpaca_api`: Config reads `trading.broker`; always ok for Alpaca (REST only, no gateway process needed)
- `telegram_bot`: `systemctl is-active atlas-telegram-bot` -- must be active
- `dashboard_service`: `systemctl is-active atlas-dashboard` -- warn if down (non-critical)
- `secrets_telegram`: `~/.atlas-secrets.json` has `telegram_bot_token` + `telegram_chat_id`
- `secrets_broker`: Same file has `ALPACA_API_KEY` + `ALPACA_SECRET_KEY`

**OK looks like:** 5 green checks -- both services active, all 4 credential keys present.

**Most common failure:** `telegram_bot fail` -- service crashed (OOM or config error).
**Fix:** `journalctl -u atlas-telegram-bot -n 20` then `systemctl restart atlas-telegram-bot`

---

### `data` -- Data and Cache
**Run with:** `--section data`

**Checks:**
- `cache_dir`: `data/cache/{market_id}/` exists; fail if missing entirely
- `cache_files`: Parquet count >= 50 tickers; warn if sparse (new install or partial ingest)
- `cache_freshness`: Newest parquet < 48h old (tolerates weekends); warn if stale
- `cache_empty_files`: Samples 50 parquets for 0-row or unreadable files; warn if any found
- `cache_short_history`: Samples 50 parquets for < 100 rows; warn if any found
- `universe`: `data/processed/universe_{market_id}.json` exists with ticker list

**OK looks like:** All 6 checks green, 400-503 parquets, newest file < 24h old.

**Most common failure:** `cache_freshness warn` -- no ingest since last session.
**Fix:** `python3 scripts/cli.py -m sp500 ingest` (takes ~5 min)

---

### `config` -- Configuration
**Run with:** `--section config`

**Checks:**
- `config_file`: `config/active/{market_id}.json` exists; fail if missing
- `config_version`: Reads `.version` field (informational)
- `config_risk/fees/strategies/backtest/data/trading`: Each top-level section present; fail if missing
- `strategies_enabled`: At least one strategy has `enabled: true`; warn if none
- `strategies_disabled`: Lists disabled strategies (informational, always ok)
- `risk_equity`: `risk.starting_equity > 0`; fail if zero or missing
- `risk_positions`: Reports max_open_positions and max_risk_per_trade_pct (informational)
- `trading_mode`: Reports mode/broker/live_enabled/dry_run_first (informational)
- `optimization`: Reports last-optimized date and Sharpe from _optimization_metadata

**OK looks like:** All 6 required sections present, >= 1 strategy enabled, equity > 0.

**Most common failure:** `strategies_enabled warn` -- all strategies accidentally disabled after a config edit.
**Fix:** Open `config/active/sp500.json`, set `enabled: true` on desired strategies, re-run healthz.

---

### `broker` -- Broker Connection
**Run with:** `--section broker`

**Checks:**
- `broker_connect`: `broker.connect()` via `brokers.registry.get_broker()`; fail if auth error or API down
- `broker_account`: Reports live equity, cash, and position count from Alpaca
- `broker_positions`: Lists open position tickers (informational)
- `broker_orders`: Warns if pending open orders present (may block new signals)
- `broker_error`: Catch-all for unexpected exceptions during broker init

**OK looks like:** Connected, equity reported, no open orders (or orders explained by today plan).

**Most common failure:** `broker_connect fail` -- API keys revoked or Alpaca API outage.
**Fix:** Check `~/.atlas-secrets.json` keys; verify at https://app.alpaca.markets.

---

### `portfolio` -- Portfolio State
**Run with:** `--section portfolio`

**Checks:**
- `live_state`: `brokers/state/live_{market_id}.json` exists; warn if missing (system never ran)
- `equity_history`: Array of equity snapshots; warn if empty (no postclose runs yet)
- `closed_trades`: Count of closed trades in state (informational at any count)
- `halt_status`: `state.halted == false`; **fail** if true (drawdown limit triggered)
- `latest_equity`: Reports current equity and position count from most recent snapshot
- `trade_quality`: Win rate and total PnL across all closed trades
- `latest_plan`: Date and status of most recent `plans/plan_*.json`

**OK looks like:** Not halted, equity history growing, latest plan is EXECUTED or APPROVED.

**Most common failure:** `halt_status fail` -- drawdown limit triggered, trading stopped.
**Fix:** Investigate drawdown cause first. Then clear halt via LivePortfolio.clear_halt(). Never clear without understanding the cause.

---

### `cron` -- Cron and Automation
**Run with:** `--section cron`

**Checks:**
- `crontab`: `crontab -l` contains >= 1 Atlas jobs; warn if empty
- `cron_premarket/postclose/research/dashboard/maintenance`: Each key job present; warn if missing
- `cron_last_run`: Newest `logs/pi-cron-*.log` < 30h old; warn if stale
- `recent_recoveries`: `logs/recover_*.log` files < 72h old; warn if present (sign of recent auto-recovery)

**OK looks like:** 5 jobs installed, last cron log < 24h ago, no recent recovery logs.

**Most common failure:** `cron_last_run warn` after a weekend -- check if premarket ran Friday.
**Fix:** `crontab -l | grep atlas` to confirm schedule; `journalctl -u cron -n 20` for system errors.

---

### `research` -- Research Pipeline
**Run with:** `--section research`

**Checks:**
- `research_queue`: `research/queue.json` exists with experiments; warn if missing
- `research_pending`: Reports count of queued experiments
- `research_journal`: Reports verdict distribution from `research/journal.json`
- `research_experiments`: Count of `research/experiments/exp-*.json` result files

**OK looks like:** Queue exists, journal has entries, at least some experiments completed.

**Most common failure:** `research_queue warn` -- queue file missing.
**Fix:** During live accumulation phase, research is intentionally paused -- this warn is expected and non-actionable.

---

### `logging` -- Logging and Observability
**Run with:** `--section logging`

**Checks:**
- `decision_journal`: `journal/decision_journal.json` exists with signal entries
- `dj_fields`: Latest entry has all expected fields (timestamp, ticker, strategy, confidence, features, action, market_id)
- `dj_market_id`: Latest entry market_id non-empty; warn for old-format entries (self-heals on next plan)
- `trade_ledger`: `journal/trade_ledger.json` exists with entries
- `execution_journal`: `logs/live_executions.jsonl` exists (JSONL of broker events)
- `eod_summaries`: `logs/eod_summary_*.json` files exist (one per trading day)
- `dashboard_data`: `dashboard/data/dashboard-data.json` < 24h old

**OK looks like:** All 4 journals present, dashboard data fresh, no missing fields.

**Most common failure:** `dashboard_data warn` -- dashboard refresh cron skipped.
**Fix:** `python3 scripts/cli.py -m sp500 dashboard` or wait for next scheduled refresh.

---

### `disk` -- Disk and Housekeeping
**Run with:** `--section disk`

**Checks:**
- `project_size`: du -sh of project root (informational)
- `large_logs`: Any file in `logs/` > 5 MB; warns with filename and size
- `atlas_log`: `atlas.log` size; warn if > 10 MB
- `pycache`: Count of __pycache__ dirs; warn if > 5
- `disk_free`: Free space on /; warn < 5 GB, fail < 1 GB

**OK looks like:** Project < 2 GB, no individual log > 5 MB, > 5 GB free disk.

**Most common failure:** `pycache warn` after Python package updates.
**Fix:** `bash scripts/weekly_maintenance.sh` (automated on Saturday maintenance cron)

---

### `backtest` -- Backtest Performance
**Run with:** `--section backtest`

**Checks:**
- `optimization_meta`: _optimization_metadata in config; warn if absent (config never optimized)
- `sharpe`: ok >= 0.8, warn 0.3-0.8, **fail < 0.3** (optimized Sharpe from last reopt)
- `cagr`: warn if < 5% (annualized CAGR from optimization)
- `oos_ratio`: OOS Sharpe divided by IS Sharpe; warn if < 0.70 (overfitting signal)
- `perturbation`: warn if any of 10 perturbation trials showed negative CAGR
- `walk_forward`: warn if < 60% of walk-forward windows were profitable

**OK looks like:** Sharpe >= 0.8, CAGR > 10%, OOS ratio >= 0.70, 0 perturbation negatives, >= 60% WF windows.

**Most common failure:** `oos_ratio warn` -- OOS/IS drift below 0.70 (data staleness or regime shift).
**Fix:** Trigger reoptimization via the `atlas-reoptimize` skill.

---

## Saturday Health Report Pattern

The cron runs a health check every **Saturday at 9:00 AM AEST** as part of `scripts/weekly_maintenance.sh`.

**Purpose:** Weekly system audit -- catch silent degradation before it becomes a Monday incident.

**Output:** Full JSON report saved to `logs/health_check_YYYY-MM-DD.json`

**Quick summary with Pi tool:**

```
atlas_artifacts_summarize(path='logs/health_check_2026-03-15.json', kind='health_check')
```

**Compare two Saturday reports to detect week-over-week drift:**

```
atlas_artifacts_compare(
  leftPath='logs/health_check_2026-03-08.json',
  rightPath='logs/health_check_2026-03-15.json'
)
```

**Key things to review in the Saturday report:**

| Area | What to look for | Action if bad |
|------|-----------------|---------------|
| **Backtest drift** | sharpe dropped vs last Saturday; oos_ratio < 0.70 | Queue reoptimization |
| **Disk growth** | project_size +500 MB week-over-week; large_logs appearing | weekly_maintenance.sh or manual purge |
| **Data freshness** | cache_freshness > 72h (Friday ingest may have failed) | Manual ingest before Monday open |
| **Cron health** | recent_recoveries > 0 (auto-recovery fired during the week) | Check logs/recover_*.log for root cause |
| **Broker account** | Equity unexpectedly low; open orders stuck | Check Alpaca dashboard, review pending orders |

---

## Pre-Operation Health Gates

Run healthz **before** these operations. Never proceed if any gated section has a `fail` verdict.

### Before Config Promotion
**Sections required:** All 10 (full health check)
**Gate:** Zero `fail` verdicts. Warnings acceptable.

```bash
python3 pi-package/atlas-ops/skills/atlas-healthz/scripts/healthz.py --market sp500
# Must exit 0 (healthy) or 1 (warnings only) -- not 2 (failures present)
```

**Why:** A failing broker or stale data invalidates the comparison between old and new config.

### Before Reoptimization
**Sections required:** `data`, `config`, `backtest`

```bash
python3 pi-package/atlas-ops/skills/atlas-healthz/scripts/healthz.py --section data --market sp500
python3 pi-package/atlas-ops/skills/atlas-healthz/scripts/healthz.py --section config --market sp500
python3 pi-package/atlas-ops/skills/atlas-healthz/scripts/healthz.py --section backtest --market sp500
```

**Why:** Stale data produces overfit parameters. Broken config means the optimizer reads wrong risk settings.

### Before Daily Plan (Premarket)
**Sections required:** `data`, `broker`, `portfolio` -- the premarket cron verifies these implicitly.
**Gate:** `data.cache_freshness` ok, `broker_connect` ok, `halt_status` ok.
**Why:** A plan generated with stale data or against a halted portfolio wastes the trading day.

---

## Interpreting Cascading Failures

One root failure typically produces several downstream check failures. Always trace to the **root cause**, not the symptoms.

### Chain 1: Stale Data

```
data.cache_freshness WARN/FAIL
  -> backtest.sharpe      stale (optimization ran on old data)
  -> config.optimization  stale (metadata reflects bad backtest)
  -> portfolio signals    unreliable (strategy used wrong price history)
```

**Root fix:** `python3 scripts/cli.py -m sp500 ingest` -- refreshes cache; checks clear on next run.

### Chain 2: Broker Failure

```
broker.broker_connect FAIL
  -> portfolio.equity_history  GAP (no postclose snapshot taken)
  -> portfolio.latest_equity   STALE (reflects yesterday)
  -> logging.eod_summaries     MISSING (EOD settlement could not write)
```

**Root fix:** Restore broker connection (API keys, Alpaca status page), then re-run postclose manually.

### Chain 3: Cron Failure

```
cron.cron_last_run WARN (>30h gap)
  -> data.cache_freshness    WARN (ingest did not run)
  -> portfolio.latest_plan   STALE (plan not generated)
  -> portfolio.closed_trades NOT_UPDATED (execution did not run)
  -> logging.eod_summaries   MISSING (EOD settlement skipped)
```

**Root fix:** Verify crontab schedule; check `journalctl -u cron -n 20`.
Re-run missed jobs in order: ingest -> plan -> approve -> paper_run -> eod_settlement.

### Chain 4: Halt Trigger

```
portfolio.halt_status FAIL
  -> cron.premarket           plan generated but execution guard trips
  -> broker.broker_orders     protective orders remain open
  -> logging.decision_journal no new entries (signals blocked while halted)
```

**Root fix:** Investigate drawdown cause before clearing halt. If safe: clear_halt(), then re-run premarket.

---

## Integration with Other Skills

### atlas-incident
Healthz **finds** problems; the incident skill **fixes** them. Standard workflow:
1. Run healthz -- identify the failing section and check name
2. Open `atlas-incident` skill -- find the matching failure pattern by check name
3. Follow the incident playbook -- re-run `--section <name>` to confirm resolution

Example: `telegram_bot fail` -> incident skill "Telegram bot down" pattern -> restart and verify.

### atlas-reoptimize
Phase 1 of the reoptimize skill **always runs healthz first**:
- Checks `data`, `config`, `backtest` sections before queuing optimization
- If `backtest.sharpe` drops below threshold, reoptimization is triggered
- If `data.cache_freshness` is stale, ingest first then reoptimize
- Post-promotion: run full healthz to confirm new config loaded correctly

### atlas-daily
Premarket and postclose cron jobs include **implicit health gates**:
- Premarket: verifies broker connectivity and data freshness before plan generation
- Postclose: verifies execution journal and equity snapshot before EOD settlement
- When a daily cron fails, first diagnostic step is healthz on the suspected section:

```bash
python3 pi-package/atlas-ops/skills/atlas-healthz/scripts/healthz.py --market sp500 --section <failing_area>
```

- Then escalate to `atlas-incident` if healthz confirms a failure.
