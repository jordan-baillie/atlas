import { usePortfolioData, useRegimeCurrent, useRegimeHistory, useOverlayDecisions, useSystemHealth, useMacroGauges, usePositionRisk, useRegimeTransitions } from '../../api/queries'
import { Skeleton } from '../layout/Skeleton'
import { SectionBoundary } from '../layout/SectionBoundary'
import { SummaryStrip } from './SummaryStrip'
import { EquityChart } from './EquityChart'
import { PerformanceSection } from './PerformanceSection'
import { PositionsGrid } from './PositionsGrid'
import { RiskSection } from './RiskSection'
import { MacroGauges } from './MacroGauges'
import { RegimeSection } from './RegimeSection'
import { OverlayDecisions } from './OverlayDecisions'
import { OrdersTable } from './OrdersTable'
import { SystemHealth } from './SystemHealth'

export function PortfolioTab() {
  const portfolio = usePortfolioData()
  const regimeCurrent = useRegimeCurrent()
  const regimeHistory = useRegimeHistory()
  const overlay = useOverlayDecisions()
  const health = useSystemHealth()
  const macro = useMacroGauges()
  const risk = usePositionRisk()
  const transitions = useRegimeTransitions()

  // keep regimeCurrent in scope — used for future regime indicator
  void regimeCurrent

  return (
    <div className="space-y-4 md:space-y-6">
      <SectionBoundary title="Summary">
        {portfolio.data?.account
          ? <SummaryStrip account={portfolio.data.account} positionsCount={portfolio.data.positions?.length ?? 0} />
          : <Skeleton className="h-28" />}
      </SectionBoundary>

      <SectionBoundary title="Equity">
        {portfolio.data ? <EquityChart /> : <Skeleton className="h-96" />}
      </SectionBoundary>

      <SectionBoundary title="Performance">
        {portfolio.data ? <PerformanceSection data={portfolio.data} /> : <Skeleton className="h-64" />}
      </SectionBoundary>

      <SectionBoundary title="Positions">
        {portfolio.data?.positions
          ? <PositionsGrid positions={portfolio.data.positions} />
          : <Skeleton className="h-48" />}
      </SectionBoundary>

      <SectionBoundary title="Risk">
        {risk.data ? <RiskSection data={risk.data} /> : <Skeleton className="h-64" />}
      </SectionBoundary>

      <SectionBoundary title="Macro">
        {macro.data ? <MacroGauges data={macro.data} /> : <Skeleton className="h-40" />}
      </SectionBoundary>

      <SectionBoundary title="Regime">
        {regimeHistory.data && transitions.data
          ? <RegimeSection history={regimeHistory.data} transitions={transitions.data} />
          : <Skeleton className="h-64" />}
      </SectionBoundary>

      <SectionBoundary title="Overlay">
        {overlay.data ? <OverlayDecisions decisions={overlay.data} /> : <Skeleton className="h-40" />}
      </SectionBoundary>

      <SectionBoundary title="Orders">
        {portfolio.data?.recent_orders
          ? <OrdersTable orders={portfolio.data.recent_orders} />
          : <Skeleton className="h-32" />}
      </SectionBoundary>

      <SectionBoundary title="Health">
        {health.data ? <SystemHealth data={health.data} /> : <Skeleton className="h-40" />}
      </SectionBoundary>
    </div>
  )
}
