// React Query hooks for /api/knowledge/* + the Track-3a research dashboard
// endpoints (/api/research/discovery-funnel, /api/research/queue-health).

import { useQuery, useMutation, useQueryClient, keepPreviousData } from '@tanstack/react-query'
import { get, post } from './client'
import { qk } from './keys'
import type {
  OpenContradictionsResponse,
  KnowledgeSourcesResponse,
  KnowledgeSourceDetailResponse,
  StrategySummaryDetailResponse,
  ContradictionsTimelineResponse,
  DigestHistoryResponse,
  ExtractionConfidenceResponse,
  StrategySummariesResponse,
  DiscoveryFunnelResponse,
  QueueHealthResponse,
  Resolution,
  Severity,
} from './knowledge-types'

const STALE_2MIN = 2 * 60_000
const STALE_5MIN = 5 * 60_000

// ── Contradictions ───────────────────────────────────────────────────────────

export function useOpenContradictions(
  params: { severity?: Severity; strategy?: string; limit?: number } = {},
  enabled = true,
) {
  const qs = new URLSearchParams()
  if (params.severity) qs.set('severity', params.severity)
  if (params.strategy) qs.set('strategy', params.strategy)
  if (params.limit) qs.set('limit', String(params.limit))
  return useQuery({
    queryKey: qk.knowledge.openContradictions(params as Record<string, unknown>),
    queryFn: () =>
      get<OpenContradictionsResponse>(
        `/api/knowledge/contradictions/open?${qs.toString()}`,
      ),
    enabled,
    placeholderData: keepPreviousData,
    staleTime: STALE_2MIN,
  })
}

export function useResolveContradiction() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (args: { id: number; resolution: Resolution; note?: string }) =>
      post(`/api/knowledge/contradictions/${args.id}/resolve`, {
        resolution: args.resolution,
        note: args.note,
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: qk.knowledge.all() })
    },
  })
}

// ── Sources ──────────────────────────────────────────────────────────────────

export function useKnowledgeSources(
  params: { kind?: string; q?: string; limit?: number; offset?: number } = {},
  enabled = true,
) {
  const qs = new URLSearchParams()
  if (params.kind) qs.set('kind', params.kind)
  if (params.q) qs.set('q', params.q)
  if (params.limit) qs.set('limit', String(params.limit))
  if (params.offset) qs.set('offset', String(params.offset))
  return useQuery({
    queryKey: qk.knowledge.sources(params as Record<string, unknown>),
    queryFn: () =>
      get<KnowledgeSourcesResponse>(`/api/knowledge/sources?${qs.toString()}`),
    enabled,
    placeholderData: keepPreviousData,
    staleTime: STALE_5MIN,
  })
}

export function useKnowledgeSource(id: string, enabled = true) {
  return useQuery({
    queryKey: qk.knowledge.sourceById(id),
    queryFn: () =>
      get<KnowledgeSourceDetailResponse>(`/api/knowledge/sources/${encodeURIComponent(id)}`),
    enabled: enabled && !!id,
    staleTime: STALE_5MIN,
  })
}

// ── Per-strategy summary drilldown ───────────────────────────────────────────

export function useStrategySummaryDetail(
  strategy: string,
  params: { universe?: string; open_contradictions_limit?: number } = {},
  enabled = true,
) {
  const qs = new URLSearchParams()
  if (params.universe) qs.set('universe', params.universe)
  if (params.open_contradictions_limit !== undefined)
    qs.set('open_contradictions_limit', String(params.open_contradictions_limit))
  return useQuery({
    queryKey: qk.knowledge.strategySummary(strategy, params as Record<string, unknown>),
    queryFn: () =>
      get<StrategySummaryDetailResponse>(
        `/api/knowledge/strategy/${encodeURIComponent(strategy)}/summary?${qs.toString()}`,
      ),
    enabled: enabled && !!strategy,
    staleTime: STALE_5MIN,
  })
}

// ── Timeseries + histograms ──────────────────────────────────────────────────

export function useContradictionsTimeline(days = 30, enabled = true) {
  return useQuery({
    queryKey: qk.knowledge.contradictionsTimeline(days),
    queryFn: () =>
      get<ContradictionsTimelineResponse>(
        `/api/knowledge/contradictions-timeline?days=${days}`,
      ),
    enabled,
    placeholderData: keepPreviousData,
    staleTime: STALE_5MIN,
  })
}

export function useDigestHistory(limit = 30, enabled = true) {
  return useQuery({
    queryKey: qk.knowledge.digestHistory(limit),
    queryFn: () => get<DigestHistoryResponse>(`/api/knowledge/digest-history?limit=${limit}`),
    enabled,
    staleTime: STALE_5MIN,
  })
}

export function useExtractionConfidence(enabled = true) {
  return useQuery({
    queryKey: qk.knowledge.extractionConfidence(),
    queryFn: () => get<ExtractionConfidenceResponse>('/api/knowledge/extraction-confidence'),
    enabled,
    staleTime: STALE_5MIN,
  })
}

export function useStrategySummaries(enabled = true) {
  return useQuery({
    queryKey: qk.knowledge.strategySummaries(),
    queryFn: () =>
      get<StrategySummariesResponse>('/api/knowledge/strategy-summaries?limit=200'),
    enabled,
    placeholderData: keepPreviousData,
    staleTime: STALE_2MIN,
  })
}

// ── Research namespace (dashboard support) ───────────────────────────────────

export function useDiscoveryFunnel(days = 30, enabled = true) {
  return useQuery({
    queryKey: qk.research.discoveryFunnel(days),
    queryFn: () =>
      get<DiscoveryFunnelResponse>(`/api/research/discovery-funnel?days=${days}`),
    enabled,
    placeholderData: keepPreviousData,
    staleTime: STALE_5MIN,
  })
}

export function useQueueHealth(enabled = true) {
  return useQuery({
    queryKey: qk.research.queueHealth(),
    queryFn: () => get<QueueHealthResponse>('/api/research/queue-health'),
    enabled,
    staleTime: STALE_2MIN,
  })
}
