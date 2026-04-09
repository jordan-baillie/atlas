import type { Position } from '../../api/types'
import { fmtCcy, fmtSignedCcy, fmtSignedPct, pnlClass, daysHeld } from '../../lib/format'
import { getStrategyColor } from '../../lib/colors'

interface Props { position: Position }

export function PositionCard({ position }: Props) {
  const color = getStrategyColor(position.strategy)
  const held = daysHeld(position.entry_date)
  return (
    <div
      className="bg-[var(--color-surface)] rounded-xl p-3 md:p-4 relative border border-[var(--color-border)] dash-card"
      style={{ borderLeftColor: color, borderLeftWidth: 3 }}
    >
      <div className="flex items-center justify-between mb-3">
        <div className="font-mono font-semibold text-sm md:text-base">{position.ticker ?? '\u2014'}</div>
        <div
          className="rounded-full px-2 py-0.5 text-[10px] font-mono"
          style={{ backgroundColor: `${color}33`, color }}
        >
          {position.strategy ?? '\u2014'}
        </div>
      </div>

      <div className="grid grid-cols-3 gap-2 mb-3 text-center">
        <div>
          <div className="text-[10px] uppercase tracking-wider text-[var(--color-text-muted)]">ENTRY</div>
          <div className="font-mono text-sm">{fmtCcy(position.entry_price)}</div>
        </div>
        <div>
          <div className="text-[10px] uppercase tracking-wider text-[var(--color-text-muted)]">CURRENT</div>
          <div className="font-mono text-sm">{fmtCcy(position.current_price)}</div>
        </div>
        <div>
          <div className="text-[10px] uppercase tracking-wider text-[var(--color-text-muted)]">STOP</div>
          <div className="font-mono text-sm">{position.stop_price != null ? fmtCcy(position.stop_price) : '\u2014'}</div>
        </div>
      </div>

      <div className={`font-mono font-semibold text-lg ${pnlClass(position.unrealized_pnl)}`}>
        {fmtSignedCcy(position.unrealized_pnl)}{' '}
        <span className="text-sm font-normal">{fmtSignedPct(position.unrealized_pnl_pct)}</span>
      </div>

      <div className="flex items-center justify-between mt-2 text-xs">
        <div className={pnlClass(position.today_pnl)}>
          Today: {fmtSignedCcy(position.today_pnl)} ({fmtSignedPct(position.intraday_pnl_pct)})
        </div>
        <div className="text-[var(--color-text-muted)] font-mono">Prev: {fmtCcy(position.lastday_price)}</div>
      </div>

      {held != null && (
        <div className="mt-3 inline-block text-[10px] uppercase tracking-wider bg-[var(--color-surface-alt)] px-2 py-0.5 rounded-md text-[var(--color-text-muted)]">
          {held}d held
        </div>
      )}
    </div>
  )
}
