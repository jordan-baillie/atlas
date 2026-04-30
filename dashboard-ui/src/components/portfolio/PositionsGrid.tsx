import type { Position } from '../../api/types'
import { PositionCard } from './PositionCard'
import { EmptyState } from '../shared/EmptyState'

interface Props { positions: Position[] }

export function PositionsGrid({ positions }: Props) {
  return (
    <div>
      <div className="text-[11px] uppercase tracking-[0.12em] text-[var(--color-text-muted)] font-semibold mb-3">
        OPEN POSITIONS ({positions.length})
      </div>
      {positions.length === 0 ? (
        <EmptyState
          icon="\u25a1"
          heading="No open positions"
          description="Positions will appear here when the portfolio is active."
        />
      ) : (
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 gap-3">
          {positions.map((p, i) => <PositionCard key={p.ticker ?? i} position={p} />)}
        </div>
      )}
    </div>
  )
}
