import type { Account } from '../../api/types'
import { StatCard } from '../shared/StatCard'
import { fmtCcy, fmtSignedCcy, fmtPct, pnlClass } from '../../lib/format'

interface Props { account: Account; positionsCount?: number }

export function SummaryStrip({ account, positionsCount }: Props) {
  // num_positions from API is unreliable (returns 0). Prefer explicit positionsCount from positions array.
  const count = positionsCount ?? account.num_positions ?? 0
  return (
    <div className="grid grid-cols-2 md:grid-cols-4 gap-3 md:gap-4">
      <StatCard label="PORTFOLIO" value={fmtCcy(account.equity)} hero />
      <StatCard label="TODAY P&L" value={<span className={pnlClass(account.total_pnl)}>{fmtSignedCcy(account.total_pnl)}</span>} />
      <StatCard label="POSITIONS" value={`${count}/10`} />
      <StatCard label="MARGIN USED" value={fmtPct(account.margin_usage_pct)} />
    </div>
  )
}
