---
name: atlas-elastic-planner
description: "Plan parallel agent work before dispatching it — classify task risk, generate a DAG plan, evaluate safety gates, and optionally dispatch read-only burst agents or queue write dispatch. Use whenever you are considering spawning more than one agent: research sweeps, code reviews, multi-file refactors, incident triage, or any parallelizable task. Triggers: 'plan this with agents', 'how many agents should I use', 'can I parallelize this', 'elastic plan', 'spin up scouts', 'review with agents', 'QA this', 'security scan'."
type: reference
---

# atlas-elastic-planner — Elastic Agent Planning Skill

**When to use**: Before dispatching any parallel agent work — research sweeps, code reviews, multi-file refactors, incident triage, or any task where you're considering spawning more than 1 agent. Use this skill to classify the task, get a DAG plan, evaluate safety gates, and optionally dispatch read-only burst agents or queue write dispatch.

**Trigger phrases**: "plan this with agents", "how many agents should I use", "can I parallelize this", "spin up scouts", "elastic plan", "parallel agents for X", "review with agents", "QA this", "security scan"

---

## Quick Reference

### Commands

| Command | Description |
|---------|-------------|
| `/elastic-plan <objective>` | Classify + DAG + gates (dry-run only, no spawn) |
| `/elastic-plan <objective> -- file1 file2` | Plan with specific files affected |
| `/elastic-run <objective>` | Gate evaluation + command hints (plan/gate only) |
| `/elastic-run <objective> --execute-read-only` | Gate + **actually run** burst agents (read_only/planning/review_qa) |
| `/elastic-run <objective> -- file1 --confirm` | Gate + return manual write-plan message (write_bounded, gates must pass) |
| `/elastic-status` | Show last 20 audit entries |

### Tools (LLM-callable)

| Tool | Description |
|------|-------------|
| `atlas_elastic_plan` | Plan a task (dry-run, no spawn) |
| `atlas_elastic_run` | Gate check + optional burst or dispatch |

**`atlas_elastic_run` parameters:**
- `objective` — task objective
- `files_affected` — optional file list
- `execute_read_only` — `true` to actually run burst agents (read_only/planning/review_qa only)
- `confirmed` — `true` to return a manual write-plan message (write_bounded only, all gates must pass)

---

## Risk Classes

| Class | Keywords / patterns (classification) | Roles | Auto-spawn? | Gates |
|-------|--------------------------------------|-------|-------------|-------|
| `read_only` | (default for no write/QA/live keywords) | scout, researcher | ✓ with execute_read_only=true | None |
| `planning` | plan, spec, design, architecture, DAG | planner, spec_writer | ✓ with execute_read_only=true | None |
| `review_qa` | review, code review, verify, test analysis, security scan, QA, lint — AND no write keywords | reviewer, test-runner, security-reviewer | ✓ with execute_read_only=true | None |
| `write_bounded` | refactor, implement, add (including sentence-initial), fix, update, create (or files present without live/QA keywords) | scout→builder(s)→reviewer | ✗ confirmed=true queues message | Clean tree + ownership |
| `live_trading_ops` | deploy/promote + config (regex, catches “promote sp500 config”, “promote active config” etc.), broker state/position/order/account/trade/mutation, live trade, execute trade, active\_config, rebalance portfolio | executor | ✗ ALWAYS BLOCKED | Explicit risk gate |

**Classification priority** (most restrictive first):
1. `live_trading_ops` — any live/broker/active_config keywords
2. `review_qa` — QA/review keywords AND no write keywords
3. `write_bounded` — write keywords OR files_affected (without live/QA keywords)
4. `planning` — spec/design keywords
5. `read_only` — default

---

## Workflow

### Step 1 — Plan (always first)

```
/elastic-plan <objective> [-- file1 file2 ...]
```

Or call `atlas_elastic_plan` tool with:
- `objective`: what needs to be done
- `files_affected`: optional list of files
- `dry_run`: true (default)

### Step 2 — Review gates

- All `status=required` gates must be `satisfied=true` before dispatch
- `clean_working_tree` unsatisfied → dirty tree, stash or commit first
- Protected files → manual approval required
- `review_qa` and `read_only` → no required gates (dispatch immediately)

### Step 3 — Dispatch

**For `read_only`, `planning`, `review_qa`** — Dispatch immediately:

```
/elastic-run <objective> --execute-read-only
```
or `atlas_elastic_run` with `execute_read_only: true`

→ Runs bounded-concurrency burst via `pi` CLI:
  - `--tools read,grep,find,ls` (read-only tools)
  - `--no-session` (no state persistence)
  - `--mode json` (structured output)
  - `--system-prompt "You are Claude Code..."` (Claude Max OAuth)
  - Default concurrency: **4 agents** (hard safe default)
  - **Per-agent timeout**: from `policy.agent_roles[role].timeout_sec` (e.g. reviewer/test-runner/security-reviewer → 300s, researcher → 600s). Fallback: 300s. Pass `timeoutMs` to override all agents.
  - Audit: `read_only_started` → `read_only_complete` or `dispatch_rejected`

**For `write_bounded`** — Requires human confirmation:

```
/elastic-run <objective> -- file1 file2 --confirm
```
or `atlas_elastic_run` with `confirmed: true`

→ Validates gates (clean tree required), then **returns** a manual write-plan message with:
  - Copy-pasteable task objective
  - Full file ownership table (builder → files, exclusive)
  - Explicit warning: queued, not auto-executed
  - Audit: `dispatch_requested`
  - If dirty tree: always rejects as `write_gate_rejected` even with confirmed=true

**For `live_trading_ops`** — STOP, use risk gates:
```
atlas_risk_check_plan_gate + atlas_risk_approve_plan
```
Never use elastic-run for live trading ops.

### Step 4 — Check audit

```
/elastic-status
```

All plan+run attempts logged to `.pi/elastic-agents/audit.jsonl`.

---

## TUI Integration

`atlas_elastic_run` is registered as a **delegation tool** in the `atlas-tui-widget`. While it is running, the TUI shows:
- `→` icon (delegation activity) in the activity feed
- Agent counter incremented (`agents 1/1` etc.)

No additional persistent widgets are added. Use `/elastic-status` for history.

---

## Safety Rules (Non-Negotiable)

1. **No write dispatch without clean tree** — `git status --short` must return empty, even with `confirmed=true`
2. **No write dispatch without file ownership table** — every builder owns exclusive files
3. **No live_trading_ops auto-execution** — always use risk gate tools
4. **No Anthropic API key** — all LLM calls via `pi` CLI with `--system-prompt` flag
5. **Kill switch** — if `config/agent-scale-policy.yaml:global.kill_switch=true`, all spawning blocked
6. **No overlapping ownership** — `validateOwnershipTable()` run before write dispatch
7. **Burst concurrency cap** — default 4 agents max; capped at `policy.global.max_concurrent_agents`
10. **Per-agent timeout** — each burst agent uses `policy.agent_roles[role].timeout_sec` (300s for reviewer/test-runner/security-reviewer, 600s for researcher). Fallback 300s. Configurable via `timeoutMs` option to override all agents.
8. **Read-only tools only for burst** — `--tools read,grep,find,ls` enforces no write access
9. **No shell interpolation** — subprocess args passed as arrays; objectives sent via stdin

---

## Policy Config

Policy lives at `config/agent-scale-policy.yaml`. Key settings:

```yaml
global:
  max_concurrent_agents: 16   # hard ceiling for all spawning
  max_write_agents: 4         # write-phase cap
  kill_switch: false          # set true to halt all spawning

agent_roles:
  reviewer:
    timeout_sec: 300           # burst timeout for reviewer agents
  test-runner:
    timeout_sec: 300           # burst timeout for test-runner agents
  security-reviewer:
    timeout_sec: 300           # burst timeout for security-reviewer agents
  researcher:
    timeout_sec: 600           # longer timeout for deep research
```

Change `kill_switch: true` to instantly stop all new agent spawns.

`agent_roles[role].timeout_sec` drives per-agent burst timeouts. If a role is missing, the fallback is 300s.

---

## Audit Log

Every plan and run attempt is logged to:
```
.pi/elastic-agents/audit.jsonl
```

Each entry contains:
- `timestamp`, `task_id`, `objective`
- `risk_class` — one of the 5 classes above
- `decision` — `plan_generated`, `gates_passed`, `gates_blocked`, `read_only_started`,
  `read_only_complete`, `dispatch_requested`, `dispatch_rejected`, `write_gate_rejected`,
  `kill_switch_active`
- `gates` — full gate status array
- `warnings`, `blockers`
- `dry_run` flag

---

## What is Actual vs Queued

| Feature | Status | Notes |
|---------|--------|-------|
| Risk classification + DAG | ✅ Actual | Pure function, runs always |
| Gate evaluation | ✅ Actual | Git check runs in real repo |
| Read-only burst (pi CLI) | ✅ Actual | execute_read_only=true required |
| Write-plan message | ✅ Actual | confirmed=true generates a plan message; execution is NOT automatic |
| Automatic write execution | ❌ Never | User must execute manually or use a focused agent with explicit scope |
| Live trading auto-execute | ❌ Never | Use atlas_risk_check_plan_gate |

---

## Examples

### Read-only sweep (safe, dispatchable)

```
/elastic-run search all deprecated function calls in src/ --execute-read-only
```
→ `read_only` | scouts in parallel | `read_only_complete`

### Code review (review_qa, dispatchable)

```
/elastic-run review the authentication module --execute-read-only
```
→ `review_qa` | reviewer + test-runner + security-reviewer | `read_only_complete`

### Security scan (review_qa)

```
/elastic-run security scan on the broker integration --execute-read-only
```
→ `review_qa` | security-reviewer + reviewer | `read_only_complete`

> ⚠️ Note: “security scan on broker integration” correctly classifies as `review_qa`, not `live_trading_ops`.
> Only **specific broker mutation terms** (broker state, broker order, broker position, etc.) trigger
> `live_trading_ops`. Bare “broker” alone does not.

### Multi-file refactor (write — queues, doesn't execute)

```
/elastic-run refactor momentum_breakout strategy -- strategies/momentum_breakout.py strategies/base.py tests/test_momentum.py --confirm
```
→ `write_bounded` | gates checked | if clean: `dispatch_requested` with ownership table

### Live-trading config change (always blocked here)

```
/elastic-plan promote sp500 config to live
```
→ `live_trading_ops` | BLOCKED | use `atlas_risk_check_plan_gate` + `atlas_risk_approve_plan`

---

## References

- Spec: `docs/elastic-parallel-agent-orchestrator.md`
- Board memo: `/root/ceo-board/memos/2026-05-25-parallel-agent-scaling/memo.md`
- Policy: `config/agent-scale-policy.yaml`
- Audit log: `.pi/elastic-agents/audit.jsonl`
- Extension: `pi-package/atlas-ops/extensions/atlas-elastic-agents/`
- Parallel work rules: `/root/AGENTS.md` (Parallel Work and Long-Running Tasks section)
