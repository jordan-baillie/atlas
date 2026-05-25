/**
 * atlas-elastic-agents — Policy Loader & Validator
 *
 * Loads config/agent-scale-policy.yaml and exposes typed accessors.
 * YAML is parsed via Python (yaml module), since no js-yaml in node_modules.
 * All exports are pure functions — no Pi runtime dependency.
 */

import { existsSync, readFileSync } from "node:fs";
import { spawnSync } from "node:child_process";
import { resolve } from "node:path";

// ─── Types ────────────────────────────────────────────────────────────────────

export type RiskClass =
  | "read_only"
  | "planning"
  | "review_qa"
  | "write_bounded"
  | "live_trading_ops";

export interface RiskClassConfig {
  type: string;
  default_concurrency: number;
  max_builders?: number;
  roles: string[];
  gates: Record<string, boolean>[];
  examples: string[];
}

export interface AgentRoleConfig {
  risk_class: string;
  concurrency_cap: number;
  timeout_sec: number;
  can_spawn: string[];
  cannot_spawn: string[];
  isolation?: string;
  owned_files_required?: boolean;
  gates?: string[];
}

export interface GlobalConfig {
  max_concurrent_agents: number;
  max_write_agents: number;
  max_parallel_builders_per_file: number;
  kill_switch: boolean;
  budget_tokens_per_task: number;
}

export interface AgentScalePolicy {
  version: string;
  global: GlobalConfig;
  risk_classes: Record<string, RiskClassConfig>;
  agent_roles: Record<string, AgentRoleConfig>;
  protected_files: string[];
  approval_gates: Record<string, string>;
}

// ─── Policy loading ───────────────────────────────────────────────────────────

/**
 * Load and parse agent-scale-policy.yaml via Python yaml module.
 * Returns a typed AgentScalePolicy object.
 * Throws on missing file, parse error, or schema violation.
 */
export function loadPolicy(policyPath: string): AgentScalePolicy {
  const absPath = resolve(policyPath);
  if (!existsSync(absPath)) {
    throw new Error(`Policy file not found: ${absPath}`);
  }

  const yamlText = readFileSync(absPath, "utf8");
  const result = spawnSync(
    "python3",
    ["-c", "import yaml, json, sys; print(json.dumps(yaml.safe_load(sys.stdin.read())))"],
    { input: yamlText, encoding: "utf8" }
  );

  if (result.status !== 0 || result.error) {
    throw new Error(
      `Failed to parse policy YAML: ${result.stderr || result.error?.message || "unknown error"}`
    );
  }

  let parsed: unknown;
  try {
    parsed = JSON.parse(result.stdout.trim());
  } catch {
    throw new Error(`Policy YAML parsed but JSON conversion failed: ${result.stdout}`);
  }

  return validatePolicy(parsed, absPath);
}

/**
 * Validate a parsed policy object and cast it to AgentScalePolicy.
 * Throws with a descriptive message if required fields are missing.
 */
export function validatePolicy(raw: unknown, source?: string): AgentScalePolicy {
  const label = source ? `policy (${source})` : "policy";
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) {
    throw new Error(`${label}: must be an object`);
  }
  const obj = raw as Record<string, unknown>;

  if (typeof obj["version"] !== "string") {
    throw new Error(`${label}: missing or non-string 'version'`);
  }
  if (!obj["global"] || typeof obj["global"] !== "object") {
    throw new Error(`${label}: missing 'global' section`);
  }
  const g = obj["global"] as Record<string, unknown>;
  for (const key of [
    "max_concurrent_agents",
    "max_write_agents",
    "max_parallel_builders_per_file",
    "budget_tokens_per_task",
  ] as const) {
    if (typeof g[key] !== "number") {
      throw new Error(`${label}: global.${key} must be a number`);
    }
  }
  if (typeof g["kill_switch"] !== "boolean") {
    throw new Error(`${label}: global.kill_switch must be a boolean`);
  }
  if (!obj["risk_classes"] || typeof obj["risk_classes"] !== "object") {
    throw new Error(`${label}: missing 'risk_classes' section`);
  }
  if (!obj["agent_roles"] || typeof obj["agent_roles"] !== "object") {
    throw new Error(`${label}: missing 'agent_roles' section`);
  }
  if (!Array.isArray(obj["protected_files"])) {
    throw new Error(`${label}: 'protected_files' must be an array`);
  }

  return obj as unknown as AgentScalePolicy;
}

// ─── Policy accessors ─────────────────────────────────────────────────────────

/** Return true if the global kill switch is enabled. */
export function isKillSwitchActive(policy: AgentScalePolicy): boolean {
  return policy.global.kill_switch === true;
}

/**
 * Return true if the given file path is protected by policy.
 * Checks exact match, prefix match, and path-contains match.
 */
export function isProtectedFile(
  policy: AgentScalePolicy,
  filePath: string
): boolean {
  const norm = filePath.replace(/\\/g, "/");
  for (const pattern of policy.protected_files) {
    const p = pattern.replace(/\\/g, "/");
    // Exact match
    if (norm === p) return true;
    // Glob suffix: pattern ends with / or _ → prefix match
    if (p.endsWith("/") || p.endsWith("_")) {
      if (norm.startsWith(p)) return true;
    }
    // Pattern contains no special chars — substring match
    if (norm.includes(p)) return true;
  }
  return false;
}

/** Return the risk class config for a given class name, or undefined. */
export function getRiskClassConfig(
  policy: AgentScalePolicy,
  className: string
): RiskClassConfig | undefined {
  return policy.risk_classes[className];
}

/** Return max concurrency for a risk class (falls back to 1 if unknown). */
export function maxConcurrency(
  policy: AgentScalePolicy,
  riskClass: RiskClass
): number {
  const cfg = policy.risk_classes[riskClass];
  if (!cfg) return 1;
  const cap = policy.global.max_concurrent_agents;
  return Math.min(cfg.default_concurrency, cap);
}

/** Return the approval requirement for a risk class. */
export function approvalRequirement(
  policy: AgentScalePolicy,
  riskClass: RiskClass
): string {
  return policy.approval_gates[riskClass] ?? "explicit";
}

/** Default policy path relative to an atlas cwd. */
export function defaultPolicyPath(cwd?: string): string {
  return resolve(cwd ?? process.cwd(), "config/agent-scale-policy.yaml");
}
