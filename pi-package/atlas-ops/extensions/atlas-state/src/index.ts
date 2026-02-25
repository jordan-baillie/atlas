import type { ExtensionAPI } from "@mariozechner/pi-coding-agent";
import { Type } from "@sinclair/typebox";
import {
  existsSync,
  mkdirSync,
  readFileSync,
  readdirSync,
  rmSync,
  statSync,
  writeFileSync
} from "node:fs";
import { randomBytes } from "node:crypto";
import { dirname, join, resolve } from "node:path";

type JsonRecord = Record<string, unknown>;

function nowIso(): string {
  return new Date().toISOString();
}

function ensureDir(path: string): void {
  mkdirSync(path, { recursive: true });
}

function readJson(path: string): unknown {
  return JSON.parse(readFileSync(path, "utf8"));
}

function writeJson(path: string, data: unknown): void {
  writeFileSync(path, `${JSON.stringify(data, null, 2)}\n`, "utf8");
}

function stateBaseDir(cwd?: string): string {
  return resolve(cwd ?? process.cwd(), ".pi", "atlas-state");
}

function sanitizeSegment(input: string): string {
  return input.replace(/[^a-zA-Z0-9_.-]+/g, "_");
}

function kvPath(cwd: string | undefined, scope: string | undefined, key: string): string {
  const scopeName = sanitizeSegment(scope?.trim() || "default");
  const fileName = `${sanitizeSegment(key)}.json`;
  return join(stateBaseDir(cwd), "kv", scopeName, fileName);
}

function lockPath(cwd: string | undefined, name: string): string {
  return join(stateBaseDir(cwd), "locks", `${sanitizeSegment(name)}.json`);
}

function readJsonIfExists<T>(path: string): T | null {
  if (!existsSync(path)) return null;
  try {
    return readJson(path) as T;
  } catch {
    return null;
  }
}

function lockIsExpired(lock: JsonRecord): boolean {
  const expiresAt = typeof lock.expiresAt === "string" ? Date.parse(lock.expiresAt) : NaN;
  return Number.isFinite(expiresAt) && Date.now() > expiresAt;
}

const PutSchema = Type.Object({
  key: Type.String({ minLength: 1 }),
  value: Type.Any(),
  scope: Type.Optional(Type.String({ minLength: 1 })),
  cwd: Type.Optional(Type.String())
});

const GetSchema = Type.Object({
  key: Type.String({ minLength: 1 }),
  scope: Type.Optional(Type.String({ minLength: 1 })),
  cwd: Type.Optional(Type.String())
});

const ListSchema = Type.Object({
  scope: Type.Optional(Type.String({ minLength: 1 })),
  prefix: Type.Optional(Type.String()),
  cwd: Type.Optional(Type.String()),
  limit: Type.Optional(Type.Number({ minimum: 1, maximum: 500 }))
});

const DeleteSchema = Type.Object({
  key: Type.String({ minLength: 1 }),
  scope: Type.Optional(Type.String({ minLength: 1 })),
  cwd: Type.Optional(Type.String())
});

const CorrelationSchema = Type.Object({
  prefix: Type.Optional(Type.String({ minLength: 1 })),
  metadata: Type.Optional(Type.Any()),
  cwd: Type.Optional(Type.String())
});

const LockAcquireSchema = Type.Object({
  name: Type.String({ minLength: 1 }),
  owner: Type.Optional(Type.String({ minLength: 1 })),
  ttlSec: Type.Optional(Type.Number({ minimum: 1 })),
  cwd: Type.Optional(Type.String()),
  stealExpired: Type.Optional(Type.Boolean())
});

const LockReleaseSchema = Type.Object({
  name: Type.String({ minLength: 1 }),
  owner: Type.Optional(Type.String({ minLength: 1 })),
  cwd: Type.Optional(Type.String()),
  force: Type.Optional(Type.Boolean())
});

const LockStatusSchema = Type.Object({
  name: Type.String({ minLength: 1 }),
  cwd: Type.Optional(Type.String())
});

export default function atlasStateExtension(pi: ExtensionAPI) {
  pi.registerTool({
    name: "atlas_state_put",
    label: "Atlas State Put",
    description: "Store arbitrary JSON state under .pi/atlas-state/kv/<scope>/<key>.json.",
    parameters: PutSchema,
    async execute(_toolCallId, params) {
      const path = kvPath(params.cwd, params.scope, params.key);
      ensureDir(dirname(path));
      const record = {
        key: params.key,
        scope: params.scope ?? "default",
        updatedAt: nowIso(),
        value: params.value
      };
      writeJson(path, record);
      return {
        content: [{ type: "text", text: `Stored atlas state key ${params.key}.` }],
        details: {
          key: params.key,
          scope: params.scope ?? "default",
          path,
          updatedAt: record.updatedAt
        }
      };
    }
  });

  pi.registerTool({
    name: "atlas_state_get",
    label: "Atlas State Get",
    description: "Load a JSON state record previously stored with atlas_state_put.",
    parameters: GetSchema,
    async execute(_toolCallId, params) {
      const path = kvPath(params.cwd, params.scope, params.key);
      const record = readJsonIfExists<JsonRecord>(path);
      return {
        content: [
          {
            type: "text",
            text: record ? `Loaded atlas state key ${params.key}.` : `Atlas state key ${params.key} not found.`
          }
        ],
        details: {
          key: params.key,
          scope: params.scope ?? "default",
          path,
          found: !!record,
          record
        }
      };
    }
  });

  pi.registerTool({
    name: "atlas_state_list",
    label: "Atlas State List",
    description: "List stored atlas state keys in a scope.",
    parameters: ListSchema,
    async execute(_toolCallId, params) {
      const scope = params.scope ?? "default";
      const dir = resolve(stateBaseDir(params.cwd), "kv", sanitizeSegment(scope));
      const files = existsSync(dir) ? readdirSync(dir).filter((f) => f.endsWith(".json")) : [];
      const prefix = params.prefix ?? "";
      const limit = Math.trunc(params.limit ?? 100);
      const items = files
        .map((name) => {
          const path = join(dir, name);
          const stat = statSync(path);
          const parsed = readJsonIfExists<JsonRecord>(path);
          const key = typeof parsed?.key === "string" ? parsed.key : name.replace(/\.json$/i, "");
          return {
            key,
            path,
            bytes: stat.size,
            mtime: stat.mtime.toISOString()
          };
        })
        .filter((item) => !prefix || item.key.startsWith(prefix))
        .sort((a, b) => b.mtime.localeCompare(a.mtime))
        .slice(0, limit);

      return {
        content: [{ type: "text", text: `Listed ${items.length} atlas state keys in scope=${scope}.` }],
        details: {
          scope,
          dir,
          count: items.length,
          items
        }
      };
    }
  });

  pi.registerTool({
    name: "atlas_state_delete",
    label: "Atlas State Delete",
    description: "Delete a stored atlas state key.",
    parameters: DeleteSchema,
    async execute(_toolCallId, params) {
      const path = kvPath(params.cwd, params.scope, params.key);
      const existed = existsSync(path);
      if (existed) rmSync(path, { force: true });
      return {
        content: [
          { type: "text", text: existed ? `Deleted atlas state key ${params.key}.` : `Atlas state key ${params.key} not found.` }
        ],
        details: {
          key: params.key,
          scope: params.scope ?? "default",
          path,
          deleted: existed
        }
      };
    }
  });

  pi.registerTool({
    name: "atlas_state_new_correlation",
    label: "Atlas State New Correlation",
    description: "Generate and persist a correlation ID for a multi-step workflow run.",
    parameters: CorrelationSchema,
    async execute(_toolCallId, params) {
      const prefix = sanitizeSegment(params.prefix ?? "corr");
      const id = `${prefix}_${Date.now()}_${randomBytes(4).toString("hex")}`;
      const path = kvPath(params.cwd, "correlations", id);
      ensureDir(dirname(path));
      const record = {
        key: id,
        scope: "correlations",
        createdAt: nowIso(),
        metadata: params.metadata ?? null
      };
      writeJson(path, record);
      return {
        content: [{ type: "text", text: `Created correlation ID ${id}.` }],
        details: { id, path, record }
      };
    }
  });

  pi.registerTool({
    name: "atlas_state_lock_acquire",
    label: "Atlas State Lock Acquire",
    description: "Acquire a persistent lock under .pi/atlas-state/locks to coordinate Pi workflows.",
    parameters: LockAcquireSchema,
    async execute(_toolCallId, params) {
      const path = lockPath(params.cwd, params.name);
      ensureDir(dirname(path));
      const owner = params.owner ?? "pi";
      const ttlSec = Math.trunc(params.ttlSec ?? 3600);
      const existing = readJsonIfExists<JsonRecord>(path);
      const expired = existing ? lockIsExpired(existing) : false;
      const stealExpired = params.stealExpired !== false;

      if (existing && !expired) {
        return {
          content: [{ type: "text", text: `Lock ${params.name} is already held.` }],
          details: {
            acquired: false,
            name: params.name,
            path,
            existing,
            expired: false
          }
        };
      }

      if (existing && expired && !stealExpired) {
        return {
          content: [{ type: "text", text: `Lock ${params.name} is expired but stealExpired=false.` }],
          details: {
            acquired: false,
            name: params.name,
            path,
            existing,
            expired: true
          }
        };
      }

      const now = new Date();
      const record = {
        name: params.name,
        owner,
        acquiredAt: now.toISOString(),
        expiresAt: new Date(now.getTime() + ttlSec * 1000).toISOString(),
        ttlSec
      };
      writeJson(path, record);
      return {
        content: [{ type: "text", text: `Acquired lock ${params.name}.` }],
        details: {
          acquired: true,
          name: params.name,
          path,
          record,
          replacedExpired: !!existing && expired
        }
      };
    }
  });

  pi.registerTool({
    name: "atlas_state_lock_release",
    label: "Atlas State Lock Release",
    description: "Release a persistent atlas state lock. Requires matching owner unless force=true.",
    parameters: LockReleaseSchema,
    async execute(_toolCallId, params) {
      const path = lockPath(params.cwd, params.name);
      const existing = readJsonIfExists<JsonRecord>(path);
      if (!existing) {
        return {
          content: [{ type: "text", text: `Lock ${params.name} is not present.` }],
          details: { released: false, name: params.name, path, reason: "not_found" }
        };
      }

      const existingOwner = typeof existing.owner === "string" ? existing.owner : undefined;
      if (!params.force && params.owner && existingOwner && existingOwner !== params.owner) {
        return {
          content: [{ type: "text", text: `Lock ${params.name} held by ${existingOwner}; owner mismatch.` }],
          details: {
            released: false,
            name: params.name,
            path,
            reason: "owner_mismatch",
            existing
          }
        };
      }

      rmSync(path, { force: true });
      return {
        content: [{ type: "text", text: `Released lock ${params.name}.` }],
        details: {
          released: true,
          name: params.name,
          path,
          previous: existing
        }
      };
    }
  });

  pi.registerTool({
    name: "atlas_state_lock_status",
    label: "Atlas State Lock Status",
    description: "Inspect a persistent atlas state lock.",
    parameters: LockStatusSchema,
    async execute(_toolCallId, params) {
      const path = lockPath(params.cwd, params.name);
      const existing = readJsonIfExists<JsonRecord>(path);
      return {
        content: [
          {
            type: "text",
            text: existing ? `Lock ${params.name} exists.` : `Lock ${params.name} does not exist.`
          }
        ],
        details: {
          name: params.name,
          path,
          exists: !!existing,
          expired: existing ? lockIsExpired(existing) : null,
          lock: existing
        }
      };
    }
  });
}
