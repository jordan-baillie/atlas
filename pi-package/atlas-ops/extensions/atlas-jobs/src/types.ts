export type AtlasJobName =
  | "health_check"
  | "reoptimize_full_universe"
  | "validate_oos"
  | "auto_reoptimize"
  | "daily_automation"
  | "dashboard_generate_data"
  | "cli_ingest"
  | "cli_universe"
  | "cli_backtest"
  | "cli_plan"
  | "cli_approve"
  | "cli_paper_run"
  | "cli_status"
  | "cli_ledger"
  | "cli_eod_settlement"
  | "anneal_review";

export type AtlasJobStatus =
  | "queued"
  | "running"
  | "succeeded"
  | "failed"
  | "canceled"
  | "not_implemented";

export interface AtlasJobArtifact {
  kind:
    | "log"
    | "metrics_json"
    | "report_json"
    | "config_json"
    | "plan_json"
    | "portfolio_state_json"
    | "dashboard_json"
    | "unknown";
  path: string;
  description: string;
}

export interface AtlasJobSpec {
  name: AtlasJobName;
  category: "health" | "optimization" | "validation" | "daily" | "cli" | "reporting" | "research";
  summary: string;
  commandPreview: string;
  estimatedRuntimeSec?: number;
  writes: string[];
  reads: string[];
  artifacts: AtlasJobArtifact[];
  args?: {
    name: string;
    type: "string" | "number" | "boolean";
    required?: boolean;
    description: string;
  }[];
  approvalHint?: "safe" | "review" | "high_risk";
}

export interface AtlasJobRunRecord {
  runId: string;
  job: AtlasJobName;
  status: AtlasJobStatus;
  requestedAt: string;
  startedAt?: string;
  finishedAt?: string;
  cwd?: string;
  command?: string;
  args?: Record<string, unknown>;
  dryRun?: boolean;
  idempotencyKey?: string;
  timeoutSec?: number;
  exitCode?: number;
  pid?: number;
  signal?: string | null;
  stdoutTail?: string;
  stderrTail?: string;
  error?: string;
  artifacts?: AtlasJobArtifact[];
  notImplemented?: boolean;
  manifestPath?: string;
  stdoutLogPath?: string;
  stderrLogPath?: string;
  lockKey?: string;
  lockFilePath?: string;
}
