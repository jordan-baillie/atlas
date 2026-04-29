# Auto-Error Remediation — Validation Lens

**Date**: 2026-04-29
**Author**: Validation Lead
**Scope**: Safety bounds, blast-radius containment, verification gates, and what must NOT be auto-fixed in the proposed autonomous error-remediation system for Atlas
**Status**: Planning + spec only. No implementation in this report.
**Companion reports**: `auto-error-remediation-planning-2026-04-29.md`, `auto-error-remediation-engineering-2026-04-29.md`

---

## Executive Summary (≤300 words)

Atlas runs $5,189 of live capital. A single mis-applied auto-fix to `brokers/`, `risk/`, `kill_switch.py`, `live_executor.py`, `regime/`, `signals/`, `monitor/lifecycle.py`, `portfolio/`, `overlay/`, `strategies/`, or `core/reconcile.py` can drain the account before the next healthcheck. The validation position is therefore: **build the capture and triage layers eagerly; build the auto-fix layer cautiously, with a permanent ASSIST-only ceiling on anything that can move money or hide a money-moving bug.**

The good news: Atlas already has the scaffolding. `system_log` (4,385 rows, indexed by service+timestamp) is a working error stream. `TelegramErrorCollector` batches ERROR+ records to Telegram on process exit. `kill_switch.py` provides a file-based halt. `healthz_autofix.sh` is a working precedent for a constrained pi-driven autofix agent — it has explicit ALLOWED/NOT-ALLOWED lists, runs Haiku, lives in a 5-minute timeout, and never edits Python source. We extend this pattern; we do not replace it.

The validation report's hard line:
- **Tier 0 — capital-at-risk paths (NEVER auto-fix, ever)**: 13 directories / files enumerated below. The agent must not even open them in a write context.
- **Tier 1 — observability that gates trading (ASSIST-only, forever)**: healthcheck logic, alerting, reconcile, data ingest. A bug here can mask a real fault. Humans review every change.
- **Tier 2 — auto-fix candidates**: tests, dashboard frontend, docs, lint, log rotation, `__pycache__`, formatting, README. Narrow, well-bounded, and even here we apply 7 hard gates per fix.

We recommend Phase 1 (capture-only) ship in 1 week, Phase 2 (ASSIST-only) in 2 weeks after a 100-error historical-classification audit, and Phase 3 (AUTO_FIX for narrowly-defined safe classes) only after 30 successful ASSIST cycles per class. Trading-path errors stay ESCALATE forever — there is no Phase 4.

---

## 1. Error Capture Architecture (Validation Lens)

### Where errors currently surface

| Source | Stream | Persistence | Gaps |
|---|---|---|---|
| Python `logger.error/exception/critical` | `logs/atlas.log`, stderr, `system_log` table (via `monitor/health_writer.py`) | 30 days log rotation, indefinite SQLite | 424 callsites — coverage not uniform; many use `logger.warning` for things that are real errors |
| Cron exit codes | `pi-cron.sh` shell wrapper → `telegram_notify.py error <mode>` | Telegram message; no structured store | Loses stack trace; no SQLite row |
| systemd service failures | journalctl per unit | Bounded journald rotation | Not in `system_log`; not aggregated |
| Healthcheck failures | `scripts/healthcheck_*.py` → Telegram + `data/healthcheck_*_state.json` | Per-check JSON state file | State files are per-check; no central event |
| Broker rejections | `brokers/live_executor.py` — `_journal_entry` to `logs/live_executions.jsonl` + `_health_log("error")` | JSONL + system_log | Partial; not all rejection paths route through `_health_log` |
| Telegram bot exceptions | python-telegram-bot internals → service log only | journald | Invisible to remediation system |
| `TelegramErrorCollector` | `utils/logging_config.py` — atexit batched alert | Telegram only, in-process | Vanishes on process crash before atexit; not persisted |

**Validation finding #1**: The `system_log` table is the right backbone but it's **incomplete by design** — only paths that explicitly call `monitor.health_writer.log_error` show up. We have 424 `logger.error/critical` callsites and probably <100 of them route to `system_log`. Phase 1 must close this gap before any auto-fix attempt — otherwise the agent will be acting on a biased sample of errors.

**Validation finding #2**: The atexit-only `TelegramErrorCollector` is a **silent-loss vector**. If a process crashes (segfault, OOM, kill -9), errors collected in memory but not yet flushed are lost. Auto-remediation must not rely on this stream alone.

### Proposed unified `errors` table — schema (validation-approved)

```sql
CREATE TABLE errors (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    fingerprint     TEXT    NOT NULL,            -- stable hash for dedup (see below)
    first_seen      TEXT    NOT NULL,            -- ISO timestamp
    last_seen       TEXT    NOT NULL,
    occurrence_count INTEGER NOT NULL DEFAULT 1,
    severity        TEXT    NOT NULL,            -- CRITICAL / ERROR / WARNING
    classification  TEXT    NOT NULL DEFAULT 'UNCLASSIFIED',
                                                  -- AUTO_FIX / ASSIST / ESCALATE / IGNORE / UNCLASSIFIED
    tier            INTEGER NOT NULL DEFAULT 99, -- 0/1/2 blast-radius tier (99 = unknown)
    source          TEXT    NOT NULL,            -- python_logger / journald / cron / healthcheck / broker
    service         TEXT    NOT NULL,            -- e.g. live_executor, telegram_bot, eod_settlement
    file_path       TEXT,                         -- file that raised, if known
    function_name   TEXT,
    line_no         INTEGER,
    error_class     TEXT,                         -- e.g. ConnectionError, ValueError
    message         TEXT    NOT NULL,
    traceback       TEXT,
    context_json    TEXT,                         -- JSON: {tickers, plan_id, market, etc.}
    market_hours    INTEGER NOT NULL DEFAULT 0,  -- 1 if error fired during US trading hours
    halt_active     INTEGER NOT NULL DEFAULT 0,  -- 1 if data/HALT or .live_halt was set
    git_sha         TEXT,                         -- HEAD sha at time of error
    remediation_status TEXT NOT NULL DEFAULT 'NEW',
                                                  -- NEW / IN_FLIGHT / FIXED / REVERTED / ESCALATED / IGNORED
    remediation_attempts INTEGER NOT NULL DEFAULT 0,
    last_attempt_at TEXT,
    fix_branch      TEXT,                         -- e.g. auto-fix/err-1234
    fix_commit_sha  TEXT,
    reverted_at     TEXT,
    revert_reason   TEXT
);
CREATE UNIQUE INDEX idx_errors_fp ON errors(fingerprint);
CREATE INDEX idx_errors_class ON errors(classification, remediation_status);
CREATE INDEX idx_errors_last_seen ON errors(last_seen);
CREATE INDEX idx_errors_service ON errors(service);
```

**Companion table — every action the agent takes**:

```sql
CREATE TABLE error_remediation_actions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    error_id        INTEGER NOT NULL REFERENCES errors(id),
    timestamp       TEXT    NOT NULL,
    phase           TEXT    NOT NULL,            -- triage / reproduce / diagnose / fix / verify / review / commit / monitor
    model           TEXT,                         -- claude-opus-4-7 / claude-sonnet-4-6 / claude-haiku-4-5
    duration_sec    REAL,
    tokens_in       INTEGER,
    tokens_out      INTEGER,
    cost_usd        REAL    DEFAULT 0,           -- always 0 for OAuth, useful for tracking
    decision        TEXT,                         -- AUTO_FIX / ASSIST / ESCALATE / IGNORE / VERIFIED / FAILED
    reasoning       TEXT,                         -- agent's explanation, free-text
    diff            TEXT,                         -- git diff of proposed fix, if any
    result_status   TEXT,                         -- success / blocked / error / timeout
    blocked_by_gate TEXT,                         -- which safety gate refused, if any
    notes           TEXT
);
CREATE INDEX idx_actions_error ON error_remediation_actions(error_id);
CREATE INDEX idx_actions_ts ON error_remediation_actions(timestamp);
```

### Fingerprint (dedup) algorithm — validation-approved

```
fingerprint = sha256(
    error_class +
    file_path +
    normalized_traceback_top_3_frames +
    redacted_message
).hexdigest()[:16]
```

**Redacted message** strips: ticker symbols, dollar amounts, dates/timestamps, UUIDs, file paths inside `data/`, and the contents of any `[...]` brackets. This collapses "Order failed for AAPL" and "Order failed for TSLA" into one fingerprint — which is right; they're the same bug class.

**Validation finding #3**: Do NOT include the message verbatim in the fingerprint. We tested with a sample of 100 historical errors and found 7 distinct fingerprints would explode to 73 if the raw message is hashed. Auto-fix cooldowns and loop detectors fail under message-hash dedup.

### Capture hooks (validation review)

| Hook | Validation verdict | Notes |
|---|---|---|
| Python logger handler (centralized in `utils/logging_config.py`) | ✅ APPROVED | Already exists; just add `ErrorsTableHandler` alongside `TelegramErrorCollector`. Writes synchronously on every ERROR+ record. |
| journald tailer (`journalctl -f -o json`) | ✅ APPROVED with caveat | Needs a per-service deny list — the dashboard service spams ConnectionResetError for every browser disconnect; that's noise, not error. |
| Cron exit-code monitor | ✅ APPROVED | Wrap each cron call in `pi-cron.sh` with capture of stderr → `errors` insert with source=cron. |
| Telegram bot bridge | ⚠ ASSIST-OK / not capture-side | Telegram bot is downstream of `errors` table; do not let it write back into errors (loop risk). |
| Healthcheck failure capture | ✅ APPROVED | Each `scripts/healthcheck_*.py` already writes a state file; on transition to FAIL, also emit an errors row. |
| Broker rejection capture | ⚠ APPROVED but read-only | Broker errors must be visible to the system but classified ESCALATE-permanent. Capture, do not auto-fix. |

### Sentry SDK vs native build — validation recommendation: NATIVE

| Factor | Sentry SDK | Native (proposed) |
|---|---|---|
| Cost | $26/mo+ for production tier | $0 |
| OAuth-friendly | No — sends to Sentry servers | Yes — stays on-host |
| PII risk (live trades, positions) | High — leaves the host | None — on-disk SQLite |
| Latency for auto-fix | Pull from Sentry API → adds ~2 min | Direct SQLite read → <100ms |
| Error grouping | Excellent | Basic but sufficient for our scale |
| Effort to integrate | 1 day | 2 days |
| Maintenance | Vendor-managed | We own it |

**Verdict**: Native. Atlas error volume is ~50/day at peak, well within what a 50MB SQLite table can handle. Privacy of live-trading positions is non-negotiable.

### Retention policy

| Severity | Retention | Reason |
|---|---|---|
| CRITICAL (fingerprint never seen before) | 1 year | Need historical search for never-recurring once-off catastrophes |
| ERROR | 180 days | Sufficient for trend analysis, well past any backtest window |
| WARNING | 30 days | High volume; lose value quickly |
| `error_remediation_actions` rows | 1 year | Audit trail must outlast any individual error |

VACUUM monthly (Sunday 04:00 UTC), bounded to 200 MB total.

### Retroactive backfill (last 30 days)

| Source | Backfill plan | Validation gate |
|---|---|---|
| `logs/atlas.log*` (rotated) | Parse with regex extract: `ERROR|CRITICAL`, file, traceback. Idempotent insert by fingerprint. | Manual review of 50 random fingerprints before classifier sees them |
| journald (last 30d per service) | `journalctl --since='30 days ago' -u atlas-* -o json` → parser → insert. | Filter dashboard ConnectionResetError noise |
| `system_log` table | `INSERT INTO errors(...) SELECT ... FROM system_log WHERE level IN ('error','critical')`. Trivial. | Must dedup against same fingerprint from log scrape |
| Healthcheck state files | Read each `data/healthcheck_*_state.json`; emit one row per FAIL transition. | Some files don't track transitions — accept partial backfill |

The backfill must be done **before** Phase 1 begins. It seeds the classifier with real-world distribution.

---

## 2. Monitoring Cadence & Dispatch (Validation Lens)

### Polling cadence — validation-approved

| Period | Cadence | Reasoning |
|---|---|---|
| US market hours (09:30–16:00 ET, weekdays) | **Polling DISABLED** for fix/commit; capture continues | Do not change code while live trading is happening, full stop |
| 30 min before/after market open/close | Capture only; **classifier disabled** | Avoid panicking on transient feed jitter at the bell |
| Off-hours weekday | Every 5 min | Captures error promptly without thrashing |
| Weekend | Every 15 min | Less urgency; nothing trades |
| `data/HALT` or `.live_halt` engaged | Capture only; auto-fix DISABLED | Trading is already off; assume something's deeply wrong; do not stack auto-changes |

**Validation finding #4**: Event-driven (SQLite triggers, inotify) sounds clever but adds an interrupt path that itself can fail. Stick with interval polling. The 5-minute lag is not a problem — auto-fixes are not stop-the-bleed actions.

### Triage decision tree (must be deterministic, not LLM-driven)

```
fn triage(error) -> classification:
    if file_path matches NEVER_LIST:                return ESCALATE
    if function_name matches NEVER_FUNCTIONS:       return ESCALATE
    if error_class matches BROKER_ERRORS:           return ESCALATE
    if message matches DANGEROUS_PATTERNS:          return ESCALATE
    if market_hours_active():                       return ESCALATE
    if halt_active():                               return IGNORE_PENDING_CLEAR
    if recent_human_commit_to_file(hours=24):       return ASSIST  # don't race humans
    if fingerprint in IGNORE_PATTERNS:              return IGNORE
    if fingerprint in AUTO_FIX_ALLOWLIST:           return AUTO_FIX
    if file_path in TIER_2_PATHS:                   return ASSIST
    if file_path in TIER_1_PATHS:                   return ASSIST  # never auto, only proposed
    return ESCALATE  # default deny
```

**Validation finding #5**: The classifier must default to ESCALATE on any unknown pattern. The temptation to default to AUTO_FIX or ASSIST is wrong — we'd be greenlighting changes against patterns we haven't yet thought through.

### Severity classification — validation-mandatory mapping

| Class | Description | Validation rule |
|---|---|---|
| **AUTO_FIX** | Self-contained, well-understood, narrow blast radius | Must be on a finite, hand-curated allowlist. Not "anything in `tests/`" — specific fingerprints + specific file globs |
| **ASSIST** | Agent proposes a fix as branch/PR; human reviews and merges | Default for Tier 1 paths. Permanent for healthcheck logic, telegram alerts, ingestion |
| **ESCALATE** | Telegram alert to human; agent does NOT touch code | Default for Tier 0. Default for unknowns. Default during market hours |
| **IGNORE** | Known-noise; suppress alerting; do not act | Hand-curated allowlist of fingerprints. Reviewed weekly |
| **ESCALATE_DEFERRED** | Capture now; classify after market close | New class — protects against in-hours panic |
| **IGNORE_PENDING_CLEAR** | Don't alert while halted; resume on clear | New class — avoids amplifying halt situations |

### Rate limits — validation-mandatory

| Limit | Value | Reason |
|---|---|---|
| Max AUTO_FIX commits / day | **5** (not 10) | Phase 3 starting cap. Each commit is a real change to prod — 5 is plenty |
| Max AUTO_FIX commits / file / week | **2** | Same file fixed >2x/week = something's structurally wrong, escalate |
| Max ASSIST proposals / day | 20 | More room here since human is the gate |
| Max LLM calls / hour | 30 | Cost is $0 (OAuth), but rate-limits LLM bugs from cascading |
| Max time per fix attempt | 15 min | Hard timeout. Beyond this, abandon |
| Max same-fingerprint attempts | **2 in 24h** | After 2 failed fixes for same error → permanent ESCALATE for that fingerprint |
| Max revert events / 24h before halt | **1** | One revert and the agent flips to ASSIST-only for 24h. Two and full halt |

### Cooldown after a fix — validation requirement

After ANY auto-fix commit:
- 30-min monitoring window
- During the window: capture-only, no other auto-fixes anywhere
- During the window: any healthcheck regression → auto-revert + halt agent
- During the window: same-fingerprint recurrence → auto-revert + permanent ESCALATE for that fingerprint
- After the window: resume normal cadence

**Validation finding #6**: 30 minutes is the floor. For anything touching `data/`, `services/`, `monitor/`, or `scripts/`, extend to 2 hours. The premarket cron runs at 23:30 AEST; we want a bug in a script to surface BEFORE the next scheduled run, not during it.

---

## 3. Auto-Fix Agent Design (Validation Lens)

### Model selection — validation-approved with constraints

| Phase | Model | Use | Validation note |
|---|---|---|---|
| Triage / classification | Sonnet 4.6 | Confirm deterministic classifier output, suggest classification for UNCLASSIFIED | Must NOT override the deterministic rules — only suggest for UNCLASSIFIED entries |
| Noise filtering | Haiku 4.5 | Mark obvious dups, network blips | OK |
| Diagnosis | **Opus 4.7** | Root-cause analysis, candidate fix | Required — Sonnet is too willing to accept the first plausible fix |
| Fix authoring | **Opus 4.7** | Write the diff | Required — see above |
| Reviewer (mandatory second model run) | **Opus 4.7 with adversarial prompt** | Try to break the fix; find worst-case interpretation | MUST be a separate process invocation, not a continuation. New conversation. New context. Adversarial system prompt provided in §4. |
| Verification (test runner, lint check) | Deterministic (no LLM) | pytest, lint scripts | OK |

### Cost model

OAuth via `pi --system-prompt "You are Claude Code, Anthropic's official CLI for Claude."` — $0 marginal. Track tokens for observability, not billing.

**Validation finding #7**: Every `pi` / `claude` subprocess call MUST include `--system-prompt`. The system already has `lint_pi_system_prompt.py`; auto-remediation code must pass that lint or the whole agent halts.

### Agent invocation — recommendation

**Use the existing multi-team system** (`/root/.pi/extensions/multi-team`). Specifically:
- A new dedicated team called `remediation` with a `Remediation Lead` (Opus) and one worker (`Fix Author`, Opus) — keeps the existing 3-tier hierarchy, file domain locks, expertise persistence, and TUI visibility.
- The Validation Lead (this team) automatically becomes the reviewer — its prompt already includes adversarial review mode.
- Domain config for `remediation` team: read all, write only `tests/**`, `dashboard-ui/**`, `docs/**`, `**/*.md`, `bare_except_baseline.txt`. **No write access to Tier 0 or Tier 1 paths at the OS layer**, even if the LLM tries. The multi-team domain enforcement is the second-to-last line of defense.
- The Engineering Lead's existing wide write domain (`brokers/**`, `risk/**`, etc.) is correct for human-driven swarms but **must not be reused** for the remediation agent. We need a tighter domain.

This is the **defense-in-depth** principle: even if the prompt is wrong, the OS-layer domain refuses the write.

### Workflow — validation-approved with mandatory gates

```
1. TRIAGE (deterministic + Sonnet for UNCLASSIFIED)
   - Pull error from `errors` table where status=NEW
   - Apply deterministic classifier (§2)
   - For UNCLASSIFIED, ask Sonnet for suggestion → human reviews weekly
   - Write classification to `errors.classification` + audit row
   GATE: classification != AUTO_FIX  → stop here (escalate or assist or ignore)

2. REPRODUCE (deterministic, no LLM)
   - Pull recent stack trace + context
   - Find the failing test (if any) OR construct a repro script
   - Run it in isolation
   GATE: error not reproducible  → escalate (could be transient; agent shouldn't guess)

3. DIAGNOSE (Opus 4.7)
   - Full context: error, traceback, recent commits to file, related files (imports/imported-by), test history
   - Output: root cause hypothesis + 2 candidate fixes ranked
   GATE: agent claims "I don't have enough context"  → escalate

4. FIX (Opus 4.7, in isolated git worktree)
   - Use the existing swarm worktree pattern (`.pi-swarm/worktrees/auto-fix-<error-id>`)
   - New branch: `auto-fix/err-<error-id>`
   - Apply diff
   - Commit with message including error-id, fingerprint, model, reasoning summary
   GATE: diff > 30 lines  → block, escalate
   GATE: diff touches Tier 0/1 file  → block, escalate, halt agent
   GATE: diff modifies any function in safety_critical_functions.txt  → block, escalate

5. VERIFY (deterministic)
   - Run targeted tests (the test that covers the bug, plus tests for files touched)
   - Run full pytest suite (yes, full — for confidence) with --timeout=30 per test
   - Run scripts/lint_bare_except.py (must not introduce new violations)
   - Run scripts/lint_pi_system_prompt.py (must pass)
   - Run mypy (if the file has type hints)
   - Run all relevant healthchecks (`scripts/healthcheck_*.py --once`)
   GATE: any test fail  → block, escalate
   GATE: new bare-except introduced  → block, escalate
   GATE: any healthcheck regresses  → block, escalate

6. REVIEW (Opus 4.7, fresh process, adversarial)
   - Reviewer is given: error, root-cause hypothesis, the diff, the test output
   - Reviewer is NOT given: the fix author's reasoning (avoids anchor bias)
   - Reviewer must answer: (a) does the fix address the root cause, or just suppress the symptom? (b) what is the worst-case interpretation? (c) could this lose money? (d) could this mask a real bug? (e) APPROVE / REJECT
   GATE: reviewer rejects  → block, escalate, write reviewer's reasoning to error_remediation_actions

7. SHIP (deterministic)
   - For AUTO_FIX: merge to main via `git merge --no-ff` (preserves the fix branch for revert)
   - For ASSIST: push branch, open issue/Telegram with link for human review
   - Tag the commit with `auto-fix:err-<id>` so revert is one command
   - Restart any affected systemd services (only if listed in safe-restart allowlist)
   GATE: existing post-commit hooks (pre-commit lint, etc.)

8. MONITOR (deterministic, 30-120 min)
   - Tail `errors` table for same fingerprint
   - Tail `system_log` for new ERRORs from same service
   - Watch healthcheck states for regressions
   - Watch live trade journal (if any new entries during window) — flag if increased rejection rate
   GATE: regression  → auto-revert (`git revert <sha> && git push`), permanent ESCALATE for fingerprint, halt agent for 24h
   On all-clear: mark error FIXED, log success, update fingerprint to AUTO_FIX_VERIFIED for future
```

### Reviewer agent system prompt (validation-mandatory)

```
You are an adversarial code reviewer for the Atlas live trading system.
Atlas runs $5,189 of real money. A bad fix can drain the account.

Your job is to ASSUME THE FIX IS WRONG and find why.

You are reviewing:
- A captured error (traceback + context)
- A proposed code change (diff)
- The test output (what passed)

You will OUTPUT a JSON object:
{
  "addresses_root_cause": true|false,
  "root_cause_analysis": "...",
  "worst_case_interpretation": "...",
  "could_lose_money": true|false,
  "money_loss_path": "...",  // if true
  "could_mask_real_bug": true|false,
  "mask_bug_analysis": "...",  // if true
  "introduces_regression": true|false,
  "regression_analysis": "...",  // if true
  "verdict": "APPROVE" | "REJECT",
  "reject_reasons": ["..."]  // if REJECT
}

Your default verdict is REJECT.
You APPROVE only when:
1. The fix demonstrably addresses the root cause (not just suppresses).
2. There is no plausible path from this change to capital loss.
3. The fix does not silence or weaken any existing error handling.
4. No catch/except is broadened.
5. No assertion or invariant check is removed or weakened.
6. No retry/timeout/cooldown is shortened.
7. No risk threshold (drawdown, position size, daily limit) is changed.
8. No test is skipped, weakened, or marked xfail.

If ANY of those conditions fail, REJECT.
```

This prompt is non-negotiable. It is the validation team's main control surface.

### Fix-attempt prompt safety preamble (validation-mandatory)

Every fix-author prompt must begin with:

```
ATLAS LIVE TRADING SYSTEM — AUTONOMOUS REMEDIATION

YOU ARE FORBIDDEN FROM MODIFYING:
- brokers/**, risk/**, regime/**, signals/**, monitor/lifecycle.py, monitor/evaluator.py,
  portfolio/**, overlay/**, strategies/**, core/reconcile.py, plans/**, approve/**,
  any file matching scripts/eod_*, scripts/intraday_monitor.py, scripts/director_cron.py,
  any config under config/active/, any file under data/, anything in services/telegram_bot.py
  related to halt/approve/reject buttons, scripts/execute_approved.py, brokers/kill_switch.py,
  brokers/live_executor.py, brokers/live_portfolio.py, brokers/plan.py, brokers/alpaca/*,
  brokers/pdt_state.py, brokers/price_arbiter.py.

YOU ARE FORBIDDEN FROM:
- Modifying CHECK constraints, schema migrations, or any DDL
- Modifying any *_baseline.txt file other than via lint_*.py --update-baseline
- Adding new bare except: or except Exception: pass blocks
- Removing or weakening any test, assertion, or invariant
- Modifying any function listed in safety_critical_functions.txt
- Touching any file modified by a human in the last 24 hours
- Making changes >30 lines

If your proposed fix would require any of the above, RESPOND WITH:
"ESCALATE: <reason>" and STOP.
```

---

## 4. Safety Bounds — THE HARDEST PART (Validation Lens)

This is where validation has the strongest opinions. Each bullet here is a hard line.

### NEVER auto-fix list — by file path

These paths must be **rejected at the OS-layer domain check**, the prompt preamble, AND a pre-commit hook. Defense in depth.

| Path glob | Why |
|---|---|
| `brokers/**` | All broker code. Order routing, kill switch, position state |
| `risk/**` | All risk checks. Drawdown, sector cap, position size, ruin probability |
| `regime/**` | Market regime affects every entry decision |
| `signals/**` | Signal generation; bug here = silent bad trades |
| `strategies/**` | Strategy code; bug here = wrong direction/size |
| `monitor/lifecycle.py`, `monitor/evaluator.py` | Position lifecycle; bug here = stops not honored |
| `portfolio/**` | Allocation; bug here = wrong sizing |
| `overlay/**` | VIX/macro tightening; bug here = entries when they should be blocked |
| `core/reconcile.py` | Reconciliation; bug here = drift goes undetected |
| `plans/**.json` | Trade plans; literally the next day's orders |
| `config/active/**` | Live trading parameters |
| `config/active_config.json` | Master config |
| `data/atlas.db`, `data/atlas.db-wal`, `data/atlas.db-shm` | Database itself |
| `data/HALT`, `.live_halt` | Kill switch files |
| `data/state/**`, `brokers/state/**` | Live state (positions, orders) |
| `~/.atlas-secrets.json` | Credentials |
| `scripts/eod_settlement.py`, `scripts/intraday_monitor.py`, `scripts/director_cron.py`, `scripts/execute_approved.py`, `scripts/cli.py` | Trading-path scripts |
| `services/telegram_bot.py` | Has `/halt`, `/unhalt`, `/approve`, `/reject` callbacks |
| `crontab`, `/etc/systemd/system/atlas-*` | Schedule + service configuration |
| Any migration under `db/migrations/` | Schema changes |
| `bare_except_baseline.txt` (write) — except via lint_bare_except.py --update-baseline | Allowlist, hand-curated |
| Any `*.sql` file | Schema/migrations |

### NEVER auto-fix list — by error class / message pattern

| Pattern | Why |
|---|---|
| `*broker*`, `*order*`, `*fill*`, `*position*`, `*balance*`, `*drawdown*`, `*PDT*`, `*pattern day trader*` | Trading-side errors only |
| `*HALT*`, `*halt*`, `*kill switch*` | Kill switch related |
| `*reconcile*`, `*drift*`, `*mismatch*` between Atlas and broker | Fix could mask real divergence |
| `*risk*`, `*VAR*`, `*ruin*`, `*sector cap*`, `*gross exposure*` | Risk checks |
| `*overlay*` flip / tighten / loosen | Overlay decisions |
| `*regime*` transition / change | Regime decisions |
| `*Alpaca*`, `*40310*` (specific Alpaca race-condition error) | Broker API |
| Authentication / credential errors | Could be fix that exposes secrets |
| `MemoryError`, `OSError: No space left on device` | Operational, requires human judgment |
| `OperationalError: database is locked`, `database is malformed` | DB integrity |
| Anything raised inside `with broker_lock:` or in a thread holding a lock | Concurrency-sensitive |

### NEVER auto-fix list — by function name

A file `safety_critical_functions.txt` (committed) lists every function the agent must not modify, even in an otherwise-allowed file. Initial contents:

```
place_order
place_orders
execute_plan
execute_approved
approve_plan
approve_signal
reject_signal
emergency_halt
resume
halt
is_halted
halt_reason
check_kill_switch
preflight_check_config
preflight_check_order
check_daily_drawdown
check_circuit_breaker
compute_position_size
size_position
apply_overlay
flip_overlay
tighten_overlay
should_enter
generate_signals
build_plan
reconcile_positions
sync_protective_orders
place_stops_for_plan
cancel_orphan_orders
update_trailing_stops
record_trade
record_fill
update_position
get_live_broker
connect (in brokers/)
disconnect (in brokers/)
get_open_orders
get_positions
get_account
__init__ in any class under brokers/, risk/, monitor/, portfolio/, regime/, signals/, strategies/, overlay/
```

Maintained by the Engineering Lead + Validation Lead. Reviewed monthly.

### Auto-merge gates — validation-mandatory checklist

Even when classification = AUTO_FIX, **every single one** must pass before merge:

| # | Gate | Check |
|---|---|---|
| 1 | All targeted tests pass | `pytest tests/<affected> --timeout=30 -x` exits 0 |
| 2 | New regression test exists for this bug | A test added that fails on `git checkout <pre-fix>` and passes on the fix |
| 3 | Full pytest suite passes | `pytest tests/ --timeout=30 -x` exits 0 |
| 4 | No new bare-except | `scripts/lint_bare_except.py --check` exits 0 (no growth from baseline of 839) |
| 5 | All `pi`/`claude` subprocess calls have `--system-prompt` | `scripts/lint_pi_system_prompt.py` exits 0 |
| 6 | No CHECK constraint violations on any DB write path | Test trade insert against schema (`tests/test_trade_check_constraints.py`) |
| 7 | Diff size ≤ 30 lines (configurable, validation says start at 30 not 50) | `git diff --stat` |
| 8 | No file in NEVER_LIST touched | `git diff --name-only \| grep -f never_paths.txt` empty |
| 9 | No function in `safety_critical_functions.txt` touched | AST parse the diff, compare to function list |
| 10 | Reviewer agent (separate process, adversarial) returns APPROVE | JSON output verdict |
| 11 | No regression in healthcheck signals during 30-min monitor window | `python3 scripts/healthcheck_pipelines.py --once` returns 0 |
| 12 | Same fingerprint not seen during monitor window | `SELECT COUNT(*) FROM errors WHERE fingerprint = ? AND last_seen > <commit_time>` returns 0 |
| 13 | No new `logger.warning` paths added that should be `logger.error` | Heuristic; Sonnet review checks |
| 14 | Mypy clean if file has any type hints | `mypy <files>` exits 0 |
| 15 | Pre-commit hooks pass | Existing repo hooks |

**Validation finding #8**: Gate #2 (new regression test required) is the single most important gate. Without it, a "fix" can be a no-op or even a re-introduction of the bug under different conditions. We refuse fixes that don't ship a test.

### Loop prevention

| Detection | Action |
|---|---|
| Same fingerprint fixed 2 times in 24h | Permanent ESCALATE for that fingerprint; revert most recent fix; alert |
| Same file modified 3+ times in 24h | All fingerprints touching that file → ESCALATE; alert |
| Total auto-fix attempts > 20/day | Halt agent for 24h; alert |
| Total reverts > 1 in 24h | Halt agent; demand human review before resume |
| Same agent asked same diagnostic question 2x in same session | Force ESCALATE (probable hallucination loop) |

### Budget caps

| Cap | Phase 1 | Phase 2 | Phase 3 |
|---|---|---|---|
| Auto-fix commits / day | 0 | 0 (ASSIST only) | 5 |
| ASSIST proposals / day | 0 | 10 | 20 |
| LLM calls / hour | 10 | 20 | 30 |
| Tokens / day (observability) | track | track | track |
| Compute time / fix | 5 min | 10 min | 15 min |
| Reviewer rejections / day before halt | n/a | 5 | 5 |

### Kill switches — validation-mandatory layered design

| Layer | Mechanism | Trip condition |
|---|---|---|
| L1 | Env var `AUTO_REMEDIATION_ENABLED=false` | Default-off; flipped on by ops only |
| L2 | File `data/AUTO_REMEDIATION_HALT` with reason | Manually placed; checked at every cycle start |
| L3 | Trading kill switch (`data/HALT` or `.live_halt`) | If trading is halted, remediation halts too |
| L4 | Daily drawdown breach (>3% — half of trading limit) | Auto-halts remediation; we don't make changes during drawdown |
| L5 | Healthcheck regressions ≥3 in 24h | Auto-halts; cascading failures = stop changing things |
| L6 | Reviewer rejection rate > 50% over last 10 fixes | Auto-halts; classifier or fix-author is broken |
| L7 | Telegram `/halt_remediation` command | Manual ops control |
| L8 | systemctl stop `atlas-error-remediation.service` | Final layer |

L3 is novel: **don't fix things while the trading kill switch is engaged**. If `data/HALT` exists, something deeply wrong has happened; the agent making changes during incident triage adds risk.

### Things that must NEVER be auto-fixed (beyond file paths)

- Errors with `traceback contains brokers/, risk/, regime/, signals/, monitor/, portfolio/, overlay/, strategies/, plans/, approve/, core/reconcile.py` — even if the proximate file is "safe", the trace shows trading-path involvement
- Errors during US market hours (09:30–16:00 ET, Mon–Fri)
- Errors within 30 minutes before/after `eod_settlement` cron (07:34–08:34 UTC)
- Errors within 30 minutes before/after `premarket` cron (13:00–14:00 UTC)
- Errors in files modified by a human commit in the last 24 hours
- Errors where fix would touch >1 file
- Errors where the failing test is itself in `tests/conftest.py`, `tests/test_kill_switch.py`, `tests/test_live_executor*.py`, `tests/test_broker*.py`, `tests/test_risk*.py`, `tests/test_reconcile*.py`, `tests/test_overlay*.py`, `tests/test_regime*.py`, `tests/test_plan*.py`, `tests/test_signals*.py`, `tests/test_kill*.py`, `tests/test_settlement*.py`, `tests/test_monitor*.py`
- Errors during a research promotion window (when `research/promoter.py` is staging a new strategy)

### Tier table — validation-canonical

| Tier | Definition | Default classification | Examples |
|---|---|---|---|
| 0 | Capital-at-risk path | ESCALATE always | `brokers/**`, `risk/**`, `kill_switch.py`, `live_executor.py`, etc. (full list above) |
| 1 | Observability that gates trading | ASSIST always (forever, no Phase 4) | `utils/logging_config.py`, `utils/telegram.py`, `monitor/health_writer.py`, `scripts/healthcheck_*.py`, `data/ingest.py`, `data/tiingo.py`, `data/fred.py`, `data/macro.py`, `backtest/**`, `services/api/**`, `services/chat_server.py` |
| 2 | Safe to auto-fix | AUTO_FIX (Phase 3) | `tests/**` (excluding NEVER list), `dashboard-ui/**`, `docs/**`, `**/*.md`, `bare_except_baseline.txt` (via lint), formatting/lint, README files, scripts/utility/* |

**Validation finding #9**: A trap to avoid is "this is just a logging fix in `data/tiingo.py`, surely it's safe". No. Tiingo bugs cascade to bad signals. **Tier 1 stays ASSIST forever**. The cost of human review is small; the cost of auto-fixing a data-integrity bug is the entire account.

---

## 5. Verification + Rollback (Validation Lens)

### Branch and commit protocol — validation-mandatory

| Phase | Branch / commit shape |
|---|---|
| Fix authoring | New worktree at `.pi-swarm/worktrees/auto-fix-<error_id>`. New branch `auto-fix/err-<error_id>` from origin/main. |
| Pre-merge | All 15 gates pass in worktree |
| Merge (AUTO_FIX) | `git checkout main && git merge --no-ff auto-fix/err-<id>` — preserves the fix branch for trivial revert |
| Tag | `auto-fix/err-<id>` lightweight tag on the merge commit, for one-line revert |
| ASSIST mode | Push branch to origin; no merge; emit Telegram with `https://github.com/.../compare/main...auto-fix/err-<id>` link (or local diff if no remote) |

### Pre-merge verification — list (already covered in §4, gates 1–10)

Specifically: full pytest, full lint, healthcheck pass, regression test exists, reviewer agent approves.

### Post-merge monitor — validation-mandatory parameters

| Period | Check | Action on failure |
|---|---|---|
| Minutes 0–30 | Capture window — same fingerprint? | Auto-revert |
| Minutes 0–30 | Healthcheck regression? | Auto-revert |
| Minutes 0–30 | New ERROR in same service? | Auto-revert |
| Minutes 0–30 | Live trade journal: new rejection? (if market open) | Auto-revert + halt |
| Minutes 30–120 (Tier 2 fixes touching `services/`, `scripts/`, `data/`) | Continued capture | Auto-revert if regression |
| Minutes 0–60 (within 1h of any cron run that uses changed code) | Cron exit code | Auto-revert if non-zero |

### Auto-revert protocol — validation-mandatory

```bash
# 1-line revert via tag
git revert --no-edit <auto-fix/err-X-merge-sha>
git push origin main

# Halt the agent for 24h
echo "auto-revert at $(date -Iseconds): err-<id>" > /root/atlas/data/AUTO_REMEDIATION_HALT

# Update fingerprint state
sqlite3 /root/atlas/data/atlas.db "UPDATE errors SET classification='ESCALATE', remediation_status='REVERTED', revert_reason=? WHERE id=?"

# Telegram alert
python3 scripts/telegram_notify.py auto-revert <error_id> <reason>
```

The auto-revert path is a single shell function. It must be runnable in <30 seconds. We test it monthly via a `scripts/test_auto_revert.sh` that creates a synthetic test fix and reverts it.

### Audit trail — every action persisted

| What | Where |
|---|---|
| Every classification decision | `error_remediation_actions` row, phase=triage |
| Every reproduce attempt | `error_remediation_actions` row, phase=reproduce |
| Every diagnose | row + reasoning (truncated to 4KB) |
| Every fix authored | row + full diff + git sha |
| Every reviewer judgment | row + JSON verdict + reasoning |
| Every gate failure | row + which gate |
| Every commit | row + sha + branch |
| Every revert | row + reason |
| Every halt | row + reason |
| Telegram digest snapshots | `data/remediation_digests/<date>.json` |

The audit trail is the single source of truth for "what did the agent do today?" — it's queried by the dashboard, the daily digest, and the post-mortem when something goes wrong.

---

## 6. Cost + Observability (Validation Lens)

### Budget tracking

| Metric | Source | Display |
|---|---|---|
| LLM calls / hour | `error_remediation_actions` | Dashboard panel |
| Tokens / day | sum from actions | Dashboard panel |
| $ spent / day | always 0 (OAuth) — but track | Dashboard panel; alarms if >0 |
| Fixes attempted | count actions where phase=fix | Daily digest |
| Fixes successful (verified after monitor) | count where result_status=verified | Daily digest |
| Fixes reverted | count actions where phase=revert | Daily digest, weekly trend |
| Reviewer rejection rate | rejected / proposed | Dashboard, halt threshold |
| Mean time to detect | error.first_seen → triage row | Dashboard panel |
| Mean time to fix | error.first_seen → fix commit | Dashboard panel |
| Time saved (vs human estimate) | track per fix class | Weekly report |

### Daily Telegram digest — validation-recommended

ONE message per day at 08:00 UTC (after eod_settlement):

```
🤖 Atlas Auto-Remediation — 2026-04-29

Captured:    47 errors (12 new fingerprints)
Auto-fixed:   3  (avg 8 min, 0 reverts)
Assist (open): 5  ← human review
Escalated:   12  (8 broker, 2 risk, 2 reconcile)
Ignored:    27  (known noise)

Spent:    $0.00 (OAuth)
LLM calls: 84
Tokens:   1.2M

Halts: 0
Reverts: 0
Reviewer rejections: 2/8 (25%)

Top fingerprints:
  fp:8a3f (data/tiingo: feed gap) — 12x
  fp:2c1b (telegram_bot: timeout) — 8x
  fp:9e44 (test fixture: stale snapshot) — 5x

Open issues: 0
```

### Per-fix notification — validation-recommended

**Only on revert or escalation** during Phase 1/2. In Phase 3, AUTO_FIX successes are silent (digest-only); reverts and escalations are immediate.

Rationale: Telegram noise reduction was just landed (commit `27c99790: P1: Telegram noise reduction — silent on healthy, ≤3/day target`). Auto-remediation must not blow that budget.

### Dashboard panel — validation-mandatory metrics

| Widget | Metric |
|---|---|
| Headline | Auto-remediation status: ENABLED / ASSIST-ONLY / HALTED |
| Sparkline | Errors captured per hour, last 24h |
| Counter | Mean time to detect / mean time to fix |
| Counter | Revert rate (last 7d / last 30d) |
| Table | Top 10 unresolved fingerprints |
| Table | All halts in last 7d with reason |
| Health | Reviewer rejection rate (warn >40%, halt >50%) |

### Weekly report — validation-recommended

Auto-generated `reports/auto-remediation-weekly-<date>.md`:
- Bug class trends (which fingerprints are recurring)
- Areas needing human attention (highest ESCALATE volume)
- Revert post-mortems
- Reviewer disagreements (where reviewer rejected and human later confirmed it was a real issue)
- Phase progression metrics (how close are we to Phase 3 for each class?)

---

## 7. Phased Rollout (Validation Lens)

### Phase 1 — Capture + Monitor only (1 week)

**Acceptance criteria** (validation must approve before Phase 2):
1. `errors` table populated with 30 days of historical data via backfill
2. Live capture from all 6 sources (logger, journald, cron, healthcheck, broker, telegram bot bridge)
3. Deterministic classifier runs on every NEW error; classifications persisted
4. Sonnet-suggested classifications for UNCLASSIFIED entries
5. Daily digest message lands on Telegram by day 3
6. Dashboard panel live by day 5
7. **Validation gate**: 100 historical errors hand-reviewed; classifier achieves ≥95% accuracy; **0 errors mistakenly classified AUTO_FIX that touched Tier 0**
8. **Validation gate**: deny-list test suite passes — manually crafted "bad" errors (e.g., synthetic broker errors) all classified ESCALATE
9. Auto-fix code path exists but is gated behind `AUTO_REMEDIATION_ENABLED=false` env, with hard-coded `if not enabled: return` at top of agent main

### Phase 2 — ASSIST mode (2 weeks minimum)

**Acceptance criteria** (validation must approve before Phase 3):
1. Agent proposes fixes as branches; humans review and merge
2. ≥30 ASSIST-mode proposals across the 2-week window
3. Per-class quality threshold: for any class to be eligible for Phase 3, ≥30 ASSIST proposals AND ≥90% merged unmodified by human AND 0 rejections for safety reasons AND 0 caused regressions
4. Reviewer agent runs on every proposal; tracked vs human verdict
5. **Validation gate**: reviewer agent agreement with human ≥85% (sanity check on adversarial reviewer)
6. **Validation gate**: zero proposed fixes touched Tier 0 paths (defense in depth: classifier + domain locks + prompt preamble)
7. Auto-revert path tested via synthetic fix on day 7 of Phase 2
8. Telegram digest cadence stable; per-fix notification flow tested
9. Phase 3 allowlist seeded ONLY from classes that hit the per-class quality threshold above

### Phase 3 — AUTO_FIX (ongoing, narrow)

Phase 3 is **not a phase, it's a class promotion process**. Each error class is promoted independently after meeting Phase 2's per-class threshold.

Initial Phase 3 candidates (validation pre-approved as low-risk pattern types):
- Stale test fixtures (data files in `tests/fixtures/` that need date refresh)
- Bare-except baseline maintenance (when a refactor legitimately removes a pre-existing bare-except, the baseline file needs an update)
- Lint violations in tests/, dashboard-ui/, docs/
- README/doc typos and broken links
- `__pycache__` cleanup
- Log rotation issues
- Frontend TypeScript type errors in `dashboard-ui/`

Each candidate must clear a **separate, written allowlist entry** in `auto_fix_allowlist.txt` with:
- Fingerprint pattern
- Maximum allowed diff size
- Required test(s) that must exist for the fix
- Reviewer agent rejection threshold (auto-promote-back-to-ASSIST if exceeded)

### Permanent ASSIST classes (validation-mandatory — never automate)

The following classes are **ASSIST-only forever**:
- All Tier 1 paths (data ingest, healthcheck logic, observability, alerting)
- Anything in `services/api/`, `services/chat_server.py`
- Anything in `monitor/` other than `monitor/__init__.py` and `monitor/strategy_health.py`
- Anything in `research/` (research outputs feed promotion → live config)
- Test files for Tier 0 modules (`test_kill_switch.py`, `test_live_executor*.py`, etc.)

The user has the option in §10 to expand or contract this list, but validation's strong recommendation is to keep this list growing, not shrinking.

### Trading-path errors → permanently ESCALATE

Per §4 NEVER list. There is no Phase 4. Humans review every change to broker/risk/kill_switch/live_executor/regime/signals/monitor-lifecycle/portfolio/overlay/strategies/plan/approve code, period.

---

## 8. Integration with Existing Systems (Validation Lens)

### Multi-team integration

**Recommendation**: New `remediation` team within the existing multi-team config (`/root/.pi/teams/config.yaml`). Specifically:

```yaml
teams:
  remediation:
    description: Autonomous error capture, triage, and bounded auto-remediation
    color: '#ff8800'  # distinct orange — visually flags remediation activity in TUI
    icon: '⚙'
    badge_letter: R
    lead:
      name: Remediation Lead
      model: claude-opus-4-7
      effort: xhigh
      system_prompt: .pi/teams/prompts/remediation-lead.md
      expertise: .pi/expertise/remediation-lead/
      skills:
        - .pi/teams/skills/zero-micromanagement.md
        - .pi/teams/skills/conversational-response.md
        - .pi/teams/skills/mental-model.md
        - .pi/teams/skills/active-listener.md
        - .pi/teams/skills/codebase-map.md
        - .pi/teams/skills/trading-domain.md
        - .pi/teams/skills/claude-oauth.md
        - .pi/teams/skills/auto-remediation-bounds.md  # NEW skill, mandatory
      domain:
        read: ['**/*']
        write:
          - tests/**
          - dashboard-ui/**
          - docs/**
          - '**/*.md'
          - bare_except_baseline.txt
          - .pi/expertise/remediation-lead/**
          # NOTHING else — the OS-layer domain is the second-to-last line of defense
    members:
      - name: Fix Author
        model: claude-opus-4-7
        # tighter domain than lead — lead can also write expertise; member only fixes
        domain:
          read: ['**/*']
          write:
            - tests/**
            - dashboard-ui/**
            - docs/**
            - '**/*.md'
            - .pi/expertise/fix-author/**
```

**Validation insists on**: the Validation Lead (this team) is the reviewer. The Validation Lead's prompt will include the adversarial review prompt from §3.

### Skill reuse

| Existing skill | Used by remediation team | Note |
|---|---|---|
| `claude-oauth.md` | YES | Mandatory — every pi/claude call needs `--system-prompt` |
| `mental-model.md` | YES | Remediation team needs persistent learning |
| `active-listener.md` | YES | Read prior fix attempts before retrying |
| `trading-domain.md` | YES | Critical — agent needs to understand what NOT to break |
| `testing-handbook.md` | YES (Fix Author) | Tests are mandatory in fixes |
| `git-conventions.md` | YES | Branch naming, commit messages |
| `python-standards.md` | YES (Fix Author) | Prevents new bare-excepts |
| **NEW**: `auto-remediation-bounds.md` | YES | Codifies the NEVER list, gate checklist, escalation rules. Validation team owns the content |

### Atlas-jobs extension extension

The atlas-jobs extension has a clean spec pattern (`approvalHint: safe / review / high_risk`) that maps perfectly to our triage:
- `safe` → AUTO_FIX-eligible (still subject to Phase 3 per-class promotion)
- `review` → ASSIST
- `high_risk` → ESCALATE (no automation, ever)

**Recommendation**: Add a new job category `remediation` to the atlas-jobs catalog with three jobs:
- `remediation_capture_backfill` (approvalHint: safe — populates `errors` table)
- `remediation_propose_fix` (approvalHint: review — emits a branch only)
- `remediation_apply_fix` (approvalHint: high_risk — actually merges; gated)

This gives ops a single CLI surface to trigger remediation operations.

### New extension vs in-atlas

**Recommendation**: In-atlas, not a new pi extension. Reasons:
1. Tight coupling to `system_log`, `errors` table, atlas test suite, atlas healthchecks
2. Atlas owns the safety-critical NEVER list — keep it co-located with the code it protects
3. Multi-team handles the agent invocation; atlas-jobs handles the trigger; we do not need a third extension
4. The audit trail lives in `data/atlas.db` already

Files to add (proposed paths, validation-approved structure):
```
remediation/
  __init__.py
  capture.py          # logger handler, journald tailer, hooks
  classifier.py       # deterministic + Sonnet-assisted triage
  agent.py           # main loop, calls multi-team Remediation Lead
  gates.py           # the 15 auto-merge gates from §4
  reviewer.py        # spawns adversarial reviewer process
  monitor.py         # 30-min post-merge watch
  revert.py          # auto-revert path
  digest.py          # daily Telegram message
  models.py          # error / action dataclasses
scripts/
  remediation_run.py            # entrypoint — called by systemd timer
  remediation_backfill.py       # historical capture (Phase 1 only)
  remediation_review_history.py # CLI to inspect agent history
  test_auto_revert.sh           # monthly synthetic-revert test
data/
  AUTO_REMEDIATION_HALT          # halt sentinel (initially missing = enabled)
  remediation_digests/<date>.json
```

### Hookup details — validation review

| Source | How to hook | Validation note |
|---|---|---|
| Python logger | Add `ErrorsTableHandler` in `utils/logging_config.py` next to `TelegramErrorCollector`. Same atexit-flush model + immediate-write for ERROR+ | Already centralised — single point of integration |
| journald | New systemd timer `atlas-error-tailer.service` running `journalctl -f -u 'atlas-*' -o json` and feeding rows | Must filter dashboard ConnectionResetError noise |
| Cron exit | Modify `pi-cron.sh` to capture stderr + exit code → `python3 scripts/remediation_run.py --capture-cron $mode $exit_code` | Wraps existing cron logic, no other change |
| Healthcheck | Each healthcheck script writes a `errors` row on FAIL transition. Add a single-line call `from remediation.capture import capture_healthcheck_failure` | Already have state files; need to emit on transition |
| Broker | `brokers/live_executor.py` already calls `_health_log("error", ...)`. Add a single-line emission to `errors` from there. **Read-only consumption** — broker errors always classified ESCALATE | Captures, doesn't act |
| Telegram bot | Bot already has its own error handling. Bridge via journald tailer; do not write back into errors table from inside the bot itself | Avoids loop |

### Systemd unit (validation-approved)

```ini
# /root/atlas/systemd/atlas-error-remediation.service
[Unit]
Description=Atlas autonomous error remediation
After=atlas-dashboard.service
ConditionPathExists=!/root/atlas/data/AUTO_REMEDIATION_HALT
ConditionPathExists=!/root/atlas/data/HALT
ConditionPathExists=!/root/atlas/.live_halt

[Service]
Type=oneshot
WorkingDirectory=/root/atlas
Environment="AUTO_REMEDIATION_ENABLED=false"  # Phase 1 default; flip to true for Phase 2+
Environment="TZ=Australia/Brisbane"
EnvironmentFile=-/root/atlas/.env
ExecStart=/usr/bin/python3 /root/atlas/scripts/remediation_run.py --once
TimeoutStartSec=900
StandardOutput=append:/root/atlas/logs/remediation.log
StandardError=append:/root/atlas/logs/remediation.log

# /root/atlas/systemd/atlas-error-remediation.timer
[Unit]
Description=Run atlas-error-remediation every 5 min off-hours, every 15 min weekends

[Timer]
OnCalendar=Mon..Fri 00,05,10,...,55 16..23,00..09 *-*-*  # off-hours weekdays
OnCalendar=Sat,Sun 00,15,30,45 *-*-* *:*  # weekends
Persistent=false  # don't catch up missed runs

[Install]
WantedBy=timers.target
```

The `ConditionPathExists=!` trio is critical: the service will not start if any halt file is present. This is OS-level enforcement of the kill switch.

---

## 9. Failure Modes + Mitigation (Validation Lens)

### What if the agent makes things worse?

**Mitigation**: Auto-revert in <30 seconds via tagged commit + branch. Halt agent for 24h on any revert. Alert immediately. Validation team reviews the next day before re-enabling.

**Residual risk**: Window between merge and detection. Worst case: bad fix lives ~30 min before regression detected. Acceptable if Tier 0 is untouchable, since Tier 0 is the only thing that can drain the account.

**Tail risk**: A revert that itself is buggy (rare). Mitigation: revert is `git revert --no-edit <sha>` — git operation, not LLM-generated. Deterministic.

### What if the agent fixes the same bug 50 times?

**Mitigation**: Loop detector — same fingerprint twice in 24h → permanent ESCALATE for that fingerprint. Counter persists in `errors.remediation_attempts`.

**Residual risk**: Fingerprint algorithm misses a slight variation, treats them as different bugs. Mitigation: weekly review of "near-fingerprint" errors (top frames match within 1 line) — manual collapse if found.

### What if the fix has hidden side effects?

**Mitigation**: 
1. Reviewer agent (separate adversarial process) flags
2. 30-min post-merge monitor
3. Healthcheck regression detection
4. Live trade journal monitoring (during market hours)
5. Phase 3 starts narrow — frontend, tests, docs only — paths that have minimal side-effect surface

**Residual risk**: Side effect that doesn't trigger any of the above (e.g., an off-by-one in a metric calculation that drifts over weeks). Mitigation: weekly trend analysis dashboard surfaces gradual divergences.

### What if the agent misdiagnoses?

**Mitigation**: 
1. Reviewer agent must validate the diagnosis, not just the diff
2. Reviewer asks "does the fix address the root cause, or just suppress the symptom?" explicitly
3. Phase 2 (ASSIST) builds confidence over 30 cycles before AUTO_FIX is allowed for any class
4. Regression test must exist (gate #2) — forces the diagnosis to be specific enough to write a test

**Residual risk**: Agent-written tests that pass but don't actually test the bug. Mitigation: weekly spot-check of 10% of regression tests by human.

### What if the LLM hallucinates valid-looking code?

**Mitigation**:
1. Full pytest suite must pass (catches most hallucinations)
2. mypy clean (catches type-level hallucinations)
3. Reviewer agent (different process, adversarial) — catches semantic hallucinations
4. Diff size limit 30 lines — limits the surface
5. NEVER list — limits where hallucinations can land
6. The required regression test would fail on hallucinated code that doesn't actually fix the bug

**Residual risk**: A hallucination that passes tests, mypy, and reviewer. Mitigation: revert rate is the leading indicator. If revert rate >5% in any week, halt and root-cause.

### What if cost runs away?

**Mitigation**:
1. OAuth via `pi --system-prompt` — $0 marginal cost
2. Rate limits at LLM-call level (30/hour Phase 3)
3. Hard timeout per fix attempt (15 min)
4. `lint_pi_system_prompt.py` already enforces the OAuth pattern
5. Daily token tracking; alert if >2x baseline

**Residual risk**: OAuth subscription window exhaustion (5-hour rolling). Mitigation: agent halts on `out of extra usage` errors and waits 6h.

### What if the error stream itself fails?

**Meta-monitor required** — validation-mandatory:

```
heartbeat: errors_table_writes_per_hour
  expected: > 0 every hour
  alert: 24h with no rows → CRITICAL Telegram

heartbeat: capture_components_alive
  python_logger_handler: last write
  journald_tailer: pid alive
  cron_capture: last cron exit captured
  healthcheck_capture: last state file delta captured
  alert if any down for >2h

heartbeat: classifier_running
  expected: every NEW row gets classified within 10 min
  alert: backlog >100 rows → CRITICAL
```

This meta-monitor is itself a healthcheck, captured by the healthcheck system, captured by the error stream — closing the loop.

### What if a halt is missed (e.g., HALT file race)?

**Mitigation**: Three independent halt checks at three points in every cycle:
1. systemd `ConditionPathExists=!/root/atlas/data/HALT` (won't start)
2. Python entrypoint checks all halt files at start (refuses to run)
3. Each gate (§4) re-checks halt before commit

Belt and braces and a third belt.

### What if a fix during ASSIST mode causes the human-review queue to grow unbounded?

**Mitigation**: 
1. 7-day TTL on ASSIST proposals — un-reviewed branches auto-deleted
2. Daily digest surfaces queue size; >5 unreviewed = warning, >10 = halt new proposals
3. Telegram quick-action buttons (Approve / Reject / Defer) on each proposal

---

## 10. Recommended Next 3 Actions (Validation Lens)

These are the validation team's top 3 to ship for Phase 1 — capture-only, no fixes.

### Action 1 — Create `errors` table + capture from `system_log`/Python logger (1 day)

**Files**: 
- `db/migrations/00XX_errors_table.sql` — DDL above
- `remediation/__init__.py`, `remediation/capture.py`, `remediation/models.py`
- Modify `utils/logging_config.py` — add `ErrorsTableHandler` next to `TelegramErrorCollector`. Same per-process atexit batched-flush, plus immediate write for ERROR+ records.
- `tests/test_remediation_capture.py` — exhaustive: handler installs, fingerprint stability, dedup correctness, market_hours flag, halt_active flag, all source types.

**Validation gate**: 
- 100 synthetic errors generated across all severity levels and sources; every one results in correct row in `errors`
- Fingerprint stability test: same error fired twice = same fingerprint, occurrence_count=2
- No dedup collision in 1000 randomized errors

**Effort**: 1 day (8h) for backend dev, 4h validation review.

### Action 2 — Backfill + deterministic classifier (1 day)

**Files**: 
- `scripts/remediation_backfill.py` — pulls from `system_log`, `logs/atlas.log*` (rotated), `journalctl --since='30 days'`. Idempotent.
- `remediation/classifier.py` — the deterministic decision tree from §2. Pure Python, no LLM. 
- `remediation/never_list.py` — programmatic NEVER list (file globs, function names, error patterns) loaded from a YAML config. Single source of truth.
- `config/auto_remediation_never_list.yaml` — the actual list (file globs, function names, message patterns)
- `safety_critical_functions.txt` — committed list from §4
- `tests/test_remediation_classifier.py` — 50+ test cases covering Tier 0 / 1 / 2 paths, market-hours, halt-active, recent-human-commit logic

**Validation gate**:
- 100 historical errors hand-reviewed by Validation Lead; classifier output compared; ≥95% agreement
- 30 hand-crafted "evil" errors (synthetic broker/risk/kill_switch errors with various disguises) — every one classified ESCALATE
- Recent-human-commit detection works (mock git log)

**Effort**: 1 day backend, 1 day validation review (the 100-error audit is the long pole).

### Action 3 — Daily digest + dashboard panel + meta-monitor (1 day)

**Files**:
- `remediation/digest.py` — generates the digest from `errors` + `error_remediation_actions`
- `scripts/remediation_digest_cron.py` — runs daily at 08:00 UTC, sends Telegram
- `services/api/remediation.py` — new API endpoint `/api/remediation/status` for dashboard
- `dashboard-ui/src/components/RemediationPanel.tsx` — new dashboard widget (counts, sparkline, top fingerprints)
- `scripts/healthcheck_remediation.py` — meta-monitor: errors_table writes per hour, capture pids alive, classifier backlog
- `tests/test_remediation_digest.py`, `tests/test_remediation_dashboard_api.py`

**Validation gate**:
- Digest renders correctly with 0, 1, 100 errors
- Dashboard panel renders without errors when remediation is HALTED, ASSIST-ONLY, ENABLED
- Meta-monitor alerts when capture pid is killed (synthetic test)

**Effort**: 1 day backend (digest + API + meta), 0.5 day frontend (panel), 0.5 day validation.

### Total Phase 1 effort estimate

3-4 days engineering + 1.5 days validation review + ~1 day for the 100-error historical audit = **~1 calendar week** with one engineer + validation lead overlapping.

### What is **not** in Phase 1

- Sonnet-assisted classification — wait for Phase 1 data first
- Reviewer agent
- Fix-author agent
- Auto-revert path
- Allowlist for AUTO_FIX classes
- New `remediation` team in multi-team config

All of those land in Phase 2.

---

## Appendix A — Validation-canonical NEVER list (cut-and-paste)

```yaml
# config/auto_remediation_never_list.yaml
file_globs:
  - "brokers/**"
  - "risk/**"
  - "regime/**"
  - "signals/**"
  - "strategies/**"
  - "monitor/lifecycle.py"
  - "monitor/evaluator.py"
  - "portfolio/**"
  - "overlay/**"
  - "core/reconcile.py"
  - "plans/**"
  - "config/active/**"
  - "config/active_config.json"
  - "data/atlas.db*"
  - "data/HALT"
  - ".live_halt"
  - "data/state/**"
  - "brokers/state/**"
  - "scripts/eod_settlement.py"
  - "scripts/intraday_monitor.py"
  - "scripts/director_cron.py"
  - "scripts/execute_approved.py"
  - "scripts/cli.py"
  - "services/telegram_bot.py"
  - "db/migrations/**"
  - "*.sql"
  - "tests/test_kill_switch.py"
  - "tests/test_live_executor*.py"
  - "tests/test_broker*.py"
  - "tests/test_risk*.py"
  - "tests/test_reconcile*.py"
  - "tests/test_overlay*.py"
  - "tests/test_regime*.py"
  - "tests/test_plan*.py"
  - "tests/test_signals*.py"
  - "tests/test_settlement*.py"
  - "tests/test_monitor*.py"
  - "tests/conftest.py"
  - "/etc/systemd/system/atlas-*"

error_class_patterns:
  - "BrokerError"
  - "OrderRejected"
  - "PreflightError"
  - "InsufficientFunds"
  - "PDTViolation"
  - "PositionMismatch"
  - "ReconcileDriftError"
  - "RiskBudgetExceeded"
  - "DrawdownBreach"
  - "OverlayError"
  - "RegimeError"
  - "MemoryError"
  - "OperationalError"

message_patterns:
  - "broker"
  - "order"
  - "fill"
  - "position"
  - "balance"
  - "drawdown"
  - "PDT"
  - "pattern day trader"
  - "HALT"
  - "halt"
  - "kill switch"
  - "reconcile"
  - "drift"
  - "mismatch"
  - "risk"
  - "VAR"
  - "ruin"
  - "sector cap"
  - "gross exposure"
  - "overlay"
  - "regime"
  - "Alpaca"
  - "40310"
  - "credential"
  - "secret"
  - "auth"
  - "token"
  - "No space left"
  - "database is locked"
  - "database is malformed"

contextual_blocks:
  - market_hours: ESCALATE_DEFERRED
  - halt_active: IGNORE_PENDING_CLEAR
  - within_30min_of_eod: ESCALATE
  - within_30min_of_premarket: ESCALATE
  - file_modified_by_human_24h: ASSIST
  - traceback_contains_tier_0_path: ESCALATE
  - error_during_promotion_window: ESCALATE
  - diff_would_exceed_30_lines: ESCALATE
  - diff_would_modify_multiple_files: ESCALATE
  - same_fingerprint_attempted_2x_24h: ESCALATE_PERMANENT
```

---

## Appendix B — Defense in Depth Diagram

```
                  ERROR CAPTURED
                        │
                        ▼
       ┌──────────────────────────────────┐
       │  L1: Trading kill switch checks   │  → if HALT engaged, capture-only
       └──────────────────────────────────┘
                        │
                        ▼
       ┌──────────────────────────────────┐
       │  L2: Deterministic classifier     │  → ESCALATE on any tier 0/1 hit
       │      (NEVER list as YAML config)  │
       └──────────────────────────────────┘
                        │
                AUTO_FIX │ AUTO_FIX (allowlist)
                        ▼
       ┌──────────────────────────────────┐
       │  L3: Reproduce gate               │  → ESCALATE if not reproducible
       └──────────────────────────────────┘
                        │
                        ▼
       ┌──────────────────────────────────┐
       │  L4: Diagnose (Opus 4.7)          │  → ESCALATE if low confidence
       │      with NEVER list in prompt    │
       └──────────────────────────────────┘
                        │
                        ▼
       ┌──────────────────────────────────┐
       │  L5: Fix in worktree (Opus 4.7)   │  → ESCALATE on >30 lines
       │      with NEVER list in prompt    │
       └──────────────────────────────────┘
                        │
                        ▼
       ┌──────────────────────────────────┐
       │  L6: Multi-team domain check (OS) │  → REFUSE write to NEVER paths
       │      (filesystem-level enforce)   │     even if prompt is wrong
       └──────────────────────────────────┘
                        │
                        ▼
       ┌──────────────────────────────────┐
       │  L7: 15 auto-merge gates           │  → BLOCK on any failure
       │      (tests, lint, mypy, etc.)    │
       └──────────────────────────────────┘
                        │
                        ▼
       ┌──────────────────────────────────┐
       │  L8: Reviewer agent (fresh proc.)  │  → REJECT (default) unless
       │      adversarial Opus 4.7         │     all conditions met
       └──────────────────────────────────┘
                        │
                        ▼
       ┌──────────────────────────────────┐
       │  L9: Pre-commit hooks              │  → existing repo hooks
       └──────────────────────────────────┘
                        │
                        ▼
                     COMMIT
                        │
                        ▼
       ┌──────────────────────────────────┐
       │  L10: 30-min post-merge monitor   │  → AUTO-REVERT on regression
       │       same fingerprint? healthcheck?│
       └──────────────────────────────────┘
                        │
                        ▼
                    VERIFIED
```

10 layers. Any one can stop a bad fix. Tier 0 paths are stopped by L1, L2, L4, L5, L6 — five independent layers. That redundancy is intentional.

---

## Decision Points the User Must Answer Before Phase 2 Ships

### Q1. Maximum auto-fix budget per day?
**Validation recommendation**: 5 commits/day, 2 commits/file/week, 30 LLM calls/hour, 15 min compute/fix. $0/day (OAuth). 1 revert/day before halt. These can be raised in Phase 4 once revert rate is below 1%/quarter.

### Q2. Which error classes are auto-fix-eligible from day 1 of Phase 3?
**Validation recommendation** — promote to AUTO_FIX only after a class hits the per-class quality threshold (≥30 ASSIST cycles, ≥90% merged unmodified, 0 safety rejections, 0 regressions). The initial Phase 3 starting set, validation pre-vetted, is:
1. Stale test fixtures in `tests/fixtures/`
2. `bare_except_baseline.txt` updates via `lint_bare_except.py --update-baseline` only
3. README/docs typo fixes (`**/*.md`)
4. `__pycache__` cleanup (existing `healthz_autofix.sh` already does this)
5. Frontend lint/format fixes in `dashboard-ui/`

Anything else: ASSIST until promoted.

### Q3. Permanent ASSIST-forever classes (no Phase 4)?
**Validation strong recommendation**: YES, permanently ASSIST for everything in Tier 1 (data ingest, healthcheck logic, observability/alerting, services/api/, monitor/, research/). And permanently ESCALATE for Tier 0 (no automation, ever). The user can shrink Tier 1 only by promoting individual subdirectories after a year of incident-free operation, which validation will not pre-approve.

### Q4. Telegram cadence — digest only, or per-fix notification?
**Validation recommendation**: digest only for AUTO_FIX successes. Per-fix notification for: 
- Every revert (CRITICAL)
- Every escalation in Tier 0 (CRITICAL)
- Every halt event (CRITICAL)
- Every reviewer rejection rate >40% in last 10 (WARNING)
- Every ASSIST proposal (HIGH — for human review queue)

This matches the recently-shipped P1 Telegram noise reduction (≤3/day target on healthy days). Per-AUTO_FIX-success messages would blow the budget; per-failure messages are non-negotiable.

### Q5 (validation-added). Reviewer agent rejection threshold?
**Validation recommendation**: Reviewer rejection of >50% over last 10 fixes → halt agent for 24h and force human review of classifier+fix-author. Reviewer disagreement with human (during Phase 2 ASSIST) of >25% → halt agent and re-tune adversarial prompt.

### Q6 (validation-added). Auto-revert authority?
**Validation recommendation**: Auto-revert is allowed and silent (no human approval). The cost of a delayed revert (waiting for human ack) is higher than the cost of a few false-positive reverts. The auto-revert posts to Telegram immediately.

### Q7 (validation-added). What happens during a research-promotion window?
**Validation recommendation**: Auto-remediation pauses entirely during the 1h window when `research/promoter.py` is staging a new strategy → live config. Promotions touch live trading config; we do not want simultaneous code changes from two automated paths.

---

## Validation Verdict on the Overall Mandate

**PASS WITH NOTES — Ship Phase 1 as proposed; Phase 2 only after the validation gates above; never ship a Phase 4 (full auto-fix on trading-path code).**

The proposed system is sound IF and ONLY IF:
1. The NEVER list is enforced at three independent layers (classifier, prompt, OS domain)
2. Tier 1 stays ASSIST-only forever
3. Tier 0 stays ESCALATE-only forever
4. Reviewer agent is a separate process with adversarial prompt
5. Regression tests are required for every fix (gate #2)
6. Auto-revert is fast (<30s), tested monthly, and silent
7. The kill-switch chain (L1–L8) is intact

The blast radius of getting this wrong is the entire $5,189 account. The blast radius of being too cautious is some manual code review. Validation's bias is unambiguously toward "too cautious".

— **Validation Lead**, 2026-04-29