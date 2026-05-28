/**
 * FunnelChart -- horizontal funnel for the discovery pipeline.
 *
 * Ports the Variant-D HTML prototype's funnel SVG to React.  Each stage
 * is a trapezoid whose width is proportional to its 7-day rolling total.
 * Drop-off pills between stages show pass-rate + drop %.
 *
 * Clicking a stage calls onSelect(stage.key).  The selected stage is
 * highlighted via the `selected` prop (URL-synced state lives one level
 * up in ResearchTab).
 */

import { useMemo } from 'react'
import type { DiscoveryFunnelDay } from '../../api/knowledge-types'

export type StageKey = 'papers_found' | 'papers_filtered' | 'specs_extracted' | 'strategies_generated'

interface StageDef {
  key: StageKey
  label: string
  short: string
}

const STAGES: StageDef[] = [
  { key: 'papers_found',         label: 'Papers found',        short: 'FOUND'    },
  { key: 'papers_filtered',      label: 'Filtered (score≥6)',  short: 'FILTER'   },
  { key: 'specs_extracted',      label: 'Specs extracted',     short: 'SPECS'    },
  { key: 'strategies_generated', label: 'Strategies generated', short: 'STRAT'   },
]

interface FunnelChartProps {
  funnel: DiscoveryFunnelDay[]
  selected: StageKey | null
  onSelect: (key: StageKey | null) => void
}

function sumWindow(funnel: DiscoveryFunnelDay[], days: number): Record<StageKey, number> {
  const slice = funnel.slice(-days)
  const totals: Record<StageKey, number> = {
    papers_found: 0,
    papers_filtered: 0,
    specs_extracted: 0,
    strategies_generated: 0,
  }
  for (const row of slice) {
    totals.papers_found        += row.papers_found
    totals.papers_filtered     += row.papers_filtered
    totals.specs_extracted     += row.specs_extracted
    totals.strategies_generated += row.strategies_generated
  }
  return totals
}

export function FunnelChart({ funnel, selected, onSelect }: FunnelChartProps) {
  const totals7d = useMemo(() => sumWindow(funnel, 7), [funnel])
  const max = Math.max(totals7d.papers_found, 1)

  const stages = STAGES.map((stage, idx) => {
    const val = totals7d[stage.key]
    const widthPct = Math.max(8, (val / max) * 100)
    const prevKey = idx > 0 ? STAGES[idx - 1].key : null
    const prevVal = prevKey ? totals7d[prevKey] : null
    const passRate = prevVal && prevVal > 0 ? (val / prevVal) * 100 : null
    const dropPct = passRate != null ? 100 - passRate : null
    return { ...stage, val, widthPct, passRate, dropPct }
  })

  return (
    <div className="space-y-3">
      <div className="text-[10px] uppercase tracking-wider text-[var(--color-text-muted)] font-medium">
        Discovery funnel — last 7 days
      </div>

      <div className="flex flex-col gap-2">
        {stages.map((s, i) => {
          const isSelected = selected === s.key
          return (
            <div key={s.key} className="flex flex-col gap-1">
              <div className="flex items-center justify-between text-xs">
                <div className="flex items-center gap-2">
                  <span className="text-[10px] uppercase tracking-wider text-[var(--color-text-muted)] font-mono">
                    {s.short}
                  </span>
                  <span className="text-[var(--color-text)]">{s.label}</span>
                </div>
                {i > 0 && s.passRate != null && (
                  <div
                    className="text-[10px] font-mono px-1.5 py-0.5 rounded-full border"
                    style={{
                      color:
                        s.passRate >= 50 ? 'var(--color-green)'
                        : s.passRate >= 20 ? 'var(--color-amber, #f59e0b)'
                        : 'var(--color-red)',
                      borderColor: 'var(--color-border)',
                    }}
                  >
                    {s.passRate.toFixed(0)}% pass · −{(s.dropPct ?? 0).toFixed(0)}%
                  </div>
                )}
              </div>
              <button
                onClick={() => onSelect(isSelected ? null : s.key)}
                aria-pressed={isSelected}
                className={`group relative h-8 rounded-md text-left overflow-hidden transition-all ${
                  isSelected
                    ? 'ring-2 ring-[var(--color-accent)] ring-offset-1 ring-offset-[var(--color-surface)]'
                    : 'hover:brightness-125'
                }`}
                style={{
                  width: `${s.widthPct}%`,
                  minWidth: '120px',
                  background: `linear-gradient(90deg,
                    var(--color-accent) 0%,
                    ${i === 0 ? 'var(--color-accent)'
                      : i === 1 ? '#14b8a6'
                      : i === 2 ? '#6366f1'
                      : '#a855f7'} 100%)`,
                  opacity: isSelected ? 1 : 0.85,
                }}
              >
                <div className="absolute inset-0 flex items-center justify-between px-3 text-xs font-mono text-white/95">
                  <span className="font-semibold">{s.val.toLocaleString()}</span>
                  <span className="opacity-80 text-[10px]">{(s.val / 7).toFixed(1)}/day</span>
                </div>
              </button>
            </div>
          )
        })}
      </div>
    </div>
  )
}

/** Stage labels exported so other components can render the same short keys. */
export const STAGE_DEFS = STAGES
