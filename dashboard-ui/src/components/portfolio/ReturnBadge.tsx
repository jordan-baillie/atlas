import { memo } from 'react'
import type { DashboardData } from '../../api/types'
import { Badge } from '../shared/Badge'
import { fmtSignedPct } from '../../lib/format'

interface Props { data: DashboardData }

function ReturnBadgeInner({ data }: Props) {
  const portfolioReturn = data.summary?.total_pnl_pct ?? 0
  const spyReturn = data.benchmark?.return_pct ?? 0
  const alpha = portfolioReturn - spyReturn
  const variant = portfolioReturn >= 0 ? 'success' : 'danger'
  return (
    <Badge variant={variant} size="sm">
      {fmtSignedPct(portfolioReturn)} ({fmtSignedPct(alpha)} vs SPY)
    </Badge>
  )
}

export const ReturnBadge = memo(ReturnBadgeInner)
