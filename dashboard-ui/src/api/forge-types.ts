// Forge API types — match services/api/forge.py (GET /api/forge/state).

export interface ForgeStatus {
  running: boolean
  enabled: boolean
  next_run_str: string | null
  last_trigger_str: string | null
  last_cycle_ts: string | null
}

export interface ForgeSummary {
  cycles: number
  ran: number
  passes: number
  fails: number
  errors: number
  pass_rate: string
  experiments: number
  sources: number
  candidates: number
  families: number
  wiki_pages: number
  fdr_bar: number
  best_holdout_sharpe: number | null
}

export interface ForgeFamily {
  family: string
  tier: string | null
  dsr: number | null
  passed_all: boolean
}

export interface ForgeFdr {
  bar: number
  n_families: number
  families: ForgeFamily[]
  history: number[]
}

export interface StageStat {
  label: string
  value: number | string
}

export interface ForgeStage {
  key: string
  label: string
  icon: string
  count: number
  accent: boolean
  stats: StageStat[]
}

export type CycleStatus = 'pass' | 'fail' | 'error'

export interface CycleMetrics {
  search_sharpe: number | null
  holdout_sharpe: number | null
  degradation_pct: number | null
  holdout_pass: boolean | null
  holdout_reasons: string[]
  full_sharpe: number | null
  full_maxdd: number | null
  n_trades: number | null
  dsr: number | null
  median_cpcv: number | null
  pbo: number | null
  deployment_passed: boolean | null
  promote_bar: number | null
  n_families: number | null
}

export interface ForgeCycle {
  ts: string | null
  id: string | null
  title: string
  status: CycleStatus
  ran: boolean
  tier: string | null
  passed_all: boolean
  family: string | null
  premium: string | null
  market: string | null
  hypothesis: {
    signal_approach: string | null
    why_not_duplicate: string | null
    pairs_with: string | null
    prior: string | null
  }
  data: {
    free_or_owned: string | null
    data_source: string | null
    gate0_data_check: string | null
  }
  metrics: CycleMetrics
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
  summary: ForgeSummary
  fdr: ForgeFdr
  pipeline: ForgeStage[]
  cycles: ForgeCycle[]
  candidates: ForgeCandidate[]
  log_tail: string[]
}
