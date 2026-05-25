/**
 * Atlas TUI Widget — Verification Script
 *
 * Tests pure rendering logic and state management in core.ts.
 * Does NOT require a Pi session, terminal, or @mariozechner/pi-tui.
 *
 * Run:
 *   npx tsx pi-package/atlas-ops/extensions/atlas-tui-widget/tests/verify.ts
 *   # or from atlas-ops dir:
 *   npm run verify-tui
 *
 * Test categories:
 *   1. Width-safety: every rendered line ≤ requested width
 *   2. Header format: phase, agents, tools, errors, elapsed labels
 *   3. Status/color correctness: statusColor(), statusIcon(), rowIcon() mappings
 *   4. Bounded memory: recentActivity never exceeds MAX_ACTIVITY
 *   5. summarizeArgs: argument extraction priority
 *   6. fmtDuration: ms/s/m:ss formatting
 *   7. Non-interactive guard + capActiveTools
 */

import { strict as assert } from "node:assert";

import {
  MAX_ACTIVITY,
  capActiveTools,
  createState,
  fmtDuration,
  isDelegationTool,
  pushBounded,
  renderWidget,
  rowIcon,
  statusColor,
  statusIcon,
  summarizeArgs,
  type ActivityEntry,
  type Theme,
  type WidthFns,
} from "../src/core";

// ─── Mock theme (identity — no ANSI codes → visibleWidth = string length) ────

const mockTheme: Theme = {
  fg: (_color: string, text: string) => text,
};

// ─── Mock width functions (simple character count, no ANSI awareness) ─────────

const mockWidthFns: WidthFns = {
  /** Simple truncation: chop at width, append ellipsis if provided and string is longer. */
  truncate: (str: string, width: number, ellipsis?: string): string => {
    if (str.length <= width) return str;
    if (ellipsis && ellipsis.length < width) {
      return str.slice(0, width - ellipsis.length) + ellipsis;
    }
    return str.slice(0, width);
  },
  visible: (str: string): number => str.length,
};

// ─── Test runner ──────────────────────────────────────────────────────────────

let passed = 0;
let failed = 0;

function test(name: string, fn: () => void) {
  try {
    fn();
    console.log(`  ✓  ${name}`);
    passed++;
  } catch (err) {
    console.error(`  ✗  ${name}`);
    console.error(`     ${(err as Error).message}`);
    failed++;
  }
}

// ─── 1. Width-safety ─────────────────────────────────────────────────────────

console.log("\n── 1. Width-safety ──");

test("empty state renders all lines ≤ 80 cols", () => {
  const state = createState();
  const lines = renderWidget(state, mockTheme, 80, mockWidthFns);
  for (const line of lines) {
    assert.ok(
      line.length <= 80,
      `Line too wide (${line.length} > 80): "${line}"`,
    );
  }
});

test("empty state renders all lines ≤ 40 cols (minimum)", () => {
  const state = createState();
  const lines = renderWidget(state, mockTheme, 40, mockWidthFns);
  for (const line of lines) {
    assert.ok(
      line.length <= 40,
      `Line too wide (${line.length} > 40): "${line}"`,
    );
  }
});

test("returns empty array when width < 40 (too narrow to render)", () => {
  const state = createState();
  const lines = renderWidget(state, mockTheme, 30, mockWidthFns);
  assert.strictEqual(lines.length, 0, "Should return [] for width < 40");
});

test("running tool renders all lines ≤ 120 cols", () => {
  const state = createState();
  state.activeTools.set("t1", {
    toolCallId: "t1",
    toolName: "Bash",
    args: "find /root/atlas -name '*.py' | xargs grep -l 'momentum_breakout'",
    status: "running",
    startMs: Date.now() - 2500,
  });
  const lines = renderWidget(state, mockTheme, 120, mockWidthFns);
  for (const line of lines) {
    assert.ok(line.length <= 120, `Line too wide (${line.length} > 120): "${line}"`);
  }
});

test("very long tool name and args render ≤ 60 cols", () => {
  const state = createState();
  state.recentActivity.push({
    toolCallId: "t2",
    toolName: "atlas_jobs_run_very_long_name_that_exceeds_column",
    args: "This is an extremely long argument string that should be truncated properly",
    status: "success",
    startMs: Date.now() - 5000,
    durationMs: 4200,
  });
  const lines = renderWidget(state, mockTheme, 60, mockWidthFns);
  for (const line of lines) {
    assert.ok(line.length <= 60, `Line too wide (${line.length} > 60): "${line}"`);
  }
});

test("mixed running + completed renders ≤ 80 cols", () => {
  const state = createState();
  state.toolTotal = 3;
  state.toolSuccess = 2;
  state.delegations = 1;
  state.activeTools.set("run1", {
    toolCallId: "run1",
    toolName: "subagent",
    args: "researcher — deep analysis task",
    status: "running",
    startMs: Date.now() - 10_000,
  });
  state.recentActivity.push(
    {
      toolCallId: "c1",
      toolName: "Read",
      args: "memory/SUMMARY.md",
      status: "success",
      startMs: Date.now() - 3000,
      durationMs: 200,
    },
    {
      toolCallId: "c2",
      toolName: "Bash",
      args: "git status --short",
      status: "success",
      startMs: Date.now() - 2000,
      durationMs: 89,
    },
  );
  const lines = renderWidget(state, mockTheme, 80, mockWidthFns);
  for (const line of lines) {
    assert.ok(line.length <= 80, `Line too wide (${line.length} > 80): "${line}"`);
  }
});

test("separator line is exactly `width` chars", () => {
  const state = createState();
  const lines = renderWidget(state, mockTheme, 80, mockWidthFns);
  // Line at index 1 is the separator "─".repeat(width) + possible theme.fg wrapper.
  // With mockTheme (identity), it should be exactly "─".repeat(80).
  assert.strictEqual(lines[1], "─".repeat(80), "Separator should fill exactly width");
});

// ─── 2. Header format ────────────────────────────────────────────────────────

console.log("\n── 2. Header format ──");

test("header contains '◆' accent marker", () => {
  const state = createState();
  const rendered = renderWidget(state, mockTheme, 80, mockWidthFns).join("\n");
  assert.ok(rendered.includes("◆"), `Expected '◆' in header:\n${rendered}`);
});

test("header shows 'idle' when no active tools", () => {
  const state = createState();
  const rendered = renderWidget(state, mockTheme, 80, mockWidthFns)[0];
  assert.ok(rendered.includes("idle"), `Expected 'idle' in header: "${rendered}"`);
  assert.ok(!rendered.includes("working"), `Expected no 'working' in idle header: "${rendered}"`);
});

test("header shows 'working' when tools are active", () => {
  const state = createState();
  state.activeTools.set("r1", {
    toolCallId: "r1",
    toolName: "Bash",
    args: "sleep 1",
    status: "running",
    startMs: Date.now(),
  });
  const rendered = renderWidget(state, mockTheme, 80, mockWidthFns)[0];
  assert.ok(rendered.includes("working"), `Expected 'working' in header: "${rendered}"`);
  assert.ok(!rendered.includes("idle"), `Expected no 'idle' in working header: "${rendered}"`);
});

test("header shows 'agents' label", () => {
  const state = createState();
  const rendered = renderWidget(state, mockTheme, 80, mockWidthFns)[0];
  assert.ok(rendered.includes("agents"), `Expected 'agents' label in header: "${rendered}"`);
});

test("header shows 'tools' label", () => {
  const state = createState();
  state.toolTotal = 5;
  const rendered = renderWidget(state, mockTheme, 80, mockWidthFns)[0];
  assert.ok(rendered.includes("tools"), `Expected 'tools' label in header: "${rendered}"`);
  assert.ok(rendered.includes("5"), `Expected tool count '5' in header: "${rendered}"`);
});

test("header shows 'errors' label with count", () => {
  const state = createState();
  state.toolError = 3;
  const rendered = renderWidget(state, mockTheme, 80, mockWidthFns)[0];
  assert.ok(rendered.includes("errors"), `Expected 'errors' label in header: "${rendered}"`);
  assert.ok(rendered.includes("3"), `Expected error count '3' in header: "${rendered}"`);
});

test("header shows 'elapsed' label", () => {
  const state = createState();
  const rendered = renderWidget(state, mockTheme, 80, mockWidthFns)[0];
  assert.ok(rendered.includes("elapsed"), `Expected 'elapsed' in header: "${rendered}"`);
});

test("header shows 'agents 0' when no delegations have run", () => {
  const state = createState();
  // state.delegations = 0 by default — no delegation tools ran yet
  const header = renderWidget(state, mockTheme, 80, mockWidthFns)[0];
  assert.ok(
    /agents\s+0(?!\/)/.test(header),
    `Expected 'agents 0' (no slash) in header: "${header}"`,
  );
});

test("header shows active/total when delegation tools exist", () => {
  const state = createState();
  state.delegations = 3; // 3 total this session
  // 2 currently in-flight delegation tools + 1 regular tool
  state.activeTools.set("a1", {
    toolCallId: "a1", toolName: "subagent", args: "scout",
    status: "running", startMs: Date.now(),
  });
  state.activeTools.set("a2", {
    toolCallId: "a2", toolName: "swarm", args: "build",
    status: "running", startMs: Date.now(),
  });
  state.activeTools.set("t1", {
    toolCallId: "t1", toolName: "Bash", args: "ls",
    status: "running", startMs: Date.now(),
  });
  const header = renderWidget(state, mockTheme, 80, mockWidthFns)[0];
  // "agents 2/3" — 2 active delegation tools, 3 total (Bash excluded)
  assert.ok(
    /agents\s+2\/3/.test(header),
    `Expected 'agents 2/3' in header: "${header}"`,
  );
});

test("header shows '0/N' when delegations ran but none currently active", () => {
  const state = createState();
  state.delegations = 2; // 2 total, none in-flight right now
  const header = renderWidget(state, mockTheme, 80, mockWidthFns)[0];
  assert.ok(
    /agents\s+0\/2/.test(header),
    `Expected 'agents 0/2' in header: "${header}"`,
  );
});

// ─── 3. Status / color correctness ───────────────────────────────────────────

console.log("\n── 3. Status/color correctness ──");

test("statusColor(running) → 'warning'", () => {
  assert.strictEqual(statusColor("running"), "warning");
});

test("statusColor(success) → 'success'", () => {
  assert.strictEqual(statusColor("success"), "success");
});

test("statusColor(error) → 'error'", () => {
  assert.strictEqual(statusColor("error"), "error");
});

test("statusIcon(running) → '⟳'", () => {
  assert.strictEqual(statusIcon("running"), "⟳");
});

test("statusIcon(success) → '✓'", () => {
  assert.strictEqual(statusIcon("success"), "✓");
});

test("statusIcon(error) → '✗'", () => {
  assert.strictEqual(statusIcon("error"), "✗");
});

test("rowIcon: delegation + running → '→'", () => {
  assert.strictEqual(rowIcon("running", true), "→");
});

test("rowIcon: delegation + success → '✓' (not '→')", () => {
  assert.strictEqual(rowIcon("success", true), "✓");
});

test("rowIcon: non-delegation + running → '⟳'", () => {
  assert.strictEqual(rowIcon("running", false), "⟳");
});

test("isDelegationTool: subagent → true", () => {
  assert.ok(isDelegationTool("subagent"));
});

test("isDelegationTool: swarm → true", () => {
  assert.ok(isDelegationTool("swarm"));
});

test("isDelegationTool: delegate_scout → true (prefix match)", () => {
  assert.ok(isDelegationTool("delegate_scout"));
});

test("isDelegationTool: Bash → false", () => {
  assert.ok(!isDelegationTool("Bash"));
});

test("isDelegationTool: Read → false", () => {
  assert.ok(!isDelegationTool("Read"));
});

test("isDelegationTool: atlas_elastic_run → true (elastic burst counts as delegation)", () => {
  assert.ok(isDelegationTool("atlas_elastic_run"));
});

test("renderWidget uses → for running atlas_elastic_run tool", () => {
  const state = createState();
  state.delegations = 1;
  state.activeTools.set("er1", {
    toolCallId: "er1",
    toolName: "atlas_elastic_run",
    args: "review authentication module",
    status: "running",
    startMs: Date.now() - 2000,
  });
  const rendered = renderWidget(state, mockTheme, 80, mockWidthFns).join("\n");
  assert.ok(rendered.includes("→"), `Expected '→' for atlas_elastic_run in:\n${rendered}`);
  assert.ok(rendered.includes("atlas_elastic_ru"), `Expected tool name in feed:\n${rendered}`);
});

test("header counts atlas_elastic_run as active delegation agent", () => {
  const state = createState();
  state.delegations = 1;
  state.activeTools.set("er1", {
    toolCallId: "er1",
    toolName: "atlas_elastic_run",
    args: "verify code",
    status: "running",
    startMs: Date.now(),
  });
  const header = renderWidget(state, mockTheme, 80, mockWidthFns)[0];
  // Should show '1/1' — 1 active delegation, 1 total
  assert.ok(
    /agents\s+1\/1/.test(header),
    `Expected 'agents 1/1' in header: "${header}"`
  );
});

test("renderWidget includes ✓ for a success entry", () => {
  const state = createState();
  state.toolSuccess = 1;
  state.toolTotal = 1;
  state.recentActivity.push({
    toolCallId: "ok1",
    toolName: "Read",
    args: "foo.ts",
    status: "success",
    startMs: Date.now() - 500,
    durationMs: 120,
  });
  const rendered = renderWidget(state, mockTheme, 80, mockWidthFns).join("\n");
  assert.ok(rendered.includes("✓"), `Expected '✓' in:\n${rendered}`);
});

test("renderWidget includes ✗ for an error entry", () => {
  const state = createState();
  state.toolError = 1;
  state.toolTotal = 1;
  state.recentActivity.push({
    toolCallId: "err1",
    toolName: "Write",
    args: "tasks/todo.md",
    status: "error",
    startMs: Date.now() - 100,
    durationMs: 50,
  });
  const rendered = renderWidget(state, mockTheme, 80, mockWidthFns).join("\n");
  assert.ok(rendered.includes("✗"), `Expected '✗' in:\n${rendered}`);
});

test("renderWidget includes ⟳ for a running non-delegation tool", () => {
  const state = createState();
  state.activeTools.set("run1", {
    toolCallId: "run1",
    toolName: "atlas_jobs_run",
    args: "health_check",
    status: "running",
    startMs: Date.now() - 1200,
  });
  const rendered = renderWidget(state, mockTheme, 80, mockWidthFns).join("\n");
  assert.ok(rendered.includes("⟳"), `Expected '⟳' in:\n${rendered}`);
});

test("renderWidget uses → for running delegation tool (subagent)", () => {
  const state = createState();
  state.activeTools.set("del1", {
    toolCallId: "del1",
    toolName: "subagent",
    args: "researcher — analysis",
    status: "running",
    startMs: Date.now() - 1000,
  });
  const rendered = renderWidget(state, mockTheme, 80, mockWidthFns).join("\n");
  assert.ok(rendered.includes("→"), `Expected '→' for running delegation in:\n${rendered}`);
});

// ─── 4. Bounded memory ────────────────────────────────────────────────────────

console.log("\n── 4. Bounded memory ──");

test(`pushBounded never exceeds MAX_ACTIVITY (${MAX_ACTIVITY})`, () => {
  const state = createState();
  for (let i = 0; i < MAX_ACTIVITY + 5; i++) {
    const entry: ActivityEntry = {
      toolCallId: `t${i}`,
      toolName: "Read",
      args: `file${i}.ts`,
      status: "success",
      startMs: Date.now(),
      durationMs: 100,
    };
    pushBounded(state.recentActivity, entry, MAX_ACTIVITY);
  }
  assert.ok(
    state.recentActivity.length <= MAX_ACTIVITY,
    `Expected ≤ ${MAX_ACTIVITY}, got ${state.recentActivity.length}`,
  );
});

test("FIFO eviction: oldest entries removed first", () => {
  const state = createState();
  for (let i = 0; i < MAX_ACTIVITY + 2; i++) {
    pushBounded(
      state.recentActivity,
      {
        toolCallId: `t${i}`,
        toolName: "Read",
        args: `file${i}.ts`,
        status: "success",
        startMs: Date.now(),
        durationMs: 100,
      },
      MAX_ACTIVITY,
    );
  }
  assert.ok(
    !state.recentActivity.find((e) => e.toolCallId === "t0"),
    "t0 should have been evicted",
  );
  assert.ok(
    !state.recentActivity.find((e) => e.toolCallId === "t1"),
    "t1 should have been evicted",
  );
  const newest = `t${MAX_ACTIVITY + 1}`;
  assert.ok(
    state.recentActivity.find((e) => e.toolCallId === newest),
    `${newest} should be present after eviction`,
  );
});

test("rendered feed is capped at MAX_ACTIVITY rows even with many active tools", () => {
  const state = createState();
  // Fill more concurrent tools than MAX_ACTIVITY
  for (let i = 0; i < MAX_ACTIVITY + 3; i++) {
    state.activeTools.set(`run${i}`, {
      toolCallId: `run${i}`,
      toolName: "Bash",
      args: `cmd${i}`,
      status: "running",
      startMs: Date.now(),
    });
  }
  const lines = renderWidget(state, mockTheme, 80, mockWidthFns);
  // Header + separator + up to MAX_ACTIVITY activity rows
  assert.ok(
    lines.length <= 2 + MAX_ACTIVITY,
    `Expected ≤ ${2 + MAX_ACTIVITY} lines, got ${lines.length}`,
  );
});

test("createState produces a fresh state with zeroed counters", () => {
  const s1 = createState();
  s1.toolTotal = 99;
  const s2 = createState();
  assert.strictEqual(s2.toolTotal, 0, "New state should have toolTotal=0");
  assert.strictEqual(s2.recentActivity.length, 0, "New state should have empty activity");
  assert.strictEqual(s2.activeTools.size, 0, "New state should have empty activeTools");
  assert.ok(s2.enabled, "New state should be enabled");
});

// ─── 5. summarizeArgs ────────────────────────────────────────────────────────

console.log("\n── 5. summarizeArgs ──");

test("prefers path over other fields", () => {
  const result = summarizeArgs({ path: "foo/bar.ts", command: "echo hi" });
  assert.strictEqual(result, "foo/bar.ts");
});

test("prefers command when no path", () => {
  const result = summarizeArgs({ command: "git status", query: "x" });
  assert.strictEqual(result, "git status");
});

test("strips newlines from command", () => {
  const result = summarizeArgs({ command: "echo\nhello\nworld" });
  assert.ok(!result.includes("\n"), "Should not contain newlines");
  assert.ok(result.includes("echo"), "Should still contain the command");
});

test("falls back to first value when no priority key present", () => {
  const result = summarizeArgs({ objective: "build feature X" });
  assert.strictEqual(result, "build feature X");
});

test("handles empty args gracefully", () => {
  const result = summarizeArgs({});
  assert.strictEqual(result, "");
});

test("handles non-object args gracefully", () => {
  // Cast to bypass TS - simulates runtime unexpected value
  const result = summarizeArgs(null as unknown as Record<string, unknown>);
  assert.strictEqual(result, "");
});

// ─── 6. fmtDuration ──────────────────────────────────────────────────────────

console.log("\n── 6. fmtDuration ──");

test("< 1 s → milliseconds", () => {
  assert.strictEqual(fmtDuration(450), "450ms");
});

test("exactly 1 s → '1.0s'", () => {
  assert.strictEqual(fmtDuration(1000), "1.0s");
});

test("1–59 s → decimal seconds", () => {
  assert.strictEqual(fmtDuration(4200), "4.2s");
});

test("≥ 60 s → m:ss", () => {
  assert.strictEqual(fmtDuration(90_000), "1:30");
});

test("59.9 s → '59.9s' (not minutes)", () => {
  assert.strictEqual(fmtDuration(59_900), "59.9s");
});

test("0 ms → '0ms' (edge: zero input)", () => {
  assert.strictEqual(fmtDuration(0), "0ms");
});

// ─── 7. Non-interactive guard + capActiveTools ────────────────────────────────

// NOTE: The /atlas-tui command handler lives in index.ts, which imports
// @mariozechner/pi-coding-agent and @mariozechner/pi-tui — neither of which
// is available in this test runner context. The guard is verified by code
// review: the handler early-returns without touching ctx.ui when ctx.hasUI
// is false.

console.log("\n── 7. Non-interactive guard + capActiveTools ──");

test("capActiveTools evicts oldest entries when limit exceeded", () => {
  const state = createState();
  const limit = 4;
  for (let i = 0; i < limit + 3; i++) {
    state.activeTools.set(`run${i}`, {
      toolCallId: `run${i}`,
      toolName: "Bash",
      args: `cmd${i}`,
      status: "running",
      startMs: Date.now(),
    });
  }
  capActiveTools(state, limit);
  assert.ok(
    state.activeTools.size <= limit,
    `Expected ≤ ${limit} active tools, got ${state.activeTools.size}`,
  );
  assert.ok(!state.activeTools.has("run0"), "Oldest entry (run0) should have been evicted");
  assert.ok(!state.activeTools.has("run1"), "Second entry (run1) should have been evicted");
  assert.ok(!state.activeTools.has("run2"), "Third entry (run2) should have been evicted");
  assert.ok(
    state.activeTools.has(`run${limit + 2}`),
    "Newest entry should be retained",
  );
});

test("capActiveTools is a no-op when under the limit", () => {
  const state = createState();
  state.activeTools.set("r1", {
    toolCallId: "r1",
    toolName: "Bash",
    args: "cmd",
    status: "running",
    startMs: Date.now(),
  });
  capActiveTools(state, 10);
  assert.strictEqual(state.activeTools.size, 1, "Should not evict when under limit");
});

test("capActiveTools is a no-op on empty activeTools (limit=0 edge case)", () => {
  const state = createState();
  // Must not throw even with an aggressive limit of 0
  capActiveTools(state, 0);
  assert.strictEqual(state.activeTools.size, 0);
});

// ─── Summary ──────────────────────────────────────────────────────────────────

console.log(`\n${"─".repeat(50)}`);
const total = passed + failed;
if (failed === 0) {
  console.log(`✓  All ${total} tests passed\n`);
} else {
  console.error(`✗  ${failed}/${total} tests failed\n`);
  process.exit(1);
}
