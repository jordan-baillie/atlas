/* eslint-disable react-refresh/only-export-components -- mixed kit file: tokens + components by design */
/**
 * Forge UI kit — the shared visual language across all dashboard tabs.
 *
 * Captures the "Forge" aesthetic: slim status strips, compact metric tiles,
 * left-aligned tracking-widest section headers, subtle glow on live elements.
 * Tabs import these primitives so the look stays consistent by construction.
 */
import type { ReactNode } from 'react'

export const C = {
  gold: '#fbbf24', ember: '#f59e0b', hot: '#f97316',
  iron: '#71717a', green: '#22c55e', red: '#ef4444',
  indigo: '#6366f1', sky: '#38bdf8',
} as const

/** Left-aligned section header with an accent tick — replaces centered dividers. */
export function SectionLabel({ children, icon, right }: { children: ReactNode; icon?: string; right?: ReactNode }) {
  return (
    <div className="flex items-center justify-between gap-3 mt-1 mb-1 px-0.5">
      <div className="flex items-center gap-2 min-w-0">
        <span className="w-0.5 h-3.5 rounded-full bg-[var(--color-border)]" />
        <span className="text-[11px] uppercase tracking-widest text-[var(--color-text-muted)] font-semibold truncate">
          {icon && <span className="mr-1">{icon}</span>}{children}
        </span>
      </div>
      {right && <div className="shrink-0 text-[11px] text-[var(--color-text-muted)]">{right}</div>}
    </div>
  )
}

/** Card with an optional tracking-widest header + right slot. The standard panel. */
export function SectionCard({ title, icon, right, children, className = '', glow = false }: {
  title?: ReactNode; icon?: string; right?: ReactNode; children: ReactNode; className?: string; glow?: boolean
}) {
  return (
    <div className={`bg-[var(--color-surface)] border border-[var(--color-border)] rounded-xl p-4 md:p-5 ${glow ? 'forge-pulse' : ''} ${className}`}>
      {(title || right) && (
        <div className="flex items-center justify-between gap-3 mb-3">
          <div className="text-[11px] uppercase tracking-widest text-[var(--color-text-muted)] font-semibold flex items-center gap-2 min-w-0">
            {icon && <span className="text-sm">{icon}</span>}<span className="truncate">{title}</span>
          </div>
          {right && <div className="shrink-0">{right}</div>}
        </div>
      )}
      {children}
    </div>
  )
}

/** Compact KPI chip for status-strip rows. */
export function StatChip({ label, value, color }: { label: string; value: ReactNode; color?: string }) {
  return (
    <div className="px-3 py-1.5 rounded-lg bg-[var(--color-surface-alt)] text-center min-w-[64px]">
      <div className="text-[9px] uppercase tracking-wide text-[var(--color-text-muted)]">{label}</div>
      <div className="text-sm font-bold tabular-nums leading-tight" style={{ color: color || 'var(--color-text)' }}>{value}</div>
    </div>
  )
}

/** Running/idle pill with a blinking dot. */
export function StatusBadge({ on, labelOn, labelOff, colorOn = C.green }: {
  on: boolean; labelOn: string; labelOff: string; colorOn?: string
}) {
  return (
    <span className="px-1.5 py-0.5 rounded text-[10px] font-bold tracking-wide inline-flex items-center gap-1"
      style={{ background: on ? 'rgba(34,197,94,0.15)' : 'rgba(113,113,122,0.15)', color: on ? colorOn : C.iron }}>
      <span className="w-1.5 h-1.5 rounded-full forge-blink" style={{ background: on ? colorOn : C.iron }} />
      {on ? labelOn : labelOff}
    </span>
  )
}

/** Small uppercase tag (e.g. mode = PAPER, KILL active). */
export function Pill({ children, color = C.ember, tone = 0.15 }: { children: ReactNode; color?: string; tone?: number }) {
  const rgba = hexToRgba(color, tone)
  return (
    <span className="inline-flex items-center px-2 py-1 rounded text-[10px] font-semibold uppercase tracking-wider border"
      style={{ background: rgba, color, borderColor: hexToRgba(color, 0.3) }}>
      {children}
    </span>
  )
}

/** Slim status strip — the signature Forge header. */
export function StatusStrip({ icon, title, badge, meta, chips, glow = false }: {
  icon?: string; title: ReactNode; badge?: ReactNode; meta?: ReactNode; chips?: ReactNode; glow?: boolean
}) {
  return (
    <div className={`bg-[var(--color-surface)] border border-[var(--color-border)] rounded-xl px-5 py-3.5 ${glow ? 'forge-pulse' : ''}`}>
      <div className="flex flex-col md:flex-row md:items-center justify-between gap-3">
        <div className="flex items-center gap-3 min-w-0">
          {icon && <span className={`text-2xl shrink-0 ${glow ? 'forge-glow' : ''}`}>{icon}</span>}
          <div className="min-w-0">
            <div className="text-sm font-bold text-[var(--color-text)] flex items-center gap-2 flex-wrap">{title}{badge}</div>
            {meta && <div className="text-[11px] text-[var(--color-text-muted)] mt-0.5">{meta}</div>}
          </div>
        </div>
        {chips && <div className="flex flex-wrap gap-2">{chips}</div>}
      </div>
    </div>
  )
}

function hexToRgba(hex: string, a: number): string {
  const h = hex.replace('#', '')
  const n = parseInt(h.length === 3 ? h.split('').map((c) => c + c).join('') : h, 16)
  return `rgba(${(n >> 16) & 255}, ${(n >> 8) & 255}, ${n & 255}, ${a})`
}
