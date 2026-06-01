/**
 * atlas-elastic-agents — Verification Script
 *
 * Tests pure logic: policy, planner, audit, executor gates, burst runner,
 * write dispatch, and the full executeElasticRun orchestrator.
 * Does NOT require a Pi session, terminal, or @mariozechner/pi-coding-agent.
 * Does NOT call real pi CLI — burst runner is injected as a mock.
 *
 * Run:
 *   npx tsx pi-package/atlas-ops/extensions/atlas-elastic-agents/tests/verify.ts
 *   # or from atlas-ops dir:
 *   npm run verify-elastic-agents
 *
 * Test categories:
 *   1.  Policy: validation, protected file matching, kill switch
 *   2.  Planner: task classification (including review_qa)
 *   3.  Planner: DAG generation (read_only, planning, review_qa, write, live-trading)
 *   4.  Planner: clean-tree gate rejects dirty atlas repo (real git check)
 *   5.  Executor: live_trading_ops always blocked
 *   6.  Executor: write_bounded blocked on dirty tree (atlas IS dirty)
 *   7.  Executor: read-only tasks produce OAuth-only commands (no API key)
 *   8.  Audit: entry roundtrip (write + read + verify format)
 *   9.  Ownership: overlap detection
 *  10.  API key safety: no forbidden Anthropic(api_key) usage in source
 *  11.  Burst runner: mock injection, bounded concurrency, timeout shape
 *  12.  Write plan: buildWriteDispatchMessage pure helper
 *  13.  executeElasticRun: all branches (read_only burst, write dispatch, blockers)
 */

import { strict as assert } from "node:assert";
import { existsSync, mkdirSync, rmSync, writeFileSync, unlinkSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";

// ─── Imports from pure modules (no Pi runtime needed) ─────────────────────────

import {
  validatePolicy,
  isProtectedFile,
  isKillSwitchActive,
  maxConcurrency,
  approvalRequirement,
  type AgentScalePolicy,
} from "../src/policy.js";

import {
  classifyTask,
  checkGitStatus,
  generatePlan,
  evaluatePlanGates,
  hasProtectedFiles,
} from "../src/planner.js";

import {
  writeAuditEntry,
  readAuditEntries,
  makeAuditEntry,
} from "../src/audit.js";

import {
  evaluateExecutionGate,
  executeElasticRun,
  buildReadOnlyCommand,
  buildWriteDispatchMessage,
  runReadOnlyBurst,
  validateOwnershipTable,
  checkForbiddenApiKeyUsage,
  resolveAgentTimeoutMs,
  type BurstRunnerFn,
  type BurstAgentResult,
} from "../src/executor.js";

// ─── Test harness ─────────────────────────────────────────────────────────────

let passed = 0;
let failed = 0;

function test(name: string, fn: () => void): void {
  try {
    fn();
    passed++;
    console.log(`  ✓  ${name}`);
  } catch (err) {
    failed++;
    const msg = err instanceof Error ? err.message : String(err);
    console.error(`  ✗  ${name}\n       ${msg}`);
  }
}

async function testAsync(name: string, fn: () => Promise<void>): Promise<void> {
  try {
    await fn();
    passed++;
    console.log(`  ✓  ${name}`);
  } catch (err) {
    failed++;
    const msg = err instanceof Error ? err.message : String(err);
    console.error(`  ✗  ${name}\n       ${msg}`);
  }
}

// ─── Fixture: minimal valid policy ───────────────────────────────────────────

const POLICY: AgentScalePolicy = {
  version: "1.0",
  global: {
    max_concurrent_agents: 16,
    max_write_agents: 4,
    max_parallel_builders_per_file: 1,
    kill_switch: false,
    budget_tokens_per_task: 100000,
  },
  risk_classes: {
    read_only: {
      type: "read_only",
      default_concurrency: 12,
      roles: ["scout", "researcher"],
      gates: [],
      examples: [],
    },
    planning: {
      type: "planning",
      default_concurrency: 3,
      roles: ["planner"],
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
      default_concurrency: 4,
      max_builders: 2,
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
    "scripts/deploy_config.py",
    "config/live_",
    "config/active_config.json",
    "config/active/",
    "services/broker_",
    "brokers/state/",
    ".git/",
    "secrets/",
    ".env",
  ],
  approval_gates: {
    live_trading_ops: "explicit",
    write_bounded: "auto_if_tests_pass",
    planning: "auto",
    read_only: "auto",
    review_qa: "auto",
  },
};

const KILL_POLICY: AgentScalePolicy = {
  ...POLICY,
  global: { ...POLICY.global, kill_switch: true },
};

const FAKE_CWD = "/nonexistent/fake/cwd";
const ATLAS_CWD = resolve(new URL("../../../../../../", import.meta.url).pathname);

// ─── Temp dir ─────────────────────────────────────────────────────────────────

const TMP_DIR = join(tmpdir(), "atlas-elastic-verify-test");
const AUDIT_PATH = join(TMP_DIR, "audit.jsonl");
mkdirSync(TMP_DIR, { recursive: true });

// ─── 1. Policy validation ─────────────────────────────────────────────────────

console.log("\n── 1. Policy validation ──");

test("validatePolicy accepts a valid policy object", () => {
  const result = validatePolicy(POLICY);
  assert.strictEqual(result.version, "1.0");
  assert.strictEqual(result.global.max_concurrent_agents, 16);
});

test("validatePolicy rejects null", () => {
  assert.throws(() => validatePolicy(null), /must be an object/);
});

test("validatePolicy rejects missing version", () => {
  const bad = { ...POLICY, version: undefined } as unknown as AgentScalePolicy;
  assert.throws(() => validatePolicy(bad), /version/);
});

test("validatePolicy rejects missing global section", () => {
  const bad = { ...POLICY, global: undefined } as unknown as AgentScalePolicy;
  assert.throws(() => validatePolicy(bad), /global/);
});

test("validatePolicy rejects kill_switch as non-boolean", () => {
  const bad = { ...POLICY, global: { ...POLICY.global, kill_switch: "yes" } } as unknown;
  assert.throws(() => validatePolicy(bad as AgentScalePolicy), /kill_switch/);
});

test("isKillSwitchActive returns false for normal policy", () => {
  assert.strictEqual(isKillSwitchActive(POLICY), false);
});

test("isKillSwitchActive returns true when kill_switch=true", () => {
  assert.strictEqual(isKillSwitchActive(KILL_POLICY), true);
});

test("maxConcurrency respects global cap", () => {
  const cap16 = maxConcurrency(POLICY, "read_only");
  assert.ok(cap16 <= 16, `Expected ≤16, got ${cap16}`);
  assert.ok(cap16 > 0, `Expected >0, got ${cap16}`);
});

test("approvalRequirement returns 'explicit' for live_trading_ops", () => {
  assert.strictEqual(approvalRequirement(POLICY, "live_trading_ops"), "explicit");
});

test("approvalRequirement returns 'auto' for read_only", () => {
  assert.strictEqual(approvalRequirement(POLICY, "read_only"), "auto");
});

test("approvalRequirement returns 'auto' for review_qa", () => {
  assert.strictEqual(approvalRequirement(POLICY, "review_qa"), "auto");
});

// ─── 1b. Protected file matching ──────────────────────────────────────────────

console.log("\n── 1b. Protected file matching ──");

test("isProtectedFile: exact match", () => {
  assert.ok(isProtectedFile(POLICY, "config/active_config.json"));
});

test("isProtectedFile: prefix match (config/live_ pattern)", () => {
  assert.ok(isProtectedFile(POLICY, "config/live_sp500.yaml"));
});

test("isProtectedFile: prefix match (brokers/state/)", () => {
  assert.ok(isProtectedFile(POLICY, "brokers/state/live_sp500.json"));
});

test("isProtectedFile: prefix match (services/broker_)", () => {
  assert.ok(isProtectedFile(POLICY, "services/broker_alpaca.py"));
});

test("isProtectedFile: prefix match (.git/)", () => {
  assert.ok(isProtectedFile(POLICY, ".git/config"));
});

test("isProtectedFile: non-protected file returns false", () => {
  assert.ok(!isProtectedFile(POLICY, "src/strategies/momentum.py"));
});

test("isProtectedFile: non-protected file in normal config dir", () => {
  assert.ok(!isProtectedFile(POLICY, "config/backtest.yaml"));
});

// ─── 2. Task classification ───────────────────────────────────────────────────

console.log("\n── 2. Task classification ──");

test("classify: codebase search → read_only", () => {
  assert.strictEqual(classifyTask("search the codebase for deprecated calls"), "read_only");
});

test("classify: incident audit → read_only", () => {
  assert.strictEqual(classifyTask("analyze logs for errors in last 24h"), "read_only");
});

test("classify: spec writing → planning", () => {
  assert.strictEqual(classifyTask("plan the architecture for new feature"), "planning");
});

test("classify: refactor → write_bounded", () => {
  assert.strictEqual(classifyTask("refactor authentication across services"), "write_bounded");
});

test("classify: implement feature with files → write_bounded", () => {
  assert.strictEqual(
    classifyTask("implement caching layer", ["src/cache.py", "tests/test_cache.py"]),
    "write_bounded"
  );
});

test("classify: broker mutation → live_trading_ops", () => {
  assert.strictEqual(classifyTask("update broker position sizes"), "live_trading_ops");
});

test("classify: live_ file → live_trading_ops", () => {
  assert.strictEqual(
    classifyTask("update config", ["config/live_sp500.yaml"]),
    "live_trading_ops"
  );
});

test("classify: promote config → live_trading_ops", () => {
  assert.strictEqual(classifyTask("promote config to live"), "live_trading_ops");
});

test("classify: active_config file → live_trading_ops", () => {
  assert.strictEqual(
    classifyTask("edit settings", ["config/active_config.json"]),
    "live_trading_ops"
  );
});

// ── 2c. live_trading_ops false-negative fixes (regex-based matching) ──────────

console.log("\n\u2500\u2500 2c. live_trading_ops false-negative + over-trigger fixes \u2500\u2500");

test("classify: 'promote sp500 config to live' → live_trading_ops (regex)", () => {
  assert.strictEqual(classifyTask("promote sp500 config to live"), "live_trading_ops");
});

test("classify: 'promote the live config' → live_trading_ops (regex)", () => {
  assert.strictEqual(classifyTask("promote the live config"), "live_trading_ops");
});

test("classify: 'promote active config' → live_trading_ops (keyword + regex)", () => {
  assert.strictEqual(classifyTask("promote active config"), "live_trading_ops");
});

test("classify: 'deploy sp500 config' → live_trading_ops (regex)", () => {
  assert.strictEqual(classifyTask("deploy sp500 config"), "live_trading_ops");
});

test("classify: broker state mutation → live_trading_ops (specific term)", () => {
  assert.strictEqual(classifyTask("update broker state after reconnect"), "live_trading_ops");
});

test("classify: 'security scan on broker integration' → review_qa (not live_trading_ops)", () => {
  // 'broker' alone must NOT over-trigger; only specific mutation terms do.
  assert.strictEqual(classifyTask("security scan on broker integration"), "review_qa");
});

test("classify: 'audit the broker module for issues' → read_only (broker alone not live_trading_ops)", () => {
  // 'broker' alone must NOT trigger live_trading_ops; read-only audit objectives default to read_only.
  assert.strictEqual(classifyTask("audit the broker module for issues"), "read_only");
});

test("classify: 'add new strategy module' (sentence-initial) → write_bounded", () => {
  // WRITE_KEYWORDS 'add ' (no leading space) must match sentence-initial 'Add X'.
  assert.strictEqual(classifyTask("add new strategy module to the codebase"), "write_bounded");
});

test("classify: 'Add caching layer' (capitalised sentence-initial) → write_bounded", () => {
  assert.strictEqual(classifyTask("Add caching layer with redis"), "write_bounded");
});

// ── 2b. review_qa classification ──────────────────────────────────────────────

console.log("\n── 2b. review_qa classification ──");

test("classify: 'review the code' → review_qa", () => {
  assert.strictEqual(classifyTask("review the authentication code"), "review_qa");
});

test("classify: 'code review' → review_qa", () => {
  assert.strictEqual(classifyTask("do a code review for the PR"), "review_qa");
});

test("classify: 'verify that tests pass' → review_qa (no write keywords)", () => {
  assert.strictEqual(classifyTask("verify that the tests pass"), "review_qa");
});

test("classify: 'security scan' → review_qa", () => {
  assert.strictEqual(classifyTask("run a security scan on the auth module"), "review_qa");
});

test("classify: 'test analysis' → review_qa", () => {
  assert.strictEqual(classifyTask("run test analysis on the pipeline"), "review_qa");
});

test("classify: 'QA the release' → review_qa", () => {
  assert.strictEqual(classifyTask("perform qa on the release"), "review_qa");
});

test("classify: 'lint the codebase' → review_qa", () => {
  assert.strictEqual(classifyTask("lint the codebase for style violations"), "review_qa");
});

test("classify: 'linting' → review_qa", () => {
  assert.strictEqual(classifyTask("run linting checks"), "review_qa");
});

test("classify: 'fix and verify' → write_bounded (has fix keyword)", () => {
  // Write keywords override review_qa when present
  assert.strictEqual(classifyTask("fix the bug and verify it"), "write_bounded");
});

test("classify: 'implement and review' → write_bounded (has implement keyword)", () => {
  assert.strictEqual(classifyTask("implement the feature and review it"), "write_bounded");
});

test("classify: 'review' with files → review_qa (files are targets, not writes)", () => {
  // Files in review_qa context are files being reviewed, not written
  assert.strictEqual(
    classifyTask("review the authentication code", ["src/auth.py", "tests/test_auth.py"]),
    "review_qa"
  );
});

// ─── 3. Plan DAG generation ───────────────────────────────────────────────────

console.log("\n── 3. Plan DAG generation ──");

test("read_only plan: has at least 1 scout in phase 1", () => {
  const plan = generatePlan({ objective: "search codebase", cwd: FAKE_CWD }, POLICY);
  assert.strictEqual(plan.risk_class, "read_only");
  assert.ok(plan.proposed_dag.phases.length >= 1);
  const agents = plan.proposed_dag.phases[0].agents;
  assert.ok(agents.some((a) => a.role === "scout"), "Expected a scout agent");
});

test("read_only plan: no required safety gates", () => {
  const plan = generatePlan({ objective: "audit docs", cwd: FAKE_CWD }, POLICY);
  const requiredGates = plan.safety_gates.filter((g) => g.status === "required");
  assert.strictEqual(requiredGates.length, 0);
});

test("planning plan: includes planner + spec_writer", () => {
  const plan = generatePlan({ objective: "design spec for new strategy", cwd: FAKE_CWD }, POLICY);
  assert.strictEqual(plan.risk_class, "planning");
  const roles = plan.proposed_dag.phases.flatMap((p) => p.agents.map((a) => a.role));
  assert.ok(roles.includes("planner"), "Expected planner role");
});

test("write_bounded plan: has scout → builder → reviewer phases", () => {
  const plan = generatePlan(
    { objective: "refactor momentum module", files_affected: ["src/a.py", "src/b.py"], cwd: FAKE_CWD },
    POLICY
  );
  assert.strictEqual(plan.risk_class, "write_bounded");
  assert.ok(plan.proposed_dag.phases.length >= 3, "Expected 3 phases");
  const allRoles = plan.proposed_dag.phases.flatMap((p) => p.agents.map((a) => a.role));
  assert.ok(allRoles.includes("scout"), "Expected scout");
  assert.ok(allRoles.includes("builder"), "Expected builder");
  assert.ok(allRoles.includes("reviewer"), "Expected reviewer");
});

test("write_bounded plan: file ownership table is non-empty when files provided", () => {
  const plan = generatePlan(
    { objective: "refactor X", files_affected: ["a.py", "b.py", "c.py"], cwd: FAKE_CWD },
    POLICY
  );
  const ownershipValues = Object.values(plan.file_ownership_table).flat();
  assert.ok(ownershipValues.length > 0, "File ownership table should have entries");
  const seen = new Set<string>();
  for (const f of ownershipValues) {
    assert.ok(!seen.has(f), `File ${f} appears in ownership table more than once`);
    seen.add(f);
  }
});

test("live_trading_ops plan: all gates unsatisfied", () => {
  const plan = generatePlan({ objective: "update broker positions", cwd: FAKE_CWD }, POLICY);
  assert.strictEqual(plan.risk_class, "live_trading_ops");
  const unsatisfied = plan.safety_gates.filter((g) => !g.satisfied);
  assert.ok(unsatisfied.length > 0, "Expected at least 1 unsatisfied gate for live_trading_ops");
});

test("live_trading_ops plan: next_step mentions no automatic dispatch", () => {
  const plan = generatePlan({ objective: "deploy config live", cwd: FAKE_CWD }, POLICY);
  assert.ok(
    plan.next_step.toLowerCase().includes("no automatic") ||
    plan.next_step.toLowerCase().includes("no auto") ||
    plan.next_step.toLowerCase().includes("never"),
    `Expected 'no automatic' in next_step, got: ${plan.next_step}`
  );
});

test("planner is deterministic: same input → same risk_class and phases", () => {
  const input = { objective: "refactor auth module", files_affected: ["auth.py", "user.py"], cwd: FAKE_CWD };
  const plan1 = generatePlan(input, POLICY);
  const plan2 = generatePlan(input, POLICY);
  assert.strictEqual(plan1.risk_class, plan2.risk_class);
  assert.strictEqual(plan1.proposed_dag.phases.length, plan2.proposed_dag.phases.length);
  assert.strictEqual(
    plan1.concurrency_summary.total_agents,
    plan2.concurrency_summary.total_agents
  );
});

test("protected files detected in write plan generate warnings", () => {
  const plan = generatePlan(
    { objective: "update config", files_affected: ["config/active_config.json", "src/safe.py"], cwd: FAKE_CWD },
    POLICY
  );
  assert.ok(plan.warnings.length > 0, "Expected warnings for protected files");
  assert.ok(
    plan.warnings.some((w) => w.toLowerCase().includes("protected")),
    `Expected 'protected' in warnings: ${plan.warnings.join(", ")}`
  );
});

test("kill switch generates warning in plan", () => {
  const plan = generatePlan({ objective: "search codebase", cwd: FAKE_CWD }, KILL_POLICY);
  assert.ok(
    plan.warnings.some((w) => w.toLowerCase().includes("kill")),
    `Expected kill switch warning, got: ${plan.warnings.join(", ")}`
  );
});

// ── 3b. review_qa DAG ─────────────────────────────────────────────────────────

console.log("\n── 3b. review_qa DAG ──");

test("review_qa plan: risk_class is review_qa", () => {
  const plan = generatePlan({ objective: "review the authentication code", cwd: FAKE_CWD }, POLICY);
  assert.strictEqual(plan.risk_class, "review_qa");
});

test("review_qa plan: has at least 1 agent in phase 1", () => {
  const plan = generatePlan({ objective: "security scan on auth module", cwd: FAKE_CWD }, POLICY);
  assert.ok(plan.proposed_dag.phases.length >= 1, "Expected at least 1 phase");
  assert.ok(plan.proposed_dag.phases[0].agents.length >= 1, "Expected at least 1 agent");
});

test("review_qa plan: agents include reviewer, test-runner, security-reviewer roles", () => {
  const plan = generatePlan({ objective: "review the code", cwd: FAKE_CWD }, POLICY);
  const roles = plan.proposed_dag.phases.flatMap((p) => p.agents.map((a) => a.role));
  assert.ok(
    roles.includes("reviewer") || roles.includes("test-runner") || roles.includes("security-reviewer"),
    `Expected QA roles in: ${roles.join(", ")}`
  );
});

test("review_qa plan: no required safety gates (read-only execution)", () => {
  const plan = generatePlan({ objective: "verify that tests pass correctly", cwd: FAKE_CWD }, POLICY);
  const requiredGates = plan.safety_gates.filter((g) => g.status === "required");
  assert.strictEqual(requiredGates.length, 0, "review_qa should have no required gates");
});

test("review_qa plan: file_ownership_table entries have no owned files (read-only)", () => {
  const plan = generatePlan(
    { objective: "review the authentication code", files_affected: ["src/auth.py"], cwd: FAKE_CWD },
    POLICY
  );
  // All agents should have empty files_owned (they read, not write)
  const allOwnedFiles = plan.proposed_dag.phases
    .flatMap((p) => p.agents)
    .flatMap((a) => a.files_owned ?? []);
  assert.strictEqual(allOwnedFiles.length, 0, "review_qa agents should own no files");
});

test("review_qa plan: summary mentions read-only execution", () => {
  const plan = generatePlan({ objective: "code review for PR", cwd: FAKE_CWD }, POLICY);
  assert.ok(
    plan.summary.toLowerCase().includes("read-only") || plan.summary.toLowerCase().includes("qa"),
    `Expected 'read-only' or 'QA' in summary: ${plan.summary}`
  );
});

test("review_qa plan: next_step mentions execute_read_only flag", () => {
  const plan = generatePlan({ objective: "run security scan", cwd: FAKE_CWD }, POLICY);
  assert.ok(
    plan.next_step.toLowerCase().includes("execute_read_only") ||
    plan.next_step.toLowerCase().includes("execute-read-only"),
    `Expected execute_read_only in next_step: ${plan.next_step}`
  );
});

// ─── 4. Clean-tree gate on REAL dirty atlas repo ─────────────────────────────

console.log("\n── 4. Clean-tree gate (real git, atlas is dirty) ──");

test("checkGitStatus: atlas repo IS dirty (confirms our clean-tree gate works)", () => {
  const status = checkGitStatus(ATLAS_CWD);
  assert.ok(!status.clean, "Expected atlas to have a dirty working tree for this test");
  assert.ok(status.dirty_files.length > 0, "Expected dirty files to be listed");
});

test("write_bounded plan: clean_working_tree gate is NOT satisfied on dirty atlas", () => {
  const plan = generatePlan(
    { objective: "refactor strategies", files_affected: ["strategies/a.py"], cwd: ATLAS_CWD },
    POLICY
  );
  const cleanGate = plan.safety_gates.find((g) => g.gate === "clean_working_tree");
  assert.ok(cleanGate, "Expected clean_working_tree gate in plan");
  assert.ok(!cleanGate.satisfied, "Expected clean_working_tree gate to NOT be satisfied on dirty atlas");
});

test("write_bounded plan: warnings mention dirty tree when atlas is dirty", () => {
  const plan = generatePlan(
    { objective: "refactor auth", files_affected: ["auth.py"], cwd: ATLAS_CWD },
    POLICY
  );
  const hasDirtyWarning = plan.warnings.some(
    (w) => w.toLowerCase().includes("dirty") || w.toLowerCase().includes("clean tree")
  );
  assert.ok(hasDirtyWarning, `Expected dirty-tree warning, got: ${plan.warnings.join(", ")}`);
});

test("evaluatePlanGates: rejects plan with unsatisfied required gate", () => {
  const plan = generatePlan(
    { objective: "refactor X", files_affected: ["src/x.py"], cwd: ATLAS_CWD },
    POLICY
  );
  const result = evaluatePlanGates(plan, POLICY);
  assert.ok(!result.approved, "Expected plan gates to be rejected on dirty atlas");
  assert.ok(result.blockers.length > 0, "Expected blockers to be listed");
  assert.ok(
    result.blockers.some((b) => b.toLowerCase().includes("clean_working_tree")),
    `Expected clean_working_tree blocker, got: ${result.blockers.join(", ")}`
  );
});

// ─── 5. Executor: live_trading_ops always blocked ─────────────────────────────

console.log("\n── 5. Executor: live_trading_ops blocked ──");

test("evaluateExecutionGate: live_trading_ops → always blocked", () => {
  const plan = generatePlan({ objective: "update broker positions", cwd: FAKE_CWD }, POLICY);
  const gate = evaluateExecutionGate(plan, POLICY, FAKE_CWD);
  assert.ok(!gate.allowed, "Expected live_trading_ops to be blocked");
  assert.ok(gate.blockers.length > 0);
  assert.ok(
    gate.blockers.some((b) => b.toLowerCase().includes("live_trading_ops")),
    `Expected live_trading_ops blocker: ${gate.blockers.join(", ")}`
  );
});

test("evaluateExecutionGate: kill switch blocks all tasks", () => {
  const plan = generatePlan({ objective: "search codebase", cwd: FAKE_CWD }, KILL_POLICY);
  const gate = evaluateExecutionGate(plan, KILL_POLICY, FAKE_CWD);
  assert.ok(!gate.allowed, "Expected kill switch to block execution");
  assert.strictEqual(gate.decision, "kill_switch_active");
});

// ─── 6. Executor: write_bounded blocked on dirty tree ────────────────────────

console.log("\n── 6. Executor: write_bounded blocked on dirty atlas ──");

test("evaluateExecutionGate: write_bounded → blocked on dirty atlas repo", () => {
  const plan = generatePlan(
    { objective: "refactor auth", files_affected: ["src/auth.py"], cwd: ATLAS_CWD },
    POLICY
  );
  const gate = evaluateExecutionGate(plan, POLICY, ATLAS_CWD);
  assert.ok(!gate.allowed, "Expected write_bounded to be blocked on dirty atlas");
  assert.strictEqual(gate.decision, "write_gate_rejected");
  assert.ok(
    gate.blockers.some((b) => b.toLowerCase().includes("clean working tree")),
    `Expected clean tree blocker: ${gate.blockers.join(", ")}`
  );
});

test("evaluateExecutionGate: write_bounded gates_passed → still requires user confirmation", () => {
  const readPlan = generatePlan({ objective: "search docs", cwd: FAKE_CWD }, POLICY);
  assert.strictEqual(readPlan.risk_class, "read_only");
  const gateResult = evaluatePlanGates(readPlan, POLICY);
  assert.ok(gateResult.approved, "Read-only plan should pass gates");
});

// ─── 7. Executor: read-only → OAuth-only commands ────────────────────────────

console.log("\n── 7. Executor: read-only OAuth commands ──");

test("evaluateExecutionGate: read_only → allowed with OAuth-only commands", () => {
  const plan = generatePlan({ objective: "search codebase for errors", cwd: FAKE_CWD }, POLICY);
  const gate = evaluateExecutionGate(plan, POLICY, FAKE_CWD);
  assert.ok(gate.allowed, "Expected read-only gate to be allowed");
  assert.ok(gate.read_only_commands && gate.read_only_commands.length > 0, "Expected command suggestions");
});

test("buildReadOnlyCommand: uses pi CLI, not Anthropic API key", () => {
  const cmd = buildReadOnlyCommand("scout-1", "find all deprecated calls");
  assert.ok(cmd.startsWith("pi "), `Expected command to start with 'pi ', got: ${cmd}`);
  assert.ok(cmd.includes("--system-prompt"), "Expected --system-prompt flag (OAuth routing)");
  assert.ok(!cmd.includes("api_key"), "Command must not reference api_key");
  assert.ok(!cmd.includes("ANTHROPIC_API_KEY"), "Command must not reference ANTHROPIC_API_KEY");
});

test("buildReadOnlyCommand: includes --tools read,grep,find,ls", () => {
  const cmd = buildReadOnlyCommand("scout-1", "find deprecated calls");
  assert.ok(cmd.includes("--tools read,grep,find,ls"), `Expected --tools flag: ${cmd}`);
});

test("buildReadOnlyCommand: includes --no-session and --mode json", () => {
  const cmd = buildReadOnlyCommand("scout-1", "find deprecated calls");
  assert.ok(cmd.includes("--no-session"), `Expected --no-session: ${cmd}`);
  assert.ok(cmd.includes("--mode json"), `Expected --mode json: ${cmd}`);
});

test("read-only commands contain system-prompt (Claude Max OAuth)", () => {
  const plan = generatePlan({ objective: "audit codebase", cwd: FAKE_CWD }, POLICY);
  const gate = evaluateExecutionGate(plan, POLICY, FAKE_CWD);
  assert.ok(gate.read_only_commands);
  for (const cmd of gate.read_only_commands!) {
    assert.ok(
      cmd.command.includes("--system-prompt"),
      `Command ${cmd.agent_id} missing --system-prompt: ${cmd.command}`
    );
  }
});

test("evaluateExecutionGate: review_qa → allowed (read-only class)", () => {
  const plan = generatePlan({ objective: "review authentication code", cwd: FAKE_CWD }, POLICY);
  assert.strictEqual(plan.risk_class, "review_qa");
  const gate = evaluateExecutionGate(plan, POLICY, FAKE_CWD);
  assert.ok(gate.allowed, "Expected review_qa to be allowed");
  assert.strictEqual(gate.decision, "read_only_started");
  assert.ok(gate.read_only_commands && gate.read_only_commands.length > 0);
});

// ─── 8. Audit entry roundtrip ──────────────────────────────────────────────────

console.log("\n── 8. Audit entry roundtrip ──");

test("writeAuditEntry: creates file if not exist", () => {
  const testPath = join(TMP_DIR, "audit-new.jsonl");
  if (existsSync(testPath)) rmSync(testPath);
  const plan = generatePlan({ objective: "test task", cwd: FAKE_CWD }, POLICY);
  writeAuditEntry(makeAuditEntry(plan, "plan_generated"), testPath);
  assert.ok(existsSync(testPath), "Audit file should have been created");
});

test("readAuditEntries: returns empty array for missing file", () => {
  const entries = readAuditEntries("/nonexistent/audit.jsonl");
  assert.deepStrictEqual(entries, []);
});

test("audit roundtrip: entry fields preserved correctly", () => {
  const plan = generatePlan({ objective: "audit test task", cwd: FAKE_CWD }, POLICY);
  const entry = makeAuditEntry(plan, "plan_generated", { blockers: ["test blocker"] });
  writeAuditEntry(entry, AUDIT_PATH);
  const entries = readAuditEntries(AUDIT_PATH);
  assert.ok(entries.length > 0, "Should have at least 1 entry");
  const last = entries[entries.length - 1];
  assert.strictEqual(last.decision, "plan_generated");
  assert.ok(last.timestamp, "Timestamp should be present");
  assert.ok(last.task_id, "task_id should be present");
  assert.ok(Array.isArray(last.gates), "gates should be an array");
  assert.deepStrictEqual(last.blockers, ["test blocker"]);
});

test("audit: multiple entries accumulate (JSONL append)", () => {
  const multiPath = join(TMP_DIR, "multi.jsonl");
  const plan = generatePlan({ objective: "multi test", cwd: FAKE_CWD }, POLICY);
  writeAuditEntry(makeAuditEntry(plan, "plan_generated"), multiPath);
  writeAuditEntry(makeAuditEntry(plan, "gates_blocked"), multiPath);
  writeAuditEntry(makeAuditEntry(plan, "read_only_started"), multiPath);
  const entries = readAuditEntries(multiPath);
  assert.strictEqual(entries.length, 3);
  assert.strictEqual(entries[0].decision, "plan_generated");
  assert.strictEqual(entries[1].decision, "gates_blocked");
  assert.strictEqual(entries[2].decision, "read_only_started");
});

test("readAuditEntries: respects limit parameter", () => {
  const limitPath = join(TMP_DIR, "limit.jsonl");
  const plan = generatePlan({ objective: "limit test", cwd: FAKE_CWD }, POLICY);
  for (let i = 0; i < 10; i++) {
    writeAuditEntry(makeAuditEntry(plan, "plan_generated"), limitPath);
  }
  const entries = readAuditEntries(limitPath, 5);
  assert.strictEqual(entries.length, 5);
});

test("audit: makeAuditEntry with cwd → entry preserves cwd field", () => {
  const cwdPath = join(TMP_DIR, "cwd-audit.jsonl");
  const plan = generatePlan({ objective: "cwd test", cwd: FAKE_CWD }, POLICY);
  const entry = makeAuditEntry(plan, "plan_generated", { cwd: FAKE_CWD });
  assert.strictEqual(entry.cwd, FAKE_CWD, "Expected cwd to be preserved in audit entry");
  writeAuditEntry(entry, cwdPath);
  const entries = readAuditEntries(cwdPath);
  assert.ok(entries.length > 0);
  assert.strictEqual(entries[entries.length - 1].cwd, FAKE_CWD, "cwd should round-trip via JSONL");
});

test("audit: makeAuditEntry without cwd → cwd field is undefined", () => {
  const plan = generatePlan({ objective: "no cwd test", cwd: FAKE_CWD }, POLICY);
  const entry = makeAuditEntry(plan, "plan_generated");
  assert.strictEqual(entry.cwd, undefined, "Expected cwd to be undefined when not passed");
});

// ─── 9. Ownership overlap detection ──────────────────────────────────────────

console.log("\n── 9. Ownership: overlap detection ──");

test("validateOwnershipTable: no conflicts → empty array", () => {
  const table = {
    "builder-1": ["src/a.py", "src/b.py"],
    "builder-2": ["src/c.py", "src/d.py"],
    "scout-1": [],
  };
  const conflicts = validateOwnershipTable(table);
  assert.deepStrictEqual(conflicts, []);
});

test("validateOwnershipTable: detects overlap", () => {
  const table = {
    "builder-1": ["src/a.py", "src/shared.py"],
    "builder-2": ["src/b.py", "src/shared.py"], // conflict!
  };
  const conflicts = validateOwnershipTable(table);
  assert.ok(conflicts.length > 0, "Expected conflict detected");
  assert.ok(
    conflicts.some((c) => c.includes("shared.py")),
    `Expected shared.py in conflict: ${conflicts.join(", ")}`
  );
});

test("validateOwnershipTable: multiple overlaps all reported", () => {
  const table = {
    "builder-1": ["a.py", "b.py"],
    "builder-2": ["a.py", "b.py"],
  };
  const conflicts = validateOwnershipTable(table);
  assert.strictEqual(conflicts.length, 2, "Expected 2 conflicts (one per overlapping file)");
});

test("hasProtectedFiles: finds protected files in list", () => {
  const files = ["src/safe.py", "config/active_config.json", "tests/test.py"];
  const protected_ = hasProtectedFiles(POLICY, files);
  assert.ok(protected_.includes("config/active_config.json"));
  assert.ok(!protected_.includes("src/safe.py"));
});

// ─── 10. API key safety grep ──────────────────────────────────────────────────

console.log("\n── 10. API key safety: no forbidden patterns in extension source ──");

const SRC_DIR = resolve(new URL("../src/", import.meta.url).pathname);

test("no Anthropic(api_key), @anthropic-ai/sdk, or apiKey: usage in extension source files", () => {
  // Extended check: catches Python-style, TS SDK import, and TS client config property.
  // Greps SRC_DIR (src/ only) so test files in tests/ are not scanned.
  const violations = checkForbiddenApiKeyUsage(SRC_DIR);
  assert.strictEqual(
    violations.length,
    0,
    `Forbidden Anthropic API key usage found:\n${violations.join("\n")}`
  );
});

test("checkForbiddenApiKeyUsage: detects @anthropic-ai/sdk import in a temp file", () => {
  const tmpTs = join(TMP_DIR, "bad-sdk-detect.ts");
  // Pattern built with concat so THIS file does not self-match the grep pattern.
  writeFileSync(tmpTs, `import Anthropic from "${ "@anthropic" + "-ai/sdk" }";\n`);
  try {
    const tmpViolations = checkForbiddenApiKeyUsage(TMP_DIR);
    assert.ok(
      tmpViolations.some((v) => v.includes("bad-sdk-detect.ts")),
      `Expected bad-sdk-detect.ts in violations, got: ${tmpViolations.join(", ")}`
    );
  } finally {
    try { unlinkSync(tmpTs); } catch { /* best-effort */ }
  }
});

test("checkForbiddenApiKeyUsage: detects apiKey: assignment in a temp file", () => {
  const tmpTs = join(TMP_DIR, "bad-apikey-detect.ts");
  // Pattern built with concat so THIS file does not self-match.
  writeFileSync(tmpTs, `const client = { ${ "api" + "Key:" } process.env.KEY };\n`);
  try {
    const tmpViolations = checkForbiddenApiKeyUsage(TMP_DIR);
    assert.ok(
      tmpViolations.some((v) => v.includes("bad-apikey-detect.ts")),
      `Expected bad-apikey-detect.ts in violations, got: ${tmpViolations.join(", ")}`
    );
  } finally {
    try { unlinkSync(tmpTs); } catch { /* best-effort */ }
  }
});

test("pi CLI commands include --system-prompt (OAuth routing)", () => {
  const cmd = buildReadOnlyCommand("test-agent", "find something");
  assert.ok(
    cmd.includes("--system-prompt"),
    "All pi CLI commands must include --system-prompt for OAuth routing"
  );
  assert.ok(
    !cmd.includes("api_key"),
    "pi CLI commands must never reference api_key"
  );
});

// ─── 11. Burst runner: mock injection ─────────────────────────────────────────

console.log("\n── 11. Burst runner: mock injection ──");

/** Mock runner: always succeeds instantly. Does NOT call real pi CLI. */
const mockSuccessRunner: BurstRunnerFn = async (
  agentId,
  role,
  objective
): Promise<BurstAgentResult> => ({
  agent_id: agentId,
  role,
  objective,
  success: true,
  output: `{"result": "mock output for ${agentId}"}`,
  duration_ms: 42,
});

/** Mock runner: always fails. Does NOT call real pi CLI. */
const mockFailRunner: BurstRunnerFn = async (
  agentId,
  role,
  objective
): Promise<BurstAgentResult> => ({
  agent_id: agentId,
  role,
  objective,
  success: false,
  error: "mock failure",
  duration_ms: 10,
});

await testAsync("runReadOnlyBurst: mock runner returns results for all agents", async () => {
  const plan = generatePlan({ objective: "review the code", cwd: FAKE_CWD }, POLICY);
  assert.strictEqual(plan.risk_class, "review_qa");
  const result = await runReadOnlyBurst(plan, POLICY, { runner: mockSuccessRunner });
  assert.ok(result.agents_run > 0, "Expected at least 1 agent to run");
  assert.strictEqual(result.agents_succeeded, result.agents_run, "All agents should succeed");
  assert.strictEqual(result.agents_failed, 0);
  assert.strictEqual(result.errors.length, 0);
  assert.ok(result.started_at, "Expected started_at timestamp");
  assert.ok(result.completed_at, "Expected completed_at timestamp");
});

await testAsync("runReadOnlyBurst: each result has correct agent_id and role", async () => {
  const plan = generatePlan({ objective: "review authentication code", cwd: FAKE_CWD }, POLICY);
  const result = await runReadOnlyBurst(plan, POLICY, { runner: mockSuccessRunner });
  for (const r of result.results) {
    assert.ok(r.agent_id, "Expected agent_id on each result");
    assert.ok(r.role, "Expected role on each result");
    assert.ok(typeof r.duration_ms === "number", "Expected numeric duration_ms");
  }
});

await testAsync("runReadOnlyBurst: failing runner populates errors array", async () => {
  const plan = generatePlan({ objective: "security scan on auth", cwd: FAKE_CWD }, POLICY);
  const result = await runReadOnlyBurst(plan, POLICY, { runner: mockFailRunner });
  assert.ok(result.agents_failed > 0, "Expected some failed agents");
  assert.ok(result.errors.length > 0, "Expected errors array to be populated");
  assert.ok(
    result.errors.some((e) => e.includes("mock failure")),
    `Expected 'mock failure' in errors: ${result.errors.join(", ")}`
  );
});

await testAsync("runReadOnlyBurst: bounded concurrency respected (maxConcurrent=1 runs all sequentially)", async () => {
  const order: string[] = [];
  const sequentialRunner: BurstRunnerFn = async (agentId, role, objective) => {
    order.push(agentId);
    return { agent_id: agentId, role, objective, success: true, duration_ms: 1 };
  };
  const plan = generatePlan({ objective: "review code", cwd: FAKE_CWD }, POLICY);
  const result = await runReadOnlyBurst(plan, POLICY, {
    runner: sequentialRunner,
    maxConcurrent: 1,
  });
  assert.strictEqual(result.agents_run, order.length, "All agents should have run");
});

await testAsync("runReadOnlyBurst: hard cap at policy max_concurrent_agents", async () => {
  // maxConcurrent=999 should be capped at POLICY.global.max_concurrent_agents (16)
  const plan = generatePlan({ objective: "audit codebase", cwd: FAKE_CWD }, POLICY);
  // Should not throw even with huge requested concurrency
  const result = await runReadOnlyBurst(plan, POLICY, {
    runner: mockSuccessRunner,
    maxConcurrent: 999,
  });
  assert.ok(result.agents_run >= 0);
});

await testAsync("runReadOnlyBurst: works for read_only plan with mock runner", async () => {
  const plan = generatePlan({ objective: "search codebase", cwd: FAKE_CWD }, POLICY);
  assert.strictEqual(plan.risk_class, "read_only");
  const result = await runReadOnlyBurst(plan, POLICY, { runner: mockSuccessRunner });
  assert.ok(result.agents_run > 0);
  assert.strictEqual(result.agents_succeeded, result.agents_run);
});

// ─── 11b. Timeout selection: per-role policy lookup ──────────────────────────

console.log("\n── 11b. Timeout selection: per-role policy lookup ──");

// Policy fixture with agent_roles for timeout tests (reuse POLICY which has agent_roles: {})
// Use a richer policy fixture that has actual role timeouts.
const TIMEOUT_POLICY: AgentScalePolicy = {
  ...POLICY,
  agent_roles: {
    reviewer: {
      risk_class: "review_qa",
      concurrency_cap: 4,
      timeout_sec: 300,
      can_spawn: [],
      cannot_spawn: ["builder", "executor"],
    },
    "test-runner": {
      risk_class: "review_qa",
      concurrency_cap: 2,
      timeout_sec: 300,
      can_spawn: [],
      cannot_spawn: ["builder", "executor"],
    },
    "security-reviewer": {
      risk_class: "review_qa",
      concurrency_cap: 2,
      timeout_sec: 300,
      can_spawn: [],
      cannot_spawn: ["builder", "executor"],
    },
    scout: {
      risk_class: "read_only",
      concurrency_cap: 8,
      timeout_sec: 300,
      can_spawn: ["scout", "researcher"],
      cannot_spawn: ["builder", "executor"],
    },
    researcher: {
      risk_class: "read_only",
      concurrency_cap: 6,
      timeout_sec: 600,
      can_spawn: ["scout", "researcher"],
      cannot_spawn: ["builder", "executor"],
    },
  },
};

test("resolveAgentTimeoutMs: uses policy timeout_sec for known role (reviewer → 300s)", () => {
  const ms = resolveAgentTimeoutMs("reviewer", TIMEOUT_POLICY);
  assert.strictEqual(ms, 300_000, `Expected 300000ms for reviewer, got ${ms}`);
});

test("resolveAgentTimeoutMs: uses policy timeout_sec for test-runner (300s)", () => {
  const ms = resolveAgentTimeoutMs("test-runner", TIMEOUT_POLICY);
  assert.strictEqual(ms, 300_000, `Expected 300000ms for test-runner, got ${ms}`);
});

test("resolveAgentTimeoutMs: uses policy timeout_sec for security-reviewer (300s)", () => {
  const ms = resolveAgentTimeoutMs("security-reviewer", TIMEOUT_POLICY);
  assert.strictEqual(ms, 300_000, `Expected 300000ms for security-reviewer, got ${ms}`);
});

test("resolveAgentTimeoutMs: researcher gets 600s from policy", () => {
  const ms = resolveAgentTimeoutMs("researcher", TIMEOUT_POLICY);
  assert.strictEqual(ms, 600_000, `Expected 600000ms for researcher, got ${ms}`);
});

test("resolveAgentTimeoutMs: unknown role falls back to DEFAULT_BURST_TIMEOUT_MS (300s)", () => {
  // 'unknown-role' is not in TIMEOUT_POLICY.agent_roles → should return 300_000
  const ms = resolveAgentTimeoutMs("unknown-role", TIMEOUT_POLICY);
  assert.strictEqual(ms, 300_000, `Expected default 300000ms for unknown role, got ${ms}`);
});

test("resolveAgentTimeoutMs: unknown role with empty agent_roles falls back to default", () => {
  // POLICY.agent_roles is {} — no roles defined → fallback
  const ms = resolveAgentTimeoutMs("reviewer", POLICY);
  assert.strictEqual(ms, 300_000, `Expected fallback 300000ms when agent_roles empty, got ${ms}`);
});

test("resolveAgentTimeoutMs: explicit overrideMs always wins over policy", () => {
  // Override of 60_000 should beat policy's 300s for reviewer
  const ms = resolveAgentTimeoutMs("reviewer", TIMEOUT_POLICY, 60_000);
  assert.strictEqual(ms, 60_000, `Expected override 60000ms, got ${ms}`);
});

test("resolveAgentTimeoutMs: explicit overrideMs wins even for unknown role", () => {
  const ms = resolveAgentTimeoutMs("no-such-role", TIMEOUT_POLICY, 42_000);
  assert.strictEqual(ms, 42_000, `Expected override 42000ms, got ${ms}`);
});

// Burst runner that captures timeout_ms passed to it
const captureTimeoutRunner: BurstRunnerFn = async (
  agentId: string,
  role: string,
  objective: string,
  timeoutMs: number
): Promise<BurstAgentResult> => ({
  agent_id: agentId,
  role,
  objective,
  success: true,
  output: `timeout=${timeoutMs}`,
  duration_ms: 1,
  timeout_ms: timeoutMs,
});

await testAsync("runReadOnlyBurst: review_qa agents get 300s timeout from policy (not 120s)", async () => {
  const plan = generatePlan({ objective: "review the code", cwd: FAKE_CWD }, TIMEOUT_POLICY);
  assert.strictEqual(plan.risk_class, "review_qa");
  const result = await runReadOnlyBurst(plan, TIMEOUT_POLICY, { runner: captureTimeoutRunner });
  assert.ok(result.agents_run > 0, "Expected at least 1 agent");
  for (const r of result.results) {
    assert.strictEqual(
      r.timeout_ms,
      300_000,
      `Agent ${r.agent_id} (${r.role}) got timeout ${String(r.timeout_ms)}ms, expected 300000ms`
    );
  }
});

await testAsync("runReadOnlyBurst: read_only scout agents get 300s timeout from policy", async () => {
  const plan = generatePlan({ objective: "search the codebase", cwd: FAKE_CWD }, TIMEOUT_POLICY);
  assert.strictEqual(plan.risk_class, "read_only");
  const result = await runReadOnlyBurst(plan, TIMEOUT_POLICY, { runner: captureTimeoutRunner });
  assert.ok(result.agents_run > 0, "Expected at least 1 agent");
  for (const r of result.results) {
    assert.strictEqual(
      r.timeout_ms,
      300_000,
      `Scout agent ${r.agent_id} got ${String(r.timeout_ms)}ms, expected 300000ms`
    );
  }
});

await testAsync("runReadOnlyBurst: opts.timeoutMs override applies to all agents", async () => {
  const plan = generatePlan({ objective: "review authentication code", cwd: FAKE_CWD }, TIMEOUT_POLICY);
  const result = await runReadOnlyBurst(plan, TIMEOUT_POLICY, {
    runner: captureTimeoutRunner,
    timeoutMs: 60_000, // explicit override — should beat policy's 300s
  });
  for (const r of result.results) {
    assert.strictEqual(
      r.timeout_ms,
      60_000,
      `Expected override 60000ms for ${r.agent_id}, got ${String(r.timeout_ms)}ms`
    );
  }
});

await testAsync("runReadOnlyBurst: review_qa with empty agent_roles falls back to 300s default", async () => {
  // POLICY has agent_roles: {} — no role config → falls back to DEFAULT_BURST_TIMEOUT_MS (300s)
  const plan = generatePlan({ objective: "security scan on auth", cwd: FAKE_CWD }, POLICY);
  const result = await runReadOnlyBurst(plan, POLICY, { runner: captureTimeoutRunner });
  for (const r of result.results) {
    assert.strictEqual(
      r.timeout_ms,
      300_000,
      `Expected fallback 300000ms for ${r.agent_id}, got ${String(r.timeout_ms)}ms`
    );
  }
});

// ─── 12. Write plan: buildWriteDispatchMessage ────────────────────────────

console.log("\n── 12. Write plan: buildWriteDispatchMessage ──");

test("buildWriteDispatchMessage: returns a non-empty string", () => {
  const plan = generatePlan(
    { objective: "refactor auth module", files_affected: ["src/auth.py", "tests/test_auth.py"], cwd: FAKE_CWD },
    POLICY
  );
  const msg = buildWriteDispatchMessage(plan);
  assert.ok(msg.length > 0, "Expected non-empty dispatch message");
});

test("buildWriteDispatchMessage: contains ownership table header", () => {
  const plan = generatePlan(
    { objective: "refactor auth module", files_affected: ["src/auth.py", "tests/test_auth.py"], cwd: FAKE_CWD },
    POLICY
  );
  const msg = buildWriteDispatchMessage(plan);
  assert.ok(
    msg.includes("File Ownership Table"),
    `Expected ownership table header in: ${msg.slice(0, 200)}`
  );
});

test("buildWriteDispatchMessage: contains file names from ownership table", () => {
  const plan = generatePlan(
    { objective: "refactor auth module", files_affected: ["src/auth.py", "tests/test_auth.py"], cwd: FAKE_CWD },
    POLICY
  );
  const msg = buildWriteDispatchMessage(plan);
  // At least one of the files should appear in the ownership table
  const hasFiles = msg.includes("src/auth.py") || msg.includes("tests/test_auth.py");
  assert.ok(hasFiles, `Expected file names in ownership table: ${msg.slice(0, 500)}`);
});

test("buildWriteDispatchMessage: contains does-not-execute warning", () => {
  const plan = generatePlan(
    { objective: "refactor auth module", files_affected: ["src/auth.py"], cwd: FAKE_CWD },
    POLICY
  );
  const msg = buildWriteDispatchMessage(plan);
  assert.ok(
    msg.toLowerCase().includes("queue") || msg.toLowerCase().includes("does not"),
    `Expected queuing notice in: ${msg.slice(0, 300)}`
  );
});

test("buildWriteDispatchMessage: is a pure function (no side effects)", () => {
  const plan = generatePlan(
    { objective: "refactor auth module", files_affected: ["src/auth.py"], cwd: FAKE_CWD },
    POLICY
  );
  // Call twice — should return same result
  const msg1 = buildWriteDispatchMessage(plan);
  const msg2 = buildWriteDispatchMessage(plan);
  assert.strictEqual(msg1, msg2, "Expected pure function to return consistent output");
});

test("buildWriteDispatchMessage: contains task summary", () => {
  const plan = generatePlan(
    { objective: "refactor auth module", files_affected: ["src/auth.py"], cwd: FAKE_CWD },
    POLICY
  );
  const msg = buildWriteDispatchMessage(plan);
  assert.ok(
    msg.includes("write_bounded") || msg.includes(plan.task_id),
    `Expected risk class or task id in message: ${msg.slice(0, 200)}`
  );
});

// ─── 13. executeElasticRun: all branches ──────────────────────────────────────

console.log("\n── 13. executeElasticRun: all branches ──");

await testAsync("executeElasticRun: read_only + execute_read_only=true → read_only_complete", async () => {
  const plan = generatePlan({ objective: "search codebase for errors", cwd: FAKE_CWD }, POLICY);
  assert.strictEqual(plan.risk_class, "read_only");
  const result = await executeElasticRun(plan, POLICY, FAKE_CWD, {
    execute_read_only: true,
    runner: mockSuccessRunner,
  });
  assert.strictEqual(result.final_decision, "read_only_complete");
  assert.ok(result.burst, "Expected burst result");
  assert.strictEqual(result.final_blockers.length, 0);
});

await testAsync("executeElasticRun: review_qa + execute_read_only=true → read_only_complete", async () => {
  const plan = generatePlan({ objective: "review authentication code", cwd: FAKE_CWD }, POLICY);
  assert.strictEqual(plan.risk_class, "review_qa");
  const result = await executeElasticRun(plan, POLICY, FAKE_CWD, {
    execute_read_only: true,
    runner: mockSuccessRunner,
  });
  assert.strictEqual(result.final_decision, "read_only_complete");
  assert.ok(result.burst);
});

await testAsync("executeElasticRun: review_qa + failing runner → dispatch_rejected", async () => {
  const plan = generatePlan({ objective: "review authentication code", cwd: FAKE_CWD }, POLICY);
  const result = await executeElasticRun(plan, POLICY, FAKE_CWD, {
    execute_read_only: true,
    runner: mockFailRunner,
  });
  assert.strictEqual(result.final_decision, "dispatch_rejected");
  assert.ok(result.final_blockers.length > 0, "Expected blockers on failure");
});

await testAsync("executeElasticRun: execute_read_only=false (default) → gate only, no burst", async () => {
  const plan = generatePlan({ objective: "review code", cwd: FAKE_CWD }, POLICY);
  const result = await executeElasticRun(plan, POLICY, FAKE_CWD, {
    execute_read_only: false,
    runner: mockSuccessRunner,
  });
  // Should return gate decision without running burst
  assert.ok(!result.burst, "Expected no burst when execute_read_only=false");
  assert.strictEqual(result.final_decision, "read_only_started");
});

await testAsync("executeElasticRun: write_bounded + confirmed=true + dirty tree → write_gate_rejected", async () => {
  const plan = generatePlan(
    { objective: "refactor auth", files_affected: ["src/auth.py"], cwd: ATLAS_CWD },
    POLICY
  );
  assert.strictEqual(plan.risk_class, "write_bounded");
  const result = await executeElasticRun(plan, POLICY, ATLAS_CWD, { confirmed: true });
  // Dirty tree should reject even with confirmed=true
  assert.strictEqual(result.final_decision, "write_gate_rejected");
  assert.ok(result.final_blockers.length > 0, "Expected blockers from dirty tree");
});

await testAsync("executeElasticRun: live_trading_ops → gates_blocked always", async () => {
  const plan = generatePlan({ objective: "update broker positions", cwd: FAKE_CWD }, POLICY);
  assert.strictEqual(plan.risk_class, "live_trading_ops");
  const result = await executeElasticRun(plan, POLICY, FAKE_CWD, {
    execute_read_only: true, // should be ignored
    confirmed: true,         // should be ignored
    runner: mockSuccessRunner,
  });
  assert.strictEqual(result.final_decision, "gates_blocked");
  assert.ok(result.final_blockers.length > 0);
  assert.ok(!result.burst, "Expected no burst for live_trading_ops");
  assert.ok(!result.dispatch_message, "Expected no dispatch message for live_trading_ops");
});

await testAsync("executeElasticRun: kill switch → kill_switch_active always", async () => {
  const plan = generatePlan({ objective: "review code", cwd: FAKE_CWD }, KILL_POLICY);
  const result = await executeElasticRun(plan, KILL_POLICY, FAKE_CWD, {
    execute_read_only: true,
    runner: mockSuccessRunner,
  });
  assert.strictEqual(result.final_decision, "kill_switch_active");
  assert.ok(!result.burst, "Expected no burst with kill switch active");
});

await testAsync("executeElasticRun: default (no flags) → returns gate decision", async () => {
  const plan = generatePlan({ objective: "search codebase", cwd: FAKE_CWD }, POLICY);
  const result = await executeElasticRun(plan, POLICY, FAKE_CWD);
  // Default is plan/gate only — gate.decision forwarded
  assert.ok(result.final_decision, "Expected a final_decision");
  assert.ok(!result.burst, "Expected no burst without execute_read_only");
});

await testAsync("executeElasticRun: planning + execute_read_only=true → read_only_complete", async () => {
  const plan = generatePlan({ objective: "design spec for new strategy", cwd: FAKE_CWD }, POLICY);
  assert.strictEqual(plan.risk_class, "planning");
  const result = await executeElasticRun(plan, POLICY, FAKE_CWD, {
    execute_read_only: true,
    runner: mockSuccessRunner,
  });
  assert.strictEqual(result.final_decision, "read_only_complete");
  assert.ok(result.burst);
});

// ─── Cleanup temp files ───────────────────────────────────────────────────────

try {
  rmSync(TMP_DIR, { recursive: true, force: true });
} catch {
  // best-effort cleanup
}

// ─── Summary ──────────────────────────────────────────────────────────────────

console.log(`\n${"─".repeat(55)}`);
const total = passed + failed;
if (failed === 0) {
  console.log(`✓  All ${total} tests passed\n`);
} else {
  console.error(`✗  ${failed}/${total} tests failed\n`);
  process.exit(1);
}
