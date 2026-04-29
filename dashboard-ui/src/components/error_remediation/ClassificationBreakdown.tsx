interface Summary {
  by_classification: Record<string, number>
  errors_total: number
}

interface Props {
  summary: Summary
}

const CLASS_COLORS: Record<string, string> = {
  AUTO_FIX: 'var(--color-red)',
  ASSIST: '#3b82f6',            // blue-500
  ESCALATE: '#f97316',          // orange-500
  ESCALATE_DEFERRED: '#fb923c', // orange-400
  IGNORE: 'var(--color-green)',
  IGNORE_PENDING_CLEAR: '#86efac', // green-300
  UNCLASSIFIED: 'var(--color-text-muted)',
}

const CLASS_LABEL: Record<string, string> = {
  AUTO_FIX: 'Auto Fix',
  ASSIST: 'Assist',
  ESCALATE: 'Escalate',
  ESCALATE_DEFERRED: 'Escalate (Deferred)',
  IGNORE: 'Ignore',
  IGNORE_PENDING_CLEAR: 'Ignore (Pending)',
  UNCLASSIFIED: 'Unclassified',
}

// Preferred display order
const ORDER = ['AUTO_FIX', 'ASSIST', 'ESCALATE', 'ESCALATE_DEFERRED', 'IGNORE', 'IGNORE_PENDING_CLEAR', 'UNCLASSIFIED']

export function ClassificationBreakdown({ summary }: Props) {
  const { by_classification } = summary
  const total = Object.values(by_classification).reduce((a, b) => a + b, 0)

  // Build segments in preferred order, then any unlisted keys
  const known = ORDER.filter((k) => (by_classification[k] ?? 0) > 0)
  const extra = Object.keys(by_classification).filter((k) => !ORDER.includes(k) && by_classification[k] > 0)
  const segments = [...known, ...extra]

  return (
    <div className="bg-[var(--color-surface)] border border-[var(--color-border)] rounded-xl p-5">
      <div className="text-[10px] uppercase tracking-wider text-[var(--color-text-muted)] font-medium mb-3">
        Classification Breakdown
      </div>

      {total === 0 ? (
        <div className="text-sm text-[var(--color-text-muted)]">No errors recorded yet.</div>
      ) : (
        <>
          {/* Stacked bar */}
          <div className="flex h-6 rounded-md overflow-hidden gap-px mb-3" role="img" aria-label="Classification distribution">
            {segments.map((cls) => {
              const count = by_classification[cls] ?? 0
              const pct = total > 0 ? (count / total) * 100 : 0
              if (pct < 0.5) return null
              return (
                <div
                  key={cls}
                  className="transition-all duration-300"
                  style={{
                    width: `${pct}%`,
                    backgroundColor: CLASS_COLORS[cls] ?? '#6b7280',
                    minWidth: '2px',
                  }}
                  title={`${CLASS_LABEL[cls] ?? cls}: ${count} (${pct.toFixed(1)}%)`}
                />
              )
            })}
          </div>

          {/* Legend */}
          <div className="flex flex-wrap gap-x-4 gap-y-2 text-xs">
            {segments.map((cls) => {
              const count = by_classification[cls] ?? 0
              const pct = total > 0 ? (count / total) * 100 : 0
              return (
                <div key={cls} className="flex items-center gap-1.5">
                  <span
                    className="inline-block w-2.5 h-2.5 rounded-sm flex-shrink-0"
                    style={{ backgroundColor: CLASS_COLORS[cls] ?? '#6b7280' }}
                  />
                  <span className="text-[var(--color-text-muted)]">{CLASS_LABEL[cls] ?? cls}</span>
                  <span className="font-mono text-[var(--color-text)]">{count}</span>
                  <span className="text-[var(--color-text-muted)]">({pct.toFixed(1)}%)</span>
                </div>
              )
            })}
          </div>
        </>
      )}
    </div>
  )
}
