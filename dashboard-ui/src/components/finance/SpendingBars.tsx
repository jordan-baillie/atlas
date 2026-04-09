import type { SpendCategory } from '../../api/types'
import { fmtCcy } from '../../lib/format'

interface Props {
  categories: SpendCategory[]
  total?: number
}

export function SpendingBars({ categories, total }: Props) {
  const sorted = [...categories].sort((a, b) => (b.amount ?? 0) - (a.amount ?? 0)).slice(0, 10)
  const max = Math.max(...sorted.map(c => c.amount ?? 0), 1)

  return (
    <div className="bg-[var(--color-surface)] border border-[var(--color-border)] rounded-xl p-5 dash-card">
      <div className="flex items-center justify-between mb-4">
        <div className="text-[11px] uppercase tracking-[0.12em] text-[var(--color-text-muted)] font-semibold">SPENDING BY CATEGORY</div>
        <div className="text-[10px] uppercase tracking-wider text-[var(--color-text-muted)] font-medium">{fmtCcy(total)}</div>
      </div>
      <div>
        {sorted.map((c, i) => (
          <div key={c.category ?? i} className="flex items-center gap-3 mb-2">
            <div className="w-24 text-xs md:text-sm text-[var(--color-text-muted)] truncate">{c.label ?? c.category}</div>
            <div className="flex-1 h-5 bg-[var(--color-surface-alt)] rounded-md overflow-hidden">
              <div
                className="h-full bg-[var(--color-green)]/40 rounded-md"
                style={{ width: ((c.amount ?? 0) / max * 100) + '%' }}
              />
            </div>
            <div className="w-20 text-right font-mono text-xs md:text-sm">{fmtCcy(c.amount)}</div>
          </div>
        ))}
      </div>
    </div>
  )
}
