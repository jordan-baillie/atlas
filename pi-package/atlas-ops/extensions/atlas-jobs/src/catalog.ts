import type { AtlasJobSpec } from "./types";

export const ATLAS_JOB_CATALOG: AtlasJobSpec[] = [
  {
    name: "health_check",
    category: "health",
    summary: "Run recent-period backtest health check and write a health report JSON.",
    commandPreview: "python scripts/health_check.py [--config-path PATH] [--report-path PATH] [--months N]",
    estimatedRuntimeSec: 300,
    reads: ["config/active_config.json", "data/cache/*.parquet"],
    writes: ["logs/health_check_YYYY-MM-DD.json"],
    artifacts: [
      {
        kind: "report_json",
        path: "logs/health_check_YYYY-MM-DD.json",
        description: "Health status, baseline comparison, degradation flags, and metrics."
      }
    ],
    args: [
      {
        name: "configPath",
        type: "string",
        description: "Optional config JSON path; defaults to config/active_config.json."
      },
      {
        name: "reportPath",
        type: "string",
        description: "Optional health report output path; defaults to logs/health_check_YYYY-MM-DD.json."
      },
      {
        name: "months",
        type: "number",
        description: "Recent data window in months; defaults to 18."
      }
    ],
    approvalHint: "safe"
  },
  {
    name: "reoptimize_full_universe",
    category: "optimization",
    summary: "Coordinate-descent re-optimization across active strategies on the full cached universe.",
    commandPreview:
      "python scripts/reoptimize_full_universe.py [--candidate-path PATH] [--results-path PATH] [--backup-path PATH] [--promote-active]",
    estimatedRuntimeSec: 7200,
    reads: ["config/active_config.json", "data/cache/*.parquet"],
    writes: ["backtest/results/reoptimization_full_universe.json", "config/*.json"],
    artifacts: [
      {
        kind: "metrics_json",
        path: "backtest/results/reoptimization_full_universe.json",
        description: "Optimization sweep results and baseline/optimized scores."
      },
      {
        kind: "config_json",
        path: "config/config_candidate_reoptimized_*.json",
        description: "Staged optimized candidate config (default behavior, no active overwrite)."
      }
    ],
    args: [
      {
        name: "candidatePath",
        type: "string",
        description: "Optional staged candidate config path to write."
      },
      {
        name: "resultsPath",
        type: "string",
        description: "Optional reoptimization results JSON output path."
      },
      {
        name: "backupPath",
        type: "string",
        description: "Optional path for backing up current active config before optimization."
      },
      {
        name: "promoteActive",
        type: "boolean",
        description: "If true, also overwrite config/active_config.json (high risk)."
      }
    ],
    approvalHint: "high_risk"
  },
  {
    name: "validate_oos",
    category: "validation",
    summary: "Run OOS split, perturbation robustness, and walk-forward consistency validation.",
    commandPreview: "python scripts/validate_oos.py [--config-path PATH] [--output-path PATH]",
    estimatedRuntimeSec: 5400,
    reads: ["config/active_config.json", "data/cache/*.parquet"],
    writes: ["backtest/results/v92_oos_validation.json"],
    artifacts: [
      {
        kind: "report_json",
        path: "backtest/results/v92_oos_validation.json",
        description: "OOS metrics, perturbation trials, and walk-forward window analysis."
      },
      {
        kind: "report_json",
        path: "backtest/results/v92_oos_validation_candidate_*.json",
        description: "Candidate-specific OOS validation artifact for staged promotion workflows."
      }
    ],
    args: [
      {
        name: "configPath",
        type: "string",
        description: "Optional config JSON to validate; defaults to active config."
      },
      {
        name: "outputPath",
        type: "string",
        description: "Optional validation report output path."
      }
    ],
    approvalHint: "review"
  },
  {
    name: "auto_reoptimize",
    category: "optimization",
    summary: "Legacy end-to-end health check, reoptimize, validate, compare, and restore/promote flow (high risk).",
    commandPreview: "python scripts/auto_reoptimize.py",
    estimatedRuntimeSec: 10800,
    reads: ["config/active_config.json", "logs/health_check_*.json", "backtest/results/*.json"],
    writes: [
      "logs/auto_reoptimize_YYYY-MM-DD.log",
      "config/active_config_backup_*.json",
      "config/config_candidate_auto_reopt_*.json",
      "backtest/results/v92_oos_validation_candidate_*.json"
    ],
    artifacts: [
      {
        kind: "log",
        path: "logs/auto_reoptimize_YYYY-MM-DD.log",
        description: "Pipeline log including comparison and promotion/revert decisions."
      }
    ],
    approvalHint: "high_risk"
  },
  // daily_automation removed from catalog during pi migration.
  // The old script auto-approved plans when approval_required=false,
  // bypassing the pi risk-gate workflow. Use the atlas-daily skill
  // (cli_ingest → cli_plan → approve → cli_paper_run → dashboard)
  // for all normal daily operations.
  {
    name: "dashboard_generate_data",
    category: "reporting",
    // RETIRED 2026-05-18: dashboard/generate_data.py no longer exists.
    // Dashboard data is served directly from FastAPI (services/api/dashboard.py)
    // with a 30 s in-process cache.  This entry is kept for catalog stability
    // (external callers that reference this job name get a no-op exit 0).
    summary: "[RETIRED 2026-05-18] No-op stub — dashboard is now served live from FastAPI (services/api/dashboard.py). Kept for backward compatibility; exits 0 with a log message.",
    commandPreview: `python -c "print('[dashboard_generate_data] retired — no-op'); sys.exit(0)"`,
    estimatedRuntimeSec: 5,
    reads: [],
    writes: [],
    artifacts: [],
    approvalHint: "safe"
  },
  {
    name: "cli_ingest",
    category: "cli",
    summary: "Refresh/download market data through Atlas CLI.",
    commandPreview: "python scripts/cli.py -m <market> ingest",
    estimatedRuntimeSec: 900,
    reads: ["config/active_config.json"],
    writes: ["data/cache/*.parquet"],
    artifacts: [],
    args: [
      {
        name: "market",
        type: "string",
        description: "Market ID: sp500, asx, or hk. Defaults to sp500."
      }
    ],
    approvalHint: "safe"
  },
  {
    name: "cli_universe",
    category: "cli",
    summary: "Build the trading universe through Atlas CLI.",
    commandPreview: "python scripts/cli.py -m <market> universe",
    estimatedRuntimeSec: 120,
    reads: ["config/active_config.json", "data/cache/*.parquet"],
    writes: ["data/processed/universe.json"],
    artifacts: [
      {
        kind: "report_json",
        path: "data/processed/universe.json",
        description: "Universe tickers and metadata generated by the universe builder."
      }
    ],
    args: [
      {
        name: "market",
        type: "string",
        description: "Market ID: sp500, asx, or hk. Defaults to sp500."
      }
    ],
    approvalHint: "safe"
  },
  {
    name: "cli_backtest",
    category: "cli",
    summary: "Run the walk-forward backtest via Atlas CLI.",
    commandPreview: "python scripts/cli.py -m <market> backtest",
    estimatedRuntimeSec: 1200,
    reads: ["config/active_config.json", "data/cache/*.parquet"],
    writes: ["backtest/results/backtest_*.json"],
    artifacts: [
      {
        kind: "metrics_json",
        path: "backtest/results/backtest_*.json",
        description: "Timestamped CLI backtest summary output."
      }
    ],
    args: [
      {
        name: "market",
        type: "string",
        description: "Market ID: sp500, asx, or hk. Defaults to sp500."
      }
    ],
    approvalHint: "review"
  },
  {
    name: "cli_plan",
    category: "cli",
    summary: "Generate the daily trade plan JSON (approval gate remains separate).",
    commandPreview: "python scripts/cli.py -m <market> plan [--date YYYY-MM-DD]",
    estimatedRuntimeSec: 300,
    reads: ["config/active_config.json", "data/cache/*.parquet", "paper_engine/portfolio_state.json"],
    writes: ["paper_engine/plans/plan_YYYY-MM-DD.json"],
    artifacts: [
      {
        kind: "plan_json",
        path: "paper_engine/plans/plan_YYYY-MM-DD.json",
        description: "Daily trade plan with proposed/rejected entries and risk summary."
      }
    ],
    args: [
      {
        name: "market",
        type: "string",
        description: "Market ID: sp500, asx, or hk. Defaults to sp500."
      },
      {
        name: "date",
        type: "string",
        description: "Trade date in YYYY-MM-DD. Defaults to local current date."
      }
    ],
    approvalHint: "review"
  },
  {
    name: "cli_approve",
    category: "cli",
    summary: "Approve a previously generated daily plan.",
    commandPreview: "python scripts/cli.py -m <market> approve [--date YYYY-MM-DD]",
    estimatedRuntimeSec: 30,
    reads: ["paper_engine/plans/plan_YYYY-MM-DD.json"],
    writes: ["paper_engine/plans/plan_YYYY-MM-DD.json"],
    args: [
      {
        name: "market",
        type: "string",
        description: "Market ID: sp500, asx, or hk. Defaults to sp500."
      },
      {
        name: "date",
        type: "string",
        description: "Trade date in YYYY-MM-DD."
      }
    ],
    artifacts: [],
    approvalHint: "high_risk"
  },
  {
    name: "cli_paper_run",
    category: "cli",
    summary: "Execute an approved daily trade plan via live broker (automated mode).",
    commandPreview: "python3 scripts/cli.py -m <market> live-run --auto [--date YYYY-MM-DD]",
    estimatedRuntimeSec: 120,
    reads: ["plans/plan_YYYY-MM-DD.json", "config/active/*.json", "data/cache/*.parquet"],
    writes: ["logs/live_executions.jsonl", "journal/trade_ledger.json", "journal/round_trips.jsonl"],
    args: [
      {
        name: "market",
        type: "string",
        description: "Market ID: sp500, asx, or hk. Defaults to sp500."
      },
      {
        name: "date",
        type: "string",
        description: "Trade date in YYYY-MM-DD."
      }
    ],
    artifacts: [
      {
        kind: "portfolio_state_json",
        path: "paper_engine/portfolio_state.json",
        description: "Updated paper portfolio state after execution."
      }
    ],
    approvalHint: "high_risk"
  },
  {
    name: "cli_status",
    category: "cli",
    summary: "Print current portfolio/config/data status via Atlas CLI.",
    commandPreview: "python scripts/cli.py -m <market> status",
    estimatedRuntimeSec: 60,
    reads: ["config/active_config.json", "paper_engine/portfolio_state.json", "data/cache/*.parquet"],
    writes: [],
    artifacts: [],
    args: [
      {
        name: "market",
        type: "string",
        description: "Market ID: sp500, asx, or hk. Defaults to sp500."
      }
    ],
    approvalHint: "safe"
  },
  {
    name: "cli_ledger",
    category: "cli",
    summary: "Print recent trade ledger summary via Atlas CLI.",
    commandPreview: "python scripts/cli.py -m <market> ledger [--days N]",
    estimatedRuntimeSec: 30,
    reads: ["journal/trade_ledger.json"],
    writes: [],
    args: [
      {
        name: "market",
        type: "string",
        description: "Market ID: sp500, asx, or hk. Defaults to sp500."
      },
      {
        name: "days",
        type: "number",
        description: "Trailing days to summarize; defaults to 30."
      }
    ],
    artifacts: [],
    approvalHint: "safe"
  },
  {
    name: "cli_eod_settlement",
    category: "cli",
    summary: "Run end-of-day settlement: refresh closing prices, check stop-loss/take-profit exits, update MAE/MFE, record equity snapshot, refresh dashboard.",
    commandPreview: "python scripts/eod_settlement.py -m <market> [--dry-run]",
    estimatedRuntimeSec: 300,
    reads: ["config/active_config.json", "paper_engine/portfolio_state.json", "data/cache/*.parquet"],
    writes: [
      "paper_engine/portfolio_state.json",
      "journal/trade_ledger.json",
      "journal/mistake_log.json",
      "dashboard/data/dashboard-data.json",
      "logs/eod_settlement.log"
    ],
    artifacts: [
      {
        kind: "portfolio_state_json",
        path: "paper_engine/portfolio_state.json",
        description: "Updated portfolio state after EOD settlement with equity snapshot and any stop/TP exits."
      },
      {
        kind: "log",
        path: "logs/eod_settlement.log",
        description: "EOD settlement run log."
      }
    ],
    args: [
      {
        name: "market",
        type: "string",
        description: "Market ID: sp500, asx, or hk. Defaults to sp500."
      },
      {
        name: "dryRun",
        type: "boolean",
        description: "If true, check exits and report without modifying state."
      }
    ],
    approvalHint: "review"
  },
  {
    name: "anneal_review",
    category: "research",
    summary: "Run the self-annealing review / hypothesis / experiment cycle.",
    commandPreview: "python scripts/cli.py -m <market> review",
    estimatedRuntimeSec: 1800,
    reads: ["journal/*.json", "config/active_config.json", "data/cache/*.parquet"],
    writes: ["journal/changelog.json", "config/config_v*.json", "config/active_config.json"],
    artifacts: [
      {
        kind: "report_json",
        path: "journal/changelog.json",
        description: "Annealing cycle changelog and promotion history."
      }
    ],
    args: [
      {
        name: "market",
        type: "string",
        description: "Market ID: sp500, asx, or hk. Defaults to sp500."
      }
    ],
    approvalHint: "high_risk"
  }
];

export const ATLAS_JOB_NAMES = ATLAS_JOB_CATALOG.map((job) => job.name);
