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
      className={`relative overflow-hidden bg-[var(--color-surface)] border border-[var(--color-border)] rounded-xl p-3 md:p-4 dash-card ${className}`}
    >
      {/* Optional top accent stripe (e.g. coloured for P&L sign) */}
      {accent && (
        <div
          className="absolute top-0 left-0 right-0 h-0.5"
          style={{ backgroundColor: accent }}
        />
      )}

      {/* Label */}
      <div className="text-[10px] uppercase tracking-wider text-[var(--color-text-muted)] mb-1.5 font-semibold">
        {label}
      </div>

      {/* Value — mono + tabular-nums for all numeric content */}
      <div
        className={`font-mono font-semibold tabular-nums text-[var(--color-text)] leading-tight ${
          hero ? 'text-2xl' : 'text-xl'
        }`}
      >
        {value}
      </div>

      {/* Sub / delta — small chip with subtle background */}
      {sub != null && (
        <div className="mt-2 inline-flex items-center px-1.5 py-0.5 rounded text-[10px] font-mono
                        bg-[var(--color-surface-alt)] text-[var(--color-text-muted)]">
          {sub}
        </div>
      )}
    </div>
  )
}
