import type { VixTermStructure } from '../../api/types'
import type { ReactNode } from 'react'

interface Props { data: VixTermStructure }

// Regime badge
function regimeBadge(regime?: string): ReactNode {
  const r = (regime ?? '').toLowerCase()
  const map: Record<string, { label: string; cls: string }> = {
    strong_contango:     { label: 'STRONG CONTANGO',     cls: 'bg-[var(--color-green)]/30 text-[var(--color-green)]' },
    contango:            { label: 'CONTANGO',             cls: 'bg-[var(--color-green)]/20 text-[var(--color-green)]' },
    flat:                { label: 'FLAT',                 cls: 'bg-[var(--color-surface-alt)] text-[var(--color-text-muted)]' },
    backwardation:       { label: 'BACKWARDATION',        cls: 'bg-[#f97316]/20 text-[#f97316]' },
    extreme_backwardation: { label: 'EXTREME BKWD',     cls: 'bg-[var(--color-red)]/20 text-[var(--color-red)]' },
  }
  const entry = map[r] ?? { label: regime?.toUpperCase() ?? '\u2014', cls: 'bg-[var(--color-surface-alt)] text-[var(--color-text-muted)]' }
  return (
    <span className={`rounded-md px-2 py-0.5 text-[10px] font-mono font-semibold uppercase tracking-wide ${entry.cls}`}>
      {entry.label}
    </span>
  )
}

// Action badge
function actionBadge(action?: string): ReactNode {
  const a = (action ?? '').toUpperCase()
  const map: Record<string, string> = {
    NORMAL:        'bg-[var(--color-surface-alt)] text-[var(--color-text-muted)]',
    WATCH:         'bg-[#f59e0b]/20 text-[#f59e0b]',
    REDUCE_GROSS:  'bg-[var(--color-red)]/20 text-[var(--color-red)]',
  }
  const cls = map[a] ?? 'bg-[var(--color-surface-alt)] text-[var(--color-text-muted)]'
  return (
    <span className={`rounded-md px-2 py-0.5 text-[10px] font-mono font-semibold uppercase tracking-wide ${cls}`}>
      {a || '\u2014'}
    </span>
  )
}

export function VixTermStructureCard({ data }: Props) {
  // Graceful error state
  if (data.error) {
    return (
      <div className="bg-[var(--color-surface)] border border-[var(--color-border)] rounded-xl p-5 dash-card">
        <div className="text-[10px] uppercase tracking-wider text-[var(--color-text-muted)] font-medium mb-3">VIX Term Structure</div>
        <div className="text-xs text-[var(--color-red)] font-mono">{data.error}</div>
      </div>
    )
  }

  const ratio = data.ratio != null ? data.ratio.toFixed(4) : '\u2014'
  const vix    = data.vix    != null ? data.vix.toFixed(2)    : '\u2014'
  const vix3m  = data.vix3m  != null ? data.vix3m.toFixed(2)  : '\u2014'
  const persistence = data.persistence_days != null ? `${data.persistence_days}d` : '\u2014'
  const mean30d = data.ratio_30d_mean != null ? data.ratio_30d_mean.toFixed(4) : '\u2014'
  const min30d  = data.ratio_30d_min  != null ? data.ratio_30d_min.toFixed(4)  : '\u2014'
  const max30d  = data.ratio_30d_max  != null ? data.ratio_30d_max.toFixed(4)  : '\u2014'
  const asOf = data.as_of ?? ''

  return (
    <div className="bg-[var(--color-surface)] border border-[var(--color-border)] rounded-xl p-5 dash-card">
      {/* Header row */}
      <div className="flex items-center justify-between mb-3">
        <div className="text-[10px] uppercase tracking-wider text-[var(--color-text-muted)] font-medium">
          VIX Term Structure
        </div>
        {asOf && (
          <div className="text-[10px] font-mono text-[var(--color-text-muted)]">{asOf}</div>
        )}
      </div>

      {/* Main ratio + regime badge */}
      <div className="flex items-center gap-3 mb-3">
        <span className="font-mono text-2xl text-[var(--color-text)]">{ratio}</span>
        {regimeBadge(data.regime)}
      </div>

      {/* VIX | VIX3M | Persistence sub-row */}
      <div className="flex items-center gap-4 mb-3 text-xs font-mono text-[var(--color-text-muted)]">
        <span>VIX <span className="text-[var(--color-text)]">{vix}</span></span>
        <span className="text-[var(--color-border)]">|</span>
        <span>VIX3M <span className="text-[var(--color-text)]">{vix3m}</span></span>
        <span className="text-[var(--color-border)]">|</span>
        <span>Persistence: <span className="text-[var(--color-text)]">{persistence}</span></span>
      </div>

      {/* Action badge */}
      <div className="flex items-center gap-2 mb-3">
        <span className="text-[10px] uppercase tracking-wide text-[var(--color-text-muted)] font-medium">Action:</span>
        {actionBadge(data.action)}
      </div>

      {/* 30-day range fine print */}
      <div className="text-[10px] font-mono text-[var(--color-text-muted)] bg-[var(--color-surface-alt)] rounded-md px-3 py-1.5">
        30d: {min30d} \u2014 {max30d} <span className="text-[var(--color-text-muted)]/60">(mean {mean30d})</span>
      </div>
    </div>
  )
}
