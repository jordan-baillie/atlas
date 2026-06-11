/* eslint-disable react-refresh/only-export-components -- mixed kit file: tokens + components by design */
import type { CycleStatus } from '../../api/forge-types'
import { CornerBrackets } from '../ui/hud'

// Forge palette — embers/gold over the dark design tokens.
export const C = {
  gold: '#fbbf24', ember: '#f59e0b', hot: '#f97316',
  iron: '#71717a', green: '#22c55e', red: '#ef4444', indigo: '#6366f1',
} as const

export function statusColor(s: CycleStatus): string {
  if (s === 'pass') return C.gold
  if (s === 'near_miss') return C.ember
  if (s === 'error') return C.red
  return C.iron
}
export function statusLabel(s: CycleStatus, tier: string | null): string {
  if (s === 'pass') return 'PASS'
  if (s === 'near_miss') return 'NEAR-MISS'
  if (s === 'error') return 'ERROR'
  return tier || 'FAIL'
}

export function Card({ children, className = '', glow = false, brackets = false }: {
  children: React.ReactNode; className?: string; glow?: boolean; brackets?: boolean
}) {
  return (
    <div className={`mc-frame relative rounded-xl ${glow ? 'forge-pulse' : ''} ${className}`}>
      {brackets && <CornerBrackets />}
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
