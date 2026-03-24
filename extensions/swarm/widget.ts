/**
 * TUI widget renderer for swarm agent live progress.
 *
 * Returns string[] lines for ctx.ui.setWidget() — raw ANSI strings,
 * no pi-tui component imports required.
 *
 * Example output (width=58):
 * ╭─ ⚡ Agents ── BUILDING ────────────── $0.87 ── 2m14s ─╮
 * │ ✓ scout-1    ██████████ 100%  done                $0.14 │
 * │ ◉ builder-1  ██████░░░░  62%  edit momentum.py    $0.38 │
 * │ ◉ builder-2  █████░░░░░  48%  bash npm test       $0.35 │
 * │ ○ reviewer-1 ░░░░░░░░░░   0%  waiting             $0.00 │
 * ╰─────────────────────────────────────────────────────────╯
 */

import type { SwarmAgent } from "./types.js";

// ── Widget-specific display types ──────────────────────────────────────────
// NOTE: These are exported so Builder 1 can re-export them from types.ts if desired.

export type AgentDisplayStatus =
  | "pending"
  | "running"
  | "completed"
  | "failed"
  | "merge_failed";

/** Serialisable display state for a single agent row. */
export interface AgentWidgetState {
  name: string;
  status: AgentDisplayStatus;
  /** 0–100 */
  progressPct: number;
  /** Short description of what the agent is currently doing, e.g. "edit foo.py" */
  currentAction: string;
  /** Cumulative cost in USD */
  cost: number;
}

/** Full state fed to renderAgentWidget(). */
export interface WidgetState {
  agents: AgentWidgetState[];
  /** Human-readable phase label, e.g. "BUILDING", "REVIEWING", "MERGING" */
  phase: string;
  /** Sum of all agent costs */
  totalCost: number;
  /** ISO timestamp of the overall run start */
  startedAt: string;
  /** ISO timestamp of the overall run end (null while in progress) */
  completedAt?: string | null;
}

// ── ANSI helpers ────────────────────────────────────────────────────────────

const R = "\x1b[0m"; // reset
const BOLD = "\x1b[1m";
const DIM = "\x1b[2m";

const C = {
  green: "\x1b[32m",
  yellow: "\x1b[33m",
  red: "\x1b[31m",
  cyan: "\x1b[36m",
  blue: "\x1b[34m",
  magenta: "\x1b[35m",
  white: "\x1b[37m",
  gray: "\x1b[90m",
  brightWhite: "\x1b[97m",
} as const;

/** Strip ANSI escape codes to measure visible character width. */
function visLen(s: string): number {
  // eslint-disable-next-line no-control-regex
  return s.replace(/\x1b\[[0-9;]*[mGKHF]/g, "").length;
}

/** Right-pad a plain string to `width` chars, truncating with … if over. */
function fixedWidth(s: string, width: number): string {
  if (s.length > width) return s.slice(0, width - 1) + "…";
  return s.padEnd(width);
}

/** Pad an ANSI-decorated string so its VISIBLE width reaches `width`. */
function padVis(s: string, width: number): string {
  const vl = visLen(s);
  if (vl >= width) return s;
  return s + " ".repeat(width - vl);
}

// ── Status icons & colours ────────────────────────────────────────────────

const STATUS_ICON: Record<AgentDisplayStatus, string> = {
  pending: "○",
  running: "◉",
  completed: "✓",
  failed: "✗",
  merge_failed: "⚠",
};

function statusColor(status: AgentDisplayStatus): string {
  switch (status) {
    case "completed": return C.green;
    case "running":   return C.cyan;
    case "failed":    return C.red;
    case "merge_failed": return C.yellow;
    case "pending":   return C.gray;
  }
}

// ── Core helpers ─────────────────────────────────────────────────────────

/**
 * Format elapsed time between two ISO timestamps (or from start to now).
 *
 * @param startIso  Start time as ISO 8601 string.
 * @param endIso    Optional end time; defaults to Date.now().
 * @returns         Human-readable elapsed string, e.g. "2m14s", "45s", "1h03m".
 */
export function formatWidgetElapsed(startIso: string, endIso?: string | null): string {
  const start = new Date(startIso).getTime();
  if (isNaN(start)) return "?";
  const end = endIso ? new Date(endIso).getTime() : Date.now();
  const totalSec = Math.max(0, Math.floor((end - start) / 1000));

  if (totalSec < 60) return `${totalSec}s`;

  const m = Math.floor(totalSec / 60);
  const s = totalSec % 60;
  if (m < 60) return `${m}m${s.toString().padStart(2, "0")}s`;

  const h = Math.floor(m / 60);
  const mm = m % 60;
  return `${h}h${mm.toString().padStart(2, "0")}m`;
}

/** Render a 10-character block-fill progress bar (█ filled, ░ empty). */
function progressBar(pct: number): string {
  const clamped = Math.max(0, Math.min(100, pct));
  const filled = Math.round((clamped / 100) * 10);
  return "█".repeat(filled) + "░".repeat(10 - filled);
}

// ── Main renderer ─────────────────────────────────────────────────────────

/**
 * Render a compact live progress panel for all swarm agents.
 *
 * @param state  Current widget state (agents, phase, totals).
 * @param width  Terminal width in columns (box will fill this exactly).
 * @param _theme  Pi theme object (unused — we emit raw ANSI directly).
 * @returns      Array of ANSI strings, one per line, all `width` chars wide.
 */
export function renderAgentWidget(
  state: WidgetState,
  width: number,
  _theme: unknown,
): string[] {
  const lines: string[] = [];
  // Inner content width (total minus 2 border chars ╭ and ╮)
  const inner = Math.max(10, width - 2);

  // ── Header line ────────────────────────────────────────────────────
  // Pattern: ╭─ ⚡ Agents ── PHASE ────── $X.XX ── Xs ─╮
  const elapsed = formatWidgetElapsed(state.startedAt, state.completedAt);
  const costStr = `\$${state.totalCost.toFixed(2)}`;
  const phaseStr = state.phase.toUpperCase();

  // Fixed visible components (without ANSI)
  const leftFixed  = `─ ⚡ Agents ── ${phaseStr} `;   // after ╭
  const rightFixed = ` ${costStr} ── ${elapsed} ─`;   // before ╮
  const dashes = "─".repeat(Math.max(0, inner - leftFixed.length - rightFixed.length));

  const headerContent =
    `─ ${BOLD}⚡ Agents${R} ── ` +
    `${C.cyan}${BOLD}${phaseStr}${R} ` +
    dashes +
    `${C.yellow}${costStr}${R}` +
    ` ── ${DIM}${elapsed}${R} ─`;

  lines.push(`╭${headerContent}╮`);

  // ── Agent rows ─────────────────────────────────────────────────────
  // Fixed per-row visible widths:
  //  " I " = 3   (space + icon + space)
  //  name  = nameCols
  //  " "   = 1
  //  bar   = 10
  //  " "   = 1
  //  pct   = 4   ("100%")
  //  "  "  = 2
  //  action = actionCols
  //  "  "  = 2
  //  cost  = 5   ("$X.XX")
  //  " "   = 1
  // Total fixed (excl name + action) = 3 + 1 + 10 + 1 + 4 + 2 + 2 + 5 + 1 = 29
  const NAME_COLS = 10;
  const FIXED_COLS = 3 + NAME_COLS + 1 + 10 + 1 + 4 + 2 + 2 + 5 + 1; // = 39
  const actionCols = Math.max(6, inner - FIXED_COLS);

  for (const agent of state.agents) {
    const col = statusColor(agent.status);
    const icon = `${col}${STATUS_ICON[agent.status]}${R}`;
    const nameRaw = fixedWidth(agent.name, NAME_COLS);
    const name = agent.status === "running"
      ? `${C.brightWhite}${nameRaw}${R}`
      : `${C.gray}${nameRaw}${R}`;

    const bar = agent.status === "completed"
      ? `${C.green}${progressBar(agent.progressPct)}${R}`
      : agent.status === "running"
      ? `${C.cyan}${progressBar(agent.progressPct)}${R}`
      : `${C.gray}${progressBar(agent.progressPct)}${R}`;

    const pctRaw = `${Math.round(agent.progressPct).toString().padStart(3)}%`;
    const pct = `${DIM}${pctRaw}${R}`;

    const actionRaw = fixedWidth(agent.currentAction || "waiting", actionCols);
    const action = `${DIM}${actionRaw}${R}`;

    const costRaw = `\$${agent.cost.toFixed(2)}`.padStart(5);
    const cost = `${C.gray}${costRaw}${R}`;

    // Assemble visible row (without border chars)
    const rowInner = ` ${icon} ${name} ${bar} ${pct}  ${action}  ${cost} `;

    // Pad to inner width accounting for ANSI codes
    const padded = padVis(rowInner, inner);
    lines.push(`│${padded}│`);
  }

  // ── Footer ─────────────────────────────────────────────────────────
  lines.push(`╰${"─".repeat(inner)}╯`);

  return lines;
}

// ── Conversion helper ─────────────────────────────────────────────────────

/**
 * Convert a runtime SwarmAgent to an AgentWidgetState for rendering.
 *
 * @param agent          Live SwarmAgent from the orchestrator.
 * @param currentAction  Overrides activity log auto-detection (optional).
 */
export function agentToWidgetState(
  agent: SwarmAgent,
  currentAction?: string,
): AgentWidgetState {
  // "merged" is a terminal success state — display as completed
  const displayStatus: AgentDisplayStatus =
    agent.status === "merged" ? "completed" : (agent.status as AgentDisplayStatus);

  // Progress: use explicit field if set, else infer from terminal states
  let progress = agent.progressPct ?? 0;
  if (agent.status === "completed" || agent.status === "merged") progress = 100;
  if (agent.status === "pending") progress = 0;

  // Best-guess current action from activity log
  let action = currentAction;
  if (!action && agent.activityLog && agent.activityLog.length > 0) {
    action = agent.activityLog[agent.activityLog.length - 1].summary;
  }
  if (!action) {
    action = agent.status === "pending" ? "waiting"
           : agent.status === "completed" || agent.status === "merged" ? "done"
           : agent.status === "failed" ? (agent.failReason ?? "failed")
           : agent.status === "merge_failed" ? "merge failed"
           : "running";
  }

  return {
    name: agent.name,
    status: displayStatus,
    progressPct: Math.max(0, Math.min(100, Math.round(progress))),
    currentAction: action,
    cost: agent.usage.cost,
  };
}
