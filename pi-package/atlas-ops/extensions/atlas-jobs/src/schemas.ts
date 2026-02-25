import { Type } from "@sinclair/typebox";
import { ATLAS_JOB_NAMES } from "./catalog";

const jobLiterals = ATLAS_JOB_NAMES.map((name) => Type.Literal(name));

export const AtlasJobNameSchema =
  jobLiterals.length === 1 ? jobLiterals[0] : Type.Union(jobLiterals);

export const AtlasJobRunRequestSchema = Type.Object({
  job: AtlasJobNameSchema,
  args: Type.Optional(
    Type.Record(
      Type.String(),
      Type.Union([Type.String(), Type.Number(), Type.Boolean()])
    )
  ),
  cwd: Type.Optional(
    Type.String({
      description: "Atlas workspace root. Defaults to current project cwd."
    })
  ),
  timeoutSec: Type.Optional(
    Type.Number({
      minimum: 1,
      maximum: 43200,
      description: "Hard timeout in seconds for the job process."
    })
  ),
  dryRun: Type.Optional(
    Type.Boolean({
      description: "Validate resolution and command construction without spawning a process."
    })
  ),
  idempotencyKey: Type.Optional(
    Type.String({
      description: "Optional caller-supplied key to deduplicate retries in a future state backend."
    })
  )
});

export const AtlasJobGetSchema = Type.Object({
  runId: Type.String({ minLength: 1 }),
  includeStdoutTail: Type.Optional(Type.Boolean()),
  includeStderrTail: Type.Optional(Type.Boolean())
});

export const AtlasJobListRunsSchema = Type.Object({
  job: Type.Optional(AtlasJobNameSchema),
  status: Type.Optional(
    Type.Union([
      Type.Literal("queued"),
      Type.Literal("running"),
      Type.Literal("succeeded"),
      Type.Literal("failed"),
      Type.Literal("canceled"),
      Type.Literal("not_implemented")
    ])
  ),
  limit: Type.Optional(Type.Number({ minimum: 1, maximum: 200, default: 20 }))
});

export const AtlasJobCancelSchema = Type.Object({
  runId: Type.String({ minLength: 1 }),
  reason: Type.Optional(Type.String())
});
