# Elastic Parallel-Agent Orchestrator — Implementation Spec

**Status**: Phase 1–2 MVP Delivered (2026-05-26)
**Board Decision**: Conditional Accept (2026-05-25, vote 5-0)  
**Doc**: Supersedes exploratory notes; governs Phases 1–5 rollout  
**Review Date**: After Phase 2 read-only burst mode on 5+ real tasks without runaway spawning

## MVP Implementation (2026-05-26)

Phases 1–2 delivered as a Pi extension at `pi-package/atlas-ops/extensions/atlas-elastic-agents/`.

### What's implemented

| Phase | Status | Notes |
|-------|--------|-------|
| Policy config | ✅ | `config/agent-scale-policy.yaml` — YAML, validated at load time via Python |
| Policy loader/validator | ✅ | `policy.ts` — typed accessors, kill switch, protected file matching |
| Dry-run planner | ✅ | `planner.ts` — classifies task, generates DAG, evaluates all gates |
| Audit logging | ✅ | `audit.ts` — JSONL to `.pi/elastic-agents/audit.jsonl` |
| Execution gate | ✅ | `executor.ts` — live_trading always blocked; write requires human confirm |
| Read-only burst support | ✅ | Gated stub: returns OAuth-only pi CLI commands for coordinator use |
| Commands | ✅ | `/elastic-plan`, `/elastic-run`, `/elastic-status` |
| Tools | ✅ | `atlas_elastic_plan`, `atlas_elastic_run` |
| Skill | ✅ | `skills/atlas-elastic-planner/SKILL.md` |
| Tests | ✅ | 114/114 elastic-agent tests pass; proves dirty-tree gate rejects on real atlas repo |
| Write builder orchestration | 🔲 | Returns a gated manual write plan with ownership guidance (Phase 3) |
| TUI live agent dashboard | 🔲 | Phase 5; audit log readable via `/elastic-status` now |

### Usage

```bash
# Plan a task (dry-run, no agents spawned)
/elastic-plan refactor authentication module -- src/auth.py tests/test_auth.py

# Check execution gate
/elastic-run search codebase for deprecated calls

# View audit log
/elastic-status
```

### Key safety properties proven by tests

- `live_trading_ops` tasks: **always blocked** by executor gate
- `write_bounded` tasks: **blocked** on dirty working tree (verified on real dirty atlas repo)
- `read_only` tasks: **allowed**, returns OAuth-only `pi` CLI commands (no API key)
- Protected files (broker state, live config, `.git/`, secrets): detected and flagged
- Kill switch (`global.kill_switch: true`): blocks all new spawns
- No `Anthropic(api_key=...)` usage in any extension source file
- Audit trail written for every plan/run attempt

---

## Goal

Enable Pi to spawn elastic parallel agents (scouts, builders, reviewers, researchers) to **scale delivery velocity** without sacrificing safety, code quality, or live-trading stability. Not literal unbounded concurrency — rather, **governed parallelism scaled to the maximum useful number allowed by task decomposition and safety policy**.

### Success Metrics

- Phase 1: Planner recommends agent allocation within 2s; 0 spawn false positives.
- Phase 2: 5+ read-only tasks executed with broad agent parallelism; synthesis accuracy ≥95%.
- Phase 3: Multi-file refactors merge cleanly; 0 manual conflict resolution needed.
- Phase 4: All tasks pass verification before completion; 0 regressions from parallel execution.
- Phase 5: Operators can control and observe all parallel work via TUI; rollback time <30s.

---

## Non-Goals

- Literal unlimited agent spawning
- Autonomous live-trading decisions without explicit gates
- Removal of file ownership or merge hygiene discipline
- Pay-per-token Anthropic API usage (Claude Max OAuth only)
- Background agents hidden from TUI
- Parallelism that breaks existing sequential verification workflows

---

## Architecture Overview

### Coordinator Pattern (Overstory-style)

```
┌─ User Task ──────────────────────────────────────────┐
│  "refactor X across the codebase"                    │
└─ Coordinator ────────────────────────────────────────┘
     ↓ (Phase 1: planner)
  ┌─ DAG + role allocation ─────────────────────────┐
  │ • Risk class: write-bounded                      │
  │ • Scout → 2 Builders → 1 Reviewer → Merge       │
  │ • Max concurrency: 3                             │
  │ • File ownership table                           │
  │ • Dependencies: builder-2 depends on builder-1   │
  └──────────────────────────────────────────────────┘
     ↓ (Phase 2–3: execution engine)
  ┌─ Execute agents in parallel (by role + rules) ──┐
  │ Scout (read-only): immediate                    │
  │ Builder-1 (owns file A,B): worktree-1, isolated │
  │ Builder-2 (owns file C,D): waits for B→success │
  │ Reviewer: runs in parallel with builders        │
  └──────────────────────────────────────────────────┘
     ↓ (Phase 4–5: verification + controls)
  ┌─ Verify + Merge ──────────────────────────────────┐
  │ • Run acceptance tests                           │
  │ • Confirm no overlapping file edits              │
  │ • Merge worktrees into main branch               │
  │ • TUI shows final status & artifacts             │
  └──────────────────────────────────────────────────┘
```

### Core Components

| Component | Responsibility | Lives where |
|-----------|-----------------|-------------|
| **Planner** | Classify risk, produce DAG, propose concurrency | `pi-package/atlas-ops/skills/atlas-elastic-planner/` |
| **Coordinator** | Spawn agents by role, track ownership, enforce gates | CEO layer / focused-agent dispatch |
| **Execution Engine** | Run agents in isolated worktrees, aggregate outputs | manual execution or focused-agent workflow |
| **Verifier** | Parallel testing, code review, safety checks | `atlas-incident` skill (extended) |
| **TUI** | Live agent dashboard, controls, audit log | `/root/.pi/tui/` (Phase 5) |
| **Policy Store** | Agent concurrency rules, risk thresholds, gates | `config/agent-scale-policy.yaml` |

---

## Task Classification

Every task assigned to agents falls into one of these **risk classes**:

| Class | Concurrency | Gates | Writers | Examples |
|-------|----------|-------|---------|----------|
| **Read-only reconnaissance** | Elastic (8–16 agents) | None | 0 | search, incident triage, docs audit, codebase mapping |
| **Planning** | Low/moderate (2–4) | None | 0 | spec writing, DAG generation, risk assessment |
| **Write builders** | Bounded (1–4 per file) | File ownership table | 1 per file | refactors, features, tests, migrations |
| **Review/QA** | Elastic/moderate (4–8) | None | 0 | code review, test analysis, security scan |
| **Live trading ops** | Gated/manual (1) | Explicit risk approval | 1 | broker state mutation, config promotion, position changes |

---

## Phase 1: Policy + Dry-Run Planner

**Objective**: Prove that task decomposition and agent allocation can be automated safely.

### Deliverables

#### 1. Agent Scale Policy (`config/agent-scale-policy.yaml`)

Schema:

```yaml
# config/agent-scale-policy.yaml

version: "1.0"

# Global hard caps
global:
  max_concurrent_agents: 16
  max_write_agents: 4
  max_parallel_builders_per_file: 1
  kill_switch: false
  budget_tokens_per_task: 100000

# Risk class definitions
risk_classes:

  read_only:
    type: "read_only"
    default_concurrency: 12
    roles: ["scout", "researcher"]
    gates: []
    examples: ["codebase search", "incident triage", "docs review"]

  planning:
    type: "planning"
    default_concurrency: 3
    roles: ["planner", "spec_writer"]
    gates: []
    examples: ["spec writing", "risk assessment", "DAG generation"]

  write_bounded:
    type: "write_bounded"
    default_concurrency: 4
    max_builders: 2
    roles: ["builder", "reviewer"]
    gates:
      - clean_working_tree: true
      - file_ownership_table_required: true
    examples: ["refactoring", "multi-file feature", "test suite addition"]

  live_trading_ops:
    type: "live_trading_ops"
    default_concurrency: 1
    roles: ["executor"]
    gates:
      - explicit_human_approval: true
      - trading_halt_check: true
      - config_validation: true
    examples: ["broker mutation", "config promotion", "portfolio rebalance"]

# Per-agent concurrency rules
agent_roles:

  scout:
    risk_class: read_only
    concurrency_cap: 8
    timeout_sec: 300
    can_spawn: ["scout", "researcher"]
    cannot_spawn: ["builder", "executor"]

  researcher:
    risk_class: read_only
    concurrency_cap: 6
    timeout_sec: 600
    can_spawn: ["scout", "researcher"]
    cannot_spawn: ["builder", "executor"]

  planner:
    risk_class: planning
    concurrency_cap: 2
    timeout_sec: 120
    can_spawn: ["scout", "researcher"]
    cannot_spawn: ["builder", "executor"]

  builder:
    risk_class: write_bounded
    concurrency_cap: 2
    timeout_sec: 1200
    can_spawn: []
    cannot_spawn: ["executor"]
    isolation: worktree
    owned_files_required: true

  reviewer:
    risk_class: review_qa
    concurrency_cap: 4
    timeout_sec: 300
    can_spawn: []
    cannot_spawn: ["builder", "executor"]

  executor:
    risk_class: live_trading_ops
    concurrency_cap: 1
    timeout_sec: 60
    can_spawn: []
    cannot_spawn: ["builder"]
    gates: ["explicit_approval", "trading_halt_check", "config_validation"]

# Files that require special gates (no automatic write)
protected_files:
  - "scripts/deploy_config.py"
  - "config/live_*.yaml"
  - "services/broker_*.py"
  - ".git/**"

# Approval thresholds
approval_gates:
  live_trading_ops: "explicit"  # always require human sign-off
  write_bounded: "auto_if_tests_pass"  # auto-approve if verification passes
```

#### 2. Dry-Run Planner (`atlas-elastic-planner` skill)

**Input**: User task description + objective  
**Output**: Structured JSON plan (no agents spawned)

**Example output**:

```json
{
  "task_id": "refactor-X-across-codebase",
  "risk_class": "write_bounded",
  "summary": "Replace deprecated X() calls with Y() across 6 files",
  "proposed_dag": {
    "phases": [
      {
        "phase": 1,
        "agents": [
          {
            "id": "scout-1",
            "role": "scout",
            "objective": "Find all X() calls and affected modules",
            "concurrency_slot": 1
          }
        ]
      },
      {
        "phase": 2,
        "agents": [
          {
            "id": "builder-1",
            "role": "builder",
            "objective": "Refactor X→Y in files A, B, C",
            "files_owned": ["src/file_a.py", "src/file_b.py", "src/file_c.py"],
            "depends_on": ["scout-1"]
          },
          {
            "id": "builder-2",
            "role": "builder",
            "objective": "Refactor X→Y in files D, E, F",
            "files_owned": ["src/file_d.py", "src/file_e.py", "src/file_f.py"],
            "depends_on": ["scout-1"]
          }
        ]
      },
      {
        "phase": 3,
        "agents": [
          {
            "id": "reviewer-1",
            "role": "reviewer",
            "objective": "Code review + test analysis",
            "depends_on": ["builder-1", "builder-2"]
          }
        ]
      }
    ]
  },
  "concurrency_summary": {
    "max_parallel_agents": 2,
    "total_agents": 4,
    "estimated_time_minutes": 15,
    "estimated_tokens": 8500
  },
  "file_ownership_table": {
    "builder-1": ["src/file_a.py", "src/file_b.py", "src/file_c.py"],
    "builder-2": ["src/file_d.py", "src/file_e.py", "src/file_f.py"],
    "scout-1": []
  },
  "safety_gates": [
    {
      "gate": "clean_working_tree",
      "status": "required",
      "satisfied": true
    },
    {
      "gate": "no_overlapping_ownership",
      "status": "required",
      "satisfied": true
    },
    {
      "gate": "file_ownership_coverage",
      "status": "required",
      "satisfied": true
    }
  ],
  "warnings": [],
  "next_step": "Review plan. If approved, call coordinator to spawn agents."
}
```

#### 3. Policy Enforcement Rules (hardcoded in coordinator)

- **Clean tree check**: `git status --short` must be empty before write-phase spawn
- **File ownership uniqueness**: No two builders write the same file; new files assigned to exactly one builder
- **Dependency linearization**: If Builder B depends on Builder A, run A first; Builder B waits for A to commit
- **Protected files**: `scripts/deploy_config.py`, `config/live_*.yaml`, `services/broker_*.py` cannot be auto-written; require explicit gate
- **Concurrency caps**: Respect per-agent and global caps; queue excess agents; never exceed limits
- **Kill switch**: If `agent-scale-policy.yaml:global.kill_switch` is `true`, no new agent spawns (in-flight agents complete)
- **OAuth-only**: All LLM calls via `pi` subprocess with `--system-prompt` flag; no Anthropic API key usage
- **Audit trail**: Every agent spawn logged to `.pi/elastic-agents/audit.jsonl` with timestamp, task, role, and plan hash

### Acceptance Criteria for Phase 1

- [ ] `config/agent-scale-policy.yaml` schema is valid YAML and matches the spec above
- [ ] Planner skill exists at `pi-package/atlas-ops/skills/atlas-elastic-planner/SKILL.md`
- [ ] Planner accepts a task description and returns a valid JSON plan within 2 seconds
- [ ] Planner correctly identifies risk class, proposes concurrency, and flags all protected files
- [ ] Planner is tested against 3 historical task types: small bugfix, multi-file refactor, live-trading config change
- [ ] All planner outputs pass `jsonschema` validation
- [ ] Coordinator can read policy, validate plan against policy, and reject unsafe plans with clear reasoning
- [x] Audit log is written to `.pi/elastic-agents/audit.jsonl` (implemented in Phase 2)
- [ ] No agents spawned in Phase 1 (dry-run only)

---

## Phase 2: Read-Only Burst Mode

**Objective**: Prove that elastic read-only parallelism is safe and accurate.

### Deliverables

- `scout` and `researcher` agents can run in batches (8–16 at once)
- Findings aggregated into a single compressed brief with source links
- No file writes permitted
- Automatic timeout and graceful failure per agent

### Acceptance Criteria

- [ ] Scout task launches ≤8 parallel scouts; all complete or timeout
- [ ] Researcher task launches ≤6 parallel researchers; all complete or timeout
- [ ] Findings aggregated into structured JSON with source links
- [ ] Tested on 5+ real reconnaissance tasks
- [ ] Synthesis accuracy (facts per primary source) ≥95%
- [ ] Zero false positives or contradictions in aggregated output
- [ ] Timeout and error handling prevent infinite loops
- [ ] TUI shows all active scouts/researchers and their progress

---

## Phase 3: Elastic Builder Workflow

**Objective**: Prove multi-file changes merge cleanly with strict ownership.

### Deliverables

- Worktree-backed builders (1 per file group)
- File ownership registry prevents overlaps
- Staged dependencies (Builder B waits for Builder A)
- Automatic re-dispatch on failure (no manual conflict resolution)

### Acceptance Criteria

- [ ] Multi-file refactors (6+ files) merge cleanly; 0 manual fixes
- [ ] Builders own code + tests; tests run post-build
- [ ] Failed builders re-dispatched with tighter scope; eventually succeed
- [ ] Merge conflicts detected and escalated to coordinator
- [ ] cleanup removes stale worktrees and branches post-merge
- [ ] 0 regressions vs. sequential execution

---

## Phase 4: Verification Workflow

**Objective**: Parallel testing + review ensure quality before merge.

### Deliverables

- Parallel test runners on each builder's output
- Code review agents
- Security/lint scanners
- Failure triage and targeted re-dispatch

### Acceptance Criteria

- [ ] Every builder output verified before merge
- [ ] Test parallelism reduces verification time by ≥40%
- [ ] 0 test flakes due to concurrency
- [ ] Security checks catch 100% of manually-seeded issues

---

## Phase 5: TUI + Observability

**Objective**: Live visibility and manual control over all parallel work.

### Deliverables

- Real-time agent dashboard in TUI
- Agent status: spawned, running, verified, merged, failed
- Files owned by each builder (readonly view)
- Pause/cancel/escalate controls
- Audit log: every spawn + decision logged
- Preserved footer: context %, tokens, model, thinking cost

### Acceptance Criteria

- [ ] TUI shows agents + queued tasks without adding >500ms latency
- [ ] Pause/cancel/escalate work with no orphaned processes
- [ ] Audit log queries run <100ms
- [ ] Footer always visible and up-to-date

---

## Safety Red Lines (Non-negotiable)

1. **No unbounded write agents.** Max 4 concurrent writers, each owns exclusive files.
2. **No live-trading mutation without explicit approval.** Every `live_trading_ops` task requires human sign-off.
3. **No dirty-tree spawns.** `git status --short` must be empty; write execution is refused otherwise.
4. **No overlapping file ownership.** Coordinator rejects plans where two builders touch the same file.
5. **No API-key Anthropic usage.** All LLM calls via Claude Max OAuth (`pi` subprocess with `--system-prompt`).
6. **No hidden background agents.** Every spawn logged and visible in TUI.
7. **No manual conflict resolution.** Merge conflicts escalated to coordinator; never hand-fixed.
8. **No skipped verification.** Task marked complete only after independent verification passes.

---

## Integration Points

### TUI Display (Phase 5)

The existing TUI (commit `2e5b3660`) shows phase, active agents, tools, errors, elapsed, and preserves footer context. Phase 1–4 must log events consumable by the TUI without requiring changes to footer layout.

### Claude Max OAuth (All Phases)

Every subagent call includes:

```bash
pi --system-prompt "You are Claude Code, Anthropic's official CLI for Claude." \
   --model claude-sonnet-4-6 --mode json
```

Reference: `/root/AGENTS.md` and `/root/.pi/teams/skills/claude-oauth.md`.

### Coordinator Gateway

Planner output → user review → explicit approval gate → coordinator dispatch → audit log.

No auto-spawning without user permission.

### File Ownership Registry

Stored in-memory during task execution; persisted to `~/.pi/agent-ownership.json` for TUI queries.

---

## Implementation Roadmap

### Phase 1 — Policy + Planner (THIS SPEC)

| Item | Owner | Effort | Timeline |
|------|-------|--------|----------|
| Write `config/agent-scale-policy.yaml` | ops | 4h | Week 1 |
| Implement `atlas-elastic-planner` skill | worker | 12h | Week 1–2 |
| Validate against 3 historical task types | reviewer | 6h | Week 2 |
| Coordinator reads policy + rejects unsafe plans | worker | 8h | Week 2 |
| Audit logging stub | worker | 2h | Week 2 |
| **Total Phase 1** | | **32h** | **2 weeks** |

### Phase 2–5 Expansion

Detailed roadmap in `/root/atlas/docs/phases-2-5-roadmap.md` (to be created post-Phase 1).

---

## Monitoring + Alerting

### Key Metrics (Phase 1–5)

- Spawn latency (planner → decision, p50/p95)
- Agent success rate (spawned / completed / failed)
- Merge conflict rate (attempts / conflicts)
- Verification pass rate (spawned / verified)
- Token usage per task (actual vs. estimate)
- Time savings vs. sequential (speedup factor)

### Kill Switch

If `config/agent-scale-policy.yaml:global.kill_switch` is set to `true`, no new agents spawn. In-flight agents complete, but no new tasks are dispatched. Coordinator logs reason to `.pi/elastic-agents/audit.jsonl`.

---

## Next Steps (After Phase 1 Approval)

1. **Config creation**: Write `config/agent-scale-policy.yaml` with example values
2. **Planner skill**: Implement `atlas-elastic-planner/SKILL.md` and CLI entry point
3. **Test harness**: Write 3 historical task descriptions and validate planner output
4. **Coordinator integration**: Wire planner into CEO layer; add approval gate
5. **Documentation**: Update `/root/AGENTS.md` with elastic parallelism rules
6. **TUI stub**: Add audit log viewer to existing TUI (Phase 5, but no changes to footer)

---

## Appendix: Schema Validation

All policy and plan files validate against JSON Schema:

- `config/agent-scale-policy.yaml` → YAML
- Planner output (JSON) → `/root/atlas/schemas/agent-plan.json`
- Audit log entries (JSONL) → `/root/atlas/schemas/audit-log-entry.json`

Validation enforced at load time; invalid configs cause immediate agent spawn rejection.

---

## Appendix: Example Task Walkthrough

### Task: "Refactor auth across 6 API endpoints"

**User prompt**: `refactor_task: "Replace old_auth() with new_oauth() in services/api/*.py"`

**Planner runs** (5 seconds):

```json
{
  "task_id": "refactor-auth-api",
  "risk_class": "write_bounded",
  "files_affected": 7,
  "proposed_concurrency": 2,
  "agents": [
    {"id": "scout-auth", "role": "scout"},
    {"id": "builder-auth-1", "role": "builder", "owns": ["api/auth.py", "api/user.py"]},
    {"id": "builder-auth-2", "role": "builder", "owns": ["api/admin.py", "api/token.py", "api/session.py"]},
    {"id": "reviewer-auth", "role": "reviewer"}
  ],
  "warnings": []
}
```

**Coordinator checks**:
- ✓ Clean tree
- ✓ No overlaps
- ✓ Protected files not touched
- ✓ OAuth-only

**Coordinator asks user**: "Approve this plan? (Y/n)"

**User approves**: `Y`

**Coordinator spawns**:
- Scout runs immediately (read-only, no gate needed)
- Builders 1 & 2 run in parallel (separate worktrees, clean git)
- Reviewer runs during builds (no conflicts)
- All log to audit trail

**Results**:
- Scout: "Found 14 old_auth() calls in 6 files"
- Builder-1: Refactored 2 files, 8 calls replaced, tests pass
- Builder-2: Refactored 3 files, 6 calls replaced, tests pass
- Reviewer: Code review passed, security scan passed
- Coordinator: Merge builders into main, delete worktrees
- Task marked complete

**Total time**: 8 minutes vs. 35 minutes sequential.

---

## References

- Board memo: `/root/ceo-board/memos/2026-05-25-parallel-agent-scaling/memo.md`
- CEO journal: `/root/.pi/agent/ceo-journal.md` (2026-05-25 entry)
- Atlas architecture: `/root/atlas/docs/ARCHITECTURE.md`
- Parallel work rules: `/root/AGENTS.md` (Parallel Work and Long-Running Tasks section)
- TUI commit: `2e5b3660`

---

**Document version**: 1.0  
**Last updated**: 2026-05-26  
**Next review**: After Phase 2 completion (estimated 2026-06-23)
