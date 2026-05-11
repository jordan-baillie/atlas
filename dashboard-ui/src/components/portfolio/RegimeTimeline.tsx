import { memo } from 'react'
import type { RegimeHistoryDay } from '../../api/types'
import { getRegimeColor, CANONICAL_REGIME_NAMES } from '../../lib/colors'
import { Badge } from '../shared/Badge'

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

function RegimeTimelineInner({ history }: Props) {
  const last90 = history.slice(-90)
  const states = Array.from(
    new Set(last90.map((d) => d.state).filter((s): s is string => s != null)),
  )
  const segments = buildSegments(last90)
  const totalDays = last90.length
  const currentState = last90.length > 0 ? last90[last90.length - 1].state : undefined

  return (
    <div data-testid="regime-timeline">
      {/* Header row */}
      <div className="flex items-center justify-between mb-3">
        <div className="text-[11px] uppercase tracking-[0.12em] text-[var(--color-text-muted)] font-semibold">
          90-DAY REGIME HISTORY
        </div>
        {currentState && (
          <div className="flex items-center gap-1.5">
            <span
              className="inline-block w-2 h-2 rounded-full"
              style={{ backgroundColor: getRegimeColor(currentState) }}
            />
            <span className="text-xs font-mono" style={{ color: getRegimeColor(currentState) }}>
              {currentState}
            </span>
          </div>
        )}
      </div>

      {/* Timeline bar */}
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
                borderRadius:
                  i === 0
                    ? '0.5rem 0 0 0.5rem'
                    : i === segments.length - 1
                    ? '0 0.5rem 0.5rem 0'
                    : '0',
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

      {/* Date range labels */}
      <div className="flex justify-between mt-1.5 text-[10px] text-[var(--color-text-muted)] font-mono tabular-nums">
        <span>{last90[0]?.date ?? ''}</span>
        <span>{last90[last90.length - 1]?.date ?? ''}</span>
      </div>

      {/* Legend pills — Badge with custom-color icon dot */}
      <div className="flex flex-wrap gap-2 mt-3">
        {CANONICAL_REGIME_NAMES.map((s) => {
          const isCurrent = s === currentState
          const seen = states.includes(s)
          if (!seen && !isCurrent) return null
          const color = getRegimeColor(s)
          return (
            <Badge
              key={s}
              variant="neutral"
              size="xs"
              icon={
                <span
                  className="inline-block rounded-full flex-shrink-0"
                  style={{ width: 6, height: 6, backgroundColor: color }}
                  aria-hidden="true"
                />
              }
              className={
                isCurrent
                  ? 'ring-1 ring-[var(--color-border)] font-semibold !text-[var(--color-text)]'
                  : ''
              }
            >
              {s}
            </Badge>
          )
        })}
      </div>
    </div>
  )
}

export const RegimeTimeline = memo(RegimeTimelineInner)
