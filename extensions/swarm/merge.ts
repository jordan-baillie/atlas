/**
 * Branch merge logic for swarm agents.
 *
 * After builders complete, their worktree branches need to be merged
 * back into the base branch. Merges are ordered by conflict score —
 * least-conflicting branches merge first to maximise the number of
 * successful merges. After each successful merge the remaining branches
 * are re-evaluated against the updated target HEAD.
 *
 * If a merge fails with conflicts, a rebase fallback is attempted before
 * reporting the failure. Conflict errors include the first 5 lines of
 * diff output for rapid triage.
 */

import type { ExtensionAPI } from "@mariozechner/pi-coding-agent";
import type { MergeResult } from "./types.js";

// ── Helpers ───────────────────────────────────────────────────────────

/**
 * Return a diff snippet (first `maxLines` non-empty lines) for the given
 * conflicted files. Called while the repo is in a conflicted-merge state so
 * `git diff -- <file>` shows the conflict markers vs HEAD.
 */
async function getConflictDiffSnippet(
  pi: ExtensionAPI,
  repoRoot: string,
  conflictedFiles: string[],
  maxLines = 5,
): Promise<string> {
  if (conflictedFiles.length === 0) return "";

  const snippets: string[] = [];
  // Cap at 3 files to keep the message readable
  for (const file of conflictedFiles.slice(0, 3)) {
    const diffResult = await pi.exec("git", ["diff", "--", file], { cwd: repoRoot });
    const lines = (diffResult.stdout ?? "")
      .split("\n")
      .filter((l) => l.trim())
      .slice(0, maxLines);
    if (lines.length > 0) {
      snippets.push(`--- ${file} ---\n${lines.join("\n")}`);
    }
  }
  return snippets.join("\n\n");
}

// ── Conflict matrix ───────────────────────────────────────────────────

/**
 * Build a pairwise conflict matrix for a set of branches.
 *
 * Uses `git merge-tree --write-tree` (a no-op dry-run) to check:
 *   a) each branch against `targetBranch`, and
 *   b) each branch pair against each other.
 *
 * Returns a `Map<branch, Set<conflicting-branch>>` where entries in the
 * set are other branch names (including `targetBranch` if that branch
 * would conflict with the current target HEAD).
 */
export async function buildConflictMatrix(
  pi: ExtensionAPI,
  repoRoot: string,
  branches: { branch: string; agentName: string }[],
  targetBranch: string,
): Promise<Map<string, Set<string>>> {
  const matrix = new Map<string, Set<string>>();

  for (const { branch } of branches) {
    matrix.set(branch, new Set<string>());
  }

  // ── Phase 1: each branch vs target ───────────────────────────────
  for (const { branch } of branches) {
    const result = await pi.exec(
      "git",
      ["merge-tree", "--write-tree", targetBranch, branch],
      { cwd: repoRoot },
    );
    if (result.code !== 0) {
      matrix.get(branch)!.add(targetBranch);
    }
  }

  // ── Phase 2: pairwise cross-checks ───────────────────────────────
  for (let i = 0; i < branches.length; i++) {
    for (let j = i + 1; j < branches.length; j++) {
      const a = branches[i].branch;
      const b = branches[j].branch;
      const result = await pi.exec(
        "git",
        ["merge-tree", "--write-tree", a, b],
        { cwd: repoRoot },
      );
      if (result.code !== 0) {
        matrix.get(a)!.add(b);
        matrix.get(b)!.add(a);
      }
    }
  }

  return matrix;
}

/** Compute a sort score for a branch: target conflicts are heavily penalised. */
function conflictScore(
  branch: string,
  targetBranch: string,
  matrix: Map<string, Set<string>>,
): number {
  const set = matrix.get(branch) ?? new Set<string>();
  const targetConflict = set.has(targetBranch) ? 100 : 0;
  const pairwise = [...set].filter((b) => b !== targetBranch).length;
  return targetConflict + pairwise;
}

// ── Single-branch merge ───────────────────────────────────────────────

/**
 * Merge a single agent branch into the target branch.
 *
 * On conflict:
 *   1. Captures a diff snippet (first 5 lines per conflicted file).
 *   2. Aborts the conflicted merge.
 *   3. Attempts a rebase fallback (`git rebase --onto targetBranch`).
 *   4. If the rebase succeeds, fast-forward merges and returns success.
 *   5. If the rebase also fails, aborts it and returns the original
 *      conflict error enriched with the diff snippet.
 */
export async function mergeBranch(
  pi: ExtensionAPI,
  repoRoot: string,
  agentBranch: string,
  targetBranch: string,
  agentName: string,
): Promise<MergeResult> {
  // Ensure we're on the target branch
  const checkoutResult = await pi.exec("git", ["checkout", targetBranch], { cwd: repoRoot });
  if (checkoutResult.code !== 0) {
    return {
      branch: agentBranch,
      agentName,
      success: false,
      conflicts: [],
      error: `Failed to checkout ${targetBranch}: ${checkoutResult.stderr}`,
    };
  }

  // Attempt merge
  const mergeResult = await pi.exec(
    "git",
    ["merge", "--no-ff", agentBranch, "-m", `swarm: merge ${agentName} (${agentBranch})`],
    { cwd: repoRoot },
  );

  if (mergeResult.code === 0) {
    return { branch: agentBranch, agentName, success: true, conflicts: [] };
  }

  // Identify conflicting files
  const conflictResult = await pi.exec(
    "git",
    ["diff", "--name-only", "--diff-filter=U"],
    { cwd: repoRoot },
  );
  const conflicts = conflictResult.stdout
    .trim()
    .split("\n")
    .filter((l: string) => l.trim());

  if (conflicts.length > 0) {
    // Capture diff snippet while still in the conflicted-merge state
    const diffSnippet = await getConflictDiffSnippet(pi, repoRoot, conflicts);

    // Abort the conflicted merge
    await pi.exec("git", ["merge", "--abort"], { cwd: repoRoot });

    // ── Rebase fallback ───────────────────────────────────────────
    const rebaseResult = await pi.exec(
      "git",
      ["rebase", "--onto", targetBranch, `${targetBranch}~1`, agentBranch],
      { cwd: repoRoot },
    );

    if (rebaseResult.code === 0) {
      // Rebase succeeded — switch to target and fast-forward
      await pi.exec("git", ["checkout", targetBranch], { cwd: repoRoot });
      const ffResult = await pi.exec(
        "git",
        ["merge", "--ff-only", agentBranch],
        { cwd: repoRoot },
      );
      if (ffResult.code === 0) {
        return { branch: agentBranch, agentName, success: true, conflicts: [] };
      }
      // FF failed after rebase — abort and fall through to conflict report
      await pi.exec("git", ["rebase", "--abort"], { cwd: repoRoot });
    } else {
      // Rebase itself conflicted — abort it cleanly
      await pi.exec("git", ["rebase", "--abort"], { cwd: repoRoot });
    }

    // Report original conflict with diff context
    const diffContext = diffSnippet ? `\n\nConflict preview:\n${diffSnippet}` : "";
    return {
      branch: agentBranch,
      agentName,
      success: false,
      conflicts,
      error: `Merge conflicts in ${conflicts.length} file(s): ${conflicts.join(", ")}${diffContext}`,
    };
  }

  // Non-conflict failure (no unmerged files detected)
  return {
    branch: agentBranch,
    agentName,
    success: false,
    conflicts: [],
    error: mergeResult.stderr,
  };
}

// ── Multi-branch merge ────────────────────────────────────────────────

/**
 * Merge multiple agent branches, ordering by conflict score so the
 * cleanest branch merges first.
 *
 * Algorithm:
 *   1. Build a conflict matrix for remaining branches against the current
 *      target HEAD.
 *   2. Sort ascending by score: target conflicts penalised heavily (×100),
 *      then by pairwise conflict count.
 *   3. Merge the lowest-scoring branch.
 *   4. After each success the target HEAD advances — loop back to step 1
 *      with the updated target so that previously-conflicting branches may
 *      now resolve cleanly.
 *   5. All branches are attempted; failures are collected and returned.
 */
export async function mergeAll(
  pi: ExtensionAPI,
  repoRoot: string,
  branches: { branch: string; agentName: string }[],
  targetBranch: string,
): Promise<MergeResult[]> {
  const results: MergeResult[] = [];
  let remaining = [...branches];

  while (remaining.length > 0) {
    // Re-build matrix against current target HEAD each iteration
    const matrix = await buildConflictMatrix(pi, repoRoot, remaining, targetBranch);

    // Sort ascending by conflict score (least conflicting first)
    remaining.sort(
      (a, b) =>
        conflictScore(a.branch, targetBranch, matrix) -
        conflictScore(b.branch, targetBranch, matrix),
    );

    // Take the head branch and attempt the merge
    const next = remaining.shift()!;
    const result = await mergeBranch(pi, repoRoot, next.branch, targetBranch, next.agentName);
    results.push(result);
    // Always continue — all branches are attempted even on failure.
  }

  return results;
}

// ── Dry-run mergeability check ────────────────────────────────────────

/**
 * Dry-run merge check — see if a branch can merge cleanly.
 */
export async function checkMergeability(
  pi: ExtensionAPI,
  repoRoot: string,
  agentBranch: string,
  targetBranch: string,
): Promise<{ clean: boolean; conflicts: string[] }> {
  const result = await pi.exec(
    "git",
    ["merge-tree", "--write-tree", targetBranch, agentBranch],
    { cwd: repoRoot },
  );

  if (result.code === 0) {
    return { clean: true, conflicts: [] };
  }

  // Parse conflict file list from stderr output
  const conflicts = result.stderr
    .split("\n")
    .filter((l: string) => l.includes("CONFLICT"))
    .map((l: string) => l.replace(/^CONFLICT.*:\s*/, "").trim());

  return { clean: false, conflicts };
}

// ── Post-merge tests ──────────────────────────────────────────────────

/**
 * Run a post-merge test command and report whether it passed.
 *
 * @param command  Shell command to execute (run via `sh -c`).
 * @returns        `{ passed, output }` — `passed` is true iff exit code is 0.
 */
export async function runPostMergeTests(
  pi: ExtensionAPI,
  repoRoot: string,
  command: string,
): Promise<{ passed: boolean; output: string }> {
  const result = await pi.exec("sh", ["-c", command], { cwd: repoRoot });
  const output = [(result.stdout ?? ""), (result.stderr ?? "")]
    .map((s: string) => s.trim())
    .filter(Boolean)
    .join("\n");
  return {
    passed: (result.code ?? 1) === 0,
    output,
  };
}
