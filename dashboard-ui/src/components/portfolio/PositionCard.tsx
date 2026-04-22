import type { Position } from '../../api/types'
import { fmtCcy, fmtSignedCcy, fmtSignedPct, pnlClass, daysHeld } from '../../lib/format'
import { getStrategyColor } from '../../lib/colors'

interface Props { position: Position }

export function PositionCard({ position }: Props) {
  const stratColor = getStrategyColor(position.strategy)
  const pnl = position.unrealized_pnl ?? 0
  const borderColor = pnl > 0 ? 'var(--color-green)' : pnl < 0 ? 'var(--color-red)' : 'var(--color-border)'
  const held = daysHeld(position.entry_date)

  return (
    <div
      data-testid="position-card"
      className="bg-[var(--color-surface)] rounded-xl p-3 md:p-4 relative border border-[var(--color-border)] hover:translate-y-[-2px] hover:shadow-lg transition-all duration-200"
      style={{ borderLeftColor: borderColor, borderLeftWidth: 3 }}
    >
      <div className="flex items-center justify-between mb-1">
        <div className="font-mono font-bold text-base md:text-lg tracking-tight">{position.ticker ?? '\u2014'}</div>
        <div
          className="rounded-full px-2 py-0.5 text-[10px] font-mono"
          style={{ backgroundColor: `${stratColor}33`, color: stratColor }}
        >
          {position.strategy ?? '\u2014'}
        </div>
      </div>

      {held != null && (
        <div className="text-[10px] text-[var(--color-text-muted)] font-mono mb-3">
          {held}d held · {position.shares ?? 0} shares
        </div>
      )}

      <div className={`font-mono font-bold text-xl mb-3 ${pnlClass(pnl)}`}>
        {fmtSignedCcy(pnl)}{' '}
        <span className="text-sm font-semibold">{fmtSignedPct(position.unrealized_pnl_pct)}</span>
      </div>

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
          <div className="font-mono text-xs tabular-nums mt-0.5">{position.stop_price != null ? fmtCcy(position.stop_price) : '\u2014'}</div>
        </div>
      </div>

      <div className="mt-3 pt-2 border-t border-[var(--color-border)]/50 flex items-center justify-between text-xs">
        <div className={pnlClass(position.intraday_pnl)}>
          <span className="text-[var(--color-text-muted)]">Today </span>
          <span className="font-mono tabular-nums">{fmtSignedCcy(position.intraday_pnl)}</span>
        </div>
        <div className="text-[var(--color-text-muted)] font-mono tabular-nums">
          Prev: {fmtCcy(position.lastday_price)}
        </div>
      </div>
    </div>
  )
}
