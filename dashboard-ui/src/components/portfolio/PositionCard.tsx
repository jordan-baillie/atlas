import type { Position } from '../../api/types'
import { fmtCcy, fmtSignedCcy, fmtSignedPct, pnlClass, daysHeld } from '../../lib/format'
import { Badge } from '../shared/Badge'
import { AnimatedNumber } from '../ui/AnimatedNumber'

interface Props { position: Position }

export function PositionCard({ position }: Props) {
  const pnl = position.unrealized_pnl ?? 0
  const borderColor = pnl > 0 ? 'var(--color-green)' : pnl < 0 ? 'var(--color-red)' : 'var(--color-border)'
  const held = daysHeld(position.entry_date)

  return (
    <div
      data-testid="position-card"
      className="mc-frame rounded-xl p-3 relative hover:shadow-md transition-shadow duration-200"
      style={{ borderLeftColor: borderColor, borderLeftWidth: 3 }}
    >
      {/* Header row: ticker + strategy badge */}
      <div className="flex items-center justify-between mb-1 gap-2">
        {/* Ticker — prominent, text-lg per spec */}
        <div className="font-mono font-semibold text-lg tracking-tight leading-none">
          {position.ticker ?? '\u2014'}
        </div>
        {/* Strategy — accent badge (spec: Badge variant="accent" size="xs") */}
        <Badge variant="accent" size="xs">
          {position.strategy ?? '\u2014'}
        </Badge>
      </div>

      {/* Days held + share count */}
      {held != null && (
        <div className="text-[10px] text-[var(--color-text-muted)] font-mono tabular-nums mb-2.5">
          {held}d held &middot; {position.shares ?? 0} shares
        </div>
      )}

      {/* Primary P&L — bold, mono, signed */}
      <div className={`font-mono font-bold text-xl tabular-nums mb-2.5 ${pnlClass(pnl)}`}>
        <AnimatedNumber value={pnl} format={fmtSignedCcy} flashOnDelta />{' '}
        <span className="text-sm font-semibold tabular-nums">
          {fmtSignedPct(position.unrealized_pnl_pct)}
        </span>
      </div>

      {/* Price grid: Entry / Current / Stop */}
      <div className="grid grid-cols-3 gap-2 text-center">
        <div>
          <div className="text-[9px] uppercase tracking-wider text-[var(--color-text-muted)]">Entry</div>
          <div className="font-mono text-xs tabular-nums mt-0.5">{fmtCcy(position.entry_price)}</div>
        </div>
        <div>
          <div className="text-[9px] uppercase tracking-wider text-[var(--color-text-muted)]">Current</div>
          <div className="font-mono text-xs tabular-nums mt-0.5">{fmtCcy(position.current_price)}</div>
        </div>
        <div>
          <div className="text-[9px] uppercase tracking-wider text-[var(--color-text-muted)]">Stop</div>
          <div className="font-mono text-xs tabular-nums mt-0.5">
            {position.stop_price != null ? fmtCcy(position.stop_price) : '\u2014'}
          </div>
        </div>
      </div>

      {/* Footer: intraday P&L + prev close */}
      <div className="mt-2.5 pt-2 border-t border-[var(--color-border)]/50 flex items-center justify-between text-xs">
        <div className={pnlClass(position.intraday_pnl)}>
          <span className="text-[var(--color-text-muted)]">Today </span>
          <AnimatedNumber value={position.intraday_pnl} format={fmtSignedCcy} flashOnDelta className="text-xs" />
        </div>
        <div className="text-[var(--color-text-muted)] font-mono tabular-nums">
          Prev: {fmtCcy(position.lastday_price)}
        </div>
      </div>
    </div>
  )
}
