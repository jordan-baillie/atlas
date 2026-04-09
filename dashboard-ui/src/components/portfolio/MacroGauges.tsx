import type { MacroGaugeData } from '../../api/types'
import { GaugeCard } from './GaugeCard'

interface Props { data: MacroGaugeData }

export function MacroGauges({ data }: Props) {
  return (
    <details className="bg-[var(--color-surface)] border border-[var(--color-border)] rounded-xl dash-card" open>
      <summary className="cursor-pointer list-none flex items-center justify-between p-5">
        <h3 className="text-[10px] uppercase tracking-wider text-[var(--color-text-muted)] font-medium">Macro Indicator Gauges</h3>
        <div className="text-xs font-mono text-[var(--color-text-muted)] bg-[var(--color-surface-alt)] rounded-md px-2 py-1">
          Composite: {data.composite != null ? data.composite.toFixed(3) : '\u2014'} \u2022 {data.date ?? ''}
        </div>
      </summary>
      <div className="p-5 pt-0 grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
        {(data.dimensions ?? []).map((dim, i) => <GaugeCard key={dim.name ?? i} dimension={dim} />)}
      </div>
    </details>
  )
}
