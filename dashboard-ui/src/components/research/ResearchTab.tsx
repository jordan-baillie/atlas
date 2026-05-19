import { useState, useMemo } from 'react'
import { AreaChart, Area, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer } from 'recharts'
import { ChartGate } from '../shared/ChartGate'
import { useResearchSummary, useResearchStrategies, useResearchTimeline, useResearchExperiments, useResearchBrain, useResearchDiscoveries, useResearchOverview, useResearchLeaderboard } from '../../api/research-queries'
import { Skeleton } from '../layout/Skeleton'
import { SectionBoundary } from '../layout/SectionBoundary'
import { ChartTooltip } from '../shared/ChartTooltip'
import { Badge } from '../shared/Badge'
import type { BadgeVariant } from '../shared/Badge'
import { StatusDot } from '../shared/StatusDot'
import { CHART_GRID, CHART_TICK, CHART_ANIM, CHART_CURSOR } from '../../lib/chart-palette'
import { fmtNum, fmtPct, fmtDateShort, fmtRelativeTime } from '../../lib/format'
import type { StrategyDetail, Experiment, BrainParam, Discovery, UniverseInfo, LeaderboardEntry, DailyCount } from '../../api/research-types'
import { CoverageMatrix } from './CoverageMatrix'
import { PaperProgressPanel } from './PaperProgressPanel'
import { PendingPromotionsWidget } from './PendingPromotionsWidget'

// ── Keep Rate Ring SVG ──────────────────────────────────────────
function KeepRateRing({ rate, size = 40 }: { rate: number; size?: number }) {
  const r = (size - 4) / 2
  const circ = 2 * Math.PI * r
  const offset = circ * (1 - Math.min(rate, 100) / 100)
  const color = rate > 15 ? 'var(--color-green)' : rate > 10 ? 'var(--color-amber, #f59e0b)' : 'var(--color-red)'
  return (
    <svg width={size} height={size} className="transform -rotate-90">
      <circle cx={size/2} cy={size/2} r={r} fill="none" stroke="var(--color-border)" strokeWidth={3} />
      <circle cx={size/2} cy={size/2} r={r} fill="none" stroke={color} strokeWidth={3}
        strokeDasharray={circ} strokeDashoffset={offset} strokeLinecap="round"
        className="transition-all duration-700" />
    </svg>
  )
}

// ── Summary Cards ───────────────────────────────────────────────
function SummaryCards({ data }: { data: { total_experiments?: number; keep_rate?: number; strategies_count?: number; experiments_7d?: number; last_research_ts?: string } }) {
  const cards = [
    { label: 'Total Experiments', value: (data.total_experiments ?? 0).toLocaleString() },
    { label: 'Keep Rate', value: fmtPct(data.keep_rate), color: (data.keep_rate ?? 0) > 15 ? 'var(--color-green)' : (data.keep_rate ?? 0) > 10 ? 'var(--color-amber, #f59e0b)' : 'var(--color-red)' },
    { label: 'Strategies', value: String(data.strategies_count ?? 0) },
    { label: 'Last 7 Days', value: String(data.experiments_7d ?? 0) },
    { label: 'Last Run', value: fmtRelativeTime(data.last_research_ts) },
  ]
  return (
    <div className="grid grid-cols-2 md:grid-cols-5 gap-3">
      {cards.map((c) => (
        <div key={c.label} className="bg-[var(--color-surface)] border border-[var(--color-border)] rounded-xl p-4 dash-card">
          <div className="text-[10px] uppercase tracking-wider text-[var(--color-text-muted)] font-medium mb-1">{c.label}</div>
          <div className="text-2xl font-mono font-semibold" style={c.color ? { color: c.color } : undefined}>{c.value}</div>
        </div>
      ))}
    </div>
  )
}

// ── Sharpe Timeline Chart ───────────────────────────────────────
const PERIODS = [
  { key: '7d', days: 7 },
  { key: '30d', days: 30 },
  { key: '90d', days: 90 },
  { key: 'ALL', days: 365 },
] as const

function SharpeChart() {
  const [days, setDays] = useState(30)
  const timeline = useResearchTimeline(days, true)

  const chartData = useMemo(() => {
    if (!timeline.data?.dates || !timeline.data?.series) return []
    const series = timeline.data.series
    return timeline.data.dates.map((date) => {
      let totalSharpe = 0, count = 0, totalExperiments = 0
      for (const pts of Object.values(series)) {
        const pt = (pts as Array<{ date?: string; best_sharpe?: number; experiments?: number }>).find((p) => p.date === date)
        if (pt) {
          if (pt.best_sharpe != null) { totalSharpe += pt.best_sharpe; count++ }
          totalExperiments += pt.experiments ?? 0
        }
      }
      return { date, avgSharpe: count > 0 ? +(totalSharpe / count).toFixed(4) : 0, experiments: totalExperiments }
    })
  }, [timeline.data])

  if (!timeline.data) return <Skeleton className="h-64" />

  return (
    <div className="bg-[var(--color-surface)] border border-[var(--color-border)] rounded-xl p-5 dash-card">
      <div className="flex items-center justify-between mb-4 flex-wrap gap-2">
        <h3 className="text-[10px] uppercase tracking-wider text-[var(--color-text-muted)] font-medium">Sharpe Trajectory</h3>
        <div className="flex gap-1">
          {PERIODS.map(({ key, days: d }) => (
            <button key={key} onClick={() => setDays(d)}
              className={`px-2.5 py-1 rounded-full text-[10px] font-mono font-medium tracking-wide transition-colors ${
                days === d ? 'bg-[var(--color-accent)] text-white' : 'bg-[var(--color-surface-alt)] text-[var(--color-text-muted)] hover:text-[var(--color-text)]'
              }`}>{key}</button>
          ))}
        </div>
      </div>
      <ChartGate className="h-[200px] md:h-[260px]">
        <ResponsiveContainer width="100%" height="100%" minWidth={0} minHeight={0}>
          <AreaChart data={chartData}>
            <defs>
              <linearGradient id="sharpeGrad" x1="0" y1="0" x2="0" y2="1">
                <stop offset="0%" stopColor="var(--color-accent, #6366f1)" stopOpacity={0.3} />
                <stop offset="100%" stopColor="var(--color-accent, #6366f1)" stopOpacity={0} />
              </linearGradient>
            </defs>
            <CartesianGrid {...CHART_GRID} />
            <XAxis dataKey="date" tickFormatter={(v) => fmtDateShort(v as string)} axisLine={false} tickLine={false}
              tick={CHART_TICK} minTickGap={40} />
            <YAxis yAxisId="left" axisLine={false} tickLine={false} tick={CHART_TICK} width={50}
              tickFormatter={(v) => (v as number).toFixed(2)} />
            <YAxis yAxisId="right" orientation="right" axisLine={false} tickLine={false} tick={CHART_TICK} width={40} />
            <Tooltip cursor={CHART_CURSOR} content={<ChartTooltip labelFormatter={(l) => fmtDateShort(l)} formatter={(v, n) => n === 'Experiments' ? String(v) : (v as number).toFixed(4)} />} />
            <Area yAxisId="left" dataKey="avgSharpe" name="Avg Sharpe" stroke="var(--color-accent, #6366f1)" strokeWidth={2} fill="url(#sharpeGrad)" dot={{ r: 3, fill: 'var(--color-accent, #6366f1)' }} {...CHART_ANIM} />
            <Area yAxisId="right" dataKey="experiments" name="Experiments" stroke="none" fill="var(--color-text-muted)" fillOpacity={0.1} {...CHART_ANIM} />
          </AreaChart>
        </ResponsiveContainer>
      </ChartGate>
    </div>
  )
}

// ── Strategy Grid ───────────────────────────────────────────────
function StrategyCard({ s }: { s: StrategyDetail }) {
  const keepRate = (s.total_experiments ?? 0) > 0 ? ((s.kept_count ?? 0) / (s.total_experiments ?? 1)) * 100 : 0
  return (
    <div className="bg-[var(--color-surface)] border border-[var(--color-border)] rounded-xl p-4 dash-card">
      <div className="flex items-start justify-between gap-3">
        <div className="flex-1 min-w-0">
          <div className="flex items-center flex-wrap gap-1">
            <span className="font-medium text-sm truncate">{(s.strategy ?? '').replace(/_/g, ' ')}</span>
            {s.is_solo === false && (
              <span className="text-xs px-2 py-0.5 rounded bg-yellow-700/40 text-yellow-200" title={s.contamination_note || ''}>
                🟡 PORTFOLIO-CONTAMINATED
              </span>
            )}
            {s.is_solo === true && (
              <span className="text-xs px-2 py-0.5 rounded bg-emerald-800/40 text-emerald-200">
                🟢 SOLO
              </span>
            )}
          </div>
          <div className="text-[10px] text-[var(--color-text-muted)] mt-0.5">
            {s.total_experiments ?? 0} experiments · {s.kept_count ?? 0} kept
          </div>
        </div>
        <div className="relative flex-shrink-0">
          <KeepRateRing rate={keepRate} size={44} />
          <span className="absolute inset-0 flex items-center justify-center text-[9px] font-mono font-bold">{Math.round(keepRate)}%</span>
        </div>
      </div>
      <div className="grid grid-cols-2 gap-2 mt-3">
        <div>
          <div className="text-[9px] uppercase text-[var(--color-text-muted)]">Best Sharpe</div>
          <div className="font-mono text-sm font-semibold">{fmtNum(s.best_sharpe, 4)}</div>
        </div>
        <div>
          <div className="text-[9px] uppercase text-[var(--color-text-muted)]">Best CAGR</div>
          <div className="font-mono text-sm font-semibold">{fmtPct(s.best_cagr, 1)}</div>
        </div>
      </div>
      {s.best_params && Object.keys(s.best_params).length > 0 && (
        <div className="flex flex-wrap gap-1 mt-2">
          {Object.entries(s.best_params).slice(0, 4).map(([k, v]) => (
            <span key={k} className="text-[9px] font-mono px-1.5 py-0.5 rounded bg-[var(--color-surface-alt)] text-[var(--color-text-muted)]">
              {k}={String(v)}
            </span>
          ))}
        </div>
      )}
    </div>
  )
}

function StrategyGrid({ strategies }: { strategies: StrategyDetail[] }) {
  const sorted = useMemo(() => [...strategies].sort((a, b) => (b.best_sharpe ?? 0) - (a.best_sharpe ?? 0)), [strategies])
  return (
    <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-3">
      {sorted.map((s) => <StrategyCard key={s.strategy} s={s} />)}
    </div>
  )
}

// ── Status Badge ────────────────────────────────────────────────
function StatusBadge({ status }: { status?: string }) {
  const s = status ?? 'unknown'
  const variant: BadgeVariant =
    s === 'kept' ? 'success'
    : s === 'discarded' ? 'danger'
    : s === 'discard_solo' ? 'warning'
    : s === 'running' ? 'info'
    : 'neutral'
  return <Badge variant={variant} size="xs">{s}</Badge>
}

// ── Experiments Table ───────────────────────────────────────────
function ExperimentsTable() {
  const [limit, setLimit] = useState(30)
  const [filterStrategy, setFilterStrategy] = useState('')
  const [filterStatus, setFilterStatus] = useState('')
  const summary = useResearchSummary(true)
  const experiments = useResearchExperiments(
    { limit, strategy: filterStrategy || undefined, status: filterStatus || undefined }, true
  )

  const strategyOptions = useMemo(() => {
    const items = summary.data?.by_strategy ?? []
    return items.map((s: { strategy?: string }) => s.strategy ?? '').filter(Boolean).sort()
  }, [summary.data])

  if (!experiments.data) return <Skeleton className="h-48" />

  const rows = experiments.data.experiments ?? []

  return (
    <div className="bg-[var(--color-surface)] border border-[var(--color-border)] rounded-xl dash-card overflow-hidden">
      <div className="p-4 flex flex-wrap gap-2 border-b border-[var(--color-border)]">
        <select value={filterStrategy} onChange={(e) => setFilterStrategy(e.target.value)}
          className="text-xs bg-[var(--color-surface-alt)] border border-[var(--color-border)] rounded-lg px-3 py-1.5 text-[var(--color-text)]">
          <option value="">All Strategies</option>
          {strategyOptions.map((s: string) => <option key={s} value={s}>{s.replace(/_/g, ' ')}</option>)}
        </select>
        <select value={filterStatus} onChange={(e) => setFilterStatus(e.target.value)}
          className="text-xs bg-[var(--color-surface-alt)] border border-[var(--color-border)] rounded-lg px-3 py-1.5 text-[var(--color-text)]">
          <option value="">All Statuses</option>
          <option value="kept">Kept</option>
          <option value="discarded">Discarded</option>
          <option value="discard_solo">Discard Solo</option>
        </select>
      </div>
      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b border-[var(--color-border)]">
              <th className="text-left text-[10px] uppercase tracking-wider text-[var(--color-text-muted)] font-medium px-4 py-2">Strategy</th>
              <th className="text-left text-[10px] uppercase tracking-wider text-[var(--color-text-muted)] font-medium px-4 py-2 hidden md:table-cell">Description</th>
              <th className="text-right text-[10px] uppercase tracking-wider text-[var(--color-text-muted)] font-medium px-4 py-2">Sharpe</th>
              <th className="text-right text-[10px] uppercase tracking-wider text-[var(--color-text-muted)] font-medium px-4 py-2 hidden md:table-cell">Trades</th>
              <th className="text-right text-[10px] uppercase tracking-wider text-[var(--color-text-muted)] font-medium px-4 py-2 hidden lg:table-cell">Max DD</th>
              <th className="text-right text-[10px] uppercase tracking-wider text-[var(--color-text-muted)] font-medium px-4 py-2 hidden lg:table-cell">PF</th>
              <th className="text-right text-[10px] uppercase tracking-wider text-[var(--color-text-muted)] font-medium px-4 py-2 hidden md:table-cell">CAGR</th>
              <th className="text-center text-[10px] uppercase tracking-wider text-[var(--color-text-muted)] font-medium px-4 py-2">Status</th>
              <th className="text-right text-[10px] uppercase tracking-wider text-[var(--color-text-muted)] font-medium px-4 py-2">Date</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((e: Experiment) => (
              <tr key={e.id} className="border-b border-[var(--color-border)] hover:bg-[var(--color-surface-alt)] transition-colors">
                <td className="px-4 py-2 font-medium text-xs">{(e.strategy ?? '').replace(/_/g, ' ')}</td>
                <td className="px-4 py-2 text-xs text-[var(--color-text-muted)] truncate max-w-[200px] hidden md:table-cell">{e.description ?? ''}</td>
                <td className="px-4 py-2 text-right font-mono text-xs">{fmtNum(e.sharpe, 4)}</td>
                <td className="px-4 py-2 text-right font-mono text-xs hidden md:table-cell">{e.trades ?? '—'}</td>
                <td className="px-4 py-2 text-right font-mono text-xs hidden lg:table-cell">{fmtPct(e.max_dd_pct, 1)}</td>
                <td className="px-4 py-2 text-right font-mono text-xs hidden lg:table-cell">{fmtNum(e.profit_factor, 2)}</td>
                <td className="px-4 py-2 text-right font-mono text-xs hidden md:table-cell">{fmtPct(e.cagr_pct, 1)}</td>
                <td className="px-4 py-2 text-center"><StatusBadge status={e.status} /></td>
                <td className="px-4 py-2 text-right text-xs text-[var(--color-text-muted)]">{fmtDateShort(e.created_at)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {rows.length >= limit && (
        <div className="p-3 text-center border-t border-[var(--color-border)]">
          <button onClick={() => setLimit((l) => l + 50)}
            className="text-xs px-4 py-1.5 bg-[var(--color-surface-alt)] hover:bg-[var(--color-border)] rounded-lg transition-colors">
            Show More
          </button>
        </div>
      )}
    </div>
  )
}

// ── Brain Knowledge ─────────────────────────────────────────────
function BrainTable({ params }: { params: BrainParam[] }) {
  const sorted = useMemo(() => [...params].sort((a, b) => (b.tests ?? 0) - (a.tests ?? 0)), [params])
  return (
    <div className="bg-[var(--color-surface)] border border-[var(--color-border)] rounded-xl dash-card overflow-hidden">
      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b border-[var(--color-border)]">
              <th className="text-left text-[10px] uppercase tracking-wider text-[var(--color-text-muted)] font-medium px-4 py-2">Parameter</th>
              <th className="text-right text-[10px] uppercase tracking-wider text-[var(--color-text-muted)] font-medium px-4 py-2">Tests</th>
              <th className="text-right text-[10px] uppercase tracking-wider text-[var(--color-text-muted)] font-medium px-4 py-2">Strategies</th>
              <th className="text-right text-[10px] uppercase tracking-wider text-[var(--color-text-muted)] font-medium px-4 py-2">Improved</th>
              <th className="text-right text-[10px] uppercase tracking-wider text-[var(--color-text-muted)] font-medium px-4 py-2">Avg Δ Sharpe</th>
            </tr>
          </thead>
          <tbody>
            {sorted.map((p: BrainParam) => (
              <tr key={p.param_name} className="border-b border-[var(--color-border)]">
                <td className="px-4 py-2 font-mono text-xs">{p.param_name}</td>
                <td className="px-4 py-2 text-right font-mono text-xs">{p.tests ?? 0}</td>
                <td className="px-4 py-2 text-right font-mono text-xs">{p.strategies_tested ?? 0}</td>
                <td className="px-4 py-2 text-right font-mono text-xs">{p.improved ?? 0}</td>
                <td className={`px-4 py-2 text-right font-mono text-xs ${(p.avg_sharpe_delta ?? 0) > 0 ? 'text-[var(--color-green)]' : 'text-[var(--color-red)]'}`}>
                  {(p.avg_sharpe_delta ?? 0) > 0 ? '+' : ''}{fmtNum(p.avg_sharpe_delta, 4)}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}

// ── Discovery Cards ─────────────────────────────────────────────
function DiscoveryCards({ discoveries }: { discoveries: Discovery[] }) {
  if (!discoveries.length) return <div className="text-sm text-[var(--color-text-muted)]">No discoveries yet</div>
  return (
    <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
      {discoveries.map((d: Discovery) => (
        <div key={d.id} className="bg-[var(--color-surface)] border border-[var(--color-border)] rounded-xl p-4 dash-card">
          <div className="flex justify-between items-start mb-2">
            <div className="text-sm font-medium">{d.run_date}</div>
            <StatusBadge status={d.status} />
          </div>
          <div className="grid grid-cols-4 gap-2 text-center mb-3">
            {[
              { label: 'Found', value: d.papers_found },
              { label: 'Filtered', value: d.papers_filtered },
              { label: 'Specs', value: d.specs_extracted },
              { label: 'Strategies', value: d.strategies_generated },
            ].map((item) => (
              <div key={item.label}>
                <div className="text-lg font-mono font-semibold">{item.value ?? 0}</div>
                <div className="text-[9px] uppercase text-[var(--color-text-muted)]">{item.label}</div>
              </div>
            ))}
          </div>
          {d.paper_titles && d.paper_titles.length > 0 && (
            <div className="text-[10px] text-[var(--color-text-muted)]">
              {d.paper_titles.slice(0, 3).map((t: string, i: number) => <div key={i} className="truncate">📄 {t}</div>)}
            </div>
          )}
        </div>
      ))}
    </div>
  )
}

// ── Engine Status Hero ──────────────────────────────────────────
function EngineHero({ engine }: { engine: { status?: string; total_experiments_all_time?: number; experiments_today?: number; kept_all_time?: number; daily_counts?: DailyCount[] } }) {
  const status = engine.status ?? 'unknown'
  const isRunning = status === 'running'
  return (
    <div className="bg-[var(--color-surface)] border border-[var(--color-border)] rounded-xl p-5 dash-card">
      <div className="flex flex-col md:flex-row md:items-center md:justify-between gap-4">
        <div>
          <div className="flex items-center gap-2 mb-2">
            <StatusDot status={isRunning ? 'green' : 'gray'} size="md" pulse={isRunning} />
            <span className="font-medium text-sm">
              Research Engine — {isRunning ? 'Running' : 'Idle'}
            </span>
            {isRunning && <Badge variant="success" size="xs">live</Badge>}
          </div>
          <div className="flex items-center gap-4 text-xs text-[var(--color-text-muted)]">
            <span><span className="font-mono font-semibold text-[var(--color-text)] text-base">{(engine.total_experiments_all_time ?? 0).toLocaleString()}</span> experiments all-time</span>
            <span className="text-[var(--color-border)]">·</span>
            <span><span className="font-mono font-semibold text-[var(--color-text)] text-base">{engine.experiments_today ?? 0}</span> today</span>
            <span className="text-[var(--color-border)]">·</span>
            <span><span className="font-mono font-semibold text-[var(--color-green)] text-base">{(engine.kept_all_time ?? 0).toLocaleString()}</span> kept</span>
          </div>
        </div>
        {engine.daily_counts && engine.daily_counts.length > 0 && (
          <DailySparkline data={engine.daily_counts} />
        )}
      </div>
    </div>
  )
}

function DailySparkline({ data }: { data: DailyCount[] }) {
  const recent = data.slice(-14)
  const maxCount = Math.max(...recent.map(d => d.count ?? 0), 1)
  return (
    <div className="flex flex-col items-end gap-1">
      <div className="flex items-end gap-[2px] h-10">
        {recent.map((d, i) => {
          const h = Math.max(((d.count ?? 0) / maxCount) * 36, 2)
          const keptH = (d.count ?? 0) > 0 && (d.kept ?? 0) > 0 ? ((d.kept ?? 0) / (d.count ?? 1)) * h : 0
          return (
            <div key={i} className="relative" style={{ width: 8, height: 36 }}>
              <div className="absolute bottom-0 w-full rounded-sm bg-[var(--color-text-muted)]/20" style={{ height: h }} />
              {keptH > 0 && <div className="absolute bottom-0 w-full rounded-sm bg-[var(--color-green)]/70" style={{ height: keptH }} />}
            </div>
          )
        })}
      </div>
      <span className="text-[9px] text-[var(--color-text-muted)]">14-day activity</span>
    </div>
  )
}

// ── Universe Cards ──────────────────────────────────────────────
function sharpeGaugeColor(s: number | null | undefined): string {
  if (s == null || s < 0) return '#ef4444'
  if (s < 0.3) return '#eab308'
  if (s < 0.6) return '#22c55e'
  return '#3b82f6'
}

function UniverseCard({ u }: { u: UniverseInfo }) {
  const title = (u.id ?? '').replace(/_/g, ' ').replace(/\b\w/g, c => c.toUpperCase())
  const isLive = u.mode === 'live'
  const priKey = u.priority ?? 'low'
  const priStars = priKey === 'high' ? '★★★' : priKey === 'medium' ? '★★' : '★'
  const sharpe = u.best_sharpe ?? 0
  const gaugeW = Math.min(Math.max(sharpe / 1.0 * 100, 0), 100)
  const gaugeColor = sharpeGaugeColor(u.best_sharpe)
  const keepRate = u.keep_rate ?? 0
  const stratKeys = u.strategies ? Object.keys(u.strategies) : []

  return (
    <div className="bg-[var(--color-surface)] border border-[var(--color-border)] rounded-xl p-4 dash-card">
      <div className="flex items-start justify-between gap-2 mb-3">
        <span className="font-medium text-sm truncate">{title}</span>
        <div className="flex gap-1.5 flex-shrink-0">
          <Badge variant={isLive ? 'success' : 'neutral'} size="xs">
            {isLive ? 'LIVE' : 'PASSIVE'}
          </Badge>
          <Badge variant={priKey === 'high' ? 'warning' : 'neutral'} size="xs">
            {priStars} {priKey.toUpperCase()}
          </Badge>
        </div>
      </div>
      <div className="mb-3">
        <div className="h-1.5 w-full bg-[var(--color-surface-alt)] rounded-full overflow-hidden">
          <div className="h-full rounded-full transition-all duration-700" style={{ width: `${gaugeW}%`, background: gaugeColor }} />
        </div>
        <div className="flex justify-between mt-1">
          <span className="font-mono text-sm font-semibold">{u.best_sharpe != null ? u.best_sharpe.toFixed(4) : '—'}</span>
          <span className="text-[9px] uppercase text-[var(--color-text-muted)]">Best Sharpe</span>
        </div>
      </div>
      <div className="grid grid-cols-3 gap-2 text-center">
        <div>
          <div className="font-mono text-sm font-semibold">{(u.total_experiments ?? 0).toLocaleString()}</div>
          <div className="text-[9px] uppercase text-[var(--color-text-muted)]">Experiments</div>
        </div>
        <div>
          <div className="font-mono text-sm font-semibold">{u.experiments_today ?? 0}</div>
          <div className="text-[9px] uppercase text-[var(--color-text-muted)]">Today</div>
        </div>
        <div>
          <div className="font-mono text-sm font-semibold">{keepRate.toFixed(1)}%</div>
          <div className="text-[9px] uppercase text-[var(--color-text-muted)]">Keep Rate</div>
        </div>
      </div>
      {stratKeys.length > 0 && (
        <div className="flex flex-wrap gap-1 mt-2">
          {stratKeys.slice(0, 6).map(k => (
            <Badge key={k} variant="neutral" size="xs">{k.replace(/_/g, ' ')}</Badge>
          ))}
        </div>
      )}
    </div>
  )
}

function UniverseGrid({ universes }: { universes: UniverseInfo[] }) {
  if (!universes.length) return <div className="text-sm text-[var(--color-text-muted)]">No universes configured</div>
  return (
    <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-3">
      {universes.map(u => <UniverseCard key={u.id} u={u} />)}
    </div>
  )
}

// ── Strategy × Universe Heatmap ─────────────────────────────────
function sharpeHeatColor(s: number | null | undefined): string {
  if (s == null) return 'transparent'
  if (s < 0) return 'rgba(239,68,68,0.2)'
  if (s < 0.3) return 'rgba(234,179,8,0.2)'
  if (s < 0.6) return 'rgba(34,197,94,0.2)'
  return 'rgba(59,130,246,0.3)'
}

function SharpeHeatmap({ universes, leaderboard }: { universes: UniverseInfo[]; leaderboard: LeaderboardEntry[] }) {
  const { strats, unis, lookup } = useMemo(() => {
    const stratSet = new Set<string>()
    const uniSet = new Set<string>()
    const lk: Record<string, Record<string, number>> = {}

    for (const u of universes) {
      if (u.id) uniSet.add(u.id)
      if (u.strategies) {
        for (const [s, info] of Object.entries(u.strategies)) {
          stratSet.add(s)
          if (!lk[s]) lk[s] = {}
          if (info.best_sharpe != null) lk[s][u.id!] = info.best_sharpe
        }
      }
    }
    for (const row of leaderboard) {
      if (row.strategy) stratSet.add(row.strategy)
      if (row.universe) uniSet.add(row.universe)
      if (row.strategy && row.universe && row.sharpe != null) {
        if (!lk[row.strategy]) lk[row.strategy] = {}
        if (lk[row.strategy][row.universe] == null || row.sharpe > lk[row.strategy][row.universe]) {
          lk[row.strategy][row.universe] = row.sharpe
        }
      }
    }
    return { strats: [...stratSet].sort(), unis: [...uniSet].sort(), lookup: lk }
  }, [universes, leaderboard])

  if (!strats.length || !unis.length) return null

  return (
    <div className="bg-[var(--color-surface)] border border-[var(--color-border)] rounded-xl dash-card overflow-hidden">
      <div className="p-4 border-b border-[var(--color-border)]">
        <h3 className="text-[10px] uppercase tracking-wider text-[var(--color-text-muted)] font-medium">🧬 Sharpe Heatmap — strategy × universe</h3>
      </div>
      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b border-[var(--color-border)]">
              <th className="text-left text-[10px] uppercase tracking-wider text-[var(--color-text-muted)] font-medium px-3 py-2">Strategy</th>
              {unis.map(u => (
                <th key={u} className="text-center text-[10px] uppercase tracking-wider text-[var(--color-text-muted)] font-medium px-2 py-2 whitespace-nowrap">
                  {u.replace(/_/g, ' ')}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {strats.map(s => (
              <tr key={s} className="border-b border-[var(--color-border)]">
                <td className="px-3 py-2 text-xs whitespace-nowrap">{s.replace(/_/g, ' ')}</td>
                {unis.map(u => {
                  const val = lookup[s]?.[u] ?? null
                  return (
                    <td key={u} className="px-2 py-2 text-center font-mono text-xs" style={{ background: sharpeHeatColor(val) }}>
                      {val != null ? val.toFixed(2) : '—'}
                    </td>
                  )
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}

// ── Leaderboard ─────────────────────────────────────────────────
function LeaderboardTable({ entries }: { entries: LeaderboardEntry[] }) {
  if (!entries.length) return <div className="text-sm text-[var(--color-text-muted)]">No leaderboard data yet</div>
  return (
    <div className="bg-[var(--color-surface)] border border-[var(--color-border)] rounded-xl dash-card overflow-hidden">
      <div className="p-4 border-b border-[var(--color-border)]">
        <h3 className="text-[10px] uppercase tracking-wider text-[var(--color-text-muted)] font-medium">🏆 Leaderboard — best combos by Sharpe</h3>
      </div>
      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b border-[var(--color-border)]">
              <th className="text-left text-[10px] uppercase tracking-wider text-[var(--color-text-muted)] font-medium px-4 py-2">#</th>
              <th className="text-left text-[10px] uppercase tracking-wider text-[var(--color-text-muted)] font-medium px-4 py-2">Strategy</th>
              <th className="text-left text-[10px] uppercase tracking-wider text-[var(--color-text-muted)] font-medium px-4 py-2">Universe</th>
              <th className="text-right text-[10px] uppercase tracking-wider text-[var(--color-text-muted)] font-medium px-4 py-2">Sharpe</th>
              <th className="text-right text-[10px] uppercase tracking-wider text-[var(--color-text-muted)] font-medium px-4 py-2">Trades</th>
              <th className="text-right text-[10px] uppercase tracking-wider text-[var(--color-text-muted)] font-medium px-4 py-2">Max DD</th>
              <th className="text-right text-[10px] uppercase tracking-wider text-[var(--color-text-muted)] font-medium px-4 py-2">Experiments</th>
            </tr>
          </thead>
          <tbody>
            {entries.map((row, i) => {
              const s = row.sharpe ?? 0
              const color = s >= 0.5 ? 'var(--color-green)' : s >= 0.3 ? 'var(--color-amber, #eab308)' : 'var(--color-red)'
              return (
                <tr key={`${row.strategy}-${row.universe}`} data-testid="leaderboard-row" className="border-b border-[var(--color-border)] hover:bg-[var(--color-surface-alt)] transition-colors">
                  <td className="px-4 py-2 font-mono text-xs text-[var(--color-text-muted)]">{i + 1}</td>
                  <td className="px-4 py-2 text-xs font-medium">{(row.strategy ?? '').replace(/_/g, ' ')}</td>
                  <td className="px-4 py-2">
                    <Badge variant="neutral" size="xs">{row.universe ?? '—'}</Badge>
                  </td>
                  <td className="px-4 py-2 text-right font-mono text-xs font-semibold" style={{ color }}>{row.sharpe != null ? row.sharpe.toFixed(4) : '—'}</td>
                  <td className="px-4 py-2 text-right font-mono text-xs">{row.trades ?? '—'}</td>
                  <td className="px-4 py-2 text-right font-mono text-xs">{row.max_dd_pct != null ? `${row.max_dd_pct.toFixed(1)}%` : '—'}</td>
                  <td className="px-4 py-2 text-right font-mono text-xs">{row.total_experiments ?? '—'}</td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>
    </div>
  )
}

// ── Live Feed ───────────────────────────────────────────────────
function LiveFeed({ experiments }: { experiments: Experiment[] }) {
  const items = experiments.slice(0, 30)
  if (!items.length) return <div className="text-sm text-[var(--color-text-muted)]">No recent experiments</div>
  return (
    <div className="bg-[var(--color-surface)] border border-[var(--color-border)] rounded-xl dash-card overflow-hidden">
      <div className="p-4 border-b border-[var(--color-border)]">
        <h3 className="text-[10px] uppercase tracking-wider text-[var(--color-text-muted)] font-medium">🧪 Live Feed — recent experiments</h3>
      </div>
      <div className="max-h-[400px] overflow-y-auto">
        {items.map((e) => {
          const isKept = (e.status ?? '').includes('kept')
          return (
            <div key={e.id} className={`flex items-center justify-between px-4 py-2 border-b border-[var(--color-border)] text-xs hover:bg-[var(--color-surface-alt)] transition-colors ${isKept ? 'bg-[var(--color-green)]/5' : ''}`}>
              <div className="flex items-center gap-2 min-w-0">
                <span className={`inline-block w-1.5 h-1.5 rounded-full flex-shrink-0 ${isKept ? 'bg-[var(--color-green)]' : 'bg-[var(--color-red)]'}`} />
                <span className="font-medium truncate">{(e.strategy ?? '').replace(/_/g, ' ')}</span>
                {e.universe && (
                  <Badge variant="neutral" size="xs" className="flex-shrink-0">{e.universe}</Badge>
                )}
              </div>
              <div className="flex items-center gap-3 flex-shrink-0 ml-2">
                <span className="font-mono font-semibold">{e.sharpe != null ? e.sharpe.toFixed(4) : '—'}</span>
                <span className="text-[var(--color-text-muted)]">{fmtRelativeTime(e.created_at)}</span>
              </div>
            </div>
          )
        })}
      </div>
    </div>
  )
}

// ── Main Tab ────────────────────────────────────────────────────
export function ResearchTab() {
  const overview = useResearchOverview(true)
  const leaderboard = useResearchLeaderboard(true)
  const summary = useResearchSummary(true)
  const strategies = useResearchStrategies(true)
  const brain = useResearchBrain(true)
  const discoveries = useResearchDiscoveries(true)
  const experiments = useResearchExperiments({ limit: 50 }, true)

  if (!summary.data && !overview.data) return <Skeleton className="h-96" />

  const universes = overview.data?.universes ?? []
  const engine = overview.data?.engine
  const lbEntries = leaderboard.data?.leaderboard ?? []
  const expRows = experiments.data?.experiments ?? []

  return (
    <div className="space-y-4 md:space-y-6 stagger">
      <div className="animate-in">
        <PendingPromotionsWidget />
      </div>

      {engine && (
        <div className="animate-in">
          <EngineHero engine={engine} />
        </div>
      )}

      {!engine && summary.data && (
        <div className="animate-in">
          <SectionBoundary title="Summary">
            <SummaryCards data={summary.data} />
          </SectionBoundary>
        </div>
      )}

      <div className="animate-in">
        <SectionBoundary title="Coverage">
          <CoverageMatrix enabled={true} />
        </SectionBoundary>
      </div>

      {universes.length > 0 && (
        <div className="animate-in">
          <SectionBoundary title="🔬 Universes">
            <UniverseGrid universes={universes} />
          </SectionBoundary>
        </div>
      )}

      {universes.length > 0 && lbEntries.length > 0 && (
        <div className="animate-in">
          <SharpeHeatmap universes={universes} leaderboard={lbEntries} />
        </div>
      )}

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        {lbEntries.length > 0 && (
          <div className="animate-in">
            <LeaderboardTable entries={lbEntries} />
          </div>
        )}
        {expRows.length > 0 && (
          <div className="animate-in">
            <LiveFeed experiments={expRows} />
          </div>
        )}
      </div>

      <div className="animate-in">
        <SectionBoundary title="Sharpe Trajectory">
          <SharpeChart />
        </SectionBoundary>
      </div>

      {strategies.data?.strategies && strategies.data.strategies.length > 0 && (
        <div className="animate-in">
          <SectionBoundary title="Strategy Breakdown">
            <StrategyGrid strategies={strategies.data.strategies} />
          </SectionBoundary>
        </div>
      )}

      <div className="animate-in">
        <SectionBoundary title="Experiments">
          <ExperimentsTable />
        </SectionBoundary>
      </div>

      {brain.data?.params && brain.data.params.length > 0 && (
        <div className="animate-in">
          <SectionBoundary title="Brain — Parameters Tested">
            <BrainTable params={brain.data.params} />
          </SectionBoundary>
        </div>
      )}

      {discoveries.data?.discoveries && discoveries.data.discoveries.length > 0 && (
        <div className="animate-in">
          <SectionBoundary title="Paper Discoveries">
            <DiscoveryCards discoveries={discoveries.data.discoveries} />
          </SectionBoundary>
        </div>
      )}

      <div className="animate-in">
        <SectionBoundary title="Paper Trading Progress">
          <PaperProgressPanel />
        </SectionBoundary>
      </div>
    </div>
  )
}
