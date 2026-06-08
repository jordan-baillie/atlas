import type { ReactNode } from 'react'

interface StatCardProps {
  label: string
  value: string | ReactNode
  sub?: string | ReactNode
  /** When true, renders value as larger (text-3xl) bold hero number */
  hero?: boolean
  /** Sign accent — when provided, the value number is rendered in this colour
   *  (the Forge convention: coloured hero numbers, no top stripe). */
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
  positive: 'bg-[var(--color-green)]/10 text-[var(--color-green)]',
  negative: 'bg-[var(--color-red)]/10 text-[var(--color-red)]',
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
      {/* Label — 10px uppercase tracking */}
      <div className="text-[10px] uppercase tracking-wider text-[var(--color-text-muted)] mb-1.5 font-semibold">
        {label}
      </div>

      {/* Value — mono + tabular-nums; accent colours the number (Forge convention)
       *  hero=true  → text-3xl bold  (KPI dashboard hero number)
       *  hero=false → text-xl semibold (standard stat) */}
      <div
        className={`font-mono tabular-nums ${
          hero
            ? 'text-3xl font-bold leading-none'
            : 'text-xl font-semibold leading-tight'
        }`}
        style={{ color: accent || 'var(--color-text)' }}
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
