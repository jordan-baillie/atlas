import type { ReactNode } from 'react'

interface StatCardProps {
  label: string
  value: string | ReactNode
  sub?: string | ReactNode
  /** When true, renders value as larger (text-3xl) bold hero number */
  hero?: boolean
  /** Top accent stripe color — use CSS var or hex. When provided, renders a 1px
   *  full-width stripe at the top edge to visually signal sign (green/red). */
  accent?: string
  /** Drives the sub-chip color to communicate direction:
   *  - 'positive' → green tint (use for +% gains, positive P&L deltas)
   *  - 'negative' → red tint  (use for losses, negative deltas)
   *  - 'neutral'  → muted (default; use for durations, labels like "16d held")
   */
  subColor?: 'positive' | 'negative' | 'neutral'
  className?: string
}

const SUB_COLOR_CLASSES: Record<NonNullable<StatCardProps['subColor']>, string> = {
  positive: 'bg-green-500/10 text-green-400',
  negative: 'bg-red-500/10 text-red-400',
  neutral:  'bg-[var(--color-surface-alt)] text-[var(--color-text-muted)]',
}

export function StatCard({
  label,
  value,
  sub,
  hero = false,
  accent,
  subColor = 'neutral',
  className = '',
}: StatCardProps) {
  return (
    <div
      data-testid="stat-card"
      className={`relative overflow-hidden bg-[var(--color-surface)] border border-[var(--color-border)] rounded-xl p-3 md:p-4 dash-card ${className}`}
    >
      {/* 1px top accent stripe — full width, strong sign signal */}
      {accent && (
        <div
          className="absolute top-0 left-0 right-0 h-px"
          style={{ backgroundColor: accent }}
        />
      )}

      {/* Label — 10px uppercase tracking */}
      <div className="text-[10px] uppercase tracking-wider text-[var(--color-text-muted)] mb-1.5 font-semibold">
        {label}
      </div>

      {/* Value — mono + tabular-nums for all numeric content
       *  hero=true  → text-3xl bold  (KPI dashboard hero number)
       *  hero=false → text-xl semibold (standard stat) */}
      <div
        className={`font-mono tabular-nums text-[var(--color-text)] ${
          hero
            ? 'text-3xl font-bold leading-none'
            : 'text-xl font-semibold leading-tight'
        }`}
      >
        {value}
      </div>

      {/* Sub / delta chip — color-coded by subColor prop */}
      {sub != null && (
        <div
          className={`mt-2 inline-flex items-center px-1.5 py-0.5 rounded text-[10px] font-mono tabular-nums ${SUB_COLOR_CLASSES[subColor]}`}
        >
          {sub}
        </div>
      )}
    </div>
  )
}
