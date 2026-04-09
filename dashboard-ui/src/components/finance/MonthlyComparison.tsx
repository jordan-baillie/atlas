import type { MonthlyComparison as MonthlyComparisonRow } from '../../api/types'
import { DataTable } from '../shared/DataTable'
import type { Column } from '../shared/DataTable'
import { fmtCcy, fmtSignedCcy, pnlClass } from '../../lib/format'

interface Props { rows: MonthlyComparisonRow[] }

// Rule: rendering-hoist-jsx — static config outside component
const COLUMNS: Column<MonthlyComparisonRow>[] = [
  {
    key: 'month',
    label: 'Month',
    render: (r) => <span className="font-mono">{r.month ?? '\u2014'}</span>,
  },
  {
    key: 'income',
    label: 'Income',
    align: 'right',
    className: 'hidden sm:table-cell',
    render: (r) => <span className="font-mono">{fmtCcy(r.income)}</span>,
  },
  {
    key: 'spending',
    label: 'Spending',
    align: 'right',
    render: (r) => <span className="font-mono">{fmtCcy(r.spending)}</span>,
  },
  {
    key: 'net',
    label: 'Net',
    align: 'right',
    render: (r) => <span className={`font-mono ${pnlClass(r.net)}`}>{fmtSignedCcy(r.net)}</span>,
  },
]

export function MonthlyComparison({ rows }: Props) {
  return (
    <div>
      <div className="text-[11px] uppercase tracking-[0.12em] text-[var(--color-text-muted)] font-semibold mb-3">
        MONTHLY COMPARISON
      </div>
      <DataTable columns={COLUMNS} data={rows} emptyMessage="No comparison data" />
    </div>
  )
}
