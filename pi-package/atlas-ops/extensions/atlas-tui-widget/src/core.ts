/**
 * Atlas TUI Widget — Pure core logic
 *
 * No external imports. All functions are pure and testable in isolation.
 * The extension entry point (index.ts) wires these functions to the Pi
 * runtime and provides @mariozechner/pi-tui utilities for ANSI-safe rendering.
 */

// ─── Constants ────────────────────────────────────────────────────────────────

/** Maximum activity entries kept in memory (bounded growth). */
export const MAX_ACTIVITY = 5;

/** Minimum terminal width to render a useful widget. */
export const MIN_WIDTH = 40;

const WIDGET_ID = "atlas-tui";
export { WIDGET_ID };

// ─── Types ────────────────────────────────────────────────────────────────────

export type ActivityStatus = "running" | "success" | "error";

export interface ActivityEntry {
  toolCallId: string;
  toolName: string;
  /** One-line summary of tool arguments (truncated, newlines stripped). */
  args: string;
  status: ActivityStatus;
  startMs: number;
  /** Set when status transitions out of "running". */
  durationMs?: number;
}

export interface TuiState {
  enabled: boolean;
  sessionStartMs: number;
  toolTotal: number;
  toolSuccess: number;
  toolError: number;
  delegations: number;
  /** Completed entries, oldest first; bounded to MAX_ACTIVITY. */
  recentActivity: ActivityEntry[];
  /** In-flight tools by toolCallId. */
  activeTools: Map<string, ActivityEntry>;
}

/** Minimal theme interface — matches ctx.ui.theme but mockable for testing. */
export interface Theme {
  fg: (color: string, text: string) => string;
}

/**
 * Width-truncation functions injected at render time.
 * In production these come from @mariozechner/pi-tui; in tests use mocks.
 */
export interface WidthFns {
  /** Truncate `str` to at most `width` visible columns, appending `ellipsis` if cut. */
  truncate: (str: string, width: number, ellipsis?: string) => string;
  /** Return the visible column width of `str` (strips ANSI escape codes). */
  visible: (str: string) => number;
}

// ─── State factory ────────────────────────────────────────────────────────────

export function createState(): TuiState {
  return {
    enabled: true,
    sessionStartMs: Date.now(),
    toolTotal: 0,
    toolSuccess: 0,
    toolError: 0,
    delegations: 0,
    recentActivity: [],
    activeTools: new Map(),
  };
}

// ─── Pure helpers ─────────────────────────────────────────────────────────────

/**
 * Format a millisecond duration as a human-readable string.
 *   < 1 s  → "450ms"
 *   1–59 s → "4.2s"
 *   ≥ 60 s → "1:30"
 */
export function fmtDuration(ms: number): string {
  if (ms < 1000) return `${Math.round(ms)}ms`;
  if (ms < 60_000) return `${(ms / 1000).toFixed(1)}s`;
  const m = Math.floor(ms / 60_000);
  const s = Math.floor((ms % 60_000) / 1000);
  return `${m}:${s.toString().padStart(2, "0")}`;
}

/** Elapsed time from startMs to now. */
export function fmtElapsed(startMs: number): string {
  return fmtDuration(Date.now() - startMs);
}

/**
 * Extract a one-line summary from tool args.
 * Priority: path > command > query > task > objective > first string value.
 */
export function summarizeArgs(args: Record<string, unknown>): string {
  if (typeof args !== "object" || args === null) return "";
  const raw =
    args["path"] ??
    args["command"] ??
    args["query"] ??
    args["task"] ??
    args["objective"] ??
    Object.values(args)[0];
  if (raw === undefined || raw === null) return "";
  return String(raw).replace(/\n/g, " ").slice(0, 80);
}

/**
 * Returns true if the tool is a delegation (agent dispatch) tool.
 * These are shown with a distinct icon and colour in the activity feed.
 *
 * Includes atlas_elastic_run because it spawns parallel pi CLI agents
 * (read-only burst) or queues write agent dispatch — both are delegation activities.
 */
export function isDelegationTool(toolName: string): boolean {
  return (
    toolName === "subagent" ||
    toolName === "swarm" ||
    toolName === "atlas_elastic_run" ||
    toolName.startsWith("delegate")
  );
}

/**
 * Select the theme color name for a given activity status.
 *   running → "warning"   (amber)
 *   success → "success"   (green)
 *   error   → "error"     (red)
 */
export function statusColor(status: ActivityStatus): string {
  switch (status) {
    case "running": return "warning";
    case "success": return "success";
    case "error":   return "error";
  }
}

/** Single visible character for a given status. */
export function statusIcon(status: ActivityStatus): string {
  switch (status) {
    case "running": return "⟳";
    case "success": return "✓";
    case "error":   return "✗";
  }
}

/**
 * Row icon for an activity entry.
 * Delegation tools running use "→" to distinguish from regular tool "⟳".
 */
export function rowIcon(status: ActivityStatus, isDelegation: boolean): string {
  if (isDelegation && status === "running") return "→";
  return statusIcon(status);
}

/** Push an entry into a bounded list, evicting the oldest when full. */
export function pushBounded<T>(list: T[], item: T, maxLen: number): void {
  list.push(item);
  if (list.length > maxLen) list.shift();
}

/**
 * Defensively cap `activeTools` to prevent unbounded growth if
 * `tool_execution_end` events are missed (e.g. aborted sessions).
 * Evicts entries in Map insertion order (oldest first).
 *
 * Default limit: 4 × MAX_ACTIVITY, giving generous headroom for
 * concurrent operations while still bounding memory.
 */
export function capActiveTools(
  state: TuiState,
  maxSize: number = MAX_ACTIVITY * 4,
): void {
  while (state.activeTools.size > maxSize) {
    const firstKey = state.activeTools.keys().next().value;
    if (firstKey === undefined) break;
    state.activeTools.delete(firstKey);
  }
}

// ─── Renderer ─────────────────────────────────────────────────────────────────

/**
 * Render the widget lines for the given state and terminal width.
 *
 * Layout:
 *   ◆ idle  │  agents 0  │  tools 17  │  errors 0  │  elapsed 02:34
 *   ───────────────────────────────────────────────────────────────────
 *     ✓ Read              memory/SUMMARY.md                      200ms
 *     → subagent          researcher — deep analysis             1.2s…
 *
 * Phase:   "idle" when no tools in-flight; "working" when active tools exist.
 * Agents:  "active/total" delegation tools when any have run this session
 *           (e.g., "1/2"); plain "0" when none have run.
 * Tools:   total tool calls this session.
 * Errors:  total errors this session.
 * Elapsed: session wall-clock time.
 *
 * Delegation tools (subagent/swarm) use "→" while running (vs "⟳" for tools).
 *
 * @param widthFns - Width utilities (injected so tests can mock them).
 *
 * Every line is guaranteed to be ≤ width via `widthFns.truncate()`.
 */
export function renderWidget(
  state: TuiState,
  theme: Theme,
  width: number,
  widthFns: WidthFns,
): string[] {
  if (width < MIN_WIDTH) return [];

  const { truncate, visible } = widthFns;

  const dim     = (t: string) => theme.fg("dim",     t);
  const accent  = (t: string) => theme.fg("accent",  t);
  const success = (t: string) => theme.fg("success", t);
  const err     = (t: string) => theme.fg("error",   t);
  const warning = (t: string) => theme.fg("warning", t);
  const muted   = (t: string) => theme.fg("muted",   t);
  const border  = (t: string) => theme.fg("border",  t);

  const lines: string[] = [];

  // ── Header / metrics bar ───────────────────────────────────────────────────
  const sep        = dim(" │ ");
  const elapsedStr = fmtElapsed(state.sessionStartMs);

  // Phase: working whenever any tool is in-flight
  const isWorking = state.activeTools.size > 0;
  const phaseStr  = isWorking ? warning("working") : dim("idle");

  // Agents: active in-flight / total this session
  const agentsActive = [...state.activeTools.values()].filter(
    (e) => isDelegationTool(e.toolName),
  ).length;
  const agentsTotal = state.delegations;
  const agentsStr =
    agentsTotal === 0
      ? dim("0")
      : agentsActive > 0
        ? warning(String(agentsActive)) + dim(`/${agentsTotal}`)
        : dim(`0/${agentsTotal}`);

  const headerLine =
    `${accent("◆")} ${phaseStr}${sep}` +
    `${dim("agents")} ${agentsStr}${sep}` +
    `${dim("tools")} ${muted(String(state.toolTotal))}${sep}` +
    `${state.toolError > 0 ? err("errors " + state.toolError) : dim("errors 0")}${sep}` +
    `${dim("elapsed")} ${dim(elapsedStr)}`;

  lines.push(truncate(headerLine, width));

  // ── Thin separator ─────────────────────────────────────────────────────────
  // Wrapped with truncate() to honour the width-safety contract.
  lines.push(truncate(border("─".repeat(width)), width));

  // ── Activity feed ──────────────────────────────────────────────────────────
  // Running tools first (most urgent), then completed in reverse-chronological.
  const runningEntries   = Array.from(state.activeTools.values());
  const completedEntries = [...state.recentActivity].reverse();
  const feed: ActivityEntry[] = [...runningEntries, ...completedEntries].slice(0, MAX_ACTIVITY);

  if (feed.length === 0) {
    lines.push(truncate(dim("  idle"), width));
  } else {
    for (const entry of feed) {
      const isAgent  = isDelegationTool(entry.toolName);
      const icon     = rowIcon(entry.status, isAgent);
      const iconColor =
        isAgent && entry.status === "running" ? "accent" : statusColor(entry.status);

      //
      // Row layout (visible widths):
      //   "  X tool________________  args_____________________  dur____"
      //    2  1 1 TOOL_W           2  argsCol                2  DUR_W
      //
      const INDENT   = 2;
      const ICON_W   = 1;
      const ICON_SEP = 1;
      const TOOL_W   = 16;
      const COL_SEP  = 2;
      const DUR_W    = 7;
      const FIXED_W  = INDENT + ICON_W + ICON_SEP + TOOL_W + COL_SEP + COL_SEP + DUR_W;
      const argsCol  = Math.max(8, width - FIXED_W);

      const toolTrunc = truncate(entry.toolName, TOOL_W);
      const toolPad   = " ".repeat(Math.max(0, TOOL_W - visible(toolTrunc)));

      const argsTrunc = truncate(entry.args, argsCol, "…");
      const argsPad   = " ".repeat(Math.max(0, argsCol - visible(argsTrunc)));

      const durRaw   = entry.durationMs !== undefined
        ? fmtDuration(entry.durationMs)
        : fmtElapsed(entry.startMs) + "…";
      const durTrunc  = truncate(durRaw, DUR_W);
      const durPadStr = " ".repeat(Math.max(0, DUR_W - visible(durTrunc)));
      const dur =
        (entry.durationMs !== undefined ? dim(durTrunc) : warning(durTrunc)) + durPadStr;

      const row =
        " ".repeat(INDENT) +
        theme.fg(iconColor, icon) + " " +
        (isAgent ? warning(toolTrunc) : accent(toolTrunc)) + toolPad + " ".repeat(COL_SEP) +
        muted(argsTrunc) + argsPad + " ".repeat(COL_SEP) +
        dur;

      lines.push(truncate(row, width));
    }
  }

  return lines;
}
