import { memo } from 'react'
import type { Position } from '../../api/types'
import { PositionCard } from './PositionCard'
import { EmptyState } from '../shared/EmptyState'

interface Props { positions: Position[] }

function PositionsGridInner({ positions }: Props) {
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
        // Responsive: 1 col mobile / 2 col tablet (md ≥768px) / 3 col desktop (xl ≥1280px)
        <div className="stagger-pop grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-3">
          {positions.map((p, i) => <PositionCard key={p.ticker ?? i} position={p} />)}
        </div>
      )}
    </div>
  )
}

export const PositionsGrid = memo(PositionsGridInner)
