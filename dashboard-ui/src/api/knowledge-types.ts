// Types for /api/knowledge/* endpoints (Phase 4 + Track 3a).
// Mirrors the shapes returned by services/api/knowledge.py.

export type Severity = 'critical' | 'major' | 'minor'
export type Resolution =
  | 'retested'
  | 'claim_rejected'
  | 'measurement_corrected'
  | 'deferred'

export interface OpenContradiction {
  contradiction_id: number
  claim_id: string
  strategy: string
  universe: string
  metric: string                 // 'sharpe' | 'max_dd_pct' | etc.
  claimed_value: number | null
  measured_value: number | null
  delta: number | null
  delta_abs: number | null
  severity: Severity
  first_seen_at: string          // ISO
  last_checked_at: string        // ISO
  source_id: string | null
  source_title: string | null
  source_url: string | null
  source_published_at: string | null
}

export interface OpenContradictionsResponse {
  count: number
  limit: number
  rows: OpenContradiction[]
}

export interface KnowledgeSourceListRow {
  id: string
  kind: string
  url: string | null
  title: string
  venue: string | null
  published_at: string | null
  ingested_at: string
  extracted_by: string | null
  claim_count: number
  open_contradictions: number
}

export interface KnowledgeSourcesResponse {
  total: number
  limit: number
  offset: number
  rows: KnowledgeSourceListRow[]
}

export interface ContradictionsTimelinePoint {
  date: string                    // YYYY-MM-DD
  critical: number
  major: number
  minor: number
  resolved: number
}

export interface ContradictionsTimelineResponse {
  days: number
  timeline: ContradictionsTimelinePoint[]
}

export interface DigestHistoryRow {
  id: number
  kind: string                    // 'daily' | 'weekly' | 'alert'
  sent_at: string                 // ISO
  new_papers: number
  new_experiments: number
  new_contradictions: number
  lifecycle_transitions: number
  delivery_status: string | null
}

export interface DigestHistoryResponse {
  rows: DigestHistoryRow[]
}

export interface ExtractionConfidenceResponse {
  total: number
  histogram: {
    high: number
    medium: number
    low: number
    unknown: number
  }
}

export interface StrategySummaryRow {
  strategy: string
  universe: string
  solo_sharpe: number | null
  portfolio_sharpe: number | null
  max_dd_pct: number | null
  trades: number | null
  last_measured_at: string | null
  active_claims: number
  open_contradictions: number
  lifecycle_state: string | null
}

export interface StrategySummariesResponse {
  rows: StrategySummaryRow[]
  count: number
}

export interface StrategySummaryDetailResponse {
  strategy: string
  summary: StrategySummaryRow[]
  open_contradictions: OpenContradiction[]
}

export interface KnowledgeSourceDetailResponse {
  source: {
    id: string
    kind: string
    url: string | null
    title: string
    authors: string[] | null
    venue: string | null
    published_at: string | null
    sha256: string | null
    local_path: string | null
    ingested_at: string
    extracted_by: string | null
    notes: string | null
  }
  claims: Array<{
    id: string
    source_id: string
    strategy: string
    universe: string | null
    status: string
    claimed_sharpe: number | null
    claimed_max_dd_pct: number | null
    claimed_trades: number | null
    claimed_cagr_pct: number | null
    extraction_confidence: string
    notes: string | null
    created_at: string
    updated_at: string
  }>
  claim_count: number
}

// ---------------------------------------------------------------------------
// /api/research/* dashboard support endpoints (Track 3a -- in research namespace
// but typed here alongside the rest of the dashboard data shapes for clarity).
// ---------------------------------------------------------------------------

export interface DiscoveryFunnelDay {
  date: string                    // YYYY-MM-DD
  papers_found: number
  papers_filtered: number
  specs_extracted: number
  strategies_generated: number
}

export interface DiscoveryFunnelResponse {
  days: number
  funnel: DiscoveryFunnelDay[]
}

export interface QueueHealthResponse {
  source: 'queue_mirror' | 'queue.json'
  by_status: Record<string, number>
  by_category: Record<string, number>
  active: number
}
