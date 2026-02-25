import type { ExtensionAPI } from "@mariozechner/pi-coding-agent";
import { Type } from "@sinclair/typebox";
import { existsSync, readFileSync, statSync } from "node:fs";
import { resolve } from "node:path";

type ArtifactKind =
  | "auto"
  | "health_check"
  | "reoptimization_full_universe"
  | "validate_oos"
  | "paper_trade_plan"
  | "json_generic";

type ParsedArtifact = {
  data: unknown;
  warnings: string[];
  fileSizeBytes: number;
  rawLength: number;
};

const LoadSchema = Type.Object({
  path: Type.String({ minLength: 1 }),
  cwd: Type.Optional(Type.String()),
  includeData: Type.Optional(
    Type.Boolean({
      description: "Include parsed JSON payload in response details. Default false."
    })
  )
});

const SummarizeSchema = Type.Object({
  path: Type.String({ minLength: 1 }),
  kind: Type.Optional(
    Type.Union([
      Type.Literal("auto"),
      Type.Literal("health_check"),
      Type.Literal("reoptimization_full_universe"),
      Type.Literal("validate_oos"),
      Type.Literal("paper_trade_plan"),
      Type.Literal("json_generic")
    ])
  ),
  cwd: Type.Optional(Type.String())
});

const CompareSchema = Type.Object({
  leftPath: Type.String({ minLength: 1 }),
  rightPath: Type.String({ minLength: 1 }),
  kind: Type.Optional(
    Type.Union([
      Type.Literal("auto"),
      Type.Literal("health_check"),
      Type.Literal("reoptimization_full_universe"),
      Type.Literal("validate_oos"),
      Type.Literal("paper_trade_plan"),
      Type.Literal("json_generic")
    ])
  ),
  cwd: Type.Optional(Type.String())
});

function safeNumber(value: unknown): number | undefined {
  return typeof value === "number" && Number.isFinite(value) ? value : undefined;
}

function asRecord(value: unknown): Record<string, unknown> | undefined {
  if (!value || typeof value !== "object" || Array.isArray(value)) return undefined;
  return value as Record<string, unknown>;
}

function parsePythonStyleJson(raw: string): { data: unknown; warnings: string[] } {
  const warnings: string[] = [];
  try {
    return { data: JSON.parse(raw), warnings };
  } catch {
    const sanitized = raw
      .replace(/\b-Infinity\b/g, "null")
      .replace(/\bInfinity\b/g, "null")
      .replace(/\bNaN\b/g, "null");
    if (sanitized !== raw) {
      warnings.push("Sanitized non-standard JSON tokens (Infinity/NaN) emitted by Python json.dump.");
      return { data: JSON.parse(sanitized), warnings };
    }
    throw new Error("Failed to parse JSON artifact.");
  }
}

function loadArtifact(path: string, cwd?: string): { resolvedPath: string; parsed: ParsedArtifact } {
  const resolvedPath = resolve(cwd ?? process.cwd(), path);
  if (!existsSync(resolvedPath)) {
    throw new Error(`Artifact not found: ${resolvedPath}`);
  }
  const stat = statSync(resolvedPath);
  const raw = readFileSync(resolvedPath, "utf8");
  const parsed = parsePythonStyleJson(raw);
  return {
    resolvedPath,
    parsed: {
      data: parsed.data,
      warnings: parsed.warnings,
      fileSizeBytes: stat.size,
      rawLength: raw.length
    }
  };
}

function topLevelKeys(data: unknown): string[] {
  const obj = asRecord(data);
  return obj ? Object.keys(obj) : [];
}

function guessKind(path: string, data: unknown): Exclude<ArtifactKind, "auto"> {
  const lower = path.toLowerCase().replace(/\\/g, "/");
  const obj = asRecord(data);
  if (lower.includes("health_check_") || (obj && typeof obj.status === "string" && obj.metrics)) {
    return "health_check";
  }
  if (lower.includes("reoptimization_full_universe") || (obj && obj.baseline_combined)) {
    return "reoptimization_full_universe";
  }
  if (
    lower.includes("oos_validation") ||
    (obj && obj.test1_time_period_split && obj.test2_perturbation)
  ) {
    return "validate_oos";
  }
  if (
    lower.includes("/paper_engine/plans/plan_") ||
    (obj && typeof obj.trade_date === "string" && obj.portfolio_snapshot && obj.risk_summary)
  ) {
    return "paper_trade_plan";
  }
  return "json_generic";
}

function normalizePctLike(value: unknown): number | undefined {
  if (typeof value !== "number") return undefined;
  return Number.isFinite(value) ? value : undefined;
}

function summarizeHealthCheck(data: unknown) {
  const obj = asRecord(data) ?? {};
  const metrics = asRecord(obj.metrics) ?? {};
  return {
    artifact_type: "health_check",
    status: obj.status ?? null,
    date: obj.date ?? null,
    config_version: obj.config_version ?? null,
    config_path: obj.config_path ?? null,
    report_path: obj.report_path ?? null,
    metrics: {
      cagr_pct: normalizePctLike(metrics.cagr_pct),
      sharpe: normalizePctLike(metrics.sharpe),
      profit_factor: normalizePctLike(metrics.profit_factor),
      max_drawdown_pct: normalizePctLike(metrics.max_drawdown_pct),
      total_trades: safeNumber(metrics.total_trades)
    },
    flags: Array.isArray(obj.flags) ? obj.flags : [],
    tickers_tested: safeNumber(obj.tickers_tested),
    data_window_months: safeNumber(obj.data_window_months),
    runtime_s: safeNumber(obj.runtime_s)
  };
}

function summarizeReoptimization(data: unknown) {
  const obj = asRecord(data) ?? {};
  const baseline = asRecord(obj.baseline_combined) ?? {};
  const strategies = Object.entries(obj)
    .filter(([k, v]) => {
      const r = asRecord(v);
      return !["timestamp", "n_tickers", "baseline_combined", "final_combined", "optimized_combined"].includes(k)
        && !!r
        && ("baseline" in r || "optimized" in r || "best_params" in r);
    })
    .map(([name, v]) => {
      const r = asRecord(v) ?? {};
      const baselineScore = safeNumber(r.baseline_score);
      const optimizedScore = safeNumber(r.optimized_score);
      const deltaScore =
        baselineScore !== undefined && optimizedScore !== undefined
          ? optimizedScore - baselineScore
          : undefined;
      const optimized = asRecord(r.optimized) ?? {};
      const baselineMetrics = asRecord(r.baseline) ?? {};
      return {
        strategy: name,
        improved: r.improved === true,
        iterations: safeNumber(r.iterations),
        baseline_score: baselineScore,
        optimized_score: optimizedScore,
        delta_score: deltaScore,
        baseline: {
          cagr_pct: normalizePctLike(safeNumber(baselineMetrics.cagr) !== undefined ? (baselineMetrics.cagr as number) * 100 : undefined),
          sharpe: normalizePctLike(baselineMetrics.sharpe),
          pf: normalizePctLike(baselineMetrics.pf),
          trades: safeNumber(baselineMetrics.trades)
        },
        optimized: {
          cagr_pct: normalizePctLike(safeNumber(optimized.cagr) !== undefined ? (optimized.cagr as number) * 100 : undefined),
          sharpe: normalizePctLike(optimized.sharpe),
          pf: normalizePctLike(optimized.pf),
          trades: safeNumber(optimized.trades)
        },
        best_params: asRecord(r.best_params) ?? null
      };
    });

  const improvedCount = strategies.filter((s) => s.improved).length;
  const scoreDeltas = strategies
    .filter((s) => typeof s.delta_score === "number")
    .sort((a, b) => (b.delta_score as number) - (a.delta_score as number));

  const finalCombined = asRecord(obj.final_combined ?? obj.optimized_combined);

  return {
    artifact_type: "reoptimization_full_universe",
    timestamp: obj.timestamp ?? null,
    n_tickers: safeNumber(obj.n_tickers),
    candidate_config_path: obj.candidate_config_path ?? null,
    backup_config_path: obj.backup_config_path ?? null,
    active_config_path: obj.active_config_path ?? null,
    active_config_overwritten: obj.active_config_overwritten ?? null,
    baseline_combined: {
      cagr_pct: normalizePctLike(safeNumber(baseline.cagr) !== undefined ? (baseline.cagr as number) * 100 : undefined),
      sharpe: normalizePctLike(baseline.sharpe),
      pf: normalizePctLike(baseline.pf),
      max_dd_pct: normalizePctLike(safeNumber(baseline.max_dd) !== undefined ? (baseline.max_dd as number) * 100 : undefined),
      trades: safeNumber(baseline.trades),
      total_pnl: safeNumber(baseline.total_pnl)
    },
    final_combined: finalCombined
      ? {
          cagr_pct: normalizePctLike(
            safeNumber(finalCombined.cagr) !== undefined ? (finalCombined.cagr as number) * 100 : undefined
          ),
          sharpe: normalizePctLike(finalCombined.sharpe),
          pf: normalizePctLike(finalCombined.pf),
          max_dd_pct: normalizePctLike(
            safeNumber(finalCombined.max_dd) !== undefined ? (finalCombined.max_dd as number) * 100 : undefined
          ),
          trades: safeNumber(finalCombined.trades),
          total_pnl: safeNumber(finalCombined.total_pnl)
        }
      : null,
    strategies_count: strategies.length,
    improved_count: improvedCount,
    improved_strategies: strategies.filter((s) => s.improved).map((s) => s.strategy),
    top_delta_score: scoreDeltas[0] ?? null,
    worst_delta_score: scoreDeltas.length > 0 ? scoreDeltas[scoreDeltas.length - 1] : null,
    strategies
  };
}

function summarizeValidateOos(data: unknown) {
  const obj = asRecord(data) ?? {};
  const t1 = asRecord(obj.test1_time_period_split) ?? {};
  const t2 = asRecord(obj.test2_perturbation) ?? {};
  const t3 = asRecord(obj.test3_walkforward_consistency) ?? {};
  const summary = asRecord(obj.summary) ?? {};
  const t2Summary = asRecord(t2.summary) ?? {};
  const t3Window = asRecord(t3.window_analysis) ?? {};

  const metricStat = (container: Record<string, unknown>, key: string) => {
    const stat = asRecord(container[key]) ?? {};
    return {
      mean: safeNumber(stat.mean),
      std: safeNumber(stat.std),
      min: safeNumber(stat.min),
      max: safeNumber(stat.max)
    };
  };

  return {
    artifact_type: "validate_oos",
    validation_type: obj.validation_type ?? null,
    timestamp: obj.timestamp ?? null,
    config_version: obj.config_version ?? null,
    config_path: obj.config_path ?? null,
    output_path: obj.output_path ?? null,
    split_date: obj.split_date ?? null,
    summary: {
      test1_verdict: summary.test1_verdict ?? null,
      test2_verdict: summary.test2_verdict ?? null,
      test3_verdict: summary.test3_verdict ?? null,
      overall_verdict: summary.overall_verdict ?? null,
      total_runtime_s: safeNumber(summary.total_runtime_s),
      total_runtime_min: safeNumber(summary.total_runtime_min)
    },
    test1: {
      in_sample: asRecord(t1.in_sample) ?? null,
      out_of_sample: asRecord(t1.out_of_sample) ?? null,
      full_metrics: asRecord(t1.full_metrics) ?? null,
      degradation_pct: asRecord(t1.degradation_pct) ?? null,
      runtime_s: safeNumber(t1.runtime_s)
    },
    test2: {
      robust: t2.robust ?? null,
      collapse_count: safeNumber(t2.collapse_count),
      cagr_pct: metricStat(t2Summary, "cagr_pct"),
      sharpe: metricStat(t2Summary, "sharpe"),
      profit_factor: metricStat(t2Summary, "profit_factor"),
      max_drawdown_pct: metricStat(t2Summary, "max_drawdown_pct"),
      total_trades: metricStat(t2Summary, "total_trades")
    },
    test3: {
      full_metrics: asRecord(t3.full_metrics) ?? null,
      window_analysis: {
        n_windows: safeNumber(t3Window.n_windows),
        n_positive_windows: safeNumber(t3Window.n_positive_windows),
        n_negative_windows: safeNumber(t3Window.n_negative_windows),
        win_rate_windows_pct: safeNumber(t3Window.win_rate_windows_pct),
        mean_window_return_pct: safeNumber(t3Window.mean_window_return_pct),
        std_window_return_pct: safeNumber(t3Window.std_window_return_pct),
        min_window_return_pct: safeNumber(t3Window.min_window_return_pct),
        max_window_return_pct: safeNumber(t3Window.max_window_return_pct),
        mean_trades_per_window: safeNumber(t3Window.mean_trades_per_window)
      },
      runtime_s: safeNumber(t3.runtime_s)
    }
  };
}

function summarizePaperTradePlan(data: unknown) {
  const obj = asRecord(data) ?? {};
  const snapshot = asRecord(obj.portfolio_snapshot) ?? {};
  const risk = asRecord(obj.risk_summary) ?? {};
  const openPositions = Array.isArray(obj.open_positions) ? obj.open_positions : [];
  const proposedEntries = Array.isArray(obj.proposed_entries) ? obj.proposed_entries : [];
  const rejectedEntries = Array.isArray(obj.rejected_entries) ? obj.rejected_entries : [];
  const proposedExits = Array.isArray(obj.proposed_exits) ? obj.proposed_exits : [];

  const entriesByStrategy: Record<string, number> = {};
  let totalEntryRisk = 0;
  let totalEntryCost = 0;
  for (const entry of proposedEntries) {
    const e = asRecord(entry) ?? {};
    const strategy = typeof e.strategy === "string" ? e.strategy : "unknown";
    entriesByStrategy[strategy] = (entriesByStrategy[strategy] ?? 0) + 1;
    const riskAmount = safeNumber(e.risk_amount);
    if (riskAmount !== undefined) totalEntryRisk += riskAmount;
    const entryPrice = safeNumber(e.entry_price);
    const size = safeNumber(e.position_size);
    if (entryPrice !== undefined && size !== undefined) totalEntryCost += entryPrice * size;
  }

  const openByStrategy: Record<string, number> = {};
  let openUnrealizedPnl = 0;
  let largestWinner: Record<string, unknown> | null = null;
  let largestLoser: Record<string, unknown> | null = null;
  for (const pos of openPositions) {
    const p = asRecord(pos) ?? {};
    const strategy = typeof p.strategy === "string" ? p.strategy : "unknown";
    openByStrategy[strategy] = (openByStrategy[strategy] ?? 0) + 1;
    const pnl = safeNumber(p.unrealized_pnl);
    if (pnl !== undefined) {
      openUnrealizedPnl += pnl;
      if (!largestWinner || pnl > (safeNumber(largestWinner.unrealized_pnl) ?? Number.NEGATIVE_INFINITY)) {
        largestWinner = {
          ticker: typeof p.ticker === "string" ? p.ticker : null,
          strategy,
          unrealized_pnl: pnl,
          unrealized_pnl_pct: safeNumber(p.unrealized_pnl_pct) ?? null
        };
      }
      if (!largestLoser || pnl < (safeNumber(largestLoser.unrealized_pnl) ?? Number.POSITIVE_INFINITY)) {
        largestLoser = {
          ticker: typeof p.ticker === "string" ? p.ticker : null,
          strategy,
          unrealized_pnl: pnl,
          unrealized_pnl_pct: safeNumber(p.unrealized_pnl_pct) ?? null
        };
      }
    }
  }

  const rejectionReasons: Record<string, number> = {};
  for (const rejected of rejectedEntries) {
    const r = asRecord(rejected) ?? {};
    const reason = typeof r.rejection_reason === "string" ? r.rejection_reason : "unknown";
    rejectionReasons[reason] = (rejectionReasons[reason] ?? 0) + 1;
  }

  return {
    artifact_type: "paper_trade_plan",
    trade_date: obj.trade_date ?? null,
    generated_at: obj.generated_at ?? null,
    status: obj.status ?? null,
    approved_at: obj.approved_at ?? null,
    counts: {
      proposed_entries: proposedEntries.length,
      rejected_entries: rejectedEntries.length,
      proposed_exits: proposedExits.length,
      open_positions: openPositions.length
    },
    portfolio_snapshot: {
      equity: safeNumber(snapshot.equity),
      cash: safeNumber(snapshot.cash),
      open_positions: safeNumber(snapshot.open_positions),
      total_pnl: safeNumber(snapshot.total_pnl),
      total_pnl_pct: safeNumber(snapshot.total_pnl_pct)
    },
    risk_summary: {
      total_proposed_cost: safeNumber(risk.total_proposed_cost),
      total_proposed_risk: safeNumber(risk.total_proposed_risk),
      positions_after: safeNumber(risk.positions_after),
      cash_after_entries: safeNumber(risk.cash_after_entries),
      portfolio_exposure_pct: safeNumber(risk.portfolio_exposure_pct)
    },
    derived: {
      proposed_entry_cost_calc: proposedEntries.length > 0 ? Math.round(totalEntryCost * 100) / 100 : 0,
      proposed_entry_risk_calc: proposedEntries.length > 0 ? Math.round(totalEntryRisk * 100) / 100 : 0,
      open_unrealized_pnl_sum: Math.round(openUnrealizedPnl * 100) / 100
    },
    entries_by_strategy: entriesByStrategy,
    open_positions_by_strategy: openByStrategy,
    rejection_reasons: rejectionReasons,
    largest_winner: largestWinner,
    largest_loser: largestLoser
  };
}

function summarizeGeneric(data: unknown) {
  const obj = asRecord(data);
  return {
    artifact_type: "json_generic",
    top_level_type: Array.isArray(data) ? "array" : typeof data,
    top_level_keys: obj ? Object.keys(obj) : [],
    array_length: Array.isArray(data) ? data.length : undefined
  };
}

function summarizeByKind(kind: ArtifactKind, path: string, data: unknown) {
  const resolvedKind = kind === "auto" ? guessKind(path, data) : kind;
  switch (resolvedKind) {
    case "health_check":
      return { kind: resolvedKind, summary: summarizeHealthCheck(data) };
    case "reoptimization_full_universe":
      return { kind: resolvedKind, summary: summarizeReoptimization(data) };
    case "validate_oos":
      return { kind: resolvedKind, summary: summarizeValidateOos(data) };
    case "paper_trade_plan":
      return { kind: resolvedKind, summary: summarizePaperTradePlan(data) };
    case "json_generic":
    default:
      return { kind: "json_generic" as const, summary: summarizeGeneric(data) };
  }
}

function flattenNumbers(value: unknown, prefix = "", out: Record<string, number> = {}) {
  if (typeof value === "number" && Number.isFinite(value)) {
    if (prefix) out[prefix] = value;
    return out;
  }
  if (Array.isArray(value)) return out;
  const obj = asRecord(value);
  if (!obj) return out;
  for (const [k, v] of Object.entries(obj)) {
    const next = prefix ? `${prefix}.${k}` : k;
    flattenNumbers(v, next, out);
  }
  return out;
}

function compareSummaries(leftSummary: unknown, rightSummary: unknown) {
  const leftNums = flattenNumbers(leftSummary);
  const rightNums = flattenNumbers(rightSummary);
  const common = Object.keys(leftNums).filter((k) => k in rightNums).sort();
  const deltas = common.map((k) => ({
    metric: k,
    left: leftNums[k],
    right: rightNums[k],
    delta: rightNums[k] - leftNums[k]
  }));
  deltas.sort((a, b) => Math.abs(b.delta) - Math.abs(a.delta));
  return {
    compared_metrics: common.length,
    top_deltas: deltas.slice(0, 20)
  };
}

export default function atlasArtifactsExtension(pi: ExtensionAPI) {
  pi.registerTool({
    name: "atlas_artifacts_load",
    label: "Atlas Artifact Load",
    description:
      "Load and parse an Atlas artifact JSON file (tolerates Python JSON Infinity/NaN) and return metadata plus optional payload.",
    parameters: LoadSchema,
    async execute(_toolCallId, params) {
      const { resolvedPath, parsed } = loadArtifact(params.path, params.cwd);
      const guessedKind = guessKind(resolvedPath, parsed.data);
      return {
        content: [
          {
            type: "text",
            text: `Loaded ${resolvedPath} (${parsed.fileSizeBytes} bytes, kind=${guessedKind}).`
          }
        ],
        details: {
          path: params.path,
          resolvedPath,
          fileSizeBytes: parsed.fileSizeBytes,
          rawLength: parsed.rawLength,
          guessedKind,
          topLevelKeys: topLevelKeys(parsed.data),
          warnings: parsed.warnings,
          data: params.includeData ? parsed.data : undefined
        }
      };
    }
  });

  pi.registerTool({
    name: "atlas_artifacts_summarize",
    label: "Atlas Artifact Summarize",
    description:
      "Summarize Atlas health check, reoptimization, or OOS validation JSON artifacts into a normalized structure.",
    parameters: SummarizeSchema,
    async execute(_toolCallId, params) {
      const { resolvedPath, parsed } = loadArtifact(params.path, params.cwd);
      const { kind, summary } = summarizeByKind(params.kind ?? "auto", resolvedPath, parsed.data);
      return {
        content: [
          {
            type: "text",
            text: `Summarized ${resolvedPath} as ${kind}.`
          }
        ],
        details: {
          path: params.path,
          resolvedPath,
          kind,
          fileSizeBytes: parsed.fileSizeBytes,
          warnings: parsed.warnings,
          summary
        }
      };
    }
  });

  pi.registerTool({
    name: "atlas_artifacts_compare",
    label: "Atlas Artifact Compare",
    description:
      "Compare two Atlas artifact JSON files by summarizing each and computing numeric deltas over common summary fields.",
    parameters: CompareSchema,
    async execute(_toolCallId, params) {
      const left = loadArtifact(params.leftPath, params.cwd);
      const right = loadArtifact(params.rightPath, params.cwd);
      const leftSumm = summarizeByKind(params.kind ?? "auto", left.resolvedPath, left.parsed.data);
      const rightSumm = summarizeByKind(params.kind ?? "auto", right.resolvedPath, right.parsed.data);
      const kind = (params.kind ?? "auto") === "auto"
        ? (leftSumm.kind === rightSumm.kind ? leftSumm.kind : "json_generic")
        : (params.kind as Exclude<ArtifactKind, "auto">);

      const comparison = compareSummaries(leftSumm.summary, rightSumm.summary);
      return {
        content: [
          {
            type: "text",
            text:
              `Compared artifacts (${left.resolvedPath} vs ${right.resolvedPath}) ` +
              `using kind=${kind}; ${comparison.compared_metrics} numeric fields in common.`
          }
        ],
        details: {
          kind,
          left: {
            path: params.leftPath,
            resolvedPath: left.resolvedPath,
            warnings: left.parsed.warnings,
            summary: leftSumm.summary
          },
          right: {
            path: params.rightPath,
            resolvedPath: right.resolvedPath,
            warnings: right.parsed.warnings,
            summary: rightSumm.summary
          },
          comparison
        }
      };
    }
  });
}
