/**
 * atlas-jobs — Runtime Verification Script
 *
 * Loads the default-exported extension factory from src/index.ts via the
 * same import path that Pi's jiti loader uses, registers tools against a
 * stub ExtensionAPI, then invokes `atlas_jobs_run` in dry-run mode to
 * assert that buildCliInvocation correctly drops `--days` for
 * subcommands that don't declare it (notably `cli_backtest`).
 *
 * This is the gold-standard regression test for the #365 / T7 bug:
 *   `atlas_jobs_run({job:"cli_backtest", args:{market:"sp500", days:"252"}, dryRun:true})`
 *   must resolve to `python3 scripts/cli.py -m sp500 backtest`
 *   (NOT `... backtest --days 252` — scripts/cli.py's backtest subparser
 *   has no --days option and would crash with `unrecognized arguments`).
 *
 * Run:
 *   # From atlas-ops dir (after `npm install` or with @sinclair/typebox
 *   # symlinked into ./node_modules):
 *   npm run verify-atlas-jobs
 *   # or directly:
 *   npx tsx pi-package/atlas-ops/extensions/atlas-jobs/tests/verify.ts
 *
 * Prerequisite: `@sinclair/typebox` must be resolvable from the
 * atlas-jobs/src directory. In the Pi runtime jiti provides it via the
 * alias map (see core/extensions/loader.js VIRTUAL_MODULES). For local
 * verification install it under pi-package/atlas-ops/node_modules — or
 * symlink the pi-mono workspace copy:
 *   ln -s /root/pi-mono/node_modules/typebox \\
 *         pi-package/atlas-ops/node_modules/@sinclair/typebox
 */

import { strict as assert } from "node:assert";
import factory from "../src/index";

interface RecordedTool {
  name: string;
  execute: (toolCallId: string, params: unknown) => Promise<{
    details?: { command?: string; status?: string };
  }>;
}

const recordedTools: RecordedTool[] = [];
const stubApi: any = {
  registerTool: (spec: RecordedTool) => recordedTools.push(spec),
  registerHandler: () => {},
  registerCommand: () => {},
  registerFlag: () => {},
  registerShortcut: () => {},
  registerMessageRenderer: () => {},
  log: () => {},
  events: { emit: () => {}, on: () => () => {} },
  runtime: {},
  cwd: "/root/atlas",
};

async function main(): Promise<void> {
  await (factory as any)(stubApi);

  const runTool = recordedTools.find((t) => t.name === "atlas_jobs_run");
  assert(runTool, "atlas_jobs_run tool must be registered by the factory");

  // === T7 — cli_backtest must not forward --days ===
  const backtest = await runTool.execute("verify-cli_backtest-days", {
    job: "cli_backtest",
    args: { market: "sp500", days: "252" },
    cwd: "/root/atlas",
    dryRun: true,
  });
  const backtestCmd = backtest.details?.command ?? "";
  console.log("cli_backtest →", backtestCmd);
  assert.equal(
    backtest.details?.status,
    "succeeded",
    `dry-run must succeed (got: ${JSON.stringify(backtest.details)})`,
  );
  assert.match(backtestCmd, /\bbacktest\b/, "subcommand `backtest` must be present");
  assert.match(backtestCmd, / -m sp500 /, "global -m flag must precede subcommand");
  assert.doesNotMatch(
    backtestCmd,
    /--days/,
    `cli_backtest must not emit --days (scripts/cli.py's backtest subparser ` +
      `rejects it). Got: ${backtestCmd}`,
  );

  // === Positive control — cli_ledger SHOULD forward --days ===
  const ledger = await runTool.execute("verify-cli_ledger-days", {
    job: "cli_ledger",
    args: { market: "sp500", days: "30" },
    cwd: "/root/atlas",
    dryRun: true,
  });
  const ledgerCmd = ledger.details?.command ?? "";
  console.log("cli_ledger  →", ledgerCmd);
  assert.match(
    ledgerCmd,
    /\bledger\b.*--days 30\b/,
    `cli_ledger must forward --days 30. Got: ${ledgerCmd}`,
  );

  // === Negative control — unknown arg on a subcommand without consumeArg
  // should be rejected by assertNoExtraArgs ===
  const unknown = await runTool.execute("verify-unknown-arg", {
    job: "cli_backtest",
    args: { market: "sp500", bogusFlag: "1" },
    cwd: "/root/atlas",
    dryRun: true,
  });
  // Note: dry-run path captures the error in details.error if buildCommand throws.
  assert(
    unknown.details?.status === "failed" ||
      (unknown.details as any)?.error?.includes?.("Unsupported"),
    `unknown args must be rejected. Got: ${JSON.stringify(unknown.details)}`,
  );

  console.log("\n✓ atlas-jobs verify: cli_backtest strips --days, cli_ledger forwards it");
}

main().catch((err) => {
  console.error("✗ atlas-jobs verify failed:", err);
  process.exit(1);
});
