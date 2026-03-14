/**
 * Atlas Context Injector Extension
 *
 * Events: session_start, before_agent_start
 *
 * On session_start:
 *   - Read system state (services, equity, config, alerts)
 *   - Display status widget with key metrics
 *
 * On before_agent_start:
 *   - Classify user prompt intent (research, trading, config, debugging, etc.)
 *   - Inject relevant context into system prompt so the agent starts oriented
 *
 * This eliminates the 2-5 minute orientation tax every session currently pays.
 */

import type { ExtensionAPI } from "@mariozechner/pi-coding-agent";
import { existsSync, readFileSync } from "node:fs";
import { join } from "node:path";

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function atlasRoot(): string {
  const envRoot = process.env.ATLAS_ROOT;
  if (envRoot) return envRoot;
  const cwd = process.cwd();
  if (existsSync(join(cwd, "config", "active"))) return cwd;
  return "/root/atlas";
}

function readJsonSafe<T>(path: string): T | null {
  try {
    if (!existsSync(path)) return null;
    return JSON.parse(readFileSync(path, "utf8")) as T;
  } catch {
    return null;
  }
}

function readTextSafe(path: string, maxLines = 50): string | null {
  try {
    if (!existsSync(path)) return null;
    const lines = readFileSync(path, "utf8").split("\n");
    return lines.slice(0, maxLines).join("\n");
  } catch {
    return null;
  }
}

// ---------------------------------------------------------------------------
// System state snapshot
// ---------------------------------------------------------------------------

interface ServiceStatus {
  name: string;
  active: boolean;
  status: string;
}

interface EquitySnapshot {
  date: string;
  equity: number;
  pnl: number;
  estimated?: boolean;
}

interface ConfigSnapshot {
  market: string;
  version: string;
  mode: string;
  approvalRequired: boolean;
  enabledStrategies: string[];
  maxPositions: number;
}

interface SystemSnapshot {
  services: ServiceStatus[];
  failedServices: string[];
  equity: Record<string, EquitySnapshot | null>;
  configs: ConfigSnapshot[];
  timestamp: string;
}

const ATLAS_SERVICES = [
  "atlas-dashboard",
  "atlas-dashboard-refresh",
  "atlas-telegram-bot",
  "atlas-director",
  "atlas-research-runner",
  "atlas-research-window",
];

const MARKETS = ["sp500", "asx"];

async function getSystemSnapshot(pi: ExtensionAPI): Promise<SystemSnapshot> {
  const root = atlasRoot();

  // Check services
  const services: ServiceStatus[] = [];
  const failedServices: string[] = [];
  try {
    const result = await pi.exec("systemctl", [
      "is-active",
      ...ATLAS_SERVICES,
    ], { timeout: 5000 });
    const statuses = result.stdout.trim().split("\n");
    for (let i = 0; i < ATLAS_SERVICES.length; i++) {
      const name = ATLAS_SERVICES[i];
      const status = statuses[i]?.trim() ?? "unknown";
      const active = status === "active";
      services.push({ name, active, status });
      if (!active) failedServices.push(name);
    }
  } catch {
    for (const name of ATLAS_SERVICES) {
      services.push({ name, active: false, status: "unknown" });
      failedServices.push(name);
    }
  }

  // Read equity curves
  const equity: Record<string, EquitySnapshot | null> = {};
  for (const market of MARKETS) {
    const curvePath = join(root, "logs", `equity_curve_${market}.json`);
    const curve = readJsonSafe<EquitySnapshot[]>(curvePath);
    equity[market] = curve && curve.length > 0 ? curve[curve.length - 1] : null;
  }

  // Read configs
  const configs: ConfigSnapshot[] = [];
  for (const market of MARKETS) {
    const configPath = join(root, "config", "active", `${market}.json`);
    const config = readJsonSafe<Record<string, unknown>>(configPath);
    if (config) {
      const trading = (config.trading as Record<string, unknown>) ?? {};
      const risk = (config.risk as Record<string, unknown>) ?? {};
      const strategies = (config.strategies as Record<string, unknown>) ?? {};
      const enabled = Object.entries(strategies)
        .filter(([, v]) => (v as Record<string, unknown>)?.enabled === true)
        .map(([k]) => k);
      configs.push({
        market,
        version: String(config.version ?? "unknown"),
        mode: String(trading.mode ?? "unknown"),
        approvalRequired: trading.approval_required === true,
        enabledStrategies: enabled,
        maxPositions: Number(risk.max_open_positions ?? 0),
      });
    }
  }

  return {
    services,
    failedServices,
    equity,
    configs,
    timestamp: new Date().toISOString(),
  };
}

// ---------------------------------------------------------------------------
// Intent classification
// ---------------------------------------------------------------------------

type Intent =
  | "research"
  | "trading"
  | "config"
  | "debugging"
  | "strategy"
  | "deployment"
  | "data"
  | "health"
  | "general";

const INTENT_PATTERNS: Array<{ intent: Intent; patterns: RegExp[] }> = [
  {
    intent: "research",
    patterns: [
      /backtest/i, /sweep/i, /research/i, /experiment/i, /anneal/i,
      /optimize/i, /reoptimize/i, /sharpe/i, /brain/i, /hypothesis/i,
    ],
  },
  {
    intent: "trading",
    patterns: [
      /trade/i, /order/i, /position/i, /broker/i, /alpaca/i,
      /execute/i, /approve.*plan/i, /plan.*approve/i, /paper.*trad/i,
      /live.*trad/i, /entry|exit/i,
    ],
  },
  {
    intent: "config",
    patterns: [
      /config/i, /promot/i, /active_config/i, /parameter/i,
      /candidate/i, /version/i, /rollback/i, /backup/i,
    ],
  },
  {
    intent: "debugging",
    patterns: [
      /error/i, /fix/i, /bug/i, /crash/i, /fail/i, /broken/i,
      /incident/i, /diagnose/i, /debug/i, /traceback/i, /exception/i,
    ],
  },
  {
    intent: "strategy",
    patterns: [
      /strategy/i, /signal/i, /BaseStrategy/i, /generate_signals/i,
      /momentum/i, /mean.?reversion/i, /trend/i, /connors/i,
      /sector.?rotation/i, /opening.?gap/i,
    ],
  },
  {
    intent: "deployment",
    patterns: [
      /deploy/i, /restart/i, /service/i, /systemctl/i, /systemd/i,
      /daemon/i, /cron/i, /timer/i,
    ],
  },
  {
    intent: "data",
    patterns: [
      /data/i, /ingest/i, /cache/i, /ticker/i, /universe/i,
      /stale/i, /refresh/i, /download/i, /yfinance/i,
    ],
  },
  {
    intent: "health",
    patterns: [
      /health/i, /status/i, /check/i, /audit/i, /monitor/i,
      /dashboard/i, /alert/i,
    ],
  },
];

function classifyIntent(prompt: string): Intent {
  let bestIntent: Intent = "general";
  let bestScore = 0;

  for (const { intent, patterns } of INTENT_PATTERNS) {
    let score = 0;
    for (const pattern of patterns) {
      if (pattern.test(prompt)) score++;
    }
    if (score > bestScore) {
      bestScore = score;
      bestIntent = intent;
    }
  }

  return bestIntent;
}

// ---------------------------------------------------------------------------
// Context injection
// ---------------------------------------------------------------------------

function buildInjection(intent: Intent, state: SystemSnapshot): string {
  const sections: string[] = [];

  // Always inject: system health summary
  const healthLine = state.failedServices.length === 0
    ? "🟢 All Atlas services healthy."
    : `🔴 Failed services: ${state.failedServices.join(", ")}`;
  sections.push(`## Atlas System State (auto-injected)\n${healthLine}`);

  // Always inject: equity & config summary
  const equityLines: string[] = [];
  for (const [market, eq] of Object.entries(state.equity)) {
    if (eq) {
      equityLines.push(`- ${market.toUpperCase()}: $${eq.equity.toFixed(2)} (PnL: $${eq.pnl.toFixed(2)}, ${eq.date})${eq.estimated ? " [estimated]" : ""}`);
    }
  }
  if (equityLines.length > 0) {
    sections.push(`### Portfolio\n${equityLines.join("\n")}`);
  }

  for (const cfg of state.configs) {
    sections.push(
      `### Config: ${cfg.market.toUpperCase()} ${cfg.version}\n` +
      `- Mode: ${cfg.mode} | Approval: ${cfg.approvalRequired} | Max positions: ${cfg.maxPositions}\n` +
      `- Strategies (${cfg.enabledStrategies.length}): ${cfg.enabledStrategies.join(", ")}`
    );
  }

  // Intent-specific context
  switch (intent) {
    case "research":
      sections.push(
        "### Research Context\n" +
        "- Backtests: `python scripts/cli.py backtest -m <market>` or use `atlas_jobs_run` tool\n" +
        "- Brain knowledge base: `brain/` directory (check INDEX.md for prior results)\n" +
        "- Research queue: `research/queue/` for pending experiments\n" +
        "- Key metrics to track: Sharpe, CAGR, max drawdown, profit factor, trade count\n" +
        "- LESSON: Always check brain/ before running a backtest to avoid re-testing"
      );
      break;

    case "trading":
      sections.push(
        "### Trading Context\n" +
        "- Broker: Alpaca (ACTIVE account, commission-free)\n" +
        "- Paper engine: `paper_engine/` directory\n" +
        "- Plan flow: ingest → plan → approve → execute\n" +
        "- CLI: `scripts/cli.py [ingest|plan|approve|paper-run|status|ledger]`\n" +
        "- LESSON: Never write paper_state files when broker is offline\n" +
        "- LESSON: Always verify plan status is APPROVED before execution"
      );
      break;

    case "config":
      sections.push(
        "### Config Context\n" +
        "- Active configs: `config/active/sp500.json`, `config/active/asx.json`\n" +
        "- Candidates: `config/candidates/`\n" +
        "- Backups: `config/versions/active_config_pre_reopt_*.json`\n" +
        "- Promotion flow: validate OOS → risk gate check → backup → copy → verify\n" +
        "- Use `atlas_risk_check_config_promotion` tool before any promotion\n" +
        "- LESSON: Always bump config version when promoting"
      );
      break;

    case "debugging":
      sections.push(
        "### Debugging Context\n" +
        "- Logs directory: `logs/` (healthz, intraday, equity curves)\n" +
        "- Service logs: `journalctl -u atlas-<service> --no-pager -n 50`\n" +
        "- Common issues:\n" +
        "  - OOM kills on research-runner (check memory limits)\n" +
        "  - Stale data cache causing bad signals (refresh with ingest)\n" +
        "  - Strategy API drift (check generate_signals signature)\n" +
        "- LESSON: Check journalctl first, then code, then config"
      );
      break;

    case "strategy":
      sections.push(
        "### Strategy Context\n" +
        "- Base class: `strategies/base_strategy.py` (BaseStrategy)\n" +
        "- Required methods: `generate_signals(data, config) -> DataFrame`\n" +
        "- Strategy dir: `strategies/` (one file per strategy)\n" +
        "- Test: `python -c \"from strategies.X import X; X()\"` for import check\n" +
        "- LESSON: Dormant strategies drift — always test import after editing\n" +
        "- LESSON: Strategy changes require service restart if research-runner is active"
      );
      break;

    case "deployment":
      sections.push(
        "### Deployment Context\n" +
        "- Services: " + ATLAS_SERVICES.join(", ") + "\n" +
        "- Service files: `/etc/systemd/system/atlas-*.service`\n" +
        "- Restart: `systemctl restart atlas-<name>`\n" +
        "- File → Service mapping:\n" +
        "  - `strategies/*` → atlas-research-runner\n" +
        "  - `dashboard/*` → atlas-dashboard, atlas-dashboard-refresh\n" +
        "  - `scripts/director_cron.py` → atlas-director\n" +
        "  - `scripts/telegram_bot.py` → atlas-telegram-bot\n" +
        "- LESSON: Always restart affected services after code changes"
      );
      break;

    case "data":
      sections.push(
        "### Data Context\n" +
        "- Cache: `data/cache/` (yfinance price data)\n" +
        "- Ingest: `python scripts/cli.py ingest -m <market>`\n" +
        "- Universe: `data/universe_sp500.json`, `data/universe_asx.json`\n" +
        "- Check freshness: look at cache file mtimes\n" +
        "- LESSON: Always ingest fresh data before backtesting or plan generation"
      );
      break;

    case "health":
      sections.push(
        "### Health Check Context\n" +
        "- Quick check: `python scripts/health_check.py`\n" +
        "- Full audit: use atlas-healthz skill\n" +
        "- Dashboard: https://localhost:8501 (auth-protected)\n" +
        "- Telegram alerts: check bot for recent messages\n" +
        "- Services: " + state.services.map(s => `${s.active ? "🟢" : "🔴"} ${s.name}`).join(", ")
      );
      break;
  }

  return sections.join("\n\n");
}

// ---------------------------------------------------------------------------
// Widget formatting
// ---------------------------------------------------------------------------

function formatStatusWidget(state: SystemSnapshot): string[] {
  const lines: string[] = [];

  // Line 1: Equity summary
  const eqParts: string[] = [];
  for (const [market, eq] of Object.entries(state.equity)) {
    if (eq) {
      const pnlSign = eq.pnl >= 0 ? "+" : "";
      eqParts.push(`${market.toUpperCase()} $${eq.equity.toFixed(2)} (${pnlSign}$${eq.pnl.toFixed(2)})`);
    }
  }
  if (eqParts.length > 0) {
    lines.push(`💰 ${eqParts.join("  |  ")}`);
  }

  // Line 2: Config versions + strategies
  const cfgParts: string[] = [];
  for (const cfg of state.configs) {
    cfgParts.push(`${cfg.market.toUpperCase()} ${cfg.version} (${cfg.enabledStrategies.length} strategies, ${cfg.mode})`);
  }
  if (cfgParts.length > 0) {
    lines.push(`📊 ${cfgParts.join("  |  ")}`);
  }

  // Line 3: Service health
  if (state.failedServices.length === 0) {
    lines.push(`🟢 All ${state.services.length} services healthy`);
  } else {
    const active = state.services.filter(s => s.active).length;
    lines.push(`🔴 ${state.failedServices.length} failed: ${state.failedServices.map(s => s.replace("atlas-", "")).join(", ")}  (${active}/${state.services.length} up)`);
  }

  return lines;
}

// ---------------------------------------------------------------------------
// Extension entry point
// ---------------------------------------------------------------------------

// Cache the snapshot so we don't re-read on every turn
let cachedSnapshot: SystemSnapshot | null = null;
let snapshotAge = 0;
const SNAPSHOT_TTL_MS = 5 * 60 * 1000; // 5 minutes

export default function atlasContextInjector(pi: ExtensionAPI) {

  async function getOrRefreshSnapshot(): Promise<SystemSnapshot> {
    const now = Date.now();
    if (cachedSnapshot && (now - snapshotAge) < SNAPSHOT_TTL_MS) {
      return cachedSnapshot;
    }
    cachedSnapshot = await getSystemSnapshot(pi);
    snapshotAge = now;
    return cachedSnapshot;
  }

  // --- session_start: Display status widget ---
  pi.on("session_start", async (_event, ctx) => {
    try {
      const state = await getOrRefreshSnapshot();
      const widgetLines = formatStatusWidget(state);
      ctx.ui.setWidget("atlas-context", widgetLines);
    } catch (err) {
      // Don't crash session on widget failure
      ctx.ui.setWidget("atlas-context", [`⚠️ Atlas context failed: ${(err as Error).message}`]);
    }
  });

  // --- before_agent_start: Inject context into system prompt ---
  pi.on("before_agent_start", async (event, ctx) => {
    try {
      const prompt = event.prompt ?? "";
      if (!prompt.trim()) return;

      const intent = classifyIntent(prompt);
      const state = await getOrRefreshSnapshot();
      const injection = buildInjection(intent, state);

      // Also refresh widget
      const widgetLines = formatStatusWidget(state);
      ctx.ui.setWidget("atlas-context", widgetLines);

      return {
        systemPrompt: event.systemPrompt + "\n\n" + injection,
      };
    } catch {
      // Silently fail — don't block the agent
      return;
    }
  });
}
