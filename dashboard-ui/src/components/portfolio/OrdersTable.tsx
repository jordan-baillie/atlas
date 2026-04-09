// Rule: rendering-content-visibility — long lists defer offscreen layout/paint
import type { Order } from '../../api/types'
import { DataTable } from '../shared/DataTable'
import type { Column } from '../shared/DataTable'
import { fmtCcy, fmtNum, fmtRelativeTime } from '../../lib/format'

interface Props { orders: Order[] }

function sideBadge(side?: 'buy' | 'sell') {
  if (!side) return <span className="font-mono text-xs text-[var(--color-text-muted)]">\u2014</span>
  const cls =
    side === 'buy'
      ? 'bg-[var(--color-green)]/20 text-[var(--color-green)]'
      : 'bg-[var(--color-red)]/20 text-[var(--color-red)]'
  return (
    <span className={`rounded-md px-2 py-0.5 text-[10px] font-mono ${cls}`}>
      {side.toUpperCase()}
    </span>
  )
}

function statusBadge(status?: string) {
  const s = (status ?? '').toUpperCase()
  let cls: string
  if (s === 'FILLED') {
    cls = 'bg-[var(--color-green)]/20 text-[var(--color-green)]'
  } else if (s === 'REJECTED' || s === 'CANCELED' || s === 'CANCELLED') {
    cls = 'bg-[var(--color-red)]/20 text-[var(--color-red)]'
  } else if (s === 'PENDING' || s === 'NEW') {
    cls = 'bg-[#f59e0b]/20 text-[#f59e0b]'
  } else {
    cls = 'bg-[var(--color-surface-alt)] text-[var(--color-text-muted)]'
  }
  return (
    <span className={`rounded-md px-2 py-0.5 text-[10px] font-mono ${cls}`}>
      {s || '\u2014'}
    </span>
  )
}

// Rule: rendering-hoist-jsx — static config outside component
const COLUMNS: Column<Order>[] = [
  {
    key: 'submitted_at',
    label: 'Time',
    render: (o) => (
      <span className="font-mono text-xs text-[var(--color-text-muted)]">
        {fmtRelativeTime(o.submitted_at)}
      </span>
    ),
  },
  {
    key: 'ticker',
    label: 'Ticker',
    render: (o) => <span className="font-mono">{o.ticker ?? o.symbol ?? '\u2014'}</span>,
  },
  {
    key: 'side',
    label: 'Side',
    render: (o) => sideBadge(o.side),
  },
  {
    key: 'filled_qty',
    label: 'Qty',
    align: 'right',
    className: 'hidden sm:table-cell',
    render: (o) => (
      <span className="font-mono">{fmtNum(o.filled_qty ?? o.qty ?? o.requested_qty, 0)}</span>
    ),
  },
  {
    key: 'fill_price',
    label: 'Price',
    align: 'right',
    render: (o) => (
      <span className="font-mono">
        {fmtCcy(o.fill_price ?? o.filled_price ?? o.limit_price ?? o.requested_price)}
      </span>
    ),
  },
  {
    key: 'status',
    label: 'Status',
    align: 'center',
    render: (o) => statusBadge(o.status),
  },
]

export function OrdersTable({ orders }: Props) {
  const rows = orders.slice(0, 15)
  return (
    <div>
      <div className="text-[11px] uppercase tracking-[0.12em] text-[var(--color-text-muted)] font-semibold mb-3">
        RECENT ORDERS ({orders.length})
      </div>
      <DataTable columns={COLUMNS} data={rows} emptyMessage="No recent orders" />
    </div>
  )
}
