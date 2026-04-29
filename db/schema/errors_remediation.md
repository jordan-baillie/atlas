# Schema: Auto-Error-Remediation Tables

**Migration**: `scripts/migrations/2026-04-29-add-errors-remediation-tables.py`  
**Phase**: Phase 0 — Error Capture & Triage Foundation  
**Objects**: 3 tables · 13 indexes · 2 triggers

---

## ER Diagram (ASCII)

```
┌──────────────────────────────────────────────────────┐
│                       errors                         │
│  id (PK)                                             │
│  fingerprint (UNIQUE)                                │
│  fixed_by_attempt_id ─────────────────────────────┐  │
│  ...                                               │  │
└──────────────────────────────────────────────────────┘
          │                                           │
          │ error_id (FK, NOT NULL)                   │ FK → fix_attempts.id
          ▼                                           ▼
┌──────────────────────────┐   ┌────────────────────────────────────────┐
│       fix_attempts       │   │             fix_audit_log              │
│  id (PK)            ◄────┼───│  attempt_id (FK, nullable)             │
│  error_id (FK) ─────┘    │   │  error_id   (FK, nullable)             │
│  status (state machine)  │   │  phase, actor, decision, reasoning ... │
│  ...                     │   │  IMMUTABLE: triggers block UPDATE &    │
└──────────────────────────┘   │  DELETE permanently                    │
                               └────────────────────────────────────────┘
```

**Relationships**:

| FK column | References | Nullable |
|-----------|-----------|----------|
| `errors.fixed_by_attempt_id` | `fix_attempts.id` | YES — set only when error is resolved |
| `fix_attempts.error_id` | `errors.id` | NO — every attempt belongs to an error |
| `fix_audit_log.attempt_id` | `fix_attempts.id` | YES — phase-capture rows before attempt creation |
| `fix_audit_log.error_id` | `errors.id` | YES — system-level events may have no error |

---

## Table: `errors`

Deduplicated error stream. One row per unique error **fingerprint** — the same
error recurring 100 times creates 1 row with `occurrence_count = 100`.

**Written by**: `SQLiteErrorWriter`, journald tailer, cron stderr capture,
healthcheck, telegram alert parser, manual entry, backfill scripts.

**Read by**: Triage classifier, fix worker, dashboard, monitoring alerts.

| Column | Type | Meaning | Written by | Read by |
|--------|------|---------|------------|---------|
| `id` | INTEGER PK | Auto-increment surrogate key | DB | all |
| `fingerprint` | TEXT UNIQUE NOT NULL | `sha256(exc_type + normalize(message) + file_path + ":" + line_number)[:16]` | error writer | classifier |
| `first_seen_ts` | TEXT NOT NULL | ISO ts of first occurrence | error writer | dashboard |
| `last_seen_ts` | TEXT NOT NULL | ISO ts of most-recent occurrence | error writer (upsert) | staleness monitor |
| `occurrence_count` | INTEGER DEFAULT 1 | Times this fingerprint has fired | error writer (increment) | triage priority |
| `ts` | TEXT NOT NULL | Timestamp of the captured log line | error writer | audit |
| `source` | TEXT NOT NULL | `python_logger` · `journald` · `cron` · `healthcheck` · `telegram_alert` · `manual` · `backfill` | error writer | classifier |
| `service` | TEXT | Systemd unit name (e.g. `atlas-dashboard`) | journald tailer | filter |
| `level` | TEXT NOT NULL | `WARNING` · `ERROR` · `CRITICAL` | error writer | triage |
| `logger_name` | TEXT | Python `__name__` logger | Python log handler | debug |
| `message` | TEXT NOT NULL | Raw log message (before normalization) | error writer | triage |
| `exc_type` | TEXT | Exception class name (e.g. `KeyError`) | log handler | fingerprint |
| `exc_message` | TEXT | `str(exception)` | log handler | triage |
| `traceback` | TEXT | Full traceback text | log handler | fix worker |
| `file_path` | TEXT | Source file (e.g. `brokers/live_executor.py`) | log handler | fingerprint |
| `line_number` | INTEGER | Line number in source file | log handler | fingerprint |
| `function_name` | TEXT | Function name from traceback | log handler | context |
| `pid` | INTEGER | Process ID | log handler | correlation |
| `hostname` | TEXT | Machine hostname | log handler | multi-host |
| `context_json` | TEXT | Free-form JSON for extra context | error writer | fix worker |
| `market_hours` | INTEGER (0/1) | Was market open when error occurred? | error writer | severity |
| `halt_active` | INTEGER (0/1) | Was a trading halt active? | error writer | severity |
| `git_sha` | TEXT | Git commit SHA at time of error | log handler | regression |
| `classification` | TEXT DEFAULT 'UNCLASSIFIED' | `AUTO_FIX` · `ASSIST` · `ESCALATE` · `IGNORE` · `UNCLASSIFIED` · `ESCALATE_DEFERRED` · `IGNORE_PENDING_CLEAR` | classifier | fix worker |
| `triage_reason` | TEXT | Human-readable rationale for classification | classifier | audit |
| `tier` | INTEGER DEFAULT 99 | Priority: 0=critical 1=high 2=medium 99=unclassified | classifier | fix worker |
| `remediation_status` | TEXT DEFAULT 'NEW' | `NEW` · `TRIAGED` · `IN_FLIGHT` · `FIXED` · `REVERTED` · `ESCALATED` · `IGNORED` · `SUPPRESSED` | fix worker | monitor |
| `remediation_attempts` | INTEGER DEFAULT 0 | Number of fix_attempts rows for this error | fix worker (increment) | retry logic |
| `last_attempt_at` | TEXT | ISO ts of most-recent fix attempt | fix worker | retry backoff |
| `fixed_by_attempt_id` | INTEGER → fix_attempts.id | FK to the attempt that resolved the error (nullable) | fix worker | resolution |
| `resolved_at` | TEXT | ISO ts when error moved to FIXED/IGNORED | fix worker | SLA tracking |
| `created_at` | TEXT DEFAULT datetime('now') | Row insertion time | DB | audit |

**Indexes (6)**:

| Index | Type | Columns | Purpose |
|-------|------|---------|---------|
| `idx_errors_fingerprint` | UNIQUE | `fingerprint` | Fast dedup lookup on every ingest |
| `idx_errors_classification` | regular | `(classification, remediation_status)` | Classifier queue: find UNCLASSIFIED/NEW |
| `idx_errors_last_seen` | regular | `last_seen_ts` | Recency-sorted dashboard queries |
| `idx_errors_service` | regular | `service` | Per-service error drill-down |
| `idx_errors_source` | regular | `(source, level)` | Source + severity filtering |
| `idx_errors_severity_pend` | **partial** | `(classification, fixed_by_attempt_id) WHERE fixed_by_attempt_id IS NULL` | Pending-fix queue (unfixed rows only) |

---

## Table: `fix_attempts`

One row per fix attempt. An error may generate multiple attempts (retry on
failure, tier demotion, new swarm). The row drives a **state machine**.

**Written by**: Fix worker (creates row on job start), each pipeline phase
(updates status/fields as it progresses).

**Read by**: Fix worker (self), reviewer, merger, monitor, graduation engine.

| Column | Type | Meaning | Written by |
|--------|------|---------|------------|
| `id` | INTEGER PK | Surrogate key | DB |
| `error_id` | INTEGER FK NOT NULL | Parent error row | fix worker |
| `fingerprint` | TEXT NOT NULL | Copied from errors.fingerprint (avoids join for hot queries) | fix worker |
| `started_ts` | TEXT NOT NULL | When this attempt began | fix worker |
| `finished_ts` | TEXT | When this attempt ended (NULL = in progress) | fix worker |
| `status` | TEXT | State machine position (see below) | each phase |
| `classification` | TEXT | Triage classification that spawned this attempt | classifier |
| `triage_model` | TEXT | LLM model used for triage | classifier |
| `triage_reason` | TEXT | Triage rationale text | classifier |
| `triage_tokens` | INTEGER | Token count for triage call | classifier |
| `diagnosis_model` | TEXT | LLM model used for diagnosis | diagnoser |
| `diagnosis_summary` | TEXT | Diagnosis narrative | diagnoser |
| `diagnosis_tokens` | INTEGER | Token count for diagnosis call | diagnoser |
| `fix_model` | TEXT | LLM model used to generate fix | fix generator |
| `fix_branch` | TEXT | Git branch name for the fix | fix generator |
| `fix_commit_sha` | TEXT | Git SHA of the fix commit | fix generator |
| `fix_diff_lines` | INTEGER | Line count of the diff | fix generator |
| `fix_tokens` | INTEGER | Token count for fix generation | fix generator |
| `review_model` | TEXT | LLM model used for review | reviewer |
| `review_verdict` | TEXT | `APPROVE` or `REJECT` (NULL = not yet reviewed) | reviewer |
| `review_confidence` | REAL [0.0–1.0] | Reviewer confidence score | reviewer |
| `review_reason` | TEXT | Review rationale | reviewer |
| `review_tokens` | INTEGER | Token count for review | reviewer |
| `test_results_json` | TEXT | Serialized pytest results JSON | verifier |
| `gates_passed_json` | TEXT | JSON array of gate names that passed | gate checker |
| `gates_failed_json` | TEXT | JSON array of gate names that failed | gate checker |
| `blocked_by_gate` | TEXT | Name of the first blocking gate | gate checker |
| `revert_commit_sha` | TEXT | Git SHA of the revert commit | monitor/merger |
| `revert_reason` | TEXT | Why this attempt was reverted | monitor/merger |
| `reverted_ts` | TEXT | When the revert was applied | monitor/merger |
| `monitor_outcome` | TEXT | `clean` · `reverted` · `pending` | monitor |
| `total_wall_seconds` | REAL | Total elapsed time start→finish | fix worker |
| `notes` | TEXT | Free-form notes | any agent |

**Indexes (3)**:

| Index | Columns | Purpose |
|-------|---------|---------|
| `idx_fix_attempts_status` | `(status, started_ts)` | Active-attempt queue ordered by age |
| `idx_fix_attempts_fingerprint` | `(fingerprint, started_ts)` | History for a fingerprint |
| `idx_fix_attempts_error_id` | `error_id` | FK join: all attempts for an error |

### `fix_attempts.status` State Machine

```
                    ┌──────────┐
   (job created) ──►│ triaged  │
                    └────┬─────┘
                         │
                    ┌────▼──────────┐
                    │ reproducing   │
                    └────┬──────────┘
                         │
                    ┌────▼──────────┐
                    │  diagnosing   │
                    └────┬──────────┘
                         │
                    ┌────▼──────────┐
                    │    fixing     │
                    └────┬──────────┘
                         │
                    ┌────▼──────────┐
                    │  verifying    │
                    └────┬──────────┘
                         │
                    ┌────▼──────────┐
                    │  reviewing    │
                    └───┬───┬───┬───┘
                        │   │   │
              ┌─────────┘   │   └────────────────────┐
              ▼             ▼                        ▼
          ┌────────┐   ┌─────────┐            ┌──────────┐
          │ merged │   │ blocked │            │  failed  │
          └───┬────┘   └─────────┘            └──────────┘
              │
      ┌───────▼─────────────────────────────┐
      │    monitor (30-min window post-      │
      │    merge)                            │
      └──── clean ──► done (status=merged)   │
           └─── revert ──► status=reverted   │
      └──────────────────────────────────────┘
```

**Terminal states** (cannot exit): `merged`, `reverted`, `failed`, `escalated`,
`blocked`, `aborted`.

---

## Table: `fix_audit_log`

**Append-only** record of every action by every agent/human during the
remediation lifecycle. Used for audit, cost accounting, debugging, and
post-incident review.

**Written by**: Every pipeline actor — classifier, fix worker, reviewer,
merger, monitor, budget guard, kill switch, graduation engine, human operators.

**Read by**: Dashboard, post-incident review, cost reporting, compliance.

**Immutability enforcement**:
- `fix_audit_log_no_update` trigger — `BEFORE UPDATE ON fix_audit_log` → `RAISE(ABORT, ...)`
- `fix_audit_log_no_delete` trigger — `BEFORE DELETE ON fix_audit_log` → `RAISE(ABORT, ...)`

Purge/archival is done by rebuild (`CREATE TABLE new ... INSERT ... DROP old ... RENAME`),
not in-place `DELETE`.

| Column | Type | Meaning |
|--------|------|---------|
| `id` | INTEGER PK | Auto-increment |
| `attempt_id` | INTEGER → fix_attempts.id | Associated attempt (nullable) |
| `error_id` | INTEGER → errors.id | Associated error (nullable) |
| `ts` | TEXT DEFAULT datetime('now') | When this entry was created |
| `phase` | TEXT NOT NULL | `capture` · `triage` · `reproduce` · `diagnose` · `fix` · `verify` · `review` · `gate_check` · `merge` · `monitor` · `revert` · `halt` · `resume` · `config_change` · `graduation` · `demotion` · `manual` |
| `actor` | TEXT NOT NULL | `classifier` · `fix_worker` · `reviewer` · `merger` · `monitor` · `budget` · `kill_switch` · `graduation_engine` · `human:<name>` |
| `model` | TEXT | LLM model name (NULL for non-LLM actors) |
| `decision` | TEXT | Short decision label (e.g. `AUTO_FIX`, `REJECT`) |
| `reasoning` | TEXT | Full reasoning text from LLM or human |
| `diff` | TEXT | Git diff snippet (for fix/revert phases) |
| `payload_json` | TEXT | Arbitrary JSON payload for the phase |
| `duration_sec` | REAL | Wall-clock duration of this action |
| `tokens_in` | INTEGER | LLM input tokens consumed |
| `tokens_out` | INTEGER | LLM output tokens produced |
| `cost_usd` | REAL DEFAULT 0 | Estimated cost of this action in USD |
| `result_status` | TEXT | `success` · `blocked` · `error` · `timeout` · `aborted` · NULL |
| `blocked_by_gate` | TEXT | Gate name that blocked this action |
| `notes` | TEXT | Free-form notes |

**Indexes (4)**:

| Index | Columns | Purpose |
|-------|---------|---------|
| `idx_audit_attempt_id` | `attempt_id` | All events for an attempt |
| `idx_audit_error_id` | `error_id` | All events for an error |
| `idx_audit_ts` | `ts` | Time-range queries (cost/activity reports) |
| `idx_audit_phase` | `(phase, actor)` | Phase-specific queries |

---

## Fingerprint Algorithm

```python
import hashlib, re

def normalize(msg: str) -> str:
    """Normalize message text before fingerprinting."""
    # Numbers (int/float standalone tokens)
    msg = re.sub(r'\b\d+(\.\d+)?\b', '<N>', msg)
    # Absolute paths
    msg = re.sub(r'/[^\s\'"]+', '<PATH>', msg)
    # ISO 8601 timestamps
    msg = re.sub(
        r'\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(\.\d+)?([Zz]|[+-]\d{2}:?\d{2})?',
        '<TS>', msg
    )
    # ALL_CAPS ticker symbols (2–5 uppercase letters)
    msg = re.sub(r'\b[A-Z]{2,5}\b', '<TICKER>', msg)
    return msg

def fingerprint(exc_type: str, message: str, file_path: str, line_number: int) -> str:
    raw = (exc_type or "") + normalize(message) + (file_path or "") + ":" + str(line_number or "")
    return hashlib.sha256(raw.encode()).hexdigest()[:16]
```

**Example**:
```
Input:  "KeyError: 'AAPL' not found in /root/atlas/data/cache at line 42"
After:  "KeyError: '<TICKER>' not found in <PATH> at line <N>"
Fingerprint (with exc_type="KeyError", file_path="brokers/live_executor.py", line=87):
        → sha256("KeyErrorKeyError: '<TICKER>'..."...)[:16]
```

---

## Retention Policy

| Table | Retention | Notes |
|-------|-----------|-------|
| `errors` | **90 days** for `FIXED` / `IGNORED` rows | Rows with `UNCLASSIFIED`, `ESCALATE`, or critical/novel fingerprints: **1 year** |
| `fix_attempts` | **Indefinite** | Never purge — needed for post-incident review and model training |
| `fix_audit_log` | **Indefinite** | Append-only; archive to cold storage after 1 year if volume requires |

**VACUUM cadence**: Monthly, after any bulk archival.

```sql
-- Monthly cron: purge resolved errors older than 90 days
DELETE FROM errors
WHERE remediation_status IN ('FIXED', 'IGNORED')
  AND resolved_at < datetime('now', '-90 days');

VACUUM;
```

---

## Idempotency

All DDL uses `IF NOT EXISTS`. The migration may be re-run any number of times
without modifying existing data or structure.

```bash
# Always safe to run again
python3 scripts/migrations/2026-04-29-add-errors-remediation-tables.py --apply
```
