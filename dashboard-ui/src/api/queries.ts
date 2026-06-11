import { useQuery, keepPreviousData } from '@tanstack/react-query'
import { get } from './client'
import { qk } from './keys'
import type { DashboardData, SystemHealth, HealthCronJob, HealthDataFreshness } from './types'

const REFETCH_60S = 60_000
const REFETCH_5MIN = 5 * 60_000

// ---------------------------------------------------------------------------
// normalizeSystemHealth — /api/system/health returns name-keyed objects;
// reshape to the arrays/records the SystemHealth component expects.
// ---------------------------------------------------------------------------
function normalizeSystemHealth(raw: Record<string, unknown>): SystemHealth {
  const svcRaw = raw.services
  const services: Record<string, string> =
    svcRaw && !Array.isArray(svcRaw) && typeof svcRaw === 'object'
      ? Object.fromEntries(Object.entries(svcRaw as Record<string, unknown>).map(([k, v]) => [k, String(v)]))
      : {}
  const cronRaw = (raw.cron ?? raw.cron_jobs)
  const cron: Record<string, HealthCronJob> =
    cronRaw && !Array.isArray(cronRaw) && typeof cronRaw === 'object'
      ? (cronRaw as Record<string, HealthCronJob>)
      : {}
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
// Equity chart series merge + return stats (single-pass select).
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

export function buildEquityChartData(data: DashboardData): EquityChartData {
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
// Live pipeline (forge->live shadow loop) — /api/live
// ---------------------------------------------------------------------------
export interface LiveBookStats {
  book_equity?: number | null
  cash?: number | null
  n_positions?: number | null
  capital_base?: number | null
  cum_return?: number | null
  last_return?: number | null
  days_tracked: number
  realized_sharpe?: number | null
  equity_curve: { date?: string; equity?: number }[]
}
export interface LiveDeployed {
  name: string
  provider: string
  state: string
  broker: string
  capital: number
  approved: boolean
  expectation?: Record<string, number>
  book?: LiveBookStats
}
export interface LivePortfolio {
  n_strategies: number
  n_tracked: number
  total_equity: number
  total_capital_base: number
  total_pnl: number
  total_return?: number | null
}
export interface LiveDailyResult {
  name: string
  state: string
  broker: string
  n_orders: number
  executed: number
  dry_run: boolean
  track_status?: string | null
  blocked?: string | null
  awaiting_approval?: boolean
  error?: string | null
}
// Go-live gates (G6 slippage / G7 broker errors / track-vs-expectation).
// All fields defensive-optional: the UI must render "AWAITING DATA" when absent.
export interface GateSlippage {
  median_bps?: number | null
  p75_bps?: number | null
  worst_bps?: number | null
  n_fills?: number
  lookback_days?: number
  bar_bps?: number
  pass?: boolean | null
}
export interface GateBrokerErrors {
  n_orders?: number
  n_errors?: number
  n_unmatched?: number
  error_rate_pct?: number | null
  bar_pct?: number
  pass?: boolean | null
}
export interface GateTrack {
  status?: string | null
  n_obs?: number
  realized_mean?: number | null
  realized_sharpe?: number | null
  expected_sharpe?: number | null
  mean_z?: number | null
  worst_daily_z?: number | null
  reasons?: string[]
  pass?: boolean | null
}
export interface GoLiveGates {
  slippage?: GateSlippage
  broker_errors?: GateBrokerErrors
  track?: GateTrack
  pass?: boolean | null
}
export interface GatesOverall {
  pass?: boolean | null
  n_strategies?: number
  n_pass?: number
  n_fail?: number
  failing?: string[]
}
export interface LiveState {
  deployed: LiveDeployed[]
  portfolio?: LivePortfolio | null
  daily: { date: string; mode: string; results: LiveDailyResult[] } | null
  kill_switch: { blocked: boolean; reason?: string | null; layer?: string | null }
  gates?: { per_strategy?: Record<string, GoLiveGates>; overall?: GatesOverall } | null
}

// ---------------------------------------------------------------------------
// Hooks
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

export function useSystemHealth() {
  return useQuery({
    queryKey: qk.system.health(),
    queryFn: () => get<Record<string, unknown>>('/api/system/health').then(normalizeSystemHealth),
    refetchInterval: REFETCH_5MIN,
    placeholderData: keepPreviousData,
    staleTime: 2 * 60_000,
  })
}

export function useLiveState() {
  return useQuery({
    queryKey: ['live', 'state'],
    queryFn: () => get<LiveState>('/api/live'),
    refetchInterval: REFETCH_60S,
    placeholderData: keepPreviousData,
    staleTime: 30_000,
  })
}
