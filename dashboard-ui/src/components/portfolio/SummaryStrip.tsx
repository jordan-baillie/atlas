import type { Account } from '../../api/types'
import { StatCard } from '../shared/StatCard'
import { AsOfBadge } from '../shared/AsOfBadge'
import { AnimatedNumber } from '../ui/AnimatedNumber'
import { fmtCcy, fmtSignedCcy, fmtPct } from '../../lib/format'

interface Props {
  account: Account
  todayPnl?: number
  positionsCount?: number
  /** Position cap from active config. Absent when no config strategies are live (cap not applicable). */
  maxPositions?: number
  /** ISO timestamp of the live broker pull (DashboardData.timestamp). Optional — badge degrades gracefully. */
  asOf?: string
  /** Glow the live cards while the market is open. */
  marketOpen?: boolean
}

export function SummaryStrip({ account, todayPnl, positionsCount, maxPositions, asOf, marketOpen = false }: Props) {
  // num_positions from API is unreliable (returns 0). Prefer explicit positionsCount from positions array.
  const count = positionsCount ?? account.num_positions ?? 0

  // TODAY P&L accent: green for gains, red for losses, none for null
  const todayAccent =
    todayPnl != null
      ? todayPnl >= 0
        ? 'var(--color-positive)'
        : 'var(--color-negative)'
      : undefined

  return (
    <div data-testid="summary-strip" className="grid grid-cols-2 md:grid-cols-4 gap-2 md:gap-3">
      <StatCard
        label="PORTFOLIO"
        brackets
        glow={marketOpen}
        value={
          <span className="flex items-center gap-1.5 flex-wrap tabular-nums">
            <AnimatedNumber value={account.equity} format={fmtCcy} flashOnDelta />
            <AsOfBadge source="live" asOf={asOf} />
          </span>
        }
        hero
      />
      {/* TODAY P&L — hero card: this is the focal number on the strip */}
      <StatCard
        label="TODAY P&L"
        brackets
        glow={marketOpen}
        value={<AnimatedNumber value={todayPnl} format={fmtSignedCcy} flashOnDelta />}
        hero
        accent={todayAccent}
      />
      <StatCard
        label="POSITIONS"
        value={
          <span className="tabular-nums">
            {count}{maxPositions != null ? `/${maxPositions}` : ''}
            {(account.open_orders ?? 0) > 0 && (
              <span className="ml-1.5 text-[11px] text-[var(--color-amber)]" title="Pending orders awaiting fill (reserve margin until executed)">
                +{account.open_orders} pending
              </span>
            )}
          </span>
        }
      />
      <StatCard
        label="MARGIN USED"
        value={
          <span className="tabular-nums">
            {fmtPct(account.margin_usage_pct)}
            {(account.open_orders ?? 0) > 0 && (account.margin_usage_pct ?? 0) > 0 && (
              <span className="ml-1.5 text-[10px] text-[var(--color-text-muted)]">incl. pending</span>
            )}
          </span>
        }
      />
    </div>
  )
}
