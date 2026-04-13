import type { PositionRiskRow, StopProbabilityEntry } from '../../api/types'
import { DataTable } from '../shared/DataTable'
import type { Column } from '../shared/DataTable'
import { fmtCcy, fmtPct, pnlClass } from '../../lib/format'
import { getStrategyColor } from '../../lib/colors'
import type { ReactNode } from 'react'

interface Props {
  positions: PositionRiskRow[]
  stop_probability?: Record<string, StopProbabilityEntry>
}

function statusBadge(status?: string): ReactNode {
  const s = (status ?? '').toUpperCase()
  const map: Record<string, string> = {
    HIGH: 'bg-[var(--color-red)]/20 text-[var(--color-red)]',
    NORMAL: 'bg-[#f59e0b]/20 text-[#f59e0b]',
    LOW: 'bg-[var(--color-green)]/20 text-[var(--color-green)]',
    NO_STOP: 'bg-[var(--color-red)]/20 text-[var(--color-red)]',
    CRITICAL: 'bg-[var(--color-red)]/20 text-[var(--color-red)]',
    WARNING: 'bg-[#f59e0b]/20 text-[#f59e0b]',
  }
  const cls = map[s] ?? 'bg-[var(--color-surface-alt)] text-[var(--color-text-muted)]'
  return <span className={`rounded-md px-2 py-0.5 text-[10px] font-mono ${cls}`}>{s || '\u2014'}</span>
}

function volRegimeBadge(regime?: string): ReactNode {
  const r = (regime ?? '').toLowerCase()
  const map: Record<string, string> = {
    low: 'bg-[var(--color-green)]/20 text-[var(--color-green)]',
    normal: 'bg-[var(--color-surface-alt)] text-[var(--color-text-muted)]',
    high: 'bg-[#f59e0b]/20 text-[#f59e0b]',
    extreme: 'bg-[var(--color-red)]/20 text-[var(--color-red)]',
  }
  const cls = map[r] ?? 'bg-[var(--color-surface-alt)] text-[var(--color-text-muted)]'
  return <span className={`rounded-md px-2 py-0.5 text-[10px] font-mono uppercase ${cls}`}>{r || '\u2014'}</span>
}

function stopComparison(row: PositionRiskRow): ReactNode {
  const sugg = row.vol_cone?.suggested_stop_distance_pct
  const cur = row.distance_pct
  if (sugg == null) return <span className="font-mono text-[var(--color-text-muted)]">\u2014</span>
  const suggPct = sugg * 100  // sugg is fraction, distance_pct is already %
  const isWiderThanSuggested = cur != null && cur >= suggPct
  return (
    <div className="font-mono text-xs">
      <div className={isWiderThanSuggested ? 'text-[var(--color-green)]' : 'text-[#f59e0b]'}>
        {suggPct.toFixed(2)}%
      </div>
      <div className="text-[var(--color-text-muted)]">vs {cur != null ? cur.toFixed(2) + '%' : '\u2014'}</div>
    </div>
  )
}

/** Color class for a 20-day stop-touch probability value */
function stopTouchColor(prob: number): string {
  if (prob < 0.25)  return 'bg-[var(--color-green)]/20 text-[var(--color-green)]'
  if (prob < 0.50)  return 'bg-[#f59e0b]/20 text-[#f59e0b]'
  if (prob < 0.75)  return 'bg-[#f97316]/20 text-[#f97316]'
  return 'bg-[var(--color-red)]/20 text-[var(--color-red)]'
}

function stopTouchBadge(entry: StopProbabilityEntry): ReactNode {
  const prob20d = entry.horizons['20d']
  const cls = stopTouchColor(prob20d)
  const displayPct = Math.round(prob20d * 100)
  const volPct = Math.round(entry.vol_annual * 100)
  const h = entry.horizons
  const tooltipText =
    `vol=${volPct}% | ` +
    `1d ${Math.round(h['1d'] * 100)}% • ` +
    `5d ${Math.round(h['5d'] * 100)}% • ` +
    `10d ${Math.round(h['10d'] * 100)}% • ` +
    `20d ${Math.round(h['20d'] * 100)}% | ` +
    `EL $${entry.expected_loss_20d.toFixed(0)}`
  return (
    <span
      className={`rounded-md px-2 py-0.5 text-[10px] font-mono cursor-default ${cls}`}
      title={tooltipText}
    >
      {displayPct}%
    </span>
  )
}

function makeColumns(stop_probability?: Record<string, StopProbabilityEntry>): Column<PositionRiskRow>[] {
  return [
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
      key: 'vol_cone',
      label: 'Vol Regime',
      align: 'center',
      render: (r) => volRegimeBadge(r.vol_cone?.regime),
    },
    {
      key: 'suggested_stop',
      label: 'Sugg Stop',
      align: 'right',
      render: (r) => stopComparison(r),
    },
    {
      key: 'stop_touch_20d',
      label: 'Stop Touch (20d)',
      align: 'center',
      render: (r) => {
        const entry = stop_probability?.[r.ticker ?? '']
        if (!entry) return <span className="font-mono text-[var(--color-text-muted)]">\u2014</span>
        return stopTouchBadge(entry)
      },
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
}

export function RiskTable({ positions, stop_probability }: Props) {
  const columns = makeColumns(stop_probability)
  return <DataTable columns={columns} data={positions} emptyMessage="No positions" />
}
