import type { PositionRisk } from '../../api/types'
import { StatCard } from '../shared/StatCard'
import { RiskTable } from './RiskTable'
import { fmtCcy, fmtPct } from '../../lib/format'

interface Props { data: PositionRisk }

export function RiskSection({ data }: Props) {
  const s = data.summary
  const unprotected = s?.positions_without_stops ?? 0
  return (
    <div className="bg-[var(--color-surface)] border border-[var(--color-border)] rounded-xl p-5 space-y-6 dash-card">
      <div>
        <div className="text-[11px] uppercase tracking-[0.12em] text-[var(--color-text-muted)] font-semibold mb-3">POSITION RISK</div>
        <div className="grid grid-cols-4 md:grid-cols-2 sm:grid-cols-1 gap-4">
          <StatCard label="CAPITAL AT RISK" value={fmtCcy(s?.total_risk_dollars)} sub={fmtPct(s?.total_risk_pct)} />
          <StatCard label="EQUITY" value={fmtCcy(s?.equity)} sub={`${s?.num_positions ?? 0} positions`} />
          <StatCard label="AVG DISTANCE TO STOP" value={fmtPct(s?.avg_distance_to_stop)} />
          <StatCard
            label="MAX RISK/TRADE"
            value={fmtPct(s?.max_risk_per_trade_pct)}
            sub={<span className={unprotected > 0 ? 'text-[var(--color-red)]' : ''}>{unprotected} unprotected</span>}
          />
        </div>
      </div>
      <RiskTable positions={data.positions ?? []} />
    </div>
  )
}
