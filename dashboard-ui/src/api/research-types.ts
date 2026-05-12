// Research API types
// All fields optional (?) following project convention — API responses may be
// partial when data is unavailable or endpoints are still being populated.

// ---------------------------------------------------------------------------
// GET /api/research/summary
// ---------------------------------------------------------------------------
export interface StrategySummary {
  strategy?: string
  total?: number
  kept?: number
  best_sharpe?: number
}

export interface ResearchSummary {
  total_experiments?: number
  kept_count?: number
  keep_rate?: number
  experiments_7d?: number
  strategies_count?: number
  last_research_ts?: string
  by_strategy?: StrategySummary[]
}

// ---------------------------------------------------------------------------
// GET /api/research/strategies
// ---------------------------------------------------------------------------
export interface StrategyDetail {
  strategy?: string
  total_experiments?: number
  kept_count?: number
  best_sharpe?: number
  best_cagr?: number
  last_improvement?: string
  best_params?: Record<string, unknown>
  // Research integrity fields (Task A — 2026-05-12)
  is_solo?: boolean | null
  solo_fraction?: number | null
  contamination_note?: string | null
}

export interface ResearchStrategies {
  strategies?: StrategyDetail[]
}

// ---------------------------------------------------------------------------
// GET /api/research/timeline?days=30
// ---------------------------------------------------------------------------
export interface TimelinePoint {
  date?: string
  experiments?: number
  best_sharpe?: number
  kept?: number
}

export interface ResearchTimeline {
  dates?: string[]
  series?: Record<string, TimelinePoint[]>
}

// ---------------------------------------------------------------------------
// GET /api/research/experiments?limit=50&strategy=X&status=Y
// ---------------------------------------------------------------------------
export interface Experiment {
  id?: string
  strategy?: string
  universe?: string
  experiment_type?: string
  params_changed?: string
  description?: string
  sharpe?: number
  trades?: number
  max_dd_pct?: number
  profit_factor?: number
  cagr_pct?: number
  status?: string
  recommendation?: string
  baseline_sharpe?: number | null
  runtime_s?: number | null
  agent_id?: string
  created_at?: string
  completed_at?: string
}

export interface ResearchExperiments {
  experiments?: Experiment[]
  total?: number
  page?: number
  page_size?: number
}

// ---------------------------------------------------------------------------
// GET /api/research/brain
// ---------------------------------------------------------------------------
export interface BrainParam {
  param_name?: string
  tests?: number
  strategies_tested?: number
  improved?: number
  avg_sharpe_delta?: number
}

export interface ResearchBrain {
  params?: BrainParam[]
  patterns?: unknown[]
}

// ---------------------------------------------------------------------------
// GET /api/research/discoveries
// ---------------------------------------------------------------------------
export interface Discovery {
  id?: number
  run_date?: string
  papers_found?: number
  papers_filtered?: number
  specs_extracted?: number
  strategies_generated?: number
  paper_titles?: string[]
  status?: string
  created_at?: string
}

export interface ResearchDiscoveries {
  discoveries?: Discovery[]
}

// ---------------------------------------------------------------------------
// GET /api/research/overview
// ---------------------------------------------------------------------------
export interface UniverseStrategyInfo {
  best_sharpe?: number
  experiments?: number
  kept?: number
}

export interface UniverseInfo {
  id?: string
  mode?: string
  priority?: string
  best_sharpe?: number
  total_experiments?: number
  experiments_today?: number
  kept_today?: number
  keep_rate?: number
  strategies?: Record<string, UniverseStrategyInfo>
  top_strategies?: Array<{ strategy?: string; best_sharpe?: number; trades?: number }>
  last_experiment?: string
  windows_per_day?: number
}

export interface DailyCount {
  date?: string
  count?: number
  kept?: number
}

export interface EngineStatus {
  status?: string
  total_experiments_all_time?: number
  experiments_today?: number
  kept_all_time?: number
  daily_counts?: DailyCount[]
}

export interface ResearchOverview {
  universes?: UniverseInfo[]
  engine?: EngineStatus
}

// ---------------------------------------------------------------------------
// GET /api/research/leaderboard
// ---------------------------------------------------------------------------
export interface LeaderboardEntry {
  strategy?: string
  universe?: string
  sharpe?: number
  trades?: number
  max_dd_pct?: number
  total_experiments?: number
}

export interface ResearchLeaderboard {
  leaderboard?: LeaderboardEntry[]
}

// ---------------------------------------------------------------------------
// GET /api/research/coverage
// ---------------------------------------------------------------------------
export type CoverageCellStatus = 'fresh' | 'stale' | 'very_stale'

export interface CoverageCell {
  sharpe: number | null
  trades: number | null
  updated_at: string | null
  age_days: number | null
  status: CoverageCellStatus
}

export interface ResearchCoverage {
  strategies: string[]
  universes: string[]
  matrix: Record<string, Record<string, CoverageCell | null>>
  generated_at: string
}

// ---------------------------------------------------------------------------
// GET /api/promotions/pending
// ---------------------------------------------------------------------------
export interface PendingPromotion {
  pending_id: string
  strategy: string
  market: string
  delta_sharpe: number
  final_sharpe: number
  timestamp: string
  metadata: Record<string, unknown>
  status: string
}

export interface PendingPromotionsResponse {
  pending: PendingPromotion[]
  count: number
}
