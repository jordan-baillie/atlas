#!/usr/bin/env node
/**
 * Atlas Computer-Use MCP Server
 *
 * Exposes screen-control tools over stdio using the Model Context Protocol.
 * Requires: Xvfb running on :99, scrot, xdotool installed.
 *
 * Tools:
 *   screenshot  — capture screen, return base64 PNG
 *   click       — move mouse and click at (x, y)
 *   type_text   — type a string via xdotool
 *   scroll      — scroll up/down N clicks
 *   key_press   — send a key combo (e.g. "Return", "ctrl+a")
 */

import { Server } from "@modelcontextprotocol/sdk/server/index.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import {
  CallToolRequestSchema,
  ListToolsRequestSchema,
} from "@modelcontextprotocol/sdk/types.js";
import { execSync } from "child_process";
import { readFileSync } from "fs";

const DISPLAY = process.env.DISPLAY || ":99";
const EXEC_TIMEOUT_MS = 10_000;
const SCREENSHOT_PATH = "/tmp/cu_screenshot.png";

/** Run a shell command. Returns stdout string or throws with stderr. */
function run(cmd) {
  process.stderr.write(`[mcp] exec: ${cmd}\n`);
  return execSync(cmd, {
    timeout: EXEC_TIMEOUT_MS,
    encoding: "utf8",
    env: { ...process.env, DISPLAY },
  });
}

/** Capture the screen and return a base64 PNG content block. */
function screenshot() {
  run(`DISPLAY=${DISPLAY} scrot -o ${SCREENSHOT_PATH}`);
  const data = readFileSync(SCREENSHOT_PATH);
  const b64 = data.toString("base64");
  process.stderr.write(`[mcp] screenshot captured (${data.length} bytes)\n`);
  return {
    type: "image",
    data: b64,
    mimeType: "image/png",
  };
}

// ─── Tool definitions ────────────────────────────────────────────────────────

const TOOLS = [
  {
    name: "screenshot",
    description: "Capture the current screen state and return it as a PNG image.",
    inputSchema: {
      type: "object",
      properties: {},
      required: [],
    },
  },
  {
    name: "click",
    description: "Move the mouse to (x, y) and click. Use button 3 for right-click.",
    inputSchema: {
      type: "object",
      properties: {
        x: { type: "number", description: "X coordinate in pixels" },
        y: { type: "number", description: "Y coordinate in pixels" },
        button: {
          type: "number",
          description: "Mouse button: 1=left (default), 3=right",
          default: 1,
        },
      },
      required: ["x", "y"],
    },
  },
  {
    name: "type_text",
    description: "Type a string of text at the current cursor position.",
    inputSchema: {
      type: "object",
      properties: {
        text: { type: "string", description: "Text to type" },
      },
      required: ["text"],
    },
  },
  {
    name: "scroll",
    description: "Scroll up or down by N clicks at the current mouse position.",
    inputSchema: {
      type: "object",
      properties: {
        direction: {
          type: "string",
          enum: ["up", "down"],
          description: "'up' or 'down'",
        },
        clicks: {
          type: "number",
          description: "Number of scroll clicks (default: 3)",
          default: 3,
        },
      },
      required: ["direction"],
    },
  },
  {
    name: "key_press",
    description:
      "Send a key or key combo (e.g. 'Return', 'ctrl+a', 'Tab', 'BackSpace', 'alt+F4').",
    inputSchema: {
      type: "object",
      properties: {
        combo: {
          type: "string",
          description: "xdotool key name or combo, e.g. 'Return', 'ctrl+c'",
        },
      },
      required: ["combo"],
    },
  },
];

// ─── Tool handlers ───────────────────────────────────────────────────────────

function handleScreenshot(_args) {
  const img = screenshot();
  return { content: [img] };
}

function handleClick({ x, y, button = 1 }) {
  const btn = Number(button);
  run(`DISPLAY=${DISPLAY} xdotool mousemove --sync ${x} ${y}`);
  run(`DISPLAY=${DISPLAY} xdotool click ${btn}`);
  return {
    content: [
      { type: "text", text: `Clicked button ${btn} at (${x}, ${y})` },
    ],
  };
}

function handleTypeText({ text }) {
  // Escape single quotes in text for shell safety
  const escaped = text.replace(/'/g, "'\\''");
  run(`DISPLAY=${DISPLAY} xdotool type --clearmodifiers --delay 30 '${escaped}'`);
  return {
    content: [{ type: "text", text: `Typed ${text.length} characters` }],
  };
}

function handleScroll({ direction, clicks = 3 }) {
  // xdotool button 4 = scroll up, 5 = scroll down
  const btn = direction === "up" ? 4 : 5;
  const n = Math.max(1, Math.round(Number(clicks)));
  for (let i = 0; i < n; i++) {
    run(`DISPLAY=${DISPLAY} xdotool click ${btn}`);
  }
  return {
    content: [
      { type: "text", text: `Scrolled ${direction} ${n} click(s)` },
    ],
  };
}

function handleKeyPress({ combo }) {
  run(`DISPLAY=${DISPLAY} xdotool key --clearmodifiers '${combo}'`);
  return {
    content: [{ type: "text", text: `Pressed key: ${combo}` }],
  };
}

// ─── Server setup ─────────────────────────────────────────────────────────────

const server = new Server(
  { name: "atlas-computer-use", version: "1.0.0" },
  { capabilities: { tools: {} } }
);

server.setRequestHandler(ListToolsRequestSchema, async () => ({
  tools: TOOLS,
}));

server.setRequestHandler(CallToolRequestSchema, async (request) => {
  const { name, arguments: args } = request.params;
  process.stderr.write(`[mcp] tool called: ${name} args=${JSON.stringify(args)}\n`);

  try {
    switch (name) {
      case "screenshot":
        return handleScreenshot(args || {});
      case "click":
        return handleClick(args);
      case "type_text":
        return handleTypeText(args);
      case "scroll":
        return handleScroll(args);
      case "key_press":
        return handleKeyPress(args);
      default:
        return {
          content: [{ type: "text", text: `Unknown tool: ${name}` }],
          isError: true,
        };
    }
  } catch (err) {
    const msg = err?.message || String(err);
    process.stderr.write(`[mcp] ERROR in ${name}: ${msg}\n`);
    return {
      content: [{ type: "text", text: `Error executing ${name}: ${msg}` }],
      isError: true,
    };
  }
});

// ─── Start ───────────────────────────────────────────────────────────────────

const transport = new StdioServerTransport();
await server.connect(transport);
process.stderr.write(
  `[mcp] Atlas computer-use server started (DISPLAY=${DISPLAY})\n`
);
