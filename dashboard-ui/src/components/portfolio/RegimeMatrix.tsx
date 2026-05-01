import { memo } from 'react'
import type { RegimeTransitions } from '../../api/types'
import { getRegimeColor } from '../../lib/colors'

interface Props { transitions: RegimeTransitions }

function cellBg(value: number): string {
  const intensity = Math.min(value / 100, 1)
  if (intensity < 0.05) return 'transparent'
  return `rgba(99, 102, 241, ${0.1 + intensity * 0.6})`
}

function cellText(value: number): string {
  return value > 50 ? 'rgba(255,255,255,0.95)' : ''
}

function RegimeMatrixInner({ transitions }: Props) {
  const states = transitions.states ?? []
  const matrix = transitions.matrix ?? {}
  const durations = transitions.durations ?? {}
  const current = transitions.current_state

  return (
    <div className="space-y-4">
      <div className="text-[11px] uppercase tracking-[0.12em] text-[var(--color-text-muted)] font-semibold mb-3">
        REGIME TRANSITION MATRIX
      </div>
      <div className="overflow-x-auto">
        <table className="min-w-[520px] w-full text-xs font-mono border-separate border-spacing-0">
          <thead>
            <tr>
              <th className="p-2.5 text-left text-[10px] uppercase tracking-wider text-[var(--color-text-muted)] font-semibold bg-[var(--color-surface-alt)]/50 rounded-tl-lg">
                From \ To
              </th>
              {states.map((s, i) => (
                <th
                  key={s}
                  className={`p-2.5 text-center text-[10px] uppercase tracking-wider font-semibold bg-[var(--color-surface-alt)]/50 whitespace-nowrap ${
                    i === states.length - 1 ? 'rounded-tr-lg' : ''
                  }`}
                  style={{ color: getRegimeColor(s) }}
                >
                  {s}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {states.map((from, ri) => {
              const isCurrent = from === current
              return (
                <tr key={from}>
                  <td
                    className={`p-2.5 whitespace-nowrap font-semibold ${
                      ri === states.length - 1 ? 'rounded-bl-lg' : ''
                    }`}
                    style={{ color: isCurrent ? getRegimeColor(from) : 'var(--color-text-muted)' }}
                  >
                    {isCurrent && (
                      <span className="inline-block w-1.5 h-1.5 rounded-full mr-1.5" style={{ backgroundColor: getRegimeColor(from) }} />
                    )}
                    {from}
                  </td>
                  {states.map((to, ci) => {
                    const p = matrix[from]?.[to] ?? 0
                    const isLast = ri === states.length - 1 && ci === states.length - 1
                    return (
                      <td
                        key={to}
                        className={`p-2.5 text-center tabular-nums hover:ring-1 hover:ring-[var(--color-accent)] hover:z-10 relative transition-shadow ${
                          isLast ? 'rounded-br-lg' : ''
                        }`}
                        style={{
                          backgroundColor: cellBg(p),
                          color: cellText(p) || undefined,
                        }}
                        title={`${from} → ${to}: ${p.toFixed(2)}%`}
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

      <div className="grid grid-cols-1 sm:grid-cols-2 md:grid-cols-3 gap-3">
        {states.map((s) => {
          const d = durations[s]
          const isCurrent = s === current
          const color = getRegimeColor(s)
          return (
            <div
              key={s}
              className="bg-[var(--color-surface)] border border-[var(--color-border)] rounded-lg p-3 hover:translate-y-[-1px] hover:shadow-md transition-all duration-200"
              style={isCurrent ? { borderLeftColor: color, borderLeftWidth: 3 } : undefined}
            >
              <div className="flex items-center gap-2 mb-1.5">
                <span className="inline-block w-2.5 h-2.5 rounded-full" style={{ backgroundColor: color }} />
                <span className="text-xs font-mono font-semibold">{s}</span>
                {isCurrent && (
                  <span className="rounded-full px-1.5 py-0 text-[9px] font-mono bg-[var(--color-accent)]/20 text-[var(--color-accent)]">
                    CURRENT
                  </span>
                )}
              </div>
              <div className="text-[10px] text-[var(--color-text-muted)] space-y-0.5 font-mono tabular-nums">
                <div className="flex justify-between">
                  <span>avg</span><span>{d?.avg_days?.toFixed(1) ?? '\u2014'}d</span>
                </div>
                <div className="flex justify-between">
                  <span>max</span><span>{d?.max_days ?? '\u2014'}d</span>
                </div>
                <div className="flex justify-between">
                  <span>total</span><span>{d?.total_days ?? '\u2014'}d</span>
                </div>
              </div>
            </div>
          )
        })}
      </div>
    </div>
  )
}

export const RegimeMatrix = memo(RegimeMatrixInner)
