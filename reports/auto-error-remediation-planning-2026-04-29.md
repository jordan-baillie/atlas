# Atlas Autonomous Error Remediation — Planning Report
**Team:** Planning — Strategic Planning, Research, and Specification Writing  
**Author:** Planning Architect  
**Date:** 2026-04-29  
**Status:** Proposed — Pending User Decision-Point Answers (Section 9)

---

## Executive Summary

**Recommendation:** Build a phased autonomous error-remediation pipeline for Atlas, grounded in the existing `system_log` telemetry and the `healthz_hourly.sh` auto-fix precedent, advancing from passive classification (Phase 0–1) to human-supervised ASSIST mode (Phase 2) before any autonomous writes (Phase 3+). The 60-day commit record shows ~28% of commits are bug-fix or RCA work; even a 50% automation rate on that load frees ~14% of total engineering velocity for features. The system should live inside `atlas/` — not as a new pi extension — and subsume the current ad-hoc pi-agent spawn in `healthz_hourly.sh`.

**Phase plan:** Phase 0 (2 days) builds the `errors` table and replays the 637-row historical corpus through a classifier to validate signal quality. Phase 1 (1 week) wires live capture and ships a Telegram digest with zero writes. Phase 2 (2 weeks) adds ASSIST mode: the agent proposes fix branches, humans merge. Phase 3 (ongoing, gated) enables AUTO_FIX on a narrow, user-ratified whitelist of non-trading error classes. Phase 4 (aspirational) expands the whitelist under the same explicit-ratification rule.

**Top 3 risks:**
1. **Trading-path contamination** — any AUTO_FIX that touches brokers/, risk/, live_executor.py, or position management code on live $5,189 capital. The hard NEVER list must be enforced at the classifier layer, not just in agent prompts.
2. **Circuit-breaker-tripped false signal** — 427 of 637 historical errors (67%) are "Circuit breaker tripped," which are *expected system behavior*, not bugs. If the classifier mis-tiers these as ASSIST/AUTO_FIX, the system generates enormous noise from day 1.
3. **Agent quality too low for ASSIST** — if Phase 2 fix acceptance rate is <40%, the ASSIST mode adds overhead without value; the project must have a clearly defined kill/stay decision at Phase 2 end.

**Decision points that must be answered before Phase 2 begins:** (1) maximum daily auto-fix budget; (2) user-ratified AUTO_FIX whitelist; (3) which error classes stay ASSIST-forever; (4) Telegram cadence; (5) reviewer identity; (6) regression-triggered pause threshold; (7) Phase 3 kill criteria. Full question list in Section 9.


---

## 1. Problem Framing

### Why Now?

Three conditions have simultaneously matured, making this the right moment.

**Commit volume:** In the last 60 days Atlas accumulated 945 commits, of which ~270 carry fix/rca/urgent/RCA/URGENT prefixes — 28.5% of total volume. The prompt's cited figure of 253/945 (~27%) is consistent with a more conservative count. By either measure, over a quarter of engineering output is reactive bug-fixing, not forward progress. This volume is high enough to justify tooling investment.

**Telemetry maturity:** `data/atlas.db` has a `system_log` table with 4,385 rows and 637 errors already logged, spanning services from `live_executor` to `eod_settlement` to `strategy_health`. The error taxonomy is stable enough to train a classifier. The data is already there — this is not a data-collection problem, it is a classification and routing problem.

**Precursor pattern validated:** `healthz_hourly.sh` already spawns a pi agent for issue remediation (Step 5, ~line 145 of the script). The pattern — detect → triage → spawn Claude → fix — works in production. The current implementation is hardcoded in bash with a flat prompt, no structured triage, and no persistent memory. The planned system formalizes and extends it.

### What "Done" Looks Like Per Phase

| Phase | Definition of Done |
|-------|-------------------|
| Phase 0 | `errors` table populated; 30-day historical corpus classified; ≥70% IGNORE/ESCALATE precision confirmed on known-safe errors |
| Phase 1 | Live capture running; daily Telegram digest shipping; zero false ESCALATE in dry-run week; 60-day commit corpus validated |
| Phase 2 | ASSIST fix acceptance rate ≥40%; mean time to propose fix <30 min; false-positive rate <10% |
| Phase 3 | AUTO_FIX whitelist contains ≥5 error classes; regression rate on AUTO_FIX commits = 0% over 30 days |
| Phase 4 | Whitelist expanded; human review shifted to statistical sampling rather than 100% |

### Opportunity Cost

Building this displaces roughly 2–3 weeks of senior engineering time. Primary displaced items:

- **orchestrator.py shadow-mode completion** (101-line scaffold, blocked on Phase B.2 cutover gate)
- **trade_state_machine.py Phase C** (100-line scaffold, waiting on #192 + Phase B.2)
- **Phase 5 research gaps** (#216–221, research pipeline intentionally disabled)
- **#192 kill JSON dual-write** (gate approaching closure)
- **Caddy basicauth rotation** (#260, low-urgency)

The sequencing argument: orchestrator.py and trade_state_machine.py are both blocked on external gates that haven't cleared yet — the 2–3 week build fills that window productively. Auto-remediation has no blockers. Quantitatively: 270 fix/rca commits in 60 days = 4.5/day × 2h avg = 9 engineer-hours/day reactive. At 40% automation on the non-NEVER-list subset (roughly 50% of all fixes) = 1.8 hours/day freed = 108 hours over 60 days. Build cost is ~80–120 hours. Net payback in ~6 weeks.

### Cost of NOT Building This

Three compounding costs: (1) **Accumulating lint debt** — 839 bare-excepts at baseline with no systematic sweep; (2) **Latent errors staying silent** — the 2 strategy_health degradation warnings and 3 eod_settlement broker failures in `system_log` sit unaddressed without a human actively watching; (3) **Test isolation failures recur** — commits `4ea328fa` (URGENT: isolate live_*.json in tests) and `dede8d62` (URGENT: isolate kill_switch HALT file) are exactly the class Phase 3 AUTO_FIX would handle automatically.

---

## 2. Architecture Overview

### Pipeline

```
CAPTURE → TRIAGE → FIX → VERIFY → SHIP → MONITOR
```

1. **Capture:** Python logging handler in-process (primary) + journald tailer (secondary) + healthz.py JSON pipe (tertiary). Writes to `errors` table.
2. **Triage:** Classifier assigns tier: AUTO_FIX / ASSIST / ESCALATE / IGNORE. Phase 0–1: rule-based. Phase 2+: rules + LLM for ambiguous cases.
3. **Fix:** AUTO_FIX → agent generates patch, runs tests, commits. ASSIST → agent generates branch, stops for human review.
4. **Verify:** `pytest -x --timeout=30` + NEVER-list diff scan. Must pass before any commit lands.
5. **Ship:** Human-gated for ASSIST; automated for AUTO_FIX with passing gate.
6. **Monitor:** Regression check 1h post-deploy. New errors in same service → Telegram alert.

### Three Buckets of Error Sources

| Bucket | Source | Examples | Capture Method |
|--------|--------|---------|----------------|
| **Structured** | Python `logging` → `system_log` | live_executor circuit breaker trips, eod_settlement broker failures, strategy_health degradation | In-process handler: intercepts WARNING/ERROR/CRITICAL, writes to `errors` table |
| **Semi-structured** | journald / systemd service logs | Service OOM kills, startup failures, timer expirations | journald poller: `journalctl -u 'atlas-*' --since=<last_poll>` every 15 min |
| **Unstructured** | healthz.py JSON output, healthz_hourly.sh stdout | "Ledger integrity tests FAILED", dashboard dist stale, reconcile drift | Parse healthz.py `--json` output after each hourly run; pipe non-ok checks into `errors` table |

### Design Choice: Extend `system_log` vs New `errors` Table

**Recommendation: New `errors` table.**

`system_log` is write-only telemetry. The `errors` table is a workflow state machine — it needs `fix_status`, `agent_branch`, `fix_commit`, `triage_tier`, `confidence`, `resolved_at`, and `suppressed`. Mixing workflow state into a telemetry table creates read/write contention and 4,385 rows of NULL noise in every workflow column. The `errors` table is populated by a background process that reads `system_log` and enriches each row. A `UNIQUE(source_table, source_id)` constraint prevents duplicate inserts.

### Triage Tiers

| Tier | Definition | Trigger Conditions | Human in Loop? |
|------|-----------|-------------------|----------------|
| **AUTO_FIX** | Agent commits autonomously | Error class on user-ratified whitelist; confidence ≥85%; NEVER-list diff clean | No — post-hoc monitoring only |
| **ASSIST** | Agent proposes branch; human merges | Known domain, not whitelisted; OR whitelist class with confidence 60–85% | Yes — merge gate |
| **ESCALATE** | Immediate Telegram; human must act | Touches trading path; unknown class; confidence <60% on real failure; CRITICAL | Yes — blocking |
| **IGNORE** | Suppress | Expected behavior: circuit breaker trips, execution-blocked logs, reconcile confirmations | No |

**Critical finding from historical data:** 427 of 637 errors (67%) are "Circuit breaker tripped" — correct safety mechanism behavior, not bugs. Another ~175 (~27%) are "Execution blocked: Plan status is..." — expected flow control. **94% of historical errors should tier IGNORE.** Only ~40 rows (~6%) are actionable. The classifier's primary job is getting IGNORE right; misclassifying circuit-breaker trips generates hundreds of false proposals per day.

### Where Should This Live?

**Recommendation: `atlas/core/auto_remediation/` as a Python service. Not a new pi extension.**

The pi extension system is for CLI tooling. Auto-remediation needs direct SQLite access, filesystem access for git worktrees, and integration with existing systemd timers. The atlas-jobs scaffold at `/root/.pi/extensions/atlas-jobs/` is confirmed empty (no files, no package.json, not installed). Using it would mean building extension infrastructure before building the actual feature. The correct home is `atlas/core/auto_remediation/`, scheduled via `atlas-auto-remediation.timer`, consistent with every other Atlas subsystem.


---

## 3. Phased Rollout

### Phase 0 — Foundation: Error Capture and Historical Replay (Days 1–2)

**Goal:** Create the `errors` table, backfill the 30-day `system_log` corpus, and validate classifier precision before any live writes.

**Deliverables:**
1. `core/auto_remediation/__init__.py` — module scaffold
2. `core/auto_remediation/errors_db.py` — `errors` table accessor (create, insert, query, update_status)
3. `db/migrations/005_errors_table.sql` — migration:

```sql
CREATE TABLE IF NOT EXISTS errors (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id        INTEGER,
    source_table     TEXT    DEFAULT 'system_log',
    timestamp        TEXT    NOT NULL,
    level            TEXT    NOT NULL,
    service          TEXT    NOT NULL,
    message          TEXT    NOT NULL,
    detail_json      TEXT,
    triage_tier      TEXT,
    confidence       REAL,
    fix_status       TEXT    DEFAULT 'OPEN',
    agent_branch     TEXT,
    fix_commit       TEXT,
    never_list_clear INTEGER DEFAULT 1,
    suppressed       INTEGER DEFAULT 0,
    created_at       TEXT    DEFAULT (datetime('now')),
    resolved_at      TEXT,
    UNIQUE(source_table, source_id)
);
CREATE INDEX IF NOT EXISTS idx_errors_triage   ON errors(triage_tier, fix_status);
CREATE INDEX IF NOT EXISTS idx_errors_service  ON errors(service, timestamp);
CREATE INDEX IF NOT EXISTS idx_errors_created  ON errors(created_at);
```

4. `core/auto_remediation/classifier.py` — rule-based classifier (Phase 0: rules only, no LLM; LLM added Phase 2)
5. `scripts/replay_errors_classifier.py` — one-shot replay of 30-day `system_log` corpus; outputs tier distribution + precision metrics
6. `reports/phase0-classifier-validation-YYYY-MM-DD.md` — saved output of replay run, signed off by user

**Effort estimate:** 12–16 engineer-hours

**Success criteria:**
- Migration runs clean on `data/atlas.db` with no constraint violations
- Replay classifies ≥70% of historical errors as IGNORE (floor; empirical baseline is ~94%)
- Zero ESCALATE or AUTO_FIX assignments for "Circuit breaker tripped" or "Execution blocked: *"
- Classifier precision on ~40 actionable historical errors: ≥80% correctly tiered ESCALATE or ASSIST

**Exit criteria:** Replay validation report reviewed and signed off by user. ≥80% precision on actionable errors confirmed.

**Must be true before Phase 0 ships:**
- `data/atlas.db` restic snapshot confirmed <24h old
- NEVER list hard-coded in `classifier.py`: brokers/, risk/, kill_switch.py, live_executor.py, regime/, signals/, plan*.py, approve*.py, broker_orders, position_protective_orders, config/active/
- Phase 0 is read-only DB work — no connection to live trading systems

**Kill criteria:** If classifier precision on actionable errors is <70% after 2 full revision attempts, stop. The error taxonomy may be too noisy for automation without a labeled training corpus. Keep `healthz_hourly.sh` as-is.

---

### Phase 1 — Capture + Monitor + Classify, No Writes (Days 3–9)

**Goal:** Wire live error capture, run the classifier continuously, and ship a daily Telegram digest — proving triage quality on fresh errors before touching any code.

**Deliverables:**
1. `core/auto_remediation/capture.py` — in-process Python logging handler + journald poller (15-min cadence); writes to `errors` table
2. `core/auto_remediation/digest.py` — daily Telegram digest renderer: grouped by tier, count per service, top 3 ESCALATE items
3. `systemd/atlas-auto-remediation.timer` + `.service` — 15-min capture + classify loop
4. `systemd/atlas-error-digest.timer` + `.service` — daily 08:00 AEST digest
5. **Updated `healthz_hourly.sh`** — add `errors` table write after Step 3 (issue extraction); keep existing pi-agent spawn as fallback through Phase 2
6. Dashboard widget (stretch): `errors` summary via new `/api/errors/summary` endpoint

**Effort estimate:** 20–28 engineer-hours

**Success criteria:**
- ≥5 consecutive days of live capture with zero unhandled exceptions in `atlas-auto-remediation.service`
- Daily digest delivered without manual intervention for ≥5 days
- Retrospective validation: for 60-day bug-fix commits where a `system_log` error exists, ≥70% correctly tiered ASSIST or ESCALATE
- Zero false ESCALATEs for known-IGNORE patterns during the dry-run week
- Telegram volume does not exceed pre-remediation baseline

**Exit criteria:** 5-day dry-run complete. User reviews digest quality and signs off. Explicit go/no-go decision on Phase 2.

**Must be true before Phase 1 ships:**
- Phase 0 validation signed off
- Circuit breaker reused from `healthz_hourly.sh`: `BREAKER_FILE`, 18,000s window, `claude_circuit_breaker.trip()` on usage exhaustion
- Dedup rate-limiting: max 50 errors/hour; dedup by `md5(message + service)` with 4h cooldown (matching `healthz_hourly.sh` pattern)
- OAuth routing verified: every `pi` subprocess call includes `--system-prompt "You are Claude Code, Anthropic's official CLI for Claude."` — grep check enforced
- SQLite WAL mode confirmed for `data/atlas.db` (prevents write contention)
- New timer coexists with — does NOT replace — `atlas-silent-failure-watchdog.timer` and `atlas-heartbeat-watchdog.timer`

**Kill criteria:** If live capture causes `data/atlas.db` write contention (the monitoring system itself generates errors), pause immediately. If digest delivery fails 3 consecutive days due to infrastructure (not agent quality) issues, fix infrastructure first.

---

### Phase 2 — ASSIST Mode: Agent Proposes Fixes, Human Merges (Weeks 2–3)

**Goal:** For ASSIST-tiered errors, have the agent generate a fix branch automatically. Measure acceptance rate and time-to-proposal before enabling any autonomous commits.

**Deliverables:**
1. `core/auto_remediation/agent.py` — wraps `pi --system-prompt ... --skill atlas-incident --skill atlas-codebase` subprocess; takes an `errors` row, builds fix prompt, creates git worktree, updates `errors` table with `fix_status=PROPOSED` and `agent_branch`
2. `core/auto_remediation/prompt_templates/assist.md` — structured fix prompt (error context + NEVER list + verification instructions + branch naming `autofix/YYYY-MM-DD-<error-hash>`)
3. `core/auto_remediation/worktree.py` — `create_worktree()`, `remove_worktree()`, `run_tests_in_worktree()` helpers
4. `core/auto_remediation/verify.py` — post-fix verification: runs `pytest -x --timeout=30` in worktree; scans diff for NEVER-list files; reports pass/fail
5. `systemd/atlas-auto-remediation-assist.service` — triggered by new ASSIST-tiered errors (polled every 15 min)
6. Telegram per ASSIST proposal: "🔧 ASSIST fix ready: `<branch>` — `<error summary>`. Review: `git diff main <branch>`"
7. `scripts/accept_fix.sh <branch>` — one-command merge-and-verify helper
8. Weekly ASSIST metrics report written to `reports/assist-metrics-YYYY-WNN.md`

**Effort estimate:** 32–48 engineer-hours

**Success criteria (must hold over ≥2 full weeks):**
- Fix acceptance rate ≥40%
- False-positive rate <15% (proposed fixes that introduce new test failures)
- Mean time to propose fix <30 minutes from error first written to `errors` table
- Zero ASSIST fix branches touching any NEVER-list file (must be 100%)
- Agent produces runnable Python in ≥80% of proposals (`python3 -m py_compile` clean on touched files)

**Exit criteria:** 2-week live run complete. All metrics met. User explicitly ratifies the Phase 3 AUTO_FIX whitelist (Section 9 decision points #1–3). Both conditions required.

**Must be true before Phase 2 ships:**
- All 7 Section 9 decision-point questions answered by the user
- ASSIST scope hard-limited to non-trading services only; NO ASSIST on NEVER-list files, `config/active/*.json`, or strategy parameters
- `git worktree add` tested and confirmed working in the Atlas repo
- Rollback procedure documented: `git branch -D autofix/<hash>` + `git revert <commit>` for any merged fix

**Kill criteria:**
- Acceptance rate <20% after 2 weeks → ASSIST mode adds overhead without value; kill Phase 3 planning, keep Phase 1 as permanent observability win
- Any ASSIST fix touches a NEVER-list file → immediate halt, root-cause, add regression test before reopening
- More than 2 test-suite regressions from accepted ASSIST fixes in a single calendar week → pause ASSIST, review prompt templates

---

### Phase 3 — AUTO_FIX: Narrow Whitelist, Gated Expansion (Ongoing)

**Goal:** Enable zero-human-touch fixes for a narrow, explicitly ratified set of error classes where blast radius is confined to non-trading code.

**Initial AUTO_FIX candidate whitelist (requires explicit user ratification):**

| Error Class | Source | Rationale for AUTO_FIX |
|-------------|--------|------------------------|
| `test_fixture_stale` — stale ticker/date references in test files | pytest output | Zero trading-path exposure; mechanical fix |
| `lint_bare_except` — bare-excepts in non-trading modules | ast-lint scan | 839 baseline; deterministic AST transform |
| `dashboard_build_failure` — React/Vite build errors | atlas-dashboard-refresh.service | Read-only display; no trading impact |
| `doc_comment_error` — errors in documentation files | doc files only | Zero runtime impact |
| `pycache_accumulation` | healthz.py infra check | Already bash-handled; formalizing existing behavior |
| `large_log_rotation` | healthz.py infra check | Already bash-handled; formalizing existing behavior |
| `stale_lock_file` | healthz.py infra check | Already bash-handled; formalizing existing behavior |

**Deliverables:**
1. `core/auto_remediation/whitelist.py` — explicit registry; `WHITELIST_VERSION` integer incremented on every change; justification comment required per entry
2. `core/auto_remediation/scope_guard.py` — pre-flight NEVER-list diff scan; any match → downgrade to ASSIST or ESCALATE; must be 100% coverage
3. Updated `agent.py` — AUTO_FIX path: runs verify + tests; commits to main only on passing gate; logs commit hash to `errors.fix_commit`
4. `scripts/whitelist_ratify.py` — interactive CLI: shows proposed entry with sample errors + ASSIST acceptance history; requires explicit `y/N`; writes to `whitelist.py`
5. Post-fix monitoring: regression check 1h after every AUTO_FIX commit; anomaly → immediate Telegram ESCALATE
6. AUTO_FIX activity summary in daily digest
7. Removal of pi-agent spawn (Step 5) from `healthz_hourly.sh` — replaced by the new pipeline (Phase 3 only)

**Effort estimate:** 20–30 engineer-hours for infrastructure; ~2 hours marginal per new whitelist entry

**Scope ratchet (primary safety control):** The agent can propose whitelist additions after observing ≥5 consecutive ASSIST acceptances for an error class, but cannot self-modify the whitelist. Only `whitelist_ratify.py` writes to `whitelist.py`. Quarterly reviews: entries can be removed instantly; additions always require deliberate ratification.

**Success criteria:**
- 30 days of AUTO_FIX with regression rate = 0%
- Scope guard blocks all NEVER-list file touches (100%)
- Whitelist additions have explicit ratification in git log
- AUTO_FIX reduces ASSIST queue depth by ≥30% within first month

**Kill criteria:**
- Any AUTO_FIX commit introduces a regression → halt all AUTO_FIX pending root cause
- >3 scope-guard violations in any 30-day window → shut down Phase 3, return to ASSIST indefinitely
- User ratifies fewer than 3 whitelist entries post-Phase 2 → insufficient scope; stay at ASSIST

---

### Phase 4 — Whitelist Expansion (Aspirational, May Never Ship)

**Goal:** Expand AUTO_FIX to infra-level errors (non-trading service crashes, data pipeline failures) as confidence accumulates over 90+ days of Phase 3.

**Always-NEVER list — permanent, non-negotiable regardless of confidence:**
```
brokers/, risk/, kill_switch.py, live_executor.py, regime/, signals/,
plan*.py, approve*.py, broker_orders, position_protective_orders,
config/active/*.json (all active market configs)
```

**Realistic Phase 4 expansion candidates:**
- eod_settlement broker-connection retry logic (retry mechanism, not trading logic)
- atlas-dashboard service restarts
- reconcile_positions non-fix-mode read failures
- Research pipeline errors (atlas-discovery, atlas-director — fully disabled already)

**This phase may never ship.** Phase 3 is the likely terminal state. Phase 4 is only worth pursuing if Phase 3 has operated cleanly for ≥90 days with zero regressions AND the user explicitly decides to expand scope.


---

## 4. Sequencing Dependency Graph

```
Phase 0 foundations:
  [DB migration: errors table]    ─────────────────────────────┐
  [Rule-based classifier v1]      ─────────────────────────────┤
  [Historical replay script]      ──── depends on ────────────▶ [Phase 0 validation]
                                                                          │
                                                                          ▼
Phase 1 (parallel components, all depend on Phase 0 sign-off):  [Phase 0 sign-off]
  [In-process logging handler]    ──────────────┐                        │
  [Journald poller]               ──────────────┤                        │
  [healthz.py → errors pipe]      ──────────────┤    depends on ─────────▼
  [Telegram digest service]       ──────────────┤──────────────▶ [Phase 1 live]
  [systemd timers ×2]             ──────────────┘                        │
                                                              5-day dry-run passes
                                                              + Section 9 answers received
                                                                          │
                                                                          ▼
Phase 2 (parallel components, all depend on Phase 1 sign-off):  [Phase 1 sign-off]
  [agent.py fix generator]        ──────────────┐                        │
  [worktree.py isolation]         ──────────────┤                        │
  [verify.py test runner]         ──────────────┤    depends on ─────────▼
  [scope_guard.py NEVER check]    ──────────────┤──────────────▶ [Phase 2 ASSIST live]
  [accept_fix.sh helper]          ──────────────┘                        │
                                                              2-week metrics gate
                                                              + whitelist ratified by user
                                                                          │
                                                                          ▼
Phase 3 (depends on Phase 2 sign-off + user ratification):      [Phase 2 sign-off]
  [whitelist.py registry]         ──────────────┐                        │
  [whitelist_ratify.py CLI]       ──────────────┤    depends on ─────────▼
  [scope_guard hardening]         ──────────────┤──────────────▶ [Phase 3 AUTO_FIX]
  [post-fix regression monitor]   ──────────────┘
```

**Critical path:** DB migration → classifier → replay → sign-off → live capture → 5-day dry-run → agent fix generator → 2-week ASSIST → sign-off + whitelist ratification → AUTO_FIX. Every link is sequential; none can be skipped.

**Work that can run in parallel:**
- Phase 1 dry-run window: Dashboard widget (`/api/errors/summary`) and Phase 2 prompt template drafting
- Phase 2 live window: `whitelist_ratify.py` CLI and `whitelist.py` registry skeleton (build but don't activate)
- Anytime: `git worktree` isolation test — can be validated in a single afternoon, independent of capture work

**Identify the critical path bottleneck:** The Section 9 decision-point answers are the single most likely delay. If the user answers all 7 questions during Phase 1's 5-day dry-run, the critical path has no artificial delays. If answers come after Phase 1 sign-off, Phase 2 is delayed by the answer latency. The planning team should surface Section 9 questions to the user at Phase 1 day 1, not Phase 1 day 5.

---

## 5. Opportunity Cost Analysis

### Displaced Work

| Work Item | Blocked? | Approx. Effort | Displacement Impact |
|-----------|----------|---------------|---------------------|
| orchestrator.py shadow mode (Phase C.1) | Yes — Phase B.2 gate | ~3 weeks | **Low:** cannot proceed until 7-day validation closes |
| trade_state_machine.py Phase C | Yes — #192 + B.2 | ~4 weeks | **Low:** same blockers; deferral costs nothing extra |
| #192 kill JSON dual-write | No — gate closing | ~1 week | **Medium:** begin immediately after Phase B.2 closes; coexists with Phase 0 |
| Phase 5 research gaps (#216–221) | No | ~3 weeks | **Low:** research pipeline disabled; no compounding cost |
| Caddy basicauth rotation (#260) | No | ~2 days | **Low-urgency:** operational hygiene; fits in spare time |

### Why Auto-Remediation Wins the Sequencing Argument

- **Unique timing window:** orchestrator.py and trade_state_machine.py are blocked. The 2–3 week build fills that window productively.
- **Compounding tax reduction:** Every day without auto-remediation, the bug-fix queue grows. The longer we wait, the higher the inherited backlog.
- **No external dependencies:** Unlike Phase C (broker sub-account decisions) or Phase 5 (research restart decision), auto-remediation begins unilaterally tomorrow.
- **The precursor already works:** `healthz_hourly.sh` proves the Claude-based fix pattern. This is not speculative.

### Quantitative Case

```
Current reactive burden (60-day observed data):
  270 fix/rca commits ÷ 60 days = 4.5 commits/day
  × 2h avg per commit (diagnosis + fix + test + review)
  = 9 engineer-hours/day in reactive mode

With 40% automation on non-NEVER-list subset (~50% of all fixes):
  0.40 × 0.50 × 9h/day = 1.8 engineer-hours/day freed

Build investment: ~80–120 engineer-hours total (Phases 0–3)
Payback period: 100h ÷ 1.8h/day = ~56 days ≈ 8 weeks

At 90-day horizon: net gain ≈ (90 − 56) × 1.8h = +61 engineer-hours
At 180-day horizon: net gain ≈ (180 − 56) × 1.8h = +223 engineer-hours
```

These figures exclude the standalone value of Phase 1's observability win (structured error digest + capture), which delivers real benefit regardless of whether ASSIST or AUTO_FIX ever ships.

### Cost of Not Building This

- **Lint debt compounds:** 839 bare-excepts → no systematic sweep → 900, 1000, 1200 as the codebase grows
- **Latent errors stay silent:** strategy_health degradation (2 recent rows), eod_settlement broker failures (3 rows) — each sits in `system_log` until a human notices it in the raw Telegram log
- **Reactive mode is the default forever:** The current `healthz_hourly.sh` pi-agent spawn is ad-hoc, stateless, and blind to everything outside its hardcoded issue patterns

---

## 6. Failure Modes at the Strategic Level

### FM-1: ASSIST Agent Quality Too Low

**Signal:** Phase 2 fix acceptance rate falls below 20% after 2 full weeks.

**Strategic implication:** The agent is generating noise, not value. ASSIST mode adds review overhead without reducing manual fix load. Atlas code has domain-specific patterns — state machine logic, equity attribution formulas, broker race condition guards — that a general-purpose agent may generate plausible-but-wrong fixes for.

**Response:** Kill Phase 3 permanently. Keep Phase 1 (capture + classify + digest) as a permanent observability win with standalone value. Document in `tasks/lessons.md`: agent-assisted code generation in this domain requires richer context injection (GitNexus impact analysis, full execution flow context). Do not retry Phase 2 by tuning prompts alone — the fundamental variable is context richness, not prompt wording.

### FM-2: AUTO_FIX Scope Expands Too Aggressively

**Signal:** Agent proposes whitelist additions faster than they should be ratified; user approves permissively under time pressure; scope creeps into borderline classes.

**Response:** The scope ratchet (`whitelist_ratify.py`) is the primary control — additions require deliberate keyboard action. Secondary: quarterly whitelist reviews are scheduled from Phase 3 day 1. Removals are instant; additions require effort. If the user finds themselves approving every proposal without deliberation, that's a yellow flag requiring a whitelist review meeting, not a technical fix.

### FM-3: Agent Becomes Load-Bearing for Engineering Velocity

**Signal:** Team stops writing manual fixes for AUTO_FIX-whitelisted error classes; then an OAuth exhaustion event or Claude API outage causes an unexpected cliff.

**Response:** Hard halt switch: a single file `/root/atlas/config/auto_remediation_enabled.json` with `{"enabled": false}` disables all auto-fix behavior immediately, routing all errors to ESCALATE. The system must be designed from day 1 such that disabling it for a week has zero operational impact — it is an accelerator, not a dependency. The existing bash fixes in `healthz_hourly.sh` (pycache, log rotation, lock cleanup) remain operational regardless, since they are pre-agent in the pipeline.

The circuit breaker already in `healthz_hourly.sh` (18,000s / 5h cooldown) should be reused verbatim — do not build a second pattern.

### FM-4: Cost Runs Away

**Signal:** Daily `pi` subprocess call count exceeds comfortable range; OAuth circuit breaker trips repeatedly; compute time accumulates.

**Response:** Hard rate limit in `agent.py`: max 5 `pi` subprocess calls per hour. OAuth routing audit on every new call site — grep gate must return empty for any `pi` subprocess without `--system-prompt`. Daily activity auditable via:
```sql
SELECT DATE(created_at), COUNT(*)
FROM errors
WHERE fix_status IN ('PROPOSED', 'RESOLVED')
GROUP BY DATE(created_at);
```
Per `/root/AGENTS.md`: only `--system-prompt` routing is $0 marginal cost. Any missing `--system-prompt` flag burns paid credits silently.

---

## 7. Integration with Existing Systems

### Relationship to `healthz_hourly.sh`

`healthz_hourly.sh` does far more than error remediation: Cronus heartbeat checks (via `agent_heartbeats` table), restic backup verification, ledger integrity test runs (`test_ledger_integrity.py`), SuperCoach API reachability, stale lock cleanup, dashboard dist staleness, signal-write divergence detection (`check_signal_writes.py`), and overlay evaluator backlog checks. These monitoring concerns remain in `healthz_hourly.sh` permanently — they are health assertions, not error-remediation actions.

The one element to subsume is **Step 5: the pi-agent spawn** (`timeout 300 pi -p --system-prompt ... "$PROMPT"`). This is currently a fire-and-forget bash spawn with a flat 800-word prompt, no triage, and no persistent state. The new pipeline replaces it with the `errors` table-backed ASSIST/AUTO_FIX pipeline.

**Migration path:**
- **Phase 1:** `healthz_hourly.sh` writes detected issues into `errors` table after Step 3. Pi-agent spawn kept as fallback.
- **Phase 2:** Pi-agent spawn kept; new system handles ASSIST proposals in parallel. Observe which produces better results.
- **Phase 3:** Remove pi-agent spawn from Step 5. `atlas-auto-remediation.service` is the sole handler. `healthz_hourly.sh` becomes a pure monitoring script.

**Do NOT** refactor `healthz_hourly.sh` before Phase 3. It is 300+ lines of accumulated hardening (4h cooldown logic per-issue-hash, drift detection, circuit-breaker integration, multi-market reconcile, multiple hard-coded safety guards). Incremental integration is safer than replacement.

### Relationship to Multi-Team Extension

The multi-team extension (`/root/.pi/extensions/multi-team/index.ts`, ~1,566 lines) is the team orchestration layer for interactive PI CLI sessions. The auto-remediation system does not need to be a pi extension. However, when `agent.py` spawns a `pi` subprocess to generate a fix, it should use the atlas-ops skill set exactly as `healthz_hourly.sh` does:

```bash
timeout 300 pi -p \
  --system-prompt "You are Claude Code, Anthropic's official CLI for Claude." \
  --no-session --model anthropic/claude-sonnet-4-6 \
  --skill "$SKILLS_ROOT/atlas-incident" \
  --skill "$SKILLS_ROOT/atlas-state-queries" \
  --skill "$SKILLS_ROOT/atlas-codebase" \
  "$FIX_PROMPT"
```

This gives the fix agent Atlas-specific context without a full multi-team orchestrated session. The multi-team extension becomes relevant only if Phase 4 warrants a dedicated Planning/Engineering/Validation review loop for whitelist additions.

### Relationship to Atlas-Jobs Extension

Confirmed empty (`ls /root/.pi/extensions/atlas-jobs/` → empty, no package.json, not installed). Do not use for this project. If atlas-jobs is built later as a job queue, the `errors` table structure is compatible with surfacing entries as queue items — a future integration, not a current dependency.

### Error Event Source Architecture (Final Recommendation)

**Primary: Python logging handler in-process.** Adding a background forwarder from `system_log` to `errors` (polling every 15 min for new WARNING/ERROR/CRITICAL rows) is a ~30-line change with no performance impact on the hot logging path.

**Secondary: journald poller.** `journalctl -u 'atlas-*' --output=json --since=<last_poll>` every 15 minutes catches service-level failures that don't reach Python logging. Volume is low (17 active timers, most healthy) — belt-and-suspenders, not primary signal.

**Tertiary: healthz.py JSON pipe.** After each hourly `healthz_hourly.sh` run, parse the `--json` output and write non-ok checks into `errors`. No new instrumentation required — the JSON is already structured and parsed in `healthz_hourly.sh`'s Step 3.

### Existing Timer Inventory (Must Not Be Disrupted)

| Timer | Frequency | Notes |
|-------|-----------|-------|
| `atlas-heartbeat-watchdog.timer` | Every 15 min | Cronus-specific; separate domain |
| `atlas-silent-failure-watchdog.timer` | Hourly | Research pipeline specific |
| `healthz_hourly.sh` (cron) | Hourly | Subsumed in Phase 3 only |
| `atlas-dashboard-refresh.timer` | Hourly | Display only |
| `atlas-canary-check.timer` | Hourly | Separate canary |
| `atlas-backup.timer` | Daily ~04:00 AEST | Backup verification |

The new `atlas-auto-remediation.timer` runs every 15 minutes, staggered from the heartbeat watchdog.


---

## 8. Recommended Next 3 Actions (Phase 0 Starting Moves)

### Action 1: Create `errors` Table and Migration (4 hours)

**Files to create:**
- `db/migrations/005_errors_table.sql` — schema as specified in Phase 0 deliverables
- `core/auto_remediation/__init__.py` — empty module init
- `core/auto_remediation/errors_db.py` — accessor: `create_table()`, `insert_error()`, `update_status()`, `get_pending()`, `mark_suppressed()`

**Critical design requirements:**
- `UNIQUE(source_table, source_id)` constraint prevents duplicate inserts from repeated polling
- `never_list_clear INTEGER DEFAULT 1` column — set to 0 by scope_guard if any NEVER-list file appears in a diff; auto-downgrades tier to ESCALATE regardless of classifier output
- The `errors` table is append-only for source rows; status updates modify in place; nothing in `system_log` is modified

**Verification:** Run `python3 db/migrations/005_errors_table.sql` against a copy of `data/atlas.db`; verify all indexes exist; verify UNIQUE constraint rejects duplicate `(source_table, source_id)`.

**Effort:** 4 hours

---

### Action 2: Rule-Based Classifier and Historical Replay (8 hours)

**Files to create:**
- `core/auto_remediation/classifier.py`
- `scripts/replay_errors_classifier.py`

The classifier's highest-priority rules — correct before any others:

```python
# NEVER-list services — always ESCALATE regardless of message
ESCALATE_SERVICES = frozenset({
    "live_executor", "kill_switch", "broker", "risk",
})

# Expected behavior — always IGNORE (commit 427 false fixes waiting to happen)
ALWAYS_IGNORE_PATTERNS = [
    r"Circuit breaker tripped",              # 427 historical rows
    r"Execution blocked: Plan status is",    # 35+ rows each variant
    r"Execution blocked: HALTED",            # 33 rows
    r"Execution blocked: Not connected",     # 32 rows
]

# Real failures in non-trading services — ASSIST
ASSIST_PATTERNS = [
    r"Broker connection failed",    # eod_settlement — not live trading
    r"has been DEGRADED",           # strategy_health — monitoring alert
]
```

The replay script reads `system_log` for the last 30 days, classifies each row, and prints:
```
Tier Distribution (last 30 days):
  IGNORE:    427 rows  (67.1%) — circuit_breaker: 427
  IGNORE:    175 rows  (27.5%) — execution_blocked: 175
  ESCALATE:    5 rows  ( 0.8%) — broker_failures: 3, strategy_degraded: 2
  ASSIST:      3 rows  ( 0.5%) — eod_settlement_crash: 3
  UNCLASSIFIED: N rows — (requires LLM, added Phase 2)

Precision on actionable errors: XX% (target ≥80%)
False ESCALATE on IGNORE-class errors: 0 (target = 0)
```

Output saved to `reports/phase0-classifier-validation-YYYY-MM-DD.md`.

**Effort:** 8 hours (classifier logic + full test coverage for each rule + replay runner)

---

### Action 3: Git Worktree Isolation Validation (3 hours)

Before Phase 2 is specced in detail, validate that `git worktree add` works correctly in the Atlas repo. Some repos with hooks or unusual configurations have edge cases that should be discovered now, not during Phase 2 build.

**Concrete test procedure:**
```bash
cd /root/atlas
git worktree add /tmp/atlas-autofix-test -b autofix/test-$(date +%s)
cd /tmp/atlas-autofix-test

# Run a non-destructive test
python3 -m pytest tests/test_ledger_integrity.py -x --timeout=30 -q 2>&1

# Verify main working directory untouched
cd /root/atlas
git diff HEAD   # must be empty
git status      # must be clean

# Cleanup
git worktree remove /tmp/atlas-autofix-test --force
git branch -D autofix/test-*
```

**File to create:** `core/auto_remediation/worktree.py` with:
- `create_worktree(branch_name: str, base_dir: str = '/tmp') -> Path`
- `remove_worktree(path: Path) -> None`
- `run_tests_in_worktree(path: Path, test_args: list[str]) -> tuple[int, str]`

Document any Atlas-specific worktree behavior discovered (hook behavior, submodule handling).

**Effort:** 3 hours (test + helpers + integration test)

---

**Total Phase 0 effort: ~15 hours (1.5–2 engineer-days)**

---

## 9. Decision Points the User Must Answer (Gate for Phase 2)

These seven questions must be explicitly answered before Phase 2 ASSIST mode is activated. No defaults will be assumed by Engineering. The Planning team recommends surfacing these questions during Phase 1 day 1, not waiting until Phase 1's end.

**1. Maximum auto-fix budget per day?**

Candidate proposal (for user ratification):
- AUTO_FIX commits: max 10/day
- ASSIST proposals: max 20/day
- `pi` subprocess invocations: max 5/hour
- Telegram notifications: digest (1/day) + ESCALATE alerts (unbounded) + ASSIST proposals (per-fix)

Is this acceptable? Should there be a wall-clock compute-time limit per invocation (currently: `timeout 300` in `healthz_hourly.sh`)? Do you want a dollar-equivalent daily cap or just invocation count limits?

**2. Which error classes are AUTO_FIX-eligible from day 1?**

Starter list for explicit ratification (please ratify Y/N for each entry):
- [ ] `test_fixture_stale` — test files referencing stale tickers or dates
- [ ] `lint_bare_except` — bare-excepts in non-trading modules (839 current baseline)
- [ ] `dashboard_build_failure` — React/Vite build errors in atlas-dashboard-refresh
- [ ] `doc_comment_error` — errors in documentation files only
- [ ] `pycache_accumulation` — `__pycache__` accumulation (already bash-handled; formalizing)
- [ ] `large_log_rotation` — log rotation triggers (already bash-handled; formalizing)
- [ ] `stale_lock_file` — `/tmp/*.lock` cleanup (already bash-handled; formalizing)

**3. Should any error class stay ASSIST-mode forever (not only when confidence is low)?**

Candidate permanent ASSIST classes (please confirm or modify):
- Any error from `eod_settlement` — touches financial settlement state
- Any reconciliation discrepancy — touches position ledger
- Any `broker_orders` anomaly — adjacent to live order management

These are NOT on the NEVER list (they don't touch trading-path code directly), but they feel like they warrant human eyes regardless of confidence. Do you agree, or should any of these be AUTO_FIX-eligible at high confidence?

**4. Telegram cadence — digest only, per-fix notification, or both?**

Recommendation: daily digest (08:00 AEST) + immediate ESCALATE alerts + one Telegram per accepted ASSIST fix merge. AUTO_FIX activity in digest only (not per-fix, to avoid noise). Does this match your Telegram volume tolerance? Given that P1 Telegram noise reduction was a priority (commit `27c99790`), per-fix AUTO_FIX notifications seem inconsistent with that goal.

**5. Who is the human reviewer in Phase 2?**

ASSIST mode generates `autofix/<branch>` branches requiring a human merge decision. Options:
- (a) Exclusively you — simplest; `accept_fix.sh` hardcodes single-user expectation
- (b) You + designated delegate — requires ACL in `accept_fix.sh`; more resilient if you're unavailable

What is your SLA expectation for branch review latency? (Fix branches may sit for hours between creation and review — is that acceptable given the errors they address are non-critical by definition if they reached ASSIST tier rather than ESCALATE?)

**6. Halt criteria: how many regressions trigger auto-pause?**

Candidate:
- 1 regression (a test-suite failure caused by an accepted ASSIST fix) in any 7-day window → AUTO_FIX pause + root cause review
- 2 regressions in any 14-day window → full Phase 3 review meeting
- 1 regression in a financial-state-adjacent module → immediate Phase 3 kill regardless of rate

Is the 1-regression threshold too tight (will generate false pause events from flaky tests), or is it correct given live capital at risk?

**7. Kill criteria: at what point do we abandon Phase 3 and stay at ASSIST forever?**

Candidate kill conditions:
- (a) 3 NEVER-list file touch attempts by scope guard in any 30-day window
- (b) Regression rate >1% on AUTO_FIX commits over any 60-day rolling window
- (c) Any regression whatsoever in a financial-state-adjacent module (eod_settlement, reconcile, broker_orders)
- (d) User subjective determination that cognitive overhead outweighs time saved

Are these the right conditions? Is condition (c) — instant kill for any financial-adjacent regression — too conservative, or exactly right given the system's capital exposure? If (c) is too strict, what's the correct threshold?

---

## Appendix: Source References

| Claim | Source | Specific Reference |
|-------|--------|--------------------|
| 637 historical errors in `system_log` | `sqlite3 data/atlas.db` | `SELECT COUNT(*) FROM system_log WHERE level='error'` → 637 |
| 427 "Circuit breaker tripped" (67%) | `sqlite3 data/atlas.db` | `SELECT message, COUNT(*) FROM system_log WHERE level='error' GROUP BY message ORDER BY 2 DESC LIMIT 10` |
| 945 total commits, ~270 fix-type in 60 days | `git log --oneline --since="60 days ago"` | 945 lines; grep fix/rca/urgent/repair/revert/patch → 270 |
| `healthz_hourly.sh` pi-agent spawn | `/root/atlas/scripts/healthz_hourly.sh` | "Step 5: Spawn pi agent to fix remaining issues"; `timeout 300 pi -p --system-prompt ...` |
| Atlas-jobs scaffold is empty | Filesystem check | `ls /root/.pi/extensions/atlas-jobs/` → empty (zero files) |
| 17 active `atlas-*` systemd timers | `systemctl list-timers --all` | heartbeat-watchdog, silent-failure-watchdog, dashboard-refresh, canary-check, backup, risk-precompute, research-window×7, director, fred-health, universe-rebuild, discovery |
| Circuit breaker in `healthz_hourly.sh` | `/root/atlas/scripts/healthz_hourly.sh` | `BREAKER_FILE`, 18,000s window, `claude_circuit_breaker.trip()` call on usage exhaustion |
| orchestrator.py, trade_state_machine.py scaffold sizes | `wc -l core/orchestrator.py core/trade_state_machine.py` | 101 lines, 100 lines |
| 839 bare-excepts lint baseline | Commit `db583128` | "phase A.3: AST lint scripts for bare-except + pi --system-prompt" |
| OAuth routing mandate | `/root/AGENTS.md` | `--system-prompt "You are Claude Code, Anthropic's official CLI for Claude."` required on all `pi` subprocess calls |
| URGENT test-isolation commits | `git log --oneline --since="60 days ago"` | `4ea328fa` (live_*.json isolation), `dede8d62` (kill_switch HALT file isolation) |
| `system_log` 4,385 total rows | `sqlite3 data/atlas.db` | `SELECT COUNT(*) FROM system_log` → 4385 |
| 4h cooldown in `healthz_hourly.sh` | `/root/atlas/scripts/healthz_hourly.sh` | `COOLDOWN_HOURS=4`; hash-based cooldown file in `$COOLDOWN_DIR` |
| Live capital $5,189 | Prompt brief / Atlas config | Alpaca live account, confirmed in context |

---

*Report complete — 2026-04-29.*  
*Section 9 decision-point answers required before Phase 2 specification begins.*  
*Contact: Planning Lead for synthesis; Engineering Lead for Phase 0 implementation delegation.*
