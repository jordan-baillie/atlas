import { memo } from 'react'
import type { DashboardData } from '../../api/types'
import { fmtSignedPct } from '../../lib/format'

interface Props { data: DashboardData }

function ReturnBadgeInner({ data }: Props) {
  const portfolioReturn = data.summary?.total_pnl_pct ?? 0
  const spyReturn = data.benchmark?.return_pct ?? 0
  const alpha = portfolioReturn - spyReturn
  const positive = portfolioReturn >= 0
  const colorClass = positive ? 'text-[var(--color-green)]' : 'text-[var(--color-red)]'
  return (
    <div className={`rounded-md px-2.5 py-1 text-xs font-mono bg-[var(--color-surface-alt)] border border-[var(--color-border)] ${colorClass}`}>
      {fmtSignedPct(portfolioReturn)} ({fmtSignedPct(alpha)} vs SPY)
    </div>
  )
}

export const ReturnBadge = memo(ReturnBadgeInner)
