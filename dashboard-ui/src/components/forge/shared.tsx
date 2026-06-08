import type { CycleStatus } from '../../api/forge-types'

// Forge palette — embers/gold over the dark design tokens.
export const C = {
  gold: '#fbbf24', ember: '#f59e0b', hot: '#f97316',
  iron: '#71717a', green: '#22c55e', red: '#ef4444', indigo: '#6366f1',
} as const

export function statusColor(s: CycleStatus): string {
  return s === 'pass' ? C.gold : s === 'error' ? C.red : C.iron
}
export function statusLabel(s: CycleStatus, tier: string | null): string {
  if (s === 'pass') return 'PASS'
  if (s === 'error') return 'ERROR'
  return tier || 'FAIL'
}

export function Card({ children, className = '', glow = false }: { children: React.ReactNode; className?: string; glow?: boolean }) {
  return (
    <div className={`bg-[var(--color-surface)] border border-[var(--color-border)] rounded-xl ${glow ? 'forge-pulse' : ''} ${className}`}>
      {children}
    </div>
  )
}

/** Format a metric value for the run summary grid. */
export function fmtMetric(v: number | null | undefined, kind: 'sharpe' | 'pct' | 'int' | 'ratio' = 'sharpe'): string {
  if (v === null || v === undefined) return '—'
  if (kind === 'pct') return `${(v * 100).toFixed(1)}%`
  if (kind === 'int') return String(Math.round(v))
  if (kind === 'ratio') return v.toFixed(2)
  return v.toFixed(2)
}
