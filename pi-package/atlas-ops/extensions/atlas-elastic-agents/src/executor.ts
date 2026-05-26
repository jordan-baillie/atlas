/**
 * atlas-elastic-agents — Execution Gate & Dispatcher
 *
 * Evaluates whether a plan can be safely executed.
 *
 * For read-only tasks (read_only / planning / review_qa):
 *   - Synchronous gate returns CLI command hints
 *   - With execute_read_only=true: runs actual bounded-concurrency pi CLI burst
 *
 * For write_bounded:
 *   - Blocked if dirty tree or gates unsatisfied
 *   - With confirmed=true + gates passed: queues swarm dispatch message (no auto-execute)
 *
 * For live_trading_ops: ALWAYS blocked; use atlas_risk_check_plan_gate.
 *
 * NEVER uses Anthropic API key — Claude Max OAuth only (pi CLI).
 * All pi subprocess calls pass --system-prompt for OAuth routing.
 * No shell interpolation — subprocess args are arrays.
 *
 * Exports are pure functions or injectable async functions.
 * No Pi runtime dependency.
 */

import { spawn } from "node:child_process";
import { spawnSync } from "node:child_process";
import {
  type AgentScalePolicy,
  type RiskClass,
  isKillSwitchActive,
} from "./policy.js";
import {
  type AgentPlan,
  type OverallGateResult,
  checkGitStatus,
  evaluatePlanGates,
} from "./planner.js";
import { type AuditDecision } from "./audit.js";

// ─── Execution gate result ────────────────────────────────────────────────────

export interface ExecutionGateResult {
  allowed: boolean;
  decision: AuditDecision;
  risk_class: RiskClass;
  blockers: string[];
  warnings: string[];
  read_only_commands?: ReadOnlyCommand[];
  next_action: string;
}

export interface ReadOnlyCommand {
  agent_id: string;
  role: string;
  objective: string;
  /** pi CLI command string (OAuth-only, printed for review). */
  command: string;
}

// ─── Burst runner types ────────────────────────────────────────────────────────

export interface BurstAgentResult {
  agent_id: string;
  role: string;
  objective: string;
  success: boolean;
  output?: string;
  error?: string;
  duration_ms: number;
  /** Timeout applied to this agent (ms). Visible in results and audit for diagnostics. */
  timeout_ms?: number;
}

export interface BurstRunResult {
  started_at: string;
  completed_at: string;
  agents_run: number;
  agents_succeeded: number;
  agents_failed: number;
  results: BurstAgentResult[];
  errors: string[];
}

/**
 * Injectable runner function for burst execution.
 * Default implementation uses pi CLI (Claude Max OAuth, never API key).
 * Tests inject a mock to avoid real subprocess spawning.
 */
export type BurstRunnerFn = (
  agentId: string,
  role: string,
  objective: string,
  timeoutMs: number
) => Promise<BurstAgentResult>;

// ─── Elastic run options and result ───────────────────────────────────────────

export interface ElasticRunOptions {
  /**
   * When true, actually run burst agents for read_only/planning/review_qa tasks.
   * Default: false (plan/gate only — no agents spawned).
   */
  execute_read_only?: boolean;
  /**
   * When true for write_bounded tasks with all gates satisfied, generate a
   * swarm dispatch follow-up message. Does NOT execute the swarm automatically.
   */
  confirmed?: boolean;
  /** Custom runner function (injectable for testing). Default: defaultBurstRunner. */
  runner?: BurstRunnerFn;
  /**
   * Per-agent timeout override in ms.
   * When omitted, each agent uses its role's timeout_sec from policy.agent_roles
   * (e.g. reviewer/test-runner/security-reviewer → 300s, researcher → 600s).
   * Falls back to DEFAULT_BURST_TIMEOUT_MS (300s) for roles not in policy.
   */
  timeoutMs?: number;
  /**
   * Max concurrent agents. Default: 4 (hard safe default).
   * Capped at policy.global.max_concurrent_agents.
   * Caller can specify lower or higher (within global max).
   */
  maxConcurrent?: number;
}

export interface ElasticRunResult {
  gate: ExecutionGateResult;
  burst?: BurstRunResult;
  dispatch_message?: string;
  final_decision: AuditDecision;
  final_blockers: string[];
}

// ─── OAuth-only command builder ───────────────────────────────────────────────

const SYSTEM_PROMPT = "You are Claude Code, Anthropic's official CLI for Claude.";
const DEFAULT_MODEL = "claude-sonnet-4-6";
/**
 * Fallback timeout when a role has no entry in policy.agent_roles.
 * 300s is safe for read-only burst agents (no write side-effects).
 * Per-role overrides from policy.agent_roles[role].timeout_sec take precedence.
 */
const DEFAULT_BURST_TIMEOUT_MS = 300_000; // 5 minutes — read-only burst fallback
const DEFAULT_MAX_CONCURRENT = 4;         // hard safe default cap

// ─── Per-role timeout resolver ───────────────────────────────────────────────

/**
 * Resolve the effective timeout for a single burst agent.
 *
 * Priority:
 *   1. `overrideMs` — explicit caller override (timeoutMs option); always wins.
 *   2. `policy.agent_roles[role].timeout_sec * 1000` — per-role policy value.
 *   3. `DEFAULT_BURST_TIMEOUT_MS` (300s) — safe fallback for unmapped roles.
 *
 * This ensures review_qa roles (reviewer/test-runner/security-reviewer) get
 * their configured 300s from policy instead of the old 120s hard default.
 */
export function resolveAgentTimeoutMs(
  role: string,
  policy: AgentScalePolicy,
  overrideMs?: number
): number {
  if (overrideMs !== undefined) return overrideMs;
  const roleConfig = policy.agent_roles[role];
  if (roleConfig?.timeout_sec) return roleConfig.timeout_sec * 1000;
  return DEFAULT_BURST_TIMEOUT_MS;
}

/**
 * Build a pi CLI command string for display purposes.
 * Actual subprocess spawning uses args-array form (no shell interpolation).
 * Uses Claude Max OAuth (never Anthropic API key).
 */
export function buildReadOnlyCommand(agentId: string, objective: string): string {
  const safeObjective = objective.replace(/'/g, "'\\''");
  return (
    `pi -p --model ${DEFAULT_MODEL} ` +
    `--system-prompt '${SYSTEM_PROMPT}' ` +
    `--tools read,grep,find,ls ` +
    `--no-session --print --mode json ` +
    `<<< '${safeObjective}'`
  );
}

// ─── Bounded concurrency helper ───────────────────────────────────────────────

/**
 * Run async tasks with bounded concurrency, preserving input order in results.
 *
 * JavaScript is single-threaded: nextIdx++ is atomic between await points,
 * making the queue index race-safe without a mutex.
 */
async function runBounded<T>(
  tasks: Array<() => Promise<T>>,
  maxConcurrent: number
): Promise<T[]> {
  const results = new Array<T>(tasks.length);
  let nextIdx = 0;

  async function worker(): Promise<void> {
    while (true) {
      const idx = nextIdx++;
      if (idx >= tasks.length) break;
      results[idx] = await tasks[idx]();
    }
  }

  const workerCount = Math.min(maxConcurrent, tasks.length);
  if (workerCount === 0) return results;
  await Promise.all(Array.from({ length: workerCount }, worker));
  return results;
}

// ─── Default burst runner (real pi CLI) ───────────────────────────────────────

/**
 * Default burst runner: spawns pi CLI with read-only tools.
 * Uses Claude Max OAuth (never Anthropic API key, never ANTHROPIC_API_KEY).
 * Arguments passed as array — no shell interpolation — safe for arbitrary text.
 */
export const defaultBurstRunner: BurstRunnerFn = (
  agentId: string,
  role: string,
  objective: string,
  timeoutMs: number
): Promise<BurstAgentResult> => {
  return new Promise((resolve) => {
    const startMs = Date.now();

    // Array-form args: no shell interpretation, safe for any objective string.
    const args = [
      "-p",
      "--model", DEFAULT_MODEL,
      "--system-prompt", SYSTEM_PROMPT,
      "--tools", "read,grep,find,ls",
      "--no-session",
      "--print",
      "--mode", "json",
    ];

    const child = spawn("pi", args, {
      stdio: ["pipe", "pipe", "pipe"],
    });

    // Write objective to stdin — avoids command-line injection.
    child.stdin.write(objective, "utf8");
    child.stdin.end();

    const stdout: string[] = [];
    const stderr: string[] = [];

    child.stdout.on("data", (chunk: Buffer) => stdout.push(chunk.toString("utf8")));
    child.stderr.on("data", (chunk: Buffer) => stderr.push(chunk.toString("utf8")));

    // Hard timeout: SIGTERM the child after timeoutMs.
    const timer = setTimeout(() => {
      child.kill("SIGTERM");
      resolve({
        agent_id: agentId,
        role,
        objective,
        success: false,
        error: `Timeout after ${timeoutMs}ms`,
        duration_ms: Date.now() - startMs,
        timeout_ms: timeoutMs,
      });
    }, timeoutMs);

    child.on("close", (code: number | null) => {
      clearTimeout(timer);
      const duration_ms = Date.now() - startMs;
      const output = stdout.join("");
      const errText = stderr.join("").trim();

      if (code === 0) {
        resolve({ agent_id: agentId, role, objective, success: true, output, duration_ms, timeout_ms: timeoutMs });
      } else {
        resolve({
          agent_id: agentId,
          role,
          objective,
          success: false,
          output: output || undefined,
          error: errText || `Process exited with code ${String(code)}`,
          duration_ms,
          timeout_ms: timeoutMs,
        });
      }
    });

    child.on("error", (err: Error) => {
      clearTimeout(timer);
      resolve({
        agent_id: agentId,
        role,
        objective,
        success: false,
        error: err.message,
        duration_ms: Date.now() - startMs,
        timeout_ms: timeoutMs,
      });
    });
  });
};

// ─── Burst execution ──────────────────────────────────────────────────────────

/**
 * Run all agents in a read-only plan concurrently (bounded concurrency).
 * Only safe for read_only / planning / review_qa risk classes.
 *
 * Concurrency bounds:
 *   - Default: 4 (safe default for shared systems)
 *   - opts.maxConcurrent: caller override (any value 1–policyMax)
 *   - Hard ceiling: policy.global.max_concurrent_agents
 *
 * Uses injected runner (defaultBurstRunner in production, mock in tests).
 */
export async function runReadOnlyBurst(
  plan: AgentPlan,
  policy: AgentScalePolicy,
  opts: {
    runner?: BurstRunnerFn;
    timeoutMs?: number;
    maxConcurrent?: number;
  } = {}
): Promise<BurstRunResult> {
  const runner = opts.runner ?? defaultBurstRunner;
  const policyMax = policy.global.max_concurrent_agents;
  const rawMax = opts.maxConcurrent ?? DEFAULT_MAX_CONCURRENT;
  // Clamp: at least 1, at most the global policy cap
  const maxConcurrent = Math.min(Math.max(1, rawMax), policyMax);

  const agents = plan.proposed_dag.phases.flatMap((p) => p.agents);
  const startedAt = new Date().toISOString();

  // Each agent gets its own timeout: policy role timeout → fallback default.
  // opts.timeoutMs acts as a global override when explicitly provided.
  const tasks = agents.map(
    (a) => () => runner(a.id, a.role, a.objective, resolveAgentTimeoutMs(a.role, policy, opts.timeoutMs))
  );

  const results = await runBounded(tasks, maxConcurrent);
  const errors = results
    .filter((r) => !r.success)
    .map((r) => r.error ?? "unknown error");

  return {
    started_at: startedAt,
    completed_at: new Date().toISOString(),
    agents_run: results.length,
    agents_succeeded: results.filter((r) => r.success).length,
    agents_failed: results.filter((r) => !r.success).length,
    results,
    errors,
  };
}

// ─── Write dispatch message builder ──────────────────────────────────────────

/**
 * Build a user-facing swarm dispatch follow-up message for a write_bounded plan.
 * Pure function — no side effects, no subprocess calls.
 * Generated only when confirmed=true AND all gates pass.
 *
 * The message contains:
 *   - Summary and context
 *   - Copy-pasteable swarm objective
 *   - File ownership table (builder → files, exclusive)
 *   - Warning: queues, does NOT auto-execute
 */
export function buildWriteDispatchMessage(plan: AgentPlan): string {
  const lines: string[] = [
    `## 🚀 Swarm Dispatch Ready`,
    ``,
    `**Task**: ${plan.summary}`,
    `**Risk class**: \`${plan.risk_class}\``,
    `**Total agents**: ${plan.concurrency_summary.total_agents} (scout → builder(s) → reviewer)`,
    ``,
    `All safety gates are satisfied. To dispatch this work via the swarm tool, use:`,
    ``,
    `\`\`\``,
    `objective: "${plan.task_id}"`,
    `\`\`\``,
    ``,
    `## File Ownership Table (strict — no exceptions)`,
    ``,
    `| Builder | Owns (exclusive write access) |`,
    `|---------|-------------------------------|`,
  ];

  let hasOwnership = false;
  for (const [builderId, files] of Object.entries(plan.file_ownership_table)) {
    if (files.length > 0) {
      lines.push(`| ${builderId} | ${files.join(", ")} |`);
      hasOwnership = true;
    }
  }

  if (!hasOwnership) {
    lines.push(`| (no files assigned — provide files_affected for full ownership table) | — |`);
  }

  lines.push(
    ``,
    `> **⚠️ Important**: This queues the dispatch — it does NOT execute automatically.`,
    `> Verify the ownership table is correct and the working tree is clean before calling \`swarm\`.`
  );

  return lines.join("\n");
}

// ─── Main gate evaluator (synchronous) ───────────────────────────────────────

/**
 * Evaluate whether the plan can proceed to execution.
 * Synchronous — no subprocess spawning.
 *
 * Rules:
 * - Kill switch active → blocked always
 * - live_trading_ops → blocked (use atlas_risk_check_plan_gate)
 * - write_bounded → blocked if dirty tree or unsatisfied gates;
 *   returns gates_passed (allowed=false) if all gates met — needs human confirmation
 * - read_only / planning / review_qa → allowed; returns CLI command hints
 */
export function evaluateExecutionGate(
  plan: AgentPlan,
  policy: AgentScalePolicy,
  cwd: string
): ExecutionGateResult {
  const blockers: string[] = [];
  const warnings: string[] = [...plan.warnings];

  // Kill switch check
  if (isKillSwitchActive(policy)) {
    return {
      allowed: false,
      decision: "kill_switch_active",
      risk_class: plan.risk_class,
      blockers: ["Kill switch is active — no agent spawning permitted."],
      warnings,
      next_action: "Set global.kill_switch to false in agent-scale-policy.yaml to re-enable.",
    };
  }

  // Live trading ops: always blocked here; use atlas_risk_check_plan_gate instead
  if (plan.risk_class === "live_trading_ops") {
    return {
      allowed: false,
      decision: "gates_blocked",
      risk_class: "live_trading_ops",
      blockers: [
        "live_trading_ops tasks NEVER auto-execute. " +
        "Use atlas_risk_check_plan_gate + atlas_risk_approve_plan for explicit approval.",
      ],
      warnings,
      next_action:
        "Call atlas_risk_check_plan_gate to evaluate, then atlas_risk_approve_plan if appropriate.",
    };
  }

  // Write-bounded: enforce clean tree + gate check
  if (plan.risk_class === "write_bounded") {
    const gitStatus = checkGitStatus(cwd);
    if (!gitStatus.clean) {
      const detail = gitStatus.error
        ? `git error: ${gitStatus.error}`
        : `${gitStatus.dirty_files.length} dirty file(s): ${gitStatus.dirty_files.slice(0, 5).join(", ")}`;
      blockers.push(`Clean working tree gate FAILED — ${detail}`);
    }

    const gateResult: OverallGateResult = evaluatePlanGates(plan, policy);
    blockers.push(...gateResult.blockers.filter((b) => !blockers.includes(b)));
    warnings.push(...gateResult.warnings.filter((w) => !warnings.includes(w)));

    if (blockers.length > 0) {
      return {
        allowed: false,
        decision: "write_gate_rejected",
        risk_class: "write_bounded",
        blockers,
        warnings,
        next_action:
          "Resolve all blockers (clean working tree, build ownership table). " +
          "Then call with confirmed=true to queue a swarm dispatch message.",
      };
    }

    return {
      allowed: false, // write tasks need explicit human confirmation even when gates pass
      decision: "gates_passed",
      risk_class: "write_bounded",
      blockers: [],
      warnings,
      next_action:
        "All gates satisfied. Call atlas_elastic_run with confirmed=true to queue a swarm dispatch. " +
        "DO NOT auto-dispatch write agents without user consent.",
    };
  }

  // read_only, planning, and review_qa: allowed — return CLI command hints
  const agents = plan.proposed_dag.phases.flatMap((p) => p.agents);
  const readOnlyCommands: ReadOnlyCommand[] = agents.map((a) => ({
    agent_id: a.id,
    role: a.role,
    objective: a.objective,
    command: buildReadOnlyCommand(a.id, a.objective),
  }));

  return {
    allowed: true,
    decision: "read_only_started",
    risk_class: plan.risk_class,
    blockers: [],
    warnings,
    read_only_commands: readOnlyCommands,
    next_action:
      "Read-only agents can be dispatched immediately via pi CLI (Claude Max OAuth). " +
      "Pass execute_read_only=true to actually run burst agents. " +
      "Findings should be aggregated before any follow-up write tasks.",
  };
}

// ─── Async orchestrator ───────────────────────────────────────────────────────

/**
 * Full elastic run: gate evaluation + optional burst execution or write dispatch.
 *
 * Flows:
 *   execute_read_only=true + read_only/planning/review_qa + gates pass
 *     → run burst → read_only_complete or dispatch_rejected
 *   confirmed=true + write_bounded + gates pass
 *     → buildWriteDispatchMessage → dispatch_requested
 *   confirmed=true + write_bounded + gates fail (dirty tree, etc.)
 *     → write_gate_rejected (dirty tree always blocks, even with confirmation)
 *   default
 *     → return gate decision as-is
 *
 * Caller is responsible for writing audit entries before and after.
 */
export async function executeElasticRun(
  plan: AgentPlan,
  policy: AgentScalePolicy,
  cwd: string,
  opts: ElasticRunOptions = {}
): Promise<ElasticRunResult> {
  const gate = evaluateExecutionGate(plan, policy, cwd);

  // Kill switch: block immediately
  if (gate.decision === "kill_switch_active") {
    return {
      gate,
      final_decision: "kill_switch_active",
      final_blockers: gate.blockers,
    };
  }

  // Live trading ops: always blocked
  if (plan.risk_class === "live_trading_ops") {
    return {
      gate,
      final_decision: "gates_blocked",
      final_blockers: gate.blockers,
    };
  }

  // Read-only burst execution (read_only / planning / review_qa)
  const isReadOnlyClass =
    plan.risk_class === "read_only" ||
    plan.risk_class === "planning" ||
    plan.risk_class === "review_qa";

  if (isReadOnlyClass && gate.allowed && opts.execute_read_only) {
    try {
      const burst = await runReadOnlyBurst(plan, policy, {
        runner: opts.runner,
        timeoutMs: opts.timeoutMs,
        maxConcurrent: opts.maxConcurrent,
      });

      const allOk = burst.agents_failed === 0;
      const finalDecision: AuditDecision = allOk ? "read_only_complete" : "dispatch_rejected";

      return {
        gate,
        burst,
        final_decision: finalDecision,
        final_blockers: allOk
          ? []
          : burst.errors.map((e) => `Agent failed: ${e}`),
      };
    } catch (err) {
      const errMsg = err instanceof Error ? err.message : String(err);
      return {
        gate,
        final_decision: "dispatch_rejected",
        final_blockers: [`Burst execution failed: ${errMsg}`],
      };
    }
  }

  // Write dispatch: confirmed=true + write_bounded
  if (plan.risk_class === "write_bounded" && opts.confirmed) {
    // Dirty tree or gate failure still rejects even with confirmation
    if (gate.decision === "write_gate_rejected") {
      return {
        gate,
        final_decision: "write_gate_rejected",
        final_blockers: gate.blockers,
      };
    }

    // Gates passed — generate dispatch message (queue only, no swarm call)
    const dispatchMessage = buildWriteDispatchMessage(plan);
    return {
      gate,
      dispatch_message: dispatchMessage,
      final_decision: "dispatch_requested",
      final_blockers: [],
    };
  }

  // Default: return gate decision as-is
  return {
    gate,
    final_decision: gate.decision,
    final_blockers: gate.blockers,
  };
}

// ─── Ownership registry ───────────────────────────────────────────────────────

/**
 * Validate that the file ownership table has no overlaps.
 * Returns a list of conflict descriptions (empty if clean).
 */
export function validateOwnershipTable(
  fileOwnership: Record<string, string[]>
): string[] {
  const seen = new Map<string, string>(); // file → first builder
  const conflicts: string[] = [];

  for (const [builderId, files] of Object.entries(fileOwnership)) {
    for (const file of files) {
      const existing = seen.get(file);
      if (existing) {
        conflicts.push(
          `File "${file}" claimed by both "${existing}" and "${builderId}"`
        );
      } else {
        seen.set(file, builderId);
      }
    }
  }

  return conflicts;
}

// ─── Safety grep ─────────────────────────────────────────────────────────────

/**
 * Grep extension source files for forbidden Anthropic API key usage patterns.
 * Checks for three forbidden patterns (each split so this file does not self-match):
 *   Pattern 1: Python-style direct API-key constructor call
 *   Pattern 2: TypeScript Anthropic SDK package import
 *   Pattern 3: TypeScript Anthropic client config property assignment
 *
 * Only greps `dirPath` (src/ or similar) — test files in sibling dirs are excluded.
 *
 * Returns violations found (empty array if clean).
 * Safe to call in verify scripts.
 */
export function checkForbiddenApiKeyUsage(dirPath: string): string[] {
  // Patterns assembled via concatenation so this source file does not match its own grep.
  const patterns = [
    "Anthropic(" + "api_key",  // detects Python-style Anthropic client with key param
    "@anthropic" + "-ai/sdk",  // detects TypeScript SDK package import
    "api" + "Key:",            // detects TypeScript Anthropic client config property
  ];

  const violations: string[] = [];
  for (const pattern of patterns) {
    const result = spawnSync(
      "grep",
      ["-r", "--include=*.ts", "-n", pattern, dirPath],
      { encoding: "utf8" }
    );
    const output = (result.stdout ?? "").trim();
    if (output) violations.push(...output.split("\n").filter(Boolean));
  }
  return violations;
}
