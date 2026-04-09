import type { ReactNode } from 'react'

interface StatCardProps {
  label: string
  value: string | ReactNode
  sub?: string | ReactNode
  hero?: boolean
  className?: string
}

export function StatCard({ label, value, sub, hero = false, className = '' }: StatCardProps) {
  return (
    <div className={`bg-[var(--color-surface)] border border-[var(--color-border)] rounded-xl p-3 md:p-5 dash-card ${className}`}>
      <div className="text-[10px] md:text-[11px] uppercase tracking-wider text-[var(--color-text-muted)] mb-2 font-medium">{label}</div>
      <div className={`font-mono font-semibold text-[var(--color-text)] ${hero ? 'text-lg md:text-2xl' : 'text-base md:text-lg'}`}>{value}</div>
      {sub != null ? <div className="text-xs text-[var(--color-text-muted)] mt-1">{sub}</div> : null}
    </div>
  )
}
