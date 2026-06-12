import { useQuery, keepPreviousData } from '@tanstack/react-query'
import { get } from './client'
import { qk } from './keys'
import type { ForgeState } from './forge-types'
import type { ResearchMapData } from './map-types'

/** Live Hephaestus forge state. Polls every 15s (the loop is nightly, but
 *  the killswitch / countdown / log tail benefit from a near-live feel). */
export function useForgeState(enabled: boolean = true) {
  return useQuery({
    queryKey: qk.forge.state(),
    queryFn: () => get<ForgeState>('/api/forge/state'),
    enabled,
    refetchInterval: 15_000,
    placeholderData: keepPreviousData,
    staleTime: 8_000,
  })
}

/** Research map — the wiki as a lineage graph. Data changes once nightly;
 *  server caches 60s, so a slow poll keeps the queue ghosts fresh enough. */
export function useResearchMap(enabled: boolean = true) {
  return useQuery({
    queryKey: qk.forge.map(),
    queryFn: () => get<ResearchMapData>('/api/forge/map'),
    enabled,
    refetchInterval: 120_000,
    placeholderData: keepPreviousData,
    staleTime: 60_000,
  })
}
