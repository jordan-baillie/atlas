import type { PositionRiskRow, StopProbabilityEntry } from '../../api/types'
import { DataTable } from '../shared/DataTable'
import type { Column } from '../shared/DataTable'
import { Badge } from '../shared/Badge'
import type { BadgeVariant } from '../shared/Badge'
import { fmtCcy, fmtPct, pnlClass } from '../../lib/format'
import { getStrategyColor } from '../../lib/colors'
import { memo, type ReactNode } from 'react'

interface Props {
  positions: PositionRiskRow[]
  stop_probability?: Record<string, StopProbabilityEntry>
}

// ---------------------------------------------------------------------------
// Status badge — maps risk_status string → Badge variant
// ---------------------------------------------------------------------------
const STATUS_VARIANT_MAP: Record<string, BadgeVariant> = {
  HIGH: 'danger',
  NORMAL: 'warning',
  LOW: 'success',
  NO_STOP: 'danger',
  CRITICAL: 'danger',
  WARNING: 'warning',
}

function statusBadge(status?: string): ReactNode {
  const s = (status ?? '').toUpperCase()
  const variant: BadgeVariant = STATUS_VARIANT_MAP[s] ?? 'neutral'
  return (
    <Badge variant={variant} size="xs">
      {s || '\u2014'}
    </Badge>
  )
}

// ---------------------------------------------------------------------------
// Vol regime badge — maps regime string → Badge variant
// ---------------------------------------------------------------------------
const VOL_REGIME_VARIANT: Record<string, BadgeVariant> = {
  low: 'success',
  normal: 'neutral',
  high: 'warning',
  extreme: 'danger',
}

function volRegimeBadge(regime?: string): ReactNode {
  const r = (regime ?? '').toLowerCase()
  const variant: BadgeVariant = VOL_REGIME_VARIANT[r] ?? 'neutral'
  return (
    <Badge variant={variant} size="xs">
      {r || '\u2014'}
    </Badge>
  )
}

// ---------------------------------------------------------------------------
// Stop distance — color text by proximity to stop (< 1% danger, < 2% warning)
// ---------------------------------------------------------------------------
function distancePctClass(pct?: number | null): string {
  if (pct == null) return 'text-[var(--color-text-muted)]'
  if (pct < 1) return 'text-[var(--color-red)]'
  if (pct < 2) return 'text-amber-400'
  return 'text-[var(--color-text)]'
}

// ---------------------------------------------------------------------------
// Suggested stop comparison — current distance vs vol-cone suggested
// ---------------------------------------------------------------------------
function stopComparison(row: PositionRiskRow): ReactNode {
  const sugg = row.vol_cone?.suggested_stop_distance_pct
  const cur = row.distance_pct
  if (sugg == null) return <span className="font-mono tabular-nums text-[var(--color-text-muted)]">{'\u2014'}</span>
  const suggPct = sugg * 100  // sugg is fraction, distance_pct is already %
  const isWiderThanSuggested = cur != null && cur >= suggPct
  return (
    <div className="font-mono tabular-nums text-xs">
      <div className={isWiderThanSuggested ? 'text-[var(--color-green)]' : 'text-amber-400'}>
        {suggPct.toFixed(2)}%
      </div>
      <div className="text-[var(--color-text-muted)]">vs {cur != null ? cur.toFixed(2) + '%' : '\u2014'}</div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Stop-touch probability badge — 3-tier: success / warning / danger
// (Note: original had 4-tier with orange at 50-75%. Collapsed to 3 tiers since
//  Badge primitive has no orange variant. Deviation noted in report.)
// ---------------------------------------------------------------------------
function stopTouchBadge(entry: StopProbabilityEntry): ReactNode {
  const prob20d = entry.horizons['20d']
  const variant: BadgeVariant =
    prob20d < 0.25 ? 'success' : prob20d < 0.50 ? 'warning' : 'danger'
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
    <Badge variant={variant} size="xs" title={tooltipText}>
      {displayPct}%
    </Badge>
  )
}

function makeColumns(stop_probability?: Record<string, StopProbabilityEntry>): Column<PositionRiskRow>[] {
  return [
    {
      key: 'ticker',
      label: 'Ticker',
      render: (r) => <span className="font-mono">{r.ticker ?? '\u2014'}</span>,
    },
    {
      key: 'strategy',
      label: 'Strategy',
      render: (r) => (
        <div className="flex items-center gap-2 font-mono">
          <span
            className="inline-block rounded-full flex-shrink-0"
            style={{ width: 8, height: 8, backgroundColor: getStrategyColor(r.strategy) }}
          />
          {r.strategy ?? '\u2014'}
        </div>
      ),
    },
    {
      key: 'distance_pct',
      label: 'Stop Dist',
      align: 'right',
      render: (r) => (
        <div className="font-mono tabular-nums">
          <div className={distancePctClass(r.distance_pct)}>{fmtPct(r.distance_pct)}</div>
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
        if (!entry) return <span className="font-mono tabular-nums text-[var(--color-text-muted)]">{'\u2014'}</span>
        return stopTouchBadge(entry)
      },
    },
    {
      key: 'max_loss',
      label: 'Max Loss',
      align: 'right',
      render: (r) => (
        <span className={`font-mono tabular-nums ${pnlClass(-1)}`}>{fmtCcy(r.max_loss)}</span>
      ),
    },
    {
      key: 'risk_pct_equity',
      label: 'Risk % Eq',
      align: 'right',
      render: (r) => <span className="font-mono tabular-nums">{fmtPct(r.risk_pct_equity)}</span>,
    },
    {
      key: 'risk_status',
      label: 'Status',
      align: 'center',
      render: (r) => statusBadge(r.risk_status),
    },
  ]
}

function RiskTableInner({ positions, stop_probability }: Props) {
  const columns = makeColumns(stop_probability)
  return <DataTable columns={columns} data={positions} emptyMessage="No positions" />
}

export const RiskTable = memo(RiskTableInner)
