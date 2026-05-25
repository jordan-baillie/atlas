/**
 * Atlas Elastic Agents Extension
 *
 * Governs elastic parallel-agent orchestration for Atlas.
 * Board decision: CONDITIONAL ACCEPT (2026-05-25, vote 5-0).
 *
 * Registers:
 *   Commands:
 *     /elastic-plan    — Dry-run planner: classify, DAG, gates, no spawn
 *     /elastic-run     — Gate eval + optional burst/dispatch (flags below)
 *     /elastic-status  — Show recent audit entries
 *
 *   Tools:
 *     atlas_elastic_plan   — LLM-callable: plan a task (dry-run)
 *     atlas_elastic_run    — LLM-callable: gate check + optional burst or dispatch
 *
 * /elastic-run flags:
 *   --execute-read-only  Actually run burst agents (read_only/planning/review_qa only)
 *   --confirm            Queue swarm dispatch message for write_bounded when gates pass
 *
 * atlas_elastic_run params:
 *   execute_read_only  Boolean — same as --execute-read-only
 *   confirmed          Boolean — same as --confirm
 *
 * Safety invariants:
 *   - live_trading_ops: always blocked; use atlas_risk_check_plan_gate
 *   - write_bounded: blocked if dirty tree or missing ownership;
 *     confirmed=true queues dispatch message only (no auto swarm)
 *   - read_only / planning / review_qa: execute_read_only=true runs burst via pi CLI
 *   - All burst agents use Claude Max OAuth (--system-prompt, --no-session, --mode json)
 *   - No API key (Anthropic() client never instantiated)
 *   - Kill switch: blocks all new spawns
 *   - Audit log: every plan/run attempt → .pi/elastic-agents/audit.jsonl
 *   - ctx.hasUI guard on all UI calls
 *   - atlas_elastic_run counts as delegation activity in TUI widget
 */

import type { ExtensionAPI } from "@mariozechner/pi-coding-agent";
import { Type } from "@sinclair/typebox";
import { resolve } from "node:path";

import {
  loadPolicy,
  defaultPolicyPath,
  type AgentScalePolicy,
} from "./policy.js";
import {
  generatePlan,
  evaluatePlanGates,
  type AgentPlan,
} from "./planner.js";
import {
  evaluateExecutionGate,
  executeElasticRun,
  type ElasticRunResult,
} from "./executor.js";
import {
  writeAuditEntry,
  readAuditEntries,
  makeAuditEntry,
  defaultAuditPath,
} from "./audit.js";

// ─── Helpers ──────────────────────────────────────────────────────────────────

function getPolicy(cwd: string): AgentScalePolicy {
  try {
    return loadPolicy(defaultPolicyPath(cwd));
  } catch {
    // Return a safe minimal fallback policy if config is missing
    return {
      version: "1.0-fallback",
      global: {
        max_concurrent_agents: 4,
        max_write_agents: 2,
        max_parallel_builders_per_file: 1,
        kill_switch: false,
        budget_tokens_per_task: 50000,
      },
      risk_classes: {
        read_only: {
          type: "read_only",
          default_concurrency: 4,
          roles: ["scout", "researcher"],
          gates: [],
          examples: [],
        },
        planning: {
          type: "planning",
          default_concurrency: 2,
          roles: ["planner", "spec_writer"],
          gates: [],
          examples: [],
        },
        review_qa: {
          type: "review_qa",
          default_concurrency: 3,
          roles: ["reviewer", "test-runner", "security-reviewer"],
          gates: [],
          examples: [],
        },
        write_bounded: {
          type: "write_bounded",
          default_concurrency: 2,
          roles: ["builder", "reviewer"],
          gates: [{ clean_working_tree: true }, { file_ownership_table_required: true }],
          examples: [],
        },
        live_trading_ops: {
          type: "live_trading_ops",
          default_concurrency: 1,
          roles: ["executor"],
          gates: [{ explicit_human_approval: true }],
          examples: [],
        },
      },
      agent_roles: {},
      protected_files: [
        "config/live_",
        "config/active_config.json",
        "brokers/state/",
        ".git/",
        "secrets/",
      ],
      approval_gates: {
        live_trading_ops: "explicit",
        write_bounded: "auto_if_tests_pass",
        planning: "auto",
        read_only: "auto",
        review_qa: "auto",
      },
    };
  }
}

function renderPlan(plan: AgentPlan, verbose = false): string {
  const lines: string[] = [
    `📋 Elastic Agent Plan`,
    `  Task ID   : ${plan.task_id}`,
    `  Risk class: ${plan.risk_class}`,
    `  Summary   : ${plan.summary}`,
    `  Agents    : ${plan.concurrency_summary.total_agents} total, max ${plan.concurrency_summary.max_parallel_agents} parallel`,
    `  Est. time : ~${plan.concurrency_summary.estimated_time_minutes} min`,
    ``,
  ];

  if (plan.safety_gates.length > 0) {
    lines.push(`  Safety Gates:`);
    for (const g of plan.safety_gates) {
      const icon = g.satisfied ? "✓" : "✗";
      const req = g.status === "required" ? "[REQUIRED]" : "[optional]";
      lines.push(`    ${icon} ${req} ${g.gate}${g.detail ? ` — ${g.detail}` : ""}`);
    }
    lines.push("");
  }

  if (plan.warnings.length > 0) {
    lines.push(`  ⚠️  Warnings:`);
    for (const w of plan.warnings) lines.push(`    • ${w}`);
    lines.push("");
  }

  if (verbose) {
    lines.push(`  DAG:`);
    for (const phase of plan.proposed_dag.phases) {
      lines.push(`    Phase ${phase.phase}:`);
      for (const a of phase.agents) {
        const deps = a.depends_on?.length ? ` (after: ${a.depends_on.join(", ")})` : "";
        const owns = a.files_owned?.length ? `\n        owns: ${a.files_owned.join(", ")}` : "";
        lines.push(`      • [${a.role}] ${a.id}: ${a.objective}${deps}${owns}`);
      }
    }
    lines.push("");
  }

  lines.push(`  Next step: ${plan.next_step}`);
  if (plan.dry_run) lines.push(`  (dry-run — no agents spawned)`);

  return lines.join("\n");
}

function renderRunResult(plan: AgentPlan, runResult: ElasticRunResult): string {
  const gate = runResult.gate;
  const lines: string[] = [
    renderPlan(plan, false),
    "",
    `Execution gate: ${gate.allowed ? "✓ ALLOWED" : "✗ BLOCKED"}`,
    `Decision      : ${runResult.final_decision}`,
  ];

  if (runResult.final_blockers.length > 0) {
    lines.push("Blockers:");
    for (const b of runResult.final_blockers) lines.push(`  • ${b}`);
  }

  if (gate.read_only_commands?.length && !runResult.burst) {
    lines.push("");
    lines.push("Read-only agent commands (OAuth-only, --no-session, --tools read,grep,find,ls):");
    for (const cmd of gate.read_only_commands) {
      lines.push(`  [${cmd.agent_id}] ${cmd.role}: ${cmd.objective}`);
      lines.push(`    $ ${cmd.command}`);
    }
    lines.push("  Pass --execute-read-only (or execute_read_only=true) to actually run these.");
  }

  if (runResult.burst) {
    const b = runResult.burst;
    lines.push("");
    lines.push(`Burst execution: ${b.agents_run} agents run`);
    lines.push(`  ✓ Succeeded: ${b.agents_succeeded}  ✗ Failed: ${b.agents_failed}`);
    for (const r of b.results) {
      const icon = r.success ? "✓" : "✗";
      const dur = `${(r.duration_ms / 1000).toFixed(1)}s`;
      lines.push(`  ${icon} [${r.agent_id}] ${r.role} (${dur})${r.error ? ` — ${r.error}` : ""}`);
    }
  }

  if (runResult.dispatch_message) {
    lines.push("");
    lines.push(runResult.dispatch_message);
  }

  lines.push("", `Next action: ${gate.next_action}`);
  return lines.join("\n");
}

function renderStatus(entries: ReturnType<typeof readAuditEntries>): string {
  if (entries.length === 0) return "No elastic-agents audit entries found.";
  const displayEntries = entries.slice(-10);
  const lines = [`📊 Elastic Agent Status (last ${displayEntries.length} of ${entries.length} entries)`, ""];
  for (const e of displayEntries) {
    const icon =
      e.decision === "gates_blocked" || e.decision === "write_gate_rejected" || e.decision === "dispatch_rejected"
        ? "✗"
        : e.decision === "gates_passed" || e.decision === "read_only_started" ||
          e.decision === "read_only_complete" || e.decision === "dispatch_requested"
        ? "✓"
        : "·";
    lines.push(
      `  ${icon} [${e.timestamp.slice(0, 19)}] ${e.decision} | ${e.risk_class} | ${e.task_id}`
    );
    if (e.blockers?.length) {
      for (const b of e.blockers) lines.push(`      ✗ ${b}`);
    }
  }
  return lines.join("\n");
}

// ─── Arg parsing helpers ──────────────────────────────────────────────────────

/** Extract named flags from a command args string, returning cleaned args + flags. */
function parseCommandFlags(args: string): {
  cleanArgs: string;
  executeReadOnly: boolean;
  confirmed: boolean;
} {
  const executeReadOnly = args.includes("--execute-read-only");
  const confirmed = args.includes("--confirm");
  const cleanArgs = args
    .replace("--execute-read-only", "")
    .replace("--confirm", "")
    .replace(/\s+/g, " ")
    .trim();
  return { cleanArgs, executeReadOnly, confirmed };
}

// ─── TypeBox schemas ──────────────────────────────────────────────────────────

const ElasticPlanSchema = Type.Object({
  objective: Type.String({
    minLength: 1,
    description: "What needs to be done (the overall task objective).",
  }),
  files_affected: Type.Optional(
    Type.Array(Type.String(), {
      description: "File paths that will be created or modified (helps classify risk).",
    })
  ),
  cwd: Type.Optional(
    Type.String({ description: "Atlas workspace root. Defaults to current project cwd." })
  ),
  dry_run: Type.Optional(
    Type.Boolean({ description: "If true (default), only plan — do not spawn any agents." })
  ),
});

const ElasticRunSchema = Type.Object({
  objective: Type.String({ minLength: 1, description: "Task objective to gate + plan." }),
  files_affected: Type.Optional(Type.Array(Type.String())),
  cwd: Type.Optional(Type.String()),
  execute_read_only: Type.Optional(
    Type.Boolean({
      description:
        "When true, actually run burst agents for read_only/planning/review_qa tasks. " +
        "Default: false (plan/gate only — no agents spawned). " +
        "Uses Claude Max OAuth via pi CLI with --tools read,grep,find,ls --no-session.",
    })
  ),
  confirmed: Type.Optional(
    Type.Boolean({
      description:
        "When true for write_bounded tasks with all gates satisfied, generate a swarm dispatch " +
        "follow-up message with ownership table. Does NOT execute the swarm automatically. " +
        "If working tree is dirty, still rejects.",
    })
  ),
});

// ─── Extension entry point ────────────────────────────────────────────────────

export default function atlasElasticAgents(pi: ExtensionAPI) {

  // ── /elastic-plan ──────────────────────────────────────────────────────────
  pi.registerCommand("elastic-plan", {
    description:
      "Dry-run elastic agent planner: classify task risk, generate DAG, evaluate safety gates. " +
      "No agents are spawned. Risk classes: read_only, planning, review_qa, write_bounded, live_trading_ops. " +
      "Usage: /elastic-plan <objective> [-- file1 file2 ...]",
    handler: async (args, ctx) => {
      const cwd = process.cwd();
      const policy = getPolicy(cwd);
      const auditPath = defaultAuditPath(cwd);

      const parts = args.split(" -- ");
      const objective = parts[0].trim() || "unspecified task";
      const filesAffected = parts[1] ? parts[1].split(/\s+/).filter(Boolean) : [];

      const plan = generatePlan(
        { objective, files_affected: filesAffected, cwd, dry_run: true },
        policy
      );

      const gateResult = evaluatePlanGates(plan, policy);
      const decision = gateResult.approved ? "gates_passed" : "gates_blocked";

      writeAuditEntry(
        makeAuditEntry(plan, decision, { blockers: gateResult.blockers, cwd }),
        auditPath
      );

      const output = renderPlan(plan, true);

      if (ctx.hasUI) {
        ctx.ui.notify(
          `🤖 Elastic plan generated: ${plan.risk_class} (${plan.concurrency_summary.total_agents} agents)`,
          gateResult.approved ? "info" : "warning"
        );
      }

      pi.sendUserMessage(
        `Elastic agent plan generated:\n\`\`\`\n${output}\n\`\`\`\n\n` +
        (gateResult.approved
          ? "Gates satisfied. Use `/elastic-run` (with `--execute-read-only` or `--confirm` as appropriate) to proceed."
          : `Gates blocked:\n${gateResult.blockers.map((b) => `• ${b}`).join("\n")}`),
        { deliverAs: "followUp" }
      );
    },
  });

  // ── /elastic-run ───────────────────────────────────────────────────────────
  pi.registerCommand("elastic-run", {
    description:
      "Evaluate gates + optionally dispatch elastic agents. " +
      "Flags: --execute-read-only (run burst for read_only/planning/review_qa), " +
      "--confirm (queue swarm dispatch for write_bounded when gates pass). " +
      "live_trading_ops: always blocked. " +
      "Usage: /elastic-run <objective> [-- file1 file2 ...] [--execute-read-only] [--confirm]",
    handler: async (args, ctx) => {
      const cwd = process.cwd();
      const policy = getPolicy(cwd);
      const auditPath = defaultAuditPath(cwd);

      const { cleanArgs, executeReadOnly, confirmed } = parseCommandFlags(args);
      const parts = cleanArgs.split(" -- ");
      const objective = parts[0].trim() || "unspecified task";
      const filesAffected = parts[1] ? parts[1].split(/\s+/).filter(Boolean) : [];

      const plan = generatePlan(
        { objective, files_affected: filesAffected, cwd, dry_run: false },
        policy
      );

      // Pre-audit: write read_only_started before burst begins
      const isReadOnlyClass =
        plan.risk_class === "read_only" ||
        plan.risk_class === "planning" ||
        plan.risk_class === "review_qa";

      if (executeReadOnly && isReadOnlyClass) {
        const preGate = evaluateExecutionGate(plan, policy, cwd);
        if (preGate.allowed) {
          writeAuditEntry(makeAuditEntry(plan, "read_only_started", { cwd }), auditPath);
        }
      }

      const runResult = await executeElasticRun(plan, policy, cwd, {
        execute_read_only: executeReadOnly,
        confirmed,
      });

      // Write final audit entry
      writeAuditEntry(
        makeAuditEntry(plan, runResult.final_decision, {
          blockers: runResult.final_blockers,
          cwd,
          extra: runResult.burst
            ? {
                burst_agents_run: runResult.burst.agents_run,
                burst_succeeded: runResult.burst.agents_succeeded,
                burst_failed: runResult.burst.agents_failed,
              }
            : undefined,
        }),
        auditPath
      );

      const output = renderRunResult(plan, runResult);

      if (ctx.hasUI) {
        const isOk =
          runResult.final_decision === "read_only_complete" ||
          runResult.final_decision === "gates_passed" ||
          runResult.final_decision === "dispatch_requested" ||
          (runResult.final_decision === "read_only_started" && runResult.gate.allowed);
        ctx.ui.notify(
          isOk
            ? `✓ Elastic run: ${runResult.final_decision} (${plan.risk_class})`
            : `✗ Elastic run blocked: ${runResult.final_blockers[0] ?? runResult.final_decision}`,
          isOk ? "info" : "warning"
        );
      }

      pi.sendUserMessage(
        `Elastic run result:\n\`\`\`\n${output}\n\`\`\``,
        { deliverAs: "followUp" }
      );
    },
  });

  // ── /elastic-status ────────────────────────────────────────────────────────
  pi.registerCommand("elastic-status", {
    description: "Show recent elastic-agent audit entries from .pi/elastic-agents/audit.jsonl",
    handler: async (_args, ctx) => {
      const cwd = process.cwd();
      const auditPath = defaultAuditPath(cwd);
      const entries = readAuditEntries(auditPath, 20);
      const output = renderStatus(entries);

      if (ctx.hasUI) {
        ctx.ui.notify(`📊 Elastic agent status: ${entries.length} recent entries`, "info");
      }

      pi.sendUserMessage(`\`\`\`\n${output}\n\`\`\``, { deliverAs: "followUp" });
    },
  });

  // ── Tool: atlas_elastic_plan ───────────────────────────────────────────────
  pi.registerTool({
    name: "atlas_elastic_plan",
    label: "Atlas Elastic Agent Plan",
    description:
      "Classify a task's risk, generate a parallel-agent DAG, and evaluate safety gates. " +
      "Returns a structured plan with risk class, phases, concurrency, file ownership, and gate status. " +
      "No agents are spawned (dry-run). Use this before any parallel agent dispatch.",
    parameters: ElasticPlanSchema,
    async execute(_toolCallId, params) {
      const cwd = resolve(params.cwd ?? process.cwd());
      const policy = getPolicy(cwd);
      const auditPath = defaultAuditPath(cwd);

      const plan = generatePlan(
        {
          objective: params.objective,
          files_affected: params.files_affected ?? [],
          cwd,
          dry_run: params.dry_run !== false, // default true
        },
        policy
      );

      const gateResult = evaluatePlanGates(plan, policy);
      const decision = gateResult.approved ? "gates_passed" : "gates_blocked";

      writeAuditEntry(
        makeAuditEntry(plan, decision, { blockers: gateResult.blockers, cwd }),
        auditPath
      );

      const summary = renderPlan(plan, true);
      return {
        content: [{ type: "text", text: summary }],
        details: {
          plan,
          gate_result: gateResult,
          audit_path: auditPath,
        },
      };
    },
  });

  // ── Tool: atlas_elastic_run ────────────────────────────────────────────────
  pi.registerTool({
    name: "atlas_elastic_run",
    label: "Atlas Elastic Agent Run Gate",
    description:
      "Evaluate whether a task can safely proceed to agent dispatch. " +
      "For read-only tasks: returns OAuth-only pi CLI commands for coordinator use. " +
      "With execute_read_only=true: actually runs bounded-concurrency burst for read_only/planning/review_qa. " +
      "For write_bounded tasks: verifies clean tree + gates; ALWAYS requires human confirmation. " +
      "With confirmed=true: generates swarm dispatch message (does NOT auto-execute). " +
      "For live_trading_ops: always blocked; use atlas_risk_check_plan_gate instead. " +
      "Audit entries written to .pi/elastic-agents/audit.jsonl. " +
      "Counts as delegation activity in TUI widget.",
    parameters: ElasticRunSchema,
    async execute(_toolCallId, params) {
      const cwd = resolve(params.cwd ?? process.cwd());
      const policy = getPolicy(cwd);
      const auditPath = defaultAuditPath(cwd);
      const executeReadOnly = params.execute_read_only === true;
      const confirmed = params.confirmed === true;

      const plan = generatePlan(
        {
          objective: params.objective,
          files_affected: params.files_affected ?? [],
          cwd,
          dry_run: false,
        },
        policy
      );

      // Pre-audit for burst: write read_only_started before agents fire
      const isReadOnlyClass =
        plan.risk_class === "read_only" ||
        plan.risk_class === "planning" ||
        plan.risk_class === "review_qa";

      if (executeReadOnly && isReadOnlyClass) {
        const preGate = evaluateExecutionGate(plan, policy, cwd);
        if (preGate.allowed) {
          writeAuditEntry(makeAuditEntry(plan, "read_only_started", { cwd }), auditPath);
        }
      }

      const runResult = await executeElasticRun(plan, policy, cwd, {
        execute_read_only: executeReadOnly,
        confirmed,
      });

      // Write final audit entry
      writeAuditEntry(
        makeAuditEntry(plan, runResult.final_decision, {
          blockers: runResult.final_blockers,
          cwd,
          extra: runResult.burst
            ? {
                burst_agents_run: runResult.burst.agents_run,
                burst_succeeded: runResult.burst.agents_succeeded,
                burst_failed: runResult.burst.agents_failed,
              }
            : undefined,
        }),
        auditPath
      );

      const gate = runResult.gate;
      const textLines: string[] = [
        `Execution gate: ${gate.allowed ? "ALLOWED" : "BLOCKED"}`,
        `Risk class    : ${plan.risk_class}`,
        `Decision      : ${runResult.final_decision}`,
      ];
      if (runResult.final_blockers.length > 0) {
        textLines.push(`Blockers: ${runResult.final_blockers.join(" | ")}`);
      }
      textLines.push(`Next action: ${gate.next_action}`);

      if (gate.read_only_commands?.length && !runResult.burst) {
        textLines.push(`Read-only commands (${gate.read_only_commands.length} agents):`);
        for (const cmd of gate.read_only_commands) {
          textLines.push(`  ${cmd.agent_id} [${cmd.role}]: ${cmd.command}`);
        }
      }

      if (runResult.burst) {
        const b = runResult.burst;
        textLines.push(
          `Burst: ${b.agents_run} run, ${b.agents_succeeded} succeeded, ${b.agents_failed} failed`
        );
      }

      if (runResult.dispatch_message) {
        textLines.push("", runResult.dispatch_message);
      }

      return {
        content: [{ type: "text", text: textLines.join("\n") }],
        details: {
          plan,
          run_result: runResult,
          audit_path: auditPath,
        },
      };
    },
  });
}
