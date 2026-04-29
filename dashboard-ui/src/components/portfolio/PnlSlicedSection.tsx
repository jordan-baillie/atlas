import { useState, useMemo } from 'react'
import { AreaChart, Area, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer, ReferenceLine } from 'recharts'
import { ChartGate } from '../shared/ChartGate'
import { EmptyState } from '../shared/EmptyState'
import { Skeleton } from '../layout/Skeleton'
import { ChartTooltip } from '../shared/ChartTooltip'
import { usePnlFilterOptions, usePnlTrades } from '../../api/queries'
import type { PnlFilters } from '../../api/queries'
import { fmtSignedCcy, fmtDateShort, pnlClass } from '../../lib/format'
import { useCssVars } from '../../hooks/useCssVar'

// ---------------------------------------------------------------------------
// PnlSlicerRow — horizontal row of 3 dropdowns
// ---------------------------------------------------------------------------
const SELECT_CLASS =
  'text-xs bg-[var(--color-surface-alt)] border border-[var(--color-border)] rounded-lg px-3 py-1.5 text-[var(--color-text)] cursor-pointer'

interface SlicerRowProps {
  filters: PnlFilters
  markets: string[]
  strategies: string[]
  sectors: string[]
  onChange: <K extends keyof PnlFilters>(key: K, value: string) => void
}

function PnlSlicerRow({ filters, markets, strategies, sectors, onChange }: SlicerRowProps) {
  return (
    <div className="flex flex-wrap gap-2">
      <select
        value={filters.market_id}
        onChange={(e) => onChange('market_id', e.target.value)}
        className={SELECT_CLASS}
        aria-label="Filter by market"
      >
        <option value="">All Markets</option>
        {markets.map((m) => (
          <option key={m} value={m}>{m}</option>
        ))}
      </select>

      <select
        value={filters.strategy}
        onChange={(e) => onChange('strategy', e.target.value)}
        className={SELECT_CLASS}
        aria-label="Filter by strategy"
      >
        <option value="">All Strategies</option>
        {strategies.map((s) => (
          <option key={s} value={s}>{s.replace(/_/g, ' ')}</option>
        ))}
      </select>

      <select
        value={filters.sector}
        onChange={(e) => onChange('sector', e.target.value)}
        className={SELECT_CLASS}
        aria-label="Filter by sector"
      >
        <option value="">All Sectors</option>
        {sectors.map((s) => (
          <option key={s} value={s}>{s}</option>
        ))}
      </select>
    </div>
  )
}

// ---------------------------------------------------------------------------
// CumulativePnlBadge — small stat badge showing total filtered P&L
// ---------------------------------------------------------------------------
interface CumulativePnlBadgeProps {
  totalPnl: number
}

function CumulativePnlBadge({ totalPnl }: CumulativePnlBadgeProps) {
  const colorClass = pnlClass(totalPnl)
  return (
    <div
      className={`rounded-md px-2.5 py-1 text-xs font-mono bg-[var(--color-surface-alt)] border border-[var(--color-border)] ${colorClass}`}
    >
      {fmtSignedCcy(totalPnl)}
    </div>
  )
}

// ---------------------------------------------------------------------------
// PnlSlicedSection — main export
// ---------------------------------------------------------------------------
export function PnlSlicedSection() {
  const [filters, setFilters] = useState<PnlFilters>({
    market_id: '',
    strategy: '',
    sector: '',
  })

  const filterOptions = usePnlFilterOptions()
  const trades = usePnlTrades(filters)

  const colors = useCssVars([
    '--color-series-portfolio',
    '--color-series-grid',
    '--color-text-muted',
  ] as const)

  const portfolioColor = colors['--color-series-portfolio']
  const gridColor = colors['--color-series-grid']
  const textMuted = colors['--color-text-muted']

  // Build cumulative P&L series from sorted trades
  const { chartData, totalPnl } = useMemo(() => {
    const rows = trades.data ?? []
    if (rows.length === 0) return { chartData: [], totalPnl: 0 }

    const sorted = [...rows].sort((a, b) =>
      (a.date ?? '') < (b.date ?? '') ? -1 : 1
    )

    let cum = 0
    const points = sorted.map((r) => {
      const pnl = r.pnl ?? r.realized_pnl ?? 0
      cum += pnl
      return { date: r.date ?? '', cumPnl: cum }
    })

    return { chartData: points, totalPnl: cum }
  }, [trades.data])

  function handleFilterChange<K extends keyof PnlFilters>(key: K, value: string) {
    setFilters((prev) => ({ ...prev, [key]: value }))
  }

  const opts = filterOptions.data
  const isLoading = trades.isLoading
  const isEmpty = !isLoading && Array.isArray(trades.data) && trades.data.length === 0
  const hasData = !isLoading && !isEmpty && chartData.length > 0

  return (
    <div className="bg-[var(--color-surface)] border border-[var(--color-border)] rounded-xl p-5 dash-card">
      {/* Header row */}
      <div className="flex items-center justify-between mb-4 flex-wrap gap-2">
        <h3 className="text-[10px] uppercase tracking-wider text-[var(--color-text-muted)] font-medium">
          P&amp;L Breakdown
        </h3>
        {hasData && <CumulativePnlBadge totalPnl={totalPnl} />}
      </div>

      {/* Slicer dropdowns */}
      <div className="mb-4">
        <PnlSlicerRow
          filters={filters}
          markets={opts?.markets ?? []}
          strategies={opts?.strategies ?? []}
          sectors={opts?.sectors ?? []}
          onChange={handleFilterChange}
        />
      </div>

      {/* Chart / skeleton / empty state */}
      {isLoading ? (
        <Skeleton className="h-[220px]" />
      ) : isEmpty ? (
        <EmptyState message="No trades match the current filter" className="h-[220px] flex items-center justify-center" />
      ) : (
        <ChartGate className="h-[220px] md:h-[260px]">
          <ResponsiveContainer width="100%" height="100%" minWidth={0} minHeight={0}>
            <AreaChart data={chartData}>
              <defs>
                <linearGradient id="pnlSlicerGrad" x1="0" y1="0" x2="0" y2="1">
                  <stop offset="0%" stopColor={portfolioColor || '#22c55e'} stopOpacity={0.25} />
                  <stop offset="100%" stopColor={portfolioColor || '#22c55e'} stopOpacity={0} />
                </linearGradient>
              </defs>
              <CartesianGrid
                strokeDasharray="3 3"
                stroke={gridColor || 'var(--color-border)'}
                vertical={false}
              />
              <XAxis
                dataKey="date"
                tickFormatter={(v) => fmtDateShort(v as string)}
                axisLine={false}
                tickLine={false}
                interval="preserveStartEnd"
                minTickGap={40}
                tick={{ fontSize: 10, fill: textMuted || 'var(--color-text-muted)' }}
              />
              <YAxis
                tickFormatter={(v) => fmtSignedCcy(v as number)}
                axisLine={false}
                tickLine={false}
                tick={{ fontSize: 10, fill: textMuted || 'var(--color-text-muted)' }}
                width={80}
              />
              <ReferenceLine y={0} stroke={gridColor || 'var(--color-border)'} strokeDasharray="4 4" />
              <Tooltip
                cursor={{ stroke: 'var(--color-border)', strokeDasharray: '4 4' }}
                content={
                  <ChartTooltip
                    labelFormatter={(l) => fmtDateShort(l)}
                    formatter={(v) => fmtSignedCcy(v as number)}
                  />
                }
              />
              <Area
                dataKey="cumPnl"
                name="Cumulative P&L"
                stroke={portfolioColor || '#22c55e'}
                strokeWidth={2}
                fill="url(#pnlSlicerGrad)"
                connectNulls={true}
                isAnimationActive={true}
                animationDuration={800}
                animationEasing="ease-out"
              />
            </AreaChart>
          </ResponsiveContainer>
        </ChartGate>
      )}
    </div>
  )
}
