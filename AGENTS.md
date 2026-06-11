# Atlas Agent Instructions

Canonical instructions for GPT/Pi coding agents working in `/root/atlas`.

`AGENTS.md` is the authoritative agent-instruction file. `CLAUDE.md` may remain for legacy Claude-tool compatibility, but new or updated agent rules belong here first.

## Memory

- **Read `memory/SUMMARY.md` at the start of every session**
- After any correction, discovery, or decision: update it
- Keep it under 100 lines — consolidate, don't append endlessly
- If it gets long, compress repeated patterns into single rules

## Workflow Orchestration

### 1. Plan Node Default
- Enter plan mode for ANY non-trivial task (3+ steps or architectural decisions)
- If something goes sideways, STOP and re-plan immediately – don't keep pushing
- Use plan mode for verification steps, not just building
- Write detailed specs upfront to reduce ambiguity

### 2. Subagent Strategy
- Use subagents liberally to keep main context window clean
- Offload research, exploration, and parallel analysis to subagents
- For complex problems, throw more compute at it via subagents
- One tack per subagent for focused execution

### 3. Self-Improvement Loop
- After ANY correction from the user: update `memory/SUMMARY.md` with the pattern
- Write rules for yourself that prevent the same mistake
- Ruthlessly iterate on these lessons until mistake rate drops
- Review lessons at session start for relevant project

### 4. Verification Before Done
- Never mark a task complete without proving it works
- Diff behavior between main and your changes when relevant
- Ask yourself: "Would a staff engineer approve this?"
- Run tests, check logs, demonstrate correctness

### 5. Demand Elegance (Balanced)
- For non-trivial changes: pause and ask "is there a more elegant way?"
- If a fix feels hacky: "Knowing everything I know now, implement the elegant solution"
- Skip this for simple, obvious fixes – don't over-engineer
- Challenge your own work before presenting it

### 6. Autonomous Bug Fixing
- When given a bug report: just fix it. Don't ask for hand-holding
- Point at logs, errors, failing tests – then resolve them
- Zero context switching required from the user
- Go fix failing CI tests without being told how

## Task Management

1. **Plan First**: Write a plan with checkable items before building
2. **Verify Plan**: Check in before starting implementation
3. **Track Progress**: Mark items complete as you go
4. **Explain Changes**: High-level summary at each step
5. **Document Results**: Summarize what changed and why in the final report
6. **Capture Lessons**: Update `memory/SUMMARY.md` after corrections

## Infrastructure

- **VPS has 8 CPU cores** — leverage parallel execution for compute-heavy tasks like backtesting
- Split work across cores (e.g. parallel backtest runs, concurrent data processing) to maximise throughput
- Use subagents or multiprocessing to fan out work when tasks are independent

## Core Principles

- **Simplicity First**: Make every change as simple as possible. Impact minimal code.
- **No Laziness**: Find root causes. No temporary fixes. Senior developer standards.
- **Minimal Impact**: Changes should only touch what's necessary. Avoid introducing bugs.

<!-- gitnexus:start -->
# GitNexus — Code Intelligence

This project is indexed by GitNexus as **atlas** (46227 symbols, 76607 relationships, 300 execution flows). Use the GitNexus MCP tools to understand code, assess impact, and navigate safely.

> If any GitNexus tool warns the index is stale, run `npx gitnexus analyze` in terminal first.

## Always Do

- **MUST run impact analysis before editing any symbol.** Before modifying a function, class, or method, run `gitnexus_impact({target: "symbolName", direction: "upstream"})` and report the blast radius (direct callers, affected processes, risk level) to the user.
- **MUST run `gitnexus_detect_changes()` before committing** to verify your changes only affect expected symbols and execution flows.
- **MUST warn the user** if impact analysis returns HIGH or CRITICAL risk before proceeding with edits.
- When exploring unfamiliar code, use `gitnexus_query({query: "concept"})` to find execution flows instead of grepping. It returns process-grouped results ranked by relevance.
- When you need full context on a specific symbol — callers, callees, which execution flows it participates in — use `gitnexus_context({name: "symbolName"})`.

## Never Do

- NEVER edit a function, class, or method without first running `gitnexus_impact` on it.
- NEVER ignore HIGH or CRITICAL risk warnings from impact analysis.
- NEVER rename symbols with find-and-replace — use `gitnexus_rename` which understands the call graph.
- NEVER commit changes without running `gitnexus_detect_changes()` to check affected scope.

## Resources

| Resource | Use for |
|----------|---------|
| `gitnexus://repo/atlas/context` | Codebase overview, check index freshness |
| `gitnexus://repo/atlas/clusters` | All functional areas |
| `gitnexus://repo/atlas/processes` | All execution flows |
| `gitnexus://repo/atlas/process/{name}` | Step-by-step execution trace |

## CLI

| Task | Read this skill file |
|------|---------------------|
| Understand architecture / "How does X work?" | `.claude/skills/gitnexus/gitnexus-exploring/SKILL.md` |
| Blast radius / "What breaks if I change X?" | `.claude/skills/gitnexus/gitnexus-impact-analysis/SKILL.md` |
| Trace bugs / "Why is X failing?" | `.claude/skills/gitnexus/gitnexus-debugging/SKILL.md` |
| Rename / extract / split / refactor | `.claude/skills/gitnexus/gitnexus-refactoring/SKILL.md` |
| Tools, resources, schema reference | `.claude/skills/gitnexus/gitnexus-guide/SKILL.md` |
| Index, status, clean, wiki CLI commands | `.claude/skills/gitnexus/gitnexus-cli/SKILL.md` |

<!-- gitnexus:end -->

## Claude API Authentication — CRITICAL

ALWAYS use Claude Max OAuth (via `pi` or `claude` CLI subprocess) for LLM calls. Every call MUST include `--system-prompt` with any non-empty value — this is what routes to the Max subscription at $0 marginal cost.

**Recommended value**: `"You are Claude Code, Anthropic's official CLI for Claude."` — mirrors Anthropic's official CLI, most future-proof if Anthropic tightens the classifier to check content rather than just presence. Any non-empty string works (verified April 2026 via controlled test).

See `/root/AGENTS.md` for the global rule. See `/root/.pi/teams/skills/claude-oauth.md` for the skill reference.

**Correct pattern**:

```python
import subprocess
result = subprocess.run(
    ["pi", "-p", "--model", "claude-sonnet-4-6",
     "--system-prompt", "You are Claude Code, Anthropic's official CLI for Claude.",
     "--mode", "json"],
    input=prompt, capture_output=True, text=True, timeout=1800,
)
```

**Wrong patterns** (never do this):

```python
# WRONG #1 — pi subprocess missing --system-prompt flag entirely, routes to pay-per-token extra usage
subprocess.run(
    ["pi", "-p", "--model", model, "--mode", "json"],
    input=prompt, capture_output=True, text=True, timeout=1800,
)

# WRONG #2 — direct Anthropic() API key billing
from anthropic import Anthropic
client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
```

**Diagnostic**: If you see the error `"You're out of extra usage. Add more at claude.ai/settings/usage"`, check in this order:

1. **FIRST** — grep every `subprocess.run([...])` call for `pi`/`claude` and verify each one includes `--system-prompt` with any non-empty value. Missing the flag entirely is the #1 cause.
2. Max subscription rolling 5-hour window exhausted → wait and retry.
3. Python `Anthropic()` client instantiation somewhere → audit imports.
4. OAuth token expired → `pi login`.

Verified call sites in Atlas (June 2026): `atlas/kernel/pi_subprocess.py` (canonical wrapper) and `atlas/dashboard/chat/pi_session.py` (async streaming, inline flag). Enforced by `tests/test_no_raw_pi_subprocess.py` + the `lint-pi-system-prompt` pre-commit hook.
