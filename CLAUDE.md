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

## Claude CLI Subprocess — CRITICAL Routing Rule

Every `pi` or `claude` CLI subprocess call in Atlas MUST include:

```
--system-prompt "You are Claude Code, Anthropic's official CLI for Claude."
```

This flag routes the call to the Claude Max subscription ($0 marginal cost). Without it, calls route to pay-per-token "extra usage" billing and will fail with `400 out of extra usage` once credits exhaust.

### Why this matters

Pi CLI's default (no system prompt) routes to pay-per-token extra-usage billing. ANY `--system-prompt` value routes to the Claude Max subscription at $0 marginal cost. Any non-empty string works (verified April 2026); the Claude Code string is recommended as the most future-proof value. API-key / extra-usage calls can burn $10+/minute on a research loop.

### Working Python pattern

```python
import subprocess
result = subprocess.run(
    ["pi", "-p", "--model", "claude-sonnet-4-6",
     "--system-prompt", "You are Claude Code, Anthropic's official CLI for Claude.",
     "--mode", "json"],
    input=prompt, capture_output=True, text=True, timeout=1800,
)
```

### Working bash pattern

```bash
pi -p --model claude-sonnet-4-6 \
   --system-prompt "You are Claude Code, Anthropic's official CLI for Claude." \
   --mode json <<< "$PROMPT"
```

### Wrong pattern (never do this)

```python
# WRONG — no --system-prompt, routes to extra-usage billing
subprocess.run(["pi", "-p", "--model", model, "--mode", "json"], input=prompt, ...)

# ALSO WRONG — direct Anthropic() API-key billing
from anthropic import Anthropic
client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
```

### Diagnosing "out of extra usage" (400 error)

If `400 invalid_request_error: You're out of extra usage` appears:

1. **FIRST** — grep every subprocess call for `pi` or `claude` and verify each includes `--system-prompt` with any non-empty value. Missing this flag entirely is the #1 cause.
2. Only then consider: Max rolling window exhausted, `Anthropic()` client somewhere, or expired OAuth token (`pi login`).

**Never** "fix" this by adding API credits — the fix is always on the auth path.

### Verified June 2026 (post great-deletion)

Atlas pi/claude call sites (all carry `--system-prompt`, enforced by
`tests/test_no_raw_pi_subprocess.py` + the `lint-pi-system-prompt` pre-commit hook):
`atlas/kernel/pi_subprocess.py` (the canonical wrapper) and
`atlas/dashboard/chat/pi_session.py` (async streaming, inline flag).

See `/root/AGENTS.md` and `/root/.pi/teams/skills/claude-auth.md` for the full reference.
