/**
 * LifecycleHistoryModal — shows the full transition history for a single
 * (strategy, universe) combination in a vertical timeline.
 * Fetched lazily on open via useLifecycleHistory(enabled=open).
 */

import { useEffect } from 'react'
import { useLifecycleHistory } from '../../api/lifecycle'
import { Badge } from '../shared/Badge'
import { fmtRelativeTime } from '../../lib/format'
import type { LifecycleState } from '../../api/lifecycle'
import type { BadgeVariant } from '../shared/Badge'

interface Props {
  strategy: string
  universe: string
  open: boolean
  onClose: () => void
}

function lcVariant(state: LifecycleState): BadgeVariant {
  if (state === 'RESEARCH') return 'info'
  if (state === 'PAPER') return 'warning'
  if (state === 'LIVE') return 'success'
  return 'neutral'  // RETIRED
}

export function LifecycleHistoryModal({ strategy, universe, open, onClose }: Props) {
  const { data, isLoading, error } = useLifecycleHistory(strategy, universe, open)

  // ── ESC to close ─────────────────────────────────────────────────────────
  useEffect(() => {
    if (!open) return
    function onKey(e: KeyboardEvent) {
      if (e.key === 'Escape') onClose()
    }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [open, onClose])

  if (!open) return null

  return (
    <div
      className="fixed inset-0 z-50 bg-black/60 backdrop-blur-sm flex items-center justify-center p-4"
      onClick={e => { if (e.target === e.currentTarget) onClose() }}
    >
      <div
        className="animate-in bg-[var(--color-surface)] border border-[var(--color-border)] rounded-xl
                   shadow-2xl max-w-lg w-full max-h-[80vh] flex flex-col p-6"
        onClick={e => e.stopPropagation()}
      >
        {/* Header */}
        <div className="flex items-start justify-between gap-2 mb-4 flex-shrink-0">
          <div>
            <h2 className="text-xl font-semibold">Lifecycle History</h2>
            <div className="text-xs text-[var(--color-text-muted)] mt-0.5 font-mono">
              {strategy} · {universe}
            </div>
          </div>
          <button
            onClick={onClose}
            className="text-[var(--color-text-muted)] hover:text-[var(--color-text)] text-xl leading-none mt-0.5 flex-shrink-0
                       w-8 h-8 flex items-center justify-center rounded-lg hover:bg-[var(--color-surface-alt)] transition-colors"
            aria-label="Close lifecycle history"
          >
            ×
          </button>
        </div>

        {/* Body — scrollable */}
        <div className="overflow-y-auto flex-1 min-h-0">
          {isLoading && (
            <div className="text-xs text-[var(--color-text-muted)] py-6 text-center">
              Loading history…
            </div>
          )}
          {error && (
            <div className="text-xs text-[var(--color-red)] py-6 text-center">
              Failed to load: {(error as Error).message}
            </div>
          )}
          {data?.history.length === 0 && (
            <div className="text-xs text-[var(--color-text-muted)] py-6 text-center">
              No transitions recorded yet.
            </div>
          )}
          {data && data.history.length > 0 && (
            <div className="relative pl-5">
              {/* Vertical connector line */}
              <div className="absolute left-[7px] top-2 bottom-2 w-px bg-[var(--color-border)]" />
              <div className="space-y-5">
                {data.history.map((entry, idx) => (
                  <div key={idx} className="relative">
                    {/* Timeline dot */}
                    <div className="absolute -left-[14px] top-[5px] w-2.5 h-2.5 rounded-full bg-[var(--color-surface-alt)] border border-[var(--color-border)]" />
                    <div className="space-y-1 text-xs">
                      {/* State change row */}
                      <div className="flex items-center gap-2 flex-wrap">
                        {entry.from_state && (
                          <>
                            <span data-testid="history-from-state">
                              <Badge variant={lcVariant(entry.from_state)} size="xs">
                                {entry.from_state}
                              </Badge>
                            </span>
                            <span className="text-[var(--color-text-muted)]">→</span>
                          </>
                        )}
                        <span data-testid="history-to-state">
                          <Badge variant={lcVariant(entry.to_state)} size="xs">
                            {entry.to_state}
                          </Badge>
                        </span>
                        <span
                          className="text-[var(--color-text-muted)] tabular-nums font-mono"
                          title={entry.transitioned_at}
                        >
                          {fmtRelativeTime(entry.transitioned_at)}
                        </span>
                      </div>
                      {/* Operator */}
                      {entry.operator && (
                        <div className="text-[var(--color-text-muted)]">
                          by{' '}
                          <span className="font-mono text-[var(--color-text)]">
                            {entry.operator}
                          </span>
                        </div>
                      )}
                      {/* Reason */}
                      {entry.reason && (
                        <div className="text-[var(--color-text-muted)] italic">
                          "{entry.reason}"
                        </div>
                      )}
                      {/* Auto-promotion reference */}
                      {entry.auto_promotion_id != null && (
                        <div className="text-[var(--color-text-muted)]">
                          auto-promotion{' '}
                          <span className="font-mono">#{entry.auto_promotion_id}</span>
                        </div>
                      )}
                    </div>
                  </div>
                ))}
              </div>
            </div>
          )}
        </div>

        {/* Footer */}
        <div className="flex justify-end mt-4 pt-3 border-t border-[var(--color-border)] flex-shrink-0">
          <button
            onClick={onClose}
            className="h-9 px-4 rounded-md text-sm text-[var(--color-text-muted)] hover:text-[var(--color-text)]
                       bg-[var(--color-surface-alt)] border border-[var(--color-border)]
                       hover:bg-[var(--color-border)] transition-colors"
          >
            Close
          </button>
        </div>
      </div>
    </div>
  )
}
