import type { RegimeHistoryDay } from '../../api/types'
import { getRegimeColor, REGIME_COLORS } from '../../lib/colors'

interface Props { history: RegimeHistoryDay[] }

export function RegimeTimeline({ history }: Props) {
  const last90 = history.slice(-90)
  const states = Array.from(new Set(last90.map((d) => d.state).filter((s): s is string => s != null)))

  return (
    <div>
      <div className="text-[11px] uppercase tracking-[0.12em] text-[var(--color-text-muted)] font-semibold mb-3">90-DAY REGIME HISTORY</div>
      <div className="flex h-8 rounded-lg overflow-hidden">
        {last90.map((d, i) => (
          <div
            key={i}
            className="flex-1"
            style={{ backgroundColor: getRegimeColor(d.state) }}
            title={`${d.date ?? ''}: ${d.state ?? ''}`}
          />
        ))}
      </div>
      <div className="flex flex-wrap gap-3 mt-3">
        {(states.length > 0 ? states : Object.keys(REGIME_COLORS)).map((s) => (
          <div key={s} className="flex items-center gap-2 text-xs font-mono">
            <span className="inline-block rounded-full" style={{ width: 8, height: 8, backgroundColor: getRegimeColor(s) }} />
            <span className="text-[var(--color-text-muted)]">{s}</span>
          </div>
        ))}
      </div>
    </div>
  )
}
