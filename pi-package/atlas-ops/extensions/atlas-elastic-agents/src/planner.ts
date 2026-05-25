/**
 * atlas-elastic-agents — Dry-Run Planner
 *
 * Classifies task risk, generates a DAG plan, evaluates safety gates.
 * All exports are pure functions — no Pi runtime dependency.
 * Git status check uses spawnSync for determinism.
 */

import { spawnSync } from "node:child_process";
import {
  type AgentScalePolicy,
  type RiskClass,
  isProtectedFile,
  isKillSwitchActive,
  maxConcurrency,
  approvalRequirement,
} from "./policy.js";

// ─── Plan types ───────────────────────────────────────────────────────────────

export interface AgentEntry {
  id: string;
  role: string;
  objective: string;
  concurrency_slot?: number;
  files_owned?: string[];
  depends_on?: string[];
}

export interface PlanPhase {
  phase: number;
  agents: AgentEntry[];
}

export interface GateStatus {
  gate: string;
  status: "required" | "optional";
  satisfied: boolean;
  detail?: string;
}

export interface AgentPlan {
  task_id: string;
  risk_class: RiskClass;
  summary: string;
  proposed_dag: { phases: PlanPhase[] };
  concurrency_summary: {
    max_parallel_agents: number;
    total_agents: number;
    estimated_time_minutes: number;
  };
  file_ownership_table: Record<string, string[]>;
  safety_gates: GateStatus[];
  warnings: string[];
  next_step: string;
  dry_run: boolean;
  generated_at: string;
}

export interface PlanParams {
  objective: string;
  files_affected?: string[];
  cwd?: string;
  dry_run?: boolean;
}

// ─── Git status check ─────────────────────────────────────────────────────────

export interface GitStatusResult {
  clean: boolean;
  dirty_files: string[];
  error?: string;
}

/**
 * Run `git status --short` in the given directory.
 * Returns clean=true only when the output is empty (no dirty files).
 */
export function checkGitStatus(cwd: string): GitStatusResult {
  const result = spawnSync("git", ["status", "--short"], {
    cwd,
    encoding: "utf8",
  });

  if (result.error) {
    return {
      clean: false,
      dirty_files: [],
      error: result.error.message,
    };
  }

  if (result.status !== 0) {
    return {
      clean: false,
      dirty_files: [],
      error: result.stderr?.trim() || `git status exited with code ${result.status}`,
    };
  }

  const output = (result.stdout ?? "").trim();
  if (!output) {
    return { clean: true, dirty_files: [] };
  }

  const dirtyFiles = output
    .split("\n")
    .map((line) => line.trim())
    .filter(Boolean)
    .map((line) => line.slice(3).trim()); // strip 2-char status + space

  return { clean: false, dirty_files: dirtyFiles };
}

// ─── Task classification ──────────────────────────────────────────────────────

/**
 * Simple substring keywords for live-trading detection.
 *
 * Note: bare "broker" is intentionally excluded — it over-triggers on
 * read-only objectives like "security scan on broker integration".
 * Use the more specific broker-mutation terms below instead.
 */
const LIVE_TRADING_KEYWORDS = [
  "deploy config",
  "promote config",
  "active config",     // space version — catches "promote active config", "edit active config"
  "live position",
  "live trade",
  "execute trade",
  "approve plan",
  "position change",
  "rebalance portfolio",
  "live_",
  "active_config",
  // Specific broker mutation terms (not bare "broker" which over-triggers):
  "broker state",
  "broker position",
  "broker order",
  "broker account",
  "broker trade",
  "broker mutation",
  "place order",
  "submit order",
  "cancel order",
];

/**
 * Regex patterns for live-trading phrases that require flexible token matching.
 *
 * - "promote/deploy [words] config" — catches "promote sp500 config to live",
 *   "deploy active config", "promote the live config", etc.
 * - "promote ... live" — catches "promote the live config", "promote sp500 to live".
 */
const LIVE_TRADING_REGEXES: RegExp[] = [
  /\b(promote|deploy)\b.{0,60}\bconfig/i,
  /\bpromote\b.{0,40}\blive\b/i,
];

const WRITE_KEYWORDS = [
  "refactor",
  "implement",
  "add ",   // matches sentence-initial "Add X" as well as mid-sentence " add X"
  "fix ",
  "update ",
  "create ",
  "build ",
  "migrate",
  "rename ",
  "replace ",
  "delete ",
  "remove ",
  "edit ",
  "write ",
  "generate ",
  "scaffold",
];

const REVIEW_QA_KEYWORDS = [
  "review ",
  "code review",
  "verify ",
  "verify that",
  "test analysis",
  "security scan",
  " qa ",
  "run qa",
  "lint ",
  "linting",
];

const PLANNING_KEYWORDS = [
  "plan ",
  "spec ",
  "design ",
  "architecture",
  " dag",
  "assess ",
  "roadmap",
  "proposal",
  "strategy ",
];

/**
 * Classify the risk class of a task based on its objective and affected files.
 * Returns the most restrictive class when multiple signals are present.
 */
export function classifyTask(
  objective: string,
  filesAffected?: string[]
): RiskClass {
  const lower = objective.toLowerCase();

  // Check live_trading_ops first (highest risk — keywords + regex patterns)
  if (
    LIVE_TRADING_KEYWORDS.some((kw) => lower.includes(kw)) ||
    LIVE_TRADING_REGEXES.some((re) => re.test(lower))
  ) {
    return "live_trading_ops";
  }

  // Check files for live/config patterns
  if (filesAffected && filesAffected.length > 0) {
    for (const f of filesAffected) {
      const fl = f.toLowerCase();
      if (
        fl.includes("live_") ||
        fl.includes("active_config") ||
        fl.includes("broker") ||
        fl.startsWith("secrets/") ||
        fl.startsWith(".git/")
      ) {
        return "live_trading_ops";
      }
    }
  }

  // review_qa: QA/review/security activities (read-only execution) —
  // only when review keywords present and NO write keywords (prevents misclassification
  // of objectives like "implement and verify" which are write_bounded).
  if (
    REVIEW_QA_KEYWORDS.some((kw) => lower.includes(kw)) &&
    !WRITE_KEYWORDS.some((kw) => lower.includes(kw))
  ) {
    return "review_qa";
  }

  // Write-bounded: has explicit write keywords OR has affected files
  if (
    WRITE_KEYWORDS.some((kw) => lower.includes(kw)) ||
    (filesAffected && filesAffected.length > 0)
  ) {
    return "write_bounded";
  }

  // Planning: spec/design/DAG work
  if (PLANNING_KEYWORDS.some((kw) => lower.includes(kw))) {
    return "planning";
  }

  // Default: read-only recon
  return "read_only";
}

/**
 * Return true if the objective or affected files involve any protected files.
 */
export function hasProtectedFiles(
  policy: AgentScalePolicy,
  filesAffected: string[]
): string[] {
  return filesAffected.filter((f) => isProtectedFile(policy, f));
}

// ─── DAG generation ───────────────────────────────────────────────────────────

function makeTaskId(objective: string): string {
  return objective
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 48);
}

/**
 * Generate a DAG plan for the given objective.
 * Does NOT spawn agents — output is a structured recommendation.
 */
export function generatePlan(
  params: PlanParams,
  policy: AgentScalePolicy
): AgentPlan {
  const { objective, files_affected = [], cwd = process.cwd(), dry_run = true } = params;
  const riskClass = classifyTask(objective, files_affected);
  const taskId = makeTaskId(objective);
  const generatedAt = new Date().toISOString();

  const protected_ = hasProtectedFiles(policy, files_affected);
  const warnings: string[] = [];
  if (protected_.length > 0) {
    warnings.push(
      `Protected files detected — manual approval required: ${protected_.join(", ")}`
    );
  }

  if (isKillSwitchActive(policy)) {
    warnings.push("Kill switch is active — no agent spawning permitted.");
  }

  // Build DAG and gates based on risk class
  let phases: PlanPhase[];
  let fileOwnership: Record<string, string[]> = {};
  let gates: GateStatus[];
  let maxParallel: number;
  let totalAgents: number;
  let estimatedMin: number;
  let nextStep: string;
  let summary: string;

  switch (riskClass) {
    case "read_only": {
      const conc = Math.min(maxConcurrency(policy, "read_only"), 8);
      phases = [
        {
          phase: 1,
          agents: Array.from({ length: Math.min(conc, 3) }, (_, i) => ({
            id: `scout-${i + 1}`,
            role: "scout",
            objective: `${objective} — reconnaissance slice ${i + 1}`,
            concurrency_slot: i + 1,
            files_owned: [],
            depends_on: [],
          })),
        },
      ];
      gates = [];
      maxParallel = phases[0].agents.length;
      totalAgents = phases[0].agents.length;
      estimatedMin = 3;
      summary = `Read-only recon: ${phases[0].agents.length} scouts in parallel.`;
      nextStep =
        "Review plan. For read-only tasks, coordinator can dispatch scouts immediately (no write gate needed).";
      break;
    }

    case "review_qa": {
      // QA / code review / security scan — read-only execution, no gates.
      const conc = Math.min(maxConcurrency(policy, "review_qa"), 3);
      const qaRoles: Array<{ id: string; role: string; label: string }> = [
        { id: "reviewer-1",          role: "reviewer",          label: "code review" },
        { id: "test-runner-1",       role: "test-runner",       label: "test analysis" },
        { id: "security-reviewer-1", role: "security-reviewer", label: "security scan" },
      ].slice(0, Math.max(1, conc));

      phases = [
        {
          phase: 1,
          agents: qaRoles.map((r, i) => ({
            id: r.id,
            role: r.role,
            objective: `${objective} — ${r.label}`,
            concurrency_slot: i + 1,
            files_owned: [], // read-only: no file ownership
            depends_on: [],
          })),
        },
      ];
      gates = [];
      maxParallel = qaRoles.length;
      totalAgents = qaRoles.length;
      estimatedMin = 5;
      summary = `Review/QA: ${qaRoles.length} parallel agents (${qaRoles.map((r) => r.role).join(", ")}). Read-only execution.`;
      nextStep =
        "Review QA plan. Pass execute_read_only=true to /elastic-run or atlas_elastic_run " +
        "to dispatch these agents immediately (no write gate needed).";
      break;
    }

    case "planning": {
      phases = [
        {
          phase: 1,
          agents: [
            { id: "planner-1", role: "planner", objective, concurrency_slot: 1, files_owned: [], depends_on: [] },
            { id: "spec-1", role: "spec_writer", objective: `${objective} — spec draft`, concurrency_slot: 2, files_owned: [], depends_on: [] },
          ],
        },
      ];
      gates = [];
      maxParallel = 2;
      totalAgents = 2;
      estimatedMin = 5;
      summary = `Planning task: 1 planner + 1 spec writer in parallel.`;
      nextStep =
        "Review plan. Planning tasks have no write gates — coordinator can dispatch immediately.";
      break;
    }

    case "write_bounded": {
      const gitStatus = checkGitStatus(cwd);
      const cleanGateSatisfied = gitStatus.clean;
      const ownershipGateSatisfied = files_affected.length > 0;

      if (!cleanGateSatisfied && !gitStatus.error) {
        warnings.push(
          `Dirty working tree detected (${gitStatus.dirty_files.length} files). ` +
          `Clean tree is required before write agents can be dispatched. ` +
          `Run 'git stash' or commit changes first.`
        );
      }

      // Split files across builders
      const halfIdx = Math.ceil(files_affected.length / 2);
      const builder1Files = files_affected.slice(0, halfIdx);
      const builder2Files = files_affected.slice(halfIdx);

      const builders: AgentEntry[] = [];
      if (builder1Files.length > 0 || files_affected.length === 0) {
        builders.push({
          id: "builder-1",
          role: "builder",
          objective: `${objective} — part 1`,
          files_owned: builder1Files.length > 0 ? builder1Files : [],
          depends_on: ["scout-1"],
        });
      }
      if (builder2Files.length > 0) {
        builders.push({
          id: "builder-2",
          role: "builder",
          objective: `${objective} — part 2`,
          files_owned: builder2Files,
          depends_on: ["scout-1"],
        });
      }
      if (builders.length === 0) {
        builders.push({
          id: "builder-1",
          role: "builder",
          objective,
          files_owned: [],
          depends_on: ["scout-1"],
        });
      }

      // Record file ownership
      for (const b of builders) {
        fileOwnership[b.id] = b.files_owned ?? [];
      }
      fileOwnership["scout-1"] = [];
      fileOwnership["reviewer-1"] = [];

      phases = [
        {
          phase: 1,
          agents: [
            { id: "scout-1", role: "scout", objective: `${objective} — reconnaissance`, files_owned: [], depends_on: [] },
          ],
        },
        { phase: 2, agents: builders },
        {
          phase: 3,
          agents: [
            {
              id: "reviewer-1",
              role: "reviewer",
              objective: `Code review + verification for: ${objective}`,
              files_owned: [],
              depends_on: builders.map((b) => b.id),
            },
          ],
        },
      ];

      gates = [
        {
          gate: "clean_working_tree",
          status: "required",
          satisfied: cleanGateSatisfied,
          detail: cleanGateSatisfied
            ? "git status --short returned empty"
            : gitStatus.error
            ? `git error: ${gitStatus.error}`
            : `${gitStatus.dirty_files.length} dirty file(s) detected`,
        },
        {
          gate: "file_ownership_table_required",
          status: "required",
          satisfied: ownershipGateSatisfied,
          detail: ownershipGateSatisfied
            ? `${files_affected.length} file(s) assigned`
            : "No files specified — file ownership table cannot be built without a scout phase",
        },
        {
          gate: "no_overlapping_ownership",
          status: "required",
          satisfied: true,
          detail: "Ownership is mutually exclusive by construction",
        },
      ];

      if (protected_.length > 0) {
        gates.push({
          gate: "protected_files_require_manual_approval",
          status: "required",
          satisfied: false,
          detail: `Protected: ${protected_.join(", ")}`,
        });
      }

      maxParallel = Math.min(builders.length, maxConcurrency(policy, "write_bounded"));
      totalAgents = 1 + builders.length + 1;
      estimatedMin = 8 + builders.length * 5;
      const allGatesSatisfied = gates.every((g) => g.status === "optional" || g.satisfied);
      summary = `Write-bounded: 1 scout → ${builders.length} builder(s) → 1 reviewer. Gates: ${allGatesSatisfied ? "✓ all satisfied" : "✗ some gates unmet"}.`;
      nextStep = allGatesSatisfied
        ? "All gates satisfied. Review plan and confirm to dispatch via swarm or subagent coordinator."
        : "Gates NOT satisfied. Resolve warnings before dispatching write agents. Run plan again after clean tree.";
      break;
    }

    case "live_trading_ops": {
      phases = [
        {
          phase: 1,
          agents: [
            { id: "executor-1", role: "executor", objective, files_owned: [], depends_on: [] },
          ],
        },
      ];

      gates = [
        {
          gate: "explicit_human_approval",
          status: "required",
          satisfied: false,
          detail: "Live trading ops ALWAYS require explicit human sign-off before any execution.",
        },
        {
          gate: "trading_halt_check",
          status: "required",
          satisfied: false,
          detail: "Run health check to confirm no trading halt is active.",
        },
        {
          gate: "config_validation",
          status: "required",
          satisfied: false,
          detail: "Active config must be validated before any broker mutation.",
        },
      ];

      if (protected_.length > 0) {
        warnings.push(
          `This task touches protected live-trading files: ${protected_.join(", ")}. ` +
          `Coordinator will REJECT automatic dispatch.`
        );
      }

      maxParallel = 1;
      totalAgents = 1;
      estimatedMin = 2;
      summary = `LIVE TRADING OPS: requires explicit human approval before any execution.`;
      nextStep =
        "This task is classified as live_trading_ops. NO automatic dispatch. " +
        "Provide explicit approval with atlas_risk_check_plan_gate before any action.";
      break;
    }
  }

  const approvalReq = approvalRequirement(policy, riskClass);

  return {
    task_id: taskId,
    risk_class: riskClass,
    summary,
    proposed_dag: { phases },
    concurrency_summary: {
      max_parallel_agents: maxParallel,
      total_agents: totalAgents,
      estimated_time_minutes: estimatedMin,
    },
    file_ownership_table: fileOwnership,
    safety_gates: gates,
    warnings,
    next_step: nextStep,
    dry_run,
    generated_at: generatedAt,
  };
}

// ─── Gate evaluation ──────────────────────────────────────────────────────────

export interface OverallGateResult {
  approved: boolean;
  blockers: string[];
  warnings: string[];
}

/**
 * Evaluate whether a generated plan passes all required gates.
 * Returns approved=false with blockers if any required gate is unsatisfied.
 */
export function evaluatePlanGates(plan: AgentPlan, policy: AgentScalePolicy): OverallGateResult {
  const blockers: string[] = [];
  const warnings: string[] = [...plan.warnings];

  if (isKillSwitchActive(policy)) {
    blockers.push("Kill switch is active — no agent spawning permitted.");
  }

  for (const gate of plan.safety_gates) {
    if (gate.status === "required" && !gate.satisfied) {
      blockers.push(`Gate '${gate.gate}' not satisfied: ${gate.detail ?? "(no detail)"}`);
    }
  }

  return {
    approved: blockers.length === 0,
    blockers,
    warnings,
  };
}
