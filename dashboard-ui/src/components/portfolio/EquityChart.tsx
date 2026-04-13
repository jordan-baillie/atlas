import { useState, useEffect } from 'react'
import { ComposedChart, Area, Line, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer } from 'recharts'
import { useEquityChartData } from '../../api/queries'
import { Skeleton } from '../layout/Skeleton'
import { fmtCcy, fmtDateShort, fmtSignedPct } from '../../lib/format'
import { useCssVars } from '../../hooks/useCssVar'

// Rules applied in this file:
//   rerender-no-inline-components  — CustomTooltip + EquityReturnBadge hoisted to module scope
//   rerender-derived-state-no-effect — no props; data derived inside hook, not via effect
//   async-parallel — pre-computed EquityChartData from useEquityChartData hook
//   rendering-conditional-render   — explicit ternaries, no && short-circuit for JSX

// ---------------------------------------------------------------------------
// CustomTooltip — module-scoped (rerender-no-inline-components rule)
// ---------------------------------------------------------------------------
interface TooltipPayloadItem {
  dataKey?: string | number
  value?: number
}

interface CustomTooltipProps {
  active?: boolean
  payload?: TooltipPayloadItem[]
  label?: string
  portfolioColor: string
  benchmarkColor: string
}

function CustomTooltip({ active, payload, label, portfolioColor, benchmarkColor }: CustomTooltipProps) {
  if (!active || !payload || payload.length === 0) return null
  const portfolio = payload.find((p) => p.dataKey === 'portfolio')?.value
  const spy = payload.find((p) => p.dataKey === 'spy')?.value
  // Rule: rendering-conditional-render — ternary instead of && to avoid rendering "0"
  return (
    <div className="bg-[var(--color-surface)] border border-[var(--color-border)] rounded-lg px-3 py-2 text-xs font-mono shadow-lg">
      <div className="text-[var(--color-text-muted)] mb-1">{fmtDateShort(label)}</div>
      {portfolio != null ? <div style={{ color: portfolioColor }}>Portfolio {fmtCcy(portfolio)}</div> : null}
      {spy != null ? <div style={{ color: benchmarkColor }}>SPY {fmtCcy(spy)}</div> : null}
    </div>
  )
}

// ---------------------------------------------------------------------------
// EquityReturnBadge — module-scoped (rerender-no-inline-components rule)
// Replaces <ReturnBadge data={data} /> — receives pre-computed scalars so the
// badge never needs to access DashboardData or call any hook.
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
// EquityChart — no props; fetches and derives all state via hooks
// Rule: rerender-derived-state-no-effect — no prop drilling of DashboardData
// Rule: async-parallel — useEquityChartData does merge + stat computation once
// ---------------------------------------------------------------------------
export function EquityChart() {
  // Rule: js-batch-dom-css — single getComputedStyle call for all 4 color vars
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
    window.addEventListener('resize', check)
    return () => window.removeEventListener('resize', check)
  }, [])

  const query = useEquityChartData()
  // Rule: rendering-conditional-render — show Skeleton while data is absent
  if (!query.data) return <Skeleton className="h-96" />

  return (
    <div className="bg-[var(--color-surface)] border border-[var(--color-border)] rounded-xl p-5 dash-card">
      <div className="flex items-center justify-between mb-4">
        <h3 className="text-[10px] uppercase tracking-wider text-[var(--color-text-muted)] font-medium">Equity Curve</h3>
        <EquityReturnBadge
          portfolioReturnPct={query.data.portfolioReturnPct}
          alphaVsSpy={query.data.alphaVsSpy}
        />
      </div>
      <div className="h-[220px] md:h-[260px] lg:h-[300px]">
        <ResponsiveContainer width="100%" height="100%">
          <ComposedChart data={query.data.chartData}>
            <defs>
              <linearGradient id="portfolioGrad" x1="0" y1="0" x2="0" y2="1">
                <stop offset="0%" stopColor={portfolioColor || '#22c55e'} stopOpacity={0.3} />
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
              tick={{ fontSize: isMobile ? 10 : 11, fill: textMuted || 'var(--color-text-muted)' }}
            />
            <YAxis
              domain={[
                (dataMin: number) => Math.floor(dataMin * 0.99),
                (dataMax: number) => Math.ceil(dataMax * 1.01),
              ]}
              tickFormatter={(v) => '$' + Math.round(v as number).toLocaleString('en-US')}
              axisLine={false}
              tickLine={false}
              tick={{ fontSize: isMobile ? 10 : 11, fill: textMuted || 'var(--color-text-muted)' }}
              width={70}
              allowDataOverflow={false}
            />
            <Tooltip
              content={
                <CustomTooltip
                  portfolioColor={portfolioColor || '#22c55e'}
                  benchmarkColor={benchmarkColor || '#a1a1aa'}
                />
              }
            />
            <Area
              dataKey="portfolio"
              stroke={portfolioColor || '#22c55e'}
              strokeWidth={2}
              fill="url(#portfolioGrad)"
              baseValue="dataMin"
              connectNulls={true}
              isAnimationActive={false}
            />
            <Line
              dataKey="spy"
              stroke={benchmarkColor || '#a1a1aa'}
              strokeWidth={1.5}
              strokeDasharray="4 4"
              dot={false}
              connectNulls={true}
              isAnimationActive={false}
            />
          </ComposedChart>
        </ResponsiveContainer>
      </div>
    </div>
  )
}
