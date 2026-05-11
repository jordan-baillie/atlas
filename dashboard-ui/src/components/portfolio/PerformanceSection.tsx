import type { DashboardData } from '../../api/types'
import { StatCard } from '../shared/StatCard'
import { StrategyBreakdown } from './StrategyBreakdown'
import { AllocationBar } from './AllocationBar'
import { fmtCcy, fmtSignedCcy, fmtSignedPct, fmtPct, pnlClass } from '../../lib/format'

interface Props { data: DashboardData }

export function PerformanceSection({ data }: Props) {
  const overall = data.strategy_performance?.overall
  const summary = data.summary

  const totalPnl = summary?.total_pnl
  const totalPnlSign: 'positive' | 'negative' | 'neutral' = totalPnl == null ? 'neutral' : totalPnl >= 0 ? 'positive' : 'negative'
  const totalPnlAccent = totalPnl != null
    ? (totalPnl >= 0 ? 'var(--color-green)' : 'var(--color-red)')
    : undefined

  const expectancy = overall?.expectancy
  const expectancyAccent = expectancy != null
    ? (expectancy >= 0 ? 'var(--color-green)' : 'var(--color-red)')
    : undefined

  return (
    <div className="bg-[var(--color-surface)] border border-[var(--color-border)] rounded-xl p-5 space-y-6 dash-card">
      <div>
        <div className="text-[11px] uppercase tracking-[0.12em] text-[var(--color-text-muted)] font-semibold mb-3">
          PERFORMANCE
        </div>
        <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-6 gap-4">
          {/* TOTAL P&L — accent stripe + subColor for % delta */}
          <StatCard
            label="TOTAL P&L"
            value={
              <span className={`tabular-nums ${pnlClass(totalPnl)}`}>
                {fmtSignedCcy(totalPnl)}
              </span>
            }
            sub={fmtSignedPct(summary?.total_pnl_pct)}
            subColor={totalPnlSign}
            accent={totalPnlAccent}
          />
          <StatCard
            label="WIN RATE"
            value={<span className="tabular-nums">{fmtPct(overall?.win_rate)}</span>}
            sub={`${overall?.trades ?? 0} trades`}
          />
          <StatCard
            label="PROFIT FACTOR"
            value={
              <span className="tabular-nums">
                {overall?.profit_factor != null ? overall.profit_factor.toFixed(2) : '\u2014'}
              </span>
            }
          />
          <StatCard
            label="AVG WIN / LOSS"
            value={
              <span className="tabular-nums">
                {fmtCcy(overall?.avg_win)} / {fmtCcy(overall?.avg_loss)}
              </span>
            }
          />
          <StatCard
            label="TOTAL TRADES"
            value={<span className="tabular-nums">{String(overall?.trades ?? 0)}</span>}
          />
          {/* EXPECTANCY — accent stripe signal */}
          <StatCard
            label="EXPECTANCY"
            value={
              <span className={`tabular-nums ${pnlClass(expectancy)}`}>
                {fmtSignedCcy(expectancy)}
              </span>
            }
            accent={expectancyAccent}
          />
        </div>
      </div>
      <StrategyBreakdown performance={data.strategy_performance} />
      <AllocationBar allocation={data.strategy_allocation ?? []} equity={data.account?.equity} />
    </div>
  )
}
