// Forge API types — match shapes from services/api/forge.py (GET /api/forge/state).
// All fields conservative; server returns null/empty for missing.

export interface ForgeStatus {
  running: boolean
  enabled: boolean
  next_run_ms: number | null
  next_run_str: string | null
  last_trigger_str: string | null
  last_cycle_ts: string | null
}

export interface ForgeCounts {
  cycles: number
  ran: number
  passes: number
  experiments: number
  sources: number
  candidates: number
  families: number
  wiki_pages: number
}

export interface ForgeFamily {
  family: string
  tier: string | null
  dsr: number | null
  promote_dsr: number | null
  n_families: number | null
  passed_all: boolean
}

export interface ForgeFdr {
  bar: number
  n_families: number
  families: ForgeFamily[]
  history: number[]
}

export interface ForgeStage {
  key: string
  label: string
  icon: string
  count: number
  sub: string
}

export type CycleStatus = 'pass' | 'fail' | 'error'

export interface ForgeCycle {
  ts: string | null
  id: string | null
  title: string
  premium: string
  market: string
  ran: boolean
  tier: string | null
  passed_all: boolean
  holdout_pass: boolean | null
  dsr: number | null
  status: CycleStatus
}

export interface ForgeCandidate {
  title: string
  tags: string
  summary: string
  data_note: string
  free: boolean
}

export interface ForgeState {
  generated_at: string
  status: ForgeStatus
  counts: ForgeCounts
  fdr: ForgeFdr
  pipeline: ForgeStage[]
  cycles: ForgeCycle[]
  candidates: ForgeCandidate[]
  log_tail: string[]
}
