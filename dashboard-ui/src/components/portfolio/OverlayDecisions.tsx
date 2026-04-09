// Rule: rendering-content-visibility — long lists defer offscreen layout/paint
import type { OverlayDecision } from '../../api/types'
import { EmptyState } from '../shared/EmptyState'
import { fmtRelativeTime } from '../../lib/format'

interface Props { decisions: OverlayDecision[] }

function confidenceBadge(confidence?: number) {
  if (confidence == null) return null
  const pct = (confidence * 100).toFixed(0)
  return (
    <span className="rounded-md px-2 py-0.5 text-[10px] font-mono bg-[var(--color-surface-alt)] text-[var(--color-text-muted)]">
      {pct}%
    </span>
  )
}

function modeBadge(mode?: string) {
  if (!mode) return null
  const cls =
    mode === 'active'
      ? 'bg-[var(--color-green)]/20 text-[var(--color-green)]'
      : 'bg-[var(--color-surface-alt)] text-[var(--color-text-muted)]'
  return (
    <span className={`rounded-md px-2 py-0.5 text-[10px] font-mono ${cls}`}>
      {mode}
    </span>
  )
}

export function OverlayDecisions({ decisions }: Props) {
  const items = decisions.slice(0, 10)
  return (
    <div>
      <div className="text-[10px] uppercase tracking-wider text-[var(--color-text-muted)] font-medium mb-3">
        AI OVERLAY DECISIONS ({decisions.length})
      </div>
      {items.length === 0 ? (
        <EmptyState message="No overlay decisions yet" />
      ) : (
        <div className="space-y-2">
          {items.map((d, i) => (
            <div key={d.id ?? i} className="rounded-md bg-[var(--color-surface)] p-3 cv-auto">
              <div className="flex items-start justify-between gap-3">
                <div className="flex items-start gap-3 min-w-0">
                  <span className="text-xs font-mono text-[var(--color-text-muted)] whitespace-nowrap pt-0.5">
                    {fmtRelativeTime(d.timestamp)}
                  </span>
                  <div className="min-w-0">
                    <div className="flex items-center gap-2 flex-wrap">
                      <span className="text-sm font-medium">{d.action ?? d.decision ?? '\u2014'}</span>
                      {d.symbol && (
                        <span className="rounded-md px-2 py-0.5 text-[10px] font-mono bg-[var(--color-surface-alt)] text-[var(--color-text-muted)]">
                          {d.symbol}
                        </span>
                      )}
                      {d.strategy && (
                        <span className="rounded-md px-2 py-0.5 text-[10px] font-mono bg-[var(--color-surface-alt)] text-[var(--color-text-muted)]">
                          {d.strategy}
                        </span>
                      )}
                    </div>
                    {(d.rationale ?? d.reasoning) && (
                      <div className="text-xs text-[var(--color-text-muted)] mt-1 line-clamp-2">
                        {d.rationale ?? d.reasoning}
                      </div>
                    )}
                  </div>
                </div>
                <div className="flex items-center gap-1.5 shrink-0">
                  {confidenceBadge(d.confidence)}
                  {modeBadge(d.mode)}
                </div>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
