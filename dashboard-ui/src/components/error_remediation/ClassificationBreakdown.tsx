import { CATEGORICAL_5 } from '../../lib/chart-defaults'

interface Summary {
  by_classification: Record<string, number>
  errors_total: number
}

interface Props {
  summary: Summary
}

// Semantic colors for obvious classifications; CATEGORICAL_5 for others
// Note: AUTO_FIX=danger-red, IGNORE=success-green kept semantic intentionally.
// ASSIST/ESCALATE/ESCALATE_DEFERRED/IGNORE_PENDING_CLEAR use CATEGORICAL_5.
const CLASS_COLORS: Record<string, string> = {
  AUTO_FIX:             'var(--color-red)',
  ASSIST:               CATEGORICAL_5[0],   // indigo
  ESCALATE:             CATEGORICAL_5[2],   // amber
  ESCALATE_DEFERRED:    CATEGORICAL_5[3],   // pink
  IGNORE:               'var(--color-green)',
  IGNORE_PENDING_CLEAR: CATEGORICAL_5[4],   // purple
  UNCLASSIFIED:         'var(--color-text-muted)',
}

const CLASS_LABEL: Record<string, string> = {
  AUTO_FIX:             'Auto Fix',
  ASSIST:               'Assist',
  ESCALATE:             'Escalate',
  ESCALATE_DEFERRED:    'Escalate (Deferred)',
  IGNORE:               'Ignore',
  IGNORE_PENDING_CLEAR: 'Ignore (Pending)',
  UNCLASSIFIED:         'Unclassified',
}

// Preferred display order
const ORDER = ['AUTO_FIX', 'ASSIST', 'ESCALATE', 'ESCALATE_DEFERRED', 'IGNORE', 'IGNORE_PENDING_CLEAR', 'UNCLASSIFIED']

export function ClassificationBreakdown({ summary }: Props) {
  const { by_classification } = summary
  const total = Object.values(by_classification).reduce((a, b) => a + b, 0)

  const known = ORDER.filter((k) => (by_classification[k] ?? 0) > 0)
  const extra = Object.keys(by_classification).filter((k) => !ORDER.includes(k) && by_classification[k] > 0)
  const segments = [...known, ...extra]

  return (
    <div className="bg-[var(--color-surface)] border border-[var(--color-border)] rounded-xl p-5">
      <div className="text-[11px] uppercase tracking-widest text-[var(--color-text-muted)] font-semibold mb-3">
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
                    backgroundColor: CLASS_COLORS[cls] ?? CATEGORICAL_5[0],
                    minWidth: '2px',
                  }}
                  title={`${CLASS_LABEL[cls] ?? cls}: ${count} (${pct.toFixed(1)}%)`}
                />
              )
            })}
          </div>

          {/* Legend — tabular-nums for counts and percentages */}
          <div className="flex flex-wrap gap-x-4 gap-y-2">
            {segments.map((cls) => {
              const count = by_classification[cls] ?? 0
              const pct = total > 0 ? (count / total) * 100 : 0
              const color = CLASS_COLORS[cls] ?? CATEGORICAL_5[0]
              return (
                <div key={cls} className="flex items-center gap-1.5 text-xs">
                  <span
                    className="inline-block rounded-full flex-shrink-0"
                    style={{ width: 8, height: 8, backgroundColor: color }}
                    aria-hidden="true"
                  />
                  <span className="text-[var(--color-text-muted)]">{CLASS_LABEL[cls] ?? cls}</span>
                  <span className="font-mono tabular-nums text-[var(--color-text)]">{count.toLocaleString()}</span>
                  <span className="font-mono tabular-nums text-[var(--color-text-muted)]">({pct.toFixed(1)}%)</span>
                </div>
              )
            })}
          </div>
        </>
      )}
    </div>
  )
}
