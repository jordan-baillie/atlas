# Atlas Living System Plan — Three-Layer Agentic Architecture

## Core Insight

The [claude-code-hooks-mastery](https://github.com/disler/claude-code-hooks-mastery) repo reveals a pattern we're missing: **reactive extensions that intercept lifecycle events create automatic behavior.** Skills alone are passive — they wait to be loaded. Extensions are the nervous system that makes the organism react to stimuli without conscious thought.

Atlas already has 4 extensions (atlas-jobs, atlas-state, atlas-risk-gates, atlas-artifacts) providing custom tools. But we have **zero reactive hooks** — no event interception, no safety gates, no auto-context injection, no post-tool validation. Every session starts cold. Every dangerous command passes through unchecked. Every learning is manual.

## What Exists Today

### Extensions (pi-package/atlas-ops/extensions/)
| Extension | What it does | Gap |
|-----------|-------------|-----|
| `atlas-jobs` | Custom tool to run jobs (backtest, ingest, etc.) | ✅ Good |
| `atlas-state` | Custom tool to read/write state files | ✅ Good |
| `atlas-risk-gates` | Custom tool for risk checks | ✅ Good |
| `atlas-artifacts` | Custom tool to load/summarize JSON artifacts | ✅ Good |
| **No lifecycle hooks** | **Zero event interception** | ❌ Critical gap |

### Skills (pi-package/atlas-ops/skills/)
| Skill | Status |
|-------|--------|
| atlas-daily | ⚠️ Stale (refs deleted paper_engine) |
| atlas-director | ✅ Good |
| atlas-healthz | ⚠️ Basic (61 lines, no references) |
| atlas-reoptimize | ✅ Good |
| atlas-research | ⚠️ Minimal (30 lines) |
| atlas-research-loop | ⚠️ Stale (old 4-hat architecture) |
| atlas-strategy-discovery | ✅ Comprehensive |
| **No background knowledge** | ❌ Every session re-discovers codebase |
| **No incident response** | ❌ Every error diagnosed from scratch |

### Infrastructure
- 8 systemd services (3 currently failed: director, research-runner, research-window)
- 10+ cron jobs (healthz, premarket, postclose, intraday, weekly maintenance)
- Telegram bot (active, handles /task dispatch)
- Dashboard (active, 10s refresh loop)
- Brain vault (152 learnings, 35 experiments, 7 closed decisions)
- 136 lines of lessons.md (35 rules)

---

## Architecture: Three Layers

```
┌─────────────────────────────────────────────────────┐
│  LAYER 1: EXTENSIONS (Nervous System)               │
│  Reactive hooks that fire automatically              │
│                                                      │
│  session_start    → inject context + status widget   │
│  before_agent_start → modify system prompt per-task  │
│  input            → validate & transform prompts     │
│  tool_call        → safety gates, block dangerous    │
│  tool_result      → auto-validate writes             │
│  agent_end        → capture learnings, suggest deploy │
│  session_shutdown → save state                       │
├─────────────────────────────────────────────────────┤
│  LAYER 2: SKILLS (Brain)                            │
│  Knowledge + procedures, auto-loaded on keywords     │
│                                                      │
│  Background:  codebase | lessons | state-queries     │
│  Situational: incident | backtest | brain | deploy   │
│               promote  | data-audit | report         │
├─────────────────────────────────────────────────────┤
│  LAYER 3: COMMANDS (Hands)                          │
│  Explicit dispatch via /command or Telegram /task     │
│                                                      │
│  /healthz  /deploy  /backtest  /promote              │
│  /incident  /brain  /report  /meta-agent             │
└─────────────────────────────────────────────────────┘
```

The three layers compose: An extension detects an error → loads the incident skill → runs the fix procedure → triggers the deploy command → validates the result → captures the learning. All automatic. All without human intervention.

---

## Layer 1: Extensions (Nervous System)

These are Pi extensions using `pi.on()` event hooks. They intercept agent lifecycle events and react automatically. This is the single biggest gap in Atlas today.

### Extension 1: `atlas-context-injector`
**Events:** `session_start`, `before_agent_start`

**What it does:**
On `session_start`:
- Read current system state (services up/down, broker status, last error)
- Set a status widget showing: equity, open positions count, research progress
- Load the last 5 Telegram alerts (unresolved)

On `before_agent_start`:
- Analyze the user's prompt to detect intent (research? ops? debugging? strategy work?)
- Inject relevant context into the system prompt:
  - If prompt mentions strategies → inject BaseStrategy contract + common bugs
  - If prompt mentions broker/trading → inject broker integration notes + lesson #12-14
  - If prompt mentions config/promote → inject promotion checklist + closed decisions
  - If prompt mentions research/sweep → inject current brain/ summary + what's been tested
  - Always inject: current market (SP500), current config version, failed services

**Why this is transformative:** Every agent session currently wastes 2-5 minutes reading files to orient itself. This extension means every agent starts with perfect context. It's the equivalent of the hooks-mastery `setup.py` + `session_start.py` but with domain-specific Atlas intelligence.

```typescript
// Pseudocode
pi.on("session_start", async (_event, ctx) => {
  const state = getSystemState();  // services, broker, equity, errors
  ctx.ui.setWidget("atlas-status", [
    `💰 Equity: $${state.equity}  |  📊 Positions: ${state.positionCount}`,
    `🔬 Research: ${state.sweepProgress}  |  ⚠️ Alerts: ${state.unresolvedAlerts}`,
    state.failedServices.length ? `🔴 Failed: ${state.failedServices.join(", ")}` : "🟢 All services healthy"
  ]);
});

pi.on("before_agent_start", async (event, ctx) => {
  const intent = classifyIntent(event.prompt);
  const injection = buildContextInjection(intent);
  return {
    systemPrompt: event.systemPrompt + "\n\n" + injection
  };
});
```

---

### Extension 2: `atlas-safety-gates`
**Events:** `tool_call`

**What it does:** Intercepts dangerous tool calls before execution.

**Gates (block with reason):**
1. **State file protection** — Block writes to `paper_state*.json`, `equity_curve*.json` when broker is offline (lesson #12)
2. **Config protection** — Block writes to `config/active/` without confirmation ("Are you sure you want to modify live config?")
3. **Data protection** — Block `rm -rf` on `data/cache/`, `brain/`, `reports/`
4. **Live trading guard** — Block `scripts/live_executor.py` without explicit confirmation
5. **Destructive research** — Block deleting brain/ files or closed decisions without confirmation

**Gates (warn but allow):**
1. **Stale data warning** — If writing a plan file and cache is >24h old, warn
2. **Service mismatch** — If editing a file that belongs to a running service, warn about restart needed

```typescript
pi.on("tool_call", async (event, ctx) => {
  if (isToolCallEventType("write", event)) {
    if (event.input.path.includes("config/active/")) {
      const ok = await ctx.ui.confirm(
        "⚠️ Live Config Change",
        `You're about to modify live trading config: ${event.input.path}\nThis affects active trading. Continue?`
      );
      if (!ok) return { block: true, reason: "Config change cancelled by user" };
    }
  }
  
  if (isToolCallEventType("bash", event)) {
    if (event.input.command.includes("live_executor")) {
      const ok = await ctx.ui.confirm(
        "🔴 LIVE TRADING",
        "This will execute real trades with real money. Are you absolutely sure?"
      );
      if (!ok) return { block: true, reason: "Live trading blocked by user" };
    }
  }
});
```

**Why this matters:** Currently NOTHING prevents an agent from accidentally writing corrupt state files during broker downtime (which has caused 3+ incidents). This extension is a safety net that prevents the entire class of "agent broke things by writing when it shouldn't have."

---

### Extension 3: `atlas-post-tool-validator`
**Events:** `tool_result`

**What it does:** After certain file writes, automatically validates the result.

Inspired by the hooks-mastery `ruff_validator.py` — but for Atlas-specific file types:

1. **Strategy file written** → Run `python3 -c "from strategies.X import X; X()"` sanity import
2. **Config file written** → Validate JSON structure, check all required keys present
3. **State file written** → Validate JSON parseable, check schema matches expected
4. **Python file written** → Run `python3 -m py_compile <file>` to catch syntax errors
5. **Plan file written** → Validate plan JSON has required fields (signals, market_id, date)

```typescript
pi.on("tool_result", async (event, ctx) => {
  if (event.toolName === "write" && event.input.path.endsWith(".py")) {
    const result = await pi.exec("python3", ["-m", "py_compile", event.input.path]);
    if (result.code !== 0) {
      return {
        decision: "block",
        reason: `Syntax error in ${event.input.path}:\n${result.stderr}`
      };
    }
  }
});
```

---

### Extension 4: `atlas-session-lifecycle`
**Events:** `agent_end`, `session_before_compact`, `session_shutdown`

**What it does:**

On `agent_end`:
- Detect which files were modified during this agent turn
- Map modified files → affected services
- If any running service is affected, notify: "Files changed in {service}. Run /deploy to restart."
- If a strategy file was modified, suggest: "Strategy modified. Run sanity test?"

On `session_before_compact`:
- Save the current brain/ INDEX.md hash (so we can detect if brain was updated)
- Include key state snapshots in compaction summary

On `session_shutdown`:
- Log session summary (what was done, files changed, errors encountered)
- If any new lessons were identified during session, prompt to update lessons.md

---

### Extension 5: `atlas-commands`
**Events:** None (registers commands + shortcuts)

**What it does:** Registers slash commands for quick dispatch.

```typescript
pi.registerCommand("healthz", {
  description: "Run Atlas system health audit",
  handler: async (args, ctx) => {
    pi.sendUserMessage("Run the atlas-healthz skill — full system health audit. Report findings via Telegram.", 
      { deliverAs: "followUp" });
  }
});

pi.registerCommand("deploy", {
  description: "Deploy changes — restart affected services",
  handler: async (args, ctx) => {
    pi.sendUserMessage("Load the atlas-deploy skill. Check git diff for changed files, map to services, restart in order, verify health.");
  }
});

pi.registerCommand("backtest", {
  description: "Quick backtest: /backtest <strategy> [params]",
  handler: async (args, ctx) => {
    pi.sendUserMessage(`Load the atlas-backtest skill. Run: ${args}. Check brain/ first for prior results.`);
  }
});

pi.registerCommand("incident", {
  description: "Diagnose and fix: /incident <error message>",
  handler: async (args, ctx) => {
    pi.sendUserMessage(`Load the atlas-incident skill. Error: ${args}. Diagnose, fix, verify, report to Telegram.`);
  }
});

pi.registerCommand("promote", {
  description: "Config promotion pipeline: /promote <market>",
  handler: async (args, ctx) => {
    pi.sendUserMessage(`Load the atlas-promote skill. Promote ${args}. Run full pipeline: regression, OOS, backup, promote, verify.`);
  }
});

pi.registerCommand("brain", {
  description: "Query research knowledge base: /brain <query>",
  handler: async (args, ctx) => {
    pi.sendUserMessage(`Load the atlas-brain skill. Query: ${args}. Check brain/ INDEX.md, closed decisions, confirmed patterns.`);
  }
});

pi.registerCommand("report", {
  description: "Generate performance report",
  handler: async (args, ctx) => {
    pi.sendUserMessage("Load the atlas-report skill. Generate weekly performance report with charts. Deliver via Telegram.");
  }
});
```

Plus keyboard shortcuts:
```typescript
pi.registerShortcut("ctrl+shift+h", {
  description: "Quick health check",
  handler: async (ctx) => { pi.sendUserMessage("/healthz"); }
});

pi.registerShortcut("ctrl+shift+d", {
  description: "Deploy changes",
  handler: async (ctx) => { pi.sendUserMessage("/deploy"); }
});
```

---

### Extension 6: `atlas-status-dashboard`
**Events:** `session_start`, `turn_end`

**What it does:** Real-time status display in the Pi footer/widgets.

On `session_start` and periodically on `turn_end`:
- Status bar: `[SP500 $12,340 | 3 pos | Sharpe 0.69 | 🟢 Healthy]` or `[🔴 3 services down | ⚠️ 2 alerts]`
- Widget showing research progress if a sweep is running

```typescript
pi.on("session_start", async (_event, ctx) => {
  await updateDashboard(ctx);
});

pi.on("turn_end", async (_event, ctx) => {
  await updateDashboard(ctx);
});

async function updateDashboard(ctx) {
  const state = await getQuickState();
  ctx.ui.setStatus("atlas", 
    `${state.healthEmoji} SP500 $${state.equity} | ${state.positions} pos | ` +
    `Sharpe ${state.sharpe} | ${state.alertCount} alerts`
  );
}
```

---

## Layer 2: Skills (Brain)

Same as the v2 plan but with explicit interaction points to Layer 1 extensions.

### Background Skills (`user-invocable: false`)

These are the institutional memory. Every agent auto-loads them when relevant keywords appear.

#### `atlas-codebase`
Architecture map, module responsibilities, file locations, config structure, CLI commands.
- **Generated by extension:** `atlas-context-injector` can inject relevant sections from this skill based on detected intent, so agents don't need to load the full skill every time.

#### `atlas-lessons`
35 lessons organized by domain, 7 closed decisions, 5 confirmed patterns.
- **Fed by extension:** `atlas-session-lifecycle` prompts to update this after corrections.
- **Consumed by extension:** `atlas-context-injector` injects relevant lessons into system prompt.

#### `atlas-state-queries`
How to check services, broker, positions, equity, config, research, dashboard.
- **Complemented by extension:** `atlas-status-dashboard` shows live state in footer, reducing need to manually query.

### Situational Skills (auto-invoke on keyword match)

#### `atlas-incident`
Error diagnosis & resolution with 20+ known failure patterns.
- **Triggered by extension:** `atlas-context-injector` detects error keywords and can suggest loading this skill.
- **Dispatched by command:** `/incident <error>`
- **Auto-fixes gated by extension:** `atlas-safety-gates` prevents dangerous fixes.

#### `atlas-backtest`
Strategy testing, comparison, interpretation, brain recording.
- **Dispatched by command:** `/backtest <strategy> [params]`
- **Results validated by extension:** `atlas-post-tool-validator` checks output files.

#### `atlas-brain`
Research knowledge navigation, closed decision checks, parameter ranges.
- **Dispatched by command:** `/brain <query>`
- **Fed by extension:** `atlas-session-lifecycle` records new findings to brain/.

#### `atlas-deploy`
Service deployment with file→service mapping and health verification.
- **Dispatched by command:** `/deploy`
- **Triggered by extension:** `atlas-session-lifecycle` suggests deploy when affected files change.
- **Gated by extension:** `atlas-safety-gates` confirms service restarts.
- `disable-model-invocation: true` (side effects)

#### `atlas-promote`
Config promotion pipeline with regression, OOS, backup, monitoring.
- **Dispatched by command:** `/promote <market>`
- **Gated by extension:** `atlas-safety-gates` requires confirmation for config changes.
- `disable-model-invocation: true` (side effects)

#### `atlas-data-audit`
Data quality: cache freshness, ticker validity, price accuracy, adjustment mode.
- **Auto-triggered:** When `atlas-context-injector` detects stale data on session start.

#### `atlas-report`
Performance reporting with charts, Telegram delivery.
- **Dispatched by command:** `/report`
- **Schedulable:** Can be run by weekly cron job.

### Upgrades to Existing Skills

#### `atlas-daily` → Full Rewrite
- Alpaca broker, market-specific plans, volatility gate, protective order sync
- Interacts with: `atlas-context-injector` (pre-flight state), `atlas-safety-gates` (trading guards), `atlas-deploy` (post-execution)

#### `atlas-healthz` → Add References
- `references/check-catalog.md` (52 health checks), `references/autofix-rules.md`
- Interacts with: `atlas-incident` (auto-fix found issues), `atlas-status-dashboard` (display results)

#### `atlas-research-loop` → Refactor for Autoresearch
- New architecture: sweep.py + loop.py + brain/ memory
- Interacts with: `atlas-brain` (check prior work), `atlas-backtest` (run experiments), `atlas-lessons` (anti-patterns)

---

## Layer 3: Commands (Hands)

Registered by `atlas-commands` extension. These are the explicit dispatch interface — usable from:
1. Pi interactive session (`/healthz`)
2. Telegram bot (`/task @healthz`)
3. Cron jobs (pi-cron dispatch)
4. Other agents (via `pi.sendUserMessage`)

| Command | Maps to | Confirmation? |
|---------|---------|---------------|
| `/healthz` | atlas-healthz skill | No |
| `/deploy` | atlas-deploy skill | Yes (via safety gate) |
| `/backtest <args>` | atlas-backtest skill | No |
| `/promote <market>` | atlas-promote skill | Yes (via safety gate) |
| `/incident <error>` | atlas-incident skill | No |
| `/brain <query>` | atlas-brain skill | No |
| `/report` | atlas-report skill | No |
| `/daily <market>` | atlas-daily skill | Yes (trading) |

---

## Composition: How the Three Layers Create a Living System

### Scenario 1: Agent encounters error during research sweep

```
1. Research agent runs backtest → strategy crashes
2. [Extension] atlas-post-tool-validator detects non-zero exit
3. [Extension] atlas-context-injector recognizes error keywords
4. [Skill] atlas-incident auto-loads (keyword match)
   → Reads atlas-lessons (background): lesson #15 — dormant strategy API drift
   → Checks references/fix-catalog.md: matches "generate_signals signature mismatch"
   → Applies fix: update method signature
5. [Extension] atlas-post-tool-validator validates the fix compiles
6. [Extension] atlas-session-lifecycle detects file change, suggests /deploy
7. [Command] /deploy restarts research-runner
8. [Extension] atlas-safety-gates confirms restart
9. [Extension] atlas-status-dashboard updates: "🟢 research-runner healthy"
10. [Skill] atlas-brain records: "strategy X had signature bug, fixed"
```

**Human involvement: zero.** The system diagnosed, fixed, validated, deployed, and recorded the fix.

### Scenario 2: Weekly performance review (Sunday cron)

```
1. Cron triggers: pi --print "/report"
2. [Command] /report dispatches atlas-report skill
3. [Skill] atlas-report loads:
   → Reads equity curve files (week's data)
   → Reads trade history
   → Reads research journal (experiments this week)
   → Checks brain/ for new discoveries
4. [Extension] atlas-context-injector provides: current equity, positions, Sharpe
5. [Skill] generates charts via utils/charts.py
6. [Skill] sends formatted report + charts to Telegram
7. [Skill] saves report to reports/weekly/
```

**Human involvement: read Telegram on your phone.**

### Scenario 3: Interactive development session

```
1. Developer opens pi session
2. [Extension] atlas-context-injector fires on session_start:
   → Status widget shows: "$12,340 | 3 pos | 🔴 research-runner down"
   → Injects: "Note: research-runner is down since 03:15 UTC. Last error: OOM"
3. Developer types: "fix the research runner"
4. [Extension] atlas-context-injector on before_agent_start:
   → Detects "fix" + "research runner" → injects service-map, common fixes
5. [Skill] atlas-incident auto-loads (keyword: fix, error, down)
   → Checks logs: OOM kill at 03:15
   → Fix: increase memory limit in systemd service
6. Developer approves the fix
7. [Extension] atlas-safety-gates confirms systemctl edit
8. [Extension] atlas-session-lifecycle detects service file changed
9. Developer types: /deploy
10. [Extension] atlas-status-dashboard updates: "🟢 All services healthy"
11. [Extension] atlas-session-lifecycle captures: "research-runner OOM → increased to 4GB"
```

---

## Implementation Plan

### Phase 1: Foundation Extensions (highest leverage)
Build the reactive nervous system first — this multiplies the value of everything else.

| Extension | Events Used | Impact |
|-----------|------------|--------|
| `atlas-context-injector` | `session_start`, `before_agent_start` | Every session starts with perfect context |
| `atlas-safety-gates` | `tool_call` | Prevents accidental damage |
| `atlas-commands` | registerCommand ×8, registerShortcut ×3 | Quick dispatch from anywhere |
| `atlas-status-dashboard` | `session_start`, `turn_end` | Live system state visible |

**Estimated effort:** 2-3 hours for all four (they're small TypeScript files)
**Estimated impact:** Eliminates orientation tax for ALL agents immediately

### Phase 2: Background Knowledge Skills
Build the institutional memory that extensions inject.

| Skill | Lines | References |
|-------|-------|-----------|
| `atlas-codebase` | ~300 | 3 reference files |
| `atlas-lessons` | ~250 | 2 reference files |
| `atlas-state-queries` | ~200 | 2 reference files |

**Estimated effort:** 2-3 hours
**Estimated impact:** Every agent has instant domain knowledge

### Phase 3: Core Situational Skills
Build the most-used procedural skills.

| Skill | Lines | References |
|-------|-------|-----------|
| `atlas-incident` | ~400 | 3 reference files (fix-catalog, logs, broker-debug) |
| `atlas-backtest` | ~350 | 3 reference files |
| `atlas-brain` | ~250 | 2 reference files |

**Estimated effort:** 3-4 hours
**Estimated impact:** 80% of daily agent situations covered

### Phase 4: Validation Extension + Remaining Skills
Close the loop with auto-validation and remaining skills.

| Item | Type | Impact |
|------|------|--------|
| `atlas-post-tool-validator` | Extension | Auto-catches bad writes |
| `atlas-session-lifecycle` | Extension | Auto-captures learnings, suggests deploy |
| `atlas-deploy` | Skill | Deployment procedure |
| `atlas-promote` | Skill | Config promotion pipeline |
| `atlas-data-audit` | Skill | Data quality assurance |

**Estimated effort:** 3-4 hours

### Phase 5: Existing Skill Upgrades + Reporting
Bring stale skills up to date and add reporting.

| Item | Type | Impact |
|------|------|--------|
| `atlas-daily` rewrite | Skill | Operational reliability |
| `atlas-healthz` upgrade | Skill | Deeper health coverage |
| `atlas-research-loop` rewrite | Skill | Matches current architecture |
| `atlas-report` | Skill | Autonomous weekly reports |

**Estimated effort:** 2-3 hours

---

## File Organization

All new work lives in the existing pi-package structure:

```
pi-package/atlas-ops/
├── extensions/
│   ├── atlas-artifacts/          # ✅ EXISTS
│   ├── atlas-jobs/               # ✅ EXISTS
│   ├── atlas-risk-gates/         # ✅ EXISTS
│   ├── atlas-state/              # ✅ EXISTS
│   ├── atlas-context-injector/   # 🆕 NEW — session_start + before_agent_start
│   │   └── src/index.ts
│   ├── atlas-safety-gates/       # 🆕 NEW — tool_call interception
│   │   └── src/index.ts
│   ├── atlas-post-tool-validator/ # 🆕 NEW — tool_result validation
│   │   └── src/index.ts
│   ├── atlas-session-lifecycle/  # 🆕 NEW — agent_end + compact + shutdown
│   │   └── src/index.ts
│   ├── atlas-commands/           # 🆕 NEW — slash commands + shortcuts
│   │   └── src/index.ts
│   └── atlas-status-dashboard/   # 🆕 NEW — widgets + status bar
│       └── src/index.ts
├── skills/
│   ├── atlas-daily/              # ⚠️ REWRITE
│   ├── atlas-director/           # ✅ KEEP
│   ├── atlas-healthz/            # ⚠️ UPGRADE (add references/)
│   ├── atlas-reoptimize/         # ✅ KEEP
│   ├── atlas-research/           # ❌ MERGE into atlas-brain
│   ├── atlas-research-loop/      # ⚠️ REWRITE
│   ├── atlas-strategy-discovery/ # ✅ KEEP
│   ├── atlas-codebase/           # 🆕 NEW — background knowledge
│   │   ├── SKILL.md
│   │   └── references/
│   ├── atlas-lessons/            # 🆕 NEW — background knowledge
│   │   ├── SKILL.md
│   │   └── references/
│   ├── atlas-state-queries/      # 🆕 NEW — background knowledge
│   │   ├── SKILL.md
│   │   └── references/
│   ├── atlas-incident/           # 🆕 NEW — error resolution
│   │   ├── SKILL.md
│   │   └── references/
│   ├── atlas-backtest/           # 🆕 NEW — testing workflow
│   │   ├── SKILL.md
│   │   └── references/
│   ├── atlas-brain/              # 🆕 NEW — knowledge navigation
│   │   ├── SKILL.md
│   │   └── references/
│   ├── atlas-deploy/             # 🆕 NEW — deployment
│   │   ├── SKILL.md
│   │   └── references/
│   ├── atlas-promote/            # 🆕 NEW — config promotion
│   │   ├── SKILL.md
│   │   └── references/
│   ├── atlas-data-audit/         # 🆕 NEW — data quality
│   │   ├── SKILL.md
│   │   └── references/
│   └── atlas-report/             # 🆕 NEW — performance reports
│       ├── SKILL.md
│       └── references/
└── package.json                  # Already discovers extensions/**/ and skills/
```

The existing `package.json` glob pattern `"./extensions/*/src/index.ts"` will automatically discover all new extensions. Skills under `"./skills"` are also auto-discovered.

---

## Expected Impact

### Before (current state)
- Every session: 2-5 min orientation (read files, check state, remember lessons)
- Every error: 15-30 min diagnosis from scratch
- Every backtest: guess at CLI commands, forget to check brain/
- Every code change: forget to restart services 30% of the time
- Every promotion: manual multi-step with high error risk
- Zero safety gates: agents can corrupt state files freely

### After (three-layer system)
- Every session: instant orientation (extension injects context)
- Every error: known patterns auto-matched (incident skill + fix-catalog)
- Every backtest: one command (/backtest), brain/ checked automatically
- Every code change: extension detects + suggests deploy
- Every promotion: full pipeline via /promote with gates
- Safety gates: dangerous operations blocked or confirmed

### Quantified
| Source | Savings |
|--------|---------|
| Context injection (all sessions) | ~2-5 min/session × 20 sessions/day = **40-100 min/day** |
| Incident auto-resolution | ~15-30 min × 3-5 incidents/week = **45-150 min/week** |
| Backtest workflow | ~5 min × 10-20 backtests/day = **50-100 min/day** |
| Deploy automation | Prevents 1-2 "forgot to restart" incidents/week = **30-60 min/week** |
| Safety gates | Prevents 1-2 state corruption incidents/month = **2-4 hours/month** |
| Learning capture | Compounds: prevents re-testing + prevents repeat mistakes = **∞** |

### The Compounding Effect
This system gets better over time without code changes:
- `atlas-incident` gets smarter as fix-catalog grows
- `atlas-brain` gets more valuable as brain/ accumulates evidence
- `atlas-lessons` prevents more mistakes as lessons.md grows
- `atlas-context-injector` gives better context as it learns which injections help
- `atlas-post-tool-validator` catches more bugs as validation rules expand

**End state: a living system that learns from every interaction, reacts to every event, and operates autonomously from a phone.**
