/**
 * Complexity analysis and agent budget planning.
 *
 * Determines how many agents to spawn based on:
 * - Number of files to modify
 * - Task description complexity and explicit structure
 * - Cross-directory spread
 * - Cost budget and concurrency limits
 *
 * Key principle: when the user provides explicit builder assignments
 * (e.g. "BUILDER 1: ...", "BUILDER 2: ..."), honour them. The user
 * knows the task better than a heuristic scorer.
 */

import type { AgentCapability, BuilderScope, PlannedAgent, SwarmConfig, SwarmPlan } from "./types.js";

// ── Cost estimation ──────────────────────────────────────────────────

function estimateSessionCost(model: string): number {
  if (model.includes("haiku")) return 0.15;
  if (model.includes("sonnet")) return 0.80;
  if (model.includes("opus")) return 3.00;
  return 1.00;
}

// ── Explicit builder detection ───────────────────────────────────────

interface ExplicitBuilder {
  name: string;
  task: string;
  files: string[];
}

/**
 * Parse explicit builder assignments from the objective text.
 *
 * Detects patterns like:
 *   ## BUILDER 1: Market profile (markets/hk.py, markets/registry.py)
 *   ## Builder 2: Config + CLI
 *   BUILDER 3 — Tests
 *
 * Returns the parsed builders, or empty array if none found.
 */
function parseExplicitBuilders(objective: string): ExplicitBuilder[] {
  // Match "BUILDER N:" or "## BUILDER N:" or "BUILDER N —" patterns
  const pattern = /(?:^|\n)#{0,3}\s*(?:BUILDER|Builder)\s+(\d+)\s*[:\—\-–]\s*([^\n]+)/g;
  const builders: ExplicitBuilder[] = [];
  const sections: { index: number; num: number; title: string }[] = [];

  let match;
  while ((match = pattern.exec(objective)) !== null) {
    sections.push({
      index: match.index,
      num: parseInt(match[1], 10),
      title: match[2].trim(),
    });
  }

  if (sections.length < 2) return []; // Need at least 2 explicit builders to activate

  for (let i = 0; i < sections.length; i++) {
    const start = sections[i].index;
    const end = i + 1 < sections.length ? sections[i + 1].index : objective.length;
    const body = objective.slice(start, end);

    // Extract file paths from the section (look for path-like strings)
    const filePattern = /(?:^|[\s,`(])([a-zA-Z0-9_\-./]+\.[a-zA-Z]{1,10})(?=[\s,`)\n]|$)/g;
    const files: string[] = [];
    let fm;
    while ((fm = filePattern.exec(body)) !== null) {
      const f = fm[1];
      // Filter out non-file patterns (URLs, version strings, etc.)
      if (f.includes("/") && !f.startsWith("http") && !f.startsWith("//")) {
        files.push(f);
      }
    }

    builders.push({
      name: `builder-${sections[i].num}`,
      task: body.trim(),
      files: [...new Set(files)], // deduplicate
    });
  }

  return builders;
}


// ── File overlap detection ────────────────────────────────────────────

/**
 * Check for duplicate file assignments across builders and return a warning
 * string if any are found, or null if all assignments are clean.
 */
function detectFileOverlap(builders: ExplicitBuilder[]): string | null {
  const seen = new Map<string, string>(); // file → first builder name
  const duplicates: string[] = [];

  for (const b of builders) {
    for (const f of b.files) {
      if (seen.has(f)) {
        duplicates.push(`"${f}" (${seen.get(f)} & ${b.name})`);
      } else {
        seen.set(f, b.name);
      }
    }
  }

  if (duplicates.length === 0) return null;
  return `⚠️  File overlap detected — same file assigned to multiple builders: ${duplicates.join(", ")}. This will cause merge conflicts.`;
}

// ── Complexity scoring ───────────────────────────────────────────────

function scoreFileComplexity(files: string[]): number {
  if (files.length === 0) return 1;

  let score = files.length;

  // Directory spread — each unique directory adds weight
  const dirs = new Set(files.map((f) => f.split("/").slice(0, -1).join("/")));
  score += dirs.size * 1.0; // Was 0.5, now 1.0 — directory spread matters

  // Test files add complexity (need separate builder often)
  const testFiles = files.filter((f) => f.includes("test") || f.includes("spec"));
  score += testFiles.length * 0.5;

  // Config/data files add complexity (different from code files)
  const configFiles = files.filter((f) => f.endsWith(".json") || f.endsWith(".yaml") || f.endsWith(".toml"));
  score += configFiles.length * 0.3;

  return score;
}

function scoreTaskComplexity(description: string): number {
  let score = 1;

  // Length: longer descriptions = more complex tasks
  const len = description.length;
  if (len > 3000) score += 4;      // Very detailed spec
  else if (len > 1500) score += 3;  // Detailed spec
  else if (len > 500) score += 2;
  else if (len > 200) score += 1;

  // Explicit structure (numbered sections, bullet points, headers)
  const headers = (description.match(/^#{1,4}\s/gm) || []).length;
  const bullets = (description.match(/^\s*[-*]\s/gm) || []).length;
  const numberedItems = (description.match(/^\s*\d+\.\s/gm) || []).length;
  score += Math.min(4, (headers + bullets + numberedItems) * 0.15);

  // Code blocks suggest detailed implementation specs
  const codeBlocks = (description.match(/```/g) || []).length / 2;
  score += Math.min(3, codeBlocks * 0.5);

  // Keywords suggesting cross-cutting concerns
  const complexKeywords = [
    "refactor", "migrate", "redesign", "rewrite", "architecture",
    "across", "multiple", "all files", "throughout", "everywhere",
    "integration", "end-to-end", "full-stack", "end to end",
    "new market", "new module", "new service", "new feature",
    "builder 1", "builder 2", "builder 3", // explicit multi-builder intent
  ];
  for (const kw of complexKeywords) {
    if (description.toLowerCase().includes(kw)) score += 1;
  }

  // Keywords suggesting simple tasks
  const simpleKeywords = [
    "typo", "rename", "comment", "docs", "readme",
    "bump version", "update dependency", "fix import",
  ];
  for (const kw of simpleKeywords) {
    if (description.toLowerCase().includes(kw)) score -= 0.5;
  }

  return Math.max(1, score);
}

// ── Scope computation ────────────────────────────────────────────────

/**
 * Compute a BuilderScope from a list of files.
 *
 * Strategy:
 * - If 2+ files share the same immediate parent directory, own that directory.
 * - Files in unique directories become extraFiles (escape hatch).
 *
 * Example:
 *   Input:  ["src/a/foo.ts", "src/a/bar.ts", "tests/a.test.ts"]
 *   Output: { ownedDirs: ["src/a"], extraFiles: ["tests/a.test.ts"] }
 */
function computeScope(files: string[]): BuilderScope {
  // Group files by their parent directory
  const dirCount = new Map<string, number>();
  for (const f of files) {
    const dir = f.includes("/") ? f.split("/").slice(0, -1).join("/") : ".";
    dirCount.set(dir, (dirCount.get(dir) ?? 0) + 1);
  }

  const ownedDirs = new Set<string>();
  const standaloneFiles: string[] = [];

  for (const f of files) {
    const dir = f.includes("/") ? f.split("/").slice(0, -1).join("/") : ".";
    if ((dirCount.get(dir) ?? 0) >= 2) {
      ownedDirs.add(dir);
    } else {
      standaloneFiles.push(f);
    }
  }

  return {
    ownedDirs: [...ownedDirs],
    extraFiles: standaloneFiles.length > 0 ? standaloneFiles : undefined,
  };
}

// ── Directory-aware file partitioning ────────────────────────────────

/**
 * Partition files into builder batches, keeping files from the same
 * top-level directory together (avoids splitting a directory across builders).
 *
 * Uses greedy bin-packing: assign the largest directory groups first,
 * always filling the least-loaded builder.
 */
function partitionByDirectory(files: string[], maxBuilders: number): string[][] {
  if (files.length === 0) return [];

  // Group files by their top-level directory component
  const dirGroups = new Map<string, string[]>();
  for (const f of files) {
    const topDir = f.includes("/") ? f.split("/")[0] : ".";
    if (!dirGroups.has(topDir)) dirGroups.set(topDir, []);
    dirGroups.get(topDir)!.push(f);
  }

  const numBuilders = Math.min(maxBuilders, dirGroups.size);
  const builders: string[][] = Array.from({ length: Math.max(1, numBuilders) }, () => []);

  // Sort directory groups largest-first for better bin packing
  const sorted = [...dirGroups.entries()].sort((a, b) => b[1].length - a[1].length);

  for (const [, groupFiles] of sorted) {
    // Add to the builder with the fewest files (greedy bin-packing)
    let minIdx = 0;
    for (let i = 1; i < builders.length; i++) {
      if (builders[i].length < builders[minIdx].length) minIdx = i;
    }
    builders[minIdx].push(...groupFiles);
  }

  return builders.filter((b) => b.length > 0);
}

// ── Main planner ─────────────────────────────────────────────────────

/**
 * Analyze a task and propose an agent allocation plan.
 *
 * Strategy:
 * 1. Check for explicit builder assignments first — if the user spelled
 *    out "BUILDER 1:", "BUILDER 2:", etc., honour that structure exactly.
 * 2. Otherwise, score complexity and auto-partition files.
 *
 * Auto-partition rules:
 * - Simple (score ≤ 4): 1 builder, no scout, no reviewer
 * - Moderate (score ≤ 8): 1 scout + 2 builders (split files evenly)
 * - Complex (score > 8): 1-2 scouts + N builders (3 files/builder) + reviewer
 */
export function planSwarm(
  objective: string,
  files: string[],
  config: SwarmConfig,
): SwarmPlan {
  const fileScore = scoreFileComplexity(files);
  const taskScore = scoreTaskComplexity(objective);
  const totalScore = fileScore + taskScore;

  // ── Check for explicit builder assignments ──
  const explicitBuilders = parseExplicitBuilders(objective);

  if (explicitBuilders.length >= 2) {
    return planFromExplicit(objective, explicitBuilders, files, totalScore, config);
  }

  // ── Auto-plan based on complexity score ──
  let complexity: "simple" | "moderate" | "complex";
  if (totalScore <= 4) complexity = "simple";
  else if (totalScore <= 8) complexity = "moderate";
  else complexity = "complex";

  const agents: PlannedAgent[] = [];
  let reasoning: string;

  if (complexity === "simple") {
    agents.push({
      name: "builder-1",
      capability: "builder",
      task: objective,
      files: files.length > 0 ? files : undefined,
      scope: files.length > 0 ? computeScope(files) : undefined,
      model: config.defaultBuilderModel,
    });
    reasoning = `Simple task (score: ${totalScore.toFixed(1)}). Single builder.`;

  } else if (complexity === "moderate") {
    agents.push({
      name: "scout-1",
      capability: "scout",
      task: `Explore codebase for: ${objective.slice(0, 500)}. Report file layout, types, dependencies, and patterns.`,
      model: config.defaultScoutModel,
    });

    // Moderate: always try to split into 2 builders if 4+ files
    if (files.length >= 4) {
      const batches = partitionByDirectory(files, 2);

      for (let i = 0; i < batches.length; i++) {
        const batch = batches[i];
        agents.push({
          name: `builder-${i + 1}`,
          capability: "builder",
          task: `${objective} — focus on: ${batch.join(", ")}`,
          files: batch,
          scope: computeScope(batch),
          model: config.defaultBuilderModel,
          dependsOn: ["scout-1"],
        });
      }

      // If partitionByDirectory yielded only 1 batch, add a second pass with remaining
      // (shouldn't happen with valid input, but guard against it)
      if (batches.length === 1) {
        // Already added as builder-1; add a no-op second builder for parity
        // Actually no — just use a single builder in that case (already pushed above)
      }
    } else {
      agents.push({
        name: "builder-1",
        capability: "builder",
        task: objective,
        files: files.length > 0 ? files : undefined,
        scope: files.length > 0 ? computeScope(files) : undefined,
        model: config.defaultBuilderModel,
        dependsOn: ["scout-1"],
      });
    }

    const builderCount = agents.filter((a) => a.capability === "builder").length;
    reasoning = `Moderate task (score: ${totalScore.toFixed(1)}, ${files.length} files). 1 scout, ${builderCount} builder(s).`;

  } else {
    // Complex: scouts + multiple builders + reviewer
    agents.push({
      name: "scout-1",
      capability: "scout",
      task: `Explore codebase for: ${objective.slice(0, 500)}. Report file layout, types, dependencies, and patterns.`,
      model: config.defaultScoutModel,
    });

    if (files.length > 10) {
      const mid = Math.ceil(files.length / 2);
      agents.push({
        name: "scout-2",
        capability: "scout",
        task: `Explore tests and types related to: ${objective.slice(0, 200)}. Files: ${files.slice(mid).join(", ")}`,
        model: config.defaultScoutModel,
      });
    }

    // Partition files by directory: max 3 files per builder
    const filesPerBuilder = 3;
    const maxBuilders = config.maxConcurrent - agents.length - 1; // leave room for reviewer
    const desiredBuilderCount = files.length > 0
      ? Math.max(2, Math.ceil(files.length / filesPerBuilder))
      : 2; // Even without explicit files, complex tasks get 2 builders
    const cappedBuilderCount = Math.min(desiredBuilderCount, maxBuilders);

    const scoutDeps = agents.filter((a) => a.capability === "scout").map((a) => a.name);

    if (files.length > 0) {
      const batches = partitionByDirectory(files, cappedBuilderCount);

      for (let i = 0; i < batches.length; i++) {
        const batch = batches[i];
        agents.push({
          name: `builder-${i + 1}`,
          capability: "builder",
          task: `${objective} — focus on: ${batch.join(", ")}`,
          files: batch,
          scope: computeScope(batch),
          model: config.defaultBuilderModel,
          dependsOn: scoutDeps,
        });
      }
    } else {
      // No files specified — spawn cappedBuilderCount builders with no file scope
      for (let i = 0; i < cappedBuilderCount; i++) {
        agents.push({
          name: `builder-${i + 1}`,
          capability: "builder",
          task: objective,
          model: config.defaultBuilderModel,
          dependsOn: scoutDeps,
        });
      }
    }

    // Reviewer for complex tasks
    const builderNames = agents.filter((a) => a.capability === "builder").map((a) => a.name);
    agents.push({
      name: "reviewer-1",
      capability: "reviewer",
      task: `Review all changes for: ${objective.slice(0, 300)}. Check correctness, style, tests, and cross-cutting consistency.`,
      model: config.defaultReviewerModel,
      dependsOn: builderNames,
    });

    const finalBuilderCount = agents.filter((a) => a.capability === "builder").length;
    const scoutCount = agents.filter((a) => a.capability === "scout").length;
    reasoning = `Complex task (score: ${totalScore.toFixed(1)}, ${files.length} files, ${new Set(files.map((f) => f.split("/")[0])).size} dirs). ${scoutCount} scout(s), ${finalBuilderCount} builder(s), 1 reviewer.`;

    // Validate file overlap across auto-partitioned builders (appended after reasoning is set)
    const autoPlanBuilders = agents
      .filter((a) => a.capability === "builder")
      .map((a) => ({ name: a.name, files: a.files ?? [], task: a.task }));
    const autoOverlapWarning = detectFileOverlap(autoPlanBuilders);
    if (autoOverlapWarning) reasoning += " " + autoOverlapWarning;
  }

  return applyBudgetAndConcurrency(objective, complexity, agents, reasoning, config);
}

// ── Explicit builder plan ────────────────────────────────────────────

function planFromExplicit(
  objective: string,
  explicitBuilders: ExplicitBuilder[],
  allFiles: string[],
  totalScore: number,
  config: SwarmConfig,
): SwarmPlan {
  const complexity = "complex" as const; // Explicit multi-builder = always complex
  const agents: PlannedAgent[] = [];

  // Scout first (unless we'd blow the budget)
  const estimatedTotal = (1 + explicitBuilders.length + 1) * 0.80 + 0.15;
  if (estimatedTotal < config.maxBudgetUsd) {
    agents.push({
      name: "scout-1",
      capability: "scout",
      task: `Explore codebase to prepare context for ${explicitBuilders.length} builders. Objective: ${objective.slice(0, 500)}`,
      model: config.defaultScoutModel,
    });
  }

  const scoutDeps = agents.filter((a) => a.capability === "scout").map((a) => a.name);

  // Validate file ownership — warn if any file appears in multiple builders
  const overlapWarning = detectFileOverlap(explicitBuilders);

  // Create a builder for each explicit section
  for (const eb of explicitBuilders) {
    agents.push({
      name: eb.name,
      capability: "builder",
      task: eb.task,
      files: eb.files.length > 0 ? eb.files : undefined,
      scope: eb.files.length > 0 ? computeScope(eb.files) : undefined,
      model: config.defaultBuilderModel,
      dependsOn: scoutDeps.length > 0 ? [...scoutDeps] : undefined,
    });
  }

  // Reviewer
  const builderNames = agents.filter((a) => a.capability === "builder").map((a) => a.name);
  agents.push({
    name: "reviewer-1",
    capability: "reviewer",
    task: `Review all changes across ${builderNames.length} builders for: ${objective.slice(0, 300)}. Check cross-cutting consistency, correctness, and integration.`,
    model: config.defaultReviewerModel,
    dependsOn: builderNames,
  });

  let reasoning = `Explicit ${explicitBuilders.length}-builder plan detected (score: ${totalScore.toFixed(1)}). Honouring user-specified structure.`;
  if (overlapWarning) reasoning += " " + overlapWarning;

  return applyBudgetAndConcurrency(objective, complexity, agents, reasoning, config);
}

// ── Budget & concurrency caps ────────────────────────────────────────

function applyBudgetAndConcurrency(
  objective: string,
  complexity: "simple" | "moderate" | "complex",
  agents: PlannedAgent[],
  reasoning: string,
  config: SwarmConfig,
): SwarmPlan {
  let estimatedCost = 0;
  for (const agent of agents) {
    estimatedCost += estimateSessionCost(agent.model ?? config.defaultBuilderModel);
  }

  if (estimatedCost > config.maxBudgetUsd) {
    // Trim from the end: reviewers first, then extra builders, keep at least 1
    while (estimatedCost > config.maxBudgetUsd && agents.length > 1) {
      const removed = agents.pop()!;
      estimatedCost -= estimateSessionCost(removed.model ?? config.defaultBuilderModel);
    }
    reasoning += ` Budget-capped to ${agents.length} agent(s) ($${estimatedCost.toFixed(2)} < $${config.maxBudgetUsd}).`;
  }

  if (agents.length > config.maxConcurrent) {
    agents.splice(config.maxConcurrent);
    reasoning += ` Concurrency-capped to ${config.maxConcurrent}.`;
  }

  // Recalculate cost after caps
  estimatedCost = 0;
  for (const agent of agents) {
    estimatedCost += estimateSessionCost(agent.model ?? config.defaultBuilderModel);
  }

  return { objective, complexity, agents, estimatedCost, reasoning };
}
