---
name: atlas-daily
description: "Run Atlas daily live-trading operations with explicit approval gates: data refresh, plan generation, risk summary, plan approval, execution, and dashboard refresh. Use for daily operational runs and incident response on daily automation failures."
type: workflow
---

# Atlas Daily

Use this skill when operating the day-to-day live-trading workflow, diagnosing cron failures, or manually intervening in plan generation and execution.

## Primary goals

- Generate or inspect today's plan without bypassing approval requirements
- Execute only approved plans
- Refresh dashboard artifacts after plan or execution changes
- Keep a clear audit trail of which job ran and what artifacts changed

---

## 1. Cron Schedule Context

All times are **AEST (Australia/Brisbane, UTC+10)**. The crontab lives at `crontab -l` on the VPS.

### Daily trading loop (SP500)

| Time (AEST) | Days | Job | Purpose |
|---|---|---|---|
| 19:00 Mon-Fri | `1-5` | `pi-cron.sh premarket sp500` | Ingest + plan (~5.5h before US open) |
| 08:00 Tue-Sat | `2-6` | `pi-cron.sh postclose sp500` | EOD settlement + dashboard |
| 01:30-07:30 Tue-Sat | `2-6` | `intraday_monitor.py -m sp500` | 30-min position monitor during US session |
| 19:15 Mon-Fri | `1-5` | `sync_protective_orders.py` | Post-entry SL/TP placement |
| 23:45 Mon-Fri | `1-5` | `sync_protective_orders.py` | Mid-session SL/TP catch-up |
| 06:30 Tue-Sat | `2-6` | `sync_protective_orders.py` | Post-market SL/TP confirm |

### Weekly/monthly automation

| Time (AEST) | Job | Purpose |
|---|---|---|
| Sat 09:00 | `pi-cron.sh health-check sp500` | Strategy performance health report |
| Sun 08:00 | `pi-cron.sh rejected-signals sp500` | Rejected signal analysis |
| Sun 06:00 | `weekly_maintenance.sh` | Log rotation, cache cleanup |
| 1st-of-month 09:00 | `pi-cron.sh slippage-cal sp500` | Slippage calibration |
| 1st-of-month 10:00 | `pi-cron.sh calibrate sp500` | Confidence calibration |

> **Lesson #21 - US Friday = Saturday AEST**: US Friday's session closes around 07:00 AEST Saturday.
> Post-close cron runs at 08:00 Tue-**Sat** (days `2-6`) to cover this. If Saturday post-close
> is missing from `crontab -l`, Friday trades will never settle.

---

## 2. Premarket Workflow (detailed)

`pi-cron.sh premarket sp500` runs at 19:00 AEST Mon-Fri. It does NOT approve or execute.

### Steps executed by the shell script

1. **Config validation pre-flight**
   - Runs `config/schema.py validate_config_file` against `config/active/sp500.json`
   - Warnings logged to `logs/pi-cron.log` — does **not** block plan generation
   - A warning means proceed but investigate after the session

2. **Volatility gate check** (`scripts/volatility_gate.py --check --market sp500 --json`)
   - Reads macro indicators (VIX, market breadth, etc.)
   - Exit codes:
     - `0` = OK, proceed normally
     - `1` = REDUCE — one indicator flagged; position sizes will be reduced 50% at execution
     - `2` = BLOCK — entries suspended; script sends alert and exits cleanly (not an error)
   - Gate output saved to `logs/volatility_gate_TIMESTAMP.json`
   - If BLOCKED: Telegram alert fires, plan generation is skipped. **Expected behavior** — not an incident.

3. **Pi agent dispatch** (only if not BLOCKED)
   - Skills loaded: `atlas-daily`, `atlas-state-queries`, `atlas-incident`, `atlas-lessons`
   - Agent instructions:
     - Check data freshness in `data/cache/`; run `cli_ingest -m sp500` if stale
     - Run `cli_plan -m sp500`
     - Summarize the plan (entries, risk, stop levels)
     - **Stop** — do NOT approve or execute
   - Log: `logs/pi-cron-premarket-TIMESTAMP.log`

4. **Telegram notification** (always, regardless of agent exit)
   - Sent by the shell script via `telegram_notify.py premarket-approve`
   - Includes plan file path and market
   - Waits for human approval via Telegram/Pi before execution proceeds

### Volatility gate action in plan context

When gate action is `reduce`, the agent prompt includes:
> "WARNING VOLATILITY GATE: 1 indicator flagged — position sizes will be reduced 50% at execution."

When OK:
> "OK Volatility gate: OK — no macro flags."

---

## 3. Postclose Workflow (detailed)

`pi-cron.sh postclose sp500` runs at 08:00 AEST Tue-Sat (1h after US close).

### Steps executed

1. **Research daemon health check** (informational)
   - Checks `systemctl is-active atlas-research-daemon`
   - Logs heartbeat from `/tmp/research-daemon-heartbeat.json`
   - Does not block postclose if daemon is down

2. **Pi agent dispatch**
   - Skills loaded: `atlas-daily`, `atlas-state-queries`, `atlas-incident`, `atlas-lessons`
   - Agent instructions:
     - Run `cli_eod_settlement -m sp500` — processes stop-loss/take-profit exits, updates equity
     - Run `dashboard_generate_data` — refreshes all dashboard artifacts
     - Summarize any exits triggered and final equity snapshot
   - Log: `logs/pi-cron-postclose-TIMESTAMP.log`

3. **Dashboard regeneration** (always, even if agent fails)
   - `dashboard/generate_data.py` runs unconditionally as a safety net

4. **Telegram summary**
   - Sent by shell via `telegram_notify.py postclose-ok`
   - Reports settled positions and equity

---

## 4. Manual Intervention Patterns

### Re-running a failed premarket

```bash
# Check what failed
tail -100 logs/pi-cron-premarket-*.log | grep -E "ERROR|FATAL|Traceback"
tail -50 logs/pi-cron.log

# Re-run manually (generates a new timestamped log)
scripts/pi-cron.sh premarket sp500

# Or trigger recovery mode
scripts/pi-cron.sh recover premarket sp500
```

### Force-generating a plan for a specific date

```bash
cd /root/atlas
python3 scripts/cli.py plan --market sp500 --date 2026-03-17
# Inspect the result:
python3 -m json.tool paper_engine/plans/plan_sp500_2026-03-17.json | head -60
```

### Approving and executing after manual plan generation

Use `atlas_risk` tools in order:

1. `atlas_risk_check_plan_gate(action="approve", planPath="paper_engine/plans/plan_sp500_DATE.json")`
2. Get explicit user confirmation
3. `atlas_risk_approve_plan(confirmed=True, planPath="...", approver="operator")`
4. `atlas_risk_check_plan_gate(action="execute", ...)`
5. Get explicit user confirmation again
6. `atlas_jobs_run(job="cli_paper_run", args={"date": "DATE", "m": "sp500"})`

### Emergency position exit

Use the Alpaca broker dashboard or CLI directly — do NOT use atlas scripts for emergency exits.
Atlas scripts expect normal EOD flow; out-of-band broker closes will be reconciled at postclose.

### Skipping a day (market holiday or circuit breaker)

Simply do nothing — the cron will not run again until the next scheduled trigger.
If premarket already ran and you need to prevent execution, delete the plan:

```bash
# Remove today's plan to prevent accidental execution
python3 -c "import os; os.remove('paper_engine/plans/plan_sp500_2026-01-20.json')"
# Note the deletion in project_notes or lessons
```

---

## 5. Edge Cases & Error Recovery

### Stale data / ingest failure

- **Symptom**: `data/cache/` parquet files older than current trading date
- **Detection**: Agent checks modification times during premarket (step 2)
- **Fix**: `atlas_jobs_run(job="cli_ingest", args={"m": "sp500"})` manually
- **Fallback**: If yfinance is down, wait 30 min and retry; check `logs/pi-cron.log` for HTTP errors

### Broker offline during plan generation

- Plan generates successfully using cached positions + last known data
- `cli_plan` does NOT require broker connectivity — it is a local computation
- Execution (`cli_paper_run`) WILL fail if broker is offline
- Fix: wait for broker to come back, re-run execution only (plan is already approved)

### Duplicate plan — already exists for today

- **Symptom**: `cli_plan` returns "plan already exists for YYYY-MM-DD"
- **Cause**: Premarket ran twice (manual + cron overlap)
- **Fix**: If the existing plan looks correct, skip re-generation and proceed to approval
- **If regeneration needed**: delete `paper_engine/plans/plan_sp500_YYYY-MM-DD.json` then re-run plan

### Volatility gate BLOCKED — not an error

- `pi-cron.sh` exits 0 (clean) when gate blocks entries
- No plan is generated; no Telegram approval request is sent
- Check gate output: `cat logs/volatility_gate_TIMESTAMP.json`
- Expect this during high-VIX periods — correct behavior, not an incident

### Config validation warnings at premarket

- Script proceeds even with warnings (warnings are not blocking errors)
- Warnings logged to `logs/pi-cron.log` and injected into agent context
- Investigate warnings same day:

  ```bash
  python3 -c "from config.schema import validate_config_file; print(validate_config_file('config/active/sp500.json'))"
  ```

- Common causes: extra unknown fields, deprecated parameter names

### Plan generation succeeds but no entries

- Can happen when all signals are filtered (low confidence, sector exposure cap, etc.)
- Check plan: `python3 -m json.tool paper_engine/plans/plan_sp500_YYYY-MM-DD.json | head -40`
- Look for `"entries": []` — this is valid, not an error
- Dashboard still updates; postclose will still run settlement

---

## 6. Multi-Market Support

Atlas currently runs:
- **SP500** (`-m sp500`): Live mode via Alpaca, full cron automation
- **ASX** (`config/active/asx.json`): Passive mode (`"mode": "passive"`), no active cron

Market argument is passed throughout: `cli_ingest -m sp500`, `cli_plan -m sp500`, etc.

If ASX becomes active:
- Add separate cron entries for ASX-timed premarket/postclose (AEST 10:00 open, 16:00 close)
- ASX broker: requires separate credentials (not Alpaca)
- `config/active/asx.json` must have `"mode": "live"` and `"approval_required": true`

---

## 7. Intraday Monitoring

`scripts/intraday_monitor.py -m sp500` runs every 30 min during the US session
(cron: `30 1,2,3,4,5,6,7 * * 2-6` = 01:30-07:30 AEST Tue-Sat).

### What it checks

- **Stop breached**: intraday low has hit or passed the stop price → alert sent
- **Stop proximity**: price within 3% of stop → warning alert sent
- **Take-profit hit**: intraday high has reached TP target → alert sent
- **Portfolio drawdown**: equity drawdown exceeds 3% threshold → alert sent

### Key characteristics

- **Informational only** — does NOT auto-execute orders or trigger exits
- Alert deduplication prevents repeat alerts within the same session
- Log: `logs/intraday_sp500.log`
- Alert state persisted in `logs/intraday/` (per-symbol, per-session)

### Checking intraday logs

```bash
tail -100 logs/intraday_sp500.log
# Look for: STOP BREACHED, TAKE-PROFIT HIT, PORTFOLIO DD
```

If a stop breach alert fires but the position was not auto-exited (informational only), the
protective orders script is responsible for having placed a SL order on the broker. If not,
check `logs/sync_protective.log` for placement failures and place the order manually.

---

## 8. Protective Order Sync

`scripts/sync_protective_orders.py --market sp500` runs 3x daily to ensure broker-side
stop-loss and take-profit orders match the portfolio state.

### Schedule

| AEST | Trigger | Purpose |
|---|---|---|
| 19:15 Mon-Fri | 15 min after premarket | Place SL/TP after new entries |
| 23:45 Mon-Fri | Mid-US-session | Catch-up for positions entered mid-day |
| 06:30 Tue-Sat | Post-market | Final confirm before US close |

### What it does

1. Connects to Alpaca broker
2. Loads live positions from broker
3. Loads today's plan for `stop_price` and `take_profit` values
4. Checks existing open orders for each position
5. Places **only missing** SL/TP orders (idempotent — existing matching orders are left alone)
6. Sends Telegram summary of what was placed / skipped / errored

### Failure patterns

- **Broker connection refused**: orders not placed; run manually after broker recovers
- **Position in broker but not in plan**: orphaned position — investigate reconciliation
- **Order placement error**: check `logs/sync_protective.log` for HTTP 422 / rate limit errors

```bash
# Manual sync run (safe to run at any time — idempotent)
python3 scripts/sync_protective_orders.py --market sp500 --verbose

# Dry run (shows what WOULD be placed without touching broker)
python3 scripts/sync_protective_orders.py --market sp500 --dry-run

# Check last sync result
tail -50 logs/sync_protective.log
```

---

## Preferred tool flow

1. Call `atlas_jobs_list_catalog` once if job names are unclear.
2. **Check data freshness**: inspect modification times of files in `data/cache/`. If the most
   recent parquet file is older than the current trading date, run `atlas_jobs_run` with
   `job=cli_ingest` to refresh. The premarket cron handles this automatically — manual only when
   cron failed.
3. Run `atlas_jobs_run` with `job=cli_plan` (pass `args.m="sp500"` for SP500). Check volatility
   gate status before proceeding — if gate action is `block`, skip plan generation.
4. Summarize `paper_engine/plans/plan_sp500_YYYY-MM-DD.json` risk and entries before any approval.
   Look for: entry count, position sizes (reduced 50% if volatility gate was `reduce`), stop levels,
   and expected exposure %.
5. Run `atlas_risk_check_plan_gate(action="approve", ...)` before any plan approval. This validates
   plan status, broker state, and config settings.
6. Require explicit user approval, then use `atlas_risk_approve_plan(confirmed=true, ...)` instead
   of calling `cli_approve` directly.
7. Run `atlas_risk_check_plan_gate(action="execute", ...)` before `cli_paper_run`. Re-checks that
   broker is live and plan is in APPROVED state.
8. Require explicit user approval before `cli_paper_run`.
9. Run `atlas_jobs_run` with `job=cli_eod_settlement` after market close to process stop-loss/
   take-profit exits, update equity snapshots, and refresh dashboard data. Only run after US market
   has closed (after 06:00 AEST Tue-Sat). The postclose cron handles this automatically.
10. Run `atlas_jobs_run` with `job=dashboard_generate_data` after plan or execution changes.

---

## Safety rules

- **Never approve without reading the plan summary first** (step 4). Entries, sizes, and stops
  must make sense before approval.
- Do not use `daily_automation` for normal operations until auto-approval behavior is removed or gated.
- Treat `atlas_risk_approve_plan` and `cli_paper_run` as high-risk actions requiring explicit
  user confirmation.
- If `config/active/asx.json` has `"approval_required": true`, preserve that intent.
- If volatility gate action is `block`, do NOT manually override to generate a plan. The gate
  exists for capital protection.
- Never run `cli_eod_settlement` during active US trading hours — it will settle positions prematurely.
- If a plan already exists for today and cron is re-running, verify the existing plan before deleting it.

---

## Repo-specific notes

- Plan state: `paper_engine/plans/plan_sp500_YYYY-MM-DD.json`
- Portfolio state: `paper_engine/portfolio_state.json`
- Cron logs: `logs/pi-cron-premarket-*.log`, `logs/pi-cron-postclose-*.log`, `logs/pi-cron.log`
- Intraday log: `logs/intraday_sp500.log`
- Protective order log: `logs/sync_protective.log`
- Volatility gate output: `logs/volatility_gate_TIMESTAMP.json`
- Dashboard reads portfolio, plan, ledger, and backtest artifacts from their respective paths.
