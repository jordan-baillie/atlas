import type { ReactNode } from 'react'

interface StatCardProps {
  label: string
  value: string | ReactNode
  sub?: string | ReactNode
  hero?: boolean
  accent?: string
  className?: string
}

export function StatCard({ label, value, sub, hero = false, accent, className = '' }: StatCardProps) {
  return (
    <div
      data-testid="stat-card"
      className={`relative overflow-hidden bg-[var(--color-surface)] border border-[var(--color-border)] rounded-xl p-3 md:p-5 dash-card ${className}`}
    >
      {accent && (
        <div
          className="absolute top-0 left-0 right-0 h-0.5"
          style={{ backgroundColor: accent }}
        />
      )}
      <div className="text-[10px] md:text-[11px] uppercase tracking-[0.08em] text-[var(--color-text-muted)] mb-2 font-semibold">{label}</div>
      <div className={`font-mono font-bold text-[var(--color-text)] leading-tight ${hero ? 'text-lg md:text-2xl' : 'text-base md:text-lg'}`}>{value}</div>
      {sub != null ? <div className="text-[11px] text-[var(--color-text-muted)] mt-1.5 font-mono">{sub}</div> : null}
    </div>
  )
}
