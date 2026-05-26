import type { ExtensionAPI } from "@mariozechner/pi-coding-agent";
import { Type } from "@sinclair/typebox";
import {
  appendFileSync,
  existsSync,
  mkdirSync,
  readFileSync,
  readdirSync,
  rmSync,
  writeFileSync
} from "node:fs";
import { spawn, type ChildProcess } from "node:child_process";
import { join, resolve } from "node:path";
import { ATLAS_JOB_CATALOG } from "./catalog";
import {
  AtlasJobCancelSchema,
  AtlasJobGetSchema,
  AtlasJobListRunsSchema,
  AtlasJobRunRequestSchema
} from "./schemas";
import type { AtlasJobName, AtlasJobRunRecord, AtlasJobSpec } from "./types";

function nowIso(): string {
  return new Date().toISOString();
}

function makeRunId(job: string): string {
  const safe = job.replace(/[^a-z0-9_]+/gi, "_");
  return `${safe}_${Date.now()}`;
}

const MAX_STD_TAIL_CHARS = 12_000;
const RUNS_DIR_PARTS = [".pi", "atlas-runs"];
const LOGS_DIR_NAME = "logs";
const MANIFEST_EXT = ".json";

const HEAVY_LOCK_JOBS = new Set<AtlasJobName>([
  "reoptimize_full_universe",
  "validate_oos",
  "auto_reoptimize"
]);

type PrimitiveArg = string | number | boolean;
type RunArgs = Record<string, PrimitiveArg>;

interface ActiveRun {
  child: ChildProcess;
  timer?: ReturnType<typeof setTimeout>;
  cancelRequested?: boolean;
  cancelReason?: string;
  timedOut?: boolean;
  finalized?: boolean;
}

interface RunPaths {
  runsDir: string;
  logsDir: string;
  locksDir: string;
  manifestPath: string;
  stdoutLogPath: string;
  stderrLogPath: string;
}

interface LockInfo {
  lockKey: string;
  runId: string;
  job: AtlasJobName;
  createdAt: string;
}

function ensureDir(path: string): void {
  mkdirSync(path, { recursive: true });
}

function getRunsBaseDir(cwd: string): string {
  return resolve(cwd, ...RUNS_DIR_PARTS);
}

function buildRunPaths(cwd: string, runId: string): RunPaths {
  const runsDir = getRunsBaseDir(cwd);
  const logsDir = join(runsDir, LOGS_DIR_NAME);
  const locksDir = join(runsDir, "locks");
  ensureDir(runsDir);
  ensureDir(logsDir);
  ensureDir(locksDir);
  return {
    runsDir,
    logsDir,
    locksDir,
    manifestPath: join(runsDir, `${runId}${MANIFEST_EXT}`),
    stdoutLogPath: join(logsDir, `${runId}.stdout.log`),
    stderrLogPath: join(logsDir, `${runId}.stderr.log`)
  };
}

function writeRunRecord(record: AtlasJobRunRecord): void {
  if (!record.manifestPath) return;
  const copy = { ...record };
  writeFileSync(record.manifestPath, `${JSON.stringify(copy, null, 2)}\n`, "utf8");
}

function readJsonFile<T>(path: string): T | null {
  try {
    if (!existsSync(path)) return null;
    const raw = readFileSync(path, "utf8");
    return JSON.parse(raw) as T;
  } catch {
    return null;
  }
}

function readRunRecord(path: string): AtlasJobRunRecord | null {
  return readJsonFile<AtlasJobRunRecord>(path);
}

function trimTail(existing: string | undefined, chunk: string, limit: number): string {
  const combined = `${existing ?? ""}${chunk}`;
  return combined.length <= limit ? combined : combined.slice(-limit);
}

function quoteArg(value: string): string {
  if (!/[ \t"]/u.test(value)) return value;
  return `"${value.replace(/"/g, '\\"')}"`;
}

function commandString(cmd: string, args: string[]): string {
  return [cmd, ...args].map(quoteArg).join(" ");
}

function pythonExecutable(): string {
  const envBin = process.env.ATLAS_PYTHON_BIN || process.env.PI_ATLAS_PYTHON_BIN;
  return envBin && envBin.trim() ? envBin.trim() : "python3";
}

function asArgsObject(value: unknown): RunArgs {
  if (!value || typeof value !== "object" || Array.isArray(value)) return {};
  const out: RunArgs = {};
  for (const [k, v] of Object.entries(value as Record<string, unknown>)) {
    if (typeof v === "string" || typeof v === "number" || typeof v === "boolean") {
      out[k] = v;
    }
  }
  return out;
}

function consumeArg<T extends PrimitiveArg>(
  args: RunArgs,
  key: string
): T | undefined {
  if (!(key in args)) return undefined;
  const value = args[key] as T;
  delete args[key];
  return value;
}

function assertNoExtraArgs(job: AtlasJobName, args: RunArgs): void {
  const extra = Object.keys(args);
  if (extra.length > 0) {
    throw new Error(`Unsupported args for ${job}: ${extra.join(", ")}`);
  }
}

function buildPythonScriptInvocation(scriptPath: string, scriptArgs: string[]) {
  const cmd = pythonExecutable();
  const args = [scriptPath, ...scriptArgs];
  return { cmd, args, command: commandString(cmd, args) };
}

function buildCliInvocation(subcommand: string, params?: RunArgs, extraFlags?: string[]) {
  const args = { ...(params ?? {}) };
  // -m/--market is a GLOBAL flag in scripts/cli.py argparse (defined on top-level
  // parser BEFORE add_subparsers), so it MUST come BEFORE the subcommand.
  // Subcommand-specific flags (--date, --days) come AFTER.
  const cliArgs = ["scripts/cli.py"];
  const market = consumeArg<string>(args, "market");
  if (market !== undefined) {
    cliArgs.push("-m", String(market));
  }
  cliArgs.push(subcommand);
  const date = consumeArg<string>(args, "date");
  if (date !== undefined) {
    cliArgs.push("--date", String(date));
  }
  const days = consumeArg<number | string>(args, "days");
  if (days !== undefined) {
    cliArgs.push("--days", String(days));
  }
  if (extraFlags) {
    cliArgs.push(...extraFlags);
  }
  assertNoExtraArgs(subcommand as AtlasJobName, args);
  return buildPythonScriptInvocation(cliArgs[0], cliArgs.slice(1));
}

function resolveJobCommand(job: AtlasJobName, rawArgs?: RunArgs) {
  const args = { ...(rawArgs ?? {}) };
  switch (job) {
    case "health_check": {
      const configPath = consumeArg<string>(args, "configPath");
      const reportPath = consumeArg<string>(args, "reportPath");
      const months = consumeArg<number | string>(args, "months");
      assertNoExtraArgs(job, args);
      const scriptArgs = ["scripts/health_check.py"];
      if (configPath) scriptArgs.push("--config-path", String(configPath));
      if (reportPath) scriptArgs.push("--report-path", String(reportPath));
      if (months !== undefined) scriptArgs.push("--months", String(months));
      return buildPythonScriptInvocation(scriptArgs[0], scriptArgs.slice(1));
    }
    case "reoptimize_full_universe": {
      const candidatePath = consumeArg<string>(args, "candidatePath");
      const resultsPath = consumeArg<string>(args, "resultsPath");
      const backupPath = consumeArg<string>(args, "backupPath");
      const promoteActiveRaw = consumeArg<boolean | string>(args, "promoteActive");
      const promoteActive =
        promoteActiveRaw === true ||
        (typeof promoteActiveRaw === "string" && promoteActiveRaw.toLowerCase() === "true");
      assertNoExtraArgs(job, args);
      const scriptArgs = ["scripts/reoptimize_full_universe.py"];
      if (candidatePath) scriptArgs.push("--candidate-path", String(candidatePath));
      if (resultsPath) scriptArgs.push("--results-path", String(resultsPath));
      if (backupPath) scriptArgs.push("--backup-path", String(backupPath));
      if (promoteActive === true) scriptArgs.push("--promote-active");
      return buildPythonScriptInvocation(scriptArgs[0], scriptArgs.slice(1));
    }
    case "validate_oos": {
      const configPath = consumeArg<string>(args, "configPath");
      const outputPath = consumeArg<string>(args, "outputPath");
      assertNoExtraArgs(job, args);
      const scriptArgs = ["scripts/validate_oos.py"];
      if (configPath) scriptArgs.push("--config-path", String(configPath));
      if (outputPath) scriptArgs.push("--output-path", String(outputPath));
      return buildPythonScriptInvocation(scriptArgs[0], scriptArgs.slice(1));
    }
    case "auto_reoptimize":
      assertNoExtraArgs(job, args);
      return buildPythonScriptInvocation("scripts/auto_reoptimize.py", []);
    case "daily_automation": {
      const step = consumeArg<string>(args, "step");
      assertNoExtraArgs(job, args);
      const scriptArgs = ["scripts/daily_automation.py"];
      if (step) {
        scriptArgs.push("--step", String(step));
      }
      return buildPythonScriptInvocation(scriptArgs[0], scriptArgs.slice(1));
    }
    case "dashboard_generate_data":
      // RETIRED 2026-05-18 — dashboard data is now served directly from FastAPI
      // (services/api/dashboard.py with 30 s in-process cache).  The legacy
      // dashboard/generate_data.py script no longer exists.  Return a no-op
      // that exits 0 so callers (e.g. postclose cron) do not exit 2.
      assertNoExtraArgs(job, args);
      return buildPythonScriptInvocation("-c", [
        `import sys; print("[dashboard_generate_data] retired 2026-05-18 — dashboard served from FastAPI (services/api/dashboard.py); no-op"); sys.exit(0)`
      ]);
    case "cli_ingest":
      return buildCliInvocation("ingest", args);
    case "cli_universe":
      return buildCliInvocation("universe", args);
    case "cli_backtest":
      return buildCliInvocation("backtest", args);
    case "cli_plan":
      return buildCliInvocation("plan", args);
    case "cli_approve":
      return buildCliInvocation("approve", args);
    case "cli_paper_run":
      return buildCliInvocation("live-run", args, ["--auto"]);
    case "cli_status":
      return buildCliInvocation("status", args);
    case "cli_ledger":
      return buildCliInvocation("ledger", args);
    case "cli_eod_settlement": {
      const market = consumeArg<string>(args, "market");
      const dryRun = consumeArg<boolean | string>(args, "dryRun");
      assertNoExtraArgs(job, args);
      const scriptArgs = ["scripts/eod_settlement.py"];
      if (market) scriptArgs.push("-m", String(market));
      if (dryRun === true || (typeof dryRun === "string" && dryRun.toLowerCase() === "true")) {
        scriptArgs.push("--dry-run");
      }
      return buildPythonScriptInvocation(scriptArgs[0], scriptArgs.slice(1));
    }
    case "anneal_review":
      return buildCliInvocation("review", args);
    default: {
      const exhaustive: never = job;
      throw new Error(`Unsupported job: ${exhaustive}`);
    }
  }
}

function terminalStatus(status: AtlasJobRunRecord["status"]): boolean {
  return status === "succeeded" || status === "failed" || status === "canceled";
}

function lockKeyForJob(job: AtlasJobName): string | undefined {
  if (HEAVY_LOCK_JOBS.has(job)) return "heavy-backtest";
  return undefined;
}

function lockFilePath(cwd: string, lockKey: string): string {
  const runsDir = getRunsBaseDir(cwd);
  ensureDir(join(runsDir, "locks"));
  return join(runsDir, "locks", `${lockKey}.lock.json`);
}

function readLock(path: string): LockInfo | null {
  return readJsonFile<LockInfo>(path);
}

function writeLock(path: string, info: LockInfo): void {
  writeFileSync(path, `${JSON.stringify(info, null, 2)}\n`, "utf8");
}

function releaseLockForRecord(record: AtlasJobRunRecord): void {
  if (!record.lockFilePath || !record.lockKey) return;
  const current = readLock(record.lockFilePath);
  if (current && current.runId === record.runId) {
    try {
      rmSync(record.lockFilePath);
    } catch {
      // Ignore lock cleanup failures.
    }
  }
}

function acquireLock(
  cwd: string,
  job: AtlasJobName,
  runId: string
): { ok: true; lockKey: string; lockFilePath: string } | { ok: false; message: string } {
  const lockKey = lockKeyForJob(job);
  if (!lockKey) {
    return {
      ok: true,
      lockKey: "",
      lockFilePath: ""
    };
  }

  const path = lockFilePath(cwd, lockKey);
  const existing = readLock(path);
  if (existing && existing.runId !== runId) {
    const existingManifest = readRunRecord(
      join(getRunsBaseDir(cwd), `${existing.runId}${MANIFEST_EXT}`)
    );
    if (existingManifest && !terminalStatus(existingManifest.status)) {
      return {
        ok: false,
        message:
          `Lock '${lockKey}' is held by ${existing.job} (${existing.runId}) ` +
          `with status ${existingManifest.status}.`
      };
    }
    try {
      rmSync(path);
    } catch {
      // ignore stale lock cleanup failure; write may fail and surface error
    }
  }

  writeLock(path, {
    lockKey,
    runId,
    job,
    createdAt: nowIso()
  });

  return { ok: true, lockKey, lockFilePath: path };
}

function loadRunsFromDisk(cwd: string): AtlasJobRunRecord[] {
  const runsDir = getRunsBaseDir(cwd);
  if (!existsSync(runsDir)) return [];
  let files: string[] = [];
  try {
    files = readdirSync(runsDir);
  } catch {
    return [];
  }
  const out: AtlasJobRunRecord[] = [];
  for (const file of files) {
    if (!file.endsWith(MANIFEST_EXT)) continue;
    const rec = readRunRecord(join(runsDir, file));
    if (rec) out.push(rec);
  }
  return out;
}

function summarizeRecordText(record: AtlasJobRunRecord): string {
  const parts = [`${record.runId}`, `${record.job}`, `${record.status}`];
  if (record.exitCode !== undefined) parts.push(`exit=${record.exitCode}`);
  if (record.pid !== undefined) parts.push(`pid=${record.pid}`);
  return parts.join(" | ");
}

function killProcessTree(child: ChildProcess): Promise<void> {
  const pid = child.pid;
  if (!pid) return Promise.resolve();
  if (process.platform === "win32") {
    return new Promise((resolvePromise) => {
      const killer = spawn("taskkill", ["/PID", String(pid), "/T", "/F"], {
        windowsHide: true
      });
      killer.once("exit", () => resolvePromise());
      killer.once("error", () => {
        try {
          child.kill();
        } catch {
          // ignore
        }
        resolvePromise();
      });
    });
  }
  return new Promise((resolvePromise) => {
    try {
      child.kill("SIGTERM");
    } catch {
      // ignore
    }
    resolvePromise();
  });
}

export default function atlasJobsExtension(pi: ExtensionAPI) {
  const runStore = new Map<string, AtlasJobRunRecord>();
  const activeRuns = new Map<string, ActiveRun>();

  function upsertRecord(record: AtlasJobRunRecord): void {
    runStore.set(record.runId, record);
    writeRunRecord(record);
  }

  function finalizeRun(
    runId: string,
    patch: Partial<AtlasJobRunRecord>
  ): AtlasJobRunRecord | null {
    const current = runStore.get(runId) ?? null;
    if (!current) return null;
    if (terminalStatus(current.status)) return current;
    const active = activeRuns.get(runId);
    if (active?.finalized) return current;

    const next: AtlasJobRunRecord = {
      ...current,
      ...patch,
      finishedAt: patch.finishedAt ?? current.finishedAt ?? nowIso()
    };
    upsertRecord(next);

    if (active?.timer) {
      clearTimeout(active.timer);
    }
    if (active) {
      active.finalized = true;
    }
    activeRuns.delete(runId);
    releaseLockForRecord(next);
    return next;
  }

  function startJobProcess(
    spec: AtlasJobSpec,
    record: AtlasJobRunRecord,
    cmd: string,
    args: string[]
  ): AtlasJobRunRecord {
    const child = spawn(cmd, args, {
      cwd: record.cwd,
      windowsHide: true
    });

    const active: ActiveRun = { child };
    activeRuns.set(record.runId, active);

    record.status = "running";
    record.startedAt = nowIso();
    if (child.pid) {
      record.pid = child.pid;
    }
    upsertRecord(record);

    child.stdout?.on("data", (chunk: Buffer | string) => {
      const text = Buffer.isBuffer(chunk) ? chunk.toString("utf8") : String(chunk);
      if (record.stdoutLogPath) {
        try {
          appendFileSync(record.stdoutLogPath, text, "utf8");
        } catch {
          // ignore log append failures; manifest still updates
        }
      }
      record.stdoutTail = trimTail(record.stdoutTail, text, MAX_STD_TAIL_CHARS);
      upsertRecord(record);
    });

    child.stderr?.on("data", (chunk: Buffer | string) => {
      const text = Buffer.isBuffer(chunk) ? chunk.toString("utf8") : String(chunk);
      if (record.stderrLogPath) {
        try {
          appendFileSync(record.stderrLogPath, text, "utf8");
        } catch {
          // ignore log append failures
        }
      }
      record.stderrTail = trimTail(record.stderrTail, text, MAX_STD_TAIL_CHARS);
      upsertRecord(record);
    });

    child.once("error", (error) => {
      const a = activeRuns.get(record.runId);
      const canceled = a?.cancelRequested;
      const timedOut = a?.timedOut;
      finalizeRun(record.runId, {
        status: canceled ? "canceled" : "failed",
        error:
          canceled
            ? `Canceled before spawn completed${a?.cancelReason ? `: ${a.cancelReason}` : ""}`
            : timedOut
              ? `Timed out: ${error.message}`
              : error.message
      });
    });

    child.once("exit", (code, signal) => {
      const a = activeRuns.get(record.runId);
      const canceled = !!a?.cancelRequested;
      const timedOut = !!a?.timedOut;

      let status: AtlasJobRunRecord["status"];
      if (canceled) status = "canceled";
      else if (code === 0) status = "succeeded";
      else status = "failed";

      const error =
        canceled
          ? `Canceled${a?.cancelReason ? `: ${a.cancelReason}` : ""}`
          : timedOut
            ? `Timed out after ${record.timeoutSec ?? "unknown"}s`
            : code === 0
              ? undefined
              : `Process exited with code ${code ?? "null"}${signal ? ` (signal: ${signal})` : ""}`;

      finalizeRun(record.runId, {
        status,
        exitCode: code ?? undefined,
        signal,
        error
      });
    });

    const timeoutSec =
      record.timeoutSec ?? spec.estimatedRuntimeSec ?? 3600;
    active.timer = setTimeout(() => {
      const a = activeRuns.get(record.runId);
      if (!a || a.finalized) return;
      a.timedOut = true;
      a.cancelRequested = true;
      a.cancelReason = `timeout ${timeoutSec}s`;
      void killProcessTree(a.child);
    }, Math.max(1, timeoutSec) * 1000);

    return record;
  }

  pi.registerTool({
    name: "atlas_jobs_list_catalog",
    label: "Atlas Jobs Catalog",
    description:
      "List Atlas job definitions, expected artifacts, and risk hints used by other Pi workflows.",
    parameters: Type.Object({}),
    async execute() {
      return {
        content: [
          {
            type: "text",
            text: `Atlas jobs catalog (${ATLAS_JOB_CATALOG.length} jobs) loaded.`
          }
        ],
        details: {
          jobs: ATLAS_JOB_CATALOG
        }
      };
    }
  });

  pi.registerTool({
    name: "atlas_jobs_run",
    label: "Atlas Run Job",
    description:
      "Start an Atlas job by logical name (health check, reoptimize, validate, CLI commands). Writes a persistent run manifest under .pi/atlas-runs and returns immediately after spawn.",
    parameters: AtlasJobRunRequestSchema,
    async execute(_toolCallId, params) {
      const spec = ATLAS_JOB_CATALOG.find((item) => item.name === params.job);
      if (!spec) {
        return {
          content: [
            {
              type: "text",
              text: `Unknown Atlas job: ${String(params.job)}`
            }
          ],
          details: { ok: false, error: "unknown_job", job: params.job }
        };
      }

      const cwd = resolve(params.cwd ?? process.cwd());
      const argsObj = asArgsObject(params.args);
      let invocation;
      try {
        invocation = resolveJobCommand(params.job, argsObj);
      } catch (error) {
        return {
          content: [
            {
              type: "text",
              text: `Failed to build command for ${params.job}: ${(error as Error).message}`
            }
          ],
          details: {
            ok: false,
            error: (error as Error).message,
            job: params.job
          }
        };
      }

      const runId = makeRunId(params.job);
      const paths = buildRunPaths(cwd, runId);

      const lockResult = acquireLock(cwd, params.job, runId);
      if (!lockResult.ok) {
        const blockedRecord: AtlasJobRunRecord = {
          runId,
          job: params.job,
          status: "failed",
          requestedAt: nowIso(),
          finishedAt: nowIso(),
          cwd,
          args: Object.keys(argsObj).length > 0 ? argsObj : undefined,
          dryRun: !!params.dryRun,
          command: invocation.command,
          error: lockResult.message,
          artifacts: spec.artifacts,
          manifestPath: paths.manifestPath,
          stdoutLogPath: paths.stdoutLogPath,
          stderrLogPath: paths.stderrLogPath
        };
        upsertRecord(blockedRecord);
        return {
          content: [
            {
              type: "text",
              text: `Blocked by lock: ${lockResult.message}`
            }
          ],
          details: blockedRecord
        };
      }

      const lockKey = lockResult.lockKey || undefined;
      const lockFile = lockResult.lockFilePath || undefined;
      const record: AtlasJobRunRecord = {
        runId,
        job: params.job,
        status: params.dryRun ? "succeeded" : "queued",
        requestedAt: nowIso(),
        cwd,
        args: Object.keys(argsObj).length > 0 ? argsObj : undefined,
        dryRun: !!params.dryRun,
        idempotencyKey: params.idempotencyKey,
        timeoutSec: params.timeoutSec ?? spec.estimatedRuntimeSec ?? 3600,
        command: invocation.command,
        artifacts: spec.artifacts,
        manifestPath: paths.manifestPath,
        stdoutLogPath: paths.stdoutLogPath,
        stderrLogPath: paths.stderrLogPath,
        lockKey,
        lockFilePath: lockFile
      };

      upsertRecord(record);

      if (params.dryRun) {
        record.startedAt = nowIso();
        record.finishedAt = record.startedAt;
        record.exitCode = 0;
        upsertRecord(record);
        releaseLockForRecord(record);
        return {
          content: [
            {
              type: "text",
              text: `Dry run prepared ${record.runId}: ${record.command}`
            }
          ],
          details: record
        };
      }

      try {
        startJobProcess(spec, record, invocation.cmd, invocation.args);
      } catch (error) {
        const failed = finalizeRun(record.runId, {
          status: "failed",
          error: `Spawn failed: ${(error as Error).message}`
        }) ?? record;
        return {
          content: [
            {
              type: "text",
              text: `Failed to start ${failed.job}: ${failed.error}`
            }
          ],
          details: failed
        };
      }

      return {
        content: [
          {
            type: "text",
            text: `Started ${summarizeRecordText(record)}.`
          }
        ],
        details: record
      };
    }
  });

  pi.registerTool({
    name: "atlas_jobs_get",
    label: "Atlas Get Run",
    description:
      "Fetch a previously created Atlas job run record by run ID from memory or .pi/atlas-runs manifest files.",
    parameters: AtlasJobGetSchema,
    async execute(_toolCallId, params) {
      let record = runStore.get(params.runId);
      if (!record) {
        const fromDisk = readRunRecord(
          join(getRunsBaseDir(process.cwd()), `${params.runId}${MANIFEST_EXT}`)
        );
        if (fromDisk) {
          runStore.set(fromDisk.runId, fromDisk);
          record = fromDisk;
        }
      }
      if (!record) {
        return {
          content: [
            {
              type: "text",
              text: `Run ${params.runId} not found.`
            }
          ],
          details: {
            found: false,
            runId: params.runId
          }
        };
      }

      const details = { ...record };
      if (!params.includeStdoutTail) {
        delete details.stdoutTail;
      }
      if (!params.includeStderrTail) {
        delete details.stderrTail;
      }

      return {
        content: [
          {
            type: "text",
            text: `Run ${params.runId}: ${record.status}`
          }
        ],
        details
      };
    }
  });

  pi.registerTool({
    name: "atlas_jobs_list_runs",
    label: "Atlas List Runs",
    description:
      "List recent Atlas job runs from .pi/atlas-runs manifests, optionally filtered by job or status.",
    parameters: AtlasJobListRunsSchema,
    async execute(_toolCallId, params) {
      const limit = params.limit ?? 20;
      const cwd = process.cwd();
      const diskRuns = loadRunsFromDisk(cwd);
      for (const rec of diskRuns) {
        runStore.set(rec.runId, rec);
      }
      let runs = Array.from(runStore.values()).sort((a, b) =>
        b.requestedAt.localeCompare(a.requestedAt)
      );
      if (params.job) {
        runs = runs.filter((run) => run.job === params.job);
      }
      if (params.status) {
        runs = runs.filter((run) => run.status === params.status);
      }
      runs = runs.slice(0, limit);

      return {
        content: [
          {
            type: "text",
            text: `Returned ${runs.length} Atlas run record(s).`
          }
        ],
        details: {
          count: runs.length,
          runs
        }
      };
    }
  });

  pi.registerTool({
    name: "atlas_jobs_cancel",
    label: "Atlas Cancel Run",
    description:
      "Cancel a queued/running Atlas job. Attempts to terminate the tracked child process and marks the run canceled when it exits.",
    parameters: AtlasJobCancelSchema,
    async execute(_toolCallId, params) {
      let record = runStore.get(params.runId);
      if (!record) {
        const fromDisk = readRunRecord(
          join(getRunsBaseDir(process.cwd()), `${params.runId}${MANIFEST_EXT}`)
        );
        if (fromDisk) {
          record = fromDisk;
          runStore.set(fromDisk.runId, fromDisk);
        }
      }
      if (!record) {
        return {
          content: [
            {
              type: "text",
              text: `Run ${params.runId} not found.`
            }
          ],
          details: {
            canceled: false,
            runId: params.runId
          }
        };
      }

      const active = activeRuns.get(params.runId);
      if (!active) {
        if (!terminalStatus(record.status)) {
          record.status = "canceled";
          record.finishedAt = nowIso();
          record.error = `Canceled (no active child attached)${params.reason ? `: ${params.reason}` : ""}`;
          upsertRecord(record);
          releaseLockForRecord(record);
        }
        return {
          content: [
            {
              type: "text",
              text: `Run ${params.runId} had no tracked child process. Manifest status set to ${record.status}.`
            }
          ],
          details: {
            canceled: true,
            record
          }
        };
      }

      active.cancelRequested = true;
      active.cancelReason = params.reason;
      if (!record.error || terminalStatus(record.status)) {
        record.error = `Cancellation requested${params.reason ? `: ${params.reason}` : ""}`;
      }
      upsertRecord(record);
      await killProcessTree(active.child);

      return {
        content: [
          {
            type: "text",
            text: `Cancellation requested for ${params.runId}.`
          }
        ],
        details: {
          canceled: true,
          record: runStore.get(params.runId) ?? record
        }
      };
    }
  });
}
