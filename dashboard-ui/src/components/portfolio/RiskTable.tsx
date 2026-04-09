import type { PositionRiskRow } from '../../api/types'
import { DataTable } from '../shared/DataTable'
import type { Column } from '../shared/DataTable'
import { fmtCcy, fmtPct, pnlClass } from '../../lib/format'
import { getStrategyColor } from '../../lib/colors'
import type { ReactNode } from 'react'

interface Props { positions: PositionRiskRow[] }

function statusBadge(status?: string): ReactNode {
  const s = (status ?? '').toUpperCase()
  const map: Record<string, string> = {
    HIGH: 'bg-[var(--color-red)]/20 text-[var(--color-red)]',
    NORMAL: 'bg-[#f59e0b]/20 text-[#f59e0b]',
    LOW: 'bg-[var(--color-green)]/20 text-[var(--color-green)]',
    NO_STOP: 'bg-[var(--color-red)]/20 text-[var(--color-red)]',
  }
  const cls = map[s] ?? 'bg-[var(--color-surface-alt)] text-[var(--color-text-muted)]'
  return <span className={`rounded-md px-2 py-0.5 text-[10px] font-mono ${cls}`}>{s || '\u2014'}</span>
}

// Rule: rendering-hoist-jsx — static config outside component
const COLUMNS: Column<PositionRiskRow>[] = [
  { key: 'ticker', label: 'Ticker', render: (r) => <span className="font-mono">{r.ticker ?? '\u2014'}</span> },
  {
    key: 'strategy',
    label: 'Strategy',
    render: (r) => (
      <div className="flex items-center gap-2 font-mono">
        <span className="inline-block rounded-full" style={{ width: 8, height: 8, backgroundColor: getStrategyColor(r.strategy) }} />
        {r.strategy ?? '\u2014'}
      </div>
    ),
  },
  {
    key: 'distance_pct',
    label: 'Stop Dist',
    align: 'right',
    render: (r) => (
      <div className="font-mono">
        {fmtPct(r.distance_pct)}
        <div className="text-[var(--color-text-muted)] text-xs">({fmtCcy(r.distance_dollars)})</div>
      </div>
    ),
  },
  {
    key: 'max_loss',
    label: 'Max Loss',
    align: 'right',
    render: (r) => <span className={`font-mono ${pnlClass(-1)}`}>{fmtCcy(r.max_loss)}</span>,
  },
  { key: 'risk_pct_equity', label: 'Risk % Eq', align: 'right', render: (r) => <span className="font-mono">{fmtPct(r.risk_pct_equity)}</span> },
  { key: 'risk_status', label: 'Status', align: 'center', render: (r) => statusBadge(r.risk_status) },
]

export function RiskTable({ positions }: Props) {
  return <DataTable columns={COLUMNS} data={positions} emptyMessage="No positions" />
}
