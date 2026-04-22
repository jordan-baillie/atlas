import type { RegimeHistoryDay } from '../../api/types'
import { getRegimeColor, CANONICAL_REGIME_NAMES } from '../../lib/colors'

interface Props { history: RegimeHistoryDay[] }

interface Segment {
  state: string
  startIdx: number
  endIdx: number
  count: number
}

function buildSegments(days: RegimeHistoryDay[]): Segment[] {
  if (days.length === 0) return []
  const segments: Segment[] = []
  let current: Segment = { state: days[0].state ?? '', startIdx: 0, endIdx: 0, count: 1 }
  for (let i = 1; i < days.length; i++) {
    const s = days[i].state ?? ''
    if (s === current.state) {
      current.endIdx = i
      current.count++
    } else {
      segments.push(current)
      current = { state: s, startIdx: i, endIdx: i, count: 1 }
    }
  }
  segments.push(current)
  return segments
}

export function RegimeTimeline({ history }: Props) {
  const last90 = history.slice(-90)
  const states = Array.from(new Set(last90.map((d) => d.state).filter((s): s is string => s != null)))
  const segments = buildSegments(last90)
  const totalDays = last90.length
  const currentState = last90.length > 0 ? last90[last90.length - 1].state : undefined

  return (
    <div>
      <div className="flex items-center justify-between mb-3">
        <div className="text-[11px] uppercase tracking-[0.12em] text-[var(--color-text-muted)] font-semibold">
          90-DAY REGIME HISTORY
        </div>
        {currentState && (
          <div className="flex items-center gap-1.5">
            <span className="inline-block w-2 h-2 rounded-full" style={{ backgroundColor: getRegimeColor(currentState) }} />
            <span className="text-xs font-mono" style={{ color: getRegimeColor(currentState) }}>
              {currentState}
            </span>
          </div>
        )}
      </div>

      <div className="flex h-8 rounded-lg overflow-hidden gap-px bg-[var(--color-border)]/30">
        {segments.map((seg, i) => {
          const widthPct = (seg.count / totalDays) * 100
          const startDate = last90[seg.startIdx]?.date ?? ''
          const endDate = last90[seg.endIdx]?.date ?? ''
          return (
            <div
              key={i}
              className="relative group"
              style={{
                width: `${widthPct}%`,
                backgroundColor: getRegimeColor(seg.state),
                borderRadius: i === 0 ? '0.5rem 0 0 0.5rem' : i === segments.length - 1 ? '0 0.5rem 0.5rem 0' : '0',
              }}
              title={`${seg.state}: ${startDate} — ${endDate} (${seg.count}d)`}
            >
              {widthPct > 12 && (
                <span className="absolute inset-0 flex items-center justify-center text-[9px] font-mono text-white/80 font-medium">
                  {seg.count}d
                </span>
              )}
            </div>
          )
        })}
      </div>

      <div className="flex justify-between mt-1.5 text-[10px] text-[var(--color-text-muted)] font-mono">
        <span>{last90[0]?.date ?? ''}</span>
        <span>{last90[last90.length - 1]?.date ?? ''}</span>
      </div>

      <div className="flex flex-wrap gap-3 mt-3">
        {CANONICAL_REGIME_NAMES.map((s) => {
          const isCurrent = s === currentState
          const seen = states.includes(s)
          if (!seen && !isCurrent) return null
          return (
            <div
              key={s}
              className={`flex items-center gap-2 text-xs font-mono px-2 py-1 rounded-md transition-colors ${
                isCurrent ? 'bg-[var(--color-surface-alt)] ring-1 ring-[var(--color-border)]' : ''
              }`}
            >
              <span
                className="inline-block rounded-full"
                style={{ width: 8, height: 8, backgroundColor: getRegimeColor(s) }}
              />
              <span className={isCurrent ? 'text-[var(--color-text)] font-semibold' : 'text-[var(--color-text-muted)]'}>
                {s}
              </span>
            </div>
          )
        })}
      </div>
    </div>
  )
}
