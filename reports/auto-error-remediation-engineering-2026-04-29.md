# Atlas — Autonomous Error-Remediation System (Engineering Spec)

**Date:** 2026-04-29  
**Author:** Auto-Remediation Engineering Architect  
**Scope:** Full engineering spec for autonomous error detection, triage, and remediation against Atlas live trading infrastructure. $5,189 real capital at risk. Read-only investigation; no source files modified.  
**Companion:** `atlas-streamlining-audit-engineering-2026-04-29.md`

---

## Lead — Executive Summary

**The single biggest engineering insight:** approximately 70% of the infrastructure required for autonomous error remediation **already exists** in Atlas. `TelegramErrorCollector` (a fully wired `logging.Handler` in `utils/logging_config.py:54`) already captures every `ERROR+` log record from every `setup_logging()` caller. `healthz_autofix.sh` already implements the agent-dispatch loop: healthcheck JSON → classify → spawn `pi -p` with skill loading, OAuth enforcement, and Telegram notifications. `multi-team/index.ts` already provides minimatch-enforced file-domain locks — the natural hardware boundary for safe automated writes. Adding a `SQLiteErrorWriter` alongside `TelegramErrorCollector` (≈40 LOC) converts the existing broadcast stream into a queryable audit table. Adding a watchdog cron that reads that table and calls `healthz_autofix.sh`'s pattern is the entire Phase 1 foundation. **No new frameworks. No new billing surfaces. No new architecture.**

**Three highest-ROI recommendations (in order):**

1. **Wire `SQLiteErrorWriter` into `setup_logging()`** — 40 LOC, 0 callsite changes, zero-risk. Every script Atlas runs today immediately begins populating the `errors` table. Unlocks everything else.
2. **Run triage agent in DRY_RUN for one week** — measure classification accuracy against historical errors before any agent takes action. Validate that the NEVER-list is complete. This is the gate to Phase 2.
3. **Register `auto_remediation_cycle` in atlas-jobs catalog** — gains run-history tracking, lock-key (single-runner enforcement), and manifest persistence for free. No new extension needed.

**Headline phased rollout:**
- **Week 1 (Phase 1):** Capture + observe. Zero auto-fix. Wire SQLite handler, journald tailer, dashboard panel, daily digest. Triage in DRY_RUN only.
- **Weeks 2–3 (Phase 2):** ASSIST mode. Agent creates branches + Telegram diff links. Human reviews all merges. Gate: ≥80% human-merge rate.
- **Ongoing (Phase 3):** Narrow AUTO_FIX list — `tests/**`, `docs/**`, `dashboard-ui/**` typos, non-trading healthcheck thresholds, lint violations. Trading-path code is ESCALATE forever.

**Safety boundary:** The multi-team extension's `minimatch` domain check (line 1897 of `index.ts`) blocks any `write` or `edit` tool call to files outside the agent's declared `domain.write` glob list **at the OS-tool level**. This is not a prompt instruction — it is a structural enforcement. The Fix Worker's `domain.write` list IS the safety perimeter. Even a fully hallucinating LLM cannot write to `brokers/`, `risk/`, `kill_switch.py`, or `live_executor.py` if those paths are absent from the glob list.

---

## 1. Error Capture Architecture

### 1.1 Current Error Surfaces — Gaps Analysis

| Source | How captured today | Gap |
|---|---|---|
| `logs/atlas.log` | `FileHandler` via `setup_logging()` in all scripts | Not queryable by agents; no structured schema; no dedup |
| `stderr` | `StreamHandler` in `setup_logging()` | Ephemeral — lost after process exit |
| `TelegramErrorCollector` | `atexit` batch send (max 20 records); `utils/logging_config.py:54` | Send-and-forget; no persistence; no fingerprint; no history |
| `journalctl` (systemd units) | Available via `journalctl -u atlas-*` | Never written to a queryable store; no polling |
| Cron stderr → MAILTO | Configured in crontab | Currently **dropped** — no MAILTO relay configured; non-zero exits vanish |
| Healthcheck failures | `healthz.py --json` produces structured JSON | Only acted on by `healthz_autofix.sh`; not stored; no trend tracking |
| Telegram bot errors | `services/telegram_bot.py` logger | Goes to `logs/atlas-telegram-bot*.log`; never aggregated |
| Sentry | **NONE** | — |
| `scripts/auto_recover.sh` failures | Telegram only (manual message) | Crash of recovery agent itself is not recorded structurally |

**Gap summary:** Errors exist in 7 distinct sinks, none queryable, none deduplicated, none persisting beyond log rotation. The triage agent has nothing to read. Phase 1 collapses this to one sink: `data/atlas.db:errors`.

### 1.2 Proposed Unified Stream — `errors` Table

Location: `data/atlas.db` (existing WAL-mode SQLite, managed by `db/atlas_db.py`).

```sql
-- Migration: scripts/migrations/2026-04-30-add-errors-tables.py
CREATE TABLE IF NOT EXISTS errors (
  id                   INTEGER PRIMARY KEY,
  ts                   TEXT    NOT NULL,           -- ISO 8601 UTC
  source               TEXT    NOT NULL,           -- 'python_logger'|'journald'|'cron'|'healthcheck'|'telegram_alert'
  service              TEXT,                       -- 'sync_protective_orders', 'atlas-discovery', etc.
  level                TEXT    NOT NULL,           -- 'ERROR'|'CRITICAL'|'WARNING'
  logger_name          TEXT,                       -- 'atlas.live_executor', 'atlas.telegram_bot', etc.
  message              TEXT    NOT NULL,
  exc_type             TEXT,                       -- 'ConnectionError', 'KeyError', etc.
  exc_message          TEXT,
  traceback            TEXT,                       -- full traceback if available
  file_path            TEXT,                       -- originating file path (relative to atlas root)
  line_number          INTEGER,
  pid                  INTEGER,
  hostname             TEXT,
  fingerprint          TEXT    NOT NULL,           -- sha256(exc_type+normalized_message+file:line)
  occurrence_count     INTEGER DEFAULT 1,          -- bump on duplicate fingerprint within 5 min
  severity_class       TEXT,                       -- AUTO_FIX|ASSIST|ESCALATE|IGNORE (set by triage agent)
  triage_reason        TEXT,                       -- why classified that way
  fixed_by_attempt_id  INTEGER REFERENCES auto_fix_attempts(id),
  last_seen_ts         TEXT,
  first_seen_ts        TEXT
);

CREATE INDEX idx_errors_fingerprint    ON errors(fingerprint, last_seen_ts);
CREATE INDEX idx_errors_severity_pend  ON errors(severity_class, fixed_by_attempt_id)
  WHERE fixed_by_attempt_id IS NULL;
CREATE INDEX idx_errors_ts             ON errors(ts);
CREATE INDEX idx_errors_source         ON errors(source, level);
```

### 1.3 Capture Hooks — Concrete Implementation Plan

| Hook | File | LOC | Coverage |
|---|---|---|---|
| **`SQLiteErrorWriter`** | `utils/logging_config.py` (alongside `TelegramErrorCollector`, line ~185) | ~40 | All Python that calls `setup_logging()` — `eod_settlement.py`, `sync_protective_orders.py`, `telegram_bot.py`, `pi-cron.sh`-launched scripts, all services |
| **`journald_error_tailer.py`** | `scripts/journald_error_tailer.py` (new, systemd service) | ~80 | `atlas-director`, `atlas-discovery`, `atlas-research-window@*`, IB Gateway, Caddy, dashboard-ui Vite — anything not going through Python logger |
| **Cron exit-code monitor** | `scripts/pi-cron.sh` + `scripts/healthz_autofix.sh` wrapper (non-zero exit hook) | ~30 | All cron-dispatched scripts that silently fail |
| **Healthcheck bridge** | `pi-package/atlas-ops/skills/atlas-healthz/scripts/healthz.py` (add ~20 LOC to JSON output path) | ~20 | FAIL/WARN verdicts from structured healthcheck |
| **Telegram alert bridge** | `utils/telegram.py:send_message` (hook on 🚨/❌/WARNING prefix) | ~10 | All Telegram alerts originating from Atlas code |

**`SQLiteErrorWriter` integration point** (no callsite changes needed):

```
utils/logging_config.py — setup_logging() function

Current handlers attached at line ~185:
  root.addHandler(stderr_h)        # StreamHandler
  root.addHandler(main_h)          # FileHandler → logs/atlas.log
  [optional] root.addHandler(extra_h)    # extra log file
  [optional] root.addHandler(_collector) # TelegramErrorCollector (atexit)

Proposed addition (after _collector attachment):
  if not _in_pytest:
      _sqlite_writer = SQLiteErrorWriter(script_name=script_name)
      root.addHandler(_sqlite_writer)
```

`SQLiteErrorWriter.emit()` mirrors `TelegramErrorCollector.emit()` but writes synchronously to `data/atlas.db:errors` on each `ERROR+` record. Thread-safe via `threading.Lock`. Same `yfinance.*` filter at line 1 of `emit()`.

### 1.4 Deduplication Strategy

Fingerprint = `sha256(exc_type + normalize(message) + file_path + ":" + str(line_number))`

Normalization replaces:
- Numbers → `<N>` (e.g., `retry 3/5` → `retry <N>/<N>`)
- Absolute paths → `<PATH>` (e.g., `/root/atlas/data/atlas.db` → `<PATH>`)
- ISO timestamps → `<TS>`
- Ticker symbols (ALL_CAPS 2-5 chars) → `<TICKER>`

**Dedup window:** If `fingerprint` exists with `last_seen_ts > now - 5min`, bump `occurrence_count` instead of inserting. After 5 min, insert new row. This gives per-burst dedup without losing the "it happened again at 14:00" signal.

### 1.5 Native vs. Sentry Comparison

| Factor | Native SQLite + agents | Sentry SaaS |
|---|---|---|
| Marginal cost | $0 (OAuth) | $26–$80/month + bandwidth |
| Data residency | localhost, WAL-mode SQLite | US/EU cloud |
| LLM queryability | Direct SQL by triage agent | API abstraction layer |
| Dedup | Custom fingerprint (fits Atlas patterns) | Generic grouping |
| Blast radius if broken | Error stream gap only | External dependency in trading-capital path |
| Error volume | Low hundreds/day | Built for 100K+/day |
| Setup time | 40 LOC | New SDK dependency + DSN config |
| Recommendation | **NATIVE** | Revisit only if multi-host scale required |

### 1.6 Retention and Backfill

- **Retention:** 90 days for `errors` table; `auto_fix_attempts` kept indefinitely (audit trail). Daily cron prunes `errors WHERE ts < now-90d AND fixed_by_attempt_id IS NOT NULL`. Hard cap: 100K rows total (oldest pruned first on overflow).
- **Retroactive backfill:** `scripts/backfill_errors_from_logs.py` (new, one-shot) parses `logs/atlas.log*`, `logs/atlas-telegram-bot*.log`, `logs/sync_protective*.log` for last 30 days. Best-effort — same fingerprint logic applies. Ships as Phase 1 deliverable.

---

## 2. Monitoring Cadence + Dispatch

### 2.1 Timer Architecture

```
                ┌─────────────────────────────────────┐
                │         data/atlas.db:errors         │
                └──────────────┬──────────────────────┘
                               │
          ┌────────────────────┼────────────────────┐
          ▼                    ▼                    ▼
  atlas-error-monitor     atlas-error-digest    (Phase 3)
  .timer (every 5 min)   .timer (daily 09:00)  event hook on
  → query unclassified    → Telegram summary    CRITICAL inserts
  → triage batch          regardless of class   (10s response)
          │
          ▼
   Triage Agent (Haiku)
   → AUTO_FIX | ASSIST | ESCALATE | IGNORE
          │
     ┌────┴────┐
     ▼         ▼
  AUTO_FIX   ASSIST / ESCALATE
  Fix Worker  → branch only (Phase 2)
  (Opus)      → Telegram diff link
     │
     ▼
  Review Worker
  (Sonnet)
     │
  APPROVE ──→ merge + monitor
  REJECT  ──→ escalate
```

**Cadence rationale:** 5-minute interval preferred over event-driven. `TelegramErrorCollector` is already batch-at-atexit; event-driven adds complexity for <1 min latency improvement. All existing watchdogs (`atlas-silent-failure-watchdog.timer` = hourly, unified healthcheck = similar cadence) use polling and work reliably.

### 2.2 Severity Classification Table

| Class | Atlas examples | Allowed paths (write) | Allowed file ops |
|---|---|---|---|
| **AUTO_FIX** | Lint violations in `scripts/` (non-trading), test assertion typos, dashboard UI text errors, healthcheck threshold off-by-one in non-trading checks, doc/comment errors in `docs/**`, stale fixture data in `tests/`, skill `.md` file formatting errors | `tests/**`, `dashboard-ui/src/**`, `docs/**`, `tasks/**`, `scripts/healthz_*.sh`, `pi-package/**/skills/**/*.md` | Edit existing files only; no new files |
| **ASSIST** | New exception types in `services/`, dashboard server bugs, telegram bot crashes, `monitor/` logic errors, `research/` internals, healthcheck logic in non-trading paths | `services/**`, `monitor/**`, `research/**`, `db/**` (read-only schema inspection) | Propose diff in branch; no auto-merge |
| **ESCALATE** | Anything in `brokers/**`, `risk/**`, `kill_switch.py`, `live_executor.py`, `live_portfolio.py`, `regime/**`, `signals/**`, `plan*.py`, `approve*.py`, `overlay/engine.py`, `scripts/sync_protective_orders.py`, `scripts/eod_settlement.py`, `scripts/reconcile_*.py`, `scripts/execute_approved.py`, schema migrations, `config/active/**`, `data/atlas.db` direct manipulation | **NONE** — Telegram alert only | NONE |
| **IGNORE** | `yfinance.*` ERROR for delisted tickers, Caddy 502 transients, `urllib3` retry noise, `ConnectionResetError` to broker that recovered within 60s | n/a | n/a — close with reason logged |

### 2.3 Rate Limits and Cooldowns

| Limit type | Value | Action on breach |
|---|---|---|
| Per fingerprint auto-fix attempts | Max 3 / 24h | Escalate + freeze fingerprint 7 days |
| Per file auto-fix commits | Max 5 / 24h | Freeze that file 7 days |
| Global auto-fix commits | Max 20 / day | Halt agent + Telegram alert |
| Errors triaged per hour | Max 200 | Alert "error volume anomaly" |
| Post-fix observation window | 30 min | Auto-revert if same fingerprint recurs |
| Consecutive same-class auto-fix | Max 10 `KeyError` / day | Escalate — systemic pattern |

---

## 3. Auto-Fix Agent Design

### 3.1 Model Selection

| Step | Model | Why | Cost |
|---|---|---|---|
| Pre-filter (Phase 3) | Haiku 4.5 | Classify "definitely IGNORE" before triage; cheap, fast | $0 (OAuth) |
| Triage | Haiku 4.5 | ~80% of errors are noise/dedup; overkill to use Sonnet | $0 (OAuth) |
| Diagnose + fix | Opus 4.7 (`claude-opus-4-7`) | Complex root-cause analysis; $5,189 capital demands top-tier | $0 (OAuth) |
| Review | Sonnet 4.6 | Independent review with full context; cheaper than Opus, smarter than Haiku | $0 (OAuth) |

**Cost model:** ALL invocations via `pi -p --system-prompt "You are Claude Code, Anthropic's official CLI for Claude." --model <X>`. Pattern already validated in `healthz_autofix.sh:13` and `pi-cron.sh:29` — both `unset ANTHROPIC_API_KEY CLAUDE_API_KEY` as first statement. Copy this pattern exactly. Monitor rolling 5h subscription window via `scripts/claude_auth_check.py` before each agent invocation.

### 3.2 Multi-Team Remediation Team Config

The multi-team extension at `/root/.pi/extensions/multi-team/index.ts` enforces `domain.write` via `minimatch` at line 1897. The config lives at `/root/.pi/teams/config.yaml`.

```yaml
# Add to /root/.pi/teams/config.yaml under teams:
teams:
  remediation:
    description: "Autonomous error remediation for Atlas live trading system"
    color: '#ff6b35'
    dim_color: '#8b3a1a'
    bg_color: '#2a1005'
    icon: "⚕"
    badge_letter: R
    lead:
      name: "Remediation Lead"
      model: claude-opus-4-7
      effort: xhigh
      system_prompt: .pi/teams/prompts/remediation-lead.md
      expertise: .pi/expertise/remediation-lead/
      skills:
        - .pi/teams/skills/atlas-incident.md
        - .pi/teams/skills/claude-oauth.md
        - .pi/teams/skills/trading-domain.md
      domain:
        read: ["**/*"]
        write: [".pi/expertise/remediation-lead/**"]
    members:
      - name: "Triage Worker"
        model: claude-haiku-4-5
        system_prompt: .pi/teams/prompts/triage-worker.md
        expertise: .pi/expertise/triage-worker/
        skills:
          - atlas-incident
          - atlas-state-queries
          - atlas-lessons
        domain:
          read: ["**/*"]
          write: [".pi/expertise/triage-worker/**"]

      - name: "Fix Worker"
        model: claude-opus-4-7
        system_prompt: .pi/teams/prompts/fix-worker.md
        expertise: .pi/expertise/fix-worker/
        skills:
          - atlas-codebase
          - atlas-incident
          - atlas-lessons
          - testing-handbook
        domain:
          read: ["**/*"]
          write:
            - "tests/**"
            - "dashboard-ui/src/**"
            - "docs/**"
            - "tasks/**"
            - "scripts/healthz_*.sh"
            - "pi-package/**/skills/**/*.md"
            - ".pi/expertise/fix-worker/**"
            # CRITICAL: brokers/, risk/, kill_switch.py, live_executor.py,
            # live_portfolio.py, regime/, signals/, plan*.py, approve*.py,
            # overlay/engine.py, scripts/sync_protective_orders.py,
            # scripts/eod_settlement.py, scripts/reconcile_*.py,
            # scripts/execute_approved.py, config/active/**, data/atlas.db
            # are STRUCTURALLY ABSENT from this list.
            # minimatch enforcement in index.ts:1897 makes this a hard block.

      - name: "Review Worker"
        model: claude-sonnet-4-6
        system_prompt: .pi/teams/prompts/review-worker.md
        expertise: .pi/expertise/review-worker/
        skills:
          - atlas-codebase
          - atlas-lessons
          - security-audit-guide
        domain:
          read: ["**/*"]
          write: [".pi/expertise/review-worker/**"]
```

**⚠️ Critical caveat on domain enforcement:** The `minimatch` domain lock only applies when the agent is invoked **through the multi-team extension** (`pi --team remediation ...`). A direct `pi -p` invocation without `--team` bypasses all domain enforcement. This is a **Phase 1 hardening requirement**: the `scripts/auto_remediate.py` entry point MUST invoke the multi-team dispatch path, not a raw `pi -p`. Document this in the script header. Failure to do this makes the safety boundary advisory (prompt-only) rather than structural.

### 3.3 The 8-Step Fix Workflow

```
Error in errors table (severity_class = NULL)
          │
          ▼
Step 1: TRIAGE (Haiku, batch up to 20 errors)
  Loads: atlas-incident, atlas-state-queries, atlas-lessons
  Output: severity_class + triage_reason per error
  Writes: auto_fix_attempts row (status='triaged')
          │
    ┌─────┴──────┐
    │            │
  IGNORE      AUTO_FIX / ASSIST / ESCALATE
  (close)         │
                  ▼
Step 2: REPRODUCE (Opus, isolated git worktree at /tmp/atlas-fix-<id>/)
  Run failing code path locally.
  If cannot reproduce → status='escalated' (race/env issue, not auto-fixable)
                  │
                  ▼
Step 3: DIAGNOSE (Opus)
  Full context: recent commits to file, related files, test history,
  error fingerprint history (how many times, when, what changed).
  Output: { root_cause, affected_files[], test_strategy }
                  │
                  ▼
Step 4: FIX (Opus, in worktree)
  Write code change. REQUIRED: new test covering the actual error path.
  Diff size cap: 50 LOC (configurable in auto_remediation.json).
  Branch: auto-fix/<error_id>-<short_fingerprint>
                  │
                  ▼
Step 5: VERIFY (in worktree)
  pytest --timeout=30 (targeted) + full module suite
  ruff check (lint)
  scripts/lint_bare_except.py --check (no new bare-excepts)
  SQLite PRAGMA integrity_check (if DB touched)
  ALL must pass. Any failure → status='failed', escalate.
                  │
                  ▼
Step 6: REVIEW (Sonnet, separate session)
  Receives: diff + diagnosis + test results
  Validates: (a) tests cover error path? (b) no NEVER-list paths?
             (c) no new bare-excepts? (d) no secrets? (e) regression risk?
  Output: verdict (APPROVE/REJECT) + confidence (0.0–1.0) + reason
  Threshold: confidence ≥ 0.75 required for APPROVE
                  │
          ┌───────┴────────┐
          │                │
        APPROVE          REJECT
        + AUTO_FIX        → status='escalated'
          │                 Telegram diff link
          ▼
Step 7: SHIP
  Merge branch to main, push.
  Update auto_fix_attempts: status='merged', fix_commit_sha, fix_diff_lines
          │
          ▼
Step 8: MONITOR (30-min observation window)
  Poll healthz.py every 5 min.
  Watch error fingerprint — any recurrence → auto-revert.
  Watch new errors in same file (>1.5x baseline rate → revert).
  Outcome: status='succeeded' | 'reverted'
```

**Worktree pattern:** Each fix runs in `/tmp/atlas-fix-<error_id>/` via `git worktree add`. Cleanup on exit (success or failure). Mirrors the swarm extension pattern already used in multi-team builds. Atlas-jobs lock key `auto_remediation` (single-runner enforcement) prevents two fix attempts running concurrently.

---

## 4. Safety Bounds

> This is the most important section. $5,189 of real capital. Every line below represents a deliberate decision about what an automated agent may never touch.

### 4.1 NEVER Auto-Fix List — Structural Deny Table

| Path pattern | Files | Reasoning |
|---|---|---|
| `brokers/**` | `brokers/kill_switch.py`, `brokers/live_executor.py`, `brokers/live_portfolio.py`, `brokers/alpaca/broker.py`, `brokers/plan.py`, `brokers/secrets.py`, `brokers/pdt_state.py`, `brokers/registry.py` | **Direct capital path.** `live_executor.py` (2,790 LOC) is the sole order-placement authority. A single wrong line can open uncovered positions or fail to exit. |
| `risk/**` | `risk/cross_universe_guard.py` | Risk gate — incorrect change could allow over-leveraged positions. |
| `overlay/engine.py` | Single file, 1,075 LOC | LLM-mediated decision engine. Auto-patching an LLM orchestrator with another LLM is recursive fragility. |
| `regime/**` | `regime/model.py`, `regime/states.py`, `regime/distributions.py`, etc. | Regime state is the primary market-condition gate for all position sizing. |
| `signals/**` | `signals/etf_flows.py`, `signals/vix_term_structure.py`, etc. | Signal generation changes would affect position entry/exit criteria. |
| `scripts/sync_protective_orders.py` | 1,453 LOC | Manages all stop-loss and take-profit orders for live positions. A bug here = uncovered capital. |
| `scripts/eod_settlement.py` | Key settlement script | PnL calculation + position close logic. Correctness is financial. |
| `scripts/reconcile_*.py` | `reconcile_ledger.py`, `reconcile_positions.py`, `reconcile_shadow.py`, `reconcile_sqlite_to_broker.py` | Position reconciliation — a wrong reconcile commit can corrupt the canonical state. |
| `scripts/execute_approved.py` | Order execution entry point | Directly submits orders via broker. |
| `scripts/auto_reoptimize.py` | Parameter sweep + promotion | Could alter live strategy parameters. |
| `scripts/pi-cron.sh` | Master cron wrapper (ALL scheduled ops) | Changing schedule or skip logic could miss critical daily operations. |
| `db/atlas_db.py` | 2,892 LOC typed access layer | Schema migrations, CRUD for trades/plans/equity — all downstream code depends on this. |
| `db/schema.sql` | Canonical schema | Any `ALTER TABLE` or `CREATE TABLE` change needs migration + testing. |
| `scripts/migrations/**` | `2026-04-*.py` (20 migration files) | Schema migration scripts — wrong migration = irreversible DB corruption. |
| `config/active/**` | `sp500.json`, `sector_etfs.json`, `regime.json`, etc. (9 files) | Live strategy parameters. `starting_equity`, `leverage`, `max_open_positions` — all directly affect capital deployment. |
| `config/global_risk.json` | Portfolio-wide risk limits | Maximum drawdown, leverage cap for the entire portfolio. |
| `data/atlas.db` | Live production database | Direct manipulation of the production DB outside of `db/atlas_db.py` is always forbidden. |
| `brokers/state/**` | `live_sp500.json`, `live_sector_etfs.json`, `live_commodity_etfs.json` | Live position state files. Corruption here causes `stop_price=0` class bugs. |
| `data/stops_held_state.json` | Stop-retry state | Held stop-loss retry counter per ticker. |
| `data/pdt_deferred_state.json` | PDT deferral state | Pattern Day Trading deferral state. |
| `data/HALT` | Kill switch file | Presence halts all order placement. Automated creation/deletion is separately gated. |
| `.atlas-secrets.json` (at `/root/`) | Alpaca API keys, Telegram credentials | OAuth and broker credentials. |
| `.env*` | Any env files | Secrets management. |
| `~/.pi/agent/auth.json` | Pi OAuth token | Changing this = breaking all LLM calls. |
| Any `--system-prompt` or `--model` CLI argument site | All `pi -p` invocations | Could route LLM calls to pay-per-token billing (see AGENTS.md warning). |

### 4.2 Function-Name Deny List

Any fix that adds, modifies, or removes a call to the following functions must be blocked at the Review Worker stage (additional programmatic check via `grep -n` on diff):

| Function | Module | Why blocked |
|---|---|---|
| `place_order` | `brokers/live_executor.py:474` | Direct broker order placement |
| `_execute_entry` | `brokers/live_executor.py:917` | Entry order logic |
| `_execute_exit` | `brokers/live_executor.py:1384` | Exit order logic |
| `record_trade_entry` | `db/atlas_db.py` | SQLite trade record creation |
| `record_trade_exit` | `db/atlas_db.py` | SQLite trade record update |
| `transition_trade` | `db/atlas_db.py` | Trade status state machine |
| `kill_switch.halt` | `brokers/kill_switch.py:31` | Creates HALT file |
| `kill_switch.resume` | `brokers/kill_switch.py:37` | Clears HALT file |
| `_check_circuit_breaker` | `brokers/live_executor.py:320` | Daily loss circuit breaker |

### 4.3 Error-Class ESCALATE-Only List

Any error whose `exc_type` or message contains these strings is automatically classified `ESCALATE`, regardless of the file that raised it:

`BrokerError`, `OrderRejected`, `KillSwitchTriggered`, `RiskLimitBreached`, `LeverageCapBreached`, `PDTViolation`, `DrawdownLimitBreached`, `CircuitBreaker`, `HaltError`, `CapitalAtRisk`, `stop_price=0`, `uncovered position`, `take_profit=NULL`.

### 4.4 Auto-Merge Gates (ALL must pass for AUTO_FIX class)

| Gate | How checked | Failure action |
|---|---|---|
| All targeted tests pass (incl. new test covering error path) | `pytest <affected_module> --timeout=30` | Abort + escalate |
| Full module suite passes | `pytest tests/test_<module>*.py --timeout=30` | Abort + escalate |
| Lint clean | `ruff check <changed_files>` | Abort + escalate |
| No new bare-excepts | `python3 scripts/lint_bare_except.py --check` | Abort + escalate |
| SQLite integrity | `PRAGMA integrity_check` if DB touched | Abort + escalate |
| Diff size ≤ 50 LOC | `git diff --stat` | Abort + escalate |
| No NEVER-list file touched | `git diff --name-only` ∩ deny list | Abort + escalate (domain lock should have prevented, but belt-and-suspenders) |
| Review Worker confidence ≥ 0.75 | JSON output from Review Worker | REJECT → ASSIST mode branch only |
| No healthcheck regression (30 min post-merge) | `healthz.py --json` every 5 min | Auto-revert + Telegram alert |
| No same-fingerprint recurrence (30 min post-merge) | Query `errors` table | Auto-revert + fingerprint freeze |

### 4.5 Loop Prevention

| Condition | Threshold | Response |
|---|---|---|
| Same fingerprint fixed N times | 3 / 24h | Escalate; freeze fingerprint 7 days (no further auto-attempts) |
| Same file modified by auto-fix | 5 times / 24h | Freeze that file 7 days |
| Same error class (e.g., `KeyError`) auto-fixed | 10 times / 24h | Escalate — systemic pattern, needs human architectural review |
| Fix → revert cycle | 2 consecutive reverts for same fingerprint | Freeze fingerprint 30 days; Telegram alert |

### 4.6 Budget Caps

| Resource | Cap | Enforcement |
|---|---|---|
| LLM triage calls | 100 / hour | Counter in `auto_fix_attempts` + abort |
| LLM diagnosis+fix calls | 30 / hour | Counter + abort |
| LLM review calls | 20 / hour | Counter + abort |
| Auto-fix commits | 20 / day | Counter in `auto_fix_attempts` + halt |
| Fix attempt wall clock | 10 min per attempt | `timeout 600` wrapper in service |
| Subscription window | 5h rolling (Claude Max) | `scripts/claude_auth_check.py` preflight before every invocation |

### 4.7 Kill Switches — Multi-Layer Defense

```
Layer 1: Feature flag
  config/active/auto_remediation.json → "enabled": true
  (file does not exist yet — must be created in Phase 1)

Layer 2: Halt file
  data/AUTO_REMEDIATION_HALT (presence = pause)
  Created by: any kill switch below; deleted by human only

Layer 3: Telegram commands
  /halt_remediation  → creates data/AUTO_REMEDIATION_HALT + Telegram confirm
  /resume_remediation → human-only file deletion + Telegram confirm
  (add to services/telegram_bot.py alongside existing /halt and /unhalt at line 594)

Layer 4: Healthcheck regression auto-pause
  If healthz.py fails 3 times in 1 hour after a fix → auto-pause + alert

Layer 5: Budget breach auto-pause
  Any rate limit exceeded → create AUTO_REMEDIATION_HALT + Telegram
```

### 4.8 Failure Escalation Chain

```
Agent crash (non-zero pi exit)
  → Telegram: "⚠️ Auto-remediation agent crashed (exit N). Paused 1h."
  → data/AUTO_REMEDIATION_HALT created with 1h expiry

Fix merged, then healthcheck regresses post-merge
  → git revert <sha>, push
  → Telegram: "🔄 Auto-revert: fix for <fingerprint> caused healthcheck regression"
  → Fingerprint frozen 7 days

Same fingerprint recurs within 30-min observation window
  → git revert if merged; otherwise close attempt
  → Telegram alert + fingerprint freeze

Budget breach (>20 commits / day)
  → AUTO_REMEDIATION_HALT created + Telegram
  → Daily digest flags it

Domain violation attempt (Fix Worker tried to write a NEVER-list file)
  → minimatch blocks the tool call (returns ⛔ reason string — index.ts:1897)
  → Log to auto_fix_attempts.notes: "domain_violation_attempted: <path>"
  → Halt agent for 1h + Telegram forensic alert (this is a serious diagnostic signal)

Multi-team config corrupted / parse failure
  → Fail-closed: no agent dispatch
  → Telegram alert + human must fix config
```

---

## 5. Verification + Rollback

### 5.1 Branch Naming Convention

`auto-fix/<error_id>-<short_fingerprint_8chars>` (e.g., `auto-fix/1042-3f8a91b2`)

### 5.2 Pre-Merge Verification Sequence

```
In /tmp/atlas-fix-<error_id>/ git worktree:

1. pytest <affected_module_tests>/*.py -x --timeout=30
2. pytest tests/ -x --timeout=30 -q  (full suite, quick-fail)
3. ruff check <changed_files>
4. python3 scripts/lint_bare_except.py --check
5. [if any DB migration] python3 -c "import sqlite3; c=sqlite3.connect('data/atlas.db'); c.execute('PRAGMA integrity_check')"
6. git diff --name-only | grep -Ef deny_list.txt → must return empty
7. git diff --stat | grep insertions | awk → LOC ≤ 50
8. Review Worker (Sonnet) → verdict=APPROVE, confidence ≥ 0.75
```

### 5.3 Post-Merge 30-Minute Observation Window

| Check | Interval | Failure trigger |
|---|---|---|
| `healthz.py --json` | Every 5 min for 30 min | Any FAIL/WARN that wasn't present before fix |
| Same fingerprint in `errors` table | Every 5 min for 30 min | Any new row with same fingerprint |
| New errors in same file | Every 5 min for 30 min | Error rate in same file >1.5x pre-fix 24h baseline |

### 5.4 Auto-Revert Procedure

```bash
# auto_remediate.py revert path:
git revert --no-edit <fix_commit_sha>
git push origin main
# Update auto_fix_attempts: status='reverted', revert_commit_sha=<sha>, revert_reason=<reason>
# Send Telegram: "🔄 Reverted auto-fix for <fingerprint>: <reason>"
# Freeze fingerprint for 7 days
# Create AUTO_REMEDIATION_HALT (1h expiry — cool-down)
```

### 5.5 `auto_fix_attempts` Audit Table

```sql
CREATE TABLE IF NOT EXISTS auto_fix_attempts (
  id                   INTEGER PRIMARY KEY,
  error_id             INTEGER REFERENCES errors(id),
  fingerprint          TEXT    NOT NULL,
  started_ts           TEXT    NOT NULL,
  finished_ts          TEXT,
  status               TEXT    NOT NULL,
    -- 'triaged'|'reproducing'|'diagnosing'|'fixing'|'verifying'
    -- |'reviewing'|'merged'|'reverted'|'failed'|'escalated'
  severity_class       TEXT    NOT NULL,
  triage_model         TEXT,
  triage_reason        TEXT,
  triage_tokens        INTEGER,
  diagnosis_model      TEXT,
  diagnosis_summary    TEXT,
  diagnosis_tokens     INTEGER,
  fix_model            TEXT,
  fix_branch           TEXT,
  fix_commit_sha       TEXT,
  fix_diff_lines       INTEGER,
  fix_tokens           INTEGER,
  review_model         TEXT,
  review_verdict       TEXT,
  review_confidence    REAL,
  review_reason        TEXT,
  review_tokens        INTEGER,
  test_results_json    TEXT,
  revert_commit_sha    TEXT,
  revert_reason        TEXT,
  total_wall_seconds   REAL,
  notes                TEXT     -- forensic notes, domain violation attempts, etc.
);

CREATE INDEX idx_attempts_status      ON auto_fix_attempts(status, started_ts);
CREATE INDEX idx_attempts_fingerprint ON auto_fix_attempts(fingerprint, started_ts);
CREATE INDEX idx_attempts_error_id    ON auto_fix_attempts(error_id);
```

---

## 6. Cost + Observability

### 6.1 Budget Tracker

Daily totals persisted to `auto_fix_attempts` aggregated view:

| Metric | Query |
|---|---|
| Triage calls | `SELECT COUNT(*) FROM auto_fix_attempts WHERE triage_model IS NOT NULL AND DATE(started_ts)=DATE('now')` |
| Diagnosis calls | `SELECT COUNT(*) FROM auto_fix_attempts WHERE diagnosis_model IS NOT NULL AND DATE(started_ts)=DATE('now')` |
| Fix calls | `SELECT COUNT(*) FROM auto_fix_attempts WHERE fix_model IS NOT NULL AND DATE(started_ts)=DATE('now')` |
| Review calls | `SELECT COUNT(*) FROM auto_fix_attempts WHERE review_model IS NOT NULL AND DATE(started_ts)=DATE('now')` |
| Success rate | `merged / (merged + reverted + failed)` where `DATE(started_ts)=DATE('now')` |
| Revert rate | `reverted / merged` — SLO target: <5% |
| MTTD (mean time to detect) | `MIN(ts) - error_first_seen_ts` per resolved error |
| MTTF (mean time to fix) | `merged_ts - error_first_seen_ts` per resolved error |

**Cost:** $0.00 per cycle (all OAuth, no API keys). Monitor OAuth window via `scripts/claude_auth_check.py` before each batch.

### 6.2 Daily Telegram Digest (09:00 AEST)

```
🤖 Atlas Auto-Remediation — Daily Digest (2026-MM-DD)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
📊 Last 24h:
  • Errors captured: N
  • Triaged: N (AUTO_FIX: N | ASSIST: N | ESCALATE: N | IGNORE: N)
  • Auto-fixed & merged: N (success rate: N%)
  • Reverted: N [fingerprint1, fingerprint2]
  • Escalated to human: N [fingerprint3, fingerprint4]
  • Pending ASSIST review (branches open): N

⏱  MTTD: Nmin | MTTF: Nmin | Revert rate: N%
💰 Cost: $0.00 (OAuth)

🔝 Top error fingerprints (last 24h):
  1. [N×] KeyError in tests/test_allocation.py:42
  2. [N×] ConnectionTimeout in services/chat_server.py:891
  ...

⚙️  Budget: N/20 commits used | N/100 triage calls | Window: OK
```

Sent by `scripts/auto_remediate_digest.py` via `systemd/atlas-remediation-digest.timer`.

### 6.3 Dashboard Panel

New FastAPI route in `services/api/remediation.py`:

| Endpoint | Data |
|---|---|
| `GET /api/remediation/errors` | Error volume time-series, top fingerprints, severity breakdown |
| `GET /api/remediation/attempts` | Fix attempt history, current status, revert log |
| `GET /api/remediation/digest` | Same data as Telegram digest in JSON for dashboard |
| `WS /api/remediation/live` | (Phase 3) Real-time error stream |

Frontend: `dashboard-ui/src/components/RemediationPanel.tsx` — extends existing panel pattern in `dashboard-ui/src/components/` (currently contains `finance/`, `layout/`, `portfolio/`, `research/`, `shared/` subdirs).

### 6.4 Weekly Report (Sunday 18:00 AEST)

- Bug-class trend analysis (which fingerprint clusters are growing?)
- Auto-fix vs. escalation ratio per module
- Files with highest fix churn (candidates for architectural review)
- ASSIST branches still open after >7 days (stale review backlog)
- Revert-rate trend (SLO gate: halt AUTO_FIX if >5% trailing 7-day revert rate)

---

## 7. Phased Rollout

### Phase 1 — Capture & Observe (Week 1, ZERO auto-fix)

**Goal:** Build the unified error stream. Validate triage accuracy in dry-run. No agent takes any action.

| Deliverable | File(s) | Effort | Risk |
|---|---|---|---|
| `errors` + `auto_fix_attempts` schema | `scripts/migrations/2026-04-30-add-errors-tables.py` | 0.5d | Zero — additive migration |
| `SQLiteErrorWriter` handler | `utils/logging_config.py` (+~40 LOC after line 185) | 0.5d | Zero — additive handler, same pattern as `TelegramErrorCollector` at line 54 |
| Journald error tailer | `scripts/journald_error_tailer.py` + `systemd/atlas-error-tailer.service` | 1d | Low — read-only from journald |
| Cron exit-code capture | `scripts/pi-cron.sh` (non-zero exit hook, ~30 LOC) | 0.5d | Low — only fires on failure |
| Healthcheck bridge | `scripts/healthz_to_errors.py` (30-line wrapper) | 0.25d | Low |
| Backfill script | `scripts/backfill_errors_from_logs.py` | 0.5d | Zero — read-only log parse |
| Dashboard panel (read-only) | `services/api/remediation.py` + `dashboard-ui/src/components/RemediationPanel.tsx` | 1d | Low |
| Daily digest (counts only) | `scripts/auto_remediate_digest.py` + `systemd/atlas-remediation-digest.timer` | 0.5d | Low |
| Triage agent in DRY_RUN | `scripts/auto_remediate.py --dry-run` | 0.5d | Zero — no writes |
| Unit tests | `tests/test_error_capture.py`, `tests/test_remediation_api.py` | 0.5d | — |

**Total Phase 1: ~5.5 days.** Ships entirely without auto-fix logic. After 1 week of dry-run triage, compare LLM classifications against human classifications of sampled errors. Target: ≥85% agreement rate before Phase 2.

**Phase 1 gate to Phase 2:** Triage accuracy ≥85% on sampled errors AND zero false-positive ESCALATE→AUTO_FIX misclassifications on trading-path errors.

---

### Phase 2 — ASSIST Mode (Weeks 2–3, branches only, no auto-merge)

**Goal:** Build confidence in fix quality. Every fix is a branch + Telegram diff link. Human reviews and merges all changes.

| Deliverable | Details | Effort |
|---|---|---|
| Fix Worker (ASSIST mode) | Creates branch, runs verify, sends Telegram diff link. NO merge. | 1.5d |
| Review Worker | Review step still runs; verdict advisory only (human decides merge) | 0.5d |
| Branch management | Auto-close ASSIST branches after 14 days if not merged | 0.5d |
| Metric tracking | Human-merge rate, false-positive rate, time-to-review | 0.5d |
| `auto_fix_attempts` UI in dashboard | Show pending branches, review status | 0.5d |

**Phase 2 gate to Phase 3:** ≥80% human-merge rate over 2 weeks AND zero false-positive merges that touched trading paths AND revert rate of merged fixes = 0%.

---

### Phase 3 — Narrow AUTO_FIX (Ongoing, expand list as confidence grows)

**Initial AUTO_FIX-eligible paths (day 1 of Phase 3):**

| Path | Error class examples | Rationale |
|---|---|---|
| `tests/**` | Test assertion typos, import path errors, fixture data staleness | Zero capital impact; tests are sandboxed |
| `docs/**` | Doc errors, broken cross-references | Pure documentation |
| `dashboard-ui/src/**` | UI text typos, CSS class errors, broken JSX | Frontend only; no trading logic |
| `tasks/**` | TODO.md format errors | Ops documentation |
| `scripts/healthz_*.sh` | Healthcheck threshold off-by-one in non-trading checks only | Constrained to healthcheck scripts |
| `pi-package/**/skills/**/*.md` | Skill file formatting errors | Documentation |

**Expansion policy:** Add new path to AUTO_FIX-eligible only after:
1. 30 days of clean ASSIST track record on that path (zero false-positives)
2. ≥10 successful ASSIST merges reviewed and approved by human in that path
3. Engineering Lead sign-off

**Contraction policy:** If revert rate exceeds 5% trailing 7 days → freeze entire AUTO_FIX list, downgrade all to ASSIST, alert Engineering Lead. Do not re-expand without review.

**NEVER expands to (forever):** `brokers/`, `risk/`, `kill_switch.py`, `live_executor.py`, `live_portfolio.py`, `regime/`, `signals/`, `overlay/engine.py`, `scripts/sync_protective_orders.py`, `scripts/eod_settlement.py`, `scripts/reconcile_*.py`, `config/active/**`. Not even after a perfect 6-month track record. The capital risk is asymmetric.

---

### Phase 4 — Cross-System Pattern Detection (6+ months, optional)

- Aggregate fingerprints; detect "same class, 12 files" → propose architectural fix as PR (always ASSIST, never auto)
- Connect to `research/` for "this strategy keeps producing X error" → diagnostic feedback
- Weekly pattern-cluster report as supplement to daily digest

---

## 8. Integration with Existing Systems

### 8.1 Multi-Team Extension Integration

```
Invocation path (CORRECT — domain-locked):
  scripts/auto_remediate.py
    → subprocess: pi --team remediation --task "triage error_ids=[1,2,3]"
    → multi-team extension loads remediation team config
    → dispatches to Triage Worker (Haiku)
    → minimatch domain enforcement active (index.ts:1897)

Invocation path (WRONG — bypasses domain enforcement):
  subprocess: pi -p --model claude-haiku-4-5 --prompt "fix error 42"
  → no domain lock; Fix Worker could write anywhere
```

This distinction is the single most important implementation detail in the entire system. Phase 1 hardening task: add assertion in `scripts/auto_remediate.py` that verifies multi-team invocation path. Log + halt if raw `pi -p` path is attempted.

### 8.2 Skill Reuse Map

| Agent | Skills loaded | Why |
|---|---|---|
| Triage Worker | `atlas-incident`, `atlas-state-queries`, `atlas-lessons` | Error context, state queries, anti-patterns |
| Fix Worker | `atlas-codebase`, `atlas-incident`, `atlas-lessons`, `testing-handbook` | Code context, incident patterns, test standards |
| Review Worker | `atlas-codebase`, `atlas-lessons`, `security-audit-guide` | Code review, regression history, security check |
| (Phase 3+) Pattern detector | `atlas-error-patterns` (new skill, ~100 LOC) | Cross-fingerprint cluster analysis |

**No new skills required for Phase 1 + Phase 2.** All 12 existing skills in `pi-package/atlas-ops/skills/` (`atlas-backtest`, `atlas-brain`, `atlas-codebase`, `atlas-daily`, `atlas-director`, `atlas-healthz`, `atlas-incident`, `atlas-lessons`, `atlas-reoptimize`, `atlas-research-loop`, `atlas-state-queries`, `atlas-strategy-discovery`) are sufficient.

### 8.3 Atlas-Jobs Integration

Register `error_remediation_run` in `atlas-jobs/src/catalog.ts` (alongside existing `health_check` and `reoptimize_full_universe` entries):

```typescript
// Add to ATLAS_JOB_CATALOG in pi-package/atlas-ops/extensions/atlas-jobs/src/catalog.ts
{
  name: "auto_remediation_cycle",
  category: "ops",
  summary: "Run one auto-remediation triage + fix cycle (batch up to 20 unclassified errors).",
  commandPreview: "python3 scripts/auto_remediate.py --batch-size 20",
  estimatedRuntimeSec: 600,
  reads: ["data/atlas.db"],
  writes: ["data/atlas.db"],   // errors.severity_class, auto_fix_attempts rows
  lockKey: "auto_remediation", // single-runner enforcement
  timeoutMs: 600000,           // 10-minute hard cap
  approvalHint: "safe"
}
```

This gives: run history, lock enforcement (prevents concurrent cycles), manifest persistence under `.pi/atlas-runs/`, integration with the dashboard jobs panel.

### 8.4 Systemd Timer Layout

```
New systemd units (Phase 1):
  systemd/atlas-error-tailer.service      ← journald error tailer (long-running)
  systemd/atlas-error-monitor.service     ← 5-min triage cycle (oneshot)
  systemd/atlas-error-monitor.timer       ← OnCalendar=*:0/5 (every 5 min)
  systemd/atlas-remediation-digest.service← daily digest sender
  systemd/atlas-remediation-digest.timer  ← OnCalendar=*-*-* 23:00 (09:00 AEST = 23:00 UTC)

Existing timers (unchanged — complement, do not replace):
  atlas-silent-failure-watchdog.timer     ← hourly (complementary, not replaced)
  atlas-heartbeat-watchdog.timer          ← existing heartbeat monitoring
  unified-healthcheck.timer               ← existing health checks
```

---

## 9. Failure Modes + Mitigation

| # | Failure mode | Likelihood | Impact | Mitigation | Detection | Recovery |
|---|---|---|---|---|---|---|
| 1 | Agent makes fix worse (regression) | Medium | High | 30-min observation window; all-gates pre-merge; Review Worker | Healthcheck regression or new errors in same file post-merge | Auto-revert + fingerprint freeze + 1h halt |
| 2 | Agent fixes same bug 50 times (loop) | Low | Medium | Per-fingerprint cap: 3 attempts/24h; per-file cap: 5/24h | `auto_fix_attempts` row counter | Escalate + freeze fingerprint 7 days |
| 3 | Fix has hidden side effects (compiles, wrong) | Medium | High | REQUIRE tests covering actual error path (mandatory gate); Review Worker; post-merge healthcheck | Healthcheck regression; new related error class | Auto-revert; human review required |
| 4 | Agent wrong about diagnosis | Medium | Medium | Independent Review Worker in separate session; test gate; post-merge monitor | Tests fail in verify step OR healthcheck regresses | Abort at verify; escalate with diagnosis attached |
| 5 | LLM hallucinates valid-but-wrong code | Low-Med | High | Mandatory new test covering the exact error path; Review Worker scrutiny on test quality | Test covers path (reviewer checks this explicitly) | Reject at review; auto-revert post-merge |
| 6 | OAuth subscription window exhaustion | Low | Medium | `claude_auth_check.py` preflight; halt agent if fails; resume after window | `check_pi_auth()` returns failure | Pause cycle; retry in 30 min; Telegram alert |
| 7 | Error stream itself fails (DB locked, handler broken) | Low | Medium | Meta-monitor: alert if `errors` table has 0 rows in 24h on normally-noisy service | Daily digest count = 0 unexpectedly | Telegram alert; human checks `SQLiteErrorWriter` |
| 8 | Worker writes a NEVER-list file | Very Low | Critical | minimatch domain lock in `index.ts:1897` blocks at OS-tool level; CANNOT be overridden by prompt | Block event logged with file path + agent name | Halt 1h; forensic Telegram alert; human review |
| 9 | Multi-team config corrupted | Very Low | High | Fail-closed: no agent dispatch on parse error | `pi --team remediation` returns config error | Telegram alert; human fixes YAML |
| 10 | Two monitor cycles run concurrently | Low | Medium | atlas-jobs lock_key `auto_remediation`; only one cycle at a time | Lock acquisition failure logged | Second cycle exits cleanly; no double-fix |
| 11 | Triage misclassifies trading-path error as AUTO_FIX | Low | High | Fix Worker domain lock blocks write at tool level (belt+suspenders); error stays in table | Domain violation event logged in `auto_fix_attempts.notes` | Error remains unresolved; escalation via daily digest |
| 12 | Reviewer agent "colludes" (same training, same bad fix) | Low | Medium | Hard gates: diff ≤50 LOC + test requirement + post-merge healthcheck (mechanical, not LLM-based) | Post-merge healthcheck regression | Auto-revert; both agents' reasoning logged for human review |
| 13 | Git push fails (network partition) | Low | Low | Retry 3× with 5s backoff; leave commit local with warning | Push failure logged in `auto_fix_attempts.notes` | Branch exists locally; human can push; Telegram after 3 failures |
| 14 | pytest hangs (test with no timeout) | Medium | Medium | `--timeout=30` per-test via pytest-timeout; 10-min wall-clock cap on entire attempt (`timeout 600`) | Wall-clock cap exceeded | Kill subprocess; status='failed'; escalate |
| 15 | `AUTO_REMEDIATION_HALT` file not honored | Very Low | High | `scripts/auto_remediate.py` checks halt file as FIRST line of every cycle | N/A — it's a hard check | File presence = immediate exit; Telegram if halt file detected unexpectedly |
| 16 | Triage cost runs away (API key leak) | Very Low | Medium | `unset ANTHROPIC_API_KEY CLAUDE_API_KEY` before every pi invocation (copy `healthz_autofix.sh:13`); verify with `claude_auth_check.py` | `400 out of extra usage` error | Fix auth path; never add API credits |
| 17 | Fix branch left open indefinitely (ASSIST mode) | Medium | Low | Auto-close ASSIST branches older than 14 days; daily digest lists stale branches | Digest metric "branches open >7d" | Telegram prompt to human; auto-close at 14d |
| 18 | `SQLiteErrorWriter` adds latency to production paths | Low | Medium | Write via background thread (same pattern as `TelegramErrorCollector`); `busy_timeout=30000` in `get_db()`; fail-open on DB error | Latency spike in `sync_protective_orders.py` timing logs | Disable SQLite handler via feature flag; fall back to file-only |

---

## 10. Recommended Next 3 Actions (Phase 1 Deliverables)

### Action 1 — Ship the `errors` + `auto_fix_attempts` Tables + Python Capture Hook (1 day)

| File | Change | LOC |
|---|---|---|
| `scripts/migrations/2026-04-30-add-errors-tables.py` | New migration: `errors` + `auto_fix_attempts` tables + 7 indexes | ~60 |
| `utils/logging_config.py` | Add `SQLiteErrorWriter(logging.Handler)` class (~40 LOC) alongside `TelegramErrorCollector` at line 54; attach in `setup_logging()` after line 185 | ~45 |
| `tests/test_error_capture.py` | Verify `ERROR` records land in `errors` table; verify `yfinance.*` filtered; verify dedup logic; verify fingerprint generation | ~80 |

**Acceptance criteria:** After migration, `python3 -c "from utils.logging_config import setup_logging; l=setup_logging('test'); l.error('test error')"` produces a row in `data/atlas.db:errors`. Tests pass. No regressions in `pytest tests/ -x -q`.

**Why first:** Most leverage with least risk. 40 LOC on an already-wired handler pattern. Everything else depends on errors being in the table.

---

### Action 2 — Build the Journald Error Tailer + Cron-Exit Wrapper (1 day)

| File | Change | LOC |
|---|---|---|
| `scripts/journald_error_tailer.py` | Follow `journalctl -f` for `atlas-*` units; parse stderr lines; fingerprint + insert into `errors`; same dedup window | ~80 |
| `systemd/atlas-error-tailer.service` | Long-running oneshot; `WorkingDirectory=/root/atlas`; `Restart=on-failure`; mirrors `atlas-silent-failure-watchdog.service` pattern | ~15 |
| `scripts/pi-cron.sh` | Add `stderr_capture` wrapper: on non-zero exit, capture last 50 lines + insert to `errors` table (source='cron') | ~35 |
| `tests/test_journald_tailer.py` | Mock `journalctl` output; verify parsing + dedup + DB insert | ~60 |

**Acceptance criteria:** `systemctl status atlas-error-tailer` is `active (running)`. Inject a test error via `journalctl --identifier=atlas-test`; verify row appears in `errors` table within 5 seconds. A cron that exits non-zero (simulate with `false`) produces a row with `source='cron'`.

---

### Action 3 — Stand Up the Phase 1 Dashboard Panel + Daily Digest (1 day)

| File | Change | LOC |
|---|---|---|
| `services/api/remediation.py` | Read-only FastAPI routes: `GET /api/remediation/errors` (time-series + top fingerprints), `GET /api/remediation/digest` (24h summary JSON) | ~100 |
| `dashboard-ui/src/components/RemediationPanel.tsx` | Error volume chart + top fingerprints table + digest card; extends existing panel pattern in `dashboard-ui/src/components/` | ~120 |
| `scripts/auto_remediate_digest.py` | Queries `errors` + `auto_fix_attempts`; formats Telegram digest message; reads `data/AUTO_REMEDIATION_HALT` to add pause warning | ~80 |
| `systemd/atlas-remediation-digest.timer` | `OnCalendar=*-*-* 23:00` (09:00 AEST) | ~10 |
| `systemd/atlas-remediation-digest.service` | Oneshot; `ExecStart=/usr/bin/python3 /root/atlas/scripts/auto_remediate_digest.py` | ~12 |
| `tests/test_remediation_api.py` | Test API endpoints with fixture DB | ~60 |

**Acceptance criteria:** Dashboard shows error volume time-series populated by Phase 1 data. Daily Telegram digest fires at 09:00 AEST with correct counts. API returns `200` with populated JSON. No regressions.

---

**Total Phase 1: ~3 engineering days** (3 actions above; each ~1 day). Ships entirely without auto-fix logic. After 1 week of dry-run triage accumulating data, Engineering Lead reviews classification accuracy against human samples and decides Phase 2 gate.

---

## Closeout — Required Decision Points Before Phase 2

The following questions must be answered by Engineering Lead before Phase 2 (ASSIST mode) ships:

### 1. Maximum Auto-Fix Budget Per Day
- **Commits/day:** Proposed 20. Acceptable? Floor: 5 (basically symbolic). Ceiling: not higher than 20 until 30-day clean track record.
- **Wall-time/day:** Proposed 10 min per attempt × max 20 = up to 200 min/day of agent compute. Acceptable?
- **Cost/day:** $0.00 (OAuth). Monitor only for subscription window exhaustion.

### 2. Initial AUTO_FIX-Eligible Classes (Phase 3 Day 1)
- Proposed: `tests/**`, `docs/**`, `dashboard-ui/src/**` (typos only), `tasks/**`, `scripts/healthz_*.sh`, `pi-package/**/skills/**/*.md`
- **Question A:** Is `tests/**` acceptable for full AUTO_FIX from day 1? (Recommend YES, but exclude `tests/brokers/` and `tests/test_live_executor*.py`.)
- **Question B:** Should `services/telegram_bot.py` ever be AUTO_FIX-eligible? (Recommend: ASSIST-forever — it is the notification channel.)

### 3. ASSIST-Mode-Forever Classes
Proposed classes that should NEVER auto-merge even after perfect track record:
- `services/telegram_bot.py` — notification channel integrity
- `services/chat_server.py` — FastAPI server with embedded trading SQL
- `monitor/**` — intraday monitoring logic
- **Question:** Agreement? Any additions or removals from this list?

### 4. Telegram Notification Cadence
- **Option A:** Daily digest only (1 message/day at 09:00 AEST)
- **Option B:** Daily digest + per-escalation alert (~3–5 extra messages/day at current bug rate)
- **Option C:** Daily digest + per-escalation + per-auto-fix-commit (high volume initially)
- **Recommendation:** Option B for Phase 1+2; add Option C toggle in Phase 3 config.
- **Question:** Preferred option?

### 5. Reviewer Agent Veto Power
- **Binding:** Reviewer REJECT = no merge; only human can force-merge via `/approve_fix <id>`.
- **Advisory:** Reviewer REJECT = Telegram alert with diff link; human can override.
- **Recommendation:** Binding. The review step has no value if it can't stop bad fixes.
- **Question:** Binding or advisory?

### 6. Domain Expansion Policy for AUTO_FIX
Proposed criteria for adding a new path to AUTO_FIX-eligible:
1. 30 days clean ASSIST track record on that path (zero false-positives)
2. ≥10 successful human-reviewed ASSIST merges in that specific path
3. Engineering Lead explicit sign-off
4. Revert rate for that path ≤ 2% over the 30-day ASSIST window

- **Question:** Are these criteria sufficient? Should there be an additional staging environment gate?

---

*Report generated 2026-04-29. Source files read: `utils/logging_config.py` (full, 250 lines), `scripts/healthz_autofix.sh` (full, 110 lines), `scripts/silent_failure_watchdog.py` (full, 244 lines), `scripts/auto_recover.sh` (header), `db/atlas_db.py` (first 120 lines + structure), `db/schema.sql` (CREATE TABLE inventory), `brokers/kill_switch.py` (all function defs), `brokers/live_executor.py` (function signatures), `/root/.pi/extensions/multi-team/index.ts` (domain enforcement section, lines 363–1920), `/root/.pi/teams/config.yaml` (full, 333 lines), `pi-package/atlas-ops/extensions/atlas-jobs/src/catalog.ts` (first 80 lines + structure), `systemd/*.service` + `systemd/*.timer` (inventory), `scripts/lint_bare_except.py` (header), `scripts/pi-cron.sh` (header), `scripts/claude_auth_check.py` (header), `bare_except_baseline.txt` (839 entries confirmed). All file paths verified via `ls`/`find` before inclusion.*
