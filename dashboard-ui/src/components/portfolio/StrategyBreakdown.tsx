import { useSignalEV } from '../../api/queries'
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
  {
    key: 'ev_per_trade',
    label: 'EV/Trade',
    align: 'right',
    render: (r) => {
      const ev = r.ev_per_trade
      if (ev == null) return <span className="text-zinc-500">—</span>
      const cls = r.ev_classification === 'positive' ? 'text-green-400'
                : r.ev_classification === 'negative' ? 'text-red-400'
                : 'text-zinc-400'
      return <span className={`font-mono ${cls}`}>{fmtSignedCcy(ev)}</span>
    },
  },
]

export function StrategyBreakdown({ performance }: Props) {
  const { data: evData } = useSignalEV()
  const evMap = new Map((evData?.strategies ?? []).map(s => [s.strategy, s]))

  const baseRows: Row[] = Object.entries(performance?.by_strategy ?? {}).map(([name, entry]) => ({ name, ...entry }))
  const rows: Row[] = baseRows.map(r => {
    const ev = evMap.get(r.name)
    return ev ? { ...r, ev_per_trade: ev.ev_per_trade, ev_classification: ev.classification } : r
  })

  return (
    <div>
      <div className="text-[11px] uppercase tracking-[0.12em] text-[var(--color-text-muted)] font-semibold mb-3">STRATEGY BREAKDOWN</div>
      <DataTable columns={COLUMNS} data={rows} emptyMessage="No strategy data" />
    </div>
  )
}
