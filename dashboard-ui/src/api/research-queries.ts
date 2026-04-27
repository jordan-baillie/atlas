import { useQuery, keepPreviousData } from '@tanstack/react-query'
import { get } from './client'
import { qk } from './keys'
import type {
  ResearchSummary,
  ResearchStrategies,
  ResearchTimeline,
  ResearchExperiments,
  ResearchBrain,
  ResearchDiscoveries,
  ResearchOverview,
  ResearchLeaderboard,
  ResearchCoverage,
  PendingPromotionsResponse,
} from './research-types'

const STALE_5MIN = 5 * 60_000

export function useResearchSummary(enabled: boolean) {
  return useQuery({
    queryKey: qk.research.summary(),
    queryFn: () => get<ResearchSummary>('/api/research/summary'),
    enabled,
    placeholderData: keepPreviousData,
    staleTime: STALE_5MIN,
  })
}

export function useResearchStrategies(enabled: boolean) {
  return useQuery({
    queryKey: qk.research.strategies(),
    queryFn: () => get<ResearchStrategies>('/api/research/strategies'),
    enabled,
    placeholderData: keepPreviousData,
    staleTime: STALE_5MIN,
  })
}

export function useResearchTimeline(days: number, enabled: boolean) {
  return useQuery({
    queryKey: qk.research.timeline(days),
    queryFn: () => get<ResearchTimeline>(`/api/research/timeline?days=${days}`),
    enabled,
    placeholderData: keepPreviousData,
    staleTime: STALE_5MIN,
  })
}

export function useResearchExperiments(
  params: { limit?: number; strategy?: string; status?: string },
  enabled: boolean,
) {
  const qs = new URLSearchParams()
  if (params.limit) qs.set('limit', String(params.limit))
  if (params.strategy) qs.set('strategy', params.strategy)
  if (params.status) qs.set('status', params.status)
  return useQuery({
    queryKey: qk.research.experiments(params),
    queryFn: () => get<ResearchExperiments>(`/api/research/experiments?${qs}`),
    enabled,
    placeholderData: keepPreviousData,
    staleTime: STALE_5MIN,
  })
}

export function useResearchBrain(enabled: boolean) {
  return useQuery({
    queryKey: qk.research.brain(),
    queryFn: () => get<ResearchBrain>('/api/research/brain'),
    enabled,
    placeholderData: keepPreviousData,
    staleTime: STALE_5MIN,
  })
}

export function useResearchDiscoveries(enabled: boolean) {
  return useQuery({
    queryKey: qk.research.discoveries(),
    queryFn: () => get<ResearchDiscoveries>('/api/research/discoveries'),
    enabled,
    placeholderData: keepPreviousData,
    staleTime: STALE_5MIN,
  })
}

export function useResearchOverview(enabled: boolean) {
  return useQuery({
    queryKey: qk.research.all(),
    queryFn: () => get<ResearchOverview>('/api/research/overview'),
    enabled,
    placeholderData: keepPreviousData,
    staleTime: STALE_5MIN,
    refetchInterval: 60_000,
  })
}

export function useResearchLeaderboard(enabled: boolean) {
  return useQuery({
    queryKey: [...qk.research.all(), 'leaderboard'] as const,
    queryFn: () => get<ResearchLeaderboard>('/api/research/leaderboard'),
    enabled,
    placeholderData: keepPreviousData,
    staleTime: STALE_5MIN,
  })
}

export function useResearchCoverage(enabled: boolean) {
  return useQuery({
    queryKey: qk.research.coverage(),
    queryFn: () => get<ResearchCoverage>('/api/research/coverage'),
    enabled,
    placeholderData: keepPreviousData,
    staleTime: STALE_5MIN,
  })
}

export function usePendingPromotions() {
  return useQuery({
    queryKey: qk.promotions.pending(),
    queryFn: () => get<PendingPromotionsResponse>('/api/promotions/pending'),
    refetchInterval: 60_000,
    staleTime: 30_000,
  })
}
