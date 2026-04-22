import type { Account } from '../../api/types'
import { StatCard } from '../shared/StatCard'
import { fmtCcy, fmtSignedCcy, fmtPct, pnlClass } from '../../lib/format'

interface Props { account: Account; todayPnl?: number; positionsCount?: number }

export function SummaryStrip({ account, todayPnl, positionsCount }: Props) {
  // num_positions from API is unreliable (returns 0). Prefer explicit positionsCount from positions array.
  const count = positionsCount ?? account.num_positions ?? 0
  return (
    <div data-testid="summary-strip" className="grid grid-cols-2 md:grid-cols-4 gap-3 md:gap-4">
      <StatCard label="PORTFOLIO" value={fmtCcy(account.equity)} hero />
      <StatCard label="TODAY P&L" value={<span className={pnlClass(todayPnl)}>{fmtSignedCcy(todayPnl)}</span>} />
      <StatCard label="POSITIONS" value={`${count}/10`} />
      <StatCard label="MARGIN USED" value={fmtPct(account.margin_usage_pct)} />
    </div>
  )
}
