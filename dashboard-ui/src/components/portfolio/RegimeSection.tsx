import type { RegimeHistoryDay, RegimeTransitions } from '../../api/types'
import { RegimeTimeline } from './RegimeTimeline'
import { RegimeMatrix } from './RegimeMatrix'

interface Props { history: RegimeHistoryDay[]; transitions: RegimeTransitions }

export function RegimeSection({ history, transitions }: Props) {
  return (
    <div className="bg-[var(--color-surface)] border border-[var(--color-border)] rounded-xl p-5 space-y-6 dash-card">
      <div className="text-[11px] uppercase tracking-[0.12em] text-[var(--color-text-muted)] font-semibold mb-3">REGIME ANALYSIS</div>
      <RegimeTimeline history={history} />
      <RegimeMatrix transitions={transitions} />
    </div>
  )
}
