import type { ReactNode } from 'react'
import { usePortfolioData, useRegimeCurrent, useRegimeHistory, useSystemHealth, useMacroGauges, usePositionRisk, useRegimeTransitions, useVixTermStructure, useRuinProbability, useRegimeForecast } from '../../api/queries'
import type { RuinProbability, RegimeForecast } from '../../api/types'
import { Skeleton } from '../layout/Skeleton'
import { SectionBoundary } from '../layout/SectionBoundary'
import { SummaryStrip } from './SummaryStrip'
import { EquityChart } from './EquityChart'
import { PerformanceSection } from './PerformanceSection'
import { PositionsGrid } from './PositionsGrid'
import { RiskSection } from './RiskSection'
import { MacroGauges } from './MacroGauges'
import { RegimeSection } from './RegimeSection'
import { OrdersTable } from './OrdersTable'
import { SystemHealth } from './SystemHealth'
import { VixTermStructureCard } from './VixTermStructureCard'

// GroupDivider — visual separator with label between dashboard groups
function GroupDivider({ label }: { label: string }) {
  return (
    <div className="flex items-center gap-3 py-2">
      <div className="h-px flex-1 bg-[var(--color-border)]"></div>
      <span className="text-[10px] uppercase tracking-[0.15em] text-[var(--color-text-muted)] font-semibold">{label}</span>
      <div className="h-px flex-1 bg-[var(--color-border)]"></div>
    </div>
  )
}

// CollapsibleGroup — <details> element with consistent styled summary bar
interface CollapsibleGroupProps {
  label: string
  defaultOpen?: boolean
  children: ReactNode
}

function CollapsibleGroup({ label, defaultOpen = false, children }: CollapsibleGroupProps) {
  return (
    <details open={defaultOpen} className="group">
      <summary className="flex items-center gap-3 py-2 cursor-pointer list-none select-none">
        <div className="h-px flex-1 bg-[var(--color-border)]"></div>
        <span className="text-[10px] uppercase tracking-[0.15em] text-[var(--color-text-muted)] font-semibold flex items-center gap-2">
          <span className="transition-transform group-open:rotate-90">&#9654;</span>
          {label}
        </span>
        <div className="h-px flex-1 bg-[var(--color-border)]"></div>
      </summary>
      <div className="space-y-4 md:space-y-6 mt-4">
        {children}
      </div>
    </details>
  )
}

// SurvivalOddsBanner — horizontal banner showing 90-day ruin probability
function SurvivalOddsBanner({ data }: { data?: RuinProbability }) {
  if (!data || !data.horizons) return null
  const h90 = data.horizons['90d']
  if (!h90) return null
  const pRuin = h90.prob_ruin ?? 0
  const survivalPct = (1 - pRuin) * 100
  const color = pRuin < 0.05 ? 'bg-green-500/10 border-green-500/40 text-green-300'
              : pRuin < 0.15 ? 'bg-amber-500/10 border-amber-500/40 text-amber-300'
              : 'bg-red-500/10 border-red-500/40 text-red-300'
  const dot = pRuin < 0.05 ? '\u{1F7E2}' : pRuin < 0.15 ? '\u{1F7E1}' : '\u{1F534}'
  return (
    <div className={`rounded-lg border px-4 py-3 mb-3 ${color}`}>
      <div className="flex items-center justify-between gap-4 flex-wrap">
        <div className="flex items-center gap-3">
          <span className="text-lg">{dot}</span>
          <div>
            <div className="text-xs uppercase tracking-wider opacity-70">Survival Odds</div>
            <div className="text-lg font-mono font-semibold">{survivalPct.toFixed(1)}% safe over 90 days</div>
          </div>
        </div>
        <div className="text-xs opacity-80 font-mono">
          P(equity &lt; ${(data.floor ?? 0).toLocaleString(undefined, { maximumFractionDigits: 0 })} in 90d): {(pRuin * 100).toFixed(2)}%
          <br/>
          Based on {(data.tickers?.length ?? 0)} positions, {data.n_paths.toLocaleString()} paths
        </div>
      </div>
    </div>
  )
}

// RegimeForecastCard — 30-day regime forecast with state probability bars
function RegimeForecastCard({ data }: { data?: RegimeForecast }) {
  if (!data || !data.horizons) return null
  const h30 = data.horizons['30d']
  if (!h30) return null
  const stateProbs = Object.entries(h30.state_probabilities ?? {})
    .sort((a, b) => b[1] - a[1])
    .slice(0, 4)
  return (
    <div className="rounded-lg border border-zinc-700 bg-zinc-900/50 p-3 text-xs">
      <div className="text-xs uppercase tracking-wider text-zinc-400 mb-2">Regime Forecast &#xB7; 30 days</div>
      <div className="grid grid-cols-3 gap-3 mb-3 font-mono">
        <div>
          <div className="text-zinc-500 text-[10px] uppercase">E[Return]</div>
          <div className={(h30.expected_return ?? 0) >= 0 ? 'text-green-400' : 'text-red-400'}>
            {((h30.expected_return ?? 0) * 100).toFixed(2)}%
          </div>
        </div>
        <div>
          <div className="text-zinc-500 text-[10px] uppercase">Downside (5%)</div>
          <div className="text-red-400">{((h30.var_5 ?? 0) * 100).toFixed(2)}%</div>
        </div>
        <div>
          <div className="text-zinc-500 text-[10px] uppercase">P(positive)</div>
          <div className="text-zinc-200">{((h30.prob_positive ?? 0) * 100).toFixed(0)}%</div>
        </div>
      </div>
      <div className="text-zinc-500 text-[10px] uppercase mb-1">Most likely states at day 30</div>
      <div className="space-y-1">
        {stateProbs.map(([state, p]) => (
          <div key={state} className="flex items-center gap-2 font-mono text-[11px]">
            <span className="text-zinc-400 w-44 truncate">{state}</span>
            <div className="flex-1 bg-zinc-800 h-1.5 rounded overflow-hidden">
              <div className="bg-blue-400 h-full" style={{ width: `${(p * 100).toFixed(0)}%` }} />
            </div>
            <span className="text-zinc-300 w-10 text-right">{(p * 100).toFixed(0)}%</span>
          </div>
        ))}
      </div>
    </div>
  )
}

export function PortfolioTab() {
  const portfolio = usePortfolioData()
  const regimeCurrent = useRegimeCurrent()
  const regimeHistory = useRegimeHistory()
  const health = useSystemHealth()
  const macro = useMacroGauges()
  const risk = usePositionRisk()
  const transitions = useRegimeTransitions()
  const vixTermStructure = useVixTermStructure()
  const { data: ruinData } = useRuinProbability()
  const { data: forecastData } = useRegimeForecast()

  // keep regimeCurrent in scope — used for future regime indicator
  void regimeCurrent

  return (
    <div className="space-y-4 md:space-y-6">
      {/* Group 1: AT-A-GLANCE */}
      <SectionBoundary title="Summary">
        {portfolio.data?.account
          ? <SummaryStrip account={portfolio.data.account} positionsCount={portfolio.data.positions?.length ?? 0} />
          : <Skeleton className="h-28" />}
      </SectionBoundary>

      <SectionBoundary title="Equity">
        {portfolio.data ? <EquityChart /> : <Skeleton className="h-96" />}
      </SectionBoundary>

      {/* Group 2: CURRENT STATE */}
      <GroupDivider label="Current State" />

      <SectionBoundary title="Positions">
        {portfolio.data?.positions
          ? <PositionsGrid positions={portfolio.data.positions} />
          : <Skeleton className="h-48" />}
      </SectionBoundary>

      <SectionBoundary title="Risk">
        <SurvivalOddsBanner data={ruinData} />
        {risk.data ? <RiskSection data={risk.data} /> : <Skeleton className="h-64" />}
      </SectionBoundary>

      {/* Group 3: MARKET CONTEXT (collapsible, default open) */}
      <CollapsibleGroup label="Market Context" defaultOpen={true}>
        <SectionBoundary title="Regime">
          {regimeHistory.data && transitions.data
            ? <RegimeSection history={regimeHistory.data} transitions={transitions.data} />
            : <Skeleton className="h-64" />}
        </SectionBoundary>

        <RegimeForecastCard data={forecastData} />

        <SectionBoundary title="Macro">
          {macro.data ? <MacroGauges data={macro.data} /> : <Skeleton className="h-40" />}
        </SectionBoundary>

        <SectionBoundary title="VIX Term Structure">
          {vixTermStructure.data
            ? <VixTermStructureCard data={vixTermStructure.data} />
            : <Skeleton className="h-40" />}
        </SectionBoundary>
      </CollapsibleGroup>

      {/* Group 4: PERFORMANCE (collapsible, default closed) */}
      <CollapsibleGroup label="Performance" defaultOpen={false}>
        <SectionBoundary title="Performance">
          {portfolio.data ? <PerformanceSection data={portfolio.data} /> : <Skeleton className="h-64" />}
        </SectionBoundary>

        <SectionBoundary title="Orders">
          {portfolio.data?.recent_orders
            ? <OrdersTable orders={portfolio.data.recent_orders} />
            : <Skeleton className="h-32" />}
        </SectionBoundary>
      </CollapsibleGroup>

      {/* Group 5: SYSTEM (collapsible, default closed) */}
      <CollapsibleGroup label="System" defaultOpen={false}>
        <SectionBoundary title="Health">
          {health.data ? <SystemHealth data={health.data} /> : <Skeleton className="h-40" />}
        </SectionBoundary>
      </CollapsibleGroup>
    </div>
  )
}
