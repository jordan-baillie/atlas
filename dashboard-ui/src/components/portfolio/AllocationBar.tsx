import type { StrategyAllocation } from '../../api/types'
import { getStrategyColor } from '../../lib/colors'
import { fmtCcy, fmtPct } from '../../lib/format'

interface Props { allocation: StrategyAllocation[]; equity?: number }

export function AllocationBar({ allocation, equity }: Props) {
  const items = allocation.filter((a) => (a.pct ?? 0) > 0)
  return (
    <div>
      <div className="text-[11px] uppercase tracking-[0.12em] text-[var(--color-text-muted)] font-semibold mb-3">CAPITAL ALLOCATION</div>
      <div className="flex h-8 rounded-lg overflow-hidden border border-[var(--color-border)]">
        {items.map((a) => (
          <div
            key={a.strategy ?? 'unknown'}
            style={{ width: `${a.pct ?? 0}%`, backgroundColor: getStrategyColor(a.strategy) }}
            title={`${a.strategy}: ${fmtPct(a.pct)}`}
          />
        ))}
      </div>
      <div className="flex flex-wrap gap-2 mt-3">
        {items.map((a) => {
          const pctOfEquity = equity && equity > 0 ? ((a.value ?? 0) / equity) * 100 : null
          return (
            <div
              key={a.strategy ?? 'unknown'}
              className="flex items-center gap-2 text-xs font-mono bg-[var(--color-surface-alt)] border border-[var(--color-border)] rounded-md px-2 py-1"
            >
              <span className="inline-block rounded-full" style={{ width: 8, height: 8, backgroundColor: getStrategyColor(a.strategy) }} />
              <span>{a.strategy}</span>
              <span className="text-[var(--color-text-muted)]">{fmtCcy(a.value)} ({fmtPct(pctOfEquity)} of equity)</span>
            </div>
          )
        })}
      </div>
    </div>
  )
}
