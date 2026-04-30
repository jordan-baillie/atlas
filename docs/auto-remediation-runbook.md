# Atlas Auto-Error-Remediation Runbook

> **Model selection note (2026-04-30):** Both Fix Worker and Reviewer now run
> on Claude Opus 4.7. Earlier design used Sonnet 4.6 for the Reviewer to
> achieve cross-model decorrelation. With both on Opus, decorrelation is
> enforced via: (1) Reviewer runs in a separate `pi -p` subprocess with
> `--no-session`, (2) Reviewer prompt is hardened to call out same-model risk
> and require concrete failure paths, (3) default verdict remains REJECT with
> 8 explicit APPROVE conditions, (4) confidence threshold ≥0.75 to APPROVE.

**Audience:** Atlas operator (the human who runs/owns the trading system).  
**Purpose:** Day-to-day operations and incident response for the auto-remediation pipeline.  
**Date:** 2026-04-29  
**Phase Status:** Phase 2 ENABLED (ASSIST dispatch active). Phase 3 code-shipped but feature-gated behind `phase_3_enabled: false` until 14d clean Phase 2 data is accumulated.

---

## 1. System Overview

### What it is

The auto-error-remediation pipeline captures every ERROR/CRITICAL log line emitted by Atlas services, fingerprints and deduplicates them, classifies each into one of four tiers, and — when safe — proposes or applies a fix autonomously. It is a meta-system operating **on top of** Atlas, never inside the trading path.

### What it does

```
logs/journald/healthchecks
        │
        ▼ (Phase 0 — Capture)
  errors table (SQLite, atlas.db)
        │
        ▼ (Phase 1 — Triage / Classify)
  classification: AUTO_FIX | ASSIST | ESCALATE | IGNORE
        │
        ├── IGNORE     ──► nothing
        ├── ESCALATE   ──► Telegram alert to operator
        ├── ASSIST     ──► (Phase 2) Fix Worker → Reviewer → 15 gates → auto-fix-staging branch
        └── AUTO_FIX   ──► (Phase 3) same as ASSIST + auto-promote staging → main after 30-min monitor
        │
        ▼ (Phase 2 — Fix Worker, Reviewer, Merger)
  fix_attempts + fix_audit_log (SQLite)
  auto-fix-staging branch (git, local)
        │
        ▼ (Phase 3 — Auto Merger + Graduation)
  main branch (auto-merge on 15/15 gates + 30-min clean window)
  graduation engine promotes/demotes error classes
        │
        ▼ (Phase 2+3 — 30-min monitor)
  revert if fingerprint recurs OR healthcheck regresses
```

### What it does NOT touch

The deny lists in `config/auto_fix_deny.yaml` and `config/auto_remediation.yaml#never_fix` codify the absolute boundaries. In summary:

- **Never touches**: `brokers/`, `risk/`, `regime/`, `signals/`, `strategies/`, `portfolio/`, `overlay/`, `monitor/lifecycle.py`, `monitor/evaluator.py`, `core/reconcile.py`, kill-switch code, live-executor code, plan/approve code, halt scripts, any config in `config/active/**`, all DB schema/migration files, `data/atlas.db*`, all state files, `scripts/pi-cron.sh`, `services/telegram_bot.py`, `systemd/atlas-*.{service,timer}`, all `.sql` files, all secrets/auth files, and all trading-path test files.
- **Scope**: Atlas only (`config/auto_remediation.yaml#defaults_applied.scope: atlas_only`). Cross-project writes never happen.
- **Causal chain rule**: Even if a fix touches a "safe" file, if the root cause traces through trading code, the error is ESCALATE regardless.

### Phase status reference

| Phase | Name | Status |
|---|---|---|
| Phase 0 | Capture + Triage | ✅ Shipped, ACTIVE |
| Phase 1 | Classifier + Audit log | ✅ Shipped, ACTIVE |
| Phase 2 | Fix Worker + Reviewer + Merger (ASSIST) | ✅ Shipped, ACTIVE |
| Phase 3 | Auto Merger + Graduation (AUTO_FIX) | ⏳ Code shipped, feature-gated (`phase_3_enabled: false`) |

**Systemd timer**: `atlas-error-remediation.timer` fires every 5 min (RTH) / 15 min (off-hours). See §8.1 for installation.

---

## 2. Daily Operations

### 2.1 Health checks

**Endpoint: `/api/error_remediation/health`**

```bash
curl -s -u atlas:$(jq -r .dashboard_password /root/.atlas-secrets.json) \
  https://atlas.local/api/error_remediation/health | python3 -m json.tool
```

Key fields to watch:

| Field | Healthy | Warning | Action |
|---|---|---|---|
| `halt_active` | `false` | `true` | See §3.2 resume procedure |
| `capture_alive.errors_last_24h` | >0 weekdays | 0 for >2h during market hours | See §10.2 |
| `classifier_backlog` | <20 | >100 | See below |
| `revert_rate_pct` | <15% | 15–25% = ALERT | >25% = auto-halt, see §10.1 |
| `phase` | 2 | — | 3 = auto-merge active, verify deliberately |

**Endpoint: `/api/error_remediation/summary`**

```bash
curl -s -u atlas:$(jq -r .dashboard_password /root/.atlas-secrets.json) \
  https://atlas.local/api/error_remediation/summary | python3 -m json.tool
```

Key fields: `errors_last_24h`, `by_classification`, `by_status`, `attempts_total`, `attempts_by_status`.

**When `errors_last_24h = 0` (capture silent)**

0 errors on a quiet weekend is expected. If this reads 0 on a US trading day with active positions:

```bash
# Step 1 — is the timer running?
systemctl status atlas-error-remediation.timer

# Step 2 — is the service completing cleanly?
journalctl -u atlas-error-remediation.service -n 50 --no-pager

# Step 3 — direct DB query
sqlite3 /root/atlas/data/atlas.db \
  "SELECT COUNT(*) FROM errors WHERE last_seen_ts > datetime('now','-1 hour');"

# Step 4 — meta-monitor
python3 /root/atlas/scripts/healthz_error_remediation.py --json
```

**When classifier backlog > 100**

```bash
# How many are unclassified?
sqlite3 /root/atlas/data/atlas.db \
  "SELECT COUNT(*) FROM errors WHERE classification='UNCLASSIFIED';"

# Force a manual classification sweep (dry-run first):
cd /root/atlas && python3 core/error_monitor.py --once --batch-size 200 --dry-run

# Apply if output looks sane:
cd /root/atlas && python3 core/error_monitor.py --once --batch-size 200
```

### 2.2 Reading the dashboard panel

**URL**: `https://atlas.local` (same dashboard as trading UI; auth: HTTP Basic)

The error remediation panel shows:

| Widget | What it means | Healthy |
|---|---|---|
| **Error volume (24h/7d)** | Total distinct fingerprints captured | Downward or flat trend |
| **Classification breakdown** | Pie: IGNORE / ASSIST / ESCALATE / AUTO_FIX | IGNORE dominant (>94%) |
| **Halt state** | Green = running, Red = halted | Green |
| **Fix attempts** | Count of ASSIST/AUTO_FIX proposals in last 7d | Low; all `merged` or `blocked` |
| **Reverted** | Count of auto-reverts in last 7d | 0 |
| **Staging branch** | Commits on `auto-fix-staging` awaiting human review | Reviewed weekly |
| **Top fingerprints** | Most-occurring unresolved errors | If >50 occurrences, investigate |

**State interpretation**:

- 🟢 **Healthy**: `halt_active=false`, `by_classification.IGNORE > 94%`, `reverted = 0`
- 🟡 **Warning**: `revert_rate_pct >= 15%` OR `classifier_backlog > 100` OR `phase_3_enabled=true` and you didn't enable it
- 🔴 **Halted**: `halt_active=true` — check L2 file, L4 drawdown, L5 healthcheck cascade

### 2.3 Reading the audit log

The `fix_audit_log` table is the forensic trail. It is **append-only** (enforced by DB triggers — `fix_audit_log_no_update` + `fix_audit_log_no_delete`).

**50 most recent entries:**

```sql
SELECT phase, actor, decision, ts
FROM fix_audit_log
ORDER BY ts DESC LIMIT 50;
```

**Filter by attempt_id:**

```sql
SELECT phase, actor, decision, reasoning, ts
FROM fix_audit_log
WHERE attempt_id = ?
ORDER BY ts ASC;
```

**Filter by error_id:**

```sql
SELECT phase, actor, decision, ts
FROM fix_audit_log
WHERE error_id = ?
ORDER BY ts ASC;
```

The append-only invariant means you can always trust the log for incident reconstruction. See §11.1 for quarterly verification.

---

## 3. Halt + Resume Procedures

### 3.1 How to halt remediation immediately

Three methods, in order of escalation:

**Method A — File-level halt (L2, recommended for most incidents)**

```bash
# Instant halt — works even if Python process is mid-cycle
touch /root/atlas/data/AUTO_REMEDIATION_HALT

# Optional: add a reason
echo "Manual halt: investigating revert spike 2026-04-30" \
  > /root/atlas/data/AUTO_REMEDIATION_HALT
```

Effect: every new cycle of `error_monitor.py`, `fix_worker.py`, `merger.py` checks this file at startup and exits immediately. In-progress LLM calls finish but no new work starts.

**Method B — Stop the systemd timer (L8, use when Method A is insufficient)**

```bash
# Stop timer from firing new cycles
sudo systemctl stop atlas-error-remediation.timer
sudo systemctl status atlas-error-remediation.timer  # confirm stopped
```

Effect: prevents future cycles from starting at the OS level. Does NOT interrupt any currently-running service instance.

**Method C — Environment variable (L1, only affects newly-started processes)**

```bash
export ATLAS_AUTO_REMEDIATION_DISABLED=1
```

This is the weakest method — it only affects processes started in the current shell session. Use it in combination with Method A for a belt-and-suspenders halt during a debugging session.

**Verify halt is active:**

```bash
cd /root/atlas && python3 -c "
from core.remediation_kill_switch import check_all_layers
r = check_all_layers()
print('BLOCKED:', r) if r else print('CLEAR — not halted')
"
```

### 3.2 How to resume

**Standard resume (removes L2 file):**

```bash
# Verify you understand WHY it was halted first:
cat /root/atlas/data/AUTO_REMEDIATION_HALT

# Remove the file:
rm /root/atlas/data/AUTO_REMEDIATION_HALT

# Or via Python helper:
cd /root/atlas && python3 -c "from core.remediation_kill_switch import resume; print(resume())"
# → True (removed) | False (wasn't set)
```

**Re-enable timer (if it was stopped via Method B):**

```bash
sudo systemctl start atlas-error-remediation.timer
sudo systemctl status atlas-error-remediation.timer
```

**Verify resume:**

```bash
cd /root/atlas && python3 -c "
from core.remediation_kill_switch import check_all_layers
r = check_all_layers()
print('BLOCKED:', r) if r else print('CLEAR — all layers OK')
"
```

**Important**: `resume()` only clears `data/AUTO_REMEDIATION_HALT`. It does NOT clear `data/HALT` (trading kill switch — L3). If L3 is tripping, use `halt_trading.py --resume` (see the trading runbook). Auto-remediation auto-halts whenever the trading system is halted.

### 3.3 The 8 kill-switch layers (full list)

| Layer | Check | Tripped by | Action when tripped |
|---|---|---|---|
| **L1** | `env ATLAS_AUTO_REMEDIATION_DISABLED=1` | Operator (shell export) | Exit immediately, no audit entry |
| **L2** | File `data/AUTO_REMEDIATION_HALT` | Operator, budget guard, monitor auto-halt, L7 Telegram command | Exit with reason logged |
| **L3** | File `data/HALT` or `.live_halt` | Trading kill switch (`halt_trading.py`) | Exit — remediation pauses when trading is halted |
| **L4** | Daily drawdown >5% on sp500 book (`portfolio_snapshots`) | DB read on each cycle | Exit, Telegram alert |
| **L5** | ≥3 distinct CRITICAL healthcheck fingerprints in 24h | DB read on each cycle | Exit, Telegram alert |
| **L6** | Reviewer rejection rate >50% over last 10 reviews | DB read on each cycle | Exit, Telegram alert |
| **L7** | Telegram `/halt-remediation` command | Operator via bot | Sets L2 file (checked via L2) |
| **L8** | `systemd` `ConditionPathExists=!` guards on L1/L2/L3 | OS-level, fires before Python | Timer never launches service if HALT files present |

All layers are checked top-down (L1→L8). The first tripped layer wins. Layers 1–3 are checked in-memory / file-stat (cheap). Layers 4–6 require DB reads. L7 routes through L2. L8 is out-of-band (systemd).

**Fail-open policy**: If layers L4/L5/L6 crash (e.g., DB locked), they return None (proceed). L1–L3 never fail-open.

### 3.4 What auto-halts the system

Each of these writes `data/AUTO_REMEDIATION_HALT` and sends an immediate Telegram alert:

| Trigger | Source | Config |
|---|---|---|
| 2 reverts in any 24h window | `core/budget.py` `enforce_budget()` | `budget.reverts_to_halt: 2` |
| 25% revert rate in 24h (min 4 merges) | `core/budget.py` | `budget.revert_rate_halt_pct: 25` |
| 10 commits in 24h (commit cap) | `core/budget.py` | `budget.max_commits_per_day: 10` |
| Daily drawdown >5% | `core/remediation_kill_switch.py` L4 | `DRAWDOWN_HALT_PCT = 5.0` |
| 3+ distinct CRITICAL healthchecks in 24h | kill switch L5 | `HEALTHCHECK_FAIL_THRESHOLD = 3` |
| Reviewer rejection rate >50% last 10 | kill switch L6 | `REVIEWER_REJECTION_HALT_PCT = 50.0` |
| Same-fingerprint recurrence in 30-min monitor | `core/merger.py` monitor | `verify.monitor_window_minutes: 30` |
| Healthcheck regression in 30-min monitor | `core/merger.py` monitor | same window |

The 15% revert rate threshold (`budget.revert_rate_alert_pct`) sends a Telegram WARNING only (no halt).

---

## 4. Reverting a Fix

### 4.1 Manual revert (operator-driven)

Use this when you spot a bad fix on `auto-fix-staging` or in `main`.

```bash
# 1. Find the commit
git -C /root/atlas log --oneline | grep -i "auto-fix\|remediation"

# 2. Revert it
cd /root/atlas && git revert --no-edit <sha>

# 3. Update DB status
sqlite3 /root/atlas/data/atlas.db "
UPDATE fix_attempts
SET status='reverted',
    revert_commit_sha='$(git rev-parse HEAD)',
    revert_reason='operator manual revert',
    reverted_ts=datetime('now')
WHERE fix_commit_sha='<orig_sha>';
"

# 4. Notify
cd /root/atlas && python3 -c "
from utils.telegram import send_message
send_message('Reverted auto-fix <orig_sha>: <reason>')
"

# 5. Consider halting if the fix was dangerous
touch /root/atlas/data/AUTO_REMEDIATION_HALT
echo "Halted after manual revert of <sha> — investigate before resuming" \
  > /root/atlas/data/AUTO_REMEDIATION_HALT
```

### 4.2 Auto-revert (system-driven during 30-min monitor)

After `merger.py` fast-forwards `auto-fix-staging`, the monitor polls for 30 minutes. Auto-revert fires when:

- Same fingerprint recurs during the monitor window, OR
- Any healthcheck signal regresses (CRITICAL-level in `errors` from `source='healthcheck'`)

Auto-revert:
1. Creates a revert commit on `auto-fix-staging`
2. Writes `data/AUTO_REMEDIATION_HALT` (halts for 24h cooldown)
3. Updates `fix_attempts.status = 'reverted'`, writes `revert_reason='monitor'`, sets `reverted_ts`
4. Appends to `fix_audit_log`: `phase='revert', actor='monitor'`
5. Sends immediate Telegram alert

Extended monitor window (2h instead of 30 min) applies to fixes touching `services/**`, `scripts/**`, `data/**`, `monitor/**`.

### 4.3 Inspect recent reverts

```sql
SELECT id, fingerprint, fix_commit_sha, revert_commit_sha,
       revert_reason, reverted_ts, notes
FROM fix_attempts
WHERE status = 'reverted'
ORDER BY reverted_ts DESC LIMIT 10;
```

To understand why an auto-revert fired:

```sql
-- Full timeline for the attempt
SELECT phase, actor, decision, reasoning, ts
FROM fix_audit_log
WHERE attempt_id = <id>
ORDER BY ts ASC;
```

---

## 5. Phase 3 Activation

**This section must be read end-to-end before enabling Phase 3. Phase 3 permits autonomous merges to `main` without human review.**

### 5.1 Pre-conditions for activating Phase 3 AUTO_FIX

All of the following must be true before you flip the switch:

- [ ] **≥14 calendar days** of Phase 2 ASSIST data (`graduation.assist_to_auto_fix.days_of_clean_assist: 14`)
- [ ] **≥5 human-merged ASSIST fixes per class** on the Day-1 whitelist (`graduation.assist_to_auto_fix.min_merged_assist_fixes: 5`)
- [ ] **0 scope-guard violations** on whitelist classes in the 14-day window
- [ ] **0 reverts** on Phase 2 fixes during the 14-day window
- [ ] **Reviewer rejection rate stable below 40%** over rolling 7d (`review.rejection_rate_alert_pct: 40`)
- [ ] **Operator manual review** of `/api/error_remediation/attempts` for all 14 days — spot-check at least 10 merged proposals

The Day-1 whitelist classes (from `config/auto_remediation.yaml#day1_auto_fix_whitelist`):

| Class | Scope |
|---|---|
| `test_import_error` | Import errors in `tests/` |
| `stale_fixture_datetime` | Stale datetime fixtures in tests |
| `lint_non_trading_files` | Lint fixes in `tests/`, `docs/`, `dashboard-ui/`, `scripts/healthz*` |
| `markdown_typos` | Typo fixes in `*.md` files |
| `dashboard_react_build_errors` | React build errors in `dashboard-ui/` |
| `healthz_section_logic` | Non-trading paths in `scripts/healthz*` only |

> **Note**: `config/auto_fix_classes.yaml` does not yet exist. The graduation engine creates it on first run. The whitelist is currently embedded in `config/auto_remediation.yaml#day1_auto_fix_whitelist`.

### 5.2 The flip procedure

```bash
# Step 1 — dry-run the graduation engine, review proposed promotions
cd /root/atlas && python3 scripts/run_graduation_engine.py --dry-run
# (script ships with Phase 3 parallel worker — not yet present)

# Step 2 — ratify or prune the whitelist
# Edit config/auto_fix_classes.yaml — remove any class you are not comfortable with.
# See §5.4 for the append-entry procedure.

# Step 3 — back up the database
cp /root/atlas/data/atlas.db \
   /root/atlas/data/atlas.db.backup-pre-phase3-$(date +%s)

# Step 4 — flip the feature flag
# In config/auto_remediation.yaml, change:
#   phase_3_enabled: false
# to:
#   phase_3_enabled: true

# Step 5 — verify Python sees the new state
cd /root/atlas && python3 -c "
import yaml
cfg = yaml.safe_load(open('config/auto_remediation.yaml'))
print('phase_3_enabled:', cfg['phase']['phase_3_enabled'])
"
# → phase_3_enabled: True

# Step 6 (optional) — env override for rapid hotfix testing
export AUTO_REMEDIATION_PHASE_3_ENABLED=true

# Step 7 — install Phase 3 crons (see §8.2 and §8.3 for unit file templates)
sudo systemctl link /root/atlas/systemd/atlas-promote-staging.service
sudo systemctl link /root/atlas/systemd/atlas-promote-staging.timer
sudo systemctl enable --now atlas-promote-staging.timer
sudo systemctl link /root/atlas/systemd/atlas-graduation-engine.service
sudo systemctl link /root/atlas/systemd/atlas-graduation-engine.timer
sudo systemctl enable --now atlas-graduation-engine.timer

# Step 8 — watch dashboard for first AUTO_FIX commits (allow up to 30 min)
# First auto-merge should be a trivial markdown_typos or test_import_error fix.
```

### 5.3 Reverting Phase 3 (back to ASSIST-only)

```bash
# Step 1 — flip the flag back
# In config/auto_remediation.yaml:
#   phase_3_enabled: true  →  phase_3_enabled: false

# Step 2 — stop Phase 3 promotion cron
sudo systemctl stop atlas-promote-staging.timer
sudo systemctl stop atlas-graduation-engine.timer

# Step 3 — manually revert any Phase 3 commits in flight (see §4.1)
git -C /root/atlas log --oneline | grep "auto-fix"
```

### 5.4 Adding a new whitelist class

1. Verify the class meets graduation thresholds:

```bash
cd /root/atlas && python3 scripts/run_graduation_engine.py --dry-run
# Look for the class in proposed promotions
```

2. Append to `config/auto_fix_classes.yaml`:

```yaml
# Example entry
- name: your_new_class
  description: "Brief description"
  scope_globs:
    - "tests/**"
  max_diff_lines: 20
  require_regression_test: true
  min_confidence: 0.80
```

3. Validate YAML syntax:

```bash
python3 -c "
import yaml
data = yaml.safe_load(open('/root/atlas/config/auto_fix_classes.yaml').read())
print(f'Loaded {len(data)} classes OK')
"
```

4. Run integration tests:

```bash
cd /root/atlas && python3 -m pytest \
  tests/test_auto_fix_classes.py \
  tests/test_auto_merger.py \
  -x --timeout=30 -q
```

5. Commit and push.

### 5.5 Demoting a class (AUTO_FIX → permanent ASSIST)

**Automatic demotion**: The graduation engine (`scripts/run_graduation_engine.py`) demotes a class when it accumulates >5 scope-guard violations in 60 days (`graduation.auto_fix_to_permanent_assist.scope_violations_threshold: 5`, `scope_violations_window_days: 60`). A Telegram alert fires on demotion.

**Manual demotion**:

```bash
# Remove the class from config/auto_fix_classes.yaml, then commit + push.
# The graduation engine logs the demotion to fix_audit_log as phase='demotion'.
```

A permanently demoted class becomes ASSIST-only. Re-promotion requires a fresh 14-day observation window.

---

## 6. Adding/Modifying Configuration

### 6.1 NEVER list (`config/auto_fix_deny.yaml`)

**This is the single most safety-critical config file.** Reducing coverage (removing entries) must be treated with the same care as modifying trading-path code. When in doubt: add, never remove.

The deny list is enforced at three layers:
1. **Triage classifier** (`core/triage.py`) — any matching error → `ESCALATE`
2. **Fix-author prompt preamble** (Phase 2) — LLM instructed never to touch these paths
3. **Multi-team domain write enforcement** (Phase 2) — OS-level minimatch glob check

**To add new path globs** (do this whenever you add new critical files to the codebase):

```yaml
# config/auto_fix_deny.yaml — append to file_globs:
file_globs:
  # ... existing entries ...
  - "your_new_critical_path/**"
```

No reload needed — the classifier reads the file on every cycle.

**Validate after editing**:

```bash
# Syntax check
python3 -c "
import yaml
d = yaml.safe_load(open('/root/atlas/config/auto_fix_deny.yaml').read())
print(f'file_globs: {len(d[\"file_globs\"])}')
print(f'error_class_patterns: {len(d[\"error_class_patterns\"])}')
print(f'message_patterns: {len(d[\"message_patterns\"])}')
"

# Full triage tests
cd /root/atlas && python3 -m pytest tests/test_triage_classifier.py --timeout=30 -q
```

### 6.2 Safety-critical functions (`config/safety_critical_functions.txt`)

One function name per line. Gate 9 (`gate_no_safety_critical_function_modified`) AST-parses every modified `.py` file and blocks the merge if any function in this file was added or changed.

Current blocked functions include: `place_order`, `execute_plan`, `halt`, `is_halted`, `check_kill_switch`, `check_daily_drawdown`, `compute_position_size`, `apply_overlay`, `generate_signals`, `reconcile_positions`, `record_trade_entry`, `record_trade_exit`, `transition_trade`, and others (see the file for the full 36-entry list).

**To add a new function**:

```bash
echo "your_new_critical_function" >> /root/atlas/config/safety_critical_functions.txt
grep "your_new_critical_function" /root/atlas/config/safety_critical_functions.txt
```

No reload needed — Gate 9 reads the file at gate-check time.

### 6.3 Budget settings

The budget section of `config/auto_remediation.yaml` controls autonomy limits:

```yaml
budget:
  max_commits_per_day: 10       # HALT if >=10 merges in 24h
  reverts_to_halt: 2            # HALT if >=2 reverts in 24h
  revert_rate_alert_pct: 15     # Telegram WARN at 15% revert rate
  revert_rate_halt_pct: 25      # HALT at 25% revert rate
```

**Why `max_commits_per_day: 10`**: 10 commits/day is the Phase 3 aggressive ceiling. In practice, expect 0–3 commits per day on a healthy codebase. The cap exists as a budget circuit-breaker to prevent a runaway fix loop from filling git history with junk.

**Effects of changing parameters**:
- Raising `max_commits_per_day`: more autonomy, higher risk of runaway
- Lowering `reverts_to_halt`: more conservative (1 = halt on first revert)
- Raising `revert_rate_halt_pct`: more autonomy, less safety
- These are **operator-locked** values — change deliberately, not reactively

### 6.4 Telegram cadence

From `config/auto_remediation.yaml`:

```yaml
telegram:
  on_success: never       # Every successful fix is SILENT
  on_failure: immediate   # Any failure: budget breach, revert, halt → immediate alert
  daily_digest: false     # No daily digest
```

`on_success: never` is intentional — the system is designed to be noise-free. Check the dashboard or audit log to see what was merged.

`on_failure: immediate` means budget breach, auto-revert, auto-halt, and L4/L5/L6 trips send a Telegram alert before the Python process exits.

**Testing the Telegram channel**:

```bash
cd /root/atlas && python3 -c "
from utils.telegram import send_message
send_message('Remediation runbook test alert — ignore')
"
```

---

## 7. Inspecting the Audit Log

### 7.1 Schema reference

Full table contracts: `db/schema/errors_remediation.md`

Quick reminder of the three tables:

| Table | Rows | Purpose |
|---|---|---|
| `errors` | One per fingerprint (deduped) | Every distinct error captured; classification; remediation_status |
| `fix_attempts` | One per fix attempt | State machine: triaged → reproducing → fixing → reviewing → merged/reverted/failed |
| `fix_audit_log` | Many per attempt | **APPEND-ONLY** forensic trail (triggers block UPDATE/DELETE) |

The `fix_audit_log` immutability is enforced by two DB triggers:
- `fix_audit_log_no_update`: `BEFORE UPDATE ON fix_audit_log → RAISE(ABORT, 'fix_audit_log is append-only')`
- `fix_audit_log_no_delete`: `BEFORE DELETE ON fix_audit_log → RAISE(ABORT, 'fix_audit_log is append-only')`

Verify these are active quarterly — see §11.1.

### 7.2 Common queries

```sql
-- Today's fix attempts (UTC)
SELECT id, fingerprint, status, classification, review_verdict, started_ts
FROM fix_attempts
WHERE date(started_ts) = date('now');

-- Reverted fixes last 7d
SELECT id, fingerprint, fix_commit_sha, revert_commit_sha,
       revert_reason, reverted_ts
FROM fix_attempts
WHERE status = 'reverted'
  AND reverted_ts > datetime('now', '-7 days');

-- Top fingerprints (most-occurring, last 24h)
SELECT fingerprint, occurrence_count, classification, message, level
FROM errors
WHERE last_seen_ts > datetime('now', '-24 hours')
ORDER BY occurrence_count DESC LIMIT 20;

-- All audit entries for a specific error
SELECT phase, actor, decision, reasoning, ts
FROM fix_audit_log
WHERE error_id = ?
ORDER BY ts ASC;

-- Reviewer rejection rate (last 24h)
SELECT review_verdict, COUNT(*) AS n
FROM fix_attempts
WHERE review_verdict IS NOT NULL
  AND started_ts > datetime('now', '-24 hours')
GROUP BY review_verdict;

-- Gate failures breakdown (what's blocking merges)
SELECT gates_failed_json, COUNT(*) AS n
FROM fix_attempts
WHERE blocked_by_gate IS NOT NULL
  AND started_ts > datetime('now', '-7 days')
GROUP BY gates_failed_json
ORDER BY n DESC;

-- Budget metrics right now
SELECT
  SUM(CASE WHEN status='merged'
           AND finished_ts > datetime('now','-24 hours') THEN 1 ELSE 0 END) AS commits_24h,
  SUM(CASE WHEN status='reverted'
           AND reverted_ts > datetime('now','-24 hours') THEN 1 ELSE 0 END) AS reverts_24h
FROM fix_attempts;
```

### 7.3 Diagnosing why a fix didn't fire

Walk down this checklist when an error you expected to be fixed wasn't:

```sql
-- Step 1: Was the error captured?
SELECT id, fingerprint, classification, remediation_status, occurrence_count
FROM errors
WHERE message LIKE '%your error text%'
ORDER BY last_seen_ts DESC LIMIT 5;
```

If no row: capture pipeline isn't writing it. Run backfill:
```bash
cd /root/atlas && python3 scripts/backfill_errors_from_logs.py --days 1 --apply
```

```sql
-- Step 2: Was it classified?
-- UNCLASSIFIED = classifier hasn't run yet (or backlogged)
-- IGNORE       = matched a deny pattern
-- ESCALATE     = trading-path causal chain or NEVER-list
-- ASSIST/AUTO_FIX = should have spawned a fix attempt
SELECT classification, triage_reason FROM errors WHERE id = <id>;

-- Step 3: Was a fix attempt dispatched?
SELECT id, status, started_ts, finished_ts, review_verdict, blocked_by_gate
FROM fix_attempts WHERE error_id = <id>;

-- Step 4: Which gate blocked it?
SELECT gates_failed_json, blocked_by_gate
FROM fix_attempts WHERE id = <attempt_id>;
```

Common gate blockers:

| Gate | Common cause | Fix |
|---|---|---|
| `no_never_list_touched` | Diff touched a deny-listed path | Reclassify error as ESCALATE; add pattern to deny |
| `diff_size_cap` | Fix >30 lines | Break error into smaller parts; or ESCALATE |
| `reviewer_approved` | Opus 4.7 returned REJECT | Inspect `review_reason`; if risky, this is correct |
| `regression_test_present` | No new `def test_` added | Fix worker didn't add a test; manual ASSIST |
| `targeted_tests` | Test failure | Fix is broken; manual repair or discard |

---

## 8. Cron Installation

### 8.1 Phase 1+2 monitor

The systemd units are at `systemd/atlas-error-remediation.{service,timer}`. They are NOT auto-installed (committed in `7cef4601`).

```bash
# Install (link from project into systemd)
sudo systemctl link /root/atlas/systemd/atlas-error-remediation.service
sudo systemctl link /root/atlas/systemd/atlas-error-remediation.timer

# Enable and start
sudo systemctl enable --now atlas-error-remediation.timer

# Verify timer fires every 5 min
systemctl status atlas-error-remediation.timer

# Check service ran successfully
journalctl -u atlas-error-remediation.service -n 20 --no-pager

# Check log file
tail -50 /root/atlas/logs/error-remediation.log
```

**To switch from dry-run to live Phase 2 dispatch**: The shipped `ExecStart` includes `--dry-run`. Remove it via systemd override AND set `monitor.dry_run: false` in `config/auto_remediation.yaml`:

```bash
# Create override
sudo systemctl edit atlas-error-remediation.service --force
# Add under [Service]:
# ExecStart=
# ExecStart=/usr/bin/python3 /root/atlas/core/error_monitor.py --once --batch-size 50
sudo systemctl daemon-reload
```

### 8.2 Phase 3 staging→main promotion (every 30 min)

> **Note**: `systemd/atlas-promote-staging.{service,timer}` do NOT yet exist. Create from this template when Phase 3 activates.

```ini
# /root/atlas/systemd/atlas-promote-staging.service
[Unit]
Description=Atlas auto-fix staging→main promoter (Phase 3)
After=network-online.target
ConditionPathExists=!/root/atlas/data/AUTO_REMEDIATION_HALT
ConditionPathExists=!/root/atlas/data/HALT
ConditionPathExists=!/root/atlas/.live_halt

[Service]
Type=oneshot
WorkingDirectory=/root/atlas
Environment="TZ=Australia/Brisbane"
EnvironmentFile=-/etc/atlas/atlas.conf
EnvironmentFile=-/root/atlas/.env
ExecStart=/usr/bin/python3 /root/atlas/scripts/promote_auto_fix_staging.py
TimeoutStartSec=300
StandardOutput=append:/root/atlas/logs/promote-staging.log
StandardError=append:/root/atlas/logs/promote-staging.log

[Install]
WantedBy=multi-user.target
```

```ini
# /root/atlas/systemd/atlas-promote-staging.timer
[Unit]
Description=Promote auto-fix-staging to main every 30 min (Phase 3)

[Timer]
OnCalendar=*:0/30
RandomizedDelaySec=60
Persistent=false
Unit=atlas-promote-staging.service

[Install]
WantedBy=timers.target
```

### 8.3 Daily graduation engine (every midnight UTC)

> **Note**: `systemd/atlas-graduation-engine.{service,timer}` do NOT yet exist.

```ini
# /root/atlas/systemd/atlas-graduation-engine.service
[Unit]
Description=Atlas error-class graduation engine (Phase 3)
After=network-online.target
ConditionPathExists=!/root/atlas/data/AUTO_REMEDIATION_HALT
ConditionPathExists=!/root/atlas/data/HALT

[Service]
Type=oneshot
WorkingDirectory=/root/atlas
Environment="TZ=Australia/Brisbane"
ExecStart=/usr/bin/python3 /root/atlas/scripts/run_graduation_engine.py
TimeoutStartSec=300
StandardOutput=append:/root/atlas/logs/graduation-engine.log
StandardError=append:/root/atlas/logs/graduation-engine.log

[Install]
WantedBy=multi-user.target
```

```ini
# /root/atlas/systemd/atlas-graduation-engine.timer
[Unit]
Description=Run graduation engine nightly at midnight UTC

[Timer]
OnCalendar=*-*-* 00:00:00
RandomizedDelaySec=120
Persistent=false
Unit=atlas-graduation-engine.service

[Install]
WantedBy=timers.target
```

### 8.4 Crontab alternative

```cron
# Phase 1+2 — error monitor every 5 min
*/5 * * * * /root/atlas/scripts/cron_stderr_capture.sh atlas_error_monitor \
  /usr/bin/python3 /root/atlas/core/error_monitor.py --once --batch-size 50

# Phase 3 — staging promotion every 30 min
*/30 * * * * /root/atlas/scripts/cron_stderr_capture.sh atlas_promote_staging \
  /usr/bin/python3 /root/atlas/scripts/promote_auto_fix_staging.py

# Daily — graduation engine at midnight UTC
0 0 * * * /root/atlas/scripts/cron_stderr_capture.sh atlas_graduation \
  /usr/bin/python3 /root/atlas/scripts/run_graduation_engine.py

# Daily — classifier validation (30-day rolling window)
30 0 * * * /root/atlas/scripts/cron_stderr_capture.sh atlas_classifier_validation \
  /usr/bin/python3 /root/atlas/scripts/validate_classifier_30day.py --days 30
```

---

## 9. Telegram Bot Commands

The following commands are Phase 3+ and **not yet shipped**. They will be added to `services/telegram_bot.py` as part of the graduation-engine release:

- `/halt-remediation` — writes `data/AUTO_REMEDIATION_HALT` (L7 → L2)
- `/resume-remediation` — removes `data/AUTO_REMEDIATION_HALT`
- `/remediation-status` — current phase, halt state, last 5 fix attempts
- `/approve_fix <id>` — manually approve a stuck ASSIST proposal

**For now**, use the file-level, `systemctl`, and SQLite procedures in §3 and §4.

---

## 10. Disaster Recovery

### 10.1 Bad fix landed in main

```bash
# Step 1 — revert immediately (<30s)
cd /root/atlas && git revert --no-edit <sha>

# Step 2 — halt remediation
touch /root/atlas/data/AUTO_REMEDIATION_HALT
echo "Halt after bad fix <sha> — investigating" \
  > /root/atlas/data/AUTO_REMEDIATION_HALT

# Step 3 — update DB
sqlite3 /root/atlas/data/atlas.db "
UPDATE fix_attempts
SET status='reverted',
    revert_commit_sha='$(cd /root/atlas && git rev-parse HEAD)',
    revert_reason='operator: bad fix in main',
    reverted_ts=datetime('now')
WHERE fix_commit_sha='<sha>';
"

# Step 4 — root-cause via audit log (§7.2)
# Which gate failed to catch this?

# Step 5 — if root cause is 'should have been blocked':
# Add the path glob / message pattern to config/auto_fix_deny.yaml
cd /root/atlas && python3 -m pytest tests/test_triage_classifier.py --timeout=30

# Step 6 — resume after investigation
rm /root/atlas/data/AUTO_REMEDIATION_HALT
```

### 10.2 Capture broken (errors table not receiving writes)

```bash
# Step 1 — meta-monitor
cd /root/atlas && python3 scripts/healthz_error_remediation.py --json

# Step 2 — direct DB check
sqlite3 /root/atlas/data/atlas.db \
  "SELECT COUNT(*) FROM errors WHERE created_at > datetime('now', '-1 hour');"

# Step 3 — timer health
systemctl status atlas-error-remediation.timer
journalctl -u atlas-error-remediation.service -n 50 --no-pager

# Step 4 — log tail
tail -100 /root/atlas/logs/error-remediation.log

# Step 5 — manual sweep to verify classifier runs
cd /root/atlas && python3 core/error_monitor.py --once --batch-size 10 --dry-run

# Step 6 — if source is journald: check journald backlog
journalctl -u 'atlas-*' --since='1 hour ago' | wc -l

# Step 7 — backfill missed errors from log files
cd /root/atlas && python3 scripts/backfill_errors_from_logs.py --days 1 --apply
```

### 10.3 Classifier returning wrong tiers

**Symptom**: ASSIST or AUTO_FIX assigned to something that should be ESCALATE; or IGNORE assigned to a real bug.

```bash
# Step 1 — run the 30-day validation harness
cd /root/atlas && python3 scripts/validate_classifier_30day.py --days 30
# Exit 0: pass (>=94% IGNORE)
# Exit 1: warn (80-94%) — classifier needs tuning, don't halt yet
# Exit 2: FAIL (<80% IGNORE) — STOP all Phase 2/3 dispatch immediately

# If exit 2: halt immediately
touch /root/atlas/data/AUTO_REMEDIATION_HALT
echo "Classifier <80pct IGNORE — full audit required" \
  > /root/atlas/data/AUTO_REMEDIATION_HALT

# Step 2 — identify wrongly-classified errors
sqlite3 /root/atlas/data/atlas.db "
SELECT classification, triage_reason, message, exc_type, file_path
FROM errors
WHERE classification IN ('ASSIST','AUTO_FIX')
  AND (
    file_path LIKE 'brokers/%'
    OR file_path LIKE 'risk/%'
    OR message LIKE '%broker%'
    OR message LIKE '%order%'
  )
ORDER BY last_seen_ts DESC LIMIT 20;
"

# Step 3 — tune config/auto_fix_deny.yaml; re-run validation until >=94% IGNORE
# Step 4 — resume
rm /root/atlas/data/AUTO_REMEDIATION_HALT
```

### 10.4 Reviewer agent not approving anything

**Symptom**: All ASSIST fixes sit in `status='blocked'` with `review_verdict='REJECT'`.

```sql
-- Rejection rate check (last 24h)
SELECT review_verdict, COUNT(*) AS n,
       ROUND(COUNT(*)*100.0 / SUM(COUNT(*)) OVER (), 1) AS pct
FROM fix_attempts
WHERE review_verdict IS NOT NULL
  AND started_ts > datetime('now', '-24 hours')
GROUP BY review_verdict;
```

**Thresholds**:
- `review.rejection_rate_alert_pct: 40` → Telegram WARNING fires automatically
- `review.rejection_rate_halt_pct: 50` → L6 auto-trips kill switch

**Triage**:
1. Look at `review_reason` for the last 10 rejected attempts — are the fixes actually bad?
2. If the fixes are legitimately risky: the reviewer is working correctly. Improve fix worker prompts.
3. If the fixes are clearly safe but getting rejected: the reviewer prompt may have drifted. Manual review of last 10 via audit log.
4. Model availability: if `pi -p` returns "out of extra usage", the reviewer cannot run at all. See §10.5.

```sql
-- Inspect reject reasons
SELECT id, review_verdict, review_confidence, review_reason
FROM fix_attempts
WHERE review_verdict = 'REJECT'
ORDER BY started_ts DESC LIMIT 10;
```

### 10.5 OAuth subscription window exhausted

**Symptom**: `pi -p` returns "out of extra usage" or `400 invalid_request_error`.

```bash
# Step 1 — verify auth
cd /root/atlas && python3 scripts/claude_auth_check.py

# Step 2 — confirm it is the window, not a missing --system-prompt
cd /root/atlas && python3 scripts/lint_pi_system_prompt.py
# Should return 0; non-zero = a call site is missing the flag

# Step 3 — wait for rolling 5-hour window to refill
# OR re-login:
pi login

# Step 4 — halt remediation while waiting
touch /root/atlas/data/AUTO_REMEDIATION_HALT
echo "OAuth window exhausted — will resume after refill" \
  > /root/atlas/data/AUTO_REMEDIATION_HALT
```

---

## 11. Compliance Checks

### 11.1 Audit log immutability invariant

Run quarterly. Verifies the two BEFORE-trigger guards are still in place:

```sql
-- Must return exactly 2 rows
SELECT name, sql
FROM sqlite_master
WHERE type = 'trigger'
  AND tbl_name = 'fix_audit_log';
```

Expected: `fix_audit_log_no_update` + `fix_audit_log_no_delete`, both with `RAISE(ABORT, ...)`.

If either trigger is missing, re-apply the migration:

```bash
cd /root/atlas && python3 \
  scripts/migrations/2026-04-29-add-errors-remediation-tables.py --apply
```

### 11.2 NEVER list coverage audit

Run quarterly. Verify `config/auto_fix_deny.yaml` enforcement:

```bash
cd /root/atlas && python3 -m pytest \
  tests/test_auto_remediation_phase2_integration.py::TestNeverListInvariant \
  --timeout=30 -v
```

Spot-check denial manually:

```bash
cd /root/atlas && python3 -c "
from core.triage import TriageClassifier
clf = TriageClassifier()
test_cases = [
    {'message': 'broker connection failed', 'file_path': 'brokers/alpaca/broker.py', 'exc_type': 'BrokerError'},
    {'message': 'drawdown limit breached', 'file_path': 'risk/drawdown.py', 'exc_type': 'DrawdownBreach'},
    {'message': 'order rejected by Alpaca', 'file_path': 'scripts/execute_approved.py', 'exc_type': 'OrderRejected'},
]
for tc in test_cases:
    result = clf.classify(tc)
    print(f'{result.classification:10s} {tc[\"message\"][:40]}')
    assert result.classification == 'ESCALATE', f'FAILED: {tc}'
print('All deny patterns verified')
"
```

### 11.3 OAuth routing audit

Every commit: pre-commit hook `lint_pi_system_prompt.py` runs automatically. Manual check:

```bash
cd /root/atlas && python3 scripts/lint_pi_system_prompt.py
# Must return 0 (all pi/claude subprocess calls have --system-prompt)
```

### 11.4 Quarterly whitelist review

Open `config/auto_fix_classes.yaml`. For each class, run:

```sql
-- Successful AUTO_FIX merges last 90 days
SELECT COUNT(*) FROM fix_attempts
WHERE classification = 'AUTO_FIX'
  AND status = 'merged'
  AND started_ts > datetime('now', '-90 days');

-- Reverts last 90 days
SELECT COUNT(*) FROM fix_attempts
WHERE classification = 'AUTO_FIX'
  AND status = 'reverted'
  AND started_ts > datetime('now', '-90 days');
```

If a class has 0 merges and >0 reverts in 90 days: consider demotion (§5.5). Dormant classes (0 merges, 0 reverts) are harmless — keep them.

---

## 12. Quick Reference

### Files

| Path | Purpose |
|---|---|
| `config/auto_remediation.yaml` | Main config — phase, budget, graduation, telegram, permanent_assist, never_fix |
| `config/auto_fix_deny.yaml` | NEVER list — file globs, error class patterns, message patterns |
| `config/auto_fix_classes.yaml` | Day-1 AUTO_FIX whitelist (Phase 3, created at graduation pre-flight) |
| `config/safety_critical_functions.txt` | Function-name AST blocks for Gate 9 |
| `data/AUTO_REMEDIATION_HALT` | Manual halt sentinel (L2) — create to halt, delete to resume |
| `data/HALT` | Trading kill switch (L3) — managed by `halt_trading.py`, not by this pipeline |
| `data/atlas.db: errors` | Captured error stream (deduped by fingerprint) |
| `data/atlas.db: fix_attempts` | Fix attempt state machine rows |
| `data/atlas.db: fix_audit_log` | Append-only forensic trail |
| `logs/error-remediation.log` | Phase 1+2 service log |
| `logs/promote-staging.log` | Phase 3 staging promotion log |
| `logs/graduation-engine.log` | Phase 3 graduation engine log |

### Commands

| Command | Effect |
|---|---|
| `touch data/AUTO_REMEDIATION_HALT` | Halt all remediation (L2) immediately |
| `rm data/AUTO_REMEDIATION_HALT` | Resume remediation |
| `python3 -c "from core.remediation_kill_switch import resume; resume()"` | Same as rm |
| `python3 -c "from core.remediation_kill_switch import check_all_layers; print(check_all_layers())"` | Print tripped layer (None = all clear) |
| `python3 core/error_monitor.py --once --dry-run` | Manual classifier sweep (no side effects) |
| `python3 core/error_monitor.py --once --batch-size 50` | Live classifier sweep |
| `python3 scripts/healthz_error_remediation.py --json` | Meta-monitor health check |
| `python3 scripts/validate_classifier_30day.py` | Re-validate >=94% IGNORE gate (exit 0=pass, 1=warn, 2=fail) |
| `python3 scripts/validate_classifier_30day.py --days 30 --output report.md` | Full validation report |
| `python3 scripts/run_graduation_engine.py --dry-run` | Show pending class promotions/demotions |
| `python3 scripts/backfill_errors_from_logs.py --days 7 --apply` | Backfill missed errors from log files |
| `python3 scripts/lint_pi_system_prompt.py` | Verify all pi subprocess calls have --system-prompt |

### Endpoints

| URL | Purpose |
|---|---|
| `/api/error_remediation/summary` | Current counts: total, 24h, by_classification, by_status, attempts |
| `/api/error_remediation/timeseries?hours=24` | Hourly error volume time-series |
| `/api/error_remediation/fingerprints?hours=24&limit=20` | Top error fingerprints |
| `/api/error_remediation/attempts?status=&limit=50` | Fix attempt history with gate details |
| `/api/error_remediation/health` | Meta-monitor signal: halt_active, backlog, revert_rate, phase |

All endpoints require HTTP Basic Auth (same as trading dashboard).

---

## Appendix A: Audit Log Phases

Every action by every actor — human or agent — appends a row to `fix_audit_log`.

| `phase` | `actor` | When |
|---|---|---|
| `capture` | `python_logger` / `journald` / `cron` / `healthcheck` / `manual` / `backfill` | An error is recorded in the `errors` table |
| `triage` | `classifier` | Triage classifier assigns a classification + tier |
| `reproduce` | `fix_worker` | (Phase 2) Fix worker attempts to reproduce the error |
| `diagnose` | `fix_worker` | (Phase 2) Diagnosis recorded with root-cause narrative |
| `fix` | `fix_worker` | Fix attempt resolved (branch + diff produced, or failed) |
| `verify` | `fix_worker` | Tests run on the fix branch; results recorded |
| `review` | `reviewer` | Adversarial reviewer (Opus 4.7) returns APPROVE or REJECT |
| `gate_check` | `merger` | All 15 merge gates evaluated; results in `gates_passed_json` / `gates_failed_json` |
| `merge` | `merger` / `auto_merger` | Branch merged to `auto-fix-staging` (Phase 2) or promoted to `main` (Phase 3) |
| `monitor` | `promote_auto_fix_staging` / `merger` | 30-min monitor outcome: clean or revert |
| `revert` | `monitor` / `operator` | Auto- or manual revert applied |
| `halt` | `kill_switch` / `budget` / `operator` | System halted (L2 file written, reason recorded) |
| `resume` | `operator` | System resumed (L2 file removed) |
| `graduation` | `graduation_engine` | Class promoted ASSIST → AUTO_FIX after meeting thresholds |
| `demotion` | `graduation_engine` | Class demoted AUTO_FIX → permanent ASSIST after scope violations |
| `config_change` | `operator` | YAML config edited (optional manual audit entry) |
| `manual` | `human:<name>` | Any operator action not covered above |

---

## Appendix B: The 15 Merge Gates

All 15 gates must pass for Phase 3 AUTO_FIX merge. Phase 2 ASSIST runs all gates and surfaces failures to the human reviewer.

| # | Gate name | Blocking? | What it checks |
|---|---|---|---|
| 1 | `targeted_tests` | YES | Targeted pytest run passes |
| 2 | `regression_test_present` | YES | Diff contains >=1 new `def test_` function |
| 3 | `full_suite` | YES | Full `pytest tests/ -x --timeout=30` (Phase 3 AUTO_FIX only) |
| 4 | `no_new_bare_except` | YES | No new bare-except vs baseline |
| 5 | `pi_system_prompt_lint` | YES | All pi/claude subprocess calls have `--system-prompt` |
| 6 | `no_check_violations` | WARNING | No new writes to CHECK-constrained tables |
| 7 | `diff_size_cap` | YES | Total diff <=30 lines added+removed |
| 8 | `no_never_list_touched` | YES | No file in `auto_fix_deny.yaml#file_globs` was modified |
| 9 | `no_safety_critical_function_modified` | YES | AST: no function in `safety_critical_functions.txt` changed |
| 10 | `reviewer_approved` | YES | Adversarial reviewer (Opus 4.7) returned APPROVE, confidence >=0.75 |
| 11 | `no_healthcheck_regression` | YES | No CRITICAL healthcheck in 30-min post-merge window |
| 12 | `no_fingerprint_recurrence` | YES | Same fingerprint not seen during 30-min monitor window |
| 13 | `no_warning_demotion` | WARNING | No new `logger.warning()` calls referencing "error" |
| 14 | `mypy_clean` | WARNING | mypy passes on modified `.py` files |
| 15 | `pre_commit_hooks` | WARNING | Pre-commit config passes |

---

## Appendix C: Key Config Values (Operator-Locked)

Ratified 2026-04-29. Do not change without deliberate review.

| Setting | Value | Rationale |
|---|---|---|
| `budget.max_commits_per_day` | 10 | Circuit-breaker; prevents runaway fix loops |
| `budget.reverts_to_halt` | 2 | Two reverts = something systematic is wrong |
| `budget.revert_rate_halt_pct` | 25 | 1 in 4 merges being bad is unacceptable |
| `graduation.assist_to_auto_fix.days_of_clean_assist` | 14 | Two calendar weeks of data before autonomy |
| `graduation.assist_to_auto_fix.min_merged_assist_fixes` | 5 | Minimum track record per class |
| `graduation.auto_fix_to_permanent_assist.scope_violations_threshold` | 5 | >5 violations = class too risky for autonomy |
| `review.approval_threshold_confidence` | 0.75 | Reviewer must be 75% confident to APPROVE |
| `review.rejection_rate_halt_pct` | 50 | >50% rejection = fixes are systematically bad |
| `verify.diff_max_lines` | 30 | Hard cap; forces atomic, reviewable changes |
| `verify.monitor_window_minutes` | 30 | Post-merge observation window before clean/revert decision |
| `monitor.batch_size` | 50 | Errors classified per cycle |
| `audit_log.retention_days` | 365 | Full-year forensic trail |

All settings cross-reference `reports/auto-error-remediation-planning-2026-04-29.md` and `reports/auto-error-remediation-validation-2026-04-29.md`.
