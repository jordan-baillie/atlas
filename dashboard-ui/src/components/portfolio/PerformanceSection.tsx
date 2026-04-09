import type { DashboardData } from '../../api/types'
import { StatCard } from '../shared/StatCard'
import { StrategyBreakdown } from './StrategyBreakdown'
import { AllocationBar } from './AllocationBar'
import { fmtCcy, fmtSignedCcy, fmtSignedPct, fmtPct, pnlClass } from '../../lib/format'

interface Props { data: DashboardData }

export function PerformanceSection({ data }: Props) {
  const overall = data.strategy_performance?.overall
  const summary = data.summary
  return (
    <div className="bg-[var(--color-surface)] border border-[var(--color-border)] rounded-xl p-5 space-y-6 dash-card">
      <div>
        <div className="text-[11px] uppercase tracking-[0.12em] text-[var(--color-text-muted)] font-semibold mb-3">PERFORMANCE</div>
        <div className="grid grid-cols-6 lg:grid-cols-3 sm:grid-cols-2 gap-4">
          <StatCard label="TOTAL P&L" value={<span className={pnlClass(summary?.total_pnl)}>{fmtSignedCcy(summary?.total_pnl)}</span>} sub={<span className={pnlClass(summary?.total_pnl_pct)}>{fmtSignedPct(summary?.total_pnl_pct)}</span>} />
          <StatCard label="WIN RATE" value={fmtPct(overall?.win_rate)} sub={`${overall?.trades ?? 0} trades`} />
          <StatCard label="PROFIT FACTOR" value={overall?.profit_factor != null ? overall.profit_factor.toFixed(2) : '\u2014'} />
          <StatCard label="AVG WIN / LOSS" value={`${fmtCcy(overall?.avg_win)} / ${fmtCcy(overall?.avg_loss)}`} />
          <StatCard label="TOTAL TRADES" value={String(overall?.trades ?? 0)} />
          <StatCard label="EXPECTANCY" value={<span className={pnlClass(overall?.expectancy)}>{fmtSignedCcy(overall?.expectancy)}</span>} />
        </div>
      </div>
      <StrategyBreakdown performance={data.strategy_performance} />
      <AllocationBar allocation={data.strategy_allocation ?? []} equity={data.account?.equity} />
    </div>
  )
}
