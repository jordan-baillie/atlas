// Types for GET /api/forge/map — the research wiki as a lineage graph.

export type MapNodeStatus = 'pass' | 'near_miss' | 'fail' | 'closed' | 'queued' | 'claimed' | 'other'
export type MapEdgeKind = 'refine' | 'orthogonal' | 'crossover' | 'pairs_with'

export interface MapNodeMetrics {
  dsr?: number
  search_sharpe?: number
  holdout_sharpe?: number
  full_sharpe?: number
  maxdd?: number
  cpcv?: number
  pbo?: number
}

export interface MapNode {
  id: string
  rid?: string
  page: string | null
  title: string
  status: MapNodeStatus
  status_raw?: string
  family: string
  lane: string
  markets: string[]
  date: string | null
  ts: string | null
  project?: string | null
  metrics: MapNodeMetrics
  prereg?: string | null
  tier?: string | null
  dsr?: number | null
  bar_at_test?: number | null
  arm?: string | null
  agent?: string | null
  elite: boolean
}

export interface MapEdge {
  source: string
  target: string
  kind: MapEdgeKind
  inferred: boolean
}

export interface MapLane {
  id: string
  label: string
  total: number
  fail: number
  near_miss: number
  pass: number
  queued: number
  premia_note?: string | null
}

export interface EliteCell {
  cell: string | null
  fitness: number | null
  title: string
  strategy_id: string | null
  ts?: string | null
}

export interface MapStats {
  experiments: number
  queued: number
  fails: number
  near_misses: number
  passes: number
  lanes: number
  families_burned: number
  fdr_bar: number
  elite_cells: number
  edges: number
  explicit_edges: number
}

export interface ResearchMapData {
  generated_at: string
  error?: string
  stats: MapStats
  lanes: MapLane[]
  nodes: MapNode[]
  edges: MapEdge[]
  elite_grid: EliteCell[]
}
