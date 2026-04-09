import type { StrategyPerformance, StrategyPerfEntry } from '../../api/types'
import { DataTable } from '../shared/DataTable'
import type { Column } from '../shared/DataTable'
import { fmtSignedCcy, fmtPct, pnlClass } from '../../lib/format'
import { getStrategyColor } from '../../lib/colors'

interface Row extends StrategyPerfEntry { name: string }
interface Props { performance?: StrategyPerformance }

// Rule: rendering-hoist-jsx — static config outside component
const COLUMNS: Column<Row>[] = [
  {
    key: 'name',
    label: 'Strategy',
    render: (r) => (
      <div className="flex items-center gap-2 font-mono">
        <span className="inline-block rounded-full" style={{ width: 8, height: 8, backgroundColor: getStrategyColor(r.name) }} />
        {r.name}
      </div>
    ),
  },
  { key: 'trades', label: 'Trades', align: 'right', render: (r) => String(r.trades ?? 0) },
  {
    key: 'pnl',
    label: 'P&L',
    align: 'right',
    render: (r) => <span className={`font-mono ${pnlClass(r.pnl)}`}>{fmtSignedCcy(r.pnl)}</span>,
  },
  {
    key: 'win_rate',
    label: 'Win Rate',
    align: 'right',
    render: (r) => {
      const total = r.trades ?? 0
      const wins = r.wins ?? 0
      const rate = total > 0 ? (wins / total) * 100 : null
      return (
        <div className="font-mono">
          {fmtPct(rate)} <span className="text-[var(--color-text-muted)] text-xs">({wins}/{total})</span>
        </div>
      )
    },
  },
]

export function StrategyBreakdown({ performance }: Props) {
  const rows: Row[] = Object.entries(performance?.by_strategy ?? {}).map(([name, entry]) => ({ name, ...entry }))

  return (
    <div>
      <div className="text-[11px] uppercase tracking-[0.12em] text-[var(--color-text-muted)] font-semibold mb-3">STRATEGY BREAKDOWN</div>
      <DataTable columns={COLUMNS} data={rows} emptyMessage="No strategy data" />
    </div>
  )
}
