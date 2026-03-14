/**
 * Atlas Commands Extension
 *
 * Registers slash commands for quick dispatch of common Atlas operations.
 * Each command sends a user message that triggers the appropriate skill or workflow.
 *
 * Commands are usable from:
 *   - Pi interactive session: /healthz
 *   - Pi RPC / automation: prompt("/healthz")
 *   - Other extensions: pi.sendUserMessage("/healthz")
 *
 * Also registers keyboard shortcuts for the most frequent operations.
 */

import type { ExtensionAPI } from "@mariozechner/pi-coding-agent";

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

const MARKETS = ["sp500", "asx", "hk"];

function marketCompletions(prefix: string) {
  const filtered = MARKETS.filter(m => m.startsWith(prefix.toLowerCase()));
  return filtered.length > 0 ? filtered.map(m => ({ value: m, label: m })) : null;
}

// ---------------------------------------------------------------------------
// Extension entry point
// ---------------------------------------------------------------------------

export default function atlasCommands(pi: ExtensionAPI) {

  // -------------------------------------------------------------------------
  // /healthz — System health audit
  // -------------------------------------------------------------------------
  pi.registerCommand("healthz", {
    description: "Run Atlas system health audit",
    handler: async (_args, ctx) => {
      pi.sendUserMessage(
        "Run the atlas-healthz skill — full system health audit. " +
        "Check services, broker, data freshness, config, cron jobs, and research. " +
        "Report findings and fix any auto-fixable issues. " +
        "Send summary via Telegram when done.",
        { deliverAs: "followUp" }
      );
      ctx.ui.notify("🏥 Health audit dispatched", "info");
    },
  });

  // -------------------------------------------------------------------------
  // /backtest — Quick backtest dispatch
  // -------------------------------------------------------------------------
  pi.registerCommand("backtest", {
    description: "Run a backtest: /backtest <strategy|market> [options]",
    getArgumentCompletions: (prefix) => {
      const strategies = [
        "momentum_breakout", "mean_reversion", "trend_following",
        "opening_gap", "sector_rotation", "short_term_mr", "connors_rsi2",
      ];
      const all = [...MARKETS, ...strategies];
      const filtered = all.filter(s => s.startsWith(prefix.toLowerCase()));
      return filtered.length > 0 ? filtered.map(s => ({ value: s, label: s })) : null;
    },
    handler: async (args, ctx) => {
      const target = args.trim() || "sp500";
      pi.sendUserMessage(
        `Run a backtest for: ${target}\n\n` +
        "Steps:\n" +
        "1. Check brain/ INDEX.md for prior results on this target\n" +
        "2. Ensure data cache is fresh (ingest if stale)\n" +
        "3. Run the backtest using atlas_jobs_run tool\n" +
        "4. Poll for completion with atlas_jobs_get\n" +
        "5. Summarize results with atlas_artifacts_summarize\n" +
        "6. Record findings to brain/ if novel\n" +
        "7. Report key metrics: Sharpe, CAGR, max DD, profit factor, trade count",
        { deliverAs: "followUp" }
      );
      ctx.ui.notify(`📈 Backtest dispatched: ${target}`, "info");
    },
  });

  // -------------------------------------------------------------------------
  // /deploy — Restart affected services
  // -------------------------------------------------------------------------
  pi.registerCommand("deploy", {
    description: "Deploy changes — detect and restart affected Atlas services",
    handler: async (_args, ctx) => {
      pi.sendUserMessage(
        "Deploy Atlas changes:\n\n" +
        "1. Run `git diff --name-only HEAD~1` to see recently changed files\n" +
        "2. Map changed files to Atlas services:\n" +
        "   - strategies/* → atlas-research-runner\n" +
        "   - dashboard/* → atlas-dashboard, atlas-dashboard-refresh\n" +
        "   - scripts/director* → atlas-director\n" +
        "   - scripts/telegram_bot* → atlas-telegram-bot\n" +
        "3. For each affected service: `systemctl restart <service>`\n" +
        "4. Wait 5 seconds, then verify with `systemctl is-active <service>`\n" +
        "5. If any service fails, check `journalctl -u <service> --no-pager -n 20`\n" +
        "6. Report results",
        { deliverAs: "followUp" }
      );
      ctx.ui.notify("🚀 Deploy dispatched", "info");
    },
  });

  // -------------------------------------------------------------------------
  // /promote — Config promotion pipeline
  // -------------------------------------------------------------------------
  pi.registerCommand("promote", {
    description: "Config promotion pipeline: /promote <market>",
    getArgumentCompletions: (prefix) => marketCompletions(prefix),
    handler: async (args, ctx) => {
      const market = args.trim() || "sp500";
      pi.sendUserMessage(
        `Run config promotion pipeline for: ${market}\n\n` +
        "Steps:\n" +
        "1. Identify candidate config in config/candidates/\n" +
        "2. Run atlas_risk_check_config_promotion tool (candidate vs active)\n" +
        "3. If verdict=block, STOP and report blockers\n" +
        "4. If verdict=review, show warnings and ask for confirmation\n" +
        "5. If verdict=allow, run atlas_risk_promote_config with confirmed=true\n" +
        "6. Verify the new active config loads correctly\n" +
        "7. Restart affected services\n" +
        "8. Report promotion result with before/after versions",
        { deliverAs: "followUp" }
      );
      ctx.ui.notify(`📦 Promote dispatched: ${market}`, "info");
    },
  });

  // -------------------------------------------------------------------------
  // /incident — Diagnose and fix errors
  // -------------------------------------------------------------------------
  pi.registerCommand("incident", {
    description: "Diagnose and fix an issue: /incident <error or symptom>",
    handler: async (args, ctx) => {
      const error = args.trim() || "unknown issue — check logs for recent errors";
      pi.sendUserMessage(
        `Diagnose and fix Atlas incident:\n\nReported issue: ${error}\n\n` +
        "Steps:\n" +
        "1. Check service status: `systemctl status atlas-* --no-pager`\n" +
        "2. Check recent logs: `journalctl -u atlas-<service> --no-pager -n 50`\n" +
        "3. Check application logs: `ls -lt logs/ | head -10` then read recent ones\n" +
        "4. Identify root cause — match against known patterns:\n" +
        "   - OOM kill → increase memory limit in service file\n" +
        "   - Strategy import error → check generate_signals signature\n" +
        "   - Stale data → run ingest\n" +
        "   - Config parse error → validate JSON\n" +
        "   - Broker connection → check APCA keys and API status\n" +
        "5. Apply fix\n" +
        "6. Verify fix works (restart service, check logs)\n" +
        "7. Update tasks/lessons.md if this is a new pattern\n" +
        "8. Report resolution via Telegram",
        { deliverAs: "followUp" }
      );
      ctx.ui.notify(`🔧 Incident dispatched: ${error.slice(0, 50)}`, "info");
    },
  });

  // -------------------------------------------------------------------------
  // /report — Performance report
  // -------------------------------------------------------------------------
  pi.registerCommand("report", {
    description: "Generate performance report",
    handler: async (_args, ctx) => {
      pi.sendUserMessage(
        "Generate Atlas performance report:\n\n" +
        "1. Read equity curves from logs/equity_curve_*.json\n" +
        "2. Read trade history from paper_engine/ state files\n" +
        "3. Calculate key metrics: total return, Sharpe, max drawdown, win rate\n" +
        "4. Read brain/ for research progress this week\n" +
        "5. Check config versions for any promotions this week\n" +
        "6. Generate summary with charts if possible\n" +
        "7. Send formatted report via Telegram",
        { deliverAs: "followUp" }
      );
      ctx.ui.notify("📊 Report dispatched", "info");
    },
  });

  // -------------------------------------------------------------------------
  // /daily — Daily trading operations
  // -------------------------------------------------------------------------
  pi.registerCommand("daily", {
    description: "Daily trading operations: /daily <market>",
    getArgumentCompletions: (prefix) => marketCompletions(prefix),
    handler: async (args, ctx) => {
      const market = args.trim() || "sp500";
      pi.sendUserMessage(
        `Run the atlas-daily skill for market: ${market}\n\n` +
        "Follow the skill's procedure for daily operations:\n" +
        "data refresh → plan generation → risk summary → plan approval → execution → dashboard refresh.\n" +
        "Pause at each approval gate and wait for confirmation.",
        { deliverAs: "followUp" }
      );
      ctx.ui.notify(`📅 Daily ops dispatched: ${market}`, "info");
    },
  });

  // -------------------------------------------------------------------------
  // /research — Research query/dispatch
  // -------------------------------------------------------------------------
  pi.registerCommand("research", {
    description: "Research dispatch: /research <query or hypothesis>",
    handler: async (args, ctx) => {
      const query = args.trim() || "review current research queue and suggest next experiment";
      pi.sendUserMessage(
        `Atlas research task: ${query}\n\n` +
        "Context:\n" +
        "1. Check brain/ INDEX.md for prior results\n" +
        "2. Check research/queue/ for pending experiments\n" +
        "3. Review closed decisions in brain/decisions/\n" +
        "4. Use atlas_jobs_run for backtests, atlas_artifacts_summarize for results\n" +
        "5. Record all findings to brain/ with proper metadata",
        { deliverAs: "followUp" }
      );
      ctx.ui.notify(`🔬 Research dispatched: ${query.slice(0, 50)}`, "info");
    },
  });

  // -------------------------------------------------------------------------
  // /brain — Knowledge base query
  // -------------------------------------------------------------------------
  pi.registerCommand("brain", {
    description: "Query research knowledge base: /brain <query>",
    handler: async (args, ctx) => {
      const query = args.trim() || "summarize latest findings";
      pi.sendUserMessage(
        `Query Atlas brain knowledge base: ${query}\n\n` +
        "Search through:\n" +
        "1. brain/INDEX.md for overview\n" +
        "2. brain/experiments/ for test results\n" +
        "3. brain/decisions/ for closed decisions\n" +
        "4. brain/patterns/ for confirmed patterns\n" +
        "5. tasks/lessons.md for operational lessons\n" +
        "Present findings in a clear summary.",
        { deliverAs: "followUp" }
      );
      ctx.ui.notify(`🧠 Brain query: ${query.slice(0, 50)}`, "info");
    },
  });

  // -------------------------------------------------------------------------
  // Keyboard shortcuts
  // -------------------------------------------------------------------------
  pi.registerShortcut("ctrl+shift+h", {
    description: "Quick Atlas health check",
    handler: async (_ctx) => {
      pi.sendUserMessage("/healthz", { deliverAs: "followUp" });
    },
  });

  pi.registerShortcut("ctrl+shift+d", {
    description: "Deploy Atlas changes",
    handler: async (_ctx) => {
      pi.sendUserMessage("/deploy", { deliverAs: "followUp" });
    },
  });

  pi.registerShortcut("ctrl+shift+b", {
    description: "Quick backtest (default market)",
    handler: async (_ctx) => {
      pi.sendUserMessage("/backtest sp500", { deliverAs: "followUp" });
    },
  });
}
