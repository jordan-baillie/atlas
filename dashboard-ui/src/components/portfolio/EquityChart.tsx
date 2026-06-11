import { useState, useEffect, useMemo } from 'react'
import { Chart } from '../shared/Chart'
import { Badge } from '../shared/Badge'
import { useEquityChartData } from '../../api/queries'
import { Skeleton } from '../layout/Skeleton'
import { fmtCcy, fmtDateShort, fmtSignedPct } from '../../lib/format'
import { useCssVars } from '../../hooks/useCssVar'
import { useReducedMotion } from '../../hooks/useReducedMotion'
import { gradientFill } from '../../lib/chart-defaults'
import { HudPanel } from '../ui/hud'
import type { ChartData, ChartOptions, Plugin } from 'chart.js'

// Soft neon glow under the portfolio line (canvas shadow, draw-time only).
const lineGlowPlugin: Plugin = {
  id: 'mcLineGlow',
  beforeDatasetDraw(chart, args) {
    if (chart.data.datasets[args.index]?.label !== 'Portfolio') return
    const ctx = chart.ctx
    ctx.save()
    ctx.shadowColor = String(chart.data.datasets[args.index].borderColor ?? '')
    ctx.shadowBlur = 8
  },
  afterDatasetDraw(chart, args) {
    if (chart.data.datasets[args.index]?.label !== 'Portfolio') return
    chart.ctx.restore()
  },
}

// Period selector options
const PERIODS = [
  { key: '1W', days: 7 },
  { key: '1M', days: 30 },
  { key: '3M', days: 90 },
  { key: 'ALL', days: Infinity },
] as const

type PeriodKey = (typeof PERIODS)[number]['key']

// ---------------------------------------------------------------------------
// EquityReturnBadge -- unchanged
// ---------------------------------------------------------------------------
interface EquityReturnBadgeProps {
  portfolioReturnPct: number
  alphaVsSpy: number
}

function EquityReturnBadge({ portfolioReturnPct, alphaVsSpy }: EquityReturnBadgeProps) {
  const variant = portfolioReturnPct >= 0 ? 'success' : 'danger'
  return (
    <Badge variant={variant} size="sm">
      {fmtSignedPct(portfolioReturnPct)}&nbsp;({fmtSignedPct(alphaVsSpy)} vs SPY)
    </Badge>
  )
}

// ---------------------------------------------------------------------------
// PeriodSelector -- unchanged
// ---------------------------------------------------------------------------
function PeriodSelector({ active, onChange }: { active: PeriodKey; onChange: (k: PeriodKey) => void }) {
  return (
    <div className="flex gap-1">
      {PERIODS.map(({ key }) => (
        <button
          key={key}
          onClick={() => onChange(key)}
          className={`px-2 py-0.5 rounded-full text-[10px] font-mono font-medium tracking-wide transition-colors border ${
            active === key
              ? 'bg-[color-mix(in_srgb,var(--accent-section,var(--color-accent))_15%,transparent)] text-[var(--accent-section,var(--color-accent))] border-[color-mix(in_srgb,var(--accent-section,var(--color-accent))_30%,transparent)]'
              : 'bg-transparent border-[var(--color-border)]/40 text-[var(--color-text-muted)] hover:text-[var(--color-text)]'
          }`}
        >
          {key}
        </button>
      ))}
    </div>
  )
}

// ---------------------------------------------------------------------------
// EquityChart -- Chart.js port
// ---------------------------------------------------------------------------
export function EquityChart() {
  const colors = useCssVars([
    '--color-series-portfolio',
    '--color-series-benchmark',
    '--color-border',
  ] as const)

  const portfolioColor = colors['--color-series-portfolio'] || '#22c55e'
  const benchmarkColor = colors['--color-series-benchmark'] || '#a1a1aa'
  const borderColor = colors['--color-border'] || '#2a2f37'

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

  const startEquity = useMemo(() => {
    const first = filteredData[0]
    return first?.portfolio ?? null
  }, [filteredData])

  const chartData = useMemo<ChartData<'line'>>(() => {
    return {
      labels: filteredData.map((d) => d.date),
      datasets: [
        {
          label: 'Portfolio',
          data: filteredData.map((d) => d.portfolio ?? null) as number[],
          borderColor: portfolioColor,
          borderWidth: 2,
          fill: true,
          backgroundColor: gradientFill(portfolioColor, 0.30) as unknown as string,
          pointRadius: 0,
          pointHoverRadius: 4,
          tension: 0.25,
          spanGaps: true,
        },
        {
          label: 'SPY',
          data: filteredData.map((d) => d.spy ?? null) as number[],
          borderColor: benchmarkColor,
          borderWidth: 1.5,
          borderDash: [4, 4],
          fill: false,
          pointRadius: 0,
          pointHoverRadius: 4,
          tension: 0.25,
          spanGaps: true,
        },
        // Reference line at start-equity drawn as a flat dataset
        ...(startEquity != null ? [{
          label: '_baseline',
          data: filteredData.map(() => startEquity) as number[],
          borderColor: borderColor,
          borderWidth: 1,
          borderDash: [2, 2],
          fill: false,
          pointRadius: 0,
          pointHoverRadius: 0,
          tension: 0,
          // Hide from tooltip + legend
        }] : []),
      ],
    }
  }, [filteredData, portfolioColor, benchmarkColor, borderColor, startEquity])

  const options = useMemo<ChartOptions<'line'>>(() => ({
    plugins: {
      legend: { display: false },
      tooltip: {
        filter: (item) => (item.dataset.label ?? '').charAt(0) !== '_',
        callbacks: {
          title: (items) => (items[0]?.label ? fmtDateShort(items[0].label) : ''),
          label: (ctx) => {
            const name = ctx.dataset.label ?? ''
            const v = typeof ctx.parsed.y === 'number' ? ctx.parsed.y : 0
            return `${name}: ${fmtCcy(v)}`
          },
        },
      },
    },
    scales: {
      x: {
        ticks: {
          color: 'var(--color-text-muted)',
          font: { size: isMobile ? 9 : 10 },
          maxRotation: 0,
          autoSkipPadding: 24,
          callback(value) {
            const label = this.getLabelForValue(Number(value))
            return fmtDateShort(label as string)
          },
        },
      },
      y: {
        ticks: {
          color: 'var(--color-text-muted)',
          font: { size: isMobile ? 9 : 10 },
          callback(v) {
            return '$' + Math.round(Number(v)).toLocaleString('en-US')
          },
        },
      },
    },
    elements: {
      point: { radius: 0, hoverRadius: 4 },
    },
    animation: { duration: 600, easing: 'easeOutQuart' },
  }), [isMobile])

  const reduced = useReducedMotion()

  if (!query.data) return <Skeleton className="h-96" />

  return (
    <HudPanel
      title="Equity Curve"
      brackets
      right={
        <div className="flex items-center gap-3">
          <PeriodSelector active={period} onChange={setPeriod} />
          <EquityReturnBadge
            portfolioReturnPct={query.data.portfolioReturnPct}
            alphaVsSpy={query.data.alphaVsSpy}
          />
        </div>
      }
    >
      <Chart
        kind="line"
        drawIn
        data={chartData as ChartData<'line' | 'bar' | 'doughnut'>}
        options={options as ChartOptions<'line' | 'bar' | 'doughnut'>}
        plugins={reduced ? undefined : [lineGlowPlugin]}
        height={isMobile ? 280 : 360}
      />
    </HudPanel>
  )
}
