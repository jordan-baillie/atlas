// Rule: rendering-content-visibility — long lists defer offscreen layout/paint
import type { RegimeTransitions } from '../../api/types'

interface Props { transitions: RegimeTransitions }

export function RegimeMatrix({ transitions }: Props) {
  const states = transitions.states ?? []
  const matrix = transitions.matrix ?? {}
  const durations = transitions.durations ?? {}
  const current = transitions.current_state

  return (
    <div className="space-y-4">
      <div className="text-[11px] uppercase tracking-[0.12em] text-[var(--color-text-muted)] font-semibold mb-3">REGIME TRANSITION MATRIX</div>
      <div className="overflow-x-auto">
        <table className="min-w-[520px] w-full text-xs font-mono border-separate border-spacing-0">
          <thead>
            <tr>
              <th className="p-2 text-left text-[var(--color-text-muted)] font-normal">From \ To</th>
              {states.map((s) => (
                <th key={s} className="p-2 text-center text-[var(--color-text-muted)] font-normal whitespace-nowrap">{s}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {states.map((from) => {
              const isCurrent = from === current
              return (
                <tr key={from} className={isCurrent ? 'ring-2 ring-[var(--color-accent,#6366f1)]' : ''}>
                  <td className="p-2 text-[var(--color-text-muted)] whitespace-nowrap">{from}</td>
                  {states.map((to) => {
                    // API returns values as percentages (0-100), not decimals.
                    const p = matrix[from]?.[to] ?? 0
                    return (
                      <td
                        key={to}
                        className="p-2 text-center"
                        style={{ backgroundColor: `rgba(99,102,241, ${Math.min(p / 100, 1)})` }}
                      >
                        {p.toFixed(1)}%
                      </td>
                    )
                  })}
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>
      <div className="grid grid-cols-3 sm:grid-cols-2 gap-3">
        {states.map((s) => {
          const d = durations[s]
          const isCurrent = s === current
          return (
            <div
              key={s}
              className={`bg-[var(--color-surface-alt)] border border-[var(--color-border)] rounded-lg p-3 ${isCurrent ? 'ring-2 ring-[var(--color-accent,#6366f1)]' : ''}`}
            >
              <div className="text-xs font-mono font-semibold mb-1">{s}</div>
              <div className="text-[10px] text-[var(--color-text-muted)] space-y-0.5 font-mono">
                <div>avg: {d?.avg_days?.toFixed(1) ?? '\u2014'}d</div>
                <div>max: {d?.max_days ?? '\u2014'}d</div>
                <div>total: {d?.total_days ?? '\u2014'}d</div>
              </div>
            </div>
          )
        })}
      </div>
    </div>
  )
}
