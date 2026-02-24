import type { ExtensionAPI } from "@mariozechner/pi-coding-agent";
import { Type } from "@sinclair/typebox";
import { copyFileSync, existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";

type JsonRecord = Record<string, unknown>;

function nowIso(): string {
  return new Date().toISOString();
}

function ensureDir(path: string): void {
  mkdirSync(path, { recursive: true });
}

function asRecord(value: unknown): JsonRecord | undefined {
  if (!value || typeof value !== "object" || Array.isArray(value)) return undefined;
  return value as JsonRecord;
}

function readJson(path: string): unknown {
  return JSON.parse(readFileSync(path, "utf8"));
}

function writeJson(path: string, data: unknown): void {
  writeFileSync(path, `${JSON.stringify(data, null, 2)}\n`, "utf8");
}

function resolvePath(cwd: string | undefined, path: string): string {
  return resolve(cwd ?? process.cwd(), path);
}

function defaultPlanPath(date?: string): string {
  if (date && date.trim()) return `paper_engine/plans/plan_${date}.json`;
  const today = new Date().toISOString().slice(0, 10);
  return `paper_engine/plans/plan_${today}.json`;
}

function loadJsonObjectOrThrow(path: string, label: string): JsonRecord {
  if (!existsSync(path)) {
    throw new Error(`${label} not found: ${path}`);
  }
  const parsed = readJson(path);
  const obj = asRecord(parsed);
  if (!obj) {
    throw new Error(`${label} must be a JSON object: ${path}`);
  }
  return obj;
}

function asBool(value: unknown): boolean | undefined {
  return typeof value === "boolean" ? value : undefined;
}

function asNumber(value: unknown): number | undefined {
  return typeof value === "number" && Number.isFinite(value) ? value : undefined;
}

function asString(value: unknown): string | undefined {
  return typeof value === "string" ? value : undefined;
}

function countArray(value: unknown): number {
  return Array.isArray(value) ? value.length : 0;
}

function evaluatePlanGate(
  plan: JsonRecord,
  activeConfig: JsonRecord,
  action: "evaluate" | "approve" | "execute",
  maxExposurePct?: number
) {
  const trading = asRecord(activeConfig.trading) ?? {};
  const approvalRequired = asBool(trading.approval_required) ?? true;
  const status = asString(plan.status) ?? "UNKNOWN";
  const riskSummary = asRecord(plan.risk_summary) ?? {};
  const portfolioSnapshot = asRecord(plan.portfolio_snapshot) ?? {};
  const exposurePct = asNumber(riskSummary.portfolio_exposure_pct);

  const blockers: string[] = [];
  const warnings: string[] = [];

  if (action === "approve") {
    if (status !== "PENDING_APPROVAL") {
      blockers.push(`Plan status must be PENDING_APPROVAL to approve (got ${status}).`);
    }
  }

  if (action === "execute") {
    if (status === "EXECUTED") {
      blockers.push("Plan is already EXECUTED.");
    } else if (approvalRequired && status !== "APPROVED") {
      blockers.push(`Execution requires APPROVED plan when trading.approval_required=true (got ${status}).`);
    } else if (!approvalRequired && status !== "APPROVED") {
      warnings.push(`Plan status is ${status}; execution is only allowed because approval_required=false.`);
    }
  }

  if (maxExposurePct !== undefined && exposurePct !== undefined && exposurePct > maxExposurePct) {
    blockers.push(
      `Portfolio exposure ${exposurePct.toFixed(2)}% exceeds gate limit ${maxExposurePct.toFixed(2)}%.`
    );
  }

  const proposedEntries = countArray(plan.proposed_entries);
  const rejectedEntries = countArray(plan.rejected_entries);
  const proposedExits = countArray(plan.proposed_exits);
  const openPositions = countArray(plan.open_positions);

  const verdict = blockers.length > 0 ? "block" : warnings.length > 0 ? "review" : "allow";
  return {
    verdict,
    blockers,
    warnings,
    plan: {
      trade_date: asString(plan.trade_date) ?? null,
      status,
      generated_at: asString(plan.generated_at) ?? null,
      approved_at: asString(plan.approved_at) ?? null,
      proposed_entries: proposedEntries,
      rejected_entries: rejectedEntries,
      proposed_exits: proposedExits,
      open_positions: openPositions,
      portfolio_snapshot: {
        equity: asNumber(portfolioSnapshot.equity) ?? null,
        cash: asNumber(portfolioSnapshot.cash) ?? null,
        total_pnl: asNumber(portfolioSnapshot.total_pnl) ?? null,
        total_pnl_pct: asNumber(portfolioSnapshot.total_pnl_pct) ?? null
      },
      risk_summary: {
        total_proposed_cost: asNumber(riskSummary.total_proposed_cost) ?? null,
        total_proposed_risk: asNumber(riskSummary.total_proposed_risk) ?? null,
        positions_after: asNumber(riskSummary.positions_after) ?? null,
        cash_after_entries: asNumber(riskSummary.cash_after_entries) ?? null,
        portfolio_exposure_pct: exposurePct ?? null
      }
    },
    config: {
      approval_required: approvalRequired,
      trading_mode: asString(trading.mode) ?? null
    }
  };
}

function enabledStrategyCount(config: JsonRecord): number {
  const strategies = asRecord(config.strategies) ?? {};
  return Object.values(strategies).filter((v) => {
    const s = asRecord(v);
    return s && s.enabled === true;
  }).length;
}

function evaluateConfigPromotionGate(
  activeConfig: JsonRecord,
  candidateConfig: JsonRecord,
  opts: {
    allowDisableApproval?: boolean;
    allowTradingModeChange?: boolean;
    maxRiskPerTradePct?: number;
  }
) {
  const blockers: string[] = [];
  const warnings: string[] = [];
  const checks: string[] = [];

  const activeTrading = asRecord(activeConfig.trading) ?? {};
  const candidateTrading = asRecord(candidateConfig.trading) ?? {};
  const activeRisk = asRecord(activeConfig.risk) ?? {};
  const candidateRisk = asRecord(candidateConfig.risk) ?? {};

  const activeVersion = asString(activeConfig.version);
  const candidateVersion = asString(candidateConfig.version);
  const activeMode = asString(activeTrading.mode);
  const candidateMode = asString(candidateTrading.mode);
  const activeApproval = asBool(activeTrading.approval_required);
  const candidateApproval = asBool(candidateTrading.approval_required);
  const activeRiskPerTrade = asNumber(activeRisk.max_risk_per_trade_pct);
  const candidateRiskPerTrade = asNumber(candidateRisk.max_risk_per_trade_pct);
  const candidateMaxPositions = asNumber(candidateRisk.max_open_positions);
  const activeMaxPositions = asNumber(activeRisk.max_open_positions);

  if (!candidateVersion) {
    blockers.push("Candidate config missing top-level version.");
  } else {
    checks.push(`candidate.version=${candidateVersion}`);
  }

  if (activeVersion && candidateVersion && activeVersion === candidateVersion) {
    warnings.push(`Candidate version matches active version (${activeVersion}).`);
  }

  if (candidateApproval !== true && !opts.allowDisableApproval) {
    blockers.push("Candidate trading.approval_required must remain true (override not provided).");
  } else if (candidateApproval === false) {
    warnings.push("Candidate disables trading approval gate.");
  }
  if (candidateApproval !== undefined) {
    checks.push(`candidate.trading.approval_required=${String(candidateApproval)}`);
  }

  if (activeMode && candidateMode && activeMode !== candidateMode && !opts.allowTradingModeChange) {
    blockers.push(`Candidate trading.mode changes ${activeMode} -> ${candidateMode} (override not provided).`);
  } else if (activeMode && candidateMode && activeMode !== candidateMode) {
    warnings.push(`Candidate trading.mode changes ${activeMode} -> ${candidateMode}.`);
  }
  if (candidateMode) {
    checks.push(`candidate.trading.mode=${candidateMode}`);
  }

  if (opts.maxRiskPerTradePct !== undefined && candidateRiskPerTrade !== undefined) {
    if (candidateRiskPerTrade > opts.maxRiskPerTradePct) {
      blockers.push(
        `Candidate risk.max_risk_per_trade_pct=${candidateRiskPerTrade} exceeds limit ${opts.maxRiskPerTradePct}.`
      );
    }
    checks.push(`gate.maxRiskPerTradePct=${opts.maxRiskPerTradePct}`);
  }

  if (
    activeRiskPerTrade !== undefined &&
    candidateRiskPerTrade !== undefined &&
    candidateRiskPerTrade > activeRiskPerTrade
  ) {
    warnings.push(
      `Candidate max_risk_per_trade_pct increased ${activeRiskPerTrade} -> ${candidateRiskPerTrade}.`
    );
  }

  if (
    activeMaxPositions !== undefined &&
    candidateMaxPositions !== undefined &&
    candidateMaxPositions > activeMaxPositions
  ) {
    warnings.push(`Candidate max_open_positions increased ${activeMaxPositions} -> ${candidateMaxPositions}.`);
  }

  const activeStrategies = enabledStrategyCount(activeConfig);
  const candidateStrategies = enabledStrategyCount(candidateConfig);
  if (candidateStrategies !== activeStrategies) {
    warnings.push(`Enabled strategy count changed ${activeStrategies} -> ${candidateStrategies}.`);
  }

  const verdict = blockers.length > 0 ? "block" : warnings.length > 0 ? "review" : "allow";
  return {
    verdict,
    blockers,
    warnings,
    checks,
    active: {
      version: activeVersion ?? null,
      trading_mode: activeMode ?? null,
      approval_required: activeApproval ?? null,
      max_risk_per_trade_pct: activeRiskPerTrade ?? null,
      max_open_positions: activeMaxPositions ?? null,
      enabled_strategies: activeStrategies
    },
    candidate: {
      version: candidateVersion ?? null,
      trading_mode: candidateMode ?? null,
      approval_required: candidateApproval ?? null,
      max_risk_per_trade_pct: candidateRiskPerTrade ?? null,
      max_open_positions: candidateMaxPositions ?? null,
      enabled_strategies: candidateStrategies
    }
  };
}

function riskAuditDir(cwd?: string): string {
  return resolve(cwd ?? process.cwd(), ".pi", "atlas-risk-gates", "audit");
}

function writeAudit(
  cwd: string | undefined,
  category: "plan-approval" | "config-promotion",
  record: JsonRecord
): string {
  const dir = join(riskAuditDir(cwd), category);
  ensureDir(dir);
  const ts = new Date().toISOString().replace(/[:.]/g, "-");
  const suffix = asString(record.id) ?? asString(record.trade_date) ?? "event";
  const safeSuffix = suffix.replace(/[^a-zA-Z0-9_.-]+/g, "_");
  const path = join(dir, `${ts}_${safeSuffix}.json`);
  writeJson(path, record);
  return path;
}

const PlanGateSchema = Type.Object({
  date: Type.Optional(Type.String({ minLength: 1 })),
  planPath: Type.Optional(Type.String({ minLength: 1 })),
  cwd: Type.Optional(Type.String()),
  action: Type.Optional(
    Type.Union([Type.Literal("evaluate"), Type.Literal("approve"), Type.Literal("execute")])
  ),
  maxExposurePct: Type.Optional(Type.Number({ minimum: 0 }))
});

const ApprovePlanSchema = Type.Object({
  date: Type.Optional(Type.String({ minLength: 1 })),
  planPath: Type.Optional(Type.String({ minLength: 1 })),
  cwd: Type.Optional(Type.String()),
  confirmed: Type.Boolean({
    description: "Must be true to write APPROVED status to the plan JSON."
  }),
  approver: Type.Optional(Type.String({ minLength: 1 })),
  note: Type.Optional(Type.String())
});

const ConfigPromotionCheckSchema = Type.Object({
  candidatePath: Type.String({ minLength: 1 }),
  activePath: Type.Optional(Type.String({ minLength: 1 })),
  cwd: Type.Optional(Type.String()),
  allowDisableApproval: Type.Optional(Type.Boolean()),
  allowTradingModeChange: Type.Optional(Type.Boolean()),
  maxRiskPerTradePct: Type.Optional(Type.Number({ minimum: 0 }))
});

const ConfigPromotionApplySchema = Type.Object({
  candidatePath: Type.String({ minLength: 1 }),
  activePath: Type.Optional(Type.String({ minLength: 1 })),
  cwd: Type.Optional(Type.String()),
  allowDisableApproval: Type.Optional(Type.Boolean()),
  allowTradingModeChange: Type.Optional(Type.Boolean()),
  maxRiskPerTradePct: Type.Optional(Type.Number({ minimum: 0 })),
  confirmed: Type.Boolean({
    description: "Must be true to overwrite active config with candidate config."
  }),
  dryRun: Type.Optional(Type.Boolean()),
  approver: Type.Optional(Type.String({ minLength: 1 })),
  note: Type.Optional(Type.String())
});

export default function atlasRiskGatesExtension(pi: ExtensionAPI) {
  pi.registerTool({
    name: "atlas_risk_check_plan_gate",
    label: "Atlas Risk Check Plan Gate",
    description:
      "Evaluate whether a paper trade plan can be approved or executed based on plan status and active trading approval settings.",
    parameters: PlanGateSchema,
    async execute(_toolCallId, params) {
      const planPath = resolvePath(params.cwd, params.planPath ?? defaultPlanPath(params.date));
      const activeConfigPath = resolvePath(params.cwd, "config/active_config.json");
      const plan = loadJsonObjectOrThrow(planPath, "Plan");
      const activeConfig = loadJsonObjectOrThrow(activeConfigPath, "Active config");
      const action = (params.action ?? "evaluate") as "evaluate" | "approve" | "execute";
      const gate = evaluatePlanGate(plan, activeConfig, action, params.maxExposurePct);
      return {
        content: [
          {
            type: "text",
            text: `Plan gate (${action}) verdict=${gate.verdict} for ${planPath}.`
          }
        ],
        details: {
          action,
          planPath,
          activeConfigPath,
          ...gate
        }
      };
    }
  });

  pi.registerTool({
    name: "atlas_risk_approve_plan",
    label: "Atlas Risk Approve Plan",
    description:
      "Mark a plan JSON as APPROVED after passing plan gate checks. Requires confirmed=true and writes an audit record.",
    parameters: ApprovePlanSchema,
    async execute(_toolCallId, params) {
      if (!params.confirmed) {
        throw new Error("Refusing to approve plan: confirmed must be true.");
      }
      const planPath = resolvePath(params.cwd, params.planPath ?? defaultPlanPath(params.date));
      const activeConfigPath = resolvePath(params.cwd, "config/active_config.json");
      const plan = loadJsonObjectOrThrow(planPath, "Plan");
      const activeConfig = loadJsonObjectOrThrow(activeConfigPath, "Active config");
      const gate = evaluatePlanGate(plan, activeConfig, "approve");
      if (gate.blockers.length > 0) {
        throw new Error(`Plan approval blocked: ${gate.blockers.join(" | ")}`);
      }

      plan.status = "APPROVED";
      plan.approved_at = nowIso();
      if (params.approver) plan.approved_by = params.approver;
      if (params.note) plan.approval_note = params.note;
      writeJson(planPath, plan);

      const auditPath = writeAudit(params.cwd, "plan-approval", {
        id: `plan_${asString(plan.trade_date) ?? "unknown"}`,
        event: "plan_approved",
        approved_at: String(plan.approved_at),
        trade_date: asString(plan.trade_date) ?? null,
        approver: params.approver ?? null,
        note: params.note ?? null,
        plan_path: planPath
      });

      return {
        content: [{ type: "text", text: `Approved plan ${planPath}.` }],
        details: {
          planPath,
          activeConfigPath,
          auditPath,
          status: plan.status,
          approved_at: plan.approved_at,
          approved_by: plan.approved_by ?? null,
          gate
        }
      };
    }
  });

  pi.registerTool({
    name: "atlas_risk_check_config_promotion",
    label: "Atlas Risk Check Config Promotion",
    description:
      "Evaluate whether a candidate config is safe to promote to active_config.json using conservative guardrails.",
    parameters: ConfigPromotionCheckSchema,
    async execute(_toolCallId, params) {
      const activePath = resolvePath(params.cwd, params.activePath ?? "config/active_config.json");
      const candidatePath = resolvePath(params.cwd, params.candidatePath);
      const activeConfig = loadJsonObjectOrThrow(activePath, "Active config");
      const candidateConfig = loadJsonObjectOrThrow(candidatePath, "Candidate config");
      const gate = evaluateConfigPromotionGate(activeConfig, candidateConfig, {
        allowDisableApproval: params.allowDisableApproval,
        allowTradingModeChange: params.allowTradingModeChange,
        maxRiskPerTradePct: params.maxRiskPerTradePct
      });
      return {
        content: [
          {
            type: "text",
            text: `Config promotion gate verdict=${gate.verdict} for candidate ${candidatePath}.`
          }
        ],
        details: {
          activePath,
          candidatePath,
          ...gate
        }
      };
    }
  });

  pi.registerTool({
    name: "atlas_risk_promote_config",
    label: "Atlas Risk Promote Config",
    description:
      "Promote a candidate config to active_config.json after gate checks. Creates a timestamped backup and audit record.",
    parameters: ConfigPromotionApplySchema,
    async execute(_toolCallId, params) {
      if (!params.confirmed) {
        throw new Error("Refusing to promote config: confirmed must be true.");
      }
      const cwd = params.cwd;
      const activePath = resolvePath(cwd, params.activePath ?? "config/active_config.json");
      const candidatePath = resolvePath(cwd, params.candidatePath);
      const activeConfig = loadJsonObjectOrThrow(activePath, "Active config");
      const candidateConfig = loadJsonObjectOrThrow(candidatePath, "Candidate config");
      const gate = evaluateConfigPromotionGate(activeConfig, candidateConfig, {
        allowDisableApproval: params.allowDisableApproval,
        allowTradingModeChange: params.allowTradingModeChange,
        maxRiskPerTradePct: params.maxRiskPerTradePct
      });
      if (gate.blockers.length > 0) {
        throw new Error(`Config promotion blocked: ${gate.blockers.join(" | ")}`);
      }

      const ts = new Date().toISOString().replace(/[-:TZ.]/g, "").slice(0, 14);
      const backupPath = join(
        resolve(cwd ?? process.cwd(), "config"),
        `active_config_backup_${ts}.json`
      );

      if (!params.dryRun) {
        ensureDir(dirname(backupPath));
        copyFileSync(activePath, backupPath);
        copyFileSync(candidatePath, activePath);
      }

      const auditPath = writeAudit(cwd, "config-promotion", {
        id: `config_${ts}`,
        event: "config_promoted",
        promoted_at: nowIso(),
        approver: params.approver ?? null,
        note: params.note ?? null,
        dry_run: params.dryRun === true,
        active_path: activePath,
        candidate_path: candidatePath,
        backup_path: backupPath,
        active_version_before: gate.active.version,
        candidate_version: gate.candidate.version,
        gate_verdict: gate.verdict,
        warnings: gate.warnings
      });

      return {
        content: [
          {
            type: "text",
            text:
              params.dryRun === true
                ? `Dry-run promotion passed for ${candidatePath}; active config not modified.`
                : `Promoted ${candidatePath} to ${activePath} (backup: ${backupPath}).`
          }
        ],
        details: {
          activePath,
          candidatePath,
          backupPath,
          auditPath,
          dryRun: params.dryRun === true,
          gate
        }
      };
    }
  });
}
