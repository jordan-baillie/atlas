import type { ReactNode } from 'react'
import { CornerBrackets } from '../ui/hud'

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
  /** Mission Control: HUD corner brackets. */
  brackets?: boolean
  /** Mission Control: static accent glow halo. */
  glow?: boolean
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
  brackets = false,
  glow = false,
  className = '',
}: StatCardProps) {
  return (
    <div
      data-testid="stat-card"
      className={`mc-frame relative overflow-hidden rounded-xl p-3 md:p-4 ${glow ? 'mc-glow-after' : ''} ${className}`}
    >
      {brackets && <CornerBrackets />}
      {/* Label — accent tick + 10px uppercase tracking */}
      <div className="flex items-center gap-1.5 text-[10px] uppercase tracking-wider text-[var(--color-text-muted)] mb-1.5 font-semibold">
        <span
          aria-hidden
          className="inline-block w-[3px] h-3 rounded-full"
          style={{ background: 'var(--accent-section, var(--color-accent))' }}
        />
        {label}
      </div>

      {/* Value — mono + tabular-nums; accent colours the number (Forge convention)
       *  hero=true  → display-num text-3xl (HUD hero number, glow text-shadow)
       *  hero=false → text-xl semibold (standard stat) */}
      <div
        className={
          hero
            ? 'display-num text-3xl leading-none'
            : 'font-mono tabular-nums text-xl font-semibold leading-tight'
        }
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
