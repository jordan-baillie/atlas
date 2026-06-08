import { useEffect, useState } from 'react'
import type { CycleStatus } from '../../api/forge-types'

// ── Forge palette (thematic — embers/gold over the dark design tokens) ──
export const C = {
  gold: '#fbbf24',
  ember: '#f59e0b',
  hot: '#f97316',
  iron: '#71717a',
  green: '#22c55e',
  red: '#ef4444',
  cyan: '#22d3ee',
  indigo: '#6366f1',
} as const

export const STAGE_ICON: Record<string, string> = {
  scout: '🔭', propose: '💡', codegen: '⚙️', run: '🛡️', record: '📖', alert: '🔔',
}

export function statusColor(s: CycleStatus): string {
  return s === 'pass' ? C.gold : s === 'error' ? C.red : C.iron
}
export function statusLabel(s: CycleStatus, tier: string | null): string {
  if (s === 'pass') return 'PASS'
  if (s === 'error') return 'ERROR'
  return tier || 'FAIL'
}

/** Live wall-clock tick (1s). Returns Date.now() ms. */
export function useNow(intervalMs = 1000): number {
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), intervalMs)
    return () => clearInterval(id)
  }, [intervalMs])
  return now
}

/** Countdown to a target epoch-ms. */
export function useCountdown(targetMs: number | null): { h: number; m: number; s: number; done: boolean; total: number } {
  const now = useNow(1000)
  if (!targetMs) return { h: 0, m: 0, s: 0, done: true, total: 0 }
  const total = Math.max(0, targetMs - now)
  const sec = Math.floor(total / 1000)
  return { h: Math.floor(sec / 3600), m: Math.floor((sec % 3600) / 60), s: sec % 60, done: total <= 0, total }
}

export function pad(n: number): string {
  return String(n).padStart(2, '0')
}

// ── Small primitives ──
export function Card({ children, className = '', glow = false }: { children: React.ReactNode; className?: string; glow?: boolean }) {
  return (
    <div
      className={`bg-[var(--color-surface)] border border-[var(--color-border)] rounded-xl ${glow ? 'forge-pulse' : ''} ${className}`}
    >
      {children}
    </div>
  )
}

export function StatTile({ label, value, sub, color, icon }: { label: string; value: React.ReactNode; sub?: string; color?: string; icon?: string }) {
  return (
    <Card className="p-3.5">
      <div className="flex items-center gap-1.5 text-[10px] uppercase tracking-wider text-[var(--color-text-muted)]">
        {icon && <span className="text-sm">{icon}</span>}
        {label}
      </div>
      <div className="mt-1 text-2xl font-bold tabular-nums" style={{ color: color || 'var(--color-text)' }}>{value}</div>
      {sub && <div className="text-[11px] text-[var(--color-text-muted)] mt-0.5">{sub}</div>}
    </Card>
  )
}

/** SVG radial gauge (0..1 fraction). */
export function RadialGauge({ value, max = 1, label, sub, color = C.ember, size = 116 }: {
  value: number; max?: number; label: string; sub?: string; color?: string; size?: number
}) {
  const frac = Math.max(0, Math.min(1, value / max))
  const r = size / 2 - 9
  const circ = 2 * Math.PI * r
  return (
    <div className="flex flex-col items-center justify-center">
      <div className="relative" style={{ width: size, height: size }}>
        <svg width={size} height={size} className="-rotate-90">
          <circle cx={size / 2} cy={size / 2} r={r} fill="none" stroke="var(--color-surface-alt)" strokeWidth={8} />
          <circle
            cx={size / 2} cy={size / 2} r={r} fill="none" stroke={color} strokeWidth={8} strokeLinecap="round"
            strokeDasharray={circ} strokeDashoffset={circ * (1 - frac)}
            style={{ transition: 'stroke-dashoffset 0.8s ease' }}
          />
        </svg>
        <div className="absolute inset-0 flex flex-col items-center justify-center">
          <div className="text-xl font-bold tabular-nums" style={{ color }}>{sub ?? `${Math.round(frac * 100)}%`}</div>
        </div>
      </div>
      <div className="text-[11px] text-[var(--color-text-muted)] mt-1 text-center max-w-[120px]">{label}</div>
    </div>
  )
}

export function tierChip(tier: string | null, passed: boolean) {
  const t = passed ? 'PASS' : (tier || 'FAIL')
  const bg = passed ? 'rgba(251,191,36,0.15)' : 'rgba(113,113,122,0.15)'
  const fg = passed ? C.gold : C.iron
  return (
    <span className="px-1.5 py-0.5 rounded text-[10px] font-bold tracking-wide" style={{ background: bg, color: fg }}>{t}</span>
  )
}
