import { useQuery, useMutation, useQueryClient, keepPreviousData } from '@tanstack/react-query'
import { get } from './client'
import { qk } from './keys'
import type {
  DashboardData,
  RegimeCurrent,
  RegimeHistory,
  SystemHealth,
  HealthCronJob,
  HealthDataFreshness,
  MacroGaugeData,
  PositionRisk,
  RegimeTransitions,
  FinanceData,
  RegimeDistributions,
  VixTermStructure,
  RuinProbability,
  RegimeForecast,
  SignalEVResponse,
  UniverseInfo,
  PnlFilterOptions,
  PnlTrade,
} from './types'

const REFETCH_60S = 60_000
const REFETCH_5MIN = 5 * 60_000
const STALE_5MIN = 5 * 60_000
const REFETCH_DAILY = 24 * 60 * 60_000  // 1 day
const STALE_1HR = 60 * 60_000

// ---------------------------------------------------------------------------
// normalizeSystemHealth
// The /api/system/health endpoint returns objects keyed by name rather than
// the arrays that the SystemHealth component expects.  Transform them here so
// the rest of the UI never sees the raw shape.
// ---------------------------------------------------------------------------
function normalizeSystemHealth(raw: Record<string, unknown>): SystemHealth {
  // services: {"atlas-dashboard": "active"} — pass through as Record<string, string>
  const svcRaw = raw.services
  const services: Record<string, string> =
    svcRaw && !Array.isArray(svcRaw) && typeof svcRaw === 'object'
      ? Object.fromEntries(Object.entries(svcRaw as Record<string, unknown>).map(([k, v]) => [k, String(v)]))
      : {}

  // cron: {"postclose": {last_run, status}} — pass through as Record<string, HealthCronJob>
  const cronRaw = (raw.cron ?? raw.cron_jobs)
  const cron: Record<string, HealthCronJob> =
    cronRaw && !Array.isArray(cronRaw) && typeof cronRaw === 'object'
      ? (cronRaw as Record<string, HealthCronJob>)
      : {}

  // data_freshness: {"ohlcv_last_date": "2026-04-07", ...} — pass through as HealthDataFreshness
  const freshnessRaw = raw.data_freshness
  const data_freshness: HealthDataFreshness =
    freshnessRaw && !Array.isArray(freshnessRaw) && typeof freshnessRaw === 'object'
      ? (freshnessRaw as HealthDataFreshness)
      : {}

  return {
    services,
    cron,
    data_freshness,
    overall: raw.overall as string | undefined,
    timestamp: raw.timestamp as string | undefined,
  }
}

// ---------------------------------------------------------------------------
// ChartPoint + mergeEquitySeries + EquityChartData + buildEquityChartData
// Rule: async-parallel — pre-merge both series in a single pass so EquityChart
// receives a ready-to-render array via `select` without recomputing on every render.
// Rule: js-combine-iterations — buildEquityChartData merges series AND computes
// return stats in one select call so EquityChart needs no further derivation.
// ---------------------------------------------------------------------------
export interface ChartPoint {
  date: string
  portfolio: number | null
  spy: number | null
}

export function mergeEquitySeries(data: DashboardData): ChartPoint[] {
  const portfolioMap = new Map<string, number>()
  for (const p of data.portfolio_history ?? []) {
    if (p.date && p.equity != null) portfolioMap.set(p.date, p.equity)
  }
  const benchMap = new Map<string, number>()
  for (const p of data.benchmark?.curve ?? []) {
    if (p.date && p.equity != null) benchMap.set(p.date, p.equity)
  }
  // Forward-fill: when a date is present in one series but not the other,
  // carry the last known value forward so Recharts never sees a null gap.
  const dates = Array.from(new Set<string>([...portfolioMap.keys(), ...benchMap.keys()])).sort()
  let lastPort: number | null = null
  let lastSpy: number | null = null
  return dates.map((date) => {
    const p = portfolioMap.get(date)
    const s = benchMap.get(date)
    if (p != null) lastPort = p
    if (s != null) lastSpy = s
    return { date, portfolio: lastPort, spy: lastSpy }
  })
}

export interface EquityChartData {
  chartData: ChartPoint[]
  summary: DashboardData['summary']
  portfolioReturnPct: number
  spyReturnPct: number
  alphaVsSpy: number
}

/** Pure selector: called once by react-query select, result is cached until
 *  the underlying DashboardData changes. Combines chart-series merge and
 *  return-stat computation in a single pass (js-combine-iterations rule). */
export function buildEquityChartData(data: DashboardData): EquityChartData {
  // Use summary.return_pct (window-aligned to portfolio_history) so the badge
  // matches the chart's visible curve. Fall back to total_pnl_pct (vs config
  // starting equity) only if return_pct is missing.
  const portfolioReturnPct = data.summary?.return_pct ?? data.summary?.total_pnl_pct ?? 0
  const spyReturnPct = data.benchmark?.return_pct ?? 0
  return {
    chartData: mergeEquitySeries(data),
    summary: data.summary,
    portfolioReturnPct,
    spyReturnPct,
    alphaVsSpy: portfolioReturnPct - spyReturnPct,
  }
}

// ---------------------------------------------------------------------------
// Hooks
// All polling hooks use:
//   - qk.*() for stable, type-safe query keys
//   - placeholderData: keepPreviousData  → eliminates flicker on each refetch
//   - staleTime: 30_000                 → avoids redundant background requests
// ---------------------------------------------------------------------------

export function usePortfolioData() {
  return useQuery({
    queryKey: qk.dashboardData(),
    queryFn: () => get<DashboardData>('/api/dashboard-data'),
    refetchInterval: REFETCH_60S,
    placeholderData: keepPreviousData,
    staleTime: 30_000,
  })
}

// useEquityChartData — pre-transforms DashboardData → EquityChartData via select
// so EquityChart receives chart series + return stats in one shot and never
// recomputes on re-renders (async-parallel rule; js-combine-iterations rule).
export function useEquityChartData() {
  return useQuery({
    queryKey: qk.dashboardData(),
    queryFn: () => get<DashboardData>('/api/dashboard-data'),
    refetchInterval: REFETCH_60S,
    placeholderData: keepPreviousData,
    staleTime: 30_000,
    select: buildEquityChartData,
  })
}

export function useRegimeCurrent() {
  return useQuery({
    queryKey: qk.regime.current(),
    queryFn: () => get<RegimeCurrent>('/api/regime/current'),
    refetchInterval: REFETCH_60S,
    placeholderData: keepPreviousData,
    staleTime: 30_000,
  })
}

export function useRegimeHistory() {
  return useQuery({
    queryKey: qk.regime.history(90),
    queryFn: () => get<RegimeHistory>('/api/regime/history?days=90'),
    refetchInterval: REFETCH_60S,
    placeholderData: keepPreviousData,
    staleTime: 30_000,
  })
}


export function useSystemHealth() {
  return useQuery({
    queryKey: qk.system.health(),
    queryFn: () =>
      get<Record<string, unknown>>('/api/system/health').then(normalizeSystemHealth),
    refetchInterval: REFETCH_5MIN,
    placeholderData: keepPreviousData,
    staleTime: 2 * 60_000,
  })
}

export function useMacroGauges() {
  return useQuery({
    queryKey: qk.macro.gauges(),
    queryFn: () => get<MacroGaugeData>('/api/macro/gauges'),
    refetchInterval: REFETCH_60S,
    placeholderData: keepPreviousData,
    staleTime: 30_000,
  })
}

export function usePositionRisk() {
  return useQuery({
    queryKey: qk.positions.risk(),
    queryFn: () => get<PositionRisk>('/api/positions/risk'),
    refetchInterval: REFETCH_60S,
    placeholderData: keepPreviousData,
    staleTime: 30_000,
  })
}

export function useRegimeTransitions() {
  return useQuery({
    queryKey: qk.regime.transitions(),
    queryFn: () => get<RegimeTransitions>('/api/regime/transitions'),
    refetchInterval: REFETCH_5MIN,
    placeholderData: keepPreviousData,
    staleTime: 2 * 60_000,
  })
}

export function useFinanceData(enabled: boolean) {
  return useQuery({
    queryKey: qk.finance(),
    queryFn: () => get<FinanceData>('/api/finance'),
    enabled,
    placeholderData: keepPreviousData,
    staleTime: STALE_5MIN,
  })
}

export function useRegimeDistributions() {
  return useQuery({
    queryKey: qk.regime.distributions(),
    queryFn: () => get<RegimeDistributions>('/api/regime/distributions'),
    refetchInterval: REFETCH_DAILY,
    placeholderData: keepPreviousData,
    staleTime: STALE_1HR,
  })
}

export function useVixTermStructure() {
  return useQuery({
    queryKey: qk.signals.vixTermStructure(),
    queryFn: () => get<VixTermStructure>('/api/signals/vix_term_structure'),
    refetchInterval: REFETCH_60S,
    placeholderData: keepPreviousData,
    staleTime: 30_000,
  })
}

export function useRuinProbability() {
  return useQuery<RuinProbability>({
    queryKey: ['risk', 'ruin'],
    queryFn: () => get<RuinProbability>('/api/risk/ruin'),
    staleTime: 60 * 60 * 1000,  // 1 hour
    refetchOnWindowFocus: false,
  })
}

export function useRegimeForecast() {
  return useQuery<RegimeForecast>({
    queryKey: ['regime', 'forecast'],
    queryFn: () => get<RegimeForecast>('/api/regime/forecast'),
    staleTime: 60 * 60 * 1000,
    refetchOnWindowFocus: false,
  })
}

export function useSignalEV() {
  return useQuery<SignalEVResponse>({
    queryKey: ['signals', 'ev'],
    queryFn: () => get<SignalEVResponse>('/api/signals/ev'),
    staleTime: 60 * 60 * 1000,
    refetchOnWindowFocus: false,
  })
}

export function useRefreshRuinProbability() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: () =>
      fetch('/api/risk/ruin/refresh', {
        method: 'POST',
        credentials: 'same-origin',
      }).then((r) => {
        if (!r.ok) throw new Error(`HTTP ${r.status}`)
        return r.json() as Promise<unknown>
      }),
    onSuccess: () => {
      // Give the server 5s to recompute, then invalidate the cached result
      setTimeout(() => {
        queryClient.invalidateQueries({ queryKey: ['risk', 'ruin'] })
      }, 5_000)
    },
  })
}

export function useUniversesHealth() {
  return useQuery<UniverseInfo[]>({
    queryKey: ['system', 'health', 'universes'],
    queryFn: () => get<UniverseInfo[]>('/api/system/health/universes'),
    staleTime: STALE_1HR,
    refetchOnWindowFocus: false,
  })
}

// ---------------------------------------------------------------------------
// P&L Slicer — filter options + filtered trades
// ---------------------------------------------------------------------------

export interface PnlFilters {
  market_id: string
  strategy: string
  sector: string
}

export function usePnlFilterOptions() {
  return useQuery({
    queryKey: qk.pnl.filterOptions(),
    queryFn: () => get<PnlFilterOptions>('/api/pnl_filter_options'),
    placeholderData: keepPreviousData,
    staleTime: STALE_5MIN,
  })
}

export function usePnlTrades(filters: PnlFilters) {
  return useQuery({
    queryKey: qk.pnl.trades(filters as unknown as Record<string, string>),
    queryFn: () => {
      const params = new URLSearchParams()
      if (filters.market_id) params.set('market_id', filters.market_id)
      if (filters.strategy) params.set('strategy', filters.strategy)
      if (filters.sector) params.set('sector', filters.sector)
      const qs = params.toString()
      return get<PnlTrade[]>(`/api/trades${qs ? '?' + qs : ''}`)
    },
    placeholderData: keepPreviousData,
    staleTime: 30_000,
  })
}
