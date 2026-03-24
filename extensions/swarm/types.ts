/**
 * Shared types for pi-swarm.
 */

export type AgentCapability = "scout" | "builder" | "reviewer" | "planner";

export interface WorktreeInfo {
  /** Absolute path to the worktree directory */
  path: string;
  /** Git branch name */
  branch: string;
  /** Agent name that owns this worktree */
  agentName: string;
}

export interface SwarmAgent {
  name: string;
  capability: AgentCapability;
  task: string;
  worktree: WorktreeInfo | null; // null for scouts/reviewers (read-only, run in-place)
  status: "pending" | "running" | "completed" | "failed" | "merged" | "merge_failed";
  exitCode: number | null;
  startedAt: string | null;
  completedAt: string | null;
  usage: UsageStats;
  model?: string;
  files?: string[]; // file scope for builders
  /** Failure reason (human-readable), populated when status = "failed" */
  failReason?: string;
  /** Which retry attempt this is (0 = first, 1 = first retry, etc.) */
  attempt?: number;
  /** Rich tracking fields — populated during run */
  toolCalls?: ToolCallStat[];
  filesModified?: { path: string; linesAdded: number; linesRemoved: number }[];
  activityLog?: ActivityEntry[];
  tokensPerSecond?: number;
  currentTurn?: number;
  progressPct?: number | null;
  /** Diagnostic info populated when status = "failed" */
  diagnostics?: AgentDiagnostics;
  /** Directory-based ownership scope for this builder */
  scope?: BuilderScope;
}

export interface UsageStats {
  input: number;
  output: number;
  cacheRead: number;
  cacheWrite: number;
  cost: number;
  contextTokens: number;
  turns: number;
}

export interface SwarmPlan {
  objective: string;
  complexity: "simple" | "moderate" | "complex";
  agents: PlannedAgent[];
  estimatedCost: number;
  reasoning: string;
}

export interface PlannedAgent {
  name: string;
  capability: AgentCapability;
  task: string;
  files?: string[];
  model?: string;
  dependsOn?: string[]; // agent names this depends on
  /** Directory-based ownership scope for this builder */
  scope?: BuilderScope;
}

export interface SwarmConfig {
  maxConcurrent: number;
  maxBudgetUsd: number;
  defaultBuilderModel: string;
  defaultScoutModel: string;
  defaultReviewerModel: string;
  staggerDelayMs: number;
  /** Per-capability timeouts in milliseconds */
  timeouts: {
    scout: number;
    builder: number;
    reviewer: number;
    planner: number;
  };
  /** Max retry attempts per failed agent (0 = no retries) */
  maxRetries: number;
  /** Shell command to run against the base branch after all merges succeed. null = skip */
  postMergeTestCommand: string | null;
}

export const DEFAULT_SWARM_CONFIG: SwarmConfig = {
  maxConcurrent: 6,
  maxBudgetUsd: 15.0,
  defaultBuilderModel: "anthropic/claude-sonnet-4-6",
  defaultScoutModel: "anthropic/claude-haiku-4-5",
  defaultReviewerModel: "anthropic/claude-sonnet-4-6",
  staggerDelayMs: 1000,
  timeouts: {
    scout: 300_000,   // 5 min
    builder: 900_000, // 15 min
    reviewer: 600_000, // 10 min
    planner: 300_000,  // 5 min
  },
  maxRetries: 1,
  postMergeTestCommand: null,
};

export interface MergeResult {
  branch: string;
  agentName: string;
  success: boolean;
  conflicts: string[];
  error?: string;
}

export function emptyUsage(): UsageStats {
  return { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, cost: 0, contextTokens: 0, turns: 0 };
}

// ── Rich tracking types ──────────────────────────────────────────────

/** Tool call tally for an agent */
export interface ToolCallStat {
  name: string;
  count: number;
  lastAt: string;
}

/** Per-agent activity log entry */
export interface ActivityEntry {
  timestamp: string;
  type: "tool_call" | "tool_result" | "turn_complete" | "error";
  summary: string;
}

/** Global cross-agent activity feed entry */
export interface GlobalActivityEntry {
  timestamp: string;
  agent: string;
  type: string;
  summary: string;
}

/** Phase record for timeline display */
export interface PhaseRecord {
  name: string;
  capability: string;
  startedAt: string;
  completedAt: string | null;
  agents: string[];
}

/** Token rate sample — cumulative totals at sample time (dashboard diffs consecutive entries) */
export interface TokenRateSample {
  timestamp: string;
  inputTotal: number;
  outputTotal: number;
  costTotal: number;
}

/** Aggregate file change summary across all agents */
export interface FileChangeStat {
  path: string;
  agent: string;
  linesAdded: number;
  linesRemoved: number;
}

// ── Scope & diagnostics types ────────────────────────────────────────

/** Directory-based ownership for builders (replaces file allowlist) */
export interface BuilderScope {
  /** Directories this builder owns (exclusive write access). Paths are relative to repo root. */
  ownedDirs: string[];
  /** Specific files outside owned dirs (escape hatch) */
  extraFiles?: string[];
  /** Glob patterns for new files this builder may create */
  newFilePatterns?: string[];
}

/** Diagnostic info for failed agents */
export interface AgentDiagnostics {
  exitCode: number;
  hasCommits: boolean;
  changedFiles: string[];
  scopeViolations: string[];
  stderrTail: string;
  lastToolCall: string;
  turnsCompleted: number;
}

// ── TUI widget state types ───────────────────────────────────────────

/** State for the inline TUI progress widget */
export interface AgentWidgetState {
  name: string;
  status: "pending" | "running" | "completed" | "failed" | "merged" | "merge_failed";
  capability: string;
  progressPct: number;
  currentAction: string;
  cost: number;
  elapsed: string;
  turns: number;
  dependsOn?: string[];
  tokensPerSecond?: number;
}

export interface WidgetState {
  agents: AgentWidgetState[];
  totalCost: number;
  totalElapsed: string;
  phase: string;
  objective: string;
}
