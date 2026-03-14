/**
 * Atlas Safety Gates Extension
 *
 * Event: tool_call
 *
 * Intercepts dangerous tool calls before execution and either blocks them
 * or requires user confirmation. Prevents the entire class of "agent
 * accidentally broke things" incidents.
 *
 * Gates:
 * 1. Config protection — writes to config/active/ require confirmation
 * 2. Live trading guard — execution commands require confirmation
 * 3. Destructive operations — rm on protected dirs blocked unless confirmed
 * 4. State file protection — writes to paper_state when broker offline
 * 5. Service file guard — edits to running service files warn about restart
 */

import type { ExtensionAPI } from "@mariozechner/pi-coding-agent";
import { isToolCallEventType } from "@mariozechner/pi-coding-agent";

// ---------------------------------------------------------------------------
// Protected paths and patterns
// ---------------------------------------------------------------------------

const PROTECTED_DIRS = [
  "data/cache",
  "brain/",
  "reports/",
  "config/active/",
  "config/versions/",
  "logs/equity_curve",
];

const DESTRUCTIVE_PATTERNS = [
  /rm\s+(-\w+\s+)*-r/,           // rm -r, rm -rf, rm -fr
  /rm\s+(-\w+\s+)*--recursive/,  // rm --recursive
  /shred\s/,
  /truncate\s/,
];

const LIVE_EXECUTION_PATTERNS = [
  /live_executor/i,
  /scripts\/execute/i,
  /cli\.py\s+(paper-run|execute)/i,
  /scripts\/eod_settlement/i,
];

const STATE_FILE_PATTERNS = [
  /paper_state/i,
  /equity_curve.*\.json/i,
  /trade_log.*\.json/i,
];

// Map files to the service that owns them
const FILE_SERVICE_MAP: Array<{ pattern: RegExp; service: string }> = [
  { pattern: /strategies\//,                service: "atlas-research-runner" },
  { pattern: /scripts\/autoresearch/,       service: "atlas-research-runner" },
  { pattern: /dashboard\//,                 service: "atlas-dashboard" },
  { pattern: /scripts\/dashboard_loop/,     service: "atlas-dashboard-refresh" },
  { pattern: /scripts\/director/,           service: "atlas-director" },
  { pattern: /scripts\/telegram_bot/,       service: "atlas-telegram-bot" },
  { pattern: /research\/.*\.py$/,           service: "atlas-research-runner" },
];

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function normalizePath(path: string): string {
  return path.replace(/^@/, "").replace(/^\/root\/atlas\//, "");
}

function matchesProtectedDir(path: string): string | null {
  const normalized = normalizePath(path);
  for (const dir of PROTECTED_DIRS) {
    if (normalized.includes(dir)) return dir;
  }
  return null;
}

function matchesDestructiveCommand(command: string): boolean {
  return DESTRUCTIVE_PATTERNS.some(p => p.test(command));
}

function matchesLiveExecution(command: string): boolean {
  return LIVE_EXECUTION_PATTERNS.some(p => p.test(command));
}

function matchesStateFile(path: string): boolean {
  return STATE_FILE_PATTERNS.some(p => p.test(path));
}

function matchesServiceFile(path: string): { service: string } | null {
  const normalized = normalizePath(path);
  for (const { pattern, service } of FILE_SERVICE_MAP) {
    if (pattern.test(normalized)) return { service };
  }
  return null;
}

function isDestructiveBashOnProtectedDir(command: string): string | null {
  if (!matchesDestructiveCommand(command)) return null;
  for (const dir of PROTECTED_DIRS) {
    if (command.includes(dir)) return dir;
  }
  return null;
}

// ---------------------------------------------------------------------------
// Extension entry point
// ---------------------------------------------------------------------------

export default function atlasSafetyGates(pi: ExtensionAPI) {

  pi.on("tool_call", async (event, ctx) => {
    // Skip if no UI (print mode, headless)
    if (!ctx.hasUI) return;

    // -----------------------------------------------------------------------
    // Gate 1: Write/Edit to config/active/ — requires confirmation
    // -----------------------------------------------------------------------
    if (isToolCallEventType("write", event)) {
      const path = event.input.path ?? "";

      // Config protection
      if (normalizePath(path).includes("config/active/")) {
        const ok = await ctx.ui.confirm(
          "⚠️ Live Config Modification",
          `You're about to write to active trading config:\n  ${path}\n\nThis directly affects trading behavior. Continue?`
        );
        if (!ok) return { block: true, reason: "Config write cancelled by user — use config/candidates/ for staging." };
      }

      // State file protection (warn, don't block)
      if (matchesStateFile(path)) {
        ctx.ui.notify(
          `⚠️ Writing state file: ${path}\nEnsure broker connectivity before modifying trading state.`,
          "warning"
        );
      }

      // Service file warning
      const serviceMatch = matchesServiceFile(path);
      if (serviceMatch) {
        ctx.ui.notify(
          `ℹ️ File belongs to ${serviceMatch.service}.\nRemember to restart the service after changes: systemctl restart ${serviceMatch.service}`,
          "info"
        );
      }
    }

    if (isToolCallEventType("edit", event)) {
      const path = event.input.path ?? "";

      // Config protection
      if (normalizePath(path).includes("config/active/")) {
        const ok = await ctx.ui.confirm(
          "⚠️ Live Config Modification",
          `You're about to edit active trading config:\n  ${path}\n\nThis directly affects trading behavior. Continue?`
        );
        if (!ok) return { block: true, reason: "Config edit cancelled by user — use config/candidates/ for staging." };
      }

      // Service file warning
      const serviceMatch = matchesServiceFile(path);
      if (serviceMatch) {
        ctx.ui.notify(
          `ℹ️ File belongs to ${serviceMatch.service}.\nRemember to restart the service after changes: systemctl restart ${serviceMatch.service}`,
          "info"
        );
      }
    }

    // -----------------------------------------------------------------------
    // Gate 2: Bash — live execution, destructive ops
    // -----------------------------------------------------------------------
    if (isToolCallEventType("bash", event)) {
      const command = event.input.command ?? "";

      // Live trading guard — highest severity
      if (matchesLiveExecution(command)) {
        const ok = await ctx.ui.confirm(
          "🔴 LIVE TRADING COMMAND",
          `This command triggers real trade execution:\n\n  ${command.slice(0, 200)}\n\nThis will submit orders to Alpaca. Are you absolutely sure?`
        );
        if (!ok) return { block: true, reason: "Live trading command blocked by user." };
      }

      // Destructive operations on protected directories
      const protectedDir = isDestructiveBashOnProtectedDir(command);
      if (protectedDir) {
        const ok = await ctx.ui.confirm(
          "🗑️ Destructive Operation on Protected Directory",
          `This command will delete files in a protected location:\n\n  ${command.slice(0, 200)}\n\nProtected dir: ${protectedDir}\nThis data may not be recoverable. Continue?`
        );
        if (!ok) return { block: true, reason: `Destructive operation on ${protectedDir} blocked by user.` };
      }

      // Systemctl stop/disable on atlas services — warn
      if (/systemctl\s+(stop|disable)\s+atlas-/i.test(command)) {
        const ok = await ctx.ui.confirm(
          "⚠️ Service Stop/Disable",
          `Stopping an Atlas service:\n\n  ${command.slice(0, 200)}\n\nThis may affect trading operations. Continue?`
        );
        if (!ok) return { block: true, reason: "Service stop cancelled by user." };
      }

      // Config file overwrite via cp/mv
      if (/(?:cp|mv)\s.*config\/active\//i.test(command)) {
        const ok = await ctx.ui.confirm(
          "⚠️ Config File Overwrite",
          `This command modifies files in config/active/:\n\n  ${command.slice(0, 200)}\n\nUse the atlas_risk_promote_config tool instead for safe promotion. Continue anyway?`
        );
        if (!ok) return { block: true, reason: "Config overwrite via bash blocked — use risk gate tools." };
      }
    }
  });
}
