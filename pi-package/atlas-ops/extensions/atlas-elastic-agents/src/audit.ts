/**
 * atlas-elastic-agents — Audit Logger
 *
 * Writes JSONL audit entries to .pi/elastic-agents/audit.jsonl.
 * Every plan attempt and execution decision is logged.
 * All exports are pure functions — no Pi runtime dependency.
 */

import { appendFileSync, existsSync, mkdirSync, readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import type { RiskClass } from "./policy.js";
import type { AgentPlan, GateStatus } from "./planner.js";

// ─── Types ────────────────────────────────────────────────────────────────────

export type AuditDecision =
  | "plan_generated"     // Planner ran and produced a plan (dry-run)
  | "gates_passed"       // All gates satisfied; ready for dispatch
  | "gates_blocked"      // Gates failed; dispatch rejected
  | "kill_switch_active" // Kill switch prevented any action
  | "dispatch_requested" // User confirmed and coordinator was asked to dispatch
  | "dispatch_rejected"  // Coordinator rejected the dispatch request
  | "read_only_started"  // Read-only burst mode started
  | "read_only_complete" // Read-only burst mode finished
  | "write_gate_rejected"; // Write operation rejected (dirty tree, no ownership, etc.)

export interface AuditEntry {
  timestamp: string;
  task_id: string;
  objective: string;
  risk_class: RiskClass;
  decision: AuditDecision;
  gates: GateStatus[];
  warnings: string[];
  dry_run: boolean;
  cwd?: string;
  agent_count?: number;
  blockers?: string[];
  extra?: Record<string, unknown>;
}

// ─── Audit path ───────────────────────────────────────────────────────────────

/** Default audit log path relative to the atlas workspace root. */
export function defaultAuditPath(cwd?: string): string {
  return resolve(cwd ?? process.cwd(), ".pi", "elastic-agents", "audit.jsonl");
}

// ─── Writer ───────────────────────────────────────────────────────────────────

/**
 * Append one JSONL audit entry to the audit log file.
 * Creates the parent directory if it does not exist.
 */
export function writeAuditEntry(entry: AuditEntry, auditPath: string): void {
  const dir = dirname(auditPath);
  if (!existsSync(dir)) {
    mkdirSync(dir, { recursive: true });
  }
  const line = JSON.stringify(entry);
  appendFileSync(auditPath, line + "\n", "utf8");
}

// ─── Reader ───────────────────────────────────────────────────────────────────

/**
 * Read the last `limit` audit entries (default 50).
 * Returns an empty array if the file does not exist.
 */
export function readAuditEntries(auditPath: string, limit = 50): AuditEntry[] {
  if (!existsSync(auditPath)) return [];
  const raw = readFileSync(auditPath, "utf8");
  const lines = raw
    .split("\n")
    .map((l) => l.trim())
    .filter(Boolean);
  const entries: AuditEntry[] = [];
  for (const line of lines) {
    try {
      entries.push(JSON.parse(line) as AuditEntry);
    } catch {
      // skip malformed lines
    }
  }
  return entries.slice(-limit);
}

// ─── Convenience factory ──────────────────────────────────────────────────────

/**
 * Build an AuditEntry from a plan + decision.
 * Extra fields can be added via the `extra` parameter.
 * `cwd` is recorded when provided to aid post-hoc diagnosis of multi-project setups.
 */
export function makeAuditEntry(
  plan: AgentPlan,
  decision: AuditDecision,
  opts?: { blockers?: string[]; cwd?: string; extra?: Record<string, unknown> }
): AuditEntry {
  const totalAgents = plan.concurrency_summary.total_agents;
  return {
    timestamp: new Date().toISOString(),
    task_id: plan.task_id,
    objective: plan.summary,
    risk_class: plan.risk_class,
    decision,
    gates: plan.safety_gates,
    warnings: plan.warnings,
    dry_run: plan.dry_run,
    cwd: opts?.cwd,
    agent_count: totalAgents,
    blockers: opts?.blockers,
    extra: opts?.extra,
  };
}
