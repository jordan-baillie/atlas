import { useQuery, keepPreviousData } from '@tanstack/react-query'
import { get } from './client'
import { qk } from './keys'
import type { ForgeState } from './forge-types'

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
