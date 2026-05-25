/**
 * Atlas TUI Widget — Pi Extension Entry Point
 *
 * Renders a compact live-activity dashboard above the Pi editor.
 * This is the SINGLE consolidated persistent dashboard for the workspace.
 *
 * Display:
 *   ◆ idle  │  agents 0  │  tools 17  │  errors 0  │  elapsed 02:34
 *   ◆ working  │  agents 1/2  │  tools 5  │  errors 0  │  elapsed 00:12
 *   ──────────────────────────────────────────────────────────────────
 *     ✓ Read              memory/SUMMARY.md                      200ms
 *     ✓ Bash              git status --short                      89ms
 *     → subagent          researcher — deep analysis task         1.2s…
 *     ✗ Write             tasks/todo.md                           12ms
 *
 * Phase shows "working" when tools are in-flight, "idle" otherwise.
 * Agents shows active/total delegation tool calls (e.g., "1/2"); "0" when none.
 * Delegation tools (subagent/swarm) use "→" while running.
 *
 * Commands:
 *   /atlas-tui         — Toggle widget on/off
 *   /atlas-tui reset   — Clear stats, restart session timer
 *
 * Width safety: every rendered line is passed through truncateToWidth().
 * Non-interactive mode: all UI calls are gated on ctx.hasUI.
 *
 * Pure core logic (state, render) lives in ./core.ts for testability
 * without a Pi runtime dependency.
 */

import type { ExtensionAPI } from "@mariozechner/pi-coding-agent";
import { truncateToWidth, visibleWidth } from "@mariozechner/pi-tui";

import {
  WIDGET_ID,
  capActiveTools,
  createState,
  isDelegationTool,
  pushBounded,
  renderWidget,
  summarizeArgs,
  type ActivityEntry,
  type Theme,
  type TuiState,
  type WidthFns,
  MAX_ACTIVITY,
} from "./core";

// Pi-TUI width functions wired once.
const piWidthFns: WidthFns = {
  truncate: truncateToWidth,
  visible: visibleWidth,
};

// ─── Extension entry point ────────────────────────────────────────────────────

export default function atlasTuiWidget(pi: ExtensionAPI) {
  let state: TuiState = createState();
  /**
   * Captured from the widget factory so event handlers can request re-renders
   * without rebuilding the factory closure via a second setWidget() call.
   */
  let requestRender: (() => void) | undefined;

  // ── Widget mount / unmount ──────────────────────────────────────────────────

  type UiCtx = {
    ui: {
      setWidget: (
        id: string,
        factory:
          | ((
              tui: { requestRender(): void },
              theme: Theme,
            ) => { render(w: number): string[]; invalidate(): void })
          | undefined,
        opts?: { placement?: string },
      ) => void;
      theme: Theme;
    };
  };

  function mountWidget(ctx: UiCtx) {
    if (!state.enabled) return;

    ctx.ui.setWidget(
      WIDGET_ID,
      (tui, theme) => {
        requestRender = () => tui.requestRender();
        return {
          render: (width: number) =>
            renderWidget(state, theme, width, piWidthFns),
          invalidate: () => {},
        };
      },
      { placement: "aboveEditor" },
    );
  }

  function unmountWidget(ctx: {
    ui: { setWidget(id: string, f: undefined): void };
  }) {
    ctx.ui.setWidget(WIDGET_ID, undefined);
    requestRender = undefined;
  }

  // ── Session lifecycle ───────────────────────────────────────────────────────

  pi.on("session_start", async (_event, ctx) => {
    if (!ctx.hasUI) return;
    state = createState();
    mountWidget(ctx);
  });

  pi.on("session_shutdown", async (_event, ctx) => {
    if (!ctx.hasUI) return;
    unmountWidget(ctx);
  });

  // ── Agent lifecycle ─────────────────────────────────────────────────────────

  pi.on("agent_start", async (_event, ctx) => {
    if (!ctx.hasUI || !state.enabled) return;
    requestRender?.();
  });

  pi.on("agent_end", async (_event, ctx) => {
    if (!ctx.hasUI || !state.enabled) return;
    requestRender?.();
  });

  // ── Tool lifecycle ──────────────────────────────────────────────────────────

  pi.on("tool_execution_start", async (event, ctx) => {
    if (!ctx.hasUI || !state.enabled) return;

    const entry: ActivityEntry = {
      toolCallId: event.toolCallId,
      toolName: event.toolName,
      args: summarizeArgs((event.args as Record<string, unknown>) ?? {}),
      status: "running",
      startMs: Date.now(),
    };

    state.activeTools.set(event.toolCallId, entry);
    state.toolTotal++;

    // Defensive cap: prevent unbounded growth if tool_execution_end is missed.
    capActiveTools(state);

    // Count delegation tools (subagent, swarm, delegate*, atlas_elastic_run, etc.)
    // Uses isDelegationTool() from core.ts so this list stays in sync with the renderer.
    if (isDelegationTool(event.toolName)) {
      state.delegations++;
    }

    requestRender?.();
  });

  pi.on("tool_execution_end", async (event, ctx) => {
    if (!ctx.hasUI || !state.enabled) return;

    const entry = state.activeTools.get(event.toolCallId);
    if (entry) {
      entry.status = event.isError ? "error" : "success";
      entry.durationMs = Date.now() - entry.startMs;
      state.activeTools.delete(event.toolCallId);

      if (event.isError) {
        state.toolError++;
      } else {
        state.toolSuccess++;
      }

      pushBounded(state.recentActivity, entry, MAX_ACTIVITY);
    }

    requestRender?.();
  });

  // ── Toggle command ──────────────────────────────────────────────────────────

  pi.registerCommand("atlas-tui", {
    description: "Toggle Atlas TUI dashboard widget. Sub-commands: reset",
    handler: async (args, ctx) => {
      if (!ctx.hasUI) {
        return; // silently no-op — ctx.ui is unavailable in non-interactive mode
      }

      const sub = args.trim().toLowerCase();

      if (sub === "reset") {
        state = createState();
        mountWidget(ctx);
        ctx.ui.notify("Atlas TUI reset", "info");
        return;
      }

      state.enabled = !state.enabled;

      if (state.enabled) {
        mountWidget(ctx);
        ctx.ui.notify("Atlas TUI enabled", "info");
      } else {
        unmountWidget(ctx);
        ctx.ui.notify("Atlas TUI hidden — /atlas-tui to restore", "info");
      }
    },
  });
}
