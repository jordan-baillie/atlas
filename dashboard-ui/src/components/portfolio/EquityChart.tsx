import { useState, useEffect, useMemo } from 'react'
import { ComposedChart, Area, Line, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer } from 'recharts'
import { useEquityChartData } from '../../api/queries'
import { Skeleton } from '../layout/Skeleton'
import { ChartTooltip } from '../shared/ChartTooltip'
import { fmtCcy, fmtDateShort, fmtSignedPct } from '../../lib/format'
import { useCssVars } from '../../hooks/useCssVar'

// Period selector options
const PERIODS = [
  { key: '1W', days: 7 },
  { key: '1M', days: 30 },
  { key: '3M', days: 90 },
  { key: 'ALL', days: Infinity },
] as const

type PeriodKey = (typeof PERIODS)[number]['key']

// ---------------------------------------------------------------------------
// EquityReturnBadge — module-scoped (rerender-no-inline-components rule)
// ---------------------------------------------------------------------------
interface EquityReturnBadgeProps {
  portfolioReturnPct: number
  alphaVsSpy: number
}

function EquityReturnBadge({ portfolioReturnPct, alphaVsSpy }: EquityReturnBadgeProps) {
  const positive = portfolioReturnPct >= 0
  const colorClass = positive ? 'text-[var(--color-green)]' : 'text-[var(--color-red)]'
  return (
    <div className={`rounded-md px-2.5 py-1 text-xs font-mono bg-[var(--color-surface-alt)] border border-[var(--color-border)] ${colorClass}`}>
      {fmtSignedPct(portfolioReturnPct)} ({fmtSignedPct(alphaVsSpy)} vs SPY)
    </div>
  )
}

// ---------------------------------------------------------------------------
// PeriodSelector — pill buttons for time range
// ---------------------------------------------------------------------------
function PeriodSelector({ active, onChange }: { active: PeriodKey; onChange: (k: PeriodKey) => void }) {
  return (
    <div className="flex gap-1">
      {PERIODS.map(({ key }) => (
        <button
          key={key}
          onClick={() => onChange(key)}
          className={`px-2.5 py-1 rounded-full text-[10px] font-mono font-medium tracking-wide transition-colors ${
            active === key
              ? 'bg-[var(--color-accent)] text-white'
              : 'bg-[var(--color-surface-alt)] text-[var(--color-text-muted)] hover:text-[var(--color-text)]'
          }`}
        >
          {key}
        </button>
      ))}
    </div>
  )
}

// ---------------------------------------------------------------------------
// EquityChart
// ---------------------------------------------------------------------------
export function EquityChart() {
  const colors = useCssVars([
    '--color-series-portfolio',
    '--color-series-benchmark',
    '--color-series-grid',
    '--color-text-muted',
  ] as const)
  const portfolioColor = colors['--color-series-portfolio']
  const benchmarkColor = colors['--color-series-benchmark']
  const gridColor = colors['--color-series-grid']
  const textMuted = colors['--color-text-muted']

  const [isMobile, setIsMobile] = useState(false)
  useEffect(() => {
    const check = () => setIsMobile(window.innerWidth < 768)
    check()
    window.addEventListener('resize', check, { passive: true })
    return () => window.removeEventListener('resize', check)
  }, [])

  const [period, setPeriod] = useState<PeriodKey>('ALL')

  const query = useEquityChartData()

  const filteredData = useMemo(() => {
    if (!query.data?.chartData) return []
    const all = query.data.chartData
    const p = PERIODS.find((pp) => pp.key === period)
    if (!p || p.days === Infinity) return all
    return all.slice(-p.days)
  }, [query.data?.chartData, period])

  if (!query.data) return <Skeleton className="h-96" />

  const tooltipFormatter = (value: number, name: string) => {
    if (name === 'Portfolio' || name === 'SPY') return fmtCcy(value)
    return value.toLocaleString()
  }

  return (
    <div className="bg-[var(--color-surface)] border border-[var(--color-border)] rounded-xl p-5 dash-card">
      <div className="flex items-center justify-between mb-4 flex-wrap gap-2">
        <div className="flex items-center gap-3">
          <h3 className="text-[10px] uppercase tracking-wider text-[var(--color-text-muted)] font-medium">Equity Curve</h3>
          <PeriodSelector active={period} onChange={setPeriod} />
        </div>
        <EquityReturnBadge
          portfolioReturnPct={query.data.portfolioReturnPct}
          alphaVsSpy={query.data.alphaVsSpy}
        />
      </div>
      <div className="h-[220px] md:h-[260px] lg:h-[300px]">
        <ResponsiveContainer width="100%" height="100%" minWidth={0} minHeight={0}>
          <ComposedChart data={filteredData}>
            <defs>
              <linearGradient id="portfolioGrad" x1="0" y1="0" x2="0" y2="1">
                <stop offset="0%" stopColor={portfolioColor || '#22c55e'} stopOpacity={0.25} />
                <stop offset="100%" stopColor={portfolioColor || '#22c55e'} stopOpacity={0} />
              </linearGradient>
            </defs>
            <CartesianGrid strokeDasharray="3 3" stroke={gridColor || 'var(--color-border)'} vertical={false} />
            <XAxis
              dataKey="date"
              tickFormatter={(v) => fmtDateShort(v as string)}
              axisLine={false}
              tickLine={false}
              interval="preserveStartEnd"
              minTickGap={40}
              tick={{ fontSize: isMobile ? 9 : 10, fill: textMuted || 'var(--color-text-muted)' }}
            />
            <YAxis
              domain={[
                (dataMin: number) => Math.floor(dataMin * 0.99),
                (dataMax: number) => Math.ceil(dataMax * 1.01),
              ]}
              tickFormatter={(v) => '$' + Math.round(v as number).toLocaleString('en-US')}
              axisLine={false}
              tickLine={false}
              tick={{ fontSize: isMobile ? 9 : 10, fill: textMuted || 'var(--color-text-muted)' }}
              width={70}
              allowDataOverflow={false}
            />
            <Tooltip
              cursor={{ stroke: 'var(--color-border)', strokeDasharray: '4 4' }}
              content={
                <ChartTooltip
                  labelFormatter={(l) => fmtDateShort(l)}
                  formatter={tooltipFormatter}
                />
              }
            />
            <Area
              dataKey="portfolio"
              name="Portfolio"
              stroke={portfolioColor || '#22c55e'}
              strokeWidth={2}
              fill="url(#portfolioGrad)"
              baseValue="dataMin"
              connectNulls={true}
              isAnimationActive={true}
              animationDuration={1200}
              animationEasing="ease-out"
            />
            <Line
              dataKey="spy"
              name="SPY"
              stroke={benchmarkColor || '#a1a1aa'}
              strokeWidth={1.5}
              strokeDasharray="4 4"
              dot={false}
              connectNulls={true}
              isAnimationActive={true}
              animationDuration={1200}
              animationEasing="ease-out"
            />
          </ComposedChart>
        </ResponsiveContainer>
      </div>
    </div>
  )
}
